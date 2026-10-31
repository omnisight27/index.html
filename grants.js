// Grants page: NicNacs tiered pipeline (HOT/WARM/COOL/COLD) + generic grant search.
// Data: grants-data.js (window.NICNACS_GRANTS, window.GRANT_DB, window.GRANT_CATEGORIES).
(function () {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  var TIER_ORDER = ['HOT', 'WARM', 'COOL', 'COLD'];
  var TIER_BLURB = {
    SUBMITTED: '📬 SUBMITTED — in the funder\'s hands. Awaiting decision.',
    HOT: '🔥 HOT — apply now. Best fit, deadline approaching.',
    WARM: '🌡️ WARM — strong fit. Get the application ready.',
    COOL: '❄️ COOL — watchlist. Not yet, but keep an eye on it.',
    COLD: '🧊 COLD — weak fit or not eligible yet. Parked, not dead.'
  };

  function daysLeft(iso) {
    if (!iso) return null;
    var ms = new Date(iso + 'T00:00:00') - new Date(new Date().toDateString());
    return Math.round(ms / 86400000);
  }

  function tierCard(g) {
    var submitted = g.tier === 'SUBMITTED';
    var tierCls = submitted ? 'hot' : g.tier.toLowerCase();
    var badgeTxt = submitted ? 'SUBMITTED' : g.tier;
    var dl = daysLeft(g.deadlineISO);
    var dlHtml = '';
    if (dl != null) {
      var cls = dl <= 21 ? 'days-left days-urgent' : 'days-left';
      dlHtml = ' <span class="' + cls + '">(' + (dl < 0 ? Math.abs(dl) + ' days past' : dl + ' days left') + ')</span>';
    }
    var url = (g.url && g.url !== 'unverified') ? g.url : '';
    var link = url
      ? '<p><a href="' + esc(url) + '" target="_blank" rel="noopener">Apply / program page →</a></p>'
      : '';
    return '<div class="card tier-' + tierCls + '">' +
      '<h3><span class="tier-badge ' + tierCls + '">' + badgeTxt + '</span>' + esc(g.name) + '</h3>' +
      '<p class="grant-meta"><span class="lvl-badge">' + esc(g.level) + '</span>' +
      '<strong>' + esc(g.amount) + '</strong> · 📅 ' + esc(g.deadline) + dlHtml + '</p>' +
      '<p><strong>Funds:</strong> ' + esc(g.funds) + '</p>' +
      '<p><strong>Eligibility:</strong> ' + esc(g.eligibility) + '</p>' +
      '<p class="grant-meta"><strong>Status:</strong> ' + esc(g.status) + '</p>' +
      (g.notes ? '<p class="hint">💡 ' + esc(g.notes) + '</p>' : '') +
      link + '</div>';
  }

  function renderTiers() {
    var el = document.getElementById('grants-tiers');
    if (!el) return;
    var grants = (window.NICNACS_GRANTS || []).slice();
    var order = { SUBMITTED: -1, HOT: 0, WARM: 1, COOL: 2, COLD: 3 };
    grants.sort(function (a, b) {
      if (order[a.tier] !== order[b.tier]) return order[a.tier] - order[b.tier];
      var da = a.deadlineISO || '9999', db = b.deadlineISO || '9999';
      return da < db ? -1 : da > db ? 1 : 0;
    });
    var html = '';
    ['SUBMITTED', 'HOT', 'WARM', 'COOL', 'COLD'].forEach(function (t) {
      var list = grants.filter(function (g) { return g.tier === t; });
      if (!list.length) return;
      html += '<h2>' + esc(TIER_BLURB[t]) + '</h2>' + list.map(tierCard).join('');
    });
    el.innerHTML = html || '<p class="hint">No grants in the pipeline right now.</p>';
    document.getElementById('grants-stamp').textContent =
      'Pipeline last reviewed Oct 30, 2026 · ' + grants.length + ' grants tracked. Always confirm deadlines with the funder.';
  }

  // ---------- generic grant search ----------
  var STATES = ['AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY','DC'];

  function initSearch() {
    var catSel = document.getElementById('gs-cat');
    var stateSel = document.getElementById('gs-state');
    if (!catSel || !stateSel) return;
    var cats = window.GRANT_CATEGORIES || [];
    var labels = window.GRANT_CATEGORY_LABELS || {};
    catSel.innerHTML = '<option value="">— Business type —</option>' +
      cats.map(function (c) { return '<option value="' + esc(c) + '">' + esc(labels[c] || c) + '</option>'; }).join('');
    stateSel.innerHTML = '<option value="">— State —</option>' +
      STATES.map(function (s) { return '<option value="' + s + '">' + s + '</option>'; }).join('');
    document.getElementById('gs-go').addEventListener('click', runSearch);
  }

  function runSearch() {
    var cat = document.getElementById('gs-cat').value;
    var state = document.getElementById('gs-state').value;
    var entity = document.getElementById('gs-entity').value;
    var statusEl = document.getElementById('gs-status');
    var resEl = document.getElementById('gs-results');
    if (!cat) { statusEl.textContent = 'Pick a business type first.'; return; }

    var db = window.GRANT_DB || [];
    var scored = [];
    db.forEach(function (g) {
      var score = 0;
      if ((g.categories || []).indexOf(cat) !== -1) score += 3; else return; // category is required
      var states = g.states || ['national'];
      if (states.indexOf('national') !== -1 || (state && states.indexOf(state) !== -1)) score += 2;
      else if (state) return; // state-specific grant, wrong state
      if (entity === 'nonprofit' && g.nonprofit_eligible) score += 2;
      else if (entity === 'forprofit' && g.forprofit_eligible) score += 2;
      else if (entity === 'either' && (g.nonprofit_eligible || g.forprofit_eligible)) score += 1;
      else return;
      scored.push({ g: g, score: score });
    });
    scored.sort(function (a, b) { return b.score - a.score; });

    if (!scored.length) {
      statusEl.textContent = 'No matches in the database for that combination yet — try a broader business type.';
      resEl.innerHTML = '';
      return;
    }
    statusEl.textContent = scored.length + ' match' + (scored.length > 1 ? 'es' : '') + ' — best fit first.';
    resEl.innerHTML = scored.slice(0, 25).map(function (s) {
      var g = s.g;
      return '<div class="card"><h3>' + esc(g.name) + '</h3>' +
        '<p class="grant-meta"><span class="lvl-badge">' + esc(g.level) + '</span>' +
        '<strong>' + esc(g.amount) + '</strong> · 📅 ' + esc(g.deadline) + '</p>' +
        '<p>' + esc(g.blurb) + '</p>' +
        '<p class="grant-meta">' + esc(g.funder) +
        (g.nonprofit_eligible && g.forprofit_eligible ? ' · nonprofits & for-profits' :
         g.nonprofit_eligible ? ' · nonprofits' : ' · for-profits') + '</p>' +
        ((g.url && g.url !== 'unverified') ? '<p><a href="' + esc(g.url) + '" target="_blank" rel="noopener">Program page →</a></p>' : '') +
        '</div>';
    }).join('');
  }

  // ---------- tabs + init ----------

  function init() {
    var tN = document.getElementById('tab-nicnacs');
    var tS = document.getElementById('tab-search');
    var pN = document.getElementById('pane-nicnacs');
    var pS = document.getElementById('pane-search');
    function show(which) {
      var nic = which === 'nic';
      tN.classList.toggle('active', nic);
      tS.classList.toggle('active', !nic);
      pN.hidden = !nic;
      pS.hidden = nic;
    }
    if (tN) tN.addEventListener('click', function () { show('nic'); });
    if (tS) tS.addEventListener('click', function () { show('search'); });
    initSearch();
  }

  window.OmniWall.init(function () { renderTiers(); init(); });
})();
