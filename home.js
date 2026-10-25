// Homepage live hints — reads the published engine run (data/latest.json)
// and fills the hero status line + the three journey-card hints. Every
// fill is guarded: if the data isn't there yet, the static fallback text
// stays. Nothing here invents a number.
(function () {
  'use strict';
  function setText(id, text) {
    var el = document.getElementById(id);
    if (el && text) el.textContent = text;
  }
  fetch('data/latest.json?_=' + Date.now())
    .then(function (r) { return r.json(); })
    .then(function (d) {
      var regime = d.regime_label || d.buy_text || d.regime || '';
      var fg = d.psychology && d.psychology.fear_greed;
      var topLong = (d.longs && d.longs[0]) || null;
      var parts = [];
      if (regime) parts.push(regime);
      if (fg !== undefined && fg !== null) parts.push('Fear & Greed ' + fg);
      if (topLong) parts.push('Top long: ' + topLong.asset + ' ' + topLong.confidence + '%');
      if (parts.length) setText('hero-status', parts.join(' · '));
      if (topLong) setText('hint-picks', 'Top long right now: ' + topLong.asset + ' at ' + topLong.confidence + '% confidence.');
      var nF = (d.foresight && d.foresight.length) || 0;
      setText('hint-foresight', nF ? nF + ' long-horizon signals being tracked — newest first.' : 'Foresight is watching the Federal Register and CBO feeds.');
      if (d.buy_text) setText('hint-money', d.buy_text + '.');
      if (window.nxStampInto) {
        var stamp = document.getElementById('data-stamp');
        if (stamp) window.nxStampInto(stamp, d);
      }
    })
    .catch(function () { /* fallbacks stay */ });
})();
