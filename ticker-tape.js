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
