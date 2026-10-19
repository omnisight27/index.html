// OmniSight picks page — renders the autonomous NexIntel loop output.
// Reads data/latest.json (committed by GitHub Actions 4x daily).

(function () {
  'use strict';

  function pickCard(p, side) {
    var cls = side === 'long' ? 'alert-buy' : 'alert-sell';
    var arrow = side === 'long' ? '▲ LONG' : '▼ SHORT';
    var t = p.thesis || {};
    var bull = (t.bull_case || []).map(function (b) { return '<li>' + b + '</li>'; }).join('');
    var bear = (t.bear_case || []).map(function (b) { return '<li>' + b + '</li>'; }).join('');
    return '<div class="alert-card ' + cls + '">' +
      '<h4>' + arrow + ' ' + p.asset + '</h4>' +
      '<p><strong>Entry:</strong> $' + Number(p.price).toLocaleString() +
      ' &nbsp; <strong>Position:</strong> ' + (p.position_pct !== undefined ? p.position_pct + '%' : '—') +
      ' <span class="live-stamp">(Kelly)</span></p>' +
      '<p><strong>Stop:</strong> $' + (t.stop_loss !== undefined ? Number(t.stop_loss).toLocaleString() : '—') +
      ' &nbsp; <strong>Target:</strong> $' + (t.take_profit !== undefined ? Number(t.take_profit).toLocaleString() : '—') +
      ' &nbsp; <strong>R:R</strong> ' + (t.risk_reward || '—') + '</p>' +
      '<p><strong>Confidence:</strong> ' + p.confidence + '% · <strong>Horizon:</strong> ' + (t.time_horizon || 'swing') + '</p>' +
      '<p><strong>Bull case:</strong></p><ul class="thesis-list">' + bull + '</ul>' +
      '<p><strong>Bear case:</strong></p><ul class="thesis-list">' + bear + '</ul>' +
      '<p class="live-stamp">' + p.reason + '</p></div>';
  }

  function init() {
    fetch('data/latest.json', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (d) {
        var stamp = document.getElementById('picks-stamp');
        if (stamp) {
          var dt = new Date(d.generated);
          stamp.textContent = 'Generated ' + dt.toLocaleString() + ' · autonomous NexIntel loop (4x daily)';
        }
        var longs = document.getElementById('picks-longs');
        if (longs) {
          longs.innerHTML = d.longs.length
            ? d.longs.map(function (p) { return pickCard(p, 'long'); }).join('')
            : '<div class="card"><p>No long signals right now.</p></div>';
        }
        var shorts = document.getElementById('picks-shorts');
        if (shorts) {
          shorts.innerHTML = d.shorts.length
            ? d.shorts.map(function (p) { return pickCard(p, 'short'); }).join('')
            : '<div class="card"><p>No short signals right now.</p></div>';
        }
        var ms = document.getElementById('model-stats');
        if (ms) {
          var tr = d.track_record || {};
          var er = d.model_error || {};
          ms.innerHTML =
            '<p><strong>Win rate:</strong> ' + (tr.win_rate !== null && tr.win_rate !== undefined ? tr.win_rate + '%' : 'warming up') + '</p>' +
            '<p><strong>Picks scored:</strong> ' + (tr.total || 0) + ' (' + (tr.wins || 0) + 'W / ' + (tr.losses || 0) + 'L)</p>' +
            '<p><strong>Mean abs error:</strong> ' + (er.mae !== null && er.mae !== undefined ? er.mae + '%' : '—') + '</p>' +
            '<p><strong>Within 5% target:</strong> ' + (er.within_5pct_rate !== null && er.within_5pct_rate !== undefined ? er.within_5pct_rate + '%' : '—') + '</p>' +
            '<p class="live-stamp">Target: 5% margin of error · weights re-optimized every run via row echelon form</p>';
        }
        var ps = document.getElementById('psychology');
        if (ps && d.psychology) {
          ps.innerHTML = '<p><strong>Regime:</strong> ' + (d.regime || '—') + '</p>' +
            '<p><strong>Fear &amp; Greed:</strong> ' + d.psychology.fear_greed +
            ' (' + (d.psychology.delta >= 0 ? '+' : '') + d.psychology.delta + ')</p>' +
            '<p><strong>Kelly size:</strong> ' + (d.kelly_pct !== undefined ? d.kelly_pct + '%' : '—') + '</p>' +
            d.psychology.notes.map(function (n) { return '<p>· ' + n + '</p>'; }).join('');
        }
        var cw = document.getElementById('contracts');
        if (cw) {
          cw.innerHTML = d.contracts_watch && d.contracts_watch.length
            ? d.contracts_watch.map(function (c) { return '<p>· ' + c + '</p>'; }).join('')
            : '<p class="live-stamp">No mega-awards flagged this run.</p>';
        }
      })
      .catch(function () {
        var stamp = document.getElementById('picks-stamp');
        if (stamp) stamp.textContent = 'Picks not yet generated — the NexIntel loop runs every 6 hours.';
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
