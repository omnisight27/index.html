// OmniSight outbound link guard (2026-10-25).
//
// Why it exists: a Command Hub link landed on a parked domain whose ad
// network shows fake "you've been hacked" warnings. This guard does three
// things:
//  1. Forces rel="noopener noreferrer" on every external link at load
//     (stops the opened page reaching back into OmniSight — tab-nabbing).
//  2. Lets verified-good domains through with no friction.
//  3. Pauses on anything else: a plain box naming the destination domain
//     and, for flagged links, the exact problem found (parked domain /
//     redirect loop). "Continue" is a real link; "Go back" closes the box.
// Nothing here phones home or blocks a domain outright — the user always
// gets the last word.
(function () {
  'use strict';

  // Domains verified live (200 + legitimate destination) on 2026-10-25.
  // Includes the site's own generated reference links (CoinGecko info,
  // SEC filings, St. Louis Fed articles) — the guard is for the Hub's
  // third-party link farm, not for the site's own content.
  var ALLOW = {
    'coinbase.com': 1, 'www.coinbase.com': 1,
    'coinmarketcap.com': 1,
    'www.nasdaq.com': 1, 'www.otcmarkets.com': 1, 'www.partsbase.com': 1,
    'vimeo.com': 1, 'www.vimeo.com': 1,
    'facebook.com': 1, 'www.facebook.com': 1, 'm.facebook.com': 1,
    'fb.me': 1, 'm.me': 1,
    'www.moneymaxaccount.com': 1, 'mydemosystem.com': 1,
    'my.rapidfunnel.com': 1, 'app.rapidfunnel.com': 1,
    'portal.ufinancialfreedom.com': 1, 'uffpowerteam.com': 1,
    'www.creditinfocenter.com': 1,
    'coingecko.com': 1, 'www.coingecko.com': 1,
    'sec.gov': 1, 'www.sec.gov': 1,
    'stlouisfed.org': 1, 'www.stlouisfed.org': 1
  };

  // Flagged links carry data-risk on the anchor; the text names the problem.
  var RISK_TEXT = {
    parked: 'This domain is parked — it no longer serves the resource it used to. Parked pages run ad networks that have shown fake "you\'ve been hacked" warnings.',
    loop: 'This link is broken — the address redirects to itself in a loop and never loads.'
  };

  var overlay, domainEl, riskEl, goEl;

  function buildModal() {
    overlay = document.createElement('div');
    overlay.className = 'guard-overlay';
    overlay.id = 'guard-overlay';
    overlay.hidden = true;
    overlay.innerHTML =
      '<div class="guard-box" role="alertdialog" aria-labelledby="guard-title">' +
        '<h3 id="guard-title">You\'re leaving OmniSight</h3>' +
        '<p class="guard-domain" id="guard-domain"></p>' +
        '<p class="guard-risk" id="guard-risk" hidden></p>' +
        '<p class="guard-note">If a page ever says you\'ve been hacked or tells you to call a number, close the tab — it\'s a scare ad, not a real warning. A real warning never asks you to call anyone.</p>' +
        '<div class="guard-actions">' +
          '<a id="guard-go" class="btn primary" target="_blank" rel="noopener noreferrer">Continue →</a>' +
          '<button id="guard-back" class="btn ghost" type="button">Go back</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(overlay);
    domainEl = overlay.querySelector('#guard-domain');
    riskEl = overlay.querySelector('#guard-risk');
    goEl = overlay.querySelector('#guard-go');
    overlay.querySelector('#guard-back').addEventListener('click', hide);
    goEl.addEventListener('click', hide);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) hide(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') hide(); });
  }

  function show(url, risk) {
    domainEl.textContent = 'Destination: ' + url;
    if (risk && RISK_TEXT[risk]) {
      riskEl.textContent = '⚠️ ' + RISK_TEXT[risk];
      riskEl.hidden = false;
      overlay.querySelector('.guard-box').classList.add('guard-risk-box');
    } else {
      riskEl.hidden = true;
      overlay.querySelector('.guard-box').classList.remove('guard-risk-box');
    }
    goEl.href = url;
    overlay.hidden = false;
  }

  function hide() { if (overlay) overlay.hidden = true; }

  function init() {
    buildModal();
    document.querySelectorAll('a[href^="http"]').forEach(function (a) {
      a.setAttribute('rel', 'noopener noreferrer');
    });
    document.addEventListener('click', function (e) {
      var a = e.target.closest ? e.target.closest('a[href^="http"]') : null;
      if (!a) return;
      // The modal's own Continue button is an external link too — never
      // guard it, or Continue re-triggers the modal instead of leaving.
      if (a.closest('#guard-overlay')) return;
      var host = '';
      try { host = new URL(a.href).hostname; } catch (err) { return; }
      if (host === window.location.hostname) return;
      if (ALLOW[host]) return;
      e.preventDefault();
      show(a.href, a.getAttribute('data-risk'));
    }, true);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else { init(); }
})();
