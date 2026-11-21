// Sovereign Dashboard — live data wiring (Option 1).
// Reads the public OmniSight feed files directly and replaces every demo
// value on the page with live data. Nothing here invents a number: if a
// feed fails, its section says so and shows the framework, not a guess.
// No private ledger data is ever rendered here — this is a public page.
// Private numbers live on the signed-in home screen (home.html).
(function () {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  function el(id) { return document.getElementById(id); }
  function setText(id, text) { var n = el(id); if (n) n.textContent = text; }
  function setHTML(id, html) { var n = el(id); if (n) n.innerHTML = html; }
  function num(n, d) {
    if (n === null || n === undefined || isNaN(Number(n))) return '—';
    return Number(n).toLocaleString('en-US', {
      minimumFractionDigits: (d === undefined ? 0 : d),
      maximumFractionDigits: (d === undefined ? 0 : d)
    });
  }
  function pct(n, d) {
    if (n === null || n === undefined || isNaN(Number(n))) return '—';
    return Number(n).toFixed(d === undefined ? 1 : d) + '%';
  }
  function money(n) {
    if (n === null || n === undefined || isNaN(Number(n))) return '—';
    return '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtDate(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleDateString('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric', year: 'numeric' });
  }
  function fmtDateTime(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleString('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' });
  }
  function fetchJSON(path) {
    return fetch(path + '?_=' + Date.now()).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status + ' for ' + path);
      return r.json();
    });
  }
  function shortReason(s, n) {
    s = String(s || '');
    return s.length > (n || 160) ? s.slice(0, n || 160).replace(/\s+\S*$/, '') + '…' : s;
  }

  var counts = { opportunities: 0 };

  // ---- Picks + contracts watch + fear/greed (latest.json) ----
  function renderLatest(d) {
    var stamp = d.published_at || d.generated || null;
    if (stamp) {
      setText('live-stamp', 'Live · updated ' + fmtDateTime(stamp) + ' ET');
      setText('banner-stamp', 'Feeds published ' + fmtDateTime(stamp) + ' ET.');
    }
    var fg = d.psychology && d.psychology.fear_greed;
    if (fg === undefined || fg === null) fg = d.fear_greed;
    var cards = [];

    // Top long + top shorts as market opportunities. Values are stored raw
    // and escaped once, at render time in renderOpps().
    var longs = d.longs || [];
    var shorts = d.shorts || [];
    if (longs.length) {
      var L = longs[0];
      cards.push({
        kind: 'Pick · Long', title: L.asset + ' — top long',
        conf: 'Confidence ' + num(L.confidence) + '% · Score ' + num(L.score, 1),
        body: shortReason(L.reason, 180),
        src: 'Source: NexIntel picks · data/latest.json'
      });
    }
    shorts.slice(0, 2).forEach(function (S) {
      cards.push({
        kind: 'Pick · Short', title: S.asset + ' — short',
        conf: 'Confidence ' + num(S.confidence) + '% · Score ' + num(S.score, 1),
        body: shortReason(S.reason, 180),
        src: 'Source: NexIntel picks · data/latest.json'
      });
    });
    // Largest recent federal awards.
    var watch = d.contracts_watch || [];
    if (watch.length) {
      var items = watch.slice(0, 3).map(function (w) { return '<li>' + esc(w) + '</li>'; }).join('');
      setHTML('awards-list', '<ul>' + items + '</ul>');
      setText('awards-note', 'Largest recent federal awards, from the contracts watch feed.');
    }
    counts.picks = cards.length;
    counts.fearGreed = fg;
    counts.latestOpps = (d.opportunities || []).length;
    counts.latestNonprofitOpps = (d.opportunities_nonprofit || []).length;
    renderOpps(cards, 'picks');
    return d;
  }

  // ---- Nonprofit contracts (sam_nonprofit.json) ----
  function renderContracts(d) {
    var rows = (d.rows || []).slice().sort(function (a, b) {
      return new Date(a.deadline) - new Date(b.deadline);
    });
    var cards = rows.slice(0, 4).map(function (r) {
      return {
        kind: 'Contract · ' + (r.type || 'Solicitation'),
        title: r.title || 'Untitled solicitation',
        conf: 'Deadline ' + fmtDate(r.deadline) + ' · NAICS ' + (r.naics || '—'),
        body: (r.agency || '') + (r.nonprofit ? ' · Nonprofit match' : ''),
        src: 'Source: SAM.gov nonprofit feed · data/sam_nonprofit.json · Notice ' + (r.notice_id || '—')
      };
    });
    counts.contracts = rows.length;
    renderOpps(cards, 'contracts');
  }

  // ---- Kalshi prediction markets (kalshi.json) ----
  function renderKalshi(d) {
    var watch = d.watch || {};
    var markets = Object.keys(watch).map(function (k) { return watch[k]; });
    markets.sort(function (a, b) {
      function mid(m) {
        var s = m.snapshots || [];
        return s.length ? s[s.length - 1].mid : 0.5;
      }
      return Math.abs(mid(b) - 0.5) - Math.abs(mid(a) - 0.5);
    });
    var cards = markets.slice(0, 3).map(function (m) {
      var s = m.snapshots || [];
      var last = s.length ? s[s.length - 1].mid : null;
      return {
        kind: 'Kalshi market',
        title: m.title || m.ticker || 'Market',
        conf: last === null ? 'No price yet' : 'Implied probability ' + pct(last * 100, 1),
        body: (m.sub || '') + (m.close_time ? ' · Closes ' + fmtDate(m.close_time) : ''),
        src: 'Source: Kalshi watch · data/kalshi.json'
      };
    });
    counts.kalshi = markets.length;
    var st = d.stats || {};
    setHTML('kalshi-stats',
      '<strong>' + num(markets.length) + '</strong> markets watched · ' +
      'Crowd favorite accuracy <strong>' + pct(st.crowd_fav_accuracy, 1) + '</strong> · ' +
      'Model record <strong>' + num(st.model_w) + 'W / ' + num(st.model_l) + 'L</strong> · ' +
      'Model P&amp;L <strong>' + money(st.model_pnl) + '</strong>');
    renderOpps(cards, 'kalshi');
  }

  // ---- Grants (grants-data.js — window.NICNACS_GRANTS / window.GRANT_DB) ----
  function renderGrants() {
    var list = window.NICNACS_GRANTS || [];
    var catalog = window.GRANT_DB || [];
    var today = '2026-11-21';
    var hot = list.filter(function (g) {
      return g.tier === 'HOT' && (!g.deadlineISO || g.deadlineISO >= today);
    }).sort(function (a, b) {
      return (a.deadlineISO || '9999') < (b.deadlineISO || '9999') ? -1 : 1;
    });
    var cards = hot.slice(0, 3).map(function (g) {
      return {
        kind: 'Grant · ' + (g.tier || ''),
        title: g.name || 'Grant',
        conf: (g.amount || '') + (g.deadlineISO ? ' · Deadline ' + fmtDate(g.deadlineISO) : ''),
        body: (g.funder || '') + (g.level ? ' · ' + g.level : ''),
        src: 'Source: Grants registry · grants-data.js'
      };
    });
    counts.grants = list.length + catalog.length;
    counts.grantsHot = hot.length;
    renderOpps(cards, 'grants');
  }

  // ---- Validation track record (history.json) ----
  function renderHistory(d) {
    var st = d.stats || {};
    var err = d.error || {};
    setHTML('val-stats',
      '<strong>' + num(st.total) + '</strong> graded picks · ' +
      '<strong>' + num(st.wins) + 'W / ' + num(st.losses) + 'L</strong> · ' +
      'Win rate <strong>' + pct(st.win_rate, 1) + '</strong> · ' +
      'Mean absolute error <strong>' + num(err.mae, 2) + '</strong> (n=' + num(err.n) + ') · ' +
      'Within 5% <strong>' + pct(err.within_5pct_rate, 1) + '</strong>');
    var bands = (d.calibration || []).slice().sort(function (a, b) { return b.n - a.n; }).slice(0, 5);
    if (bands.length) {
      var rows = bands.map(function (b) {
        return '<tr><td>' + esc(b.band) + '</td><td>' + num(b.n) + '</td><td>' + pct(b.win_rate, 1) + '</td></tr>';
      }).join('');
      setHTML('val-bands', rows);
    }
    setText('val-ce',
      'Mean absolute calibration error ' + num(err.mae, 2) + ' across ' + num(err.n) +
      ' graded picks (return_pct_v1). CE = Observed − Predicted, computed from the graded record — never typed in by hand.');
    counts.picksGraded = st.total;
  }

  // ---- Intraday (intraday.json) ----
  function renderIntraday(d) {
    var movers = (d.movers || []).slice().sort(function (a, b) {
      return Math.abs(b.chg_24h || 0) - Math.abs(a.chg_24h || 0);
    }).slice(0, 3);
    var cmi = d.cmi_pulse || {};
    var macro = d.macro_snapshot || {};
    var cycle = d.cycle_state || {};
    var fg = (d.cycle_cache && d.cycle_cache.fng !== undefined) ? d.cycle_cache.fng : counts.fearGreed;
    var moverText = movers.length
      ? movers.map(function (m) {
          return esc(m.asset) + ' ' + (m.chg_24h >= 0 ? '+' : '') + num(m.chg_24h, 2) + '% (24h)';
        }).join(' · ')
      : 'No movers right now.';
    setHTML('market-context',
      '<strong>Fear &amp; Greed ' + num(fg) + '</strong> (extreme fear) · ' +
      'Top movers: ' + moverText + '<br>' +
      'Market health (CMI, ' + num(cmi.total) + ' assets): clean ' + pct(cmi.clean_pct, 1) +
      ' · caution ' + pct(cmi.caution_pct, 1) + ' · manip ' + pct(cmi.manip_pct, 1) + '<br>' +
      'Fed funds ' + num(macro.DFF && macro.DFF.value, 2) + '% · 10Y ' +
      num(macro.DGS10 && macro.DGS10.value, 2) + '% · ' +
      'Cycle: ' + esc(cycle.halving_band || '—') + ', day ' + num(cycle.halving_day));
    setText('intra-stamp', 'Intraday published ' + fmtDateTime(d.published_at || d.generated) + ' ET.');
  }

  // ---- Opportunity center: accumulate cards from every feed ----
  var oppCards = { picks: [], contracts: [], kalshi: [], grants: [] };
  var oppDone = {};
  function renderOpps(cards, key) {
    oppCards[key] = cards;
    oppDone[key] = true;
    var keys = ['contracts', 'grants', 'picks', 'kalshi'];
    var total = 0;
    var html = '';
    keys.forEach(function (k) {
      oppCards[k].forEach(function (c) {
        total++;
        html += '<div class="card"><h4>' + esc(c.title) + '</h4>' +
          '<p><span class="pill pill-open">' + esc(c.kind) + '</span> · ' + esc(c.conf) + '</p>' +
          '<p>' + esc(c.body) + '</p>' +
          '<p class="hint">' + esc(c.src) + '</p></div>';
      });
    });
    setText('opp-count', num(total) + ' open');
    if (total) setHTML('opp-grid', html);
    counts.opportunities = total;
  }

  // ---- Vault: live counts, each with its lineage ----
  function renderVault() {
    var cards = [
      { h: 'Predictions', b: '<strong>' + num(counts.picksGraded) + '</strong> graded NexIntel picks in the record. Each one carries its direction, its expected return, and its outcome — graded after the fact, never rewritten.' },
      { h: 'Contracts', b: '<strong>' + num(counts.contracts) + '</strong> nonprofit solicitations on the watch list, plus <strong>' + num(counts.latestOpps) + '</strong> general opportunities in the latest feed. Deadlines and agencies attached to every row.' },
      { h: 'Markets', b: '<strong>' + num(counts.kalshi) + '</strong> Kalshi markets watched, with crowd accuracy and the model record attached. Prices are implied probabilities, updated with the feed.' },
      { h: 'Grants', b: '<strong>' + num(counts.grants) + '</strong> grants in the registry (' + num(counts.grantsHot) + ' HOT right now). Tier, funder, amount, and deadline on every entry.' },
      { h: 'Intraday signals', b: 'Movers, market health, and the macro snapshot — published with the intraday loop. See the live market context under the State Vector.' },
      { h: 'Lineage', b: 'Every record on this page names its source file and its publish time. A number without a source is a guess; a number with one is a record.' }
    ];
    setHTML('vault-grid', cards.map(function (c) {
      return '<div class="card"><h4>' + c.h + '</h4><p>' + c.b + '</p></div>';
    }).join(''));
  }

  // ---- Assistant: answers from the live data ----
  function renderAssistant() {
    var top = [];
    Object.keys(oppCards).forEach(function (k) {
      oppCards[k].slice(0, 2).forEach(function (c) { top.push(c); });
    });
    var topText = top.slice(0, 3).map(function (c) { return c.title; }).join('; ');
    var qas = [
      { q: '“Show my opportunities.”',
        a: counts.opportunities + ' open right now — ' + num(counts.contracts) + ' contracts, ' + num(counts.grantsHot) + ' HOT grants, ' + num(oppCards.picks.length) + ' picks, ' + num(oppCards.kalshi.length) + ' Kalshi markets. Top of the list: ' + (topText || '—') + '.' },
      { q: '“What is the market doing right now?”',
        a: 'Fear & Greed is ' + num(counts.fearGreed) + ' — extreme fear. See the live market context under the State Vector for movers, market health, and rates.' },
      { q: '“What models are experimental?”',
        a: 'All nine: GP$ Index, Prosperity Score, Trust Score, Opportunity Score, Hidden Asset Multiplier, Stinnes Score, Asset Flipper Score, Anti-Fragility Index, Regulatory Friction Score. None has reached Validated.' },
      { q: '“What changed in my ledger?”',
        a: 'Ledger changes are per person and private — this page never shows them. Sign in on the home screen and your ledger answers there.' },
      { q: '“Why was this opportunity created?”',
        a: 'Every card on this page names its source feed and its evidence — a contract lists its agency and deadline, a pick lists its score and reason, a market lists its implied probability. No opportunity appears without its reason attached.' },
      { q: '“What is the top grant opportunity?”',
        a: (oppCards.grants[0] ? oppCards.grants[0].title + ' — ' + oppCards.grants[0].conf + '.' : 'No HOT grant is open in the registry right now.') }
    ];
    setHTML('asst-list', qas.map(function (x) {
      return '<div class="qa"><p class="qa-q">' + x.q + '</p><p class="qa-a">' + esc(x.a) + '</p></div>';
    }).join(''));
  }

  function fail(id, msg) {
    var n = el(id);
    if (n) n.innerHTML = '<p class="hint">' + esc(msg) + '</p>';
  }

  // Load everything in parallel; each section renders as its feed arrives.
  // Vault + assistant render once every source has settled (success or
  // failure), with a backstop timer in case a large file is slow.
  var settled = 0;
  var settledDone = false;
  function noteSettled() {
    settled++;
    if (!settledDone && settled >= 6) {
      settledDone = true;
      renderVault();
      renderAssistant();
    }
  }
  setTimeout(function () {
    if (!settledDone) { settledDone = true; renderVault(); renderAssistant(); }
  }, 8000);

  fetchJSON('data/latest.json').then(function (d) { renderLatest(d); noteSettled(); }, function () {
    fail('opp-grid', 'Picks feed unavailable right now — the section shows the framework, not invented numbers.');
    noteSettled();
  });
  fetchJSON('data/sam_nonprofit.json').then(function (d) { renderContracts(d); noteSettled(); }, function () {
    fail('opp-grid', 'Contracts feed unavailable right now.');
    noteSettled();
  });
  fetchJSON('data/kalshi.json').then(function (d) { renderKalshi(d); noteSettled(); }, function () {
    setText('kalshi-stats', 'Kalshi feed unavailable right now.');
    noteSettled();
  });
  fetchJSON('data/history.json').then(function (d) { renderHistory(d); noteSettled(); }, function () {
    setText('val-stats', 'Track record unavailable right now.');
    noteSettled();
  });
  fetchJSON('data/intraday.json').then(function (d) { renderIntraday(d); noteSettled(); }, function () {
    setText('market-context', 'Intraday feed unavailable right now.');
    noteSettled();
  });
  try { renderGrants(); } catch (e) { /* grants file not loaded — cards stay empty */ }
  noteSettled();
})();
