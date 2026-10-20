// OmniSight auth — Supabase login (email magic link + Google), session in the
// nav. Loaded on every page after nav.js. If config.js still has placeholder
// values, this module does nothing at all: no UI, no network, site unchanged.
//
// Exposes window.OmniAuth:
//   configured            boolean
//   getUser()             current Supabase user or null (sync, after ready)
//   getAccessToken()      Promise<string|null> — JWT for backend calls
//   onChange(cb)          cb(user|null) on login/logout; returns unsubscribe
//   signOut()             Promise<void>
//   openLogin()           open the login panel (used by the Portfolio page)
(function () {
  'use strict';

  var listeners = [];
  var user = null;
  var client = null;
  var readyResolve;
  var ready = new Promise(function (res) { readyResolve = res; });

  function emit() {
    listeners.forEach(function (cb) { try { cb(user); } catch (e) {} });
  }

  var OmniAuth = {
    configured: !!window.OMNISIGHT_CONFIGURED,
    getUser: function () { return user; },
    getAccessToken: function () {
      if (!client) return Promise.resolve(null);
      return client.auth.getSession().then(function (r) {
        return (r.data && r.data.session) ? r.data.session.access_token : null;
      }).catch(function () { return null; });
    },
    onChange: function (cb) {
      listeners.push(cb);
      ready.then(function () { try { cb(user); } catch (e) {} });
      return function () {
        listeners = listeners.filter(function (f) { return f !== cb; });
      };
    },
    signOut: function () {
      if (!client) return Promise.resolve();
      return client.auth.signOut().then(function () { user = null; emit(); renderSlot(); });
    },
    openLogin: function () {
      var panel = document.getElementById('auth-panel');
      if (panel) { panel.style.display = ''; var inp = panel.querySelector('input'); if (inp) inp.focus(); }
    },
    ready: ready
  };
  window.OmniAuth = OmniAuth;

  if (!OmniAuth.configured) { readyResolve(); return; }

  // --- styles (self-contained; uses the site palette variables) ---
  var css =
    '.auth-slot{position:relative;display:flex;align-items:center;gap:10px}' +
    '.auth-btn{background:var(--surface);color:var(--accent);border:1px solid var(--border);' +
    'border-radius:4px;padding:8px 12px;font-weight:600;cursor:pointer;font-size:14px}' +
    '.auth-btn:hover{border-color:var(--accent)}' +
    '.auth-user{color:var(--text-light);font-size:13px;max-width:180px;overflow:hidden;' +
    'text-overflow:ellipsis;white-space:nowrap}' +
    '#auth-panel{position:absolute;right:0;top:calc(100% + 10px);width:290px;z-index:200;' +
    'background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;' +
    'box-shadow:0 12px 32px rgba(0,0,0,.5)}' +
    '#auth-panel h3{margin:0 0 6px;color:var(--text);font-size:16px}' +
    '#auth-panel p{margin:0 0 10px;color:var(--text-muted);font-size:13px}' +
    '#auth-panel input{width:100%;box-sizing:border-box;padding:9px;margin:0 0 10px;' +
    'background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text)}' +
    '#auth-panel .btn{width:100%;margin:0 0 8px}' +
    '#auth-panel .auth-status{font-size:13px;color:var(--accent);min-height:18px}';
  var style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'); }

  function renderSlot() {
    var slot = document.getElementById('auth-slot');
    if (!slot) return;
    if (user) {
      slot.innerHTML =
        '<span class="auth-user" title="' + esc(user.email || '') + '">' + esc(user.email || 'Signed in') + '</span>' +
        '<button class="auth-btn" id="auth-logout" type="button">Log out</button>';
      document.getElementById('auth-logout').addEventListener('click', function () { OmniAuth.signOut(); });
    } else {
      slot.innerHTML = '<button class="auth-btn" id="auth-open" type="button">Log in</button>' +
        '<div id="auth-panel" style="display:none">' +
          '<h3>Log in to OmniSight</h3>' +
          '<p>We email you a sign-in link — no password to remember.</p>' +
          '<input type="email" id="auth-email" placeholder="you@example.com" autocomplete="email">' +
          '<button class="btn" id="auth-magic" type="button">Email me a sign-in link</button>' +
          '<button class="btn" id="auth-google" type="button">Continue with Google</button>' +
          '<div class="auth-status" id="auth-status"></div>' +
        '</div>';
      var panel = document.getElementById('auth-panel');
      document.getElementById('auth-open').addEventListener('click', function (e) {
        e.stopPropagation();
        panel.style.display = panel.style.display === 'none' ? '' : 'none';
      });
      document.addEventListener('click', function (e) {
        if (panel.style.display !== 'none' && !slot.contains(e.target)) panel.style.display = 'none';
      });
      document.getElementById('auth-magic').addEventListener('click', function () {
        var email = document.getElementById('auth-email').value.trim();
        var status = document.getElementById('auth-status');
        if (!email) { status.textContent = 'Type your email first.'; return; }
        status.textContent = 'Sending…';
        client.auth.signInWithOtp({
          email: email,
          options: { emailRedirectTo: window.location.origin + window.location.pathname }
        }).then(function (r) {
          status.textContent = r.error ? ('Error: ' + r.error.message)
                                        : 'Check your email — tap the link to finish signing in.';
        });
      });
      document.getElementById('auth-google').addEventListener('click', function () {
        client.auth.signInWithOAuth({
          provider: 'google',
          options: { redirectTo: window.location.origin + window.location.pathname }
        });
      });
    }
  }

  function mountSlot() {
    var menu = document.querySelector('.nav-menu');
    if (!menu || document.getElementById('auth-slot')) return;
    var slot = document.createElement('span');
    slot.className = 'auth-slot';
    slot.id = 'auth-slot';
    menu.appendChild(slot);
    renderSlot();
  }

  // --- load Supabase JS (UMD) from CDN, then init ---
  var s = document.createElement('script');
  s.src = 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.min.js';
  s.onload = function () {
    try {
      client = window.supabase.createClient(
        window.OMNISIGHT_CONFIG.SUPABASE_URL,
        window.OMNISIGHT_CONFIG.SUPABASE_ANON_KEY
      );
    } catch (e) { readyResolve(); return; }
    client.auth.getSession().then(function (r) {
      user = (r.data && r.data.session) ? r.data.session.user : null;
      mountSlot();
      emit();
      readyResolve();
    });
    client.auth.onAuthStateChange(function (_event, session) {
      user = session ? session.user : null;
      renderSlot();
      emit();
    });
  };
  s.onerror = function () { readyResolve(); };
  document.head.appendChild(s);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mountSlot);
  } else { mountSlot(); }
})();
