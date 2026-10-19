// OmniSight engines page — Loop 4
// Live status per engine (real latency for data-backed engines),
// working enable/disable toggles, working Save Configuration.

(function () {
  'use strict';

  // Engines with a real health-check endpoint and what "healthy" means.
  // For data-backed engines we check our own latest.json — if the NexIntel
  // loop populated that feed, the engine is genuinely online.
  var HEALTH_CHECKS = {
    'Crypto Pulse': 'https://api.coingecko.com/api/v3/ping',
    'Chain Signal': 'https://api.coingecko.com/api/v3/ping',
    'Macro Scope': 'https://api.alternative.me/fng/?limit=1'
  };
  // Feed-backed engines: verified against NexIntel's own data output
  var FEED_CHECKS = {
    'Contract Intel': function (model) {
      var cw = model && model.contracts_watch;
      return cw && cw.length > 0;
    },
    'Liquidity Engine': function (model) {
      var m = model && model.macro;
      return m && (m.cpi || m.gdp || m.m2);
    }
  };

  function setStatus(card, online, latencyMs) {
    var dot = card.querySelector('.status-dot');
    var txt = card.querySelector('.status-text');
    if (!dot || !txt) return;
    if (online === null) {
      dot.className = 'status-dot standby';
      txt.textContent = 'Standby — no live feed yet';
    } else if (online) {
      dot.className = 'status-dot online';
      txt.textContent = 'Online · ' + latencyMs + 'ms';
    } else {
      dot.className = 'status-dot offline';
      txt.textContent = 'Offline — feed unreachable';
    }
  }

  function checkHealth(name, card) {
    var url = HEALTH_CHECKS[name];
    var feedCheck = FEED_CHECKS[name];
    if (feedCheck) {
      // Check NexIntel's own data output for this feed
      fetch('data/latest.json', { cache: 'no-store' })
        .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
        .then(function (model) {
          if (feedCheck(model)) {
            setStatus(card, true, 0);
            card.querySelector('.status-text').textContent = 'Online · live feed';
          } else {
            setStatus(card, null);
          }
        })
        .catch(function () { setStatus(card, false); });
      return;
    }
    if (!url) { setStatus(card, null); return; }
    var t0 = performance.now();
    fetch(url, { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error();
        setStatus(card, true, Math.round(performance.now() - t0));
      })
      .catch(function () { setStatus(card, false); });
  }

  function init() {
    if (!window.OmniSightConfig) return;
    var cfg = window.OmniSightConfig.load();

    document.querySelectorAll('#engine-grid .card').forEach(function (card) {
      var name = card.getAttribute('data-engine');
      if (!name) return;
      var box = card.querySelector('input[type="checkbox"]');
      if (box) {
        box.checked = !!cfg.engines[name];
        box.addEventListener('change', function () {
          cfg.engines[name] = box.checked;
          window.OmniSightConfig.save(cfg);
          card.classList.toggle('disabled', !box.checked);
        });
        card.classList.toggle('disabled', !box.checked);
      }
      checkHealth(name, card);
    });

    var minC = document.getElementById('cfg-min-confidence');
    var maxR = document.getElementById('cfg-max-risk');
    if (minC) minC.value = cfg.minConfidence;
    if (maxR) maxR.value = cfg.maxRisk;

    var btn = document.getElementById('cfg-save');
    if (btn) {
      btn.addEventListener('click', function () {
        cfg.minConfidence = Math.max(0, Math.min(100, parseInt(minC.value, 10) || 70));
        cfg.maxRisk = Math.max(0, Math.min(10, parseInt(maxR.value, 10) || 3));
        window.OmniSightConfig.save(cfg);
        btn.textContent = 'Saved ✓';
        setTimeout(function () { btn.textContent = 'Save Configuration'; }, 1500);
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
