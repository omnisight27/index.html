"""FRED keyless CSV feed — free data-edge track #4 (2026-11-07,
~/workspace/goals/build-out-omnisight/hidden_files/
free-data-edge-dive-2026-11-07.md, suggested build order #4 after
OKX / DeFiLlama / Deribit, all built 2026-11-07).

DISPLAY-ONLY + SHADOW. This module never touches a buy gate: it does
not import, call, or consult gate_math.py, gate_circuit.py, or the
60% live bar. Macro readings are annotations, never inputs. No
alert-tier output, no confidence influence, no trades.

Why FRED: the econ pack already carries CPI/GDP/unemployment, but
FRED gives the actual series — and the yield curve + Fed liquidity
are the two macro variables most tied to crypto regime. The cycle
track has no liquidity variable at all; WALCL week-over-week is the
one the cycle spec flags as missing.

Endpoint (GET, keyless, no sign-up — the graph CSV endpoint needs no
API key; only the JSON API does):
  https://fred.stlouisfed.org/graph/fredgraph.csv?id=<series>
  [&cosd=YYYY-MM-DD&coed=YYYY-MM-DD]
The cosd/coed bounds keep each fetch to a few dozen rows: without
them DGS10 returns the full series back to 1962.

Series fetched (one small CSV each, day-cached by the caller —
macro moves slowly, so at most one fetch per series per day):
  T10Y2Y        10y-2y yield curve spread (pp)
  DGS10         10y Treasury yield (%)
  FEDFUNDS      effective federal funds rate (%)
  CPIAUCSL      CPI index (YoY computed in-module -> real_rate_proxy)
  DTWEXBGS      nominal broad dollar index
  WALCL         Fed balance-sheet total assets ($M, weekly)
  BAMLH0A0HYM2  high-yield option-adjusted spread (pp)

Derived variables (the dive doc's proposed set):
  yield_curve_10y2y, real_rate_proxy (DGS10 minus CPI YoY),
  dollar_strength_chg (DTWEXBGS 7d / 30d), fed_liquidity_chg
  (WALCL week-over-week).

Every fetch is fail-open: a dead endpoint, a 429, or bad CSV
degrades that series to \"no data\", never an exception, never a
blocked run. A series whose window can't be built reads None
(\"warming up\"), never a guess. FRED daily series lag publication
by a few days (DGS10's latest was 4 days old at probe time) — the
snapshot records each series' latest date so staleness is visible.
"""

import datetime

BASE_URL = 'https://fred.stlouisfed.org/graph/fredgraph.csv'

# series_id -> (window_days, label). Windows are generous enough for
# the lookbacks each series needs: CPI is monthly and its latest
# print lags ~2 months, so 480 days covers a full YoY window behind
# the latest observation (400 was too short — the 2025-09-01 print
# fell outside the window); WALCL is weekly so 60 days covers ~8
# prints; the daily series need 45 days for a 30-day dollar change.
SERIES = {
    'T10Y2Y': (45, 'yield curve 10y-2y'),
    'DGS10': (45, '10y Treasury'),
    'FEDFUNDS': (60, 'fed funds'),
    'CPIAUCSL': (480, 'CPI'),
    'DTWEXBGS': (45, 'broad dollar'),
    'WALCL': (60, 'Fed balance sheet'),
    'BAMLH0A0HYM2': (45, 'high-yield spread'),
}

MISSING = '.'  # FRED's placeholder for a missing observation


# ---------------------------------------------------------------------------
# Parsers — pure, never raise, [] / None on bad data
# ---------------------------------------------------------------------------

def _as_float(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        v = str(v).strip()
        if v == '' or v == MISSING:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_date(v):
    try:
        return datetime.date.fromisoformat(str(v).strip())
    except (TypeError, ValueError):
        return None


def series_url(series_id, days, today=None):
    """Date-bounded fredgraph.csv URL for the last `days` days."""
    today = today or datetime.date.today()
    start = today - datetime.timedelta(days=days)
    return (f'{BASE_URL}?id={series_id}'
            f'&cosd={start.isoformat()}&coed={today.isoformat()}')


def parse_csv(text):
    """[(date, value), ...] oldest first from a fredgraph.csv body.

    Header is `observation_date,<SERIES>`. Rows with a '.' value or
    an unparseable date are skipped. [] on any failure.
    """
    out = []
    try:
        if not isinstance(text, str) or not text.strip():
            return out
        lines = text.strip().splitlines()
        if len(lines) < 2:
            return out
        for line in lines[1:]:
            parts = line.split(',')
            if len(parts) < 2:
                continue
            d = _as_date(parts[0])
            v = _as_float(parts[1])
            if d is None or v is None:
                continue
            out.append((d, v))
        out.sort(key=lambda x: x[0])
    except Exception:
        return out
    return out


# ---------------------------------------------------------------------------
# Lookbacks — None when the window can't be built, never a guess
# ---------------------------------------------------------------------------

def value_days_ago(series, days, today=None):
    """Value on (or the latest print on/before) `today - days`.

    Returns (date, value) or None when no print is that old yet.
    """
    if not series:
        return None
    today = today or series[-1][0]
    target = today - datetime.timedelta(days=days)
    best = None
    for d, v in series:
        if d <= target:
            best = (d, v)
        else:
            break
    return best


def pct_change(series, days, today=None):
    """% change from the print ~`days` ago to the latest print.

    None when either end is missing. A 7d change on a weekly series
    (WALCL) reads the print ~7 days back, which is the prior week.
    """
    if not series:
        return None
    today = today or series[-1][0]
    old = value_days_ago(series, days, today)
    if old is None:
        return None
    latest = series[-1][1]
    if old[1] == 0:
        return None
    return round((latest - old[1]) / abs(old[1]) * 100.0, 2)


def pp_change(series, days, today=None):
    """Percentage-point change from ~`days` ago to the latest print."""
    if not series:
        return None
    today = today or series[-1][0]
    old = value_days_ago(series, days, today)
    if old is None:
        return None
    return round(series[-1][1] - old[1], 2)


# ---------------------------------------------------------------------------
# Fetch — fail-open, one small CSV per series
# ---------------------------------------------------------------------------

def fetch_series(fetch_text, series_id, days=None):
    """[(date, value), ...] for one series, [] on any failure."""
    try:
        window = days if days is not None else SERIES[series_id][0]
        text = fetch_text(series_url(series_id, window))
        return parse_csv(text)
    except Exception:
        return []


def fetch_all(fetch_text, series_ids=None):
    """{series_id: [(date, value), ...]} for every series.

    A failed series lands as [] (\"no data\"), never an exception.
    """
    out = {}
    for sid in (series_ids or list(SERIES.keys())):
        out[sid] = fetch_series(fetch_text, sid)
    return out


# ---------------------------------------------------------------------------
# Snapshot — the derived, display-only macro state
# ---------------------------------------------------------------------------

def _latest(series):
    if not series:
        return None, None
    d, v = series[-1]
    return d.isoformat(), v


def compute_snapshot(series_map):
    """Display-only macro snapshot from {series_id: series}.

    Every cell is None when its window can't be built. Labels are
    plain-language reads, not gate inputs:
      curve: 'inverted' (< 0), 'flat' (0 to 0.25), 'normal' (> 0.25)
      liquidity: 'expanding' / 'contracting' / 'flat' on WALCL WoW
    """
    series_map = series_map or {}
    snap = {}

    curve = series_map.get('T10Y2Y') or []
    d10 = series_map.get('DGS10') or []
    ff = series_map.get('FEDFUNDS') or []
    cpi = series_map.get('CPIAUCSL') or []
    dollar = series_map.get('DTWEXBGS') or []
    walcl = series_map.get('WALCL') or []
    hy = series_map.get('BAMLH0A0HYM2') or []

    d, v = _latest(curve)
    snap['yield_curve_10y2y'] = v
    snap['yield_curve_date'] = d
    snap['yield_curve_7d_pp'] = pp_change(curve, 7)
    if v is None:
        snap['curve'] = None
    elif v < 0:
        snap['curve'] = 'inverted'
    elif v <= 0.25:
        snap['curve'] = 'flat'
    else:
        snap['curve'] = 'normal'

    d, v = _latest(d10)
    snap['dgs10'] = v
    snap['dgs10_date'] = d
    snap['dgs10_7d_pp'] = pp_change(d10, 7)

    d, v = _latest(ff)
    snap['fedfunds'] = v
    snap['fedfunds_date'] = d

    cpi_yoy = pct_change(cpi, 365)
    snap['cpi_yoy_pct'] = cpi_yoy
    d, v = _latest(cpi)
    snap['cpi_date'] = d
    if snap['dgs10'] is not None and cpi_yoy is not None:
        snap['real_rate_proxy'] = round(snap['dgs10'] - cpi_yoy, 2)
    else:
        snap['real_rate_proxy'] = None

    d, v = _latest(dollar)
    snap['dollar'] = v
    snap['dollar_date'] = d
    snap['dollar_7d_pct'] = pct_change(dollar, 7)
    snap['dollar_30d_pct'] = pct_change(dollar, 30)

    d, v = _latest(walcl)
    snap['fed_liquidity'] = v
    snap['fed_liquidity_date'] = d
    wow = pct_change(walcl, 7)
    snap['fed_liquidity_wow_pct'] = wow
    if wow is None:
        snap['liquidity'] = None
    elif wow > 0.05:
        snap['liquidity'] = 'expanding'
    elif wow < -0.05:
        snap['liquidity'] = 'contracting'
    else:
        snap['liquidity'] = 'flat'

    d, v = _latest(hy)
    snap['hy_spread'] = v
    snap['hy_spread_date'] = d
    snap['hy_spread_7d_pp'] = pp_change(hy, 7)

    return snap


def pulse_line(snap):
    """One-line run-log summary, e.g. 'FRED: curve +0.42 normal · 10y
    4.86% · real ~1.9% · liquidity flat (WALCL +0.01% WoW) · HY 3.12'."""
    if not snap:
        return 'FRED: no data'
    bits = []
    yc = snap.get('yield_curve_10y2y')
    if yc is not None:
        bits.append(f"curve {yc:+.2f} {snap.get('curve') or ''}".strip())
    d10 = snap.get('dgs10')
    if d10 is not None:
        bits.append(f'10y {d10:.2f}%')
    rr = snap.get('real_rate_proxy')
    if rr is not None:
        bits.append(f'real ~{rr:.2f}%')
    liq = snap.get('liquidity')
    wow = snap.get('fed_liquidity_wow_pct')
    if liq is not None:
        bits.append(f"liquidity {liq}"
                    + (f' (WALCL {wow:+.2f}% WoW)' if wow is not None
                       else ''))
    hy = snap.get('hy_spread')
    if hy is not None:
        bits.append(f'HY {hy:.2f}')
    if not bits:
        return 'FRED: no data'
    return 'FRED: ' + ' · '.join(bits)
