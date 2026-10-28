// OmniSight — public site configuration (Phase 1: accounts + Coinbase link).
//
// HOW TO FILL THIS IN (owner only — see backend/DEPLOY-CHECKLIST.md):
// After creating the Supabase project and deploying the backend, replace the
// three placeholder values below with the real ones and commit this file.
//   SUPABASE_URL      — Supabase dashboard → Project Settings → API → Project URL
//   SUPABASE_ANON_KEY — Supabase dashboard → Project Settings → API → anon key
//   BACKEND_URL       — the Render service URL, e.g. https://omnisight-backend.onrender.com
//
// THESE KEYS ARE PUBLIC BY DESIGN: the anon key is meant to ship in browsers
// (Row Level Security in Supabase is what protects the data). NEVER put the
// Supabase service-role key, a Coinbase key, or any other secret in this file.
window.OMNISIGHT_CONFIG = {
  SUPABASE_URL: 'https://xafnsaefesfzucrccuht.supabase.co',
  SUPABASE_ANON_KEY: 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhhZm5zYWVmZXNmenVjcmNjdWh0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTI2MjM1MDUsImV4cCI6MjEwODE5OTUwNX0.VmOfxhT2BeUD7JJKPtX0-iuygbxG4Ou1UtsmX7qFB0g',
  BACKEND_URL: 'https://omnisight-backend-4mbs.onrender.com'
};

// True once real values replace the placeholders. When false, login and
// account-linking features hide themselves and the site works exactly as
// before (manual, on-device tracking only).
window.OMNISIGHT_CONFIGURED = (function () {
  var c = window.OMNISIGHT_CONFIG;
  return [c.SUPABASE_URL, c.SUPABASE_ANON_KEY, c.BACKEND_URL].every(function (v) {
    return typeof v === 'string' && v.length > 8 && v.indexOf('YOUR-') === -1;
  });
})();
