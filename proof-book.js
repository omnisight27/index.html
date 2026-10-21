// OmniSight Proof Book — the public paper-trading record of every
// intraday call (signals + movers with plans), graded by the engine
// itself into data/intraday.json under `proof`. Plus a one-line summary
// of the swing engine's scored picks from data/history.json.
// Paper convention: every call is graded as if $1,000 rode it.

(function () {
  'use strict';

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function fmtPrice(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    if (v >= 1000) return '$' + v.toLocaleString('en-US', { maximumFractionDigits: 2 });
    if (v >= 1) return '$' + v.toFixed(2);
    if (v >= 0.01) return '$' + v.toFixed(4);
    return '$' + parseFloat(v.toPrecision(3)).toString();
  }

  function fmtMoney(v) {
    var sign = v < 0 ? '-$' : '+$';
    return sign + Math.abs(v).toLocaleString('en-US', { maximumFractionDigits: 0 });
  }

  function fmtPct(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    return (v > 0 ? '+' : '') + v.toFixed(1) + '%';
  }

  function fmtWhen(ts) {
    var d = new Date(ts);
    if (isNaN(d.getTime())) return '—';
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ', ' +
      d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
  }

  function callLabel(e) {
    var kind = e.kind === 'mover' ? '🚀 MOVER'
      : e.kind === 'setup' ? '🔭 SETUP' : '⚡ SIGNAL';
    return kind + ' ' + esc(e.asset) + (e.direction === 'SHORT' ? ' (short)' : '');
  }

  function resultCell(e) {
    if (e.status === 'open') {
      if (e.kind === 'setup' && !e.triggered) return '⏳ Waiting for the trigger';
      return '⏳ Still open';
    }
    var detail = fmtPct(e.result_pct) + ' (' + fmtMoney(e.paper_pnl || 0) + ')';
    if (e.status === 'won') return '🎯 Target hit ' + detail;
    if (e.status === 'lost') return '🛑 Stopped out ' + detail;
    if (e.never_triggered) return '⌛ Trigger never hit — no trade taken ' + detail;
    return '⌛ Ran out of time ' + detail;
  }

  // The regime dimension is always shown with the site's plain
  // labels — internal cell tags (risk_on / neutral / risk_off) are
  // mapped here and never rendered raw.
  var REGIME_LABEL = {
    risk_on: '🟢 Conditions favor buying',
    risk_off: '🔴 Conditions favor caution',
    neutral: '🟡 Mixed conditions',
    unknown: 'Regime not recorded'
  };

  function cellLabel(c) {
    var type = c.type === 'mover' ? 'Mover' : c.type === 'setup' ? 'Setup' : 'Signal';
    var band = c.band && c.band !== 'na' ? ' · ' + esc(c.band) + ' confidence' : '';
    return type + ' · ' + esc(c.direction) + band + ' · ' +
      (REGIME_LABEL[c.regime] || REGIME_LABEL.unknown);
  }

  function renderCells(st) {
    var cells = (st && st.cells) || [];
    var rows = cells.filter(function (c) { return c.graded > 0 || c.open > 0; });
    if (!rows.length) return '';
    var body = rows.map(function (c) {
      return '<tr><td>' + cellLabel(c) + '</td><td>' + c.wins + '</td><td>' +
        c.losses + '</td><td>' + c.expired + '</td><td>' + c.open + '</td><td>' +
        c.graded + '</td><td>' +
        (c.win_rate === null || c.win_rate === undefined ? '—' : c.win_rate + '%') +
        '</td></tr>';
    }).join('');
    var html = '<details style="margin-top:16px;"><summary><strong>Win/loss by scenario' +
      '</strong> — every kind of call, graded separately (all-time; counts include calls' +
      ' that have rolled off the list above)</summary>' +
      '<table class="report-table"><thead><tr><th>Scenario</th><th>Won</th><th>Lost</th>' +
      '<th>Expired</th><th>Open</th><th>Graded</th><th>Win rate</th></tr></thead>' +
      '<tbody>' + body + '</tbody></table>';
    var flags = (st && st.flagged_cells) || [];
    if (flags.length) {
      html += '<p>' + flags.map(function (f) {
        return '⭐ Review candidate (' + (f.candidate === 'include' ? 'inclusion' : 'exclusion') +
          '): ' + cellLabel(f) + ' — ' + f.win_rate + '% over ' + f.graded +
          ' graded calls. Recommendation only — no alert rule changes on its own.';
      }).join('<br>') + '</p>';
    }
    return html + '</details>';
  }

  function render(proof, swing) {
    var el = document.getElementById('proof-book');
    if (!el) return;
    var html = '';

    if (swing && swing.total) {
      html += '<p><strong>Model picks (swing engine):</strong> ' + swing.total +
        ' picks scored — ' + swing.wins + ' won, ' + swing.losses + ' lost (' +
        swing.win_rate + '% win rate).</p>';
    }

    var rec = (proof && proof.record) || [];
    var st = (proof && proof.stats) || null;

    if (!st || (!rec.length && !st.graded && !st.open)) {
      html += '<p>The book is open — calls will appear here as the system makes them ' +
        'and grade themselves over the following hours.</p>';
      el.innerHTML = html;
      return;
    }

    if (st.graded > 0) {
      html += '<p>Paper record — every call graded as if <strong>$1,000</strong> rode it. ' +
        '<strong>' + st.graded + ' calls graded:</strong> ' + st.wins + ' hit target, ' +
        st.losses + ' hit stop, ' + st.expired + ' ran out of time.' +
        (st.open ? ' ' + st.open + ' still open.' : '') +
        ' <strong>Paper P&amp;L: ' + fmtMoney(st.paper_pnl_total) + '.</strong></p>';
    } else {
      html += '<p>Paper record — every call graded as if <strong>$1,000</strong> rode it. ' +
        st.open + (st.open === 1 ? ' call' : ' calls') + ' on the books, still open — ' +
        'they grade themselves over the following hours.</p>';
    }

    if (rec.length) {
      var rows = rec.slice(0, 20).map(function (e) {
        return '<tr><td>' + fmtWhen(e.ts_open) + '</td><td>' + callLabel(e) + '</td><td>' +
          fmtPrice(e.entry) + '</td><td>' + fmtPrice(e.take_profit) + '</td><td>' +
          fmtPrice(e.stop_loss) + '</td><td>' + resultCell(e) + '</td></tr>';
      }).join('');
      html += '<table class="report-table"><thead><tr><th>When</th><th>Call</th>' +
        '<th>Bought (paper) at</th><th>Target</th><th>Stop</th><th>Result</th></tr></thead>' +
        '<tbody>' + rows + '</tbody></table>';
    }

    html += renderCells(st);

    el.innerHTML = html;
  }

  function init() {
    if (!document.getElementById('proof-book')) return;
    var proofP = fetch('data/intraday.json', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { return (d && d.proof) || null; })
      .catch(function () { return null; });
    var swingP = fetch('data/history.json', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (h) { return (h && h.stats) || null; })
      .catch(function () { return null; });
    Promise.all([proofP, swingP]).then(function (res) { render(res[0], res[1]); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
