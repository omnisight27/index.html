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

  function liquidityAnalysis(model) {
    if (!model) return pendingReport('Liquidity Analysis');
    var macro = model.macro || {};
    var rows = '';
    var m2 = macro.m2 || {};
    var cpi = macro.cpi || {};
    var unemp = macro.unemployment || {};
    var sent = macro.consumer_sentiment || {};

    function mrow(label, val, chg, interp) {
      return '<tr><td>' + label + '</td><td>' + val + '</td><td>' + chg + '</td><td>' + interp + '</td></tr>';
    }

    if (m2.value) {
      var m2t = m2.change_pct > 0 ? 'Expanding — liquidity tailwind' : 'Contracting — liquidity headwind';
      rows += mrow('M2 Money Supply', '$' + (m2.value / 1000).toFixed(1) + 'T', (m2.change_pct >= 0 ? '+' : '') + m2.change_pct + '%', m2t);
    }
    if (cpi.value) {
      var cpit = (macro.cpi_yoy || {}).value;
      rows += mrow('CPI (YoY)', cpit ? cpit.toFixed(1) + '%' : '—', cpi.date || '', cpit > 3 ? 'Hot — Fed constrained' : 'Cool — Fed has room');
    }
    if (unemp.value) {
      rows += mrow('Unemployment', unemp.value + '%', (unemp.change >= 0 ? '+' : '') + unemp.change + 'pp', unemp.change > 0.2 ? 'Rising — stress building' : 'Stable');
    }
    if (sent.value) {
      rows += mrow('Consumer Sentiment', sent.value, (sent.change >= 0 ? '+' : '') + sent.change, sent.value < 70 ? 'Weak — demand risk' : 'Healthy');
    }
    // Gold as stress barometer
    var fg = model.fear_greed;
    rows += mrow('Fear & Greed', fg, '—', fg <= 25 ? 'Extreme fear — liquidity crunch likely' : fg >= 75 ? 'Euphoria — complacency risk' : 'Normal');

    var regime = model.regime || '—';
    var verdict = regime === 'RISK_ON' ? 'Liquidity is flowing — risk assets have a tailwind.'
      : regime === 'RISK_OFF' ? 'Liquidity is tightening — defensive positioning warranted.'
      : 'Mixed liquidity signals — selective exposure.';

    return '<h3>Liquidity Analysis — ' + today() + '</h3>' +
      '<table class="report-table"><thead><tr><th>Indicator</th><th>Value</th><th>Change</th><th>Read</th></tr></thead>' +
      '<tbody>' + (rows || '<tr><td colspan="4">Macro feed not connected — add FRED_API_KEY.</td></tr>') + '</tbody></table>' +
      '<h4>Verdict</h4><p>' + verdict + '</p>' +
      '<p class="live-stamp">Regime: ' + regime + ' · Sources: FRED, alternative.me</p>';
  }

  function riskAssessment(model) {
    if (!model) return pendingReport('Risk Assessment');
    var tr = model.track_record || {};
    var err = model.model_error || {};
    var picks = (model.longs || []).concat(model.shorts || []);

    var wr = tr.win_rate !== undefined ? tr.win_rate : '—';
    var mae = err.mae !== undefined ? err.mae.toFixed(2) + '%' : '—';
    var w5 = err.within_5pct !== undefined ? err.within_5pct.toFixed(0) + '%' : '—';

    var rows = picks.map(function (p) {
      var t = p.thesis || {};
      return '<tr><td>' + p.asset + '</td><td>' + (t.direction || '—') + '</td>' +
        '<td>' + p.confidence + '%</td><td>$' + (t.stop_loss || '—') + '</td>' +
        '<td>' + (t.risk_reward || '—') + '</td><td>' + (t.max_loss_pct || '—') + '%</td></tr>';
    }).join('');

    var avgRR = picks.length ? (picks.reduce(function (s, p) { return s + ((p.thesis || {}).risk_reward || 0); }, 0) / picks.length).toFixed(2) : '—';

    return '<h3>Risk Assessment — ' + today() + '</h3>' +
      '<h4>Model track record</h4>' +
      '<table class="report-table"><tbody>' +
      '<tr><td>Win rate</td><td>' + wr + '%</td><td>Picks scored</td><td>' + (tr.total || 0) + '</td></tr>' +
      '<tr><td>Mean abs. error</td><td>' + mae + '</td><td>Within 5% target</td><td>' + w5 + '</td></tr>' +
      '</tbody></table>' +
      '<h4>Current position risk</h4>' +
      '<table class="report-table"><thead><tr><th>Asset</th><th>Dir</th><th>Conf</th><th>Stop</th><th>R:R</th><th>Max loss</th></tr></thead>' +
      '<tbody>' + (rows || '<tr><td colspan="6">No active picks.</td></tr>') + '</tbody></table>' +
      '<h4>Risk control</h4><p>Average risk:reward across picks: <strong>' + avgRR +
      '</strong>. Kelly position size: <strong>' + (model.kelly_pct || 0) + '%</strong>. ' +
      'Every pick carries a hard stop — no open-ended risk.</p>';
  }

  function engineBreakdown(model) {
    if (!model || !model.weights) return pendingReport('Engine Breakdown');
    var names = ['momentum', 'reversion', 'psych_bias', 'fg_level', 'fg_delta', 'vol',
      'acceleration', 'zscore', 'rsi', 'trend_align',
      'macd', 'ma_align', 'donchian', 'rsi2', 'roc', 'stoch',
      'boll_pctb', 'atr', 'vol_regime', 'zscore2', 'extreme_fade', 'var_proxy'];
    var groups = { Trend: 0, Momentum: 0, Volatility: 0, 'Mean-reversion': 0, Psychology: 0, Risk: 0 };
    var map = { macd: 'Trend', ma_align: 'Trend', donchian: 'Trend', trend_align: 'Trend',
      rsi: 'Momentum', rsi2: 'Momentum', roc: 'Momentum', stoch: 'Momentum', momentum: 'Momentum',
      boll_pctb: 'Volatility', atr: 'Volatility', vol: 'Volatility', vol_regime: 'Volatility', acceleration: 'Volatility',
      zscore: 'Mean-reversion', zscore2: 'Mean-reversion', reversion: 'Mean-reversion', extreme_fade: 'Mean-reversion',
      psych_bias: 'Psychology', fg_level: 'Psychology', fg_delta: 'Psychology',
      var_proxy: 'Risk' };
    model.weights.forEach(function (w, i) {
      var g = map[names[i]] || 'Trend';
      groups[g] += Math.abs(w);
    });
    var total = Object.values(groups).reduce(function (s, v) { return s + v; }, 0) || 1;
    var rows = Object.keys(groups).map(function (g) {
      var pct = (groups[g] / total * 100).toFixed(1);
      var bar = '█'.repeat(Math.round(pct / 5)) + '░'.repeat(20 - Math.round(pct / 5));
      return '<tr><td>' + g + '</td><td>' + bar + '</td><td>' + pct + '%</td></tr>';
    }).join('');
    var info = model.weight_info || {};
    return '<h3>Engine Breakdown — ' + today() + '</h3>' +
      '<p>How much each model family contributes to the current signal. Weights ' +
      (info.trained ? 'optimized via row-echelon on ' + info.samples + ' scored picks.' : 'in heuristic warm-up mode — training as picks score.') + '</p>' +
      '<table class="report-table"><thead><tr><th>Family</th><th>Weight</th><th>Share</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table>';
  }

  function macroRegime(model) {
    return fetch('data/history.json', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (h) {
        if (!h || !h.picks || !h.picks.length) {
          return '<h3>Macro Regime History</h3><p class="live-stamp">Not enough run history yet — check back after a few loop cycles.</p>';
        }
        var rows = h.picks.slice(-10).reverse().map(function (run) {
          var fg = run.fear_greed !== undefined ? run.fear_greed : '—';
          var n = ((run.longs || []).length + (run.shorts || []).length);
          var wins = (run.longs || []).concat(run.shorts || []).filter(function (p) { return p.return_pct > 0; }).length;
          return '<tr><td>' + (run.date || '').slice(0, 10) + '</td><td>' + fg +
            '</td><td>' + (run.scored ? 'Yes' : 'Pending') + '</td><td>' + n + ' picks</td><td>' + wins + ' winners</td></tr>';
        }).join('');
        var cur = model ? '<p><strong>Current regime:</strong> ' + (model.regime || '—') + '</p>' : '';
        return '<h3>Macro Regime History — ' + today() + '</h3>' + cur +
          '<table class="report-table"><thead><tr><th>Date</th><th>F&G</th><th>Scored</th><th>Picks</th><th>Winners</th></tr></thead>' +
          '<tbody>' + rows + '</tbody></table>';
      })
      .catch(function () { return pendingReport('Macro Regime History'); });
  }

  function pendingReport(name) {
    return '<h3>' + name + '</h3><p class="live-stamp">This report needs its data feed connected. ' +
      'Currently tracked as a future loop.</p>';
  }

  var GENERATORS = {
    'market-overview': marketOverview,
    'crypto-flow': cryptoFlow,
    'engine-breakdown': engineBreakdown,
    'macro-regime': macroRegime,
    'liquidity': liquidityAnalysis,
    'risk': riskAssessment
  };

  function fetchData() {
    var pricesP = fetch('https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana,ripple&vs_currencies=usd&include_24hr_change=true')
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .catch(function () { return {}; });
    var greedP = fetch('https://api.alternative.me/fng/?limit=1')
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (j) { return parseInt(j.data[0].value, 10); })
      .catch(function () { return null; });
    var modelP = fetch('data/latest.json', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .catch(function () { return null; });
    return Promise.all([pricesP, greedP, modelP]);
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
          var prices = res[0], greed = res[1], model = res[2];
          var result = gen(prices, greed, model);
          // macroRegime returns a promise
          if (result && typeof result.then === 'function') {
            result.then(function (html) {
              out.innerHTML = '<div class="card report">' + html +
                '<p class="live-stamp">Generated ' + new Date().toLocaleTimeString() + '</p></div>';
            });
          } else {
            out.innerHTML = '<div class="card report">' + result +
              '<p class="live-stamp">Generated ' + new Date().toLocaleTimeString() + ' · data: CoinGecko, alternative.me, NexIntel</p></div>';
          }
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
