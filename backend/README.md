# OmniSight Backend (Phase 1)

A small FastAPI service that holds users' exchange credentials so the static
OmniSight site never has to. GitHub Pages serves the frontend from the repo
root; this folder deploys separately (Render) and is ignored by Pages.

## What it does

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /health` | none | liveness check |
| `POST /link/coinbase` | Supabase JWT | store a Coinbase CDP key, Fernet-encrypted |
| `GET /link/status` | Supabase JWT | linked? + masked key name |
| `GET /portfolio/coinbase` | Supabase JWT | normalized balances `[{symbol, units, price_usd, value_usd, staked, staked_units}]` |
| `DELETE /link/coinbase` | Supabase JWT | delete stored credentials |

Coinbase access is **read-only by construction**: the client only ever calls
`GET /api/v3/brokerage/accounts` (plus public spot tickers for USD values).
Users are instructed to create **View-only** CDP keys.

## Security model

- **User auth**: Supabase JWTs verified against `SUPABASE_JWT_SECRET`; the
  `sub` claim is the user id and scopes every database operation.
- **Encryption at rest**: credentials are stored as a Fernet-encrypted JSON
  blob (`CRED_ENC_KEY`). Plaintext exists only in memory during a request.
  ⚠️ If `CRED_ENC_KEY` is lost or rotated, stored credentials are
  unrecoverable and users must re-link. Keep the key in Render's env vars
  (and a password manager), nowhere else.
- **Database**: Supabase Postgres, `user_credentials` table (see
  `schema.sql`). RLS is enabled with no public policies — the anon key can
  touch nothing; this service uses the service-role key, server-side only.
- **Keys are never logged**, never returned to the frontend (only a masked
  tail like `…a1b2c3`), and never touch git.
- **Known limitation — staking**: Coinbase's brokerage accounts API has no
  explicit staked-balance field. Staked assets appear as separate accounts
  whose name mentions staking (e.g. "Ethereum Staking"); we detect that and
  flag the merged position `staked`. If Coinbase changes the shape, balances
  still sync, just without the flag.

## Local run

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env   # fill values, then: export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --reload --port 8000
python -m pytest tests -q
```

## Deploy (Render)

1. Supabase first: create the project, run `schema.sql` in the SQL editor,
   enable the Google provider (Authentication → Providers) if you want the
   Google button, and add your Pages URL
   (`https://omnisight27.github.io`) to Authentication → URL Configuration →
   Redirect URLs so magic links return to the site.
2. Render: New → Web Service → connect this repo →
   **Root Directory** `backend`, **Build** `pip install -r requirements.txt`,
   **Start** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
3. Render → Environment: set `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
   `SUPABASE_JWT_SECRET`, `CRED_ENC_KEY`, `CORS_ORIGINS`
   (see `.env.example`). Free tier services sleep after ~15 min idle — the
   first request after that takes ~30–50s to wake.
4. Put the Render URL + Supabase URL/anon key into the site's `config.js`
   and commit. Full click-by-click steps: **DEPLOY-CHECKLIST.md**.
