# About this folder — the OmniSight scaffold, folded in

Everything in this folder came from the private repo **omnisight27/OmniSight**
(the original September 2026 scaffold), copied verbatim on 2026-11-18 from
its main branch at commit `bdd71d791c986d916feb3ae83b74448030e8d2b1`.

## Why it lives here instead of at the repo root

The scaffold and the live site overlap by name but not by content:

- The scaffold has its own `index.html`, `app.html`, `styles.css`, `README.md`
  and a `backend/` with a different layout (`agents/`, `api/`, `models/`,
  `services/`) than the live backend (`backend/app/`). Copying those over the
  live files would have broken the live site.
- So the whole scaffold was folded in **as-is, under this folder**, with its
  internal layout preserved. Nothing live was overwritten; nothing from the
  scaffold was lost.

## What is live

The live dashboard, engine scripts, data loops and backend live at the repo
root (`index.html`, `Scripts/`, `data/`, `backend/app/`). This folder is a
reference copy of where the project started. If a piece of it is ever promoted
into the live code, copy it out deliberately — don't edit in place and expect
the site to pick it up.

## Contents (70 files)

- Root: early app pages (`index.html`, `app.html`, `Dashboard`, `OmniSight`),
  `styles.css`, docs (`README.md`, `ROADMAP.md`, `UI-ROADMAP.md`,
  `NEXINTEL-FEED.md`, `OMNISIGHT-ARCHITECTURE.md`, `nexintel-engine.md`),
  and 1-byte placeholders (`backend.md`, `data`, `frontend`, `ui`).
- `backend/`: scaffold backend — `config.py`, `database.py`, `main.py`,
  `supabase_client.py`, `requirements.txt`, plus `agents/` (8 stubs),
  `api/` (6 stubs), `models/` (6 stubs), `services/` (14 stubs).
- `engines/`: 15 one-paragraph engine sketches (`.md`) plus `.gitkeep`/`gitkeep`.
