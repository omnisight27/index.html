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

// OmniSight picks tape — the engine's current top picks, scrolling under the
// price tape on every page. Fed by data/latest.json (published by the engine).
// Levels are shown only when they are sane (long: target > price > stop > 0;
// short: stop > price > target > 0) — a bad level is omitted, never printed.

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
      fmtPrice(price) + levels + '</span>';
  }

  function render(data) {
    var tape = document.getElementById('picks-tape');
    if (!tape) return;
    var longs = (data.longs || []).slice().sort(function (a, b) { return (b.confidence || 0) - (a.confidence || 0); }).slice(0, 4);
    var shorts = (data.shorts || []).slice().sort(function (a, b) { return (b.confidence || 0) - (a.confidence || 0); }).slice(0, 4);
    if (!longs.length && !shorts.length) { tape.style.display = 'none'; return; }
    var items = [];
    items.push('<span class="ticker-item">🎯 <strong>PICKS</strong> ' + esc(data.regime_label || '') + '</span>');
    longs.forEach(function (p) { items.push(pickItem(p, 'LONG')); });
    shorts.forEach(function (p) { items.push(pickItem(p, 'SHORT')); });
    items.push('<span class="ticker-item"><a href="picks.html">See all picks →</a></span>');
    var joined = items.join('<span class="ticker-sep">·</span>');
    tape.innerHTML = '<div class="ticker-track">' + joined + '<span class="ticker-sep">·</span>' + joined + '</div>';
  }

  function load() {
    fetch('data/latest.json', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(render)
      .catch(function () {
        var tape = document.getElementById('picks-tape');
        if (tape && !tape.innerHTML) tape.style.display = 'none';
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', load);
  } else {
    load();
  }
  setInterval(load, 300000);
})();
