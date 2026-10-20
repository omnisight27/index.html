// OmniSight Portfolio Tracker — on-device holdings, live CoinGecko valuation,
// and a regime-based allocation framework ("what you have vs what today's
// conditions suggest"). Holdings live ONLY in localStorage (nx_portfolio);
// nothing is sent anywhere. Prices: CoinGecko for crypto, manual for the rest.
(function () {
  'use strict';

  var STORAGE_KEY = 'nx_portfolio';
  var holdings = [];
  var priceMap = {};        // SYM -> price (largest-market-cap coin wins)
  var regime = 'NEUTRAL';   // fallback until latest.json loads
  var regimeLabelText = '🟡 Mixed conditions';
  var regimeKnown = false;

  var REGIME_LABELS = {
    'RISK_ON': '🟢 Conditions favor buying',
    'RISK_OFF': '🔴 Conditions favor caution',
    'NEUTRAL': '🟡 Mixed conditions'
  };
  // Framework targets by regime: BTC weight range + single non-BTC asset cap.
  var TARGETS = {
    'RISK_ON':  { btcLow: 40, btcHigh: 50, altCap: 25 },
    'NEUTRAL':  { btcLow: 50, btcHigh: 60, altCap: 20 },
    'RISK_OFF': { btcLow: 60, btcHigh: 70, altCap: 15 }
  };

  // Fixed slice colors for Nic's known assets; others get a stable hash color.
  var FIXED_COLORS = {
    'BTC': '#f7931a', 'XRP': '#64748b', 'ETH': '#627eea',
    'SOL': '#9945ff', 'XLM': '#0d9488', 'ARB': '#38bdf8'
  };
  var HASH_PALETTE = ['#f59e0b', '#22c55e', '#ec4899', '#a78bfa', '#f43f5e',
                      '#14b8a6', '#eab308', '#60a5fa', '#fb923c', '#4ade80'];
  function colorFor(sym) {
    if (FIXED_COLORS[sym]) return FIXED_COLORS[sym];
    var h = 0;
    for (var i = 0; i < sym.length; i++) h = (h * 31 + sym.charCodeAt(i)) >>> 0;
    return HASH_PALETTE[h % HASH_PALETTE.length];
  }

  function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'); }
  function fmtMoney(n) { return '$' + Math.round(n).toLocaleString('en-US'); }
  function fmtPrice(p) {
    if (p >= 100) return '$' + p.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (p >= 1) return '$' + p.toFixed(2);
    if (p >= 0.01) return '$' + p.toFixed(4);
    return '$' + p.toFixed(6);
  }
  function fmtPct(n) { return (Math.round(n * 10) / 10).toFixed(1) + '%'; }

  // ---------- Holdings entry (debt.js patterns: render rows, read rows, autosave) ----------

  function holdingRow(h, i) {
    h = h || {};
    var div = document.createElement('div');
    div.className = 'debt-row debt-card pf-row';
    div.innerHTML =
      '<div class="debt-grid">' +
      '<input type="text" placeholder="Symbol (e.g. BTC)" value="' + esc(h.symbol) + '" data-f="symbol" list="pf-symbols" autocomplete="off">' +
      '<input type="number" placeholder="Units you hold" value="' + esc(h.units) + '" min="0" step="any" data-f="units">' +
      '<input type="number" placeholder="Avg cost $ (optional)" value="' + esc(h.avg_cost) + '" min="0" step="any" data-f="avg_cost">' +
      '<input type="number" placeholder="Manual price $ (optional)" value="' + esc(h.manual_price) + '" min="0" step="any" data-f="manual_price">' +
      '</div>' +
      '<div class="debt-extra">' +
        '<label><input type="checkbox" data-f="staked"' + (h.staked ? ' checked' : '') + '> Staked</label>' +
        (h.source === 'coinbase' ? ' <span class="asset-badge badge-crypto">⟳ Synced from Coinbase</span>' : '') +
        '<input type="hidden" data-f="source" value="' + esc(h.source || '') + '">' +
      '</div>' +
      '<button class="btn-small" data-del="' + i + '">✕ Remove</button>';
    return div;
  }

  function render() {
    var list = document.getElementById('pf-list');
    list.innerHTML = '';
    holdings.forEach(function (h, i) { list.appendChild(holdingRow(h, i)); });
    if (!holdings.length) {
      list.innerHTML = '<p class="hint">No holdings yet — add your first asset below and your book gets valued live, checked against today\'s conditions.</p>';
    }
  }

  function readRows(keepAll) {
    var rows = document.querySelectorAll('.pf-row');
    var out = [];
    rows.forEach(function (r) {
      var h = {};
      r.querySelectorAll('input').forEach(function (inp) {
        var f = inp.dataset.f;
        if (!f) return;
        h[f] = inp.type === 'checkbox' ? inp.checked : inp.value;
      });
      h.symbol = String(h.symbol || '').toUpperCase().trim();
      // Keep any row the user has started filling in (never lose an edit);
      // keepAll (used by delete) preserves blank rows so indexes line up.
      if (keepAll || h.symbol || h.units || h.avg_cost || h.manual_price) out.push(h);
    });
    holdings = out;
    return out;
  }

  function save() {
    readRows();
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(holdings));
      var el = document.getElementById('pf-saved');
      if (el) el.textContent = 'Saved ✓ ' + new Date().toLocaleTimeString();
    } catch (e) {}
  }

  function load() {
    try {
      var saved = localStorage.getItem(STORAGE_KEY);
      if (saved) holdings = JSON.parse(saved) || [];
    } catch (e) { holdings = []; }
  }

  // ---------- Pricing ----------

  // Price resolution: manual entry wins (it's the override / the only stock
  // feed), then the live CoinGecko price, then avg cost as a stale stand-in.
  function priceFor(h) {
    var mp = parseFloat(h.manual_price);
    if (mp > 0) return { price: mp, source: 'manual' };
    var live = priceMap[String(h.symbol || '').toUpperCase()];
    if (live) return { price: live, source: 'live' };
    var ac = parseFloat(h.avg_cost);
    if (ac > 0) return { price: ac, source: 'stale' };
    return null;
  }

  // CoinGecko markets, 250/page ordered by market cap — so the FIRST time a
  // symbol appears is its largest-cap coin (wins symbol collisions). Pages
  // load one at a time and stop early once every holding is priced.
  function loadPrices() {
    var needed = {};
    holdings.forEach(function (h) {
      var s = String(h.symbol || '').toUpperCase();
      if (s && !(parseFloat(h.manual_price) > 0)) needed[s] = true;
    });
    var symbols = Object.keys(needed);
    if (!symbols.length) return Promise.resolve();
    var page = 1;
    function next() {
      if (page > 4 || symbols.every(function (s) { return priceMap[s]; })) return Promise.resolve();
      var url = 'https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd' +
                '&order=market_cap_desc&per_page=250&page=' + page + '&sparkline=false';
      return fetch(url)
        .then(function (r) { if (!r.ok) throw new Error('bad status'); return r.json(); })
        .then(function (coins) {
          (coins || []).forEach(function (c) {
            var s = String(c.symbol || '').toUpperCase();
            if (s && priceMap[s] === undefined && c.current_price) priceMap[s] = c.current_price;
          });
          page++;
          return next();
        })
        .catch(function () { /* keep whatever pages landed; fallbacks cover the rest */ });
    }
    return next();
  }

  // ---------- Regime / framework ----------

  function loadRegime() {
    return fetch('data/latest.json', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('bad status'); return r.json(); })
      .then(function (d) {
        // Unified freshness stamp (engine pull/publish times, ET)
        if (window.nxStampInto) window.nxStampInto(document.getElementById('data-stamp'), d);
        if (d && d.regime && TARGETS[d.regime]) {
          regime = d.regime;
          regimeLabelText = d.regime_label || REGIME_LABELS[d.regime];
          regimeKnown = true;
        }
      })
      .catch(function () { /* keep NEUTRAL fallback + "conditions unknown" wording */ });
  }

  function compute() {
    var t = TARGETS[regime] || TARGETS.NEUTRAL;
    var rows = [];
    holdings.forEach(function (h) {
      var sym = String(h.symbol || '').toUpperCase();
      if (!sym) return;
      var units = parseFloat(h.units) || 0;
      var pf = units > 0 ? priceFor(h) : null;
      var value = pf ? units * pf.price : null;
      rows.push({ sym: sym, units: units, staked: !!h.staked, avgCost: parseFloat(h.avg_cost) || 0,
                  price: pf, value: value, weight: 0 });
    });
    var total = rows.reduce(function (s, r) { return s + (r.value || 0); }, 0);
    rows.forEach(function (r) { r.weight = total > 0 && r.value ? (r.value / total * 100) : 0; });

    var btc = null;
    rows.forEach(function (r) { if (r.sym === 'BTC') btc = r; });
    var btcValue = btc && btc.value ? btc.value : 0;
    var btcWeight = total > 0 ? btcValue / total * 100 : 0;
    var btcGap = Math.max(0, t.btcLow / 100 * total - btcValue);

    // Target scenario: every non-BTC asset trimmed to at most the cap, the
    // freed weight lands in BTC; if BTC still lands under its target floor,
    // alts scale down proportionally until BTC reaches the floor.
    var altRaw = 0;
    var altTarget = {};
    rows.forEach(function (r) {
      if (r.sym === 'BTC' || !r.value) return;
      var w = Math.min(r.weight, t.altCap);
      altTarget[r.sym] = (altTarget[r.sym] || 0) + w;
      altRaw += w;
    });
    var btcTarget;
    var maxAlt = 100 - t.btcLow;
    if (altRaw > maxAlt && altRaw > 0) {
      var scale = maxAlt / altRaw;
      Object.keys(altTarget).forEach(function (k) { altTarget[k] *= scale; });
      btcTarget = t.btcLow;
    } else {
      btcTarget = 100 - altRaw;
    }

    // Unrealized P&L across positions where an avg cost was entered
    var costBasis = 0, pnlValue = 0, hasCost = false;
    rows.forEach(function (r) {
      if (r.avgCost > 0 && r.value != null) {
        hasCost = true;
        costBasis += r.units * r.avgCost;
        pnlValue += r.value;
      }
    });

    return { targets: t, rows: rows, total: total, btc: btc, btcWeight: btcWeight,
             btcGap: btcGap, altTarget: altTarget, btcTarget: btcTarget,
             hasCost: hasCost, costBasis: costBasis,
             pnl: hasCost ? pnlValue - costBasis : 0 };
  }

  function laneFor(r, c) {
    var t = c.targets;
    if (r.value == null) return 'No price yet — type one into Manual price';
    if (r.sym === 'BTC') {
      if (r.weight >= t.btcLow) return '✅ In lane — BTC is at/above the ' + t.btcLow + '% target floor';
      return '⚠️ Below target — model would add ~' + fmtMoney(t.btcLow / 100 * c.total - r.value);
    }
    if (r.staked) return '🔒 Staked — counts in your weights; no trim suggested (unstaking takes days)';
    if (r.weight > t.altCap) {
      return '⚠️ Over its lane — trim ~' + fmtMoney(r.value - t.altCap / 100 * c.total) +
             ' to hit the ' + t.altCap + '% cap';
    }
    return '✅ In lane';
  }

  // ---------- Charts (canvas donuts) ----------

  function drawDonut(canvas, slices, centerTop, centerBottom) {
    if (!canvas) return;
    var ctx = null;
    try { ctx = canvas.getContext('2d'); } catch (e) {}
    if (!ctx) return; // headless / unsupported — legend still carries the numbers
    var dpr = window.devicePixelRatio || 1;
    var size = 220;
    if (canvas.width !== size * dpr) { canvas.width = size * dpr; canvas.height = size * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size, size);
    var cx = size / 2, cy = size / 2, R = 100, r = 62;
    var start = -Math.PI / 2;
    slices.forEach(function (s) {
      var ang = s.weight / 100 * Math.PI * 2;
      if (ang <= 0) return;
      ctx.beginPath();
      ctx.arc(cx, cy, R, start, start + ang);
      ctx.arc(cx, cy, r, start + ang, start, true);
      ctx.closePath();
      ctx.fillStyle = colorFor(s.sym);
      ctx.fill();
      ctx.strokeStyle = '#111827';
      ctx.lineWidth = 2;
      ctx.stroke();
      start += ang;
    });
    ctx.fillStyle = '#ffffff';
    ctx.textAlign = 'center';
    ctx.font = 'bold 19px Arial, sans-serif';
    ctx.fillText(centerTop, cx, cy - 2);
    ctx.fillStyle = '#9ca3af';
    ctx.font = '11px Arial, sans-serif';
    ctx.fillText(centerBottom, cx, cy + 16);
  }

  function legendHtml(slices) {
    return slices.map(function (s) {
      return '<div><span class="pf-dot" style="background:' + colorFor(s.sym) + '"></span>' +
        '<strong>' + esc(s.sym) + '</strong> — ' + fmtPct(s.weight) +
        (s.value != null ? ' (' + fmtMoney(s.value) + ')' : '') + '</div>';
    }).join('');
  }

  // ---------- Rendering: summary + pies + table ----------

  function renderAnalysis() {
    var c = compute();
    var summaryEl = document.getElementById('pf-summary');
    var tableEl = document.getElementById('pf-table');
    var chartsEl = document.getElementById('pf-charts');
    var stampEl = document.getElementById('pf-stamp');
    var t = c.targets;

    var priced = c.rows.filter(function (r) { return r.value != null && r.value > 0; });
    if (!c.rows.length || !priced.length) {
      summaryEl.innerHTML = '<p class="hint">' + (c.rows.length
        ? 'Add units (and a manual price for anything CoinGecko doesn\'t carry) to see your book valued and checked.'
        : 'No holdings yet — add your first asset below to see your book valued live and checked against today\'s conditions.') + '</p>';
      tableEl.innerHTML = '<p class="hint">Nothing to value yet.</p>';
      chartsEl.style.display = 'none';
      stampEl.textContent = '';
      return;
    }

    // Summary sentence
    var opener = regimeKnown ? regimeLabelText + '.'
                             : 'Conditions unknown — showing mixed-condition targets.';
    var sentence = opener + ' Your book: BTC ' + fmtPct(c.btcWeight) +
      ' (target ' + t.btcLow + '–' + t.btcHigh + '%).';
    sentence += c.btcGap > 0
      ? ' To match today\'s conditions, the framework would move ~' + fmtMoney(c.btcGap) + ' into BTC.'
      : ' Your BTC weight is inside today\'s target range.';

    var pnlHtml = '';
    if (c.hasCost && c.costBasis > 0) {
      var pct = c.pnl / c.costBasis * 100;
      var cls = c.pnl >= 0 ? 'up' : 'down';
      var sign = c.pnl >= 0 ? '+' : '−';
      pnlHtml = '<div>Unrealized P&amp;L: <span class="change ' + cls + '">' + sign + fmtMoney(Math.abs(c.pnl)) +
        ' (' + sign + fmtPct(Math.abs(pct)) + ')</span>' +
        ' <span class="hint">on positions with a cost entered</span></div>';
    }

    summaryEl.innerHTML =
      '<div class="result-hero">' +
      '<div><span class="big">' + fmtMoney(c.total) + '</span></div>' +
      '<div>total book value</div>' +
      '<div style="margin-top:10px">' + esc(sentence) + '</div>' +
      pnlHtml +
      '<div class="hint" style="margin-top:10px">Framework for today: BTC ' + t.btcLow + '–' + t.btcHigh +
      '% · no single other asset over ' + t.altCap + '% · staked positions count in weights but are never flagged to trim.</div>' +
      '</div>';

    // Holdings table
    var html = '<table class="report-table"><tr><th>Asset</th><th>Price</th><th>Units</th><th>Value</th><th>Weight</th><th>Lane</th></tr>';
    c.rows.forEach(function (r) {
      var priceTxt = r.price ? fmtPrice(r.price.price) +
        (r.price.source === 'manual' ? ' <span class="hint">(manual)</span>'
          : r.price.source === 'stale' ? ' <span class="hint">(using avg cost)</span>' : '') : '—';
      html += '<tr><td><strong>' + esc(r.sym) + '</strong>' +
        (r.staked ? ' <span class="asset-badge badge-crypto">staked</span>' : '') + '</td>' +
        '<td>' + priceTxt + '</td>' +
        '<td>' + (r.units ? r.units.toLocaleString('en-US', { maximumFractionDigits: 8 }) : '—') + '</td>' +
        '<td>' + (r.value != null ? fmtMoney(r.value) : '—') + '</td>' +
        '<td>' + (r.value != null ? fmtPct(r.weight) : '—') + '</td>' +
        '<td>' + laneFor(r, c) + '</td></tr>';
    });
    tableEl.innerHTML = html + '</table>';

    // Pies: what you have now vs the framework scenario for today
    var nowSlices = priced.slice().sort(function (a, b) { return b.weight - a.weight; })
      .map(function (r) { return { sym: r.sym, weight: r.weight, value: r.value }; });
    var targetSlices = [];
    if (c.btcTarget > 0) targetSlices.push({ sym: 'BTC', weight: c.btcTarget, value: c.btcTarget / 100 * c.total });
    Object.keys(c.altTarget).forEach(function (sym) {
      if (c.altTarget[sym] > 0.05) targetSlices.push({ sym: sym, weight: c.altTarget[sym], value: c.altTarget[sym] / 100 * c.total });
    });
    targetSlices.sort(function (a, b) { return b.weight - a.weight; });

    chartsEl.style.display = '';
    drawDonut(document.getElementById('pf-pie-now'), nowSlices, fmtMoney(c.total), 'your book now');
    drawDonut(document.getElementById('pf-pie-target'), targetSlices, 'BTC ' + fmtPct(c.btcTarget), 'framework target');
    document.getElementById('pf-legend-now').innerHTML = legendHtml(nowSlices);
    document.getElementById('pf-legend-target').innerHTML = legendHtml(targetSlices);

    var anyLive = c.rows.some(function (r) { return r.price && r.price.source === 'live'; });
    stampEl.textContent = (anyLive ? 'Live prices via CoinGecko' : 'No live prices loaded — manual/cost prices shown') +
      ' · conditions from NexIntel · ' + new Date().toLocaleTimeString();
  }

  // ---------- Events (debt.js pattern: debounced autosave on every edit) ----------

  var saveTimer = null;
  function onEdit() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () { save(); renderAnalysis(); }, 400);
  }
  document.getElementById('pf-list').addEventListener('input', onEdit);
  document.getElementById('pf-list').addEventListener('change', function () { save(); renderAnalysis(); });

  document.getElementById('pf-list').addEventListener('click', function (e) {
    if (e.target.dataset && e.target.dataset.del !== undefined) {
      readRows(true);
      holdings.splice(parseInt(e.target.dataset.del, 10), 1);
      save();
      render();
      renderAnalysis();
      loadPrices().then(renderAnalysis);
    }
  });

  document.getElementById('pf-add').addEventListener('click', function () {
    readRows();
    holdings.push({ symbol: '', units: '', avg_cost: '', manual_price: '', staked: false });
    render();
    save();
  });

  // ---------- Coinbase sync hook (used by coinbase-link.js) ----------
  // Merges backend-normalized positions [{symbol, units, price_usd, staked}]
  // into the on-device holdings: existing rows keep their avg cost, units are
  // replaced with the synced balance, rows are marked source='coinbase'.
  // USD cash gets its manual price set to $1 (it has no CoinGecko feed);
  // everything else keeps valuing off the live feed as before.
  window.OmniPortfolioSync = {
    applyCoinbase: function (positions) {
      readRows();  // fold in-progress edits into `holdings` first
      var bySym = {};
      holdings.forEach(function (h, i) {
        var s = String(h.symbol || '').toUpperCase();
        if (s && bySym[s] === undefined) bySym[s] = i;
      });
      var applied = 0;
      (positions || []).forEach(function (p) {
        var sym = String(p.symbol || '').toUpperCase();
        if (!sym) return;
        if (bySym[sym] !== undefined) {
          var h = holdings[bySym[sym]];
          h.units = String(p.units);
          if (p.staked) h.staked = true;
          h.source = 'coinbase';
          if (sym === 'USD' && p.price_usd) h.manual_price = String(p.price_usd);
        } else {
          holdings.push({
            symbol: sym, units: String(p.units), avg_cost: '',
            manual_price: (sym === 'USD' && p.price_usd) ? String(p.price_usd) : '',
            staked: !!p.staked, source: 'coinbase'
          });
          bySym[sym] = holdings.length - 1;
        }
        applied++;
      });
      render();  // rebuild the DOM from merged holdings FIRST — save() reads the DOM
      save();
      renderAnalysis();
      loadPrices().then(renderAnalysis);
      return applied;
    }
  };

  // ---------- Init ----------

  load();
  render();
  renderAnalysis();              // instant paint from manual/cost prices
  loadRegime().then(renderAnalysis);
  loadPrices().then(renderAnalysis);
})();
