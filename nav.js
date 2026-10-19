// OmniSight nav — hamburger dropdown on mobile, horizontal bar on desktop.
(function () {
  'use strict';
  function init() {
    var toggle = document.querySelector('.nav-toggle');
    var menu = document.querySelector('.nav-menu');
    if (!toggle || !menu) return;
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
