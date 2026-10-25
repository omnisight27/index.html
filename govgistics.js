// Contract Intel.
// Two data sources, both real:
//  1. "From the engine" strip — contracts_watch inside data/latest.json,
//     produced by the NexIntel engine's USASpending pull.
//  2. Live Awards Explorer — the browser queries the public USASpending
//     API (keyless, CORS-open) directly. Empty and error states are shown
//     plainly; rows are never invented.
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
  function isoDaysAgo(days) {
    var d = new Date();
    d.setDate(d.getDate() - days);
    return d.toISOString().slice(0, 10);
  }

  // ---------- 1. Engine strip ----------
  fetch('data/latest.json?_=' + Date.now())
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.contracts_watch || !d.contracts_watch.length) return;
      var strip = document.getElementById('engine-strip');
      document.getElementById('engine-awards').innerHTML =
        d.contracts_watch.map(function (c) { return '<p>· ' + esc(c) + '</p>'; }).join('');
      var stamp = d.published_at || d.generated;
      if (stamp) {
        document.getElementById('engine-stamp').textContent =
          'From the latest engine run — ' + new Date(stamp).toLocaleString();
      }
      strip.style.display = 'block';
    })
    .catch(function () { /* no engine data yet — strip stays hidden */ });

  // ---------- 3. Nonprofit opportunities (NicNacs lane) ----------
  // Same latest.json the engine strip reads — the engine's rotating
  // NAICS watch, merged into one ledger. Empty and error states shown
  // plainly; rows are never invented.
  fetch('data/latest.json?_=' + Date.now())
    .then(function (r) { return r.json(); })
    .then(function (d) {
      var el = document.getElementById('gv-nonprofit');
      if (!el) return;
      var rows = d.opportunities_nonprofit || [];
      if (!rows.length) {
        el.innerHTML = '<p class="live-stamp">No nonprofit-relevant federal solicitations in the watch right now — the engine checks one NAICS code per run, rotating through the list, and the ledger keeps 30 days of postings. Check back after a few runs.</p>';
        return;
      }
      var t = '<table class="sched-table"><tr><th>Title</th><th>Agency</th><th>Due</th><th>NAICS</th><th>State</th><th>Why it matches</th></tr>';
      rows.slice(0, 25).forEach(function (o) {
        t += '<tr><td>' + esc(o.title) + '</td><td>' + esc(o.agency) +
          '</td><td>' + esc(o.deadline) + '</td><td>' + esc(o.naics) +
          '</td><td>' + esc(o.state || '—') + '</td><td>' + esc(o.nonprofit_match || '—') + '</td></tr>';
      });
      el.innerHTML = t + '</table>';
    })
    .catch(function () {
      var el = document.getElementById('gv-nonprofit');
      if (el) el.innerHTML = '<p class="live-stamp">Watch unavailable right now — the engine data did not load. Try again in a minute.</p>';
    });

  // ---------- 2. Live Awards Explorer ----------
  var API = 'https://api.usaspending.gov/api/v2/search/spending_by_award/';
  var statusEl = document.getElementById('gv-status');
  var resultsEl = document.getElementById('gv-results');

  function search() {
    var keyword = document.getElementById('gv-keyword').value.trim();
    var agency = document.getElementById('gv-agency').value;
    var state = document.getElementById('gv-state').value;

    var filters = {
      time_period: [{ start_date: isoDaysAgo(90), end_date: isoDaysAgo(0) }],
      award_type_codes: ['A', 'B', 'C', 'D']
    };
    if (keyword) filters.keywords = [keyword];
    if (agency) filters.agencies = [{ type: 'awarding', tier: 'toptier', name: agency }];
    if (state) filters.place_of_performance_locations = [{ country: 'USA', state: state }];

    statusEl.textContent = 'Loading awards from USASpending…';
    resultsEl.innerHTML = '';

    fetch(API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        filters: filters,
        fields: ['Award ID', 'Recipient Name', 'Award Amount', 'Start Date',
                 'Awarding Agency', 'Place of Performance State Code'],
        page: 1, limit: 25, sort: 'Award Amount', order: 'desc'
      })
    })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (d) {
        var rows = (d && d.results) || [];
        if (!rows.length) {
          statusEl.textContent = 'No awards match those filters in the last 90 days — try widening them.';
          return;
        }
        statusEl.textContent = 'Showing the ' + rows.length + ' largest matching contract awards (last 90 days of activity).';
        var t = '<table class="sched-table"><tr><th>Start date</th><th>Recipient</th><th>Agency</th><th>Amount</th><th>Place</th></tr>';
        rows.forEach(function (r) {
          t += '<tr><td>' + esc(r['Start Date'] || '—') + '</td><td>' + esc(r['Recipient Name'] || '—') +
            '</td><td>' + esc(r['Awarding Agency'] || '—') + '</td><td>' + money(r['Award Amount']) +
            '</td><td>' + esc(r['Place of Performance State Code'] || '—') + '</td></tr>';
        });
        resultsEl.innerHTML = t + '</table>';
      })
      .catch(function () {
        statusEl.textContent = 'Feed unavailable right now — USASpending didn\'t answer. Try again in a minute.';
        resultsEl.innerHTML = '';
      });
  }

  document.getElementById('gv-search').addEventListener('click', search);
  document.getElementById('gv-keyword').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') search();
  });
  document.getElementById('gv-or').addEventListener('click', function () {
    document.getElementById('gv-state').value = 'OR'; search();
  });
  document.getElementById('gv-fl').addEventListener('click', function () {
    document.getElementById('gv-state').value = 'FL'; search();
  });
  document.getElementById('gv-clear').addEventListener('click', function () {
    document.getElementById('gv-keyword').value = '';
    document.getElementById('gv-agency').value = '';
    document.getElementById('gv-state').value = '';
    search();
  });

  search(); // initial load: biggest recent awards
})();
