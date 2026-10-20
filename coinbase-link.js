// Portfolio page — "Link Coinbase" card (Phase 1).
//
// Flow: user logs in (auth.js) → pastes a View-only Coinbase CDP API key →
// the key is POSTed straight to the OmniSight backend over HTTPS (never
// stored in this browser, never written to localStorage, form is cleared
// immediately after submit) → "Sync from Coinbase" pulls normalized balances
// from the backend and merges them into the on-device holdings table via
// window.OmniPortfolioSync. Manual entry keeps working exactly as before
// for anyone who never links.
(function () {
  'use strict';

  var body = document.getElementById('cb-body');
  if (!body) return;

  function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'); }
  function backendUrl(path) {
    return window.OMNISIGHT_CONFIG.BACKEND_URL.replace(/\/+$/, '') + path;
  }

  function api(method, path, payload) {
    return window.OmniAuth.getAccessToken().then(function (token) {
      if (!token) throw new Error('not-logged-in');
      var opts = {
        method: method,
        headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' }
      };
      if (payload) opts.body = JSON.stringify(payload);
      return fetch(backendUrl(path), opts).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (data) {
          if (!r.ok) throw new Error(data.detail || ('Request failed (' + r.status + ')'));
          return data;
        });
      });
    });
  }

  function note(html) { body.innerHTML = '<p class="hint">' + html + '</p>'; }

  function render() {
    if (!window.OMNISIGHT_CONFIGURED || !window.OmniAuth || !window.OmniAuth.configured) {
      note('Account linking isn\'t set up yet. The site owner still needs to finish the ' +
           'backend setup (see the deploy checklist). Manual tracking below works exactly as always — ' +
           'nothing about this page changes until linking is switched on.');
      return;
    }
    var user = window.OmniAuth.getUser();
    if (!user) {
      body.innerHTML =
        '<p>Log in to link your Coinbase account and sync your balances automatically.</p>' +
        '<button class="btn" id="cb-login" type="button">Log in</button>';
      document.getElementById('cb-login').addEventListener('click', function () {
        window.OmniAuth.openLogin();
        window.scrollTo({ top: 0, behavior: 'smooth' });
      });
      return;
    }
    note('Checking your link status…');
    api('GET', '/link/status').then(function (st) {
      if (st.linked) renderLinked(st); else renderForm();
    }).catch(function (e) {
      note('Couldn\'t reach the OmniSight backend (' + esc(e.message) + '). ' +
           'It may be waking up — wait 30 seconds and reload. Manual tracking below is unaffected.');
    });
  }

  function renderLinked(st) {
    body.innerHTML =
      '<p>✅ Coinbase linked — key <strong>' + esc(st.key_name_masked || '') + '</strong>' +
      (st.updated_at ? ' · linked ' + esc(String(st.updated_at).slice(0, 10)) : '') + '</p>' +
      '<button class="btn" id="cb-sync" type="button">⟳ Sync from Coinbase</button> ' +
      '<button class="btn" id="cb-unlink" type="button">Unlink</button>' +
      '<div class="hint" id="cb-status" style="margin-top:8px"></div>' +
      '<p class="hint">Syncing replaces the units on matching rows with your live Coinbase balances ' +
      '(rows are marked ⟳ Synced). Your avg costs stay as you entered them, and you can edit everything afterward.</p>';
    var status = document.getElementById('cb-status');
    document.getElementById('cb-sync').addEventListener('click', function () {
      status.textContent = 'Syncing from Coinbase…';
      api('GET', '/portfolio/coinbase').then(function (positions) {
        var n = window.OmniPortfolioSync.applyCoinbase(positions);
        status.textContent = 'Synced ' + n + ' position' + (n === 1 ? '' : 's') + ' ✓ ' + new Date().toLocaleTimeString();
      }).catch(function (e) { status.textContent = 'Sync failed: ' + e.message; });
    });
    document.getElementById('cb-unlink').addEventListener('click', function () {
      status.textContent = 'Unlinking…';
      api('DELETE', '/link/coinbase').then(function () { render(); })
        .catch(function (e) { status.textContent = 'Unlink failed: ' + e.message; });
    });
  }

  function renderForm() {
    body.innerHTML =
      '<p>Linking lets this page fill itself from your Coinbase balances — no more typing units after every trade.</p>' +
      '<div class="hint" style="margin-bottom:10px">' +
        '<strong>First, make the right kind of key (about 2 minutes):</strong><br>' +
        '1. Go to <strong>portal.cdp.coinbase.com</strong> and sign in with your Coinbase account.<br>' +
        '2. Open <strong>API Keys</strong> → <strong>Create API key</strong>.<br>' +
        '3. Under permissions choose <strong>View only</strong>. ' +
        '<strong>Create a View-only API key at portal.cdp.coinbase.com — never enable Trade or Transfer.</strong><br>' +
        '4. Copy the <strong>key name</strong> and the <strong>private key</strong> it shows you (the private key is shown once) and paste them here.' +
      '</div>' +
      '<div class="debt-grid">' +
        '<input type="text" id="cb-key-name" placeholder="API key name (organizations/…/apiKeys/…)" autocomplete="off">' +
        '<input type="text" id="cb-key-secret" placeholder="Private key (-----BEGIN EC PRIVATE KEY-----)" autocomplete="off" style="-webkit-text-security:disc">' +
      '</div>' +
      '<p class="hint">Your key is sent once, over HTTPS, to the OmniSight backend and stored there encrypted. ' +
      'It is never saved in this browser. Because it is View-only, it can read balances and nothing else — ' +
      'it cannot trade, send, or withdraw.</p>' +
      '<button class="btn" id="cb-link" type="button">Link Coinbase</button>' +
      '<div class="hint" id="cb-status" style="margin-top:8px"></div>';
    var status = document.getElementById('cb-status');
    document.getElementById('cb-link').addEventListener('click', function () {
      var nameEl = document.getElementById('cb-key-name');
      var secretEl = document.getElementById('cb-key-secret');
      var keyName = nameEl.value.trim();
      var secret = secretEl.value.trim();
      // Clear the form immediately — the key should not sit on screen or in the DOM.
      nameEl.value = ''; secretEl.value = '';
      if (!keyName || !secret) { status.textContent = 'Paste both the key name and the private key.'; return; }
      status.textContent = 'Linking…';
      api('POST', '/link/coinbase', { key_name: keyName, private_key: secret })
        .then(function () { render(); })
        .catch(function (e) { status.textContent = 'Link failed: ' + e.message; });
    });
  }

  if (window.OmniAuth && window.OmniAuth.onChange) {
    window.OmniAuth.onChange(function () { render(); });
  }
  render();
})();
