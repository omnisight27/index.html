// OmniSight Kalshi Probability Watch — renders data/kalshi.json, written
// every engine run by Scripts/kalshi.py from Kalshi's public market-data
// API (no account, read-only). Kalshi prices are the crowd's probability;
// the "our model" column is NexIntel's lognormal estimate for crypto
// threshold markets only. Paper positions ($300 notional) open when the
// model disagrees with the crowd by 10+ points, and grade at settlement.

(function () {
  'use strict';

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function pct(x) {
    return (x === null || x === undefined) ? '—' : Math.round(x * 100) + '%';
  }

  function etTime(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    if (isNaN(d)) return '—';
    return d.toLocaleString('en-US', {
      timeZone: 'America/New_York', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit'
    }) + ' ET';
  }

  function money(x) {
    if (x === null || x === undefined) return '—';
    var s = (x < 0 ? '-$' : '$') + Math.abs(x).toFixed(2);
    return s;
  }

  function render(led) {
    var el = document.getElementById('kalshi');
    if (!el) return;

    if (!led || !led.watch) {
      el.innerHTML = '<p>No Kalshi data yet — the feed lands with the ' +
        'next engine run.</p>';
      return;
    }

    var st = led.stats || {};
    var html = '';

    // --- Scoreboard: crowd baseline vs our model ---
    html += '<h4>📊 The scoreboard so far</h4><ul>';
    if (st.crowd_n) {
      html += '<li><strong>The crowd (baseline):</strong> ' + st.crowd_n +
        ' markets settled · Brier score ' + esc(st.crowd_brier) +
        (st.crowd_fav_accuracy !== undefined
          ? ' · the crowd\'s favored side won ' + st.crowd_fav_accuracy + '% of the time'
          : '') +
        '. That is the number our own estimates have to beat.</li>';
    } else {
      html += '<li><strong>The crowd (baseline):</strong> no watched markets ' +
        'have settled yet — grading starts with the first settlements.</li>';
    }
    if (st.model_n) {
      html += '<li><strong>Our model (paper):</strong> ' + st.model_n +
        ' graded · ' + st.model_w + 'W / ' + st.model_l + 'L · paper P&amp;L ' +
        money(st.model_pnl) + ' on $300 positions.</li>';
    } else {
      html += '<li><strong>Our model (paper):</strong> no graded positions yet. ' +
        'A position opens only when our estimate disagrees with Kalshi by ' +
        '10+ points — most days, it won\'t.</li>';
    }
    html += '</ul>';

    // --- Open paper positions ---
    var open = led.paper_open || [];
    if (open.length) {
      html += '<h4>🎯 Open paper positions</h4><ul>';
      html += open.map(function (p) {
        return '<li><strong>' + esc(p.side.toUpperCase()) + '</strong> @ ' +
          pct(p.entry) + ' — ' + esc(p.title) + ' <em>(our model said ' +
          pct(p.model_p) + ', Kalshi said ' + pct(p.kalshi_mid) +
          '; settles ' + etTime(p.close_time) + ')</em></li>';
      }).join('');
      html += '</ul>';
    }

    // --- Watch table ---
    var rows = Object.keys(led.watch).map(function (k) { return led.watch[k]; });
    rows.sort(function (a, b) {
      return String(a.close_time || '').localeCompare(String(b.close_time || ''));
    });
    rows = rows.slice(0, 18);
    if (rows.length) {
      html += '<h4>👀 Watching now</h4>' +
        '<table style="width:100%; border-collapse: collapse;">' +
        '<tr><th style="text-align:left;">Event</th>' +
        '<th>Kalshi</th><th>Our model</th><th>Gap</th><th>Settles</th></tr>';
      html += rows.map(function (r) {
        var gap = (r.model_p === null || r.model_p === undefined)
          ? '—'
          : ((r.model_p - r.mid) >= 0 ? '+' : '') +
            Math.round((r.model_p - r.mid) * 100) + ' pts';
        var hot = (r.model_p !== null && r.model_p !== undefined &&
          Math.abs(r.model_p - r.mid) >= 0.10) ? ' <strong>🎯</strong>' : '';
        return '<tr>' +
          '<td>' + esc(r.title) + hot + '</td>' +
          '<td style="text-align:center;">' + pct(r.mid) + '</td>' +
          '<td style="text-align:center;">' + pct(r.model_p) + '</td>' +
          '<td style="text-align:center;">' + gap + '</td>' +
          '<td style="text-align:center;">' + etTime(r.close_time) + '</td>' +
          '</tr>';
      }).join('');
      html += '</table>';
    }

    // --- Recently graded ---
    var graded = (led.graded || []).slice(-6).reverse();
    if (graded.length) {
      html += '<h4>✅ Recently settled</h4><ul>';
      html += graded.map(function (g) {
        if (g.kind === 'model') {
          return '<li>' + (g.win ? '✅ Won' : '❌ Lost') + ' ' + money(g.pnl) +
            ' — ' + esc(g.title) + ' <em>(bought ' + esc(g.side) + ' @ ' +
            pct(g.entry) + ', model said ' + pct(g.model_p) + ')</em></li>';
        }
        return '<li>Crowd said ' + pct(g.crowd_mid) + ' (' + esc(g.crowd_src) +
          ') — settled ' + (g.outcome ? '<strong>Yes</strong>' : '<strong>No</strong>') +
          ' · ' + esc(g.title) + '</li>';
      }).join('');
      html += '</ul>';
    }

    html += '<p class="live-stamp">Updated ' + etTime(led.updated) +
      ' · Prices are Kalshi\'s crowd probabilities; the model column is ' +
      'NexIntel\'s estimate on crypto thresholds only. Paper trading — ' +
      'no real money, no account.</p>';

    el.innerHTML = html;
  }

  function init() {
    if (!document.getElementById('kalshi')) return;
    fetch('data/kalshi.json', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(render)
      .catch(function () { render(null); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
