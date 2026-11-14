"""DeFiLlama stablecoin / TVL feed — free data-edge track #4 (2026-11-07,
~/workspace/goals/build-out-omnisight/hidden_files/
free-data-edge-dive-2026-11-07.md).

DISPLAY-ONLY + SHADOW. This module never touches a buy gate: it does
not import, call, or consult gate_math.py, gate_circuit.py, or the
60% live bar. Stablecoin and TVL readings are annotations, never
inputs. No alert-tier output, no confidence influence, no trades.

Why stablecoins: circulating stablecoin supply is the liquidity
proxy — the "dry powder" sitting on-chain. Supply growth has led
rallies (new dollars entering crypto); contraction means money
leaving. DeFi TVL is the same idea for deployed capital. The engine
has no liquidity variable anywhere, so this is a genuinely new
dimension — and it costs nothing.

Endpoints (all GET, keyless, no sign-up):
  * Current supplies:
    /stablecoins  (https://stablecoins.llama.fi/stablecoins)
    — every stablecoin's circulating supply + peg. USDT ~$176.8B,
    USDC ~$70.6B at probe time (2026-11-07). Total USD-pegged
    circulating ~$292B. One small call per run.
  * Current TVL:
    /v2/chains  (https://api.llama.fi/v2/chains)
    — TVL by chain (421 chains). Sum of `tvl` is total DeFi TVL
    (~$81B at probe time; excludes staking/borrowed — noted where
    displayed). One small call per run.
  * History (HEAVY — day-cached by the caller, never every run):
    /stablecoincharts/all — 3,266 daily points of total stablecoin
    circulating (totalCirculatingUSD.peggedUSD). Gives the 7d/24h
    total-supply change with NO warm-up.
    /v2/historicalChainTvl — 3,329 daily points of total TVL.
    Gives the 7d/24h TVL change with NO warm-up.
  * Per-asset history does NOT exist: /stablecoin/{id} is metadata
    only (probe 2026-11-07: /stablecoin/tether 404s,
    /stablecoin/1 returns metadata, no series). So the USDT+USDC
    7d/24h change rides the cross-run history store in
    intraday.json['defillama_history'] — the same persistence trick
    as oi_flow / okx_oi_history. It warms up over the first day
    (24h change) and the first week (7d change): until a point old
    enough exists, those cells read None ("warming up"), never a
    guess.

Every fetch is fail-open: a dead endpoint, a 429, or bad JSON
degrades that leg to "no data", never an exception, never a blocked
run. History endpoints are fetched at most once a day (the caller
day-caches the derived changes); the two current-value endpoints
run once per intraday run.
"""

import math
import time

STABLECOINS_URL = 'https://stablecoins.llama.fi/stablecoins'
STABLECOIN_CHARTS_URL = 'https://stablecoins.llama.fi/stablecoincharts/all'
CHAINS_URL = 'https://api.llama.fi/v2/chains'
TVL_HISTORY_URL = 'https://api.llama.fi/v2/historicalChainTvl'

MAX_HISTORY_POINTS = 400  # ~8 days at the 30-min run cadence
DAY_SECS = 24 * 3600


# ---------------------------------------------------------------------------
# Parsers — pure, never raise, None / {} on bad data
# ---------------------------------------------------------------------------

def _as_float(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_stablecoins(data):
    """{'total_usd', 'usdt', 'usdc', 'usdt_usdc', 'dominance',
    'count'} from a /stablecoins response, or None.

    total_usd sums `circulating.peggedUSD` across every asset that
    carries it (USD-pegged supply; EUR/JPY-pegged stables carry
    their own peg keys and are not double-counted here). dominance
    is the USDT+USDC share of that total. USDT/USDC are matched by
    exact symbol (first match wins — the list holds one row per
    stablecoin)."""
    try:
        if not isinstance(data, dict):
            return None
        assets = data.get('peggedAssets')
        if not isinstance(assets, list) or not assets:
            return None
        total = 0.0
        usdt = None
        usdc = None
        count = 0
        for a in assets:
            if not isinstance(a, dict):
                continue
            circ = a.get('circulating') or {}
            if not isinstance(circ, dict):
                continue
            v = _as_float(circ.get('peggedUSD'))
            if v is None:
                continue
            count += 1
            total += v
            sym = (a.get('symbol') or '').upper().strip()
            if sym == 'USDT' and usdt is None:
                usdt = v
            elif sym == 'USDC' and usdc is None:
                usdc = v
        if total <= 0:
            return None
        both = (usdt or 0.0) + (usdc or 0.0)
        return {'total_usd': total,
                'usdt': usdt,
                'usdc': usdc,
                'usdt_usdc': both if both > 0 else None,
                'dominance': round(both / total, 4) if both > 0 else None,
                'count': count}
    except Exception:
        return None


def parse_stablecoin_charts(data):
    """[(ts, total_usd), ...] daily total-stablecoin-supply series
    from a /stablecoincharts/all response, oldest first. [] on any
    failure. Uses totalCirculatingUSD.peggedUSD (USD value of
    circulating supply)."""
    out = []
    try:
        if not isinstance(data, list):
            return out
        for row in data:
            if not isinstance(row, dict):
                continue
            ts = _as_float(row.get('date'))
            tot = (row.get('totalCirculatingUSD') or {})
            if not isinstance(tot, dict):
                continue
            v = _as_float(tot.get('peggedUSD'))
            if ts is None or v is None:
                continue
            out.append((int(ts), v))
        out.sort(key=lambda x: x[0])
    except Exception:
        return out
    return out


def parse_chains(data):
    """{'tvl_total', 'chains'} from a /v2/chains response, or None.
    tvl_total sums `tvl` across chains (DeFiLlama's TVL, excluding
    staking/borrowed). chains is the top-10 by TVL as
    [(name, tvl), ...]."""
    try:
        if not isinstance(data, list) or not data:
            return None
        rows = []
        total = 0.0
        for c in data:
            if not isinstance(c, dict):
                continue
            v = _as_float(c.get('tvl'))
            if v is None:
                continue
            total += v
            rows.append(((c.get('name') or '?'), v))
        if total <= 0:
            return None
        rows.sort(key=lambda x: x[1], reverse=True)
        return {'tvl_total': total,
                'chains': [(n, round(v, 2)) for n, v in rows[:10]]}
    except Exception:
        return None


def parse_tvl_history(data):
    """[(ts, tvl), ...] daily total-TVL series from a
    /v2/historicalChainTvl response, oldest first. [] on failure."""
    out = []
    try:
        if not isinstance(data, list):
            return out
        for row in data:
            if not isinstance(row, dict):
                continue
            ts = _as_float(row.get('date'))
            v = _as_float(row.get('tvl'))
            if ts is None or v is None:
                continue
            out.append((int(ts), v))
        out.sort(key=lambda x: x[0])
    except Exception:
        return out
    return out


# ---------------------------------------------------------------------------
# Changes — % move over a lookback, None when the window can't be built
# ---------------------------------------------------------------------------

def _change_pct(series, now_ts, lookback_secs, slack_secs=3600):
    """% change from the point nearest `lookback_secs` ago to the
    latest point. None when the window can't be built (too few
    points, gap, zero base). slack_secs is how stale the base point
    may be — 3600 for the 30-min local store, a full day for the
    daily API series."""
    try:
        if not series or len(series) < 2:
            return None
        latest_ts, latest_v = series[-1]
        target = now_ts - lookback_secs
        base = None
        for ts, v in reversed(series[:-1]):
            if ts <= target:
                base = (ts, v)
                break
        if base is None:
            return None
        base_ts, base_v = base
        if now_ts - base_ts > lookback_secs + slack_secs:
            return None
        if not base_v:
            return None
        return (latest_v - base_v) / base_v * 100
    except Exception:
        return None


def supply_changes(charts_series, now_ts):
    """{'total_chg_7d_pct', 'total_chg_24h_pct'} for total
    stablecoin supply from the daily charts series. Values are None
    while the window can't be built (the series itself carries
    ~9 years, so in practice this is never warming up)."""
    try:
        chg_7d = _change_pct(charts_series, now_ts, 7 * DAY_SECS,
                             slack_secs=DAY_SECS)
        chg_24h = _change_pct(charts_series, now_ts, DAY_SECS,
                              slack_secs=DAY_SECS)
        return {'total_chg_7d_pct': (round(chg_7d, 2)
                                     if chg_7d is not None else None),
                'total_chg_24h_pct': (round(chg_24h, 2)
                                      if chg_24h is not None else None)}
    except Exception:
        return {'total_chg_7d_pct': None, 'total_chg_24h_pct': None}


def tvl_changes(tvl_series, now_ts):
    """{'tvl_chg_7d_pct', 'tvl_chg_24h_pct'} for total DeFi TVL from
    the daily history series. Same no-warm-up shape as
    supply_changes."""
    try:
        chg_7d = _change_pct(tvl_series, now_ts, 7 * DAY_SECS,
                             slack_secs=DAY_SECS)
        chg_24h = _change_pct(tvl_series, now_ts, DAY_SECS,
                              slack_secs=DAY_SECS)
        return {'tvl_chg_7d_pct': (round(chg_7d, 2)
                                   if chg_7d is not None else None),
                'tvl_chg_24h_pct': (round(chg_24h, 2)
                                    if chg_24h is not None else None)}
    except Exception:
        return {'tvl_chg_7d_pct': None, 'tvl_chg_24h_pct': None}


# ---------------------------------------------------------------------------
# Local store — USDT+USDC supply (no per-asset history endpoint exists)
# ---------------------------------------------------------------------------

def update_supply_history(hist, usdt_usdc, now_ts):
    """Fold this run's USDT+USDC supply into the store. hist:
    {'usdt_usdc': [[ts, value], ...]}. Returns a NEW dict; a missing
    reading leaves the store untouched. Last MAX_HISTORY_POINTS
    points kept (~8 days at the 30-min cadence)."""
    new_hist = {}
    try:
        for k, pts in (hist or {}).items():
            if isinstance(pts, list):
                new_hist[k] = list(pts)
    except Exception:
        new_hist = {}
    try:
        v = _as_float(usdt_usdc)
        if v is None:
            return new_hist
        pts = new_hist.get('usdt_usdc', [])
        pts.append([now_ts, v])
        new_hist['usdt_usdc'] = pts[-MAX_HISTORY_POINTS:]
    except Exception:
        pass
    return new_hist


def usdt_usdc_changes(hist, now_ts):
    """{'usdt_usdc_chg_7d_pct', 'usdt_usdc_chg_24h_pct'} from the
    local store. None while warming up (24h change needs a day of
    points, 7d change needs a week) — the cells are 'no data', never
    a guess."""
    try:
        pts = (hist or {}).get('usdt_usdc')
        series = [(int(ts), float(v)) for ts, v in (pts or [])]
        chg_7d = _change_pct(series, now_ts, 7 * DAY_SECS)
        chg_24h = _change_pct(series, now_ts, DAY_SECS)
        return {'usdt_usdc_chg_7d_pct': (round(chg_7d, 2)
                                         if chg_7d is not None else None),
                'usdt_usdc_chg_24h_pct': (round(chg_24h, 2)
                                          if chg_24h is not None else None)}
    except Exception:
        return {'usdt_usdc_chg_7d_pct': None,
                'usdt_usdc_chg_24h_pct': None}


# ---------------------------------------------------------------------------
# Fetches — fail-open, never raise
# ---------------------------------------------------------------------------

def fetch_current(fetch_json):
    """This run's current values: stablecoin supplies + DeFi TVL.
    Two small calls. Returns a dict with None fields for anything
    missing, plus 'error' (None when at least one leg landed).
    Never raises."""
    row = {'total_usd': None, 'usdt': None, 'usdc': None,
           'usdt_usdc': None, 'dominance': None, 'stable_count': None,
           'tvl_total': None, 'top_chains': [], 'error': None}
    try:
        stables = parse_stablecoins(fetch_json(STABLECOINS_URL))
    except Exception as e:
        stables = None
        row['error'] = f'stablecoins: {str(e)[:80]}'
    if stables:
        row.update({'total_usd': stables['total_usd'],
                    'usdt': stables['usdt'],
                    'usdc': stables['usdc'],
                    'usdt_usdc': stables['usdt_usdc'],
                    'dominance': stables['dominance'],
                    'stable_count': stables['count']})
    try:
        chains = parse_chains(fetch_json(CHAINS_URL))
    except Exception as e:
        chains = None
        if row['error'] is None:
            row['error'] = f'chains: {str(e)[:80]}'
    if chains:
        row.update({'tvl_total': chains['tvl_total'],
                    'top_chains': chains['chains']})
    if row['total_usd'] is None and row['tvl_total'] is None \
            and row['error'] is None:
        row['error'] = 'all DeFiLlama legs failed'
    return row


def fetch_daily_changes(fetch_json, now_ts=None):
    """7d/24h changes from the two heavy history endpoints
    (stablecoin charts + TVL history). Called at most once a day by
    the caller — these series are ~3,300 daily points each. Returns
    {'total_chg_7d_pct', 'total_chg_24h_pct', 'tvl_chg_7d_pct',
    'tvl_chg_24h_pct', 'error'}. Never raises. now_ts is injectable
    for tests (defaults to wall-clock time); the fake series in the
    tests is pinned to 2026-11-07, so the test passes its own NOW —
    without it the 7d leg goes stale once real time drifts past the
    fake series end + slack (first failed 2026-11-14)."""
    out = {'total_chg_7d_pct': None, 'total_chg_24h_pct': None,
           'tvl_chg_7d_pct': None, 'tvl_chg_24h_pct': None,
           'error': None}
    now_ts = now_ts if now_ts is not None else time.time()
    try:
        series = parse_stablecoin_charts(fetch_json(STABLECOIN_CHARTS_URL))
        out.update(supply_changes(series, now_ts))
    except Exception as e:
        out['error'] = f'stablecoin charts: {str(e)[:80]}'
    try:
        series = parse_tvl_history(fetch_json(TVL_HISTORY_URL))
        out.update(tvl_changes(series, now_ts))
    except Exception as e:
        if out['error'] is None:
            out['error'] = f'tvl history: {str(e)[:80]}'
    if all(out[k] is None for k in ('total_chg_7d_pct',
                                    'total_chg_24h_pct', 'tvl_chg_7d_pct',
                                    'tvl_chg_24h_pct')) \
            and out['error'] is None:
        out['error'] = 'all history legs failed'
    return out


def fetch_all(fetch_json, now_ts=None):
    """One-shot convenience for tests/smoke: current values + daily
    changes + USDT+USDC changes from an EMPTY local store (so the
    USDT+USDC changes read None — warming up — unless the caller
    passes a store via fetch_snapshot). Never raises."""
    return fetch_snapshot(fetch_json, {}, now_ts)


def fetch_snapshot(fetch_json, supply_hist, now_ts=None):
    """Full snapshot: current values, daily changes (history
    endpoints), and USDT+USDC changes from the local store.
    Returns {'current': {...}, 'daily': {...}, 'usdt_usdc': {...},
    'history': <updated store>}. The history endpoints make this
    the expensive call — the intraday loop calls fetch_current
    every run and fetch_daily_changes once a day instead. Never
    raises."""
    now_ts = now_ts if now_ts is not None else time.time()
    try:
        cur = fetch_current(fetch_json)
    except Exception as e:
        cur = {'total_usd': None, 'usdt': None, 'usdc': None,
               'usdt_usdc': None, 'dominance': None,
               'stable_count': None, 'tvl_total': None,
               'top_chains': [], 'error': str(e)[:120]}
    try:
        daily = fetch_daily_changes(fetch_json, now_ts)
    except Exception as e:
        daily = {'total_chg_7d_pct': None, 'total_chg_24h_pct': None,
                 'tvl_chg_7d_pct': None, 'tvl_chg_24h_pct': None,
                 'error': str(e)[:120]}
    try:
        hist = update_supply_history(supply_hist, cur.get('usdt_usdc'),
                                     now_ts)
        usdt_usdc = usdt_usdc_changes(hist, now_ts)
    except Exception:
        hist = supply_hist if isinstance(supply_hist, dict) else {}
        usdt_usdc = {'usdt_usdc_chg_7d_pct': None,
                     'usdt_usdc_chg_24h_pct': None}
    return {'current': cur, 'daily': daily, 'usdt_usdc': usdt_usdc,
            'history': hist}


def pulse_line(snapshot):
    """One plain-words line for the run log, e.g. "DeFiLlama:
    stables $292.3B (+0.8%/7d), USDT+USDC 84.1%, TVL $81.0B
    (-1.2%/7d)". Missing fields read "no data". Pure — never
    raises."""
    try:
        snap = snapshot or {}
        cur = snap.get('current') or snap
        daily = snap.get('daily') or {}
        parts = []
        if cur.get('total_usd') is not None:
            parts.append(f"stables ${cur['total_usd'] / 1e9:.1f}B")
            if daily.get('total_chg_7d_pct') is not None:
                parts.append(f"({daily['total_chg_7d_pct']:+.1f}%/7d)")
        else:
            parts.append('stables no data')
        if cur.get('dominance') is not None:
            parts.append(f"USDT+USDC {cur['dominance'] * 100:.1f}%")
        if cur.get('tvl_total') is not None:
            parts.append(f"TVL ${cur['tvl_total'] / 1e9:.1f}B")
            if daily.get('tvl_chg_7d_pct') is not None:
                parts.append(f"({daily['tvl_chg_7d_pct']:+.1f}%/7d)")
        return 'DeFiLlama: ' + ' '.join(parts)
    except Exception:
        return 'DeFiLlama no data'


# ---------------------------------------------------------------------------
# Live smoke test: python3 Scripts/defillama_feed.py
# ---------------------------------------------------------------------------

def _http_json(url, timeout=60):
    import json
    import urllib.request
    req = urllib.request.Request(
        url, headers={'User-Agent': 'NexIntel-DeFiLlamaFeed/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def smoke_main():
    """Fetch live and print the values."""
    import json as _json
    now = time.time()
    snap = fetch_snapshot(_http_json, {}, now)
    print('  current: ' + _json.dumps(snap['current']))
    print('  daily: ' + _json.dumps(snap['daily']))
    print('  usdt_usdc: ' + _json.dumps(snap['usdt_usdc'])
          + ' (warming up — empty store)')
    print('  ' + pulse_line(snap))


if __name__ == '__main__':
    smoke_main()
