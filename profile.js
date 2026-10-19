// OmniSight profile — Loop 6
// Real local preferences: display name, notifications, engine priority.
// Persists in localStorage; priority order feeds the alerts page.

(function () {
  'use strict';

  var KEY = 'omnisight_profile_v1';
  var ENGINES = ['Govgistics', 'Money Max', 'Coinbase', 'Amazon & eBay', 'Macro', 'On-Chain'];

  function defaults() {
    return {
      displayName: '',
      emailAlerts: true,
      criticalOnly: false,
      priority: ENGINES.slice()
    };
  }

  function load() {
    try {
      var raw = localStorage.getItem(KEY);
      if (!raw) return defaults();
      var p = JSON.parse(raw);
      var d = defaults();
      if (typeof p.displayName === 'string') d.displayName = p.displayName;
      d.emailAlerts = p.emailAlerts !== false;
      d.criticalOnly = !!p.criticalOnly;
      if (Array.isArray(p.priority) && p.priority.length) {
        // keep known engines in saved order, append any missing
        d.priority = p.priority.filter(function (e) { return ENGINES.indexOf(e) > -1; });
        ENGINES.forEach(function (e) { if (d.priority.indexOf(e) < 0) d.priority.push(e); });
      }
      return d;
    } catch (e) { return defaults(); }
  }

  function save(p) {
    try { localStorage.setItem(KEY, JSON.stringify(p)); } catch (e) {}
  }

  window.OmniSightProfile = { load: load, save: save, engines: ENGINES };

  function init() {
    var p = load();

    var nameInput = document.getElementById('profile-name');
    var nameShown = document.getElementById('profile-name-shown');
    if (nameInput) {
      nameInput.value = p.displayName;
      nameInput.addEventListener('change', function () {
        p.displayName = nameInput.value.trim();
        save(p);
        if (nameShown) nameShown.textContent = p.displayName || 'OmniSight User';
      });
    }
    if (nameShown) nameShown.textContent = p.displayName || 'OmniSight User';

    var emailBox = document.getElementById('notif-email');
    var critBox = document.getElementById('notif-critical');
    if (emailBox) {
      emailBox.checked = p.emailAlerts;
      emailBox.addEventListener('change', function () { p.emailAlerts = emailBox.checked; save(p); });
    }
    if (critBox) {
      critBox.checked = p.criticalOnly;
      critBox.addEventListener('change', function () { p.criticalOnly = critBox.checked; save(p); });
    }

    // Engine priority list with up/down buttons
    var list = document.getElementById('priority-list');
    function renderPriority() {
      if (!list) return;
      list.innerHTML = '';
      p.priority.forEach(function (eng, i) {
        var li = document.createElement('li');
        li.className = 'priority-item';
        li.innerHTML = '<span class="priority-rank">#' + (i + 1) + '</span> ' +
          '<span class="priority-name">' + eng + '</span>' +
          '<span class="priority-btns">' +
          '<button class="btn btn-sm" data-move="-1" data-i="' + i + '" ' + (i === 0 ? 'disabled' : '') + '>▲</button>' +
          '<button class="btn btn-sm" data-move="1" data-i="' + i + '" ' + (i === p.priority.length - 1 ? 'disabled' : '') + '>▼</button>' +
          '</span>';
        list.appendChild(li);
      });
      list.querySelectorAll('button[data-move]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          var i = parseInt(btn.getAttribute('data-i'), 10);
          var mv = parseInt(btn.getAttribute('data-move'), 10);
          var j = i + mv;
          if (j < 0 || j >= p.priority.length) return;
          var tmp = p.priority[i]; p.priority[i] = p.priority[j]; p.priority[j] = tmp;
          save(p);
          renderPriority();
        });
      });
    }
    renderPriority();

    var dl = document.getElementById('download-data');
    if (dl) {
      dl.addEventListener('click', function () {
        var data = {
          profile: p,
          engines: window.OmniSightConfig ? window.OmniSightConfig.load() : null,
          exported: new Date().toISOString()
        };
        var blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'omnisight-data.json';
        document.body.appendChild(a);
        a.click();
        setTimeout(function () { document.body.removeChild(a); }, 100);
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
