// Foresight panel — see it before it happens.
// Reads the `foresight` array the NexIntel engine publishes in
// data/latest.json (Federal Register rulemaking + CBO budget analysis,
// lens-matched to Nic's life). Signals are hints from public sources,
// not predictions — rows are never invented, and the empty state is
// shown plainly when the week's run found nothing new.
(function () {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  var LENS_EMOJI = {
    'Health': '🏥',
    'Money & Tax': '💰',
    'Small Business & Nonprofits': '🏪',
    'Energy & Infrastructure': '⚡',
    'Tech & AI': '🤖',
    'Markets & Budgets': '📊'
  };

  fetch('data/latest.json?_=' + Date.now())
    .then(function (r) { return r.json(); })
    .then(function (d) {
      var list = document.getElementById('foresight-list');
      var stamp = document.getElementById('foresight-stamp');
      var signals = (d && d.foresight) || [];
      if (stamp && (d.published_at || d.generated)) {
        stamp.textContent = 'From the latest engine run — ' +
          new Date(d.published_at || d.generated).toLocaleString();
      }
      if (!signals.length) {
        list.innerHTML = '<p class="live-stamp">No foresight signals this week — ' +
          'the next engine run checks the Federal Register and the CBO again.</p>';
        return;
      }
      list.innerHTML = signals.map(function (s) {
        var emoji = LENS_EMOJI[s.lens] || '🔭';
        var title = s.url
          ? '<a class="asset-link" href="' + esc(s.url) + '" target="_blank" rel="noopener">' + esc(s.title) + ' ↗</a>'
          : esc(s.title);
        return '<p><strong>' + emoji + ' ' + esc(s.lens) + '</strong> — ' + title +
          '<br><span class="live-stamp">' + esc(s.source) +
          (s.agency && s.agency !== '?' ? ' · ' + esc(s.agency) : '') +
          (s.date ? ' · ' + esc(s.date) : '') + '</span>' +
          (s.why ? '<br>' + esc(s.why) : '') + '</p>';
      }).join('');
    })
    .catch(function () {
      document.getElementById('foresight-list').innerHTML =
        '<p class="live-stamp">Foresight feed unavailable right now — try reloading in a minute.</p>';
    });
})();
