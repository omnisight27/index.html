# OmniSight — Deploy Checklist (Phase 1: login + Coinbase link)

Everything below is done by the site owner (Nic). Total time: about **25–35
minutes**, most of it clicking through two signups. Do the steps **in order**.
You will copy 5 values along the way — keep a notes app open.

**Money, honestly:** Supabase is free (no card needed). Render has a **free
tier** that works fine here — the only catch is the backend "sleeps" after
~15 minutes idle and takes ~30–50 seconds to wake on the next request
(the Portfolio page tells you to wait and reload if that happens).
Render's $7/mo tier removes the sleep. No card is required for the free tier.

---

## Step 1 — Supabase (database + login) · ~10 min

1. Go to **supabase.com** → *Start your project* → sign up (GitHub login is
   easiest) → **New project**.
   - Name: `omnisight` · Region: pick the closest US one · set a database
     password (save it in your password manager; the site never uses it).
   - Wait ~2 minutes for the project to finish setting up.
2. In the left sidebar: **SQL Editor** → **New query** → paste in the entire
   contents of `backend/schema.sql` from the repo → **Run**. You should see
   "Success. No rows returned."
3. **Project Settings** (gear icon, bottom left) → **API**. Copy these three
   values into your notes, labeled:
   - **Project URL** → `SUPABASE_URL`
   - **anon public** key → `SUPABASE_ANON_KEY` (this one is safe to publish)
   - **service_role** key → `SUPABASE_SERVICE_ROLE_KEY` ⚠️ *secret — notes
     only, never paste it anywhere except Step 2's Render form*
4. Same settings page, further down (or **Project Settings → API** →
   *JWT Settings*): copy the **JWT Secret** → label it `SUPABASE_JWT_SECRET`.
5. Left sidebar **Authentication → Providers → Google**: turn it ON only if
   you want the "Continue with Google" button. It needs a free Google Cloud
   OAuth client (Google walks you through it; ~5 extra minutes). Email
   sign-in links work with zero setup, so you can skip this for now.
6. **Authentication → URL Configuration**: under **Redirect URLs** add
   `https://omnisight27.github.io/**` and save. (This lets sign-in links
   bring you back to the site.)

## Step 2 — Render (the backend server) · ~10 min

1. Go to **render.com** → sign up (GitHub login is easiest).
2. **New → Web Service** → connect the GitHub repo `omnisight27/index.html`
   (grant access if asked).
3. Settings:
   - **Name**: `omnisight-backend`
   - **Root Directory**: `backend`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - **Instance type**: Free
4. **Environment** tab — add these 5 variables (values from your notes, plus
   one you generate):
   - `SUPABASE_URL` = Step 1's Project URL
   - `SUPABASE_SERVICE_ROLE_KEY` = Step 1's service_role key
   - `SUPABASE_JWT_SECRET` = Step 1's JWT Secret
   - `CRED_ENC_KEY` = generate one: on any computer with Python run
     `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
     — or ask Muse to generate one. ⚠️ *Save this in your password manager
     too. If it's lost, everyone has to re-link their Coinbase key.*
   - `CORS_ORIGINS` = `https://omnisight27.github.io`
5. **Create Web Service**. Wait for the deploy to go green (~2–3 min), then
   copy the service URL (looks like `https://omnisight-backend.onrender.com`)
   → label it `BACKEND_URL`.
6. Sanity check: open `https://<your-render-url>/health` in a browser — you
   should see `{"status":"ok"}`.

## Step 3 — Point the website at the backend · ~3 min

1. In the repo, open `config.js` and replace the three placeholders:
   `SUPABASE_URL`, `SUPABASE_ANON_KEY` (the **anon** key — *not* the
   service-role key), and `BACKEND_URL` (your Render URL).
2. Commit and push. GitHub Pages picks it up in about a minute.
   (Ask Muse to do this step for you if you'd rather not edit files.)

## Step 4 — Your Coinbase View-only key · ~5 min

1. Go to **portal.cdp.coinbase.com** → sign in with your Coinbase account.
2. **API Keys → Create API key**.
3. Permissions: **View only**. ⚠️ *Never enable Trade or Transfer — the site
   only ever needs to read balances, and a view-only key can't move money
   even if it leaks.*
4. Create it. Coinbase shows the **key name** and **private key** once —
   copy both (the key file it offers to download contains both).

## Step 5 — Link it on the site · ~2 min

1. Open the OmniSight **Portfolio** page → top right, **Log in** (use the
   email link the first time).
2. In the **Link Coinbase** card, paste the key name + private key → **Link
   Coinbase**. (The form wipes itself the moment you submit; the key goes
   only to the backend, encrypted.)
3. Click **⟳ Sync from Coinbase**. Your balances fill the holdings table,
   marked "⟳ Synced from Coinbase."
4. Done. Re-sync any time after you trade. **Unlink** (same card) deletes
   the stored key from the backend immediately.

---

### If something looks broken

- **"Account linking isn't set up yet"** on the Portfolio page → Step 3
  wasn't saved/pushed yet, or a placeholder is still in `config.js`.
- **"Couldn't reach the OmniSight backend"** → the Render service is asleep
  (free tier). Wait 30–50 seconds and reload.
- **Login link lands on an error** → Step 1.6 redirect URL missing or typo'd.
- **Coinbase says the key is invalid** → the key was created with the wrong
  key type, or the private key paste lost its first/last line. It must
  include the `-----BEGIN EC PRIVATE KEY-----` and `-----END EC PRIVATE
  KEY-----` lines.
