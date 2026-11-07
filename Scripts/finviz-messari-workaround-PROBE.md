# Finviz FREE + Messari x402 — Workaround Probe (2026-11-07)

Display-only / shadow research. Nothing here touches a live buy gate,
places a trade, sends anything, or needs a credential. No sign-up, no
payment, no key was created for this probe.

## 1. Finviz FREE screener — PROTOTYPE WORKS LIVE

**File:** `Scripts/finviz_feed.py` · **Tests:** `Scripts/test_finviz_feed.py`
(14 passed, `python3 -m pytest Scripts/test_finviz_feed.py`)

**Live smoke test (Sat 2026-11-07, market closed — values are Fri closes):**
- `fetch_finviz_universe()` → **30/30 tickers**, 4 requests
  (2 views × 2 pages), diag
  `OK 30/30 tickers | OK view=111 rows=30 pages=2 | OK view=171 rows=30 pages=2`
- Rows carry: ticker, company, sector, industry, country, market_cap,
  PE, price, change_pct, volume, beta, ATR, SMA20/50/200 (% vs SMA),
  52w high/low (% vs), RSI, change_open_pct, gap_pct.
- Sample: AAPL $342.37, RSI 51.46, −0.52%; ABBV $291.31, RSI 59.69,
  +1.07%; ADBE $239.30, RSI 42.30, −2.90%; AMD +11.04% (big-mover day).

**What was verified about the free page:**
- Public URL `https://finviz.com/screener/stocks?v=<view>&t=<CSV>` —
  plain GET + browser User-Agent, HTTP 200, no login/key/Elite.
  (Legacy `/screener.ashx` 301-redirects here.)
- The grid is server-rendered **divs, not a `<table>`**: rows are
  `<div data-row-key="TICKER">`, cells `<div data-column-id="…">`.
  Parsed with stdlib `html.parser` only — no `finviz` PyPI lib.
- Pagination is 20 rows/page (`r=21` = page 2); 2 pages cover the 30.
- Views renumbered since the old docs — verified live:
  `111` Overview, `171` Technical (RSI lives here), `141` Performance
  (perf 1w/4w/YTD/52w, volatility, relative volume — not yet fetched
  by default; add `'141'` to `views=` to include it).
- One real parsing trap found and fixed: the ticker cell contains the
  logo tile's fallback letter, so naive text concat gave `AAAPL`.
  Ticker is now taken from `data-row-key` (authoritative).

**Honest limits (also in the module docstring):**
- Delayed data. Page footer says stock quotes delayed 1 minute;
  independent reviews report the free tier delayed up to ~15 min
  (NASDAQ) / ~20 min (NYSE/AMEX). Either way: screening/discovery
  and shadow research ONLY, never a live signal source.
- Screen-scraping is brittle by nature: Finviz can change markup,
  throttle, or block. Every fetch is fail-open (empty rows + diag,
  never raises) and the module makes ~4 requests per run. Do not
  hammer it.

## 2. Messari — docs + live no-key probes (NO payment, NO key)

**Current API host:** `https://api.messari.io`
**Auth (keyed route):** header `x-messari-api-key` — all keyed
endpoints require a key (docs: `docs.messari.io/api-reference/authentication`).
**Current path structure:** `https://api.messari.io/<family>/v1|v2/<endpoint>`
(e.g. `/token-unlocks/v1/...`, `/metrics/v2/assets`, `/news/v1/news/feed`,
`/funding/v1/rounds`, `/ai/v2/chat/completions`).
**Legacy path DEAD — verified live:** `data.messari.io/api/v2/assets` →
HTTP 404. (Also: bare `api.messari.io/v1/assets` → 404; the family
prefix is required.)

### Token Unlocks endpoint (the one that matters for OmniSight)
- `GET /token-unlocks/v1/assets/{assetId}/events` → per-asset unlock
  events: `asset{id,name,slug,symbol}` + `unlockEvents[]` with
  `timestamp`, `cliff{amountNative, amountUSD, percentOfTotalAllocation,
  allocations[{allocationRecipient, amountNative, amountUSD, …}]}`,
  `dailyLinearRateChange`. (Shape confirmed from the docs' SOL example.)
- The family has 5 endpoints: Allocations, Assets, Asset Events,
  Asset Unlocks, Asset Vesting Schedule.

### Free plan — CONFLICT, report both sides
- Legacy docs still describe an unpaid tier: auth page (updated
  2026-03-23) says "by default, all users have access to the Assets &
  Market Data and Exchanges API endpoints"; Tiers & Limits page
  (updated 2026-08-17) still lists "Unpaid / Lite / Pro: 200 req/min"
  and "Free: 10 AI chat completions/day".
- Post-acquisition reporting (CoinStats et al., pricing re-verified
  Oct 2026) says the opposite: "The Free plan no longer includes API
  access"; Pro is back at **$375/mo billed yearly, Assets endpoints
  only**; the full API sits in a sales-quoted Blockworks Data plan.
- Either way, the keyed route to **Token Unlocks is Enterprise /
  sales-gated** (Tiers page: "for News, Token Unlocks, Fundraising…
  please get in touch [Blockworks] to discuss Enterprise options").
  Do not assume a free key reaches unlocks.

### x402 pay-per-call — VERIFIED LIVE, no account/key needed
Docs: `docs.messari.io/api-reference/x402-payments` (updated 2026-05-05).
Flow: unauthenticated request → `HTTP 402 Payment Required` with
payment details → sign a USDC transfer on Base or Solana → gateway
verifies and serves the request. Discovery:
`GET https://api.messari.io/.well-known/x402` → **HTTP 200, 59 resources**
(probed live, includes all 5 unlocks routes + both news routes).

Official per-call prices (USDC):

| Endpoint | Route | Price |
|---|---|---|
| Get Asset Events | `/token-unlocks/v1/assets/{assetId}/events` | **$0.15** |
| Get Allocations | `/token-unlocks/v1/allocations` | $0.25 |
| Get Assets | `/token-unlocks/v1/assets` | $0.00 (docs) — see discrepancy below |
| Get Asset Unlocks | `/token-unlocks/v1/assets/{assetId}/unlocks` | $0.75 |
| Get Asset Vesting Schedule | `/token-unlocks/v1/assets/{assetId}/vesting-schedule` | $0.75 |
| News Feed | `/news/v1/news/feed` | **$0.55** |
| Get News Sources | `/news/v1/news/sources` | $0.00 |
| List Assets | `/metrics/v2/assets` | $0.00 |
| Asset Timeseries | `/metrics/v2/assets/{id}/metrics/…/time-series/{granularity}` | 1h $0.18 / 1d $0.15 |
| MessariAI chat | `/ai/v2/chat/completions` | $0.25 |

**Live 402 probes (no key, no payment — stopped at the 402):**
- `…/token-unlocks/v1/assets/<SOL uuid>/events` → **HTTP 402**,
  accepts show amount `150000` (= $0.15 USDC, 6 decimals) on Base
  and Solana. Matches the docs price. Route is live.
- `…/token-unlocks/v1/assets` → **HTTP 402, amount `750000` (= $0.75)**.
  ⚠️ **Discrepancy: the docs table lists Get Assets at $0.00, but the
  live gateway asks $0.75.** The docs price table (May 2026) is stale
  on at least this route — trust the live 402 amount, not the table.

### Cost sketch (x402, Events endpoint — the cheap useful one)
- One unlock-events pull for the live top-200 crypto universe:
  200 × $0.15 = **$30 per pass**. Daily = ~$900/mo; weekly = ~$120/mo.
- A focused watchlist (top 20) = $3.00 per pass.
- News feed is $0.55/call — one call returns the feed, not per asset.
- No subscription, no key management; but every call costs, so the
  cadence is the budget decision. Nothing was paid in this probe.

## 3. Bottom line for Nic

- **Finviz free: usable now, $0.** The prototype proves the free
  screener returns the full 30-ticker universe with price/change/
  volume/PE/RSI/SMAs. Best use: a screening/discovery feed for the
  stocks research track (e.g. relative-volume + RSI + gap watchlist),
  shadow-graded like everything else. It does NOT fix the real-time
  problem — still delayed — so stocks stay shadow/paper.
- **Messari unlocks: real data, real per-call cost, no free lunch.**
  The x402 route works with no account and the Events endpoint is
  $0.15/call live-verified. A token-unlocks research track is feasible
  as a weekly (not daily) pull over a focused list — but the per-call
  cost needs Nic's call before any wallet/USDC is involved, and the
  docs price table is stale in at least one place ($0.00 vs $0.75 live).
- **No code was written for Messari** (probe only, per task). A
  `messari_unlocks_feed.py` would be the next step if Nic approves —
  x402 payment flow, fail-open, display-only, shadow-graded.
