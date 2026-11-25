// Gate 1 authenticated read test — queries the Step 2 Insight Registry
// tables with the signed-in Supabase session and checks for the marked
// GATE1-RUN-001 rows. Read-only: no inserts, updates, or deletes.
(function () {
  'use strict';

  var statusEl = document.getElementById('gate-status');
  var dotEl = document.getElementById('status-dot');
  var summaryEl = document.getElementById('gate-summary');
  var authCard = document.getElementById('auth-card');
  var resultsCard = document.getElementById('results-card');
  var resultsEl = document.getElementById('gate-results');
  var loginStatus = document.getElementById('login-status');

  var TABLES = ['feed_registry', 'insights', 'recommendations', 'scores'];

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  function setStatus(msg, ok) {
    statusEl.textContent = msg;
    dotEl.className = 'dot' + (ok ? '' : ' off');
  }
  function show(el, on) { el.style.display = on ? '' : 'none'; }
  function rowText(row) { return JSON.stringify(row || {}).toLowerCase(); }
  function has(rows, needles) {
    return (rows || []).some(function (row) {
      var text = rowText(row);
      return needles.every(function (n) { return text.indexOf(String(n).toLowerCase()) !== -1; });
    });
  }
  function checkTable(table, rows) {
    if (table === 'feed_registry') return has(rows, ['gate1_test_feed']);
    if (table === 'insights') return has(rows, ['gate1-run-001 test insight']);
    if (table === 'recommendations') return has(rows, ['gate1-run-001 test recommendation', 'open']);
    if (table === 'scores') return has(rows, ['gate1_test_feed', 'gate1', '50']);
    return false;
  }

  if (!window.OMNISIGHT_CONFIGURED) {
    setStatus('Supabase is not configured on this page yet.', false);
    summaryEl.textContent = 'config.js is missing the project URL or the publishable key.';
    return;
  }

  var s = document.createElement('script');
  s.src = 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.min.js';
  s.onload = function () {
    var client;
    try {
      client = window.supabase.createClient(
        window.OMNISIGHT_CONFIG.SUPABASE_URL,
        window.OMNISIGHT_CONFIG.SUPABASE_ANON_KEY
      );
    } catch (e) {
      setStatus('Could not start the Supabase client.', false);
      return;
    }

    document.getElementById('login-google').addEventListener('click', function () {
      client.auth.signInWithOAuth({
        provider: 'google',
        options: { redirectTo: window.location.origin + window.location.pathname }
      });
    });
    document.getElementById('login-magic').addEventListener('click', function () {
      var email = document.getElementById('login-email').value.trim();
      if (!email) { loginStatus.textContent = 'Type your email first.'; return; }
      loginStatus.textContent = 'Sending…';
      client.auth.signInWithOtp({
        email: email,
        options: { emailRedirectTo: window.location.origin + window.location.pathname }
      }).then(function (r) {
        loginStatus.textContent = r.error ? ('Error: ' + r.error.message)
                                          : 'Check your email — tap the link to finish signing in.';
      });
    });

    function renderResults(results) {
      var allPass = true;
      var html = '';
      TABLES.forEach(function (table) {
        var r = results[table] || {};
        var rows = r.rows || [];
        var pass = !r.error && checkTable(table, rows);
        if (!pass) allPass = false;
        html += '<div class="gate-table"><h3>' + esc(table) + ' — ' +
          '<span class="gate-result ' + (pass ? 'pass' : 'fail') + '">' + (pass ? 'PASS' : 'FAIL') + '</span></h3>' +
          '<p class="note">' + (r.error ? ('Error: ' + esc(r.error)) : (rows.length + ' row' + (rows.length === 1 ? '' : 's') + ' returned')) + '</p>' +
          '<pre class="gate-json">' + esc(JSON.stringify(rows, null, 2)) + '</pre></div>';
      });
      resultsEl.innerHTML = html;
      show(resultsCard, true);
      setStatus(allPass ? 'Gate 1 authenticated Step 2 reads PASS.' : 'Gate 1 authenticated Step 2 reads did not pass yet.', allPass);
      summaryEl.textContent = allPass
        ? 'All four Step 2 tables returned the expected GATE1-RUN-001 rows through the signed-in session.'
        : 'One or more Step 2 tables did not return the expected GATE1-RUN-001 rows. See the table results below.';
    }

    function runTest(user) {
      if (!user) {
        setStatus('Connected to Supabase — sign in to run Gate 1.', false);
        summaryEl.textContent = 'The Step 2 reads run only after sign-in, using your authenticated session.';
        show(authCard, true);
        show(resultsCard, false);
        return;
      }
      show(authCard, false);
      setStatus('Signed in as ' + (user.email || 'user') + ' — reading Step 2 tables…', true);
      summaryEl.textContent = 'Authenticated session active. Querying feed_registry, insights, recommendations, and scores.';
      var results = {};
      var pending = TABLES.length;
      TABLES.forEach(function (table) {
        client.from(table).select('*').order('id', { ascending: true }).then(function (r) {
          results[table] = r.error ? { error: r.error.message, rows: [] } : { rows: r.data || [] };
          pending -= 1;
          if (pending === 0) renderResults(results);
        }).catch(function (e) {
          results[table] = { error: e && e.message ? e.message : 'Read failed', rows: [] };
          pending -= 1;
          if (pending === 0) renderResults(results);
        });
      });
    }

    client.auth.getSession().then(function (r) {
      runTest(r.data && r.data.session ? r.data.session.user : null);
    });
    client.auth.onAuthStateChange(function (_event, session) {
      runTest(session ? session.user : null);
    });
  };
  s.onerror = function () { setStatus('Could not load the Supabase client.', false); };
  document.head.appendChild(s);
})();
