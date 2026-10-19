// OmniSight live market data — Loop 1
// Pulls real prices from CoinGecko's free API (no key required).
// If the API is unreachable, the dashboard keeps its static content.

(function () {
  'use strict';

  var COINS = [
    { id: 'bitcoin', symbol: 'BTC', label: 'Bitcoin' },
    { id: 'ethereum', symbol: 'ETH', label: 'Ethereum' },
    { id: 'solana', symbol: 'SOL', label: 'Solana' },
    { id: 'ripple', symbol: 'XRP', label: 'XRP' }
  ];

  function fmt(n) {
    if (n >= 1000) return '$' + n.toLocaleString('en-US', { maximumFractionDigits: 0 });
    return '$' + n.toLocaleString('en-US', { maximumFractionDigits: 2 });
  }

  function render(prices) {
    var section = document.getElementById('live-markets');
    if (!section) return;
    var grid = section.querySelector('.grid');
    if (!grid) return;
    grid.innerHTML = '';
    COINS.forEach(function (c) {
      var d = prices[c.id];
      if (!d) return;
      var chg = d.usd_24h_change;
      var cls = chg >= 0 ? 'up' : 'down';
      var arrow = chg >= 0 ? '▲' : '▼';
      var card = document.createElement('div');
      card.className = 'card';
      card.innerHTML =
        '<h4>' + c.label + ' <span class="sym">' + c.symbol + '</span></h4>' +
        '<p class="price">' + fmt(d.usd) + '</p>' +
        '<p class="change ' + cls + '">' + arrow + ' ' + Math.abs(chg).toFixed(2) + '% (24h)</p>';
      grid.appendChild(card);
    });
    var stamp = document.getElementById('live-stamp');
    if (stamp) {
      stamp.textContent = 'Live via CoinGecko · updated ' + new Date().toLocaleTimeString();
    }
  }

  function init() {
    var ids = COINS.map(function (c) { return c.id; }).join(',');
    var url = 'https://api.coingecko.com/api/v3/simple/price?ids=' +
      encodeURIComponent(ids) +
      '&vs_currencies=usd&include_24hr_change=true';
    function load() {
      fetch(url)
        .then(function (r) { if (!r.ok) throw new Error('bad status'); return r.json(); })
        .then(render)
        .catch(function () {
          var stamp = document.getElementById('live-stamp');
          if (stamp) stamp.textContent = 'Live data unavailable — showing last known layout';
        });
    }
    load();
    setInterval(load, 60000); // refresh every 60s

    // Fed commentary from NexIntel
    var fedEl = document.getElementById('fed-articles');
    if (fedEl) {
      fetch('data/latest.json', { cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          var arts = d && d.fed_commentary;
          if (arts && arts.length) {
            fedEl.innerHTML = arts.map(function (a) {
              var link = a.link ? ' <a href="' + a.link + '" target="_blank" rel="noopener">read →</a>' : '';
              return '<p>· <strong>' + a.title + '</strong><br><span class="live-stamp">' +
                a.date + link + '</span></p>';
            }).join('');
          } else {
            fedEl.innerHTML = '<p class="live-stamp">No Fed commentary in this run.</p>';
          }
        })
        .catch(function () { fedEl.innerHTML = '<p class="live-stamp">Feed unavailable.</p>'; });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
