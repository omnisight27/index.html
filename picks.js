// OmniSight picks page — renders the autonomous NexIntel loop output.
// Reads data/latest.json (committed by GitHub Actions 4x daily).

(function () {
  'use strict';

  function assetBadge(p) {
    var cls = (p['class'] || '').toLowerCase();
    // Intraday signals are always crypto (no class field set — detect via CoinGecko link)
    if (cls === 'crypto' || (!p['class'] && p.info_url && p.info_url.indexOf('coingecko') > -1)) return '<span class="asset-badge badge-crypto">🪙 Crypto</span>';
    var nm = (p.name || '').toUpperCase();
    if (nm.indexOf('ETF') > -1 || nm.indexOf('SPDR') > -1 || nm.indexOf('SELECT SECTOR') > -1) return '<span class="asset-badge badge-etf">📊 ETF</span>';
    return '<span class="asset-badge badge-stock">📈 Stock</span>';
  }

  function tradableBadge(p) {
    if ((p['class'] || '').toLowerCase() !== 'crypto' && !(p.info_url && p.info_url.indexOf('coingecko') > -1)) return '';
    var t = p.tradable || [];
    if (!t.length) return ' <span class="trade-badge trade-none">⚠️ Not on your exchanges</span>';
    var labels = t.map(function (x) { return x === 'coinbase' ? 'Coinbase' : 'Crypto.com'; });
    return ' <span class="trade-badge trade-yes">✅ ' + labels.join(' + ') + '</span>';
  }

  function assetHeader(p, side) {
    var arrow = side === 'long' ? '▲ LONG' : (side === 'short' ? '▼ SHORT' : (p.direction === 'LONG' ? '▲' : '▼'));
    var nm = p.name && p.name !== p.asset ? ' <span class="asset-name">' + p.name + '</span>' : '';
    var link = p.info_url ? ' <a class="asset-link" href="' + p.info_url + '" target="_blank" rel="noopener">What is this? ↗</a>' : '';
    var edgar = p.edgar_url ? ' <a class="asset-link" href="' + p.edgar_url + '" target="_blank" rel="noopener">📄 SEC filings ↗</a>' : '';
    return '<h4>' + arrow + ' ' + p.asset + '</h4>' +
      '<p class="asset-sub">' + assetBadge(p) + nm + link + edgar + tradableBadge(p) + '</p>';
  }

  function modelDetails(p) {
    var t = p.thesis || {};
    var feats = p.features || [];
    // Feature names for the 22-model vector (10 base + 12 model features)
    var featNames = ['Trend', 'Momentum', 'Volatility', 'Mean-reversion', 'Risk', 'MACD proxy', 'MA alignment',
      'Donchian', 'RSI', 'ROC', 'Stochastic', 'Bollinger %B', 'ATR proxy', 'Vol regime', 'Z-score',
      'Extreme fade', 'VaR proxy', 'Sentiment bias', 'Regime tilt', 'Geo edge', 'Volume', 'Acceleration'];
    var featRows = feats.map(function (f, i) {
      var nm = featNames[i] || ('F' + (i + 1));
      var bar = f >= 0 ? '📈' : '📉';
      return '<tr><td>' + nm + '</td><td>' + bar + ' ' + f.toFixed(2) + '</td></tr>';
    }).join('');
    var uid = 'md_' + p.asset + '_' + Math.floor(Math.random() * 100000);
    return '<div class="model-details">' +
      '<a href="javascript:void(0)" class="details-toggle" onclick="document.getElementById(\'' + uid + '\').style.display = document.getElementById(\'' + uid + '\').style.display === \'none\' ? \'block\' : \'none\'; this.textContent = this.textContent.indexOf(\'▶\') > -1 ? \'▼ Hide model details\' : \'▶ View model details\';">▶ View model details</a>' +
      '<div id="' + uid + '" class="details-body" style="display:none">' +
      '<p><strong>Ensemble score:</strong> ' + (p.score !== undefined ? p.score : '—') +
      ' &nbsp; <strong>Geometric edge:</strong> ' + (p.geo_edge !== undefined ? p.geo_edge : '—') +
      ' &nbsp; <strong>Regime:</strong> ' + (p.regime || '—') + '</p>' +
      '<p><strong>Why this score</strong> — top model signals:</p>' +
      '<table class="feat-table"><tr><th>Signal</th><th>Value</th></tr>' + featRows + '</table>' +
      '<p class="live-stamp">Positive values push the score up (bullish), negative push it down. ' +
      'Kelly position is ' + (p.position_pct !== undefined ? p.position_pct : 0) + '% — it stays at 0% until the model has enough scored history to size positions.</p>' +
      '</div></div>';
  }

  function pickCard(p, side) {
    var cls = side === 'long' ? 'alert-buy' : 'alert-sell';
    var t = p.thesis || {};
    var bull = (t.bull_case || []).map(function (b) { return '<li>' + b + '</li>'; }).join('');
    var bear = (t.bear_case || []).map(function (b) { return '<li>' + b + '</li>'; }).join('');
    return '<div class="alert-card ' + cls + '" data-entry="' + p.price + '" data-target="' + (t.take_profit || 0) + '" data-stop="' + (t.stop_loss || 0) + '" data-exp-ret="' + (t.expected_return_pct || 0) + '" data-max-loss="' + (t.max_loss_pct || 0) + '">' +
      assetHeader(p, side) +
      '<p><strong>Buy:</strong> ' + (t.entry_date || 'today') + ' @ $' + Number(p.price).toLocaleString() +
      ' &nbsp; <strong>Sell target:</strong> ' + (t.projected_exit || '—') + ' @ $' + (t.take_profit !== undefined ? Number(t.take_profit).toLocaleString() : '—') + '</p>' +
      '<p><strong>Stop:</strong> $' + (t.stop_loss !== undefined ? Number(t.stop_loss).toLocaleString() : '—') +
      ' &nbsp; <strong>R:R</strong> ' + (t.risk_reward || '—') +
      ' &nbsp; <strong>Expected:</strong> +' + (t.expected_return_pct || 0) + '%</p>' +
      '<p><strong>Confidence:</strong> ' + p.confidence + '% · <strong>Position:</strong> ' + (p.position_pct !== undefined ? p.position_pct + '%' : '—') + ' (Kelly)</p>' +
      '<p class="profit-line" style="display:none"></p>' +
      '<p><strong>Bull case:</strong></p><ul class="thesis-list">' + bull + '</ul>' +
      '<p><strong>Bear case:</strong></p><ul class="thesis-list">' + bear + '</ul>' +
      '<p class="live-stamp">' + p.reason + '</p>' +
      '<p><a class="details-toggle" href="pick.html?asset=' + encodeURIComponent(p.asset) + '&type=swing&side=' + side + '">📄 Full detail page →</a></p>' +
      modelDetails(p) + '</div>';
  }

  function isTradable(p) {
    // Stocks/ETFs are tradable via brokerage; crypto needs exchange listing
    var cls = (p['class'] || '').toLowerCase();
    if (cls === 'equity') return true;
    if (cls === 'crypto' || (p.info_url && p.info_url.indexOf('coingecko') > -1)) {
      return (p.tradable || []).length > 0;
    }
    return true;
  }

  function setupTabs() {
    var tabs = document.querySelectorAll('#picks-tabs .tab-btn');
    tabs.forEach(function (btn) {
      btn.addEventListener('click', function () {
        tabs.forEach(function (b) { b.classList.remove('active'); });
        btn.classList.add('active');
        var tab = btn.getAttribute('data-tab');
        document.getElementById('tab-tradable').style.display = tab === 'tradable' ? 'block' : 'none';
        document.getElementById('tab-all').style.display = tab === 'all' ? 'block' : 'none';
        document.getElementById('tab-how').style.display = tab === 'how' ? 'block' : 'none';
      });
    });
  }

  function intradayCard(s, withBadge) {
    var t = s.thesis || {};
    var cls = s.direction === 'LONG' ? 'alert-buy' : 'alert-sell';
    var header = withBadge
      ? assetHeader(s).replace('</p>', ' <span class="live-stamp">' + s.confidence + '%</span></p>')
      : assetHeader(s);
    return '<div class="alert-card ' + cls + '">' +
      header +
      '<p><strong>In:</strong> ' + t.entry_time + ' @ $' + t.entry +
      ' &nbsp; <strong>Out by:</strong> ' + t.exit_by + '</p>' +
      '<p><strong>Stop:</strong> $' + t.stop_loss + ' &nbsp; <strong>Target:</strong> $' + t.take_profit + '</p>' +
      '<p><strong>1h:</strong> ' + t.chg_1h + '% · <strong>RSI:</strong> ' + t.rsi + '</p>' +
      '<p><a class="details-toggle" href="pick.html?asset=' + encodeURIComponent(s.asset) + '&type=intraday">📄 Full detail page →</a></p>' +
      '</div>';
  }

  // Intraday signals (hourly engine)
  function loadIntraday() {
    var el = document.getElementById('picks-intraday');
    var elAll = document.getElementById('picks-intraday-all');
    var stamp = document.getElementById('intraday-stamp');
    if (!el) return;
    fetch('data/intraday.json', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (d) {
        var all = (d.longs || []).concat(d.shorts || []);
        all.sort(function (a, b) { return b.confidence - a.confidence; });
        if (stamp && d.generated) {
          stamp.textContent = '(last ran ' + new Date(d.generated).toLocaleTimeString() + ')';
        }
        if (!all.length) {
          el.innerHTML = '<p class="live-stamp">No intraday signals this hour — market is quiet.</p>';
          if (elAll) elAll.innerHTML = '<p class="live-stamp">No intraday signals this hour.</p>';
          return;
        }
        // Tradable tab: filter to what Nic can trade
        var tradable = all.filter(isTradable);
        el.innerHTML = tradable.length
          ? tradable.slice(0, 6).map(function (s) { return intradayCard(s, true); }).join('')
          : '<p class="live-stamp">No tradable intraday signals this hour — check All Signals.</p>';
        // All tab: everything
        if (elAll) {
          elAll.innerHTML = all.slice(0, 6).map(function (s) { return intradayCard(s, false); }).join('');
        }
      })
      .catch(function () {
        el.innerHTML = '<p class="live-stamp">Intraday engine not yet run — check back after the next hourly cycle.</p>';
      });
  }

  function wireCalculator() {
    var container = document.querySelector('.container');
    if (!container || document.getElementById('profit-calc')) return;
    var div = document.createElement('div');
    div.className = 'card';
    div.id = 'profit-calc';
    div.innerHTML = '<h3>💰 Profit Calculator</h3>' +
      '<p>How much would you invest per pick? $' +
      '<input type="number" id="invest-amt" value="1000" min="1" step="50" style="width:120px;padding:8px;font-size:16px;"> ' +
      '<button class="btn" id="calc-profit">Calculate</button></p>' +
      '<div id="profit-results"></div>';
    var h1 = container.querySelector('h1');
    if (h1) h1.after(div);
    document.getElementById('calc-profit').addEventListener('click', function () {
      var amt = parseFloat(document.getElementById('invest-amt').value) || 0;
      if (amt <= 0) return;
      var cards = document.querySelectorAll('.alert-card[data-entry]');
      var results = [];
      cards.forEach(function (c) {
        var expRet = parseFloat(c.dataset.expRet) || 0;
        var maxLoss = parseFloat(c.dataset.maxLoss) || 0;
        var asset = c.querySelector('h4').textContent;
        var gain = amt * expRet / 100;
        var loss = amt * maxLoss / 100;
        results.push({ asset: asset, gain: gain, loss: loss, expRet: expRet });
        var line = c.querySelector('.profit-line');
        line.style.display = 'block';
        line.innerHTML = '<strong>If you invest $' + amt.toLocaleString() + ':</strong> ' +
          'profit <span style="color:var(--success)">+$' + gain.toFixed(0) + '</span> (' + expRet + '%) if target hits · ' +
          'risk <span style="color:var(--danger)">-$' + loss.toFixed(0) + '</span> if stopped out';
      });
      // Rank by expected gain
      results.sort(function (a, b) { return b.gain - a.gain; });
      var html = '<h4>Ranked by expected profit:</h4><ol>';
      results.forEach(function (r, i) {
        html += '<li><strong>' + r.asset + '</strong> — +$' + r.gain.toFixed(0) + ' (' + r.expRet + '%)</li>';
      });
      document.getElementById('profit-results').innerHTML = html + '</ol>';
    });
  }

  function init() {
    fetch('data/latest.json', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (d) {
        var stamp = document.getElementById('picks-stamp');
        var swingStamp = document.getElementById('swing-stamp');
        if (stamp) {
          var dt = new Date(d.generated);
          stamp.innerHTML = '<strong>Swing picks last ran:</strong> ' + dt.toLocaleString();
        }
        if (swingStamp) {
          var dt2 = new Date(d.generated);
          swingStamp.textContent = '(last ran ' + dt2.toLocaleTimeString() + ')';
        }
        var longs = document.getElementById('picks-longs');
        var longsAll = document.getElementById('picks-longs-all');
        if (longs) {
          var tradLongs = d.longs.filter(isTradable);
          longs.innerHTML = tradLongs.length
            ? tradLongs.map(function (p) { return pickCard(p, 'long'); }).join('')
            : '<div class="card"><p>No tradable long signals right now — check All Signals.</p></div>';
        }
        if (longsAll) {
          longsAll.innerHTML = d.longs.length
            ? d.longs.map(function (p) { return pickCard(p, 'long'); }).join('')
            : '<div class="card"><p>No long signals right now.</p></div>';
        }
        var shorts = document.getElementById('picks-shorts');
        var shortsAll = document.getElementById('picks-shorts-all');
        if (shorts) {
          var tradShorts = d.shorts.filter(isTradable);
          shorts.innerHTML = tradShorts.length
            ? tradShorts.map(function (p) { return pickCard(p, 'short'); }).join('')
            : '<div class="card"><p>No tradable short signals right now.</p></div>';
        }
        if (shortsAll) {
          shortsAll.innerHTML = d.shorts.length
            ? d.shorts.map(function (p) { return pickCard(p, 'short'); }).join('')
            : '<div class="card"><p>No short signals right now.</p></div>';
        }
        var ms = document.getElementById('model-stats');
        if (ms) {
          var tr = d.track_record || {};
          var er = d.model_error || {};
          ms.innerHTML =
            '<p><strong>Win rate:</strong> ' + (tr.win_rate !== null && tr.win_rate !== undefined ? tr.win_rate + '%' : 'warming up') + '</p>' +
            '<p><strong>Picks scored:</strong> ' + (tr.total || 0) + ' (' + (tr.wins || 0) + 'W / ' + (tr.losses || 0) + 'L)</p>' +
            '<p><strong>Mean abs error:</strong> ' + (er.mae !== null && er.mae !== undefined ? er.mae + '%' : '—') + '</p>' +
            '<p><strong>Within 5% target:</strong> ' + (er.within_5pct_rate !== null && er.within_5pct_rate !== undefined ? er.within_5pct_rate + '%' : '—') + '</p>' +
            '<p class="live-stamp">Target: 5% margin of error · weights re-optimized every run via row echelon form</p>';
        }
        var ps = document.getElementById('psychology');
        if (ps && d.psychology) {
          ps.innerHTML = '<p style="font-size:20px"><strong>' + (d.buy_text || d.regime || '—') + '</strong></p>' +
            '<p><strong>Fear &amp; Greed:</strong> ' + d.psychology.fear_greed +
            ' (' + (d.psychology.delta >= 0 ? '+' : '') + d.psychology.delta + ')</p>' +
            '<p><strong>Kelly size:</strong> ' + (d.kelly_pct !== undefined ? d.kelly_pct + '%' : '—') + '</p>' +
            d.psychology.notes.map(function (n) { return '<p>· ' + n + '</p>'; }).join('');
        }
        var lt = document.getElementById('long-term');
        if (lt && d.long_term) {
          lt.innerHTML = '<p><strong>Horizon:</strong> ' + d.long_term.horizon + '</p>' +
            d.long_term.thesis.map(function (t) { return '<p>· ' + t + '</p>'; }).join('');
        }

        var cw = document.getElementById('contracts');
        if (cw) {
          var html = '';
          if (d.contracts_watch && d.contracts_watch.length) {
            html += '<h4>Awarded (USASpending)</h4>' +
              d.contracts_watch.map(function (c) { return '<p>· ' + c + '</p>'; }).join('');
          }
          if (d.opportunities && d.opportunities.length) {
            html += '<h4>Open solicitations (SAM.gov)</h4>' +
              d.opportunities.slice(0, 10).map(function (o) {
                return '<p>· <strong>' + o.title + '</strong><br><span class="live-stamp">' +
                  o.agency + ' · ' + o.type + ' · due ' + o.deadline + ' · NAICS ' + o.naics + '</span></p>';
              }).join('');
          }
          cw.innerHTML = html || '<p class="live-stamp">No contracts flagged this run.</p>';
        }
      })
      .catch(function () {
        var stamp = document.getElementById('picks-stamp');
        if (stamp) stamp.textContent = 'Picks not yet generated — the NexIntel loop runs every 6 hours.';
      });
    wireCalculator();
    loadIntraday();
    setupTabs();
    // Auto-refresh every 5 minutes for live updates
    setInterval(function () {
      loadIntraday();
      // Re-fetch swing picks silently
      fetch('data/latest.json', { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var stamp = document.getElementById('picks-stamp');
          var swingStamp = document.getElementById('swing-stamp');
          if (stamp && d.generated) {
            stamp.innerHTML = '<strong>Swing picks last ran:</strong> ' + new Date(d.generated).toLocaleString();
          }
          if (swingStamp && d.generated) {
            swingStamp.textContent = '(last ran ' + new Date(d.generated).toLocaleTimeString() + ')';
          }
        }).catch(function () {});
    }, 300000);
    // Longs-only toggle (persisted)
    var loToggle = document.getElementById('longs-only');
    var shortsGrid = document.getElementById('picks-shorts');
    var shortsHeader = document.getElementById('shorts-header');
    function applyLongsOnly() {
      var on = loToggle.checked;
      try { localStorage.setItem('nx_longs_only', on ? '1' : '0'); } catch (e) {}
      if (shortsGrid) shortsGrid.style.display = on ? 'none' : '';
      if (shortsHeader) shortsHeader.style.display = on ? 'none' : '';
      // Also filter intraday cards
      document.querySelectorAll('#picks-intraday .alert-sell').forEach(function (c) {
        c.style.display = on ? 'none' : '';
      });
    }
    if (loToggle) {
      try { loToggle.checked = localStorage.getItem('nx_longs_only') === '1'; } catch (e) {}
      loToggle.addEventListener('change', applyLongsOnly);
      applyLongsOnly();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
