#!/usr/bin/env python3
"""Precedent / analogue model — a NEW research track for the NexIntel engine.

Nic, 2026-11-11: "sometimes we are behind the time — we need to be in
front of the wave. Use precedent models."

The engine today mostly scores what is ALREADY moving (momentum, regime,
funding). The precedent model looks forward by looking back: when current
conditions match a past setup, what happened next?

DISPLAY-ONLY + SHADOW-GRADED. This module never touches the live score or
any buy gate: it does not import, call, or consult gate_math.py,
gate_circuit.py, intraday.py, or the 60% live bar. Precedent reads are
annotations, never inputs. No alert-tier output, no confidence influence,
no trades. It imports cycle_feed.py (read-only helpers: the cycle backfill
it already built) and nothing else from the engine.

No 95% significance gate anywhere in this module. Base rates are graded
and weighted by predictive performance; the 95% level is a displayed goal
only (Nic, 2026-11-11). The only eligibility floor is MIN_N: a horizon
with fewer than MIN_N matched precedents reads "no precedent" — a
data-sufficiency floor, not a significance test.

# ---------------------------------------------------------------------------
# FINGERPRINT — the current-state vector (daily resolution)
# ---------------------------------------------------------------------------
# The history the engine actually has is DAILY (BTC closes 2016->today from
# the cycle-track backfill; Fear & Greed from 2018-02-01). There is no free
# hourly history back to 2016, so the fingerprint is daily-resolution and
# the horizons are 1d / 7d / 30d. The engine's 2h intraday horizon is NOT
# supported by this track — stated plainly, not fudged.
#
# Dimensions (all continuous; units in brackets):
#   r1d       1-day % change of close                          [%]
#   r7d       7-day % change of close                          [%]
#   r30d      30-day % change of close                         [%]
#   vol30     30-day realised volatility: std of daily log
#             returns x 100                                    [pp/day]
#   fng       Fear & Greed index                               [0-100]
#             (None before 2018-02-01 — the dim is masked, not faked)
#   dd        drawdown vs trailing 365-day high, same rule as
#             cycle_feed.dd_band                               [%]
#   halving   days since last halving / 1460 (cycle position)  [0-1]
#   coil      daily balance read, 0-1: 0.60*compression +
#             0.40*efficiency — the no-volume weights of
#             balance_score (BALANCE_WEIGHTS_NOVOL). Compression =
#             1 - (30d range / 180d range) / 0.5, clamped 0-1.
#             Efficiency = 1 - |net 30d move| / path length of
#             daily moves. (Daily adaptation: 30 daily closes play the
#             role of 24 hourly closes; 180 daily closes play the role
#             of the 7d hourly window.)
#   month_sin, month_cos
#             seasonality as a circle: sin/cos of 2*pi*(month-1)/12
#             (December and January are neighbours, not opposites)
#
# CURRENT-ONLY CONTEXT (recorded on the read, NEVER matched on — there is
# no free history source for them, so matching on them would be fiction):
#   funding_score  Kraken Futures funding score [-1,+1] — snapshots are
#                  point-in-time; no free history endpoint in the engine.
#   oi_score       open-interest flow score [-1,+1] — same, no history.
#   dom_change     BTC dominance change, points — cycle_feed documents
#                  "no keyless history source" for dominance.
#   chg_1h / balance_score
#                  intraday reads — no free hourly history back to 2016.
#
# A fingerprint needs 180 daily closes (the coil's long window), so the
# library starts ~180 days into the closes history. A day with a missing
# dim (e.g. no FNG before 2018) still fingerprints — the dim is masked
# out of that day's distances, never imputed.
#
# # ---------------------------------------------------------------------------
# MATCHING — weighted z-scored Euclidean distance, k nearest neighbours
# ---------------------------------------------------------------------------
# Why this metric: the dims have incompatible units (%, pp/day, 0-100,
# 0-1), so each dim is z-scored against the library distribution first.
# Euclidean on z-scores is transparent — anyone can read a distance as
# "about N standard deviations away per dimension". Magnitude matters
# here (a +2% day and a +20% day are NOT the same setup), so cosine
# similarity is wrong: it throws magnitude away. Mahalanobis is more
# principled (it accounts for dim correlation) but opaque and unstable
# when dims correlate — and r7d/r30d do correlate. Equal weights to
# start; the grading loop may learn per-dim weights later (weighted
# opportunities, never a gate).
#
# Rules that keep the base rates honest:
#   * Matches must be strictly in the past (no lookahead, ever).
#   * Per-horizon eligibility: a match only counts for horizon h if its
#     whole forward window is in the past — match_date <= query_date - h.
#   * Thinning: no two accepted matches within MIN_GAP_DAYS (14) of each
#     other, keeping the closest. Adjacent days have near-identical
#     fingerprints (overlapping windows); without thinning, one episode
#     would count as a dozen "independent" precedents.
#   * Max distance: matches beyond MAX_DIST z-units are not precedents.
#     A fingerprint with no close neighbours reads "no precedent" — a
#     valid, useful answer, not a failure to hide.
#
# # ---------------------------------------------------------------------------
# READ — base rates per horizon (1d / 7d / 30d)
# ---------------------------------------------------------------------------
# For each horizon: n matches, up-rate (fraction with forward return > 0),
# median forward return %, P(+4.2% favourable level touched) and
# P(-2.1% adverse level touched) — the cycle/pattern track's fixed-level
# thresholds, reused so this track is comparable with the others. The
# touch test uses daily closes (any daily close in the window beyond the
# level), which is an approximation of the hourly touch the live tracks
# grade on — stated here, not hidden. Direction: LONG if up-rate > 0.5,
# SHORT if < 0.5, FLAT if exactly 0.5 or n < MIN_N. A Wilson lower bound
# is reported as INFORMATION ONLY (how much to trust a small-n rate) —
# it never filters, gates, or changes a direction.
#
# # ---------------------------------------------------------------------------
# GRADING — how this track earns its verdict
# ---------------------------------------------------------------------------
# precedent_record() emits a shadow-ledger record (kind 'precedent'):
# asset, date, fingerprint, match dates, per-horizon reads, and the
# current-only context. grade_precedent_record(record, closes) grades it
# once the forward windows have closed: realised forward return per
# horizon, outcome vs the record's direction (win/loss), and outcome vs
# the +4.2%/-2.1% fixed-level plan (WIN / LOSS / EXPIRED — same words
# as the cycle/pattern tracks). The Proof Book wiring — writing records
# into the shadow ledger and calling grade at horizon expiry — is a
# parent follow-on: this module exposes the record + grade functions and
# never writes to data/history.json itself, and never imports intraday.py.
#
# # ---------------------------------------------------------------------------
# HONEST LIMITS (read before trusting a precedent read)
# ---------------------------------------------------------------------------
#   * Daily resolution only: no 2h intraday horizon.
#   * Funding / OI / dominance / intraday reads are context, not matched.
#   * FNG history starts 2018-02-01; pre-2018 fingerprints lack that dim.
#   * Regime change: 2016-2017 market structure differs from today (no
#     perps dominance, tiny liquidity). Match dates are reported so the
#     reader can see whether the precedents cluster in one era.
#   * Thin matches: a 10-dim fingerprint thinned to 14-day gaps may
#     yield few eligible precedents per horizon — the read says so
#     instead of dressing it up. The fix, if that happens often, is
#     broader matching (larger k, looser MAX_DIST) or fewer dims — not
#     a quieter threshold.
#   * A base rate is not a prediction. The track earns working /
#     not working / unknown from graded forward outcomes, not from the
#     backtest alone.
#
# Data: BTC daily closes via cycle_feed.fetch_btc_daily_range_coinbase
# (Coinbase public candles, keyless, paged <=300d) and Fear & Greed via
# cycle_feed.fetch_fng_history (Alternative.me, keyless). The daily-state
# history in data/cycle_history.json (the cycle-track backfill) supplies
# halving / FNG / drawdown per day. $0 — no new keys, no paid APIs.
# Pure functions never raise; fetchers fail open.
# """

import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import cycle_feed
except Exception:  # pragma: no cover - cycle_feed is in-repo; defensive only
    cycle_feed = None

# ---------------------------------------------------------------------------
# Constants (frozen at build time — no tuning, same convention as cycle_feed)
# ---------------------------------------------------------------------------

HALVING_DATES = [
    date(2012, 11, 28),
    date(2016, 7, 9),
    date(2020, 5, 11),
    date(2024, 4, 19),
]
HALVING_CYCLE_DAYS = 1460.0  # ~4 years; normalises days-since-halving to 0-1

# Fingerprint dims, in fixed order (the order is the vector's contract).
FINGERPRINT_DIMS = (
    'r1d', 'r7d', 'r30d', 'vol30', 'fng', 'dd',
    'halving', 'coil', 'month_sin', 'month_cos',
)
# Equal weights to start. The grading loop may learn per-dim weights
# later — weighted opportunities, never a gate.
DIM_WEIGHTS = {d: 1.0 for d in FINGERPRINT_DIMS}

HORIZONS_DAYS = (1, 7, 30)
HORIZON_LABELS = {1: '1d', 7: '7d', 30: '30d'}

K_DEFAULT = 12          # nearest neighbours per horizon
MIN_N = 5               # data-sufficiency floor: fewer matches = no precedent
MIN_GAP_DAYS = 14       # thinning: no two matches closer than this
MAX_DIST = 2.5          # z-units; beyond this a day is not a precedent
MIN_CLOSES = 180        # closes needed for a fingerprint (coil long window)

# Fixed-level plan thresholds, reused from the cycle/pattern tracks so
# this track grades on the same ruler (+4.2% gross / -2.1% adverse).
FAV_PCT = 4.2
ADV_PCT = 2.1

# Context dims: recorded on the read, never matched (no history source).
CONTEXT_DIMS = ('funding_score', 'oi_score', 'dom_change',
                'chg_1h', 'balance_score')

CYCLE_HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'cycle_history.json')


# ---------------------------------------------------------------------------
# Parsing helpers (pure, never raise)
# ---------------------------------------------------------------------------

def _parse_day(day):
    """date from a date / datetime / 'YYYY-MM-DD' / epoch seconds / ms."""
    try:
        if isinstance(day, datetime):
            return day.date()
        if isinstance(day, date):
            return day
        if isinstance(day, (int, float)):
            ts = float(day)
            if ts > 1e12:  # ms
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).date()
        if isinstance(day, str):
            return datetime.strptime(day.strip()[:10], '%Y-%m-%d').date()
    except Exception:
        return None
    return None


def parse_closes(closes):
    """Normalise closes to a sorted [(date, close)] list.

    Accepts [[ts, close], ...] (ts seconds or ms), [['YYYY-MM-DD',
    close], ...], or {date_str: close}. Bad rows dropped. Pure.
    """
    out = {}
    try:
        if isinstance(closes, dict):
            pairs = list(closes.items())
        else:
            pairs = [(row[0], row[1]) for row in (closes or [])
                     if isinstance(row, (list, tuple)) and len(row) >= 2]
        for key, val in pairs:
            try:
                d = _parse_day(key)
                c = float(val)
                if d is not None and c is not None and c > 0:
                    out[d] = c
            except (TypeError, ValueError, IndexError):
                continue
    except Exception:
        return []
    return sorted(out.items(), key=lambda x: x[0])


def _halving_days(day):
    """Days since the last halving, or None before the first halving."""
    try:
        d = _parse_day(day)
        if d is None:
            return None
        last = None
        for h in HALVING_DATES:
            if h <= d:
                last = h
            else:
                break
        if last is None:
            return None
        return (d - last).days
    except Exception:
        return None


def _pct_change(series, idx, lookback):
    """% change of series[idx] vs series[idx - lookback], or None."""
    try:
        if idx - lookback < 0:
            return None
        now = series[idx]
        then = series[idx - lookback]
        if not then:
            return None
        return (now - then) / then * 100.0
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        return None


def _realised_vol(closes, idx, window=30):
    """Std of daily log returns over the trailing `window` days x 100.

    Returns percentage points per day. None when too few closes.
    """
    try:
        if idx - window < 0:
            return None
        rets = []
        for i in range(idx - window + 1, idx + 1):
            prev, cur = closes[i - 1], closes[i]
            if prev and prev > 0 and cur and cur > 0:
                rets.append(math.log(cur / prev))
        if len(rets) < window // 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        return math.sqrt(var) * 100.0
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        return None


def _drawdown(closes, idx, window=365):
    """Drawdown % vs the trailing `window`-day high, or None."""
    try:
        start = max(0, idx - window + 1)
        window_closes = closes[start:idx + 1]
        if not window_closes:
            return None
        high = max(window_closes)
        cur = closes[idx]
        if not high or high <= 0 or not cur:
            return None
        return (high - cur) / high * 100.0
    except (TypeError, ValueError, IndexError):
        return None


def _coil_daily(closes, idx, short=30, long_=180):
    """Daily balance/coil read in [0, 1], no-volume weights.

    Mirrors balance_score's construction with daily closes: the last
    `short` closes play the 24 hourly closes, the last `long_` closes
    play the 7d hourly window. Compression = 1 - (short range / long
    range) / 0.5, clamped 0-1. Efficiency = 1 - |net short move| /
    path length of daily moves. Score = 0.60*compression +
    0.40*efficiency (BALANCE_WEIGHTS_NOVOL). None when too few closes.
    """
    try:
        if idx - long_ + 1 < 0:
            return None
        short_c = closes[idx - short + 1:idx + 1]
        long_c = closes[idx - long_ + 1:idx + 1]
        if len(short_c) < short or len(long_c) < long_:
            return None
        rng_s = max(short_c) - min(short_c)
        rng_l = max(long_c) - min(long_c)
        if rng_l <= 0:
            return None
        comp = max(0.0, min(1.0, 1.0 - (rng_s / rng_l) / 0.5))
        diffs = [abs(short_c[i] - short_c[i - 1])
                 for i in range(1, len(short_c))]
        path = sum(diffs)
        net = abs(short_c[-1] - short_c[0])
        eff = max(0.0, min(1.0, 1.0 - (net / path if path > 0 else 0.0)))
        return round(0.60 * comp + 0.40 * eff, 3)
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        return None


def _month_sincos(day):
    """(sin, cos) of the month angle — December/January are neighbours."""
    try:
        d = _parse_day(day)
        if d is None:
            return None, None
        ang = 2.0 * math.pi * (d.month - 1) / 12.0
        return round(math.sin(ang), 3), round(math.cos(ang), 3)
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def _fingerprint_at(idx, closes, dates, cycle_state=None, fng=None):
    """Fingerprint for closes_sorted[idx]; closes/dates precomputed.

    See fingerprint_for_day for the definition. Returns None when
    fewer than MIN_CLOSES closes exist at or before the day.
    """
    try:
        if idx is None or idx < 0 or idx >= len(dates):
            return None
        d = dates[idx]
        if idx + 1 < MIN_CLOSES:
            return None
        st = cycle_state or {}
        fng_v = st.get('fng_value')
        if fng_v is None and fng is not None:
            try:
                fng_v = int(float(fng))
            except (TypeError, ValueError):
                fng_v = None
        hdays = st.get('halving_day')
        if hdays is None:
            hdays = _halving_days(d)
        ms, mc = _month_sincos(d)
        fp = {
            'r1d': _pct_change(closes, idx, 1),
            'r7d': _pct_change(closes, idx, 7),
            'r30d': _pct_change(closes, idx, 30),
            'vol30': _realised_vol(closes, idx, 30),
            'fng': float(fng_v) if fng_v is not None else None,
            'dd': _drawdown(closes, idx, 365),
            'halving': (round(hdays / HALVING_CYCLE_DAYS, 3)
                        if hdays is not None else None),
            'coil': _coil_daily(closes, idx),
            'month_sin': ms,
            'month_cos': mc,
        }
        return {k: (round(v, 3) if isinstance(v, float) else v)
                for k, v in fp.items()}
    except Exception:
        return None


def fingerprint_for_day(day, closes_sorted, cycle_state=None, fng=None,
                      idx=None):
    """Fingerprint dict for `day` from sorted [(date, close)] closes.

    `cycle_state` is a cycle_feed.compute_state dict (or a row of
    data/cycle_history.json) — its halving_day / fng_value are used when
    present; otherwise halving is computed from HALVING_DATES and fng
    from the `fng` argument. `idx` is the day's index in closes_sorted
    (pass it when iterating — avoids a linear scan per day). Returns
    None when fewer than MIN_CLOSES closes exist at or before `day`.
    Missing dims read None (masked in distance, never imputed).
    """
    try:
        d = _parse_day(day)
        if d is None:
            return None
        closes = [c for _, c in closes_sorted]
        dates = [dt for dt, _ in closes_sorted]
        if idx is None:
            if d not in dates:
                return None
            idx = dates.index(d)
        elif idx < 0 or idx >= len(dates) or dates[idx] != d:
            return None
        return _fingerprint_at(idx, closes, dates,
                               cycle_state=cycle_state, fng=fng)
    except Exception:
        return None


def load_cycle_history(path=CYCLE_HISTORY_PATH):
    """{date_str: CycleState} from data/cycle_history.json, or {}."""
    try:
        with open(path) as f:
            data = json.load(f) or {}
        return {str(k): v for k, v in data.items()
                if isinstance(v, dict)}
    except Exception:
        return {}


def build_library(closes, cycle_states=None, asset='BTC'):
    """Historical fingerprint library from daily closes.

    Returns {'asset', 'dates', 'closes', 'fingerprints', 'stats'} where
    'fingerprints' is {date_str: {dim: value|None}} and 'stats' is
    {dim: (mean, std)} over the library (z-score basis for distance).
    Days with < MIN_CLOSES closes are skipped. Pure — never raises.
    """
    try:
        parsed = parse_closes(closes)
        if not parsed:
            return {'asset': asset, 'dates': [], 'closes': {},
                    'fingerprints': {}, 'stats': {}}
        states = cycle_states if cycle_states is not None \
            else load_cycle_history()
        closes_list = [c for _, c in parsed]
        dates_list = [d for d, _ in parsed]
        fps = {}
        for i, (d, _) in enumerate(parsed):
            st = states.get(d.isoformat()) or {}
            fp = _fingerprint_at(i, closes_list, dates_list,
                                 cycle_state=st)
            if fp is not None:
                fps[d.isoformat()] = fp
        stats = {}
        for dim in FINGERPRINT_DIMS:
            vals = [fp[dim] for fp in fps.values()
                    if fp.get(dim) is not None]
            if len(vals) >= 2:
                mean = sum(vals) / len(vals)
                var = sum((v - mean) ** 2 for v in vals) / len(vals)
                std = math.sqrt(var) or 1.0
                stats[dim] = (round(mean, 4), round(std, 4))
        return {'asset': asset,
                'dates': sorted(fps.keys()),
                'closes': {d.isoformat(): c for d, c in parsed},
                'fingerprints': fps,
                'stats': stats}
    except Exception:
        return {'asset': asset, 'dates': [], 'closes': {},
                'fingerprints': {}, 'stats': {}}


# ---------------------------------------------------------------------------
# Distance + matching
# ---------------------------------------------------------------------------

def distance(vec_a, vec_b, stats, weights=None):
    """Weighted z-scored Euclidean distance between two fingerprints.

    Only dims present (non-None) in BOTH vectors count; the distance is
    scaled by sqrt(D_total / D_shared) so a day missing a dim stays
    comparable with a full day. None when no dim is shared.
    """
    try:
        w = weights or DIM_WEIGHTS
        total = 0.0
        shared = 0
        for dim in FINGERPRINT_DIMS:
            a = (vec_a or {}).get(dim)
            b = (vec_b or {}).get(dim)
            if a is None or b is None:
                continue
            mean, std = (stats or {}).get(dim, (0.0, 1.0))
            if not std:
                std = 1.0
            total += float(w.get(dim, 1.0)) * ((a - mean) / std
                                               - (b - mean) / std) ** 2
            shared += 1
        if shared == 0:
            return None
        return round(math.sqrt(total) * math.sqrt(
            len(FINGERPRINT_DIMS) / shared), 3)
    except Exception:
        return None


def find_matches(library, query_vec, query_date, horizon_days,
                 k=K_DEFAULT, max_dist=MAX_DIST, min_gap_days=MIN_GAP_DAYS):
    """k nearest past fingerprints eligible for `horizon_days`.

    Eligibility: match date strictly before the query date AND at least
    `horizon_days` before it (the whole forward window must be in the
    past — no lookahead). Thinning: no two accepted matches within
    `min_gap_days` of each other, keeping the closest. Returns
    [(date_str, distance)] nearest-first. Empty list = no precedent.
    """
    try:
        qd = _parse_day(query_date)
        if qd is None or not query_vec:
            return []
        fps = (library or {}).get('fingerprints') or {}
        stats = (library or {}).get('stats') or {}
        scored = []
        for dstr, vec in fps.items():
            d = _parse_day(dstr)
            if d is None or d >= qd:
                continue
            if (qd - d).days < horizon_days:
                continue
            dist = distance(query_vec, vec, stats)
            if dist is None or dist > max_dist:
                continue
            scored.append((dist, dstr, d))
        scored.sort(key=lambda x: (x[0], x[1]))
        accepted = []
        for dist, dstr, d in scored:
            if all(abs((d - _parse_day(a[0])).days) >= min_gap_days
                   for a in accepted):
                accepted.append((dstr, dist))
            if len(accepted) >= k:
                break
        return accepted
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Forward outcomes + the precedent read
# ---------------------------------------------------------------------------

def forward_outcome(closes, entry_date, horizon_days):
    """Realised forward outcome from `entry_date` over `horizon_days`.

    `closes` is {date_str: close} or [(date, close)]. Returns None when
    the window is not fully in the data. Otherwise {'fwd_pct': % change
    close-to-close, 'fav': True if any daily close in the window touched
    +FAV_PCT% first, 'adv': True if -ADV_PCT% was touched first,
    'touched': 'fav' | 'adv' | None}. The touch test uses daily closes
    — an approximation of the hourly touch the live tracks grade on.
    """
    try:
        cmap = {}
        if isinstance(closes, dict):
            for k, v in closes.items():
                d = _parse_day(k)
                if d is not None:
                    cmap[d] = float(v)
        else:
            for d, c in parse_closes(closes):
                cmap[d] = float(c)
        entry = _parse_day(entry_date)
        if entry is None or entry not in cmap:
            return None
        entry_px = cmap[entry]
        if not entry_px or entry_px <= 0:
            return None
        end = entry + timedelta(days=horizon_days)
        if end not in cmap:
            return None
        fwd_pct = (cmap[end] - entry_px) / entry_px * 100.0
        fav_px = entry_px * (1 + FAV_PCT / 100.0)
        adv_px = entry_px * (1 - ADV_PCT / 100.0)
        touched = None
        d = entry + timedelta(days=1)
        while d <= end:
            px = cmap.get(d)
            if px is not None:
                if px >= fav_px:
                    touched = 'fav'
                    break
                if px <= adv_px:
                    touched = 'adv'
                    break
            d += timedelta(days=1)
        return {'fwd_pct': round(fwd_pct, 2), 'touched': touched}
    except Exception:
        return None


def wilson_lower(k, n, z=1.96):
    """Wilson score lower bound for k wins in n trials, or None.

    INFORMATION ONLY — never a filter, gate, or direction input. It says
    how much a small-n base rate can be trusted, nothing more.
    """
    try:
        if not n or n <= 0 or k is None:
            return None
        p = float(k) / float(n)
        denom = 1.0 + z * z / n
        centre = p + z * z / (2.0 * n)
        margin = z * math.sqrt((p * (1 - p) + z * z / (4.0 * n)) / n)
        return round(max(0.0, (centre - margin) / denom), 3)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def precedent_read(library, query_vec, query_date, asset='BTC',
                   k=K_DEFAULT, max_dist=MAX_DIST,
                   min_gap_days=MIN_GAP_DAYS):
    """The precedent read for one day: base rates per horizon.

    Returns {'asset', 'date', 'fingerprint', 'horizons': {label: {...}}}.
    A horizon with < MIN_N matches reads {'n': n, 'precedent': False} —
    "no precedent" is a valid answer. Pure — never raises.
    """
    try:
        qd = _parse_day(query_date)
        qstr = qd.isoformat() if qd else str(query_date)
        closes = (library or {}).get('closes') or {}
        out = {'asset': asset, 'date': qstr,
               'fingerprint': dict(query_vec or {}), 'horizons': {}}
        for h in HORIZONS_DAYS:
            label = HORIZON_LABELS[h]
            matches = find_matches(library, query_vec, qd, h, k=k,
                                   max_dist=max_dist,
                                   min_gap_days=min_gap_days)
            entry = {'n': len(matches),
                     'match_dates': [d for d, _ in matches],
                     'max_distance': (max(dist for _, dist in matches)
                                      if matches else None),
                     'precedent': len(matches) >= MIN_N}
            if not entry['precedent']:
                entry.update({'up_rate': None, 'median_fwd': None,
                              'p_fav': None, 'p_adv': None,
                              'direction': 'FLAT', 'wilson_lower': None})
                out['horizons'][label] = entry
                continue
            fwds = []
            ups = 0
            favs = 0
            advs = 0
            for dstr, _ in matches:
                oc = forward_outcome(closes, dstr, h)
                if oc is None:
                    continue
                fwds.append(oc['fwd_pct'])
                if oc['fwd_pct'] > 0:
                    ups += 1
                if oc['touched'] == 'fav':
                    favs += 1
                elif oc['touched'] == 'adv':
                    advs += 1
            n = len(fwds)
            if n < MIN_N:
                entry.update({'n': n, 'precedent': False,
                              'up_rate': None, 'median_fwd': None,
                              'p_fav': None, 'p_adv': None,
                              'direction': 'FLAT', 'wilson_lower': None})
                out['horizons'][label] = entry
                continue
            fwds_sorted = sorted(fwds)
            mid = n // 2
            median = (fwds_sorted[mid] if n % 2 == 1 else
                      (fwds_sorted[mid - 1] + fwds_sorted[mid]) / 2.0)
            up_rate = round(ups / n, 3)
            entry.update({
                'n': n,
                'up_rate': up_rate,
                'median_fwd': round(median, 2),
                'p_fav': round(favs / n, 3),
                'p_adv': round(advs / n, 3),
                'direction': ('LONG' if up_rate > 0.5 else
                              'SHORT' if up_rate < 0.5 else 'FLAT'),
                'wilson_lower': wilson_lower(ups, n),
            })
            out['horizons'][label] = entry
        return out
    except Exception:
        return {'asset': asset, 'date': str(query_date),
                'fingerprint': dict(query_vec or {}), 'horizons': {}}


def plain_words(read):
    """One plain-words line per horizon, e.g. "In 12 similar past
    setups, price rose 8 times in 7 days (median +3.1%)." Pure.
    """
    try:
        lines = []
        for h in HORIZONS_DAYS:
            label = HORIZON_LABELS[h]
            hz = (read or {}).get('horizons', {}).get(label) or {}
            n = hz.get('n') or 0
            if not hz.get('precedent'):
                lines.append(f'{label}: no close precedent '
                             f'({n} match{"es" if n != 1 else ""} found).')
                continue
            ups = int(round((hz.get('up_rate') or 0) * n))
            med = hz.get('median_fwd')
            med_s = f'{med:+.1f}%' if med is not None else 'n/a'
            lines.append(
                f"{label}: in {n} similar past setups, price rose "
                f"{ups} times (median {med_s}); "
                f"favourable +{FAV_PCT}% hit {hz.get('p_fav') or 0:.0%} "
                f"of the time, adverse -{ADV_PCT}% hit "
                f"{hz.get('p_adv') or 0:.0%} of the time.")
        return lines
    except Exception:
        return ['Precedent read unavailable.']


# ---------------------------------------------------------------------------
# Shadow-ledger record + grading (the wiring the Proof Book needs)
# ---------------------------------------------------------------------------

def precedent_record(asset, date, fingerprint, read, context=None):
    """Shadow-ledger record (kind 'precedent') for one day's read.

    `context` carries the current-only dims (funding_score, oi_score,
    dom_change, chg_1h, balance_score) — recorded, never matched.
    The record is what the Proof Book grades; it never touches a gate.
    """
    try:
        d = _parse_day(date)
        return {
            'kind': 'precedent',
            'asset': (asset or '').upper(),
            'date': d.isoformat() if d else str(date),
            'fingerprint': dict(fingerprint or {}),
            'context': dict(context or {}),
            'k': K_DEFAULT,
            'horizons': {
                label: {
                    'n': (hz or {}).get('n', 0),
                    'precedent': bool((hz or {}).get('precedent')),
                    'up_rate': (hz or {}).get('up_rate'),
                    'median_fwd': (hz or {}).get('median_fwd'),
                    'direction': (hz or {}).get('direction', 'FLAT'),
                    'match_dates': list((hz or {}).get('match_dates') or []),
                    'max_distance': (hz or {}).get('max_distance'),
                } for label, hz in (read or {}).get('horizons', {}).items()
            },
        }
    except Exception:
        return {'kind': 'precedent', 'asset': (asset or '').upper(),
                'date': str(date), 'fingerprint': {},
                'context': dict(context or {}), 'k': K_DEFAULT,
                'horizons': {}}


def grade_precedent_record(record, closes):
    """Grade a precedent record once its forward windows have closed.

    Per horizon: realised forward return %, outcome vs the record's
    direction (win/loss — realised sign matches direction; FLAT records
    are not graded), and outcome vs the +4.2%/-2.1% fixed-level plan
    (WIN / LOSS / EXPIRED, same words as the cycle/pattern tracks).
    Returns {label: {...}}; a horizon whose window is not yet in the
    data reads 'pending'. Pure — never raises.
    """
    try:
        out = {}
        horizons = (record or {}).get('horizons') or {}
        rec_date = (record or {}).get('date')
        for label, hz in horizons.items():
            h = next((k for k, v in HORIZON_LABELS.items() if v == label),
                     None)
            if h is None:
                continue
            direction = (hz or {}).get('direction', 'FLAT')
            if not (hz or {}).get('precedent') or direction == 'FLAT':
                out[label] = {'graded': False,
                              'reason': 'no precedent / FLAT direction'}
                continue
            oc = forward_outcome(closes, rec_date, h)
            if oc is None:
                out[label] = {'graded': False, 'reason': 'pending'}
                continue
            fwd = oc['fwd_pct']
            won = (fwd > 0) if direction == 'LONG' else (fwd < 0)
            plan = ('WIN' if oc['touched'] == 'fav' else
                    'LOSS' if oc['touched'] == 'adv' else 'EXPIRED')
            out[label] = {'graded': True, 'fwd_pct': fwd,
                          'direction': direction,
                          'outcome': 'win' if won else 'loss',
                          'plan': plan}
        return out
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Fetches (keyless, fail-open — reuse the cycle track's proven fetchers)
# ---------------------------------------------------------------------------

def _http_json(url, timeout=30):
    import urllib.request
    req = urllib.request.Request(
        url, headers={'User-Agent': 'NexIntel-Precedent/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_btc_daily(start_day, end_day):
    """[[ts, close], ...] BTC daily closes via Coinbase public candles
    (paged <=300d, same fetcher the cycle backfill uses). [] on failure.
    """
    try:
        if cycle_feed is None:
            return []
        s = _parse_day(start_day)
        e = _parse_day(end_day)
        if not s or not e or s > e:
            return []
        closes = []
        cur = s
        while cur <= e:
            chunk_end = min(cur + timedelta(days=299), e)
            page = cycle_feed.fetch_btc_daily_range_coinbase(
                _http_json, cur, chunk_end)
            closes.extend(page or [])
            cur = chunk_end + timedelta(days=1)
        closes.sort(key=lambda x: x[0])
        dedup = {}
        for ts, c in closes:
            dedup[int(ts)] = [int(ts), float(c)]
        out = sorted(dedup.values(), key=lambda x: x[0])
        today = datetime.now(timezone.utc).date()
        return [row for row in out
                if datetime.fromtimestamp(row[0], tz=timezone.utc).date()
                < today]
    except Exception:
        return []


def fetch_fng_history():
    """{date_str: int} Fear & Greed history via cycle_feed. {} on failure."""
    try:
        if cycle_feed is None:
            return {}
        return cycle_feed.fetch_fng_history(_http_json) or {}
    except Exception:
        return {}
