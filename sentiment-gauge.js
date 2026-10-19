// OmniSight sentiment gauge — Loop 2
// Pulls the crypto Fear & Greed Index from alternative.me (free, no key).
// Renders a gauge on the dashboard; degrades gracefully if unreachable.

(function () {
  'use strict';

  function gaugeColor(v) {
    if (v < 25) return '#dc2626';   // extreme fear
    if (v < 45) return '#f59e0b';   // fear
    if (v < 55) return '#9ca3af';   // neutral
    if (v < 75) return '#10b981';   // greed
    return '#00d9ff';               // extreme greed
  }

  function render(value, label) {
    var el = document.getElementById('sentiment-gauge');
    if (!el) return;
    var color = gaugeColor(value);
    // semicircle gauge via conic-gradient
    var pct = Math.max(0, Math.min(100, value));
    el.innerHTML =
      '<div class="gauge-dial" style="background: conic-gradient(' +
      'from 270deg, ' + color + ' 0deg, ' + color + ' ' + (pct * 1.8) + 'deg, ' +
      '#1f2937 ' + (pct * 1.8) + 'deg, #1f2937 180deg)">' +
      '<div class="gauge-center"><span class="gauge-value">' + value + '</span>' +
      '<span class="gauge-label">' + label + '</span></div></div>' +
      '<p class="live-stamp">Fear &amp; Greed Index · updated ' +
      new Date().toLocaleTimeString() + '</p>';
  }

  function init() {
    fetch('https://api.alternative.me/fng/?limit=1')
      .then(function (r) { if (!r.ok) throw new Error('bad status'); return r.json(); })
      .then(function (j) {
        var d = j && j.data && j.data[0];
        if (!d) throw new Error('no data');
        render(parseInt(d.value, 10), d.value_classification);
      })
      .catch(function () {
        var el = document.getElementById('sentiment-gauge');
        if (el) el.innerHTML = '<p class="live-stamp">Sentiment data unavailable</p>';
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
