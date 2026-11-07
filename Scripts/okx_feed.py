"""OKX derivatives feed — free data-edge track #1 (2026-11-07,
~/workspace/goals/build-out-omnisight/hidden_files/
free-data-edge-dive-2026-11-07.md).

DISPLAY-ONLY + SHADOW. This module never touches a buy gate: it does
not import, call, or consult gate_math.py, gate_circuit.py, or the
60% live bar. OKX readings are annotations, never inputs. No
alert-tier output, no confidence influence, no trades.

Why OKX: Binance's futures API 451s and Bybit's 403s from this US
runtime, so OKX is the working derivatives venue. All three endpoints
below are keyless public REST — no sign-up, no key, no payment.

Endpoints (all GET, keyless):
  * Funding rate (current, unsettled):
    /api/v5/public/funding-rate?instId={COIN}-USDT-SWAP
    OKX settles funding every 8h (Kraken: 4h), so the annualized %
    is rate * 3 * 365 * 100. The crowdedness score uses the SAME
    baseline/scale as funding.py (15% baseline, 40% scale) so OKX
    and Kraken scores are directly comparable.
  * Funding history (settled rates, newest first):
    /api/v5/public/funding-rate-history?instId={COIN}-USDT-SWAP
    &limit=100 — ~33 days of 8h rates. The funding z-score is
    computed against this history (mean/std), so there is no warm-up:
    a coin with <10 history rows reads z = None ("no data").
  * Open interest:
    /api/v5/public/open-interest?instType=SWAP&instId={COIN}-USDT-SWAP
    OI 1h/24h changes ride the cross-run history store in
    intraday.json['okx_oi_history'] — the same persistence trick as
    oi_flow (last 30 hourly points per coin). OKX has no simple
    public OI-history endpoint, so the store warms up over the first
    day: OI changes read None until two hourly points exist.
  * Long/short account ratio (crowded-long proxy):
    /api/v5/rubik/stat/contracts/long-short-account-ratio?ccy={COIN}
    &period=5m
    PARAMETER FIX (probe 2026-11-07): the rubik endpoint takes
    `ccy` (the currency, e.g. BTC), NOT `instId` — the probe's
    instId call 400'd with "ccy can't be empty". The ratio is the
    account long/short ratio across OKX contracts for that currency,
    returned as [timestamp_ms, ratio] rows, newest first.

Funding dispersion (cross-venue crowding): max minus min annualized
funding across OKX + Kraken (funding.py) per coin. Hyperliquid is
not wired yet, so dispersion is OKX+Kraken only — noted honestly
where it is displayed.

Every fetch is fail-open: a dead endpoint, a 429, a non-"0" OKX
code, or bad JSON degrades that coin (or that field) to "no data",
never an exception, never a blocked run. Calls are paced (0.12s)
to stay under OKX's public rate limit (20 req / 2s).
"""

import math
import time

BASE_URL = 'https://www.okx.com/api/v5'
FUNDING_URL = BASE_URL + '/public/funding-rate?instId={inst}'
FUNDING_HIST_URL = (BASE_URL + '/public/funding-rate-history'
                    '?instId={inst}&limit={limit}')
OI_URL = (BASE_URL + '/public/open-interest'
          '?instType=SWAP&instId={inst}')
LS_URL = (BASE_URL + '/rubik/stat/contracts/long-short-account-ratio'
          '?ccy={ccy}&period=5m')

MAX_COINS_PER_RUN = 20   # bounded universe per run (same shape as pattern_feed)
CORE_COINS = ['BTC', 'ETH', 'SOL', 'XRP']  # always fetched, even when
# no card carries them — the dispersion anchor (BTC) and the majors
# must never be missing from a run just because the card universe
# was quiet small-caps that day.
HISTORY_LIMIT = 100      # ~33 days of 8h funding rates
MIN_HISTORY_FOR_Z = 10   # fewer history rows -> z is None ("no data")
OI_MAX_POINTS = 30       # hourly OI observations kept per coin (~30h)
PACE_S = 0.12            # pacing between OKX calls (limit: 20 req / 2s)

# Crowdedness score scale — SAME as funding.py so OKX and Kraken
# scores are directly comparable (15% annualized = normal, 0 score).
BASELINE_ANN_PCT = 15.0
SCALE_ANN_PCT = 40.0
# OKX settles funding every 8h: 3 intervals/day (Kraken: 4h -> 6/day).
INTERVALS_PER_DAY = 3


def inst_id(asset):
    """Engine asset symbol -> OKX SWAP instrument id, e.g. BTC ->
    BTC-USDT-SWAP. Empty/None -> ''."""
    a = (asset or '').upper().strip()
    return f'{a}-USDT-SWAP' if a else ''


def annualized_pct(rate_8h):
    """Decimal funding rate per 8h interval -> annualized percent."""
    try:
        return float(rate_8h) * INTERVALS_PER_DAY * 365 * 100
    except (TypeError, ValueError):
        return None


def funding_score(ann_pct):
    """[-1, +1] crowdedness score on the funding.py scale. Positive
    funding (crowded longs) is negative for continuation; negative
    funding (crowded shorts) is positive (squeeze-up potential).
    None when the input is missing."""
    if ann_pct is None:
        return None
    try:
        return -math.tanh((float(ann_pct) - BASELINE_ANN_PCT)
                           / SCALE_ANN_PCT)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Parsers — pure, never raise, None / {} on bad data
# ---------------------------------------------------------------------------

def _first_data_row(data):
    """The first row of an OKX response's data list, or None. An OKX
    error (code != '0') or an empty list is "no data", not an error."""
    try:
        if not isinstance(data, dict):
            return None
        if str(data.get('code')) != '0':
            return None
        rows = data.get('data')
        if not isinstance(rows, list) or not rows:
            return None
        return rows[0]
    except Exception:
        return None


def parse_funding(data):
    """{'rate', 'ann_pct', 'score', 'next_funding_ts'} from a
    /public/funding-rate response, or None."""
    try:
        row = _first_data_row(data)
        if not isinstance(row, dict):
            return None
        rate = float(row.get('fundingRate'))
        ann = annualized_pct(rate)
        nxt = row.get('nextFundingTime')
        try:
            nxt = int(nxt) if nxt not in (None, '') else None
        except (TypeError, ValueError):
            nxt = None
        return {'rate': rate,
                'ann_pct': round(ann, 4) if ann is not None else None,
                'score': (round(funding_score(ann), 3)
                          if ann is not None else None),
                'next_funding_ts': nxt}
    except (TypeError, ValueError, AttributeError):
        return None


def parse_funding_history(data):
    """[rate, ...] settled funding rates, oldest first, from a
    /public/funding-rate-history response. [] on any failure. The
    endpoint returns newest first; rows carry fundingTime (ms)."""
    out = []
    try:
        if not isinstance(data, dict) or str(data.get('code')) != '0':
            return out
        rows = data.get('data')
        if not isinstance(rows, list):
            return out
        stamped = []
        for row in rows:
            try:
                if not isinstance(row, dict):
                    continue
                stamped.append((int(row.get('fundingTime') or 0),
                                float(row.get('fundingRate'))))
            except (TypeError, ValueError):
                continue
        stamped.sort(key=lambda x: x[0])
        out = [r for _, r in stamped]
    except Exception:
        return out
    return out


def funding_z(current_rate, history_rates):
    """Z-score of the current funding rate against its own history
    (population mean/std). None when history is too short (<10 rows)
    or flat (std == 0) — the cell is "no data", never a guess."""
    try:
        hist = [float(r) for r in (history_rates or [])]
        if current_rate is None or len(hist) < MIN_HISTORY_FOR_Z:
            return None
        cur = float(current_rate)
        mean = sum(hist) / len(hist)
        var = sum((r - mean) ** 2 for r in hist) / len(hist)
        std = math.sqrt(var)
        if std <= 0:
            return None
        return round((cur - mean) / std, 2)
    except (TypeError, ValueError):
        return None


def parse_oi(data):
    """{'oi', 'oi_usd'} from a /public/open-interest response
    (oi = contracts, oi_usd = USD notional), or None."""
    try:
        row = _first_data_row(data)
        if not isinstance(row, dict):
            return None
        return {'oi': float(row.get('oi')),
                'oi_usd': float(row.get('oiUsd'))}
    except (TypeError, ValueError, AttributeError):
        return None


def parse_ls(data):
    """{'ratio', 'ts_ms'} from a long-short-account-ratio response
    (rows are [timestamp_ms, ratio], newest first), or None."""
    try:
        if not isinstance(data, dict) or str(data.get('code')) != '0':
            return None
        rows = data.get('data')
        if not isinstance(rows, list) or not rows:
            return None
        first = rows[0]
        if not isinstance(first, (list, tuple)) or len(first) < 2:
            return None
        return {'ratio': round(float(first[1]), 2),
                'ts_ms': int(first[0])}
    except (TypeError, ValueError, IndexError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Fetches — fail-open, never raise
# ---------------------------------------------------------------------------

def fetch_coin(fetch_json, asset, pace_s=PACE_S):
    """One coin's OKX readings: funding (+z vs history), OI, and
    long/short ratio. Each endpoint is independent — a failed L/S
    call drops only the ratio, never the funding. Returns a dict
    with None fields for anything missing, plus 'error' (None when
    at least the funding leg landed). Never raises."""
    row = {'asset': (asset or '').upper(),
           'funding_rate': None, 'ann_pct': None, 'funding_score': None,
           'funding_z': None, 'oi': None, 'oi_usd': None,
           'ls_ratio': None, 'error': None}
    inst = inst_id(asset)
    if not inst:
        row['error'] = 'no instrument'
        return row
    try:
        fund = parse_funding(fetch_json(FUNDING_URL.format(inst=inst)))
    except Exception as e:
        fund = None
        row['error'] = f'funding: {str(e)[:80]}'
    if fund:
        row.update({'funding_rate': fund['rate'],
                    'ann_pct': fund['ann_pct'],
                    'funding_score': fund['score']})
    if pace_s:
        time.sleep(pace_s)
    try:
        hist = parse_funding_history(
            fetch_json(FUNDING_HIST_URL.format(inst=inst,
                                               limit=HISTORY_LIMIT)))
        row['funding_z'] = funding_z(row['funding_rate'], hist)
    except Exception:
        pass
    if pace_s:
        time.sleep(pace_s)
    try:
        oi = parse_oi(fetch_json(OI_URL.format(inst=inst)))
    except Exception:
        oi = None
    if oi:
        row.update({'oi': oi['oi'], 'oi_usd': oi['oi_usd']})
    if pace_s:
        time.sleep(pace_s)
    try:
        ls = parse_ls(fetch_json(LS_URL.format(ccy=row['asset'])))
    except Exception:
        ls = None
    if ls:
        row['ls_ratio'] = ls['ratio']
    if row['funding_rate'] is None and row['oi_usd'] is None \
            and row['ls_ratio'] is None and row['error'] is None:
        row['error'] = 'all OKX legs failed'
    return row


def fetch_all(fetch_json, assets, pace_s=PACE_S):
    """{ASSET: row} for the core coins (always) plus the requested
    assets, up to MAX_COINS_PER_RUN total (order kept, BTC first —
    the dispersion anchor). A failed coin still gets a row (all None
    + error) so the diag counts honestly. Never raises."""
    try:
        syms = []
        for a in list(CORE_COINS) + list(assets or []):
            s = (a or '').upper().strip()
            if s and s not in syms:
                syms.append(s)
        if 'BTC' in syms:
            syms.remove('BTC')
            syms.insert(0, 'BTC')
        out = {}
        for s in syms[:MAX_COINS_PER_RUN]:
            try:
                out[s] = fetch_coin(fetch_json, s, pace_s=pace_s)
            except Exception as e:
                out[s] = {'asset': s, 'funding_rate': None,
                          'ann_pct': None, 'funding_score': None,
                          'funding_z': None, 'oi': None, 'oi_usd': None,
                          'ls_ratio': None, 'error': str(e)[:120]}
        return out
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# OI history store (same persistence trick as oi_flow) + changes
# ---------------------------------------------------------------------------

def update_oi_history(hist, okx_map, now_ts):
    """Fold this run's OI-USD readings into history. hist: {ASSET:
    [[ts, oi_usd], ...]}. Returns a NEW dict; symbols with no OI
    reading are left untouched. Last OI_MAX_POINTS points kept."""
    new_hist = {a: list(pts) for a, pts in (hist or {}).items()}
    for asset, row in (okx_map or {}).items():
        oi = (row or {}).get('oi_usd')
        if oi is None:
            continue
        try:
            oi = float(oi)
        except (TypeError, ValueError):
            continue
        pts = new_hist.get(asset, [])
        pts.append([now_ts, oi])
        new_hist[asset] = pts[-OI_MAX_POINTS:]
    return new_hist


def _change_pct(hist_pts, now_ts, lookback_secs):
    """% change from the reading nearest `lookback_secs` ago to the
    latest. None when the window can't be built (too few points, gap,
    zero base)."""
    if not hist_pts or len(hist_pts) < 2:
        return None
    latest_ts, latest_oi = hist_pts[-1]
    target = now_ts - lookback_secs
    base = None
    for ts, oi in reversed(hist_pts[:-1]):
        if ts <= target:
            base = (ts, oi)
            break
    if base is None:
        return None
    base_ts, base_oi = base
    if now_ts - base_ts > lookback_secs + 3600:  # stale — a gap, not a trend
        return None
    if not base_oi:
        return None
    return (latest_oi - base_oi) / base_oi * 100


def oi_changes(asset, hist, now_ts):
    """{'oi_chg_1h_pct', 'oi_chg_24h_pct'} for one asset, or None
    when the 1h window can't be built (store still warming up)."""
    try:
        pts = (hist or {}).get(asset)
        oi_1h = _change_pct(pts, now_ts, 3600)
        if oi_1h is None:
            return None
        oi_24h = _change_pct(pts, now_ts, 24 * 3600)
        return {'oi_chg_1h_pct': round(oi_1h, 2),
                'oi_chg_24h_pct': (round(oi_24h, 2)
                                   if oi_24h is not None else None)}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Funding dispersion — cross-venue crowding (OKX + Kraken)
# ---------------------------------------------------------------------------

def dispersion_for(okx_ann_pct, kraken_ann_pct):
    """|OKX ann% - Kraken ann%| for one coin — how far apart the two
    venues' funding is. None when either reading is missing.
    Hyperliquid is not wired yet, so dispersion is OKX+Kraken only."""
    try:
        if okx_ann_pct is None or kraken_ann_pct is None:
            return None
        return round(abs(float(okx_ann_pct) - float(kraken_ann_pct)), 2)
    except (TypeError, ValueError):
        return None


def dispersion_map(okx_map, kraken_fund_map):
    """{ASSET: dispersion_pct} for coins present in both maps."""
    out = {}
    try:
        for asset, row in (okx_map or {}).items():
            krow = (kraken_fund_map or {}).get(asset)
            if not krow:
                continue
            d = dispersion_for((row or {}).get('ann_pct'),
                               krow.get('ann_pct'))
            if d is not None:
                out[asset] = d
    except Exception:
        return out
    return out


def pulse_line(okx_map):
    """One plain-words line for the run log, e.g. "OKX: BTC funding
    +0.31%/yr (z +0.4), OI $2.62B, L/S 2.93 · 12 coins". Missing
    fields read "no data". Pure — never raises."""
    try:
        m = okx_map or {}
        if not m:
            return 'OKX no data'
        btc = m.get('BTC') or {}
        parts = []
        if btc.get('ann_pct') is not None:
            parts.append(f"BTC funding {btc['ann_pct']:+.2f}%/yr")
            if btc.get('funding_z') is not None:
                parts.append(f"(z {btc['funding_z']:+.2f})")
        else:
            parts.append('BTC funding no data')
        if btc.get('oi_usd') is not None:
            parts.append(f"OI ${btc['oi_usd'] / 1e9:.2f}B")
        if btc.get('ls_ratio') is not None:
            parts.append(f"L/S {btc['ls_ratio']:.2f}")
        n_ok = sum(1 for r in m.values()
                   if (r or {}).get('funding_rate') is not None)
        parts.append(f"{n_ok}/{len(m)} coins")
        return 'OKX: ' + ' '.join(parts)
    except Exception:
        return 'OKX no data'


# ---------------------------------------------------------------------------
# Live smoke test: python3 Scripts/okx_feed.py
# ---------------------------------------------------------------------------

def _http_json(url, timeout=30):
    import json
    import urllib.request
    req = urllib.request.Request(
        url, headers={'User-Agent': 'NexIntel-OKXFeed/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def smoke_main():
    """Fetch BTC/ETH/SOL/XRP live and print the values."""
    import json as _json
    now = time.time()
    rows = fetch_all(_http_json, ['BTC', 'ETH', 'SOL', 'XRP'],
                     pace_s=PACE_S)
    hist = update_oi_history({}, rows, now)
    for asset, row in rows.items():
        chg = oi_changes(asset, hist, now)
        print(f"  {asset}: funding_rate={row['funding_rate']} "
              f"ann_pct={row['ann_pct']} score={row['funding_score']} "
              f"z={row['funding_z']} oi_usd={row['oi_usd']} "
              f"ls={row['ls_ratio']} oi_chg={chg} "
              f"error={row['error']}")
    print('  ' + pulse_line(rows))
    print('  raw BTC row: ' + _json.dumps(rows.get('BTC') or {}))


if __name__ == '__main__':
    smoke_main()
