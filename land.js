// Land page — renders researched listings from land-data.js (window.LAND_LISTINGS).
// Research only: every listing links out; sellers are never contacted.
(function () {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function money(n) {
    if (n == null || isNaN(n)) return '—';
    return '$' + Number(n).toLocaleString('en-US', { maximumFractionDigits: 0 });
  }

  function card(l) {
    var tags = '';
    if (l.seller_financing) tags += '<span class="deal-tag">seller financing</span>';
    if (l.price_cut) tags += '<span class="deal-tag">price cut</span>';
    if (l.below_comps) tags += '<span class="deal-tag">below comps</span>';
    return '<div class="card">' +
      '<h3>' + esc(l.name) + '</h3>' +
      '<div class="land-price">' + money(l.price) + '</div>' +
      '<p class="land-meta">' + esc(l.location) + ' · ' + esc(String(l.acres)) + ' acres' +
      (l.price_per_acre ? ' · ' + money(l.price_per_acre) + '/acre' : '') + '</p>' +
      '<p>' + tags + '</p>' +
      '<p class="land-why"><strong>Why it fits:</strong> ' + esc(l.why_it_fits) + '</p>' +
      (l.unverified ? '<p class="hint">⚠️ Unverified: ' + esc(l.unverified) + '</p>' : '') +
      '<p><a href="' + esc(l.url) + '" target="_blank" rel="noopener">View listing →</a> <span class="live-stamp">' + esc(l.source || '') + '</span></p>' +
      '</div>';
  }

  function render() {
    var grid = document.getElementById('land-grid');
    var list = window.LAND_LISTINGS || [];
    if (!list.length) {
      grid.innerHTML = '<p class="hint">No listings in this refresh — the research pass runs regularly. Check back soon.</p>';
      return;
    }
    // cheapest first
    list = list.slice().sort(function (a, b) { return (a.price || 1e12) - (b.price || 1e12); });
    grid.innerHTML = list.map(card).join('');
    document.getElementById('land-stamp').textContent =
      list.length + ' listings · researched ' + (window.LAND_RESEARCH_DATE || 'recently') +
      ' · regions: ' + (window.LAND_REGIONS || 'nationwide') +
      '. Prices and availability change fast — confirm with the listing agent before acting.';
  }

  window.OmniWall.init(function () { render(); });
})();
