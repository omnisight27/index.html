// Economy page — live FRED indicators with layman toggles.
// Tries the live FRED CSV feed first; falls back to the baked snapshot in
// economy-data.js (fred.stlouisfed.org does not send CORS headers, so the
// fallback is the normal path in most browsers). Every card says which.
(function () {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function parseCSV(text) {
    var rows = [];
    text.split('\n').forEach(function (line) {
      var p = line.trim().split(',');
      if (p.length === 2 && p[0].match(/^\d{4}-\d{2}-\d{2}$/) && p[1] !== '.' && p[1] !== '') {
        rows.push({ d: p[0], v: parseFloat(p[1]) });
      }
    });
    return rows;
  }

  function valNear(rows, targetDate) {
    // closest row on or before targetDate
    var best = null;
    rows.forEach(function (r) {
      if (r.d <= targetDate && (!best || r.d > best.d)) best = r;
    });
    return best;
  }

  function shiftDays(iso, n) {
    var d = new Date(iso + 'T00:00:00');
    d.setDate(d.getDate() - n);
    return d.toISOString().slice(0, 10);
  }

  function fetchSeries(def) {
    var url = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id=' + def.fredId;
    var timeout = new Promise(function (_, rej) { setTimeout(function () { rej(new Error('timeout')); }, 9000); });
    return Promise.race([fetch(url).then(function (r) {
      if (!r.ok) throw new Error('http ' + r.status);
      return r.text();
    }), timeout]).then(function (text) {
      var rows = parseCSV(text);
      if (!rows.length) throw new Error('empty');
      return { rows: rows, live: true };
    });
  }

  function summarize(def, rows, live) {
    var latest = rows[rows.length - 1];
    var r30 = valNear(rows, shiftDays(latest.d, 30));
    var r90 = valNear(rows, shiftDays(latest.d, 90));
    var out = { live: live, asOf: latest.d };

    if (def.kind === 'yoy') {
      // year-over-year % from the index
      var yr = valNear(rows, shiftDays(latest.d, 365));
      var yr30 = r30 ? valNear(rows, shiftDays(r30.d, 365)) : null;
      var yr90 = r90 ? valNear(rows, shiftDays(r90.d, 365)) : null;
      out.value = yr ? (latest.v / yr.v - 1) * 100 : null;
      out.ref30 = (r30 && yr30) ? (r30.v / yr30.v - 1) * 100 : null;
      out.ref90 = (r90 && yr90) ? (r90.v / yr90.v - 1) * 100 : null;
    } else {
      out.value = latest.v;
      out.ref30 = r30 ? r30.v : null;
      out.ref90 = r90 ? r90.v : null;
    }
    return out;
  }

  function fmtVal(def, v) {
    if (v == null || isNaN(v)) return '—';
    if (def.format === 'thousands') return Math.round(v).toLocaleString('en-US');
    if (def.kind === 'index') return v.toFixed(def.decimals);
    return v.toFixed(def.decimals) + def.unit;
  }

  function direction(def, value, ref) {
    if (value == null || ref == null) return { cls: 'flat', arrow: '—', word: 'no comparison' };
    var d = value - ref;
    var tol = def.kind === 'count' ? 1000 : 0.04;
    if (Math.abs(d) <= tol) return { cls: 'flat', arrow: '→', word: 'flat' };
    return d > 0
      ? { cls: 'up', arrow: '▲', word: 'up' }
      : { cls: 'down', arrow: '▼', word: 'down' };
  }

  function card(def, s) {
    var dir = direction(def, s.value, s.ref30);
    var dir90 = direction(def, s.value, s.ref90);
    var stamp = s.live ? 'live' : 'snapshot ' + window.ECONOMY_SNAPSHOT_DATE;
    var why = def.whyNow
      ? '<h4>Why it\'s moving now</h4><p>' + esc(def.whyNow) + '</p><p class="stamp">Researched ' + esc(def.whyNowDate || '') + '</p>'
      : '<h4>Why it\'s moving now</h4><p class="hint">Research pass pending — check back soon.</p>';
    var links = (def.links || []).map(function (l) {
      return '<li><a href="' + esc(l.url) + '" target="_blank" rel="noopener">' + esc(l.label) + ' →</a></li>';
    }).join('');
    return '<div class="card" id="econ-' + def.id + '">' +
      '<h3>' + esc(def.name) + (def.turning ? ' <span class="turning-dot dot-watch" title="Turning-trigger indicator"></span>' : '') + '</h3>' +
      '<div class="econ-val">' + esc(fmtVal(def, s.value)) + '</div>' +
      '<p class="econ-dir ' + dir.cls + '">' + dir.arrow + ' ' + dir.word + ' vs 30 days ago' +
      ' <span class="stamp">(' + dir90.arrow + ' ' + dir90.word + ' vs 90d)</span></p>' +
      '<button class="econ-toggle" type="button" data-more="more-' + def.id + '">What is this? Why is it moving? +</button>' +
      '<div class="econ-more" id="more-' + def.id + '" hidden>' +
        '<h4>What it is</h4><p>' + esc(def.whatIs) + '</p>' +
        '<h4>What it means for you</h4><p>' + esc(def.meansForYou) + '</p>' +
        why +
        (links ? '<h4>Go deeper</h4><ul>' + links + '</ul>' : '') +
      '</div>' +
      '<p class="stamp">As of ' + esc(s.asOf) + ' · ' + esc(stamp) + '</p>' +
      '</div>';
  }

  function turningLens(results) {
    // Simple, honest regime read from the three turning indicators.
    var notes = [];
    var alerts = 0;
    var byId = {};
    results.forEach(function (r) { byId[r.def.id] = r; });

    var yc = byId.yield_curve;
    if (yc && yc.s.value != null) {
      if (yc.s.value < 0) { notes.push(['dot-alert', 'Yield curve is INVERTED (' + yc.s.value.toFixed(2) + 'pp) — the classic early-warning. Recession risk is elevated on a 6–18 month horizon.']); alerts += 2; }
      else if (yc.s.value < 0.5) { notes.push(['dot-watch', 'Yield curve is thin but positive (' + yc.s.value.toFixed(2) + 'pp) — no inversion alarm, but no cushion either.']); alerts += 1; }
      else { notes.push(['dot-calm', 'Yield curve is comfortably positive (' + yc.s.value.toFixed(2) + 'pp) — no inversion signal.']); }
    }
    var jc = byId.jobless;
    if (jc && jc.s.value != null && jc.s.ref30 != null) {
      var d = jc.s.value - jc.s.ref30;
      if (d > 15000) { notes.push(['dot-alert', 'Jobless claims jumping (+' + Math.round(d/1000) + 'K vs 30d ago) — layoffs accelerating.']); alerts += 2; }
      else if (d < -5000) { notes.push(['dot-calm', 'Jobless claims easing (' + Math.round(jc.s.value/1000) + 'K) — companies holding onto workers.']); }
      else { notes.push(['dot-watch', 'Jobless claims flat — labor market neither heating nor cracking.']); alerts += 1; }
    }
    var se = byId.sentiment;
    if (se && se.s.value != null) {
      if (se.s.value < 55) { notes.push(['dot-watch', 'Consumer mood is gloomy (' + se.s.value.toFixed(1) + ') — shoppers are cautious, which favors thrift over full-price retail.']); alerts += 1; }
      else { notes.push(['dot-calm', 'Consumer mood is okay (' + se.s.value.toFixed(1) + ').']); }
    }
    var head = alerts >= 3
      ? '⚠️ Caution tilt — more than one early signal is flashing. Favor cash, cheap debt paydown, and optionality on big buys.'
      : alerts >= 1
      ? '👀 Mixed — no alarm, but the radar is picking up static. Normal plans, eyes open.'
      : '✅ Calm — the early signals are quiet. Nothing in the data says brace.';
    var el = document.getElementById('turning-lens');
    if (el) {
      el.innerHTML = '<p><strong>' + esc(head) + '</strong></p>' +
        notes.map(function (n) {
          return '<p><span class="turning-dot ' + n[0] + '"></span>' + esc(n[1]) + '</p>';
        }).join('') +
        '<p class="hint">This is a read of leading signals, not a prediction and not advice. Dots with a ◉ mark the indicators feeding this lens.</p>';
    }
  }

  function render() {
    var defs = window.ECONOMY_INDICATORS || [];
    var grid = document.getElementById('econ-grid');
    var statusEl = document.getElementById('econ-status');
    var results = [];

    if (statusEl) statusEl.textContent = 'Pulling the latest numbers…';

    var jobs = defs.map(function (def) {
      return fetchSeries(def).then(function (r) {
        var s = summarize(def, r.rows, true);
        return { def: def, s: s };
      }).catch(function () {
        // snapshot fallback — never invent, always label
        var s = { value: def.value, ref30: def.ref30, ref90: def.ref90, asOf: def.asOf, live: false };
        return { def: def, s: s };
      });
    });

    Promise.all(jobs).then(function (rs) {
      results = rs;
      grid.innerHTML = rs.map(function (r) { return card(r.def, r.s); }).join('');
      var live = rs.filter(function (r) { return r.s.live; }).length;
      if (statusEl) statusEl.textContent = '';
      document.getElementById('econ-stamp').textContent =
        live + ' of ' + rs.length + ' indicators live just now; the rest show the ' +
        window.ECONOMY_SNAPSHOT_DATE + ' snapshot. Tap any card for the plain-English read.';
      grid.addEventListener('click', function (e) {
        var b = e.target.closest ? e.target.closest('.econ-toggle') : null;
        if (!b) return;
        var more = document.getElementById(b.getAttribute('data-more'));
        if (!more) return;
        var open = more.hidden;
        more.hidden = !open;
        b.textContent = b.textContent.replace(open ? '+' : '−', open ? '−' : '+');
      });
      turningLens(rs);
      var reg = window.ECONOMY_REGIME || {};
      document.getElementById('regime-text').textContent = reg.text || '';
      document.getElementById('regime-date').textContent = reg.date ? 'Regime read: ' + reg.date : '';
    });
  }

  window.OmniWall.init(function () { render(); });
})();
