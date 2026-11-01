"""Econ feature pack — milestone 2 (Nic 2026-10-31, approved build).

Candidate variables for the NexIntel significance ranking ("Significance is
predictability based on variables used for an outcome"):
  game theory / positioning — stop_dist, squeeze_risk, crowded
  supply / demand zones    — supply_dist, demand_dist, at_supply, at_demand
  predecessors (leading)  — fund_mom, oi_mom, coil_delta
  macro / monetary        — real_yield, fed_stance, liq_regime

DISPLAY + GRADING ONLY. Nothing here feeds a buy gate, alert threshold,
verdict, or live scoring weight. Every function fails open: missing inputs
-> None (recorded as JSON null; the ranking treats missing as 0.0 and labels
never-observed variables "awaiting data" rather than "dead weight").

Variable order is FIXED (ECON_FEATURE_NAMES). compute_econ(ctx) returns
(values, display) with values aligned to ECON_FEATURE_NAMES.

Computation modes (what the caller has on hand):
  'full'   — hourly closes + volumes (intraday signals): volume-weighted
             profile zones, swing + round-number stop clusters.
  'tpo'    — closes only, no volumes (movers/sparkline): time-at-price
             (unweighted) zones, swing + round-number clusters.
  'coarse' — price + 5-window percent changes only (daily engine): round-number
             clusters, window-reconstructed swing levels. Documented as coarse.
"""

import math

ECON_FEATURE_NAMES = [
    'stop_dist', 'squeeze_risk', 'crowded',
    'supply_dist', 'demand_dist', 'at_supply', 'at_demand',
    'fund_mom', 'oi_mom', 'coil_delta',
    'real_yield', 'fed_stance', 'liq_regime',
]
N_ECON = len(ECON_FEATURE_NAMES)

# In vol-unit space: how close price must be to count as "at" a zone/cluster.
AT_ZONE_VOL = 0.5
SQUEEZE_VOL = 0.25


def _clip(x, lo, hi):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, x))


def _rms_vol_pct(closes):
    """Hourly volatility: RMS of hourly returns in %, floored at 0.3 — the
    same math as hourly_features() in intraday.py."""
    if not closes or len(closes) < 2:
        return None
    rets = []
    for i in range(1, len(closes)):
        a, b = closes[i - 1], closes[i]
        if a and b:
            rets.append((b - a) / a * 100)
    if not rets:
        return None
    return max((sum(r * r for r in rets) / len(rets)) ** 0.5, 0.3)


# ------------------------------------------------------------ stop clusters
def round_number_levels(price):
    """Psychological round prices: multiples of 10^(digits-1), +/-3 steps.
    Stops cluster at round numbers (game theory: the crowd places them where
    everyone else does). Needs price only — works in every mode."""
    try:
        price = float(price)
    except (TypeError, ValueError):
        return []
    if price <= 0:
        return []
    mag = 10 ** math.floor(math.log10(price) - 1)
    base = round(price / mag) * mag
    return [base + k * mag for k in range(-3, 4) if base + k * mag > 0]


def swing_levels(closes, vol):
    """Local swing highs/lows (window +/-2). Levels touched 2+ times within
    0.3 vol are stop magnets: equal highs = buy-stop cluster above, equal
    lows = sell-stop cluster below."""
    if not closes or len(closes) < 7 or not vol or vol <= 0:
        return []
    n = len(closes)
    swings = []
    for i in range(2, n - 2):
        c = closes[i]
        if not c:
            continue
        win = closes[i - 2:i + 3]
        if all(w for w in win):
            if c == max(win) or c == min(win):
                swings.append(c)
    tol = 0.3 * vol / 100.0  # vol is in %; tol as price fraction
    clusters = []
    for s in swings:
        placed = False
        for cl in clusters:
            if abs(s - cl[0]) / cl[0] <= tol:
                cl[1] += 1
                placed = True
                break
        if not placed:
            clusters.append([s, 1])
    return sorted(cl[0] for cl in clusters if cl[1] >= 2)


def stop_cluster_levels(price, closes=None, vol=None):
    """Union of round-number levels and equal-swing clusters."""
    levels = set(round_number_levels(price))
    for s in swing_levels(closes or [], vol or 0):
        levels.add(s)
    return sorted(levels)


def stop_distance(price, closes=None, vol=None):
    """(dist_vol_units, inside_flag). Distance from price to the nearest stop
    cluster in vol units; inside_flag=1 when price sits in the cluster zone
    (<=0.25 vol) — squeeze risk: resting stops are fuel for a fast move."""
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None, 0.0
    if price <= 0 or not vol or vol <= 0:
        return None, 0.0  # no vol unit -> distance undefined (fail open)
    levels = stop_cluster_levels(price, closes, vol)
    if not levels:
        return None, 0.0
    d_pct = min(abs(price - L) / price * 100 for L in levels)
    d_vol = d_pct / vol
    return _clip(d_vol, 0, 5), 1.0 if d_vol <= SQUEEZE_VOL else 0.0


# ------------------------------------------------------- supply/demand zones
def _profile_zones(closes, weights, price, vol):
    """Price levels where the market spent the most volume (full mode) or
    time (tpo mode): auction-market acceptance zones. Returns
    (supply_dist, demand_dist, at_supply, at_demand) in vol units; distances
    None when no zone exists on that side."""
    if not closes or len(closes) < 8 or not vol or vol <= 0:
        return None, None, 0.0, 0.0
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None, None, 0.0, 0.0
    lo, hi = min(closes), max(closes)
    if hi <= lo or price <= 0:
        return None, None, 0.0, 0.0
    n_bins = 10
    wts = weights if weights and len(weights) == len(closes) else [1.0] * len(closes)
    bins = [0.0] * n_bins
    for c, w in zip(closes, wts):
        if not c:
            continue
        b = min(n_bins - 1, int((c - lo) / (hi - lo) * n_bins))
        bins[b] += max(float(w or 0), 0.0)
    zones = []
    for i in range(n_bins):
        left = bins[i - 1] if i > 0 else -1.0
        right = bins[i + 1] if i < n_bins - 1 else -1.0
        if bins[i] > left and bins[i] > right and bins[i] > 0:
            zones.append(lo + (i + 0.5) / n_bins * (hi - lo))
    above = [z for z in zones if z > price]
    below = [z for z in zones if z < price]
    supply = min(above) if above else None
    demand = max(below) if below else None

    def to_vol(z):
        return _clip(abs(z - price) / price * 100 / vol, 0, 5)

    s_d = to_vol(supply) if supply else None
    d_d = to_vol(demand) if demand else None
    return (s_d, d_d,
            1.0 if s_d is not None and s_d <= AT_ZONE_VOL else 0.0,
            1.0 if d_d is not None and d_d <= AT_ZONE_VOL else 0.0)


def volume_profile_zones(closes, volumes, price, vol):
    """Volume-weighted acceptance zones (full mode, hourly data)."""
    return _profile_zones(closes, volumes, price, vol)


def tpo_zones(closes, price, vol):
    """Time-at-price zones (tpo mode): each hourly close weighted equally.
    Where price *spent time* is where it was accepted — same auction logic,
    no volume feed needed."""
    return _profile_zones(closes, None, price, vol)


def coarse_zones(price, windows, vol):
    """Coarse supply/demand from the 5 nested windows (daily engine): rebuild
    approximate past levels, take the swing high/low as zone proxies.
    Documented as coarse — the ranking will say whether even this predicts."""
    try:
        price = float(price)
        if price <= 0 or not vol or vol <= 0 or not windows:
            return None, None, 0.0, 0.0
        levels = []
        for k in ('r1h', 'r24h', 'r7d', 'r14d', 'r30d'):
            r = windows.get(k)
            if r is None:
                continue
            levels.append(price / (1 + float(r) / 100.0))
        if len(levels) < 2:
            return None, None, 0.0, 0.0
        swing_hi, swing_lo = max(levels), min(levels)

        def to_vol(z):
            return _clip(abs(z - price) / price * 100 / vol, 0, 5)

        s_d = to_vol(swing_hi) if swing_hi > price else None
        d_d = to_vol(swing_lo) if swing_lo < price else None
        return (s_d, d_d,
                1.0 if s_d is not None and s_d <= AT_ZONE_VOL else 0.0,
                1.0 if d_d is not None and d_d <= AT_ZONE_VOL else 0.0)
    except (TypeError, ValueError, ZeroDivisionError):
        return None, None, 0.0, 0.0


# ------------------------------------------------------------- predecessors
def _nearest_at_or_before(pts, target_ts):
    """Latest (ts, value) with ts <= target_ts. Points must be ascending."""
    best = None
    for ts, v in pts or []:
        if ts <= target_ts:
            best = (ts, v)
        else:
            break
    return best


def history_delta(hist, asset, now_ts, lookback_secs, max_gap_secs=7200):
    """Score now minus score `lookback_secs` ago from a {asset: [[ts, val]]}
    history. None when the window can't be built (too few points, stale gap).
    Generic predecessor primitive: funding_momentum uses it."""
    try:
        pts = (hist or {}).get(asset)
        if not pts or len(pts) < 2 or not now_ts:
            return None
        now_ts = float(now_ts)
        latest_ts, latest_v = pts[-1][0], pts[-1][1]
        base = _nearest_at_or_before(pts[:-1], now_ts - lookback_secs)
        if base is None:
            return None
        base_ts, base_v = base
        if now_ts - base_ts > lookback_secs + max_gap_secs:
            return None  # stale — a gap, not a trend
        return _clip(float(latest_v) - float(base_v), -1.5, 1.5)
    except (TypeError, ValueError, IndexError):
        return None


def oi_acceleration(oi_hist, asset, now_ts):
    """OI inflow *acceleration*: 1h OI-rate now minus 1h OI-rate 3h ago,
    tanh-scaled to [-1,1]. Why it leads: rising inflow = fresh fuel arriving
    (continuation ahead); collapsing inflow = the move is running on fumes
    (exhaustion ahead). Needs no price data — pure positioning flow."""
    try:
        pts = (oi_hist or {}).get(asset)
        if not pts or len(pts) < 4 or not now_ts:
            return None
        now_ts = float(now_ts)
        p_now = pts[-1]
        p_1h = _nearest_at_or_before(pts[:-1], now_ts - 3600)
        p_3h = _nearest_at_or_before(pts[:-1], now_ts - 3 * 3600)
        p_4h = _nearest_at_or_before(pts[:-1], now_ts - 4 * 3600)
        if None in (p_1h, p_3h, p_4h):
            return None
        if now_ts - p_1h[0] > 3600 + 1800:
            return None
        if not p_1h[1] or not p_4h[1]:
            return None
        r1 = (p_now[1] - p_1h[1]) / p_1h[1] * 100
        r0 = (p_3h[1] - p_4h[1]) / p_4h[1] * 100
        return round(math.tanh((r1 - r0) / 15.0), 3)
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        return None


# ------------------------------------------------------- macro / monetary
# FRED's official API needs a key Nic doesn't have. The keyless public chart
# CSV is the same endpoint the Economy page fetches in-browser (economy.js),
# so reusing it here adds no new credential and no new vendor.
FRED_SERIES = ['DFF', 'DGS10', 'CPIAUCSL']
FRED_URL = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id='


def parse_fred_csv(text):
    """Parse FRED chart CSV into [(date_str, value)]. '.' = missing."""
    out = []
    for ln in (text or '').strip().splitlines()[1:]:
        parts = ln.split(',')
        if len(parts) < 2:
            continue
        try:
            out.append((parts[0].strip(), float(parts[1])))
        except ValueError:
            continue
    return out


def fetch_fred_csv(series_id, timeout=8):
    """Keyless FRED chart CSV. [] on any failure (fail open)."""
    try:
        import urllib.request
        req = urllib.request.Request(FRED_URL + series_id,
                                     headers={'User-Agent': 'NexIntel/1.0'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return parse_fred_csv(r.read().decode('utf-8', errors='replace'))
    except Exception:
        return []


def _val_near_date(obs, target_date, max_days=10):
    """Value with observation date nearest (at or before) target_date."""
    best = None
    for d, v in obs:
        if d <= target_date and (best is None or d > best[0]):
            best = (d, v)
    return best[1] if best else None


def refresh_macro_snapshot(prior):
    """Rebuild the monetary snapshot from keyless FRED CSVs. Fail-open: any
    fetch/parse failure returns `prior` unchanged (possibly None). Values are
    slow-moving; the caller caches by day."""
    try:
        from datetime import date, timedelta
        obs = {}
        for sid in FRED_SERIES:
            o = fetch_fred_csv(sid)
            if len(o) < 30:
                return prior
            obs[sid] = o
        today = date.today().isoformat()
        dff = obs['DFF']
        d90 = (date.fromisoformat(dff[-1][0]) - timedelta(days=90)).isoformat()
        cpi = obs['CPIAUCSL']
        yoy = None
        if len(cpi) >= 13:
            yoy = (cpi[-1][1] / cpi[-13][1] - 1) * 100
        return {
            'asof': today,
            'DFF': {'value': dff[-1][1], 'date': dff[-1][0],
                    'd90': (dff[-1][1] - _val_near_date(dff, d90))
                           if _val_near_date(dff, d90) is not None else None},
            'DGS10': {'value': obs['DGS10'][-1][1], 'date': obs['DGS10'][-1][0]},
            'CPIAUCSL': {'value': cpi[-1][1], 'date': cpi[-1][0],
                         'yoy': round(yoy, 2) if yoy is not None else None},
        }
    except Exception:
        return prior


def monetary_from_snapshot(snap):
    """(real_yield, fed_stance) from a macro snapshot. real_yield = 10Y minus
    CPI YoY (the true hurdle for risk assets); fed_stance = Fed funds change
    over ~90d (+ = tightening, - = easing). (None, None) when unavailable."""
    try:
        s = snap or {}
        dgs10 = (s.get('DGS10') or {}).get('value')
        yoy = (s.get('CPIAUCSL') or {}).get('yoy')
        dff = s.get('DFF') or {}
        real_yield = _clip((dgs10 - yoy) / 3.0, -3, 3) \
            if dgs10 is not None and yoy is not None else None
        fed_stance = _clip(dff.get('d90') / 1.0, -2, 2) \
            if dff.get('d90') is not None else None
        return real_yield, fed_stance
    except (TypeError, ValueError):
        return None, None


def liquidity_regime(btc_chg24=None, mean_abs_funding=None, fg_now=None):
    """Coarse risk-liquidity tag [-1,1] from data already on hand (+1 risk-on):
    BTC 24h trend (crypto's liquidity barometer) minus funding crowdedness
    (crowded = late-cycle) plus fear/greed tilt. A proxy, documented as such —
    not a structural model."""
    try:
        parts = 0
        reg = 0.0
        if btc_chg24 is not None:
            reg += 0.5 * _clip(btc_chg24 / 10.0, -1, 1)
            parts += 1
        if mean_abs_funding is not None:
            reg += -0.3 * _clip(mean_abs_funding, 0, 1)
            parts += 1
        if fg_now is not None:
            reg += 0.2 * _clip((fg_now - 50) / 50.0, -1, 1)
            parts += 1
        if not parts:
            return None
        return round(_clip(reg, -1, 1), 3)
    except (TypeError, ValueError):
        return None


def crowdedness(funding_score):
    """|funding_score| in [0,1]: how crowded positioning is. Extreme funding
    = the crowd is already all-in (contrarian read: fragile continuation).
    Reuses Feed #2's score — no new data."""
    if funding_score is None:
        return None
    return _clip(abs(funding_score), 0, 1)


# ------------------------------------------------------------------ driver
def compute_econ(ctx):
    """ctx keys (all optional; missing -> that variable is None):
      mode: 'full' | 'tpo' | 'coarse'
      price, closes, volumes, windows {r1h,r24h,r7d,r14d,r30d},
      asset, funding_score, fund_hist, oi_hist, chg_1h, now_ts,
      coil_delta (precomputed by caller via the engine's balance_score),
      macro_snap, btc_chg24, fg_now.
    Returns (values[13] aligned to ECON_FEATURE_NAMES,
             display {'numbers': {...}, 'tags': [...]}).

    Predecessor logic (why these LEAD price):
      fund_mom  — positioning shifts precede price: the crowd adds/removes
                  leverage before the move resolves.
      oi_mom    — OI inflow acceleration precedes continuation; collapsing
                  inflow precedes exhaustion (the fuel gauge).
      coil_delta— auction theory: balance (compression) precedes imbalance
                  (expansion); a change in compression state leads the next
                  directional move.
    """
    ctx = ctx or {}
    mode = ctx.get('mode') or 'full'
    price = ctx.get('price')
    closes = ctx.get('closes')
    volumes = ctx.get('volumes')
    vol = _rms_vol_pct(closes) if closes else None

    # --- game theory / positioning ---
    # Coarse mode has no hourly series: vol proxy from the 24h window, round
    # numbers + (no swings available) for clusters.
    vol_c = None
    if mode == 'coarse':
        _win = ctx.get('windows') or {}
        try:
            vol_c = max(abs(float(_win.get('r24h') or 0)) / 10.0, 0.3)
        except (TypeError, ValueError):
            vol_c = None
    if mode == 'coarse' or not closes:
        sd, sq = stop_distance(price, None, vol_c)
    else:
        sd, sq = stop_distance(price, closes, vol)
    crowd = crowdedness(ctx.get('funding_score'))

    # --- supply / demand ---
    if mode == 'full' and closes:
        s_d, d_d, at_s, at_d = volume_profile_zones(closes, volumes, price, vol)
    elif mode == 'tpo' and closes:
        s_d, d_d, at_s, at_d = tpo_zones(closes, price, vol)
    else:
        win = ctx.get('windows')
        vOLD = max(abs((win or {}).get('r24h') or 0) / 10.0, 0.3) if win else None
        s_d, d_d, at_s, at_d = coarse_zones(price, win, vOLD)

    # --- predecessors ---
    asset = ctx.get('asset')
    now_ts = ctx.get('now_ts')
    f_mom = history_delta(ctx.get('fund_hist'), asset, now_ts, 6 * 3600)
    o_mom = oi_acceleration(ctx.get('oi_hist'), asset, now_ts)
    coil = ctx.get('coil_delta')
    coil = round(float(coil), 3) if coil is not None else None

    # --- macro / monetary ---
    real_y, fed_st = monetary_from_snapshot(ctx.get('macro_snap'))
    fund_hist = ctx.get('fund_hist') or {}
    scores = []
    for _a, pts in fund_hist.items():
        if pts:
            try:
                scores.append(abs(float(pts[-1][1])))
            except (TypeError, ValueError, IndexError):
                pass
    mean_crowd = sum(scores) / len(scores) if scores else None
    liq = liquidity_regime(ctx.get('btc_chg24'), mean_crowd, ctx.get('fg_now'))

    values = [sd, sq, crowd, s_d, d_d, at_s, at_d,
              f_mom, o_mom, coil, real_y, fed_st, liq]

    numbers = {n: (round(v, 3) if isinstance(v, float) else v)
               for n, v in zip(ECON_FEATURE_NAMES, values)}
    tags = []
    if sq:
        tags.append('Squeeze risk')
    if at_d:
        tags.append('At demand')
    if at_s:
        tags.append('At supply')
    if crowd is not None and crowd >= 0.6:
        tags.append('Funding crowded')
    if coil is not None and coil >= 0.25:
        tags.append('Coiling up')
    elif coil is not None and coil <= -0.25:
        tags.append('Uncoiling')
    if liq is not None and liq >= 0.4:
        tags.append('Risk-on')
    elif liq is not None and liq <= -0.4:
        tags.append('Risk-off')
    return values, {'numbers': numbers, 'tags': tags}
