// OmniSight ticker tape — Loop 7
// Scrolling live price ticker on the homepage (CoinGecko, free, no key).

(function () {
  'use strict';

  var COINS = [
    { id: 'bitcoin', symbol: 'BTC' },
    { id: 'ethereum', symbol: 'ETH' },
    { id: 'solana', symbol: 'SOL' },
    { id: 'ripple', symbol: 'XRP' }
  ];

  function fmt(n) {
    if (n >= 1000) return '$' + n.toLocaleString('en-US', { maximumFractionDigits: 0 });
    return '$' + n.toLocaleString('en-US', { maximumFractionDigits: 2 });
  }

  function render(prices) {
    var tape = document.getElementById('ticker-tape');
    if (!tape) return;
    var items = COINS.map(function (c) {
      var d = prices[c.id] || {};
      var chg = d.usd_24h_change || 0;
      var cls = chg >= 0 ? 'up' : 'down';
      return '<span class="ticker-item"><strong>' + c.symbol + '</strong> ' + fmt(d.usd || 0) +
        ' <span class="' + cls + '">' + (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%</span></span>';
    }).join('<span class="ticker-sep">·</span>');
    // duplicate for seamless loop
    tape.innerHTML = '<div class="ticker-track">' + items +
      '<span class="ticker-sep">·</span>' + items + '</div>';
  }

  function init() {
    var ids = COINS.map(function (c) { return c.id; }).join(',');
    fetch('https://api.coingecko.com/api/v3/simple/price?ids=' + encodeURIComponent(ids) +
      '&vs_currencies=usd&include_24hr_change=true')
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(render)
      .catch(function () {
        var tape = document.getElementById('ticker-tape');
        if (tape) tape.style.display = 'none';
      });
    // refresh every 60s
    setInterval(function () {
      fetch('https://api.coingecko.com/api/v3/simple/price?ids=' + encodeURIComponent(ids) +
        '&vs_currencies=usd&include_24hr_change=true')
        .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
        .then(render)
        .catch(function () {});
    }, 60000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

// OmniSight picks tape — rolls on every page under the price tape.
// Leads with 🎯 Today's Verdicts (the same gates as the Picks page:
// intraday LONG >= 70, or swing long >= 70 with >= 4/5 timeframes
// agreeing, or a planned mover still climbing; news-flagged assets can
// never be a BUY), then the top swing longs/shorts. Every item says
// where it trades (Coinbase / Crypto.com / Schwab / not on your
// exchanges) and exit times render in ET. Levels print only when sane
// (target > entry > stop > 0) — a bad level is omitted, never shown.

(function () {
  'use strict';

  function fmtPrice(n) {
    if (n == null || isNaN(n)) return '—';
    if (n >= 1000) return '$' + n.toLocaleString('en-US', { maximumFractionDigits: 0 });
    if (n >= 1) return '$' + n.toFixed(2);
    if (n >= 0.1) return '$' + n.toFixed(3);
    if (n >= 0.01) return '$' + n.toFixed(4);
    return '$' + n.toPrecision(3);
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function fmtClockET(v) {
    if (!v) return v;
    var s = String(v);
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
    var d = null;
    var m = s.match(/^(\d{1,2}):(\d{2})\s*UTC$/i);
    if (m) {
      d = new Date();
      d.setUTCHours(parseInt(m[1], 10), parseInt(m[2], 10), 0, 0);
    } else {
      var t = Date.parse(s);
      if (!isNaN(t)) d = new Date(t);
    }
    if (!d) return s;
    try {
      return new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit' }).format(d) + ' ET';
    } catch (e) { return s; }
  }

  function venue(p) {
    if (((p && p['class']) || '').toLowerCase() === 'equity') return 'Schwab';
    var tr = (p && p.tradable) || [];
    var cb = tr.indexOf('coinbase') > -1, cdc = tr.indexOf('cryptocom') > -1;
    if (cb && cdc) return 'Coinbase ✓ · Crypto.com ✓';
    if (cb) return 'Coinbase ✓';
    if (cdc) return 'Crypto.com only';
    return 'Not on your exchanges';
  }

  function tfAgree(p) {
    var v = p && p.tf_align;
    if (v === null || v === undefined || v === '') return null;
    if (typeof v === 'number') return v <= 1 ? Math.round(v * 5) : Math.round(v);
    var m = String(v).match(/(\d+)\s*\/\s*5/) || String(v).match(/(\d+)\s+of\s+5/i);
    return m ? parseInt(m[1], 10) : null;
  }

  // Mirror of the Picks page verdict gates (picks.js computeVerdicts).
  function computeVerdicts(swing, intra) {
    swing = swing || {}; intra = intra || {};
    var seen = {}, buys = [], watching = [];
    var newsFlagged = {};
    (swing.longs || []).concat(swing.shorts || []).forEach(function (p) {
      if (p.news_flag) newsFlagged[p.asset] = p;
    });
    function push(list, v) { if (!seen[v.asset]) { seen[v.asset] = 1; list.push(v); } }
    (intra.longs || []).filter(function (s) { return !s.direction || s.direction === 'LONG'; }).forEach(function (s) {
      if ((s.confidence || 0) >= 70 && !newsFlagged[s.asset]) {
        var t = s.thesis || {};
        push(buys, { asset: s.asset, name: s.name, 'class': s['class'], tradable: s.tradable,
          plan: t.entry !== undefined ? { entry: t.entry, take_profit: t.take_profit, stop_loss: t.stop_loss, exit_by: t.exit_by } : null });
      }
    });
    (swing.longs || []).forEach(function (p) {
      var agree = tfAgree(p);
      if ((p.confidence || 0) >= 70 && agree !== null && agree >= 4 && !newsFlagged[p.asset]) {
        var t = p.thesis || {};
        push(buys, { asset: p.asset, name: p.name, 'class': p['class'], tradable: p.tradable,
          plan: { entry: p.price, take_profit: t.take_profit, stop_loss: t.stop_loss, exit_by: t.projected_exit } });
      }
    });
    (intra.movers || []).forEach(function (m) {
      if (m.plan && (m.chg_1h || 0) > 0 && (m.chg_24h || 0) >= 15) {
        push(buys, { asset: m.asset, name: m.name, 'class': m['class'], tradable: m.tradable, plan: m.plan });
      }
    });
    // News-flagged assets are never displayed (Nic's request, 2026-10-22):
    // mark them seen so the WATCHING list and the top-longs/shorts tail
    // below skip them too — no AVOID segment anywhere.
    Object.keys(newsFlagged).forEach(function (a) { seen[a] = 1; });
    var closest = null;
    if (!buys.length) {
      var cands = [];
      (swing.longs || []).forEach(function (p) {
        if (!newsFlagged[p.asset] && !seen[p.asset]) cands.push({ p: p, conf: p.confidence || 0 });
      });
      (intra.longs || []).forEach(function (s) {
        if (!newsFlagged[s.asset] && !seen[s.asset]) cands.push({ p: s, conf: s.confidence || 0 });
      });
      cands.sort(function (a, b) { return b.conf - a.conf; });
      if (cands.length) { closest = cands[0]; seen[closest.p.asset] = 1; }
    }
    (intra.watch || []).concat(intra.accum || []).forEach(function (m) {
      if (watching.length >= 2) return;
      push(watching, { asset: m.asset, name: m.name, 'class': m['class'], tradable: m.tradable, plan: m.plan || null });
    });
    return { buys: buys, closest: closest, watching: watching, seen: seen };
  }

  function planText(plan) {
    if (!plan) return '';
    var e = plan.entry, tp = plan.take_profit, sl = plan.stop_loss;
    if (!(tp > e && e > sl && sl > 0)) return '';
    var s = 'Buy ~' + fmtPrice(e) + ' · Sell ' + fmtPrice(tp) + ' · Stop ' + fmtPrice(sl);
    if (plan.exit_by) s += ' · Out by ' + fmtClockET(plan.exit_by);
    return s;
  }

  function pickItem(p, dir) {
    var t = p.thesis || {};
    var price = p.price, tp = t.take_profit, sl = t.stop_loss;
    var levels = '';
    if (dir === 'LONG' && tp > price && price > sl && sl > 0) {
      levels = ' · Target ' + fmtPrice(tp) + ' · Stop ' + fmtPrice(sl);
    } else if (dir === 'SHORT' && sl > price && price > tp && tp > 0) {
      levels = ' · Target ' + fmtPrice(tp) + ' · Stop ' + fmtPrice(sl);
    }
    var cls = dir === 'LONG' ? 'long' : 'short';
    var arrow = dir === 'LONG' ? '▲' : '▼';
    return '<span class="ticker-item"><span class="' + cls + '">' + arrow + ' ' + dir + '</span> <strong>' +
      esc(p.asset) + '</strong> (' + esc(p.name || '') + ') ' + Math.round(p.confidence || 0) + '% · ' +
      fmtPrice(price) + levels + ' · ' + venue(p) + '</span>';
  }

  function render(swing, intra) {
    var tape = document.getElementById('picks-tape');
    if (!tape) return;
    if (!swing) { tape.style.display = 'none'; return; }
    var v = computeVerdicts(swing, intra);
    var items = [];
    var head = '🎯 <strong>PICKS</strong> ' + esc(swing.regime_label || '');
    if (!v.buys.length && !v.closest) head += ' · No buy right now';
    items.push('<span class="ticker-item">' + head + '</span>');
    v.buys.slice(0, 6).forEach(function (b) {
      var pt = planText(b.plan);
      items.push('<span class="ticker-item">🎯 <span class="long">BUY</span> <strong>' + esc(b.asset) + '</strong>' +
        (pt ? ' · ' + pt : '') + ' · ' + venue(b) + '</span>');
    });
    if (v.closest) {
      items.push('<span class="ticker-item">🎯 CLOSEST TO A BUY: <strong>' + esc(v.closest.p.asset) + '</strong> ' +
        Math.round(v.closest.conf) + '% · ' + venue(v.closest.p) + '</span>');
    }
    v.watching.forEach(function (w) {
      var pt = planText(w.plan);
      items.push('<span class="ticker-item">👀 WATCHING <strong>' + esc(w.asset) + '</strong>' +
        (pt ? ' · ' + pt : '') + ' · ' + venue(w) + '</span>');
    });
    var longs = (swing.longs || []).slice().sort(function (a, b) { return (b.confidence || 0) - (a.confidence || 0); }).slice(0, 4);
    var shorts = (swing.shorts || []).slice().sort(function (a, b) { return (b.confidence || 0) - (a.confidence || 0); }).slice(0, 4);
    longs.forEach(function (p) { if (!v.seen[p.asset]) items.push(pickItem(p, 'LONG')); });
    shorts.forEach(function (p) { if (!v.seen[p.asset]) items.push(pickItem(p, 'SHORT')); });
    items.push('<span class="ticker-item"><a href="picks.html">See all picks →</a></span>');
    var joined = items.join('<span class="ticker-sep">·</span>');
    tape.innerHTML = '<div class="ticker-track">' + joined + '<span class="ticker-sep">·</span>' + joined + '</div>';
  }

  function load() {
    var gotSwing = null, gotIntra = null, done = 0;
    function finish() { if (++done === 2) render(gotSwing, gotIntra); }
    fetch('data/latest.json', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (d) { gotSwing = d; })
      .catch(function () {})
      .then(finish);
    fetch('data/intraday.json', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (d) { gotIntra = d; })
      .catch(function () {})
      .then(finish);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', load);
  } else {
    load();
  }
  setInterval(load, 300000);
})();
