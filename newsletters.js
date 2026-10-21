// OmniSight Morning Newsletters — renders the daily digest distilled
// from Nic's nine morning newsletters into data/newsletters.json by
// scripts/build-newsletter-digest.py (goal workspace). Per source: the
// day's subject + extracted headlines, plus market flags the digest
// scanner pulled out (crypto names, risk words, macro words).

(function () {
  'use strict';

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function render(digest) {
    var el = document.getElementById('newsletters');
    if (!el) return;

    if (!digest || !digest.sources || !digest.sources.length) {
      el.innerHTML = '<p>No newsletter digest yet — the first one lands ' +
        'with tomorrow morning\'s editions.</p>';
      return;
    }

    var html = '<p><strong>' + esc(digest.date) + '</strong> — ' +
      digest.sources.length + ' newsletters read this morning.</p>';

    var flags = digest.market_flags || [];
    if (flags.length) {
      html += '<p><strong>⚑ Market flags:</strong></p><ul>';
      html += flags.map(function (f) {
        return '<li><strong>' + esc(f.term) + '</strong> — ' + esc(f.headline) +
          ' <em>(' + esc(f.source) + ')</em></li>';
      }).join('');
      html += '</ul>';
    }

    html += digest.sources.map(function (s) {
      var items = (s.headlines || []).map(function (h) {
        return '<li>' + esc(h) + '</li>';
      }).join('');
      return '<h4>' + esc(s.name) + '</h4>' +
        '<p><em>' + esc(s.subject) + '</em></p>' +
        (items ? '<ul>' + items + '</ul>' : '<p>No headlines extracted.</p>');
    }).join('');

    el.innerHTML = html;
  }

  function init() {
    if (!document.getElementById('newsletters')) return;
    fetch('data/newsletters.json', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(render)
      .catch(function () { render(null); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
