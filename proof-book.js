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
      : e.kind === 'setup' ? '🔭 SETUP'
      : e.kind === 'pattern_confluence' ? '📊 CLUSTER'
      : e.kind === 'cycle' ? '🔄 CYCLE'
      : e.kind === 'cmi_regime' ? '🧲 CMI'
      : e.kind === 'cmi_confluence' ? '🧲 CMI CONF'
      : e.kind === 'cmi_trap' ? '🧲 CMI TRAP'
      : e.kind === 'pattern' ? '📊 PATTERN' : '⚡ SIGNAL';
    var what = (e.kind === 'pattern' || e.kind === 'pattern_confluence') && e.pattern
      ? ' ' + esc(e.pattern) : '';
    if (e.kind === 'cycle' && e.variable) {
      what = ' ' + esc(e.variable) + '/' + esc(e.band || '');
    }
    if ((e.kind === 'cmi_regime' || e.kind === 'cmi_confluence' ||
         e.kind === 'cmi_trap') && e.band) {
      what = ' ' + esc(e.band);
    }
    return kind + what + ' ' + esc(e.asset) + (e.direction === 'SHORT' ? ' (short)' : '');
  }

  function resultCell(e) {
    if (e.status === 'open') {
      if (e.kind === 'setup' && !e.triggered) return '⏳ Waiting for the trigger';
      return '⏳ Still open';
    }
    var detail = fmtPct(e.result_pct) + ' (' + fmtMoney(e.paper_pnl || 0) + ')';
    if (e.cmi_exit) return '⌛ CMI exit trigger ' + detail;
    if (e.pattern_expired || e.cycle_expired || e.cmi_expired) return '⌛ Horizon close ' + detail;
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
    var type = c.type === 'mover' ? 'Mover' : c.type === 'setup' ? 'Setup'
      : c.type === 'pattern_confluence' ? 'Pattern cluster'
      : c.type === 'pattern' ? 'Pattern' : 'Signal';
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
    html += renderPatternScoreboard(st);
    html += renderCycleScoreboard(st);
    html += renderCmiScoreboard(st);

    el.innerHTML = html;
  }

  // Pattern scoreboard (research track, Phase 2): per-pattern shadow
  // results — win rate, Wilson 95% lower bound, expectancy per $1,000
  // paper. The CI is shown, never gated; promotion is Nic's call.
  function renderPatternScoreboard(st) {
    var rows = ((st && st.pattern_scoreboard) || [])
      .filter(function (r) { return r.graded > 0; });
    if (!rows.length) return '';
    function cell(v, suffix) {
      if (v === null || v === undefined) return '—';
      return v + (suffix || '');
    }
    function money(v) {
      if (v === null || v === undefined) return '—';
      return (v > 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);
    }
    var body = rows.map(function (r) {
      return '<tr><td>' + esc(r.pattern) + '</td><td>' + esc(r.direction) +
        '</td><td>' + esc(r.horizon) + '</td><td>' + r.graded + '</td><td>' +
        cell(r.win_rate, '%') + '</td><td>' + cell(r.wilson_lower_95, '%') +
        '</td><td>' + money(r.expectancy_per_1000) + '</td></tr>';
    }).join('');
    return '<details style="margin-top:16px;"><summary><strong>Pattern scoreboard (research)</strong> — shadow-graded pattern calls, 6h/24h horizons, $1,000 paper each</summary>' +
      '<p>Wilson 95% lower bound is shown, not gated — promotion is Nic\'s call. Expectancy is $ per $1,000 paper; the +$20 bar is a Phase 3 question.</p>' +
      '<table class="report-table"><thead><tr><th>Pattern</th><th>Dir</th><th>Horizon</th><th>Graded</th><th>Win rate</th><th>Wilson 95% lower</th><th>Expectancy / $1k</th></tr></thead>' +
      '<tbody>' + body + '</tbody></table></details>';
  }

  // Cycle scoreboard (research track, Phase 2): per-cycle-cell shadow
  // results — win rate vs the all-days base rate and the variable's
  // control band, Wilson 95% lower bound, expectancy per $1,000
  // paper. The CI is shown, never gated; promotion is Nic's call.
  // Halving rows are cross-asset evidence only (BTC itself is n≤4
  // per band) — the note says so on the row.
  function renderCycleScoreboard(st) {
    var rows = ((st && st.cycle_scoreboard) || [])
      .filter(function (r) { return r.graded > 0; });
    if (!rows.length) return '';
    function cell(v, suffix) {
      if (v === null || v === undefined) return '—';
      return v + (suffix || '');
    }
    function money(v) {
      if (v === null || v === undefined) return '—';
      return (v > 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);
    }
    var body = rows.map(function (r) {
      return '<tr><td>' + esc(r.variable) + '</td><td>' + esc(r.band) +
        '</td><td>' + esc(r.direction) + '</td><td>' + esc(r.horizon) +
        '</td><td>' + r.graded + '</td><td>' + cell(r.win_rate, '%') +
        '</td><td>' + cell(r.base_rate, '%') + '</td><td>' +
        cell(r.control_rate, '%') + '</td><td>' +
        cell(r.wilson_lower_95, '%') + '</td><td>' +
        money(r.expectancy_per_1000) + '</td></tr>' +
        (r.note ? '<tr><td colspan="10" class="live-stamp">' +
          esc(r.note) + '</td></tr>' : '');
    }).join('');
    return '<details style="margin-top:16px;"><summary><strong>Cycle scoreboard (research)</strong> — shadow-graded cycle calls, 24h/7d horizons, $1,000 paper each</summary>' +
      '<p>Each cell is graded against the all-days base rate for its horizon and its variable\'s control band (neutral Fear &amp; Greed, flat dominance). Wilson 95% lower bound is shown, not gated — promotion is Nic\'s call. Expectancy is $ per $1,000 paper; the +$20 bar is a Phase 3 question.</p>' +
      '<table class="report-table"><thead><tr><th>Variable</th><th>Band</th><th>Dir</th><th>Horizon</th><th>Graded</th><th>Win rate</th><th>Base rate</th><th>Control</th><th>Wilson 95% lower</th><th>Expectancy / $1k</th></tr></thead>' +
      '<tbody>' + body + '</tbody></table></details>';
  }

  // CMI scoreboard (research track, Phase 2): per-CMI-cell shadow
  // results — regime bands (24h/7d, MANIP as both LONG and SHORT
  // cells), confluence flips (6h/24h, with CMI > 0.50 exit-trigger
  // closes counted), trap flips (6h/24h). Win rate, Wilson 95%
  // lower bound, expectancy per $1,000 paper, occurrences per week.
  // The CI is shown, never gated; promotion is Nic's call. CMI
  // measures deviation from predictable price behavior — a proxy,
  // not a detector of institutional intent.
  function renderCmiScoreboard(st) {
    var rows = ((st && st.cmi_scoreboard) || [])
      .filter(function (r) { return r.graded > 0; });
    if (!rows.length) return '';
    function cell(v, suffix) {
      if (v === null || v === undefined) return '—';
      return v + (suffix || '');
    }
    function money(v) {
      if (v === null || v === undefined) return '—';
      return (v > 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);
    }
    var kindWords = { cmi_regime: 'Regime', cmi_confluence: 'Confluence',
      cmi_trap: 'Trap' };
    var body = rows.map(function (r) {
      return '<tr><td>' + esc(kindWords[r.kind] || r.kind) + '</td><td>' +
        esc(r.band) + '</td><td>' + esc(r.direction) + '</td><td>' +
        esc(r.horizon) + '</td><td>' + r.graded + '</td><td>' +
        cell(r.win_rate, '%') + '</td><td>' + cell(r.wilson_lower_95, '%') +
        '</td><td>' + money(r.expectancy_per_1000) + '</td><td>' +
        r.cmi_exits + '</td><td>' +
        cell(r.occurrences_per_week, '/wk') +
        (r.insufficient_frequency ? ' ⚠️ low frequency' : '') +
        '</td></tr>';
    }).join('');
    return '<details style="margin-top:16px;"><summary><strong>Manipulation scoreboard (research)</strong> — shadow-graded CMI calls, $1,000 paper each</summary>' +
      '<p>Regime rows grade 24h/7d; confluence and trap rows grade 6h/24h. “CMI exits” counts confluence rows closed by the CMI &gt; 0.50 exit trigger. Wilson 95% lower bound is shown, not gated — promotion is Nic\'s call. Expectancy is $ per $1,000 paper; the +$20 bar is a Phase 3 question.</p>' +
      '<table class="report-table"><thead><tr><th>Kind</th><th>Band</th><th>Dir</th><th>Horizon</th><th>Graded</th><th>Win rate</th><th>Wilson 95% lower</th><th>Expectancy / $1k</th><th>CMI exits</th><th>Frequency</th></tr></thead>' +
      '<tbody>' + body + '</tbody></table></details>';
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
