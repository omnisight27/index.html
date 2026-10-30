// OmniSight login wall — shared by pages that live behind the login.
// Usage in page HTML:
//   <div id="login-wall" class="card" hidden> ... <button id="wall-login" class="btn primary">Log in</button> ... </div>
//   <div id="walled-content" hidden> ... page content ... </div>
//   <script src="login-wall.js"></script>  (after auth.js)
//   window.OmniWall.init(function (user) { /* render page for user */ });
//
// If auth is not configured (dev), content shows immediately.
(function () {
  'use strict';

  function show(id, on) {
    var el = document.getElementById(id);
    if (el) el.hidden = !on;
  }

  function init(onReady) {
    var done = false;
    function ready(user) {
      if (done) return;
      done = true;
      if (user) {
        show('login-wall', false);
        show('walled-content', true);
        if (typeof onReady === 'function') onReady(user);
      } else {
        show('login-wall', true);
        show('walled-content', false);
        var btn = document.getElementById('wall-login');
        if (btn && window.OmniAuth && typeof window.OmniAuth.openLogin === 'function') {
          btn.addEventListener('click', function () { window.OmniAuth.openLogin(); });
        }
      }
    }

    if (!window.OmniAuth || !window.OmniAuth.configured) {
      // No auth configured — show content, nothing to gate.
      show('login-wall', false);
      show('walled-content', true);
      if (typeof onReady === 'function') { try { onReady(null); } catch (e) {} }
      return;
    }

    // React to login/logout at any time.
    window.OmniAuth.onChange(function (user) {
      done = false; // allow re-render on logout/login
      if (user) {
        show('login-wall', false);
        show('walled-content', true);
        if (typeof onReady === 'function') { try { onReady(user); } catch (e) {} }
      } else {
        show('login-wall', true);
        show('walled-content', false);
      }
    });
    // Initial state once auth is ready.
    if (window.OmniAuth.ready && typeof window.OmniAuth.ready.then === 'function') {
      window.OmniAuth.ready.then(function () {
        if (done) return;
        ready(window.OmniAuth.getUser());
      });
    } else {
      ready(window.OmniAuth.getUser());
    }

    // Re-wire the wall login button every time (renderSlot may rebuild nav).
    document.addEventListener('click', function (e) {
      if (e.target && e.target.id === 'wall-login' &&
          window.OmniAuth && typeof window.OmniAuth.openLogin === 'function') {
        window.OmniAuth.openLogin();
      }
    });
  }

  window.OmniWall = { init: init };
})();
