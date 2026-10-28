// OmniSight engine config — Loop 4
// Shared module: engine enable/disable + alert thresholds, persisted in localStorage.
// The engines page writes it; the alerts page reads it.

(function () {
  'use strict';

  var KEY = 'omnisight_engine_config_v1';

  var DEFAULTS = {
    engines: {
      'Contract Intel': true,
      'Liquidity Engine': true,
      'Crypto Pulse': true,
      'Commerce Radar': true,
      'Macro Scope': true,
      'Chain Signal': true
    },
    minConfidence: 65,
    maxRisk: 3
  };

  function load() {
    try {
      var raw = localStorage.getItem(KEY);
      if (!raw) return JSON.parse(JSON.stringify(DEFAULTS));
      var cfg = JSON.parse(raw);
      // merge with defaults so new engines never go missing
      Object.keys(DEFAULTS.engines).forEach(function (k) {
        if (!(k in cfg.engines)) cfg.engines[k] = true;
      });
      if (typeof cfg.minConfidence !== 'number') cfg.minConfidence = 65;
      if (typeof cfg.maxRisk !== 'number') cfg.maxRisk = 3;
      return cfg;
    } catch (e) {
      return JSON.parse(JSON.stringify(DEFAULTS));
    }
  }

  function save(cfg) {
    try { localStorage.setItem(KEY, JSON.stringify(cfg)); } catch (e) {}
  }

  window.OmniSightConfig = { load: load, save: save, defaults: DEFAULTS };
})();
