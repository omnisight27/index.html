// OmniSight nav — hamburger dropdown on mobile, horizontal bar on desktop.
(function () {
  'use strict';
  function init() {
    var toggle = document.querySelector('.nav-toggle');
    var menu = document.querySelector('.nav-menu');
    if (!toggle || !menu) return;
    // Pages added after the static menus were written: inject their links
    // into every page's menu (skipped where a page already links them).
    [['govgistics.html', 'Contract Intel'], ['grants.html', 'Grants'], ['economy.html', 'Economy'], ['land.html', 'Land'], ['https://omnisight27.github.io/freedom-gps/', 'Freedom GPS'], ['hub.html', 'Command Hub']].forEach(function (pair) {
      if (menu.querySelector('a[href="' + pair[0] + '"]')) return;
      var a = document.createElement('a');
      a.href = pair[0];
      a.textContent = pair[1];
      if (window.location.pathname.split('/').pop() === pair[0]) a.className = 'active';
      var profile = menu.querySelector('a[href="profile.html"]');
      if (profile) menu.insertBefore(a, profile); else menu.appendChild(a);
    });
    toggle.addEventListener('click', function (e) {
      e.stopPropagation();
      menu.classList.toggle('open');
      toggle.classList.toggle('active');
    });
    menu.querySelectorAll('a').forEach(function (a) {
      a.addEventListener('click', function () {
        menu.classList.remove('open');
        toggle.classList.remove('active');
      });
    });
    document.addEventListener('click', function (e) {
      if (!menu.contains(e.target) && e.target !== toggle) {
        menu.classList.remove('open');
        toggle.classList.remove('active');
      }
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else { init(); }
})();
