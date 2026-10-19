// OmniSight live alerts — Loop 3
// Generates real alerts from live market data (CoinGecko + Fear & Greed).
// Replaces the hardcoded placeholder alerts; engine filter actually filters.

(function () {
  'use strict';

  var COINS = {
    bitcoin:  { symbol: 'BTC', engine: 'Coinbase' },
    ethereum: { symbol: 'ETH', engine: 'Coinbase' },
    solana:   { symbol: 'SOL', engine: 'On-Chain' },
    ripple:   { symbol: 'XRP', engine: 'On-Chain' }
  };

  function fmt(n) {
    if (n >= 1000) return '$' + n.toLocaleString('en-US', { maximumFractionDigits: 0 });
    return '$' + n.toLocaleString('en-US', { maximumFractionDigits: 2 });
  }

  function stamp() {
    return new Date().toLocaleString('en-US', {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false
    });
  }

  // Build alert objects from live data
  function buildAlerts(prices, greed) {
    var alerts = [];
    var now = stamp();

    Object.keys(COINS).forEach(function (id) {
      var d = prices[id];
      if (!d) return;
      var chg = d.usd_24h_change || 0;
      var meta = COINS[id];
      if (chg >= 3) {
        alerts.push({
          type: 'buy', engine: meta.engine,
          title: meta.symbol + ' Momentum Surge',
          confidence: Math.min(95, 65 + Math.round(chg * 3)),
          body: meta.symbol + ' up ' + chg.toFixed(2) + '% in 24h to ' + fmt(d.usd) + '. Momentum building.',
          time: now
        });
      } else if (chg <= -3) {
        alerts.push({
          type: 'sell', engine: meta.engine,
          title: meta.symbol + ' Drawdown Alert',
          confidence: Math.min(95, 65 + Math.round(Math.abs(chg) * 3)),
          body: meta.symbol + ' down ' + Math.abs(chg).toFixed(2) + '% in 24h to ' + fmt(d.usd) + '. Downside pressure.',
          time: now
        });
      }
    });

    if (greed !== null) {
      if (greed <= 25) {
        alerts.push({
          type: 'buy', engine: 'Macro',
          title: 'Extreme Fear Reading',
          confidence: 82,
          body: 'Fear & Greed at ' + greed + ' — extreme fear. Historically a contrarian entry zone.',
          time: now
        });
      } else if (greed >= 75) {
        alerts.push({
          type: 'hold', engine: 'Macro',
          title: 'Extreme Greed Warning',
          confidence: 78,
          body: 'Fear & Greed at ' + greed + ' — extreme greed. Consider trimming into strength.',
          time: now
        });
      } else {
        alerts.push({
          type: 'hold', engine: 'Macro',
          title: 'Sentiment Neutral',
          confidence: 60,
          body: 'Fear & Greed at ' + greed + '. No sentiment extreme — trend-following conditions.',
          time: now
        });
      }
    }

    if (!alerts.length) {
      alerts.push({
        type: 'hold', engine: 'Coinbase',
        title: 'Markets Quiet',
        confidence: 55,
        body: 'No coin moved more than 3% in 24h. Watching for breakout.',
        time: now
      });
    }

    return alerts;
  }

  function render(alerts) {
    var feed = document.querySelector('.alert-feed');
    if (!feed) return;

    // Apply engine toggles + confidence threshold from the engines page (Loop 4)
    var cfg = window.OmniSightConfig ? window.OmniSightConfig.load()
                                     : { engines: {}, minConfidence: 0 };
    var visible = alerts.filter(function (a) {
      if (cfg.engines && cfg.engines[a.engine] === false) return false;
      return a.confidence >= (cfg.minConfidence || 0);
    });

    // Sort by engine priority from the profile page (Loop 6)
    if (window.OmniSightProfile) {
      var prio = window.OmniSightProfile.load().priority || [];
      visible.sort(function (a, b) {
        var ia = prio.indexOf(a.engine), ib = prio.indexOf(b.engine);
        if (ia < 0) ia = 99; if (ib < 0) ib = 99;
        return ia - ib;
      });
    }

    feed.innerHTML = '';
    if (!visible.length) {
      feed.innerHTML = '<div class="alert-card alert-hold"><h4>No alerts pass your filters</h4>' +
        '<p>Loosen the confidence threshold or re-enable engines on the <a href="engines.html" style="color:var(--accent)">Engines</a> page.</p></div>';
    }
    visible.forEach(function (a) {
      var card = document.createElement('div');
      card.className = 'alert-card alert-' + a.type;
      card.setAttribute('data-engine', a.engine);
      card.innerHTML =
        '<h4>' + a.title + '</h4>' +
        '<p><strong>Engine:</strong> ' + a.engine + '</p>' +
        '<p><strong>Confidence:</strong> ' + a.confidence + '%</p>' +
        '<p>' + a.body + '</p>' +
        '<p class="timestamp">' + a.time + '</p>';
      feed.appendChild(card);
    });

    var note = document.getElementById('alerts-stamp');
    if (note) note.textContent = 'Live · generated ' + new Date().toLocaleTimeString();
  }

  function wireFilter() {
    var sel = document.querySelector('.filters select');
    if (!sel) return;
    sel.addEventListener('change', function () {
      var want = sel.value;
      document.querySelectorAll('.alert-card').forEach(function (card) {
        var eng = card.getAttribute('data-engine');
        card.style.display = (want === 'All Engines' || eng === want) ? '' : 'none';
      });
    });
  }

  function init() {
    wireFilter();
    var pricesP = fetch('https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana,ripple&vs_currencies=usd&include_24hr_change=true')
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .catch(function () { return {}; });
    var greedP = fetch('https://api.alternative.me/fng/?limit=1')
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (j) { return parseInt(j.data[0].value, 10); })
      .catch(function () { return null; });
    // NexIntel model alerts (BUY/SELL from the prediction engine)
    var modelP = fetch('data/latest.json', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (d) { return d.alerts || []; })
      .catch(function () { return []; });
    Promise.all([pricesP, greedP, modelP]).then(function (res) {
      var alerts = buildAlerts(res[0], res[1]);
      // Prepend model alerts (highest priority)
      res[2].forEach(function (ma) {
        var type = ma.type === 'BUY' ? 'buy' : ma.type === 'SELL' ? 'sell' : 'hold';
        alerts.unshift({
          type: type, engine: 'NexIntel',
          title: '🤖 ' + ma.type + ': ' + ma.asset,
          confidence: ma.confidence,
          body: ma.reason + ' <a href="picks.html" style="color:var(--accent)">See thesis →</a>',
          time: new Date(ma.timestamp).toLocaleString()
        });
      });
      render(alerts);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
