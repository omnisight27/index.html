// OmniSight reports — Loop 5
// Generates real Market Overview and Crypto Flow reports from live data.
// Other report types show a "needs data feed" state until their feeds exist.

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

  function today() {
    return new Date().toLocaleDateString('en-US', {
      weekday: 'long', year: 'numeric', month: 'long', day: 'numeric'
    });
  }

  function sentimentTake(greed) {
    if (greed === null) return 'Sentiment feed unavailable.';
    if (greed <= 25) return 'Extreme fear (' + greed + ') — capitulation zones like this have historically rewarded patient buyers, but respect the downtrend.';
    if (greed < 45) return 'Fear (' + greed + ') — cautious tape. Rallies are guilty until proven innocent.';
    if (greed <= 55) return 'Neutral (' + greed + ') — no crowd extreme to fade. Follow the trend.';
    if (greed < 75) return 'Greed (' + greed + ') — optimism is building. Ride it, but keep position sizes sane.';
    return 'Extreme greed (' + greed + ') — euphoria. This is where discipline pays: trim into strength, don\'t chase.';
  }

  function marketOverview(prices, greed) {
    var rows = COINS.map(function (c) {
      var d = prices[c.id] || {};
      var chg = d.usd_24h_change || 0;
      var cls = chg >= 0 ? 'up' : 'down';
      return '<tr><td>' + c.label + ' (' + c.symbol + ')</td><td>' + fmt(d.usd || 0) +
        '</td><td class="change ' + cls + '">' + (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%</td></tr>';
    }).join('');

    var movers = COINS.map(function (c) {
      return { c: c, chg: (prices[c.id] || {}).usd_24h_change || 0 };
    }).sort(function (a, b) { return Math.abs(b.chg) - Math.abs(a.chg); });
    var top = movers[0];

    return '<h3>Market Overview — ' + today() + '</h3>' +
      '<table class="report-table"><thead><tr><th>Asset</th><th>Price</th><th>24h</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table>' +
      '<h4>Headline</h4><p>' + top.c.label + ' is the biggest mover at ' +
      (top.chg >= 0 ? '+' : '') + top.chg.toFixed(2) + '% over 24 hours.</p>' +
      '<h4>Sentiment</h4><p>' + sentimentTake(greed) + '</p>' +
      '<h4>Risk read</h4><p>' + riskRead(COINS.map(function (c) { return (prices[c.id] || {}).usd_24h_change || 0; })) + '</p>';
  }

  function riskRead(changes) {
    var big = changes.filter(function (c) { return Math.abs(c) >= 3; }).length;
    if (big >= 2) return 'Elevated volatility — multiple assets swinging 3%+. Reduce size, widen stops.';
    if (big === 1) return 'One asset breaking away from the pack. Idiosyncratic move — check for news before following.';
    return 'Quiet tape — no asset moved more than 3%. Low-vol regimes reward patience over action.';
  }

  function cryptoFlow(prices) {
    var ranked = COINS.map(function (c) {
      var d = prices[c.id] || {};
      return { label: c.label, symbol: c.symbol, price: d.usd || 0, chg: d.usd_24h_change || 0 };
    }).sort(function (a, b) { return b.chg - a.chg; });

    var rows = ranked.map(function (r, i) {
      var cls = r.chg >= 0 ? 'up' : 'down';
      var flow = r.chg > 1 ? 'Inflows' : (r.chg < -1 ? 'Outflows' : 'Balanced');
      return '<tr><td>#' + (i + 1) + '</td><td>' + r.label + ' (' + r.symbol + ')</td>' +
        '<td>' + fmt(r.price) + '</td><td class="change ' + cls + '">' +
        (r.chg >= 0 ? '+' : '') + r.chg.toFixed(2) + '%</td><td>' + flow + '</td></tr>';
    }).join('');

    return '<h3>Crypto Flow Report — ' + today() + '</h3>' +
      '<p>24-hour flow pressure ranked by momentum. Sustained inflows suggest accumulation; sharp outflows suggest distribution.</p>' +
      '<table class="report-table"><thead><tr><th>Rank</th><th>Asset</th><th>Price</th><th>24h</th><th>Flow</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table>';
  }

  function pendingReport(name) {
    return '<h3>' + name + '</h3><p class="live-stamp">This report needs its data feed connected. ' +
      'Currently tracked as a future loop.</p>';
  }

  var GENERATORS = {
    'market-overview': marketOverview,
    'crypto-flow': cryptoFlow,
    'engine-breakdown': function () { return pendingReport('Engine Breakdown'); },
    'macro-regime': function () { return pendingReport('Macro Regime History'); },
    'liquidity': function () { return pendingReport('Liquidity Analysis'); },
    'risk': function () { return pendingReport('Risk Assessment'); }
  };

  function fetchData() {
    var pricesP = fetch('https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana,ripple&vs_currencies=usd&include_24hr_change=true')
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .catch(function () { return {}; });
    var greedP = fetch('https://api.alternative.me/fng/?limit=1')
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (j) { return parseInt(j.data[0].value, 10); })
      .catch(function () { return null; });
    return Promise.all([pricesP, greedP]);
  }

  function init() {
    var out = document.getElementById('report-output');
    if (!out) return;
    document.querySelectorAll('[data-report]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var key = btn.getAttribute('data-report');
        var gen = GENERATORS[key];
        out.innerHTML = '<div class="card"><p>Generating report…</p></div>';
        out.scrollIntoView({ behavior: 'smooth', block: 'start' });
        fetchData().then(function (res) {
          out.innerHTML = '<div class="card report">' + gen(res[0], res[1]) +
            '<p class="live-stamp">Generated ' + new Date().toLocaleTimeString() + ' · data: CoinGecko, alternative.me</p></div>';
        });
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
