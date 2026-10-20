// OmniSight data stamp — one consistent freshness line for engine data.
//
// Renders "Data pulled 2:15 PM ET · Published 2:16 PM ET · 12 min ago"
// from a published engine JSON (data/latest.json or data/intraday.json).
// Times are the engine's UTC data_pulled_at / published_at, shown in
// America/New_York; the age is relative to the publish time, computed at
// render. Older files that only carry the legacy `generated` field
// degrade to a single "Data as of …" line; no timestamp → renders
// nothing (the element stays empty).
//
// Usage: nxStampInto(element, jsonData). Sets data-stamped="1" on the
// element when it writes, so page code can tell the stamp landed.

(function () {
  'use strict';

  var TZ = 'America/New_York';

  function fmtET(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleTimeString('en-US', {
      timeZone: TZ, hour: 'numeric', minute: '2-digit'
    });
  }

  function agoText(iso) {
    var t = new Date(iso).getTime();
    if (isNaN(t)) return '';
    var mins = Math.floor((Date.now() - t) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return mins + ' min ago';
    var hrs = Math.floor(mins / 60);
    if (hrs < 24) return hrs + (hrs === 1 ? ' hr ago' : ' hrs ago');
    var days = Math.floor(hrs / 24);
    return days + (days === 1 ? ' day ago' : ' days ago');
  }

  function stampText(d) {
    if (!d) return '';
    var pulled = d.data_pulled_at || null;
    var published = d.published_at || null;
    if (pulled && published) {
      var p = fmtET(pulled), q = fmtET(published), a = agoText(published);
      if (!p || !q) return '';
      return 'Data pulled ' + p + ' ET · Published ' + q + ' ET' + (a ? ' · ' + a : '');
    }
    var single = published || pulled || d.generated || null;
    if (!single) return '';
    var t = fmtET(single), ag = agoText(single);
    if (!t) return '';
    var label = published ? 'Published ' : (pulled ? 'Data pulled ' : 'Data as of ');
    return label + t + ' ET' + (ag ? ' · ' + ag : '');
  }

  window.nxStampText = stampText;
  window.nxStampInto = function (el, d) {
    if (!el) return;
    var t = stampText(d);
    if (t) {
      el.textContent = t;
      el.setAttribute('data-stamped', '1');
    }
  };
})();
