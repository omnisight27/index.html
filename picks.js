// OmniSight picks page — renders the autonomous NexIntel loop output.
// Reads data/latest.json (committed by GitHub Actions 4x daily).

(function () {
  'use strict';

  // Plain-English regime labels (internal values stay RISK_ON/RISK_OFF/NEUTRAL)
  var REGIME_LABELS = {
    'RISK_ON': '🟢 Conditions favor buying',
    'RISK_OFF': '🔴 Conditions favor caution',
    'NEUTRAL': '🟡 Mixed conditions'
  };
  function regimeLabel(r) { return REGIME_LABELS[r] || r || '—'; }
  // Soften raw regime tokens inside free-text reasons ("+ RISK_OFF)" reads as jargon)
  function plainReason(text) {
    return String(text || '')
      .replace(/RISK_OFF/g, 'conditions favor caution')
      .replace(/RISK_ON/g, 'conditions favor buying')
      .replace(/NEUTRAL/g, 'mixed conditions');
  }

  function assetBadge(p) {
    var cls = (p['class'] || '').toLowerCase();
    // Intraday signals are always crypto (no class field set — detect via CoinGecko link)
    if (cls === 'crypto' || (!p['class'] && p.info_url && p.info_url.indexOf('coingecko') > -1)) return '<span class="asset-badge badge-crypto">🪙 Crypto</span>';
    var nm = (p.name || '').toUpperCase();
    if (nm.indexOf('ETF') > -1 || nm.indexOf('SPDR') > -1 || nm.indexOf('SELECT SECTOR') > -1) return '<span class="asset-badge badge-etf">📊 ETF</span>';
    return '<span class="asset-badge badge-stock">📈 Stock</span>';
  }

  // Market qualifier for detail links: CVX is two assets (crypto Convex
  // Finance, stock Chevron), so every link to pick.html carries &market=
  // and lands on the right one instead of whichever the page finds first.
  function marketParamOf(p) {
    var cls = (p['class'] || '').toLowerCase();
    if (cls === 'crypto' || (p.info_url && p.info_url.indexOf('coingecko') > -1)) return 'crypto';
    if (cls === 'equity' || p.cik) return 'stock';
    return '';
  }
  function detailLink(p, type, side) {
    var q = 'pick.html?asset=' + encodeURIComponent(p.asset) + '&type=' + type + (side ? '&side=' + side : '');
    var m = marketParamOf(p);
    return q + (m ? '&market=' + m : '');
  }

  function tradableBadge(p) {
    if ((p['class'] || '').toLowerCase() === 'equity') return ' <span class="trade-badge trade-yes">✅ Schwab</span>';
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
      ' &nbsp; <strong>Regime:</strong> ' + regimeLabel(p.regime) + '</p>' +
      (p.tf_align ? '<p><strong>Timeframe alignment:</strong> ' + p.tf_align + ' <span class="live-stamp">(1h / 24h / 7d / 14d / 30d)</span></p>' : '') +
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
      '<p class="live-stamp">' + plainReason(p.reason) + '</p>' +
      '<p><a class="details-toggle" href="' + detailLink(p, 'swing', side) + '">📄 Full detail page →</a></p>' +
      modelDetails(p) + '</div>';
  }

  // News-flagged picks are hidden everywhere on this page (Nic's request,
  // 2026-10-22) — the grids below filter them out before rendering.
  function notNewsFlagged(p) { return !p.news_flag; }

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
      '<p><strong>In:</strong> ' + fmtClockET(t.entry_time) + ' @ $' + t.entry +
      ' &nbsp; <strong>Out by:</strong> ' + fmtClockET(t.exit_by) + '</p>' +
      '<p><strong>Stop:</strong> $' + t.stop_loss + ' &nbsp; <strong>Target:</strong> $' + t.take_profit + '</p>' +
      '<p><strong>1h:</strong> ' + t.chg_1h + '% · <strong>RSI:</strong> ' + t.rsi + '</p>' +
      '<p><a class="details-toggle" href="' + detailLink(s, 'intraday') + '">📄 Full detail page →</a></p>' +
      '</div>';
  }

  // Movers + watch strips (intraday.json) — shared state for detail links
  var lastIntraday = null;
  var lastSwing = null;  // latest.json payload, for the verdict strip
  var swingSides = {};  // asset -> 'long' | 'short', filled from latest.json

  function fmtPlanPrice(v) {
    v = Number(v);
    if (!isFinite(v)) return '—';
    if (v >= 1) return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (v >= 0.0001) return String(+v.toFixed(4));
    // Dust-priced (sub-$0.0001): toFixed(4) would read "$0.0000" — keep
    // enough decimals to stay non-zero, trailing zeros trimmed.
    var s = v.toFixed(10).replace(/0+$/, '');
    return s.charAt(s.length - 1) === '.' ? s.slice(0, -1) : s;
  }

  // Engine clock strings arrive as "HH:MM UTC" (intraday) or a plain
  // date "YYYY-MM-DD" (swing projected exit). Nic reads ET: convert
  // clock times to ET, leave dates untouched.
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

  function moverCard(m) {
    var up = m.direction === 'UP';
    var cls = up ? 'alert-buy' : 'alert-sell';
    var icon = up ? '🚀' : '📉';
    var nm = m.name && m.name !== m.asset ? ' <span class="asset-name">' + m.name + '</span>' : '';
    var link = m.info_url ? ' <a class="asset-link" href="' + m.info_url + '" target="_blank" rel="noopener">What is this? ↗</a>' : '';
    // Full detail link only when the mover is also a model pick
    var detail = '';
    var intra = lastIntraday && ((lastIntraday.longs || []).concat(lastIntraday.shorts || []))
      .some(function (s) { return s.asset === m.asset; });
    if (intra) {
      detail = '<p><a class="details-toggle" href="' + detailLink(m, 'intraday') + '">📄 Full detail page →</a></p>';
    } else if (swingSides[m.asset]) {
      detail = '<p><a class="details-toggle" href="' + detailLink(m, 'swing', swingSides[m.asset]) + '">📄 Full detail page →</a></p>';
    }
    var watchNote = m.watch_reason ? ' <span class="live-stamp">👀 ' + m.watch_reason + '</span>' : '';
    // Trade plan: buy / sell target / stop / out-by when the engine attached
    // one; a muted note line when it deliberately didn't (falling movers).
    var planLine = '';
    if (m.plan) {
      planLine = '<p><strong>Buy</strong> ~$' + fmtPlanPrice(m.plan.entry) +
        ' · 🎯 <strong>Sell target</strong> $' + fmtPlanPrice(m.plan.take_profit) +
        ' · 🛑 <strong>Stop</strong> $' + fmtPlanPrice(m.plan.stop_loss) +
        ' · <strong>Out by</strong> ' + fmtClockET(m.plan.exit_by) + '</p>';
    } else if (m.plan_note) {
      planLine = '<p class="live-stamp">' + m.plan_note + '</p>';
    }
    return '<div class="alert-card ' + cls + '">' +
      '<h4>' + icon + ' ' + m.asset + '</h4>' +
      '<p class="asset-sub">' + assetBadge(m) + nm + link + tradableBadge(m) + '</p>' +
      '<p><strong>' + (m.chg_24h >= 0 ? '+' : '') + m.chg_24h + '% 24h</strong> · ' +
      (m.chg_1h >= 0 ? '+' : '') + m.chg_1h + '% 1h · $' + Number(m.price).toLocaleString() + watchNote + '</p>' +
      planLine +
      detail + '</div>';
  }

  // Setting Up card (pre-move accumulation): compact — name, price, reason.
  function accumCard(m) {
    var nm = m.name && m.name !== m.asset ? ' <span class="asset-name">' + m.name + '</span>' : '';
    var link = m.info_url ? ' <a class="asset-link" href="' + m.info_url + '" target="_blank" rel="noopener">What is this? ↗</a>' : '';
    return '<div class="alert-card alert-hold">' +
      '<h4>🔭 ' + m.asset + '</h4>' +
      '<p class="asset-sub">' + assetBadge(m) + nm + link + tradableBadge(m) + '</p>' +
      '<p>$' + Number(m.price).toLocaleString() + ' · <span class="live-stamp">' + (m.watch_reason || '') + '</span></p>' +
      '</div>';
  }

  function renderMovers() {
    if (!lastIntraday) return;
    function fill(id, list, heading, emptyText, tradableOnly, cardFn) {
      var el = document.getElementById(id);
      if (!el) return;
      var items = tradableOnly ? list.filter(isTradable) : list;
      el.innerHTML = '<h3>' + heading + '</h3>' +
        (items.length ? '<div class="grid">' + items.map(cardFn || moverCard).join('') + '</div>'
                      : '<p class="live-stamp">' + emptyText + '</p>');
    }
    var movers = lastIntraday.movers || [];
    var watch = lastIntraday.watch || [];
    var accum = lastIntraday.accum || [];
    fill('movers-tradable', movers, '🚀 Movers <span class="live-stamp">(top 1,000 scan)</span>', 'No tradable movers above threshold this hour.', true);
    fill('accum-tradable', accum, '🔭 Setting Up <span class="live-stamp">(volume building before price moves)</span>', 'Nothing tradable setting up right now.', true, accumCard);
    fill('watch-tradable', watch, '👀 Watching <span class="live-stamp">(early signs — volume surge or fresh turn)</span>', 'Nothing tradable showing early signs right now.', true);
    fill('movers-all', movers, '🚀 Movers <span class="live-stamp">(top 1,000 scan)</span>', 'No movers above threshold this hour.', false);
    fill('accum-all', accum, '🔭 Setting Up <span class="live-stamp">(volume building before price moves)</span>', 'Nothing setting up right now.', false, accumCard);
    fill('watch-all', watch, '👀 Watching <span class="live-stamp">(early signs — volume surge or fresh turn)</span>', 'Nothing showing early signs right now.', false);
  }

  // ---------- 🎯 Today's Verdicts ----------
  // One plain-word verdict per surfaced asset, computed in the browser from
  // the two payloads this page already loads — no new engine fields.
  //   BUY      — intraday LONG signal with confidence >= 60; OR swing long
  //              with confidence >= 60 AND >= 4 of 5 timeframes agreeing;
  //              OR a mover carrying a plan whose 1h change is still
  //              positive AND whose 24h change is >= +15%.
  //   CLOSEST  — only when no BUY exists: the highest-confidence long
  //   TO A BUY   candidate under the bar, with the gap in plain words.
  //   WATCHING — top watch-list / Setting Up items (max 3).
  // News-flagged picks and falling movers used to surface here as AVOID
  // cards; Nic asked not to see them at all (2026-10-22), so they are
  // never rendered. A news-flagged asset can still never be a BUY (the
  // ZEC lesson: a bad headline outranks a model score).
  function tfAgree(p) {
    var v = p && p.tf_align;
    if (v === null || v === undefined || v === '') return null;
    if (typeof v === 'number') return v <= 1 ? Math.round(v * 5) : Math.round(v);
    var m = String(v).match(/(\d+)\s*\/\s*5/) || String(v).match(/(\d+)\s+of\s+5/i);
    return m ? parseInt(m[1], 10) : null;
  }

  function detailFor(asset) {
    var intra = lastIntraday && ((lastIntraday.longs || []).concat(lastIntraday.shorts || []))
      .find(function (s) { return s.asset === asset; });
    if (intra) return detailLink(intra, 'intraday');
    if (swingSides[asset] && lastSwing) {
      var sw = ((lastSwing.longs || []).concat(lastSwing.shorts || []))
        .find(function (p) { return p.asset === asset; });
      if (sw) return detailLink(sw, 'swing', swingSides[asset]);
    }
    return null;
  }

  function planLine(plan) {
    if (!plan || plan.take_profit === undefined || plan.stop_loss === undefined) return '';
    return '<p><strong>Buy</strong> ~$' + fmtPlanPrice(plan.entry) +
      ' · 🎯 <strong>Sell target</strong> $' + fmtPlanPrice(plan.take_profit) +
      ' · 🛑 <strong>Stop</strong> $' + fmtPlanPrice(plan.stop_loss) +
      (plan.exit_by ? ' · <strong>Out by</strong> ' + fmtClockET(plan.exit_by) : '') + '</p>';
  }

  function verdictCard(v) {
    var badges = {
      'BUY': ['#10b981', '#ffffff'],
      'CLOSEST TO A BUY': ['#f59e0b', '#0b1220'],
      'WATCHING': ['#38bdf8', '#0b1220']
    };
    var b = badges[v.verdict] || ['#6b7280', '#ffffff'];
    var cls = v.verdict === 'BUY' ? 'alert-buy' : 'alert-hold';
    var nm = v.name && v.name !== v.asset ? ' <span class="asset-name">' + v.name + '</span>' : '';
    var link = v.info_url ? ' <a class="asset-link" href="' + v.info_url + '" target="_blank" rel="noopener">What is this? ↗</a>' : '';
    var detail = v.detail ? '<p><a class="details-toggle" href="' + v.detail + '">📄 Full detail page →</a></p>' : '';
    return '<div class="alert-card ' + cls + '">' +
      '<h4><span style="background:' + b[0] + ';color:' + b[1] + ';padding:2px 10px;border-radius:999px;font-size:13px;vertical-align:middle">' + v.verdict + '</span> ' + v.asset + '</h4>' +
      '<p class="asset-sub">' + assetBadge(v) + nm + link + tradableBadge(v) + '</p>' +
      '<p>' + v.reason + '</p>' +
      planLine(v.plan) +
      detail + '</div>';
  }

  function computeVerdicts(swing, intra) {
    swing = swing || {};
    intra = intra || {};
    var seen = {};
    var buys = [], watching = [];
    var swingLongs = swing.longs || [];
    var intraLongs = (intra.longs || []).filter(function (s) { return !s.direction || s.direction === 'LONG'; });
    var movers = intra.movers || [];
    var newsFlagged = {};
    (swing.longs || []).concat(swing.shorts || []).forEach(function (p) {
      if (p.news_flag) newsFlagged[p.asset] = p;
    });
    // 'seen' is keyed by asset+market so two different assets sharing a
    // ticker (e.g. crypto CVX Convex Finance vs stock CVX Chevron) each get
    // their own verdict card instead of the second being silently dropped.
    function seenKey(p) { return p.asset + '|' + marketParamOf(p); }
    function push(list, v) { var k = seenKey(v); if (!seen[k]) { seen[k] = 1; list.push(v); } }

    // BUY — intraday signals, then aligned swing longs, then hot movers
    intraLongs.forEach(function (s) {
      if ((s.confidence || 0) >= 60 && !newsFlagged[s.asset]) {
        var t = s.thesis || {};
        push(buys, { verdict: 'BUY', asset: s.asset, name: s.name, info_url: s.info_url, 'class': s['class'], tradable: s.tradable,
          reason: 'Intraday signal at ' + s.confidence + '% confidence — past the 60% buy bar.',
          plan: t.entry !== undefined ? { entry: t.entry, take_profit: t.take_profit, stop_loss: t.stop_loss, exit_by: t.exit_by } : null,
          detail: detailLink(s, 'intraday') });
      }
    });
    swingLongs.forEach(function (p) {
      var agree = tfAgree(p);
      if ((p.confidence || 0) >= 60 && agree !== null && agree >= 4 && !newsFlagged[p.asset]) {
        var t = p.thesis || {};
        push(buys, { verdict: 'BUY', asset: p.asset, name: p.name, info_url: p.info_url, 'class': p['class'], tradable: p.tradable,
          reason: p.confidence + '% confidence with ' + agree + ' of 5 timeframes agreeing — clears the buy bar.',
          plan: { entry: p.price, take_profit: t.take_profit, stop_loss: t.stop_loss, exit_by: t.projected_exit },
          detail: detailLink(p, 'swing', 'long') });
      }
    });
    movers.forEach(function (m) {
      if (m.plan && (m.chg_1h || 0) > 0 && (m.chg_24h || 0) >= 15) {
        push(buys, { verdict: 'BUY', asset: m.asset, name: m.name, info_url: m.info_url, 'class': m['class'], tradable: m.tradable,
          reason: 'Mover still climbing: +' + m.chg_24h + '% in 24h and +' + m.chg_1h + '% this hour, with a plan attached.',
          plan: m.plan, detail: detailFor(m.asset) });
      }
    });

    // CLOSEST TO A BUY — only when nothing cleared the bar
    var closest = null;
    if (!buys.length) {
      var cands = [];
      swingLongs.forEach(function (p) {
        if (!newsFlagged[p.asset] && !seen[seenKey(p)]) cands.push({ src: 'swing', p: p, conf: p.confidence || 0 });
      });
      intraLongs.forEach(function (s) {
        if (!newsFlagged[s.asset] && !seen[seenKey(s)]) cands.push({ src: 'intra', p: s, conf: s.confidence || 0 });
      });
      cands.sort(function (a, b) { return b.conf - a.conf; });
      if (cands.length) {
        var c = cands[0], p = c.p, t = p.thesis || {}, gap;
        if (c.conf < 60) {
          gap = c.conf + '% confidence — the buy bar is 60%.';
        } else {
          var ag = tfAgree(p);
          gap = c.conf + '% confidence, but ' +
            (ag === null ? 'no timeframe confirmation yet' : 'only ' + ag + ' of 5 timeframes agree') +
            ' — the buy bar needs 4 of 5 agreeing.';
        }
        closest = { verdict: 'CLOSEST TO A BUY', asset: p.asset, name: p.name, info_url: p.info_url, 'class': p['class'], tradable: p.tradable,
          reason: 'Closest thing to a buy right now: ' + gap,
          plan: c.src === 'swing'
            ? { entry: p.price, take_profit: t.take_profit, stop_loss: t.stop_loss, exit_by: t.projected_exit }
            : (t.entry !== undefined ? { entry: t.entry, take_profit: t.take_profit, stop_loss: t.stop_loss, exit_by: t.exit_by } : null),
          detail: c.src === 'swing'
            ? detailLink(p, 'swing', 'long')
            : detailLink(p, 'intraday') };
        seen[seenKey(p)] = 1;
      }
    }

    // WATCHING — early signs, top 3 across watch + Setting Up
    (intra.watch || []).concat(intra.accum || []).forEach(function (m) {
      if (watching.length >= 3) return;
      push(watching, { verdict: 'WATCHING', asset: m.asset, name: m.name, info_url: m.info_url, 'class': m['class'], tradable: m.tradable,
        reason: 'Watching — ' + plainReason(m.watch_reason || 'early signs building'),
        plan: m.plan || null, detail: detailFor(m.asset) });
    });

    var cards = buys.concat(closest ? [closest] : []).concat(watching);
    var emptyNote = null;
    if (!buys.length && !closest) {
      var lbl = swing.regime_label || (swing.regime ? regimeLabel(swing.regime) : null);
      emptyNote = lbl
        ? 'No buy today — ' + lbl + '; the model would rather sit out than force a trade.'
        : 'No buy today — the model would rather sit out than force a trade.';
    }
    return { cards: cards, emptyNote: emptyNote };
  }

  function renderVerdicts() {
    var el = document.getElementById('verdicts');
    if (!el) return;
    if (!lastSwing && !lastIntraday) return;
    var res = computeVerdicts(lastSwing || {}, lastIntraday || {});
    var html = '<h3>🎯 Today\'s Verdicts</h3>';
    if (res.emptyNote) html += '<div class="card"><p>' + res.emptyNote + '</p></div>';
    if (res.cards.length) html += '<div class="grid">' + res.cards.map(verdictCard).join('') + '</div>';
    html += '<p class="live-stamp">Verdicts are the system\'s paper-grade calls — every BUY is recorded in the 📒 Proof Book (Reports). Real-money buys follow Nic\'s protocol: Queen B calls it first.</p>';
    el.innerHTML = html;
  }

  // Intraday signals (hourly engine)
  function loadIntraday() {
    var el = document.getElementById('picks-intraday');
    var elAll = document.getElementById('picks-intraday-all');
    if (!el) return;
    fetch('data/intraday.json', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (d) {
        lastIntraday = d;
        renderMovers();  // movers/watch render even when no signals fire
        renderVerdicts();
        var all = (d.longs || []).concat(d.shorts || []);
        all.sort(function (a, b) { return b.confidence - a.confidence; });
        // Unified freshness stamp at the top of the page — the intraday
        // file is this page's freshest surface, so it anchors the line
        // (replaces the old per-section "last ran" texts).
        if (window.nxStampInto) window.nxStampInto(document.getElementById('picks-stamp'), d);
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
        var ps = document.getElementById('picks-stamp');
        if (ps && !ps.getAttribute('data-stamped') && ps.textContent.indexOf('Loading') === 0) ps.textContent = '';
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
        // Freshness stamp: owned by loadIntraday() (unified data stamp) —
        // the old "Swing picks last ran" write here was consolidated away.
        var longs = document.getElementById('picks-longs');
        var longsAll = document.getElementById('picks-longs-all');
        if (longs) {
          var tradLongs = d.longs.filter(notNewsFlagged).filter(isTradable);
          longs.innerHTML = tradLongs.length
            ? tradLongs.map(function (p) { return pickCard(p, 'long'); }).join('')
            : '<div class="card"><p>No tradable long signals right now — check All Signals.</p></div>';
        }
        if (longsAll) {
          var allLongs = d.longs.filter(notNewsFlagged);
          longsAll.innerHTML = allLongs.length
            ? allLongs.map(function (p) { return pickCard(p, 'long'); }).join('')
            : '<div class="card"><p>No long signals right now.</p></div>';
        }
        var shorts = document.getElementById('picks-shorts');
        var shortsAll = document.getElementById('picks-shorts-all');
        if (shorts) {
          var tradShorts = d.shorts.filter(notNewsFlagged).filter(isTradable);
          shorts.innerHTML = tradShorts.length
            ? tradShorts.map(function (p) { return pickCard(p, 'short'); }).join('')
            : '<div class="card"><p>No tradable short signals right now.</p></div>';
        }
        if (shortsAll) {
          var allShorts = d.shorts.filter(notNewsFlagged);
          shortsAll.innerHTML = allShorts.length
            ? allShorts.map(function (p) { return pickCard(p, 'short'); }).join('')
            : '<div class="card"><p>No short signals right now.</p></div>';
        }
        // Remember swing pick sides so mover cards can link to detail pages
        (d.longs || []).forEach(function (p) { swingSides[p.asset] = 'long'; });
        (d.shorts || []).forEach(function (p) { swingSides[p.asset] = 'short'; });
        lastSwing = d;
        renderMovers();
        renderVerdicts();
        var ms = document.getElementById('model-stats');
        if (ms) {
          var tr = d.track_record || {};
          var er = d.model_error || {};
          ms.innerHTML =
            '<p><strong>Win rate:</strong> ' + (tr.win_rate !== null && tr.win_rate !== undefined ? tr.win_rate + '%' : 'warming up') + '</p>' +
            '<p><strong>Picks scored:</strong> ' + (tr.total || 0) + ' (' + (tr.wins || 0) + 'W / ' + (tr.losses || 0) + 'L)</p>' +
            '<p><strong>Mean abs error:</strong> ' + (er.mae !== null && er.mae !== undefined ? er.mae + '%' : '—') + '</p>' +
            '<p><strong>Within 5% target:</strong> ' + (er.within_5pct_rate !== null && er.within_5pct_rate !== undefined ? er.within_5pct_rate + '%' : '—') + '</p>' +
            (d.calibration || []).filter(function (c) { return c.n >= 3 && c.win_rate !== null && c.win_rate !== undefined; })
              .map(function (c) { return '<p><strong>Calibration:</strong> says ' + c.band + '% → actually wins ' + c.win_rate + '% (n=' + c.n + ')</p>'; }).join('') +
            '<p class="live-stamp">Target: 5% margin of error · weights re-optimized every run via row echelon form</p>';
        }
        var ps = document.getElementById('psychology');
        if (ps && d.psychology) {
          ps.innerHTML = '<p style="font-size:20px"><strong>' + (d.regime_label || d.buy_text || d.regime || '—') + '</strong></p>' +
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
          if (d.opportunities_nonprofit && d.opportunities_nonprofit.length) {
            html += '<h4>🏛️ Nonprofit opportunities (SAM.gov)</h4>' +
              d.opportunities_nonprofit.slice(0, 10).map(function (o) {
                return '<p>· <strong>' + o.title + '</strong><br><span class="live-stamp">' +
                  o.agency + ' · due ' + o.deadline + ' · NAICS ' + o.naics +
                  (o.state && o.state !== '?' ? ' · ' + o.state : '') +
                  (o.nonprofit_match ? ' · ' + o.nonprofit_match : '') + '</span></p>';
              }).join('');
          } else if (d.opportunities_nonprofit) {
            html += '<h4>🏛️ Nonprofit opportunities (SAM.gov)</h4>' +
              '<p class="live-stamp">No nonprofit-relevant solicitations in the watch right now — the engine checks one NAICS code per run, rotating through the list.</p>';
          }
          if (d.sam_pulse) {
            var p = d.sam_pulse;
            var dir = '';
            if (p.direction === 'rising' || p.direction === 'cooling' || p.direction === 'flat') {
              dir = ' — volume ' + p.direction + (p.direction_pct != null ? ' ' + Math.abs(p.direction_pct) + '% vs yesterday' : '');
            }
            var top = (p.top_agencies && p.top_agencies.length)
              ? '<br><span class="live-stamp">Most active: ' + p.top_agencies.map(function (a) { return a.agency + ' (' + a.count + ')'; }).join(', ') + ' — among the latest ' + (p.sampled || 25) + ' sampled.</span>' : '';
            html += '<h4>📡 Federal contracting pulse (SAM.gov)</h4><p>' +
              (p.total ? p.total.toLocaleString() + ' solicitations posted in the last 7 days' + (p.total_is_sample ? ' (sampled rows)' : '') : 'No solicitation count this run') + dir + top + '</p>';
          }
          cw.innerHTML = html || '<p class="live-stamp">No contracts flagged this run.</p>';
        }
      })
      .catch(function () {
        var stamp = document.getElementById('picks-stamp');
        if (stamp && !stamp.getAttribute('data-stamped')) stamp.textContent = 'Picks not yet generated — the NexIntel loop runs every 6 hours.';
      });
    wireCalculator();
    loadIntraday();
    setupTabs();
    // Auto-refresh every 5 minutes for live updates — loadIntraday() also
    // refreshes the unified data stamp. (The separate latest.json re-fetch
    // that used to live here only rewrote "last ran" texts; consolidated.
    // A quiet latest.json re-fetch remains ONLY to keep the verdict strip's
    // swing side fresh — it writes no stamps and renders no grids.)
    setInterval(function () {
      loadIntraday();
      fetch('data/latest.json', { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          lastSwing = d;
          renderVerdicts();
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
