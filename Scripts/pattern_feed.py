"""Price-action pattern research track — Phase 1 detection (spec DRAFT
2026-11-04, ~/workspace/goals/build-out-omnisight/hidden_files/
price-action-research-track-spec-2026-11-04.md).

DISPLAY-ONLY + SHADOW-GRADED. This module never touches a buy gate: it
does not import, call, or consult gate_math.py, gate_circuit.py, or the
60% live bar. Pattern tags are annotations, never inputs. No alert-tier
output, no confidence influence, no trades.

What it does:
  * Strict detection of the spec §1 catalog (18 patterns: classic
    single/double-candle, multi-candle formations, ICT/SMC set) on
    closed 1h candles only. Wolfe Wave is EXCLUDED per §1d — it cannot
    be written as candle math.
  * Kraken public OHLC fetch (keyless, interval=60) + the
    `pattern_candles` store in intraday.json (last 200 hourly candles
    per symbol) — the shared fetch the manipulation track also reads,
    so it must never be pulled twice.
  * Confluence tagging (§3): >= 2 same-direction patterns on the same
    asset+timeframe in the same run share a confluence_id.

Conventions (conservative display-only defaults, flagged for Nic):
  * detect() evaluates the LATEST closed candle as the firing candle.
    A pattern fires in the run its confirming candle closes; it does
    not re-fire from history. Confirmation formations (double top/
    bottom, flags, head & shoulders, triples) additionally require a
    FRESH break — the prior candle's close must not already be beyond
    the confirmation level — so a level broken days ago does not keep
    re-firing every hour.
  * Swing high/low = highest high / lowest low of the trailing 20
    closed candles BEFORE the firing candle. A pivot candle counts as
    a swing low/high when its low/high is the extreme of the 20
    candles ending at it.
  * ATR = Wilder's ATR(14) on the pattern timeframe.
  * Every rule function is pure and never raises: bad data returns no
    tag, never an exception. Detection needs >= 40 closed candles
    (20 swing lookback + 14 ATR + 6 formation); fewer -> no tags.
  * `strength` is informational only (e.g. wick multiple, gap size in
    ATR). It is NOT a score and nothing consumes it in Phase 1/2.

Kraken pair map: most engine symbols map to {SYM}USD; known
exceptions live in KRAKEN_PAIR_OVERRIDES. A pair the endpoint rejects
is skipped fail-open — never an exception, never a retry storm.
"""

import math

MIN_CANDLES = 40        # 20 swing lookback + 14 ATR + 6 formation
MAX_CANDLES_STORED = 200  # last 200 hourly candles per symbol (~8 days)
MAX_SYMBOLS_PER_RUN = 30  # bounded universe per spec §2
TIMEFRAME = '1h'
SWING_LOOKBACK = 20
ATR_PERIOD = 14

KRAKEN_OHLC_URL = 'https://api.kraken.com/0/public/OHLC?pair={pair}&interval=60'

# Engine symbol -> Kraken spot pair. Kraken calls bitcoin XBT, and RNDR
# trades there as RENDER. Everything else is the plain {SYM}USD pair.
# Best-effort map: an unmapped pair is skipped fail-open by the fetch.
KRAKEN_PAIR_OVERRIDES = {
    'BTC': 'XBTUSD',
    'XBT': 'XBTUSD',
    'RNDR': 'RENDERUSD',
}

PATTERN_NAMES = [
    'Hammer', 'Shooting Star',
    'Bullish Engulfing', 'Bearish Engulfing',
    'Morning Star', 'Evening Star',
    'Double Bottom', 'Double Top',
    'Bullish Flag', 'Bearish Flag',
    'Inverse Head & Shoulders', 'Head & Shoulders',
    'Triple Bottom', 'Triple Top',
    'BOS', 'FVG', 'Liquidity Sweep', 'OTE', 'Order Block',
]


def kraken_pair(asset):
    """Engine asset symbol -> Kraken spot OHLC pair ('' when unmappable)."""
    try:
        a = (asset or '').upper().strip()
    except Exception:
        return ''
    if not a:
        return ''
    if a in KRAKEN_PAIR_OVERRIDES:
        return KRAKEN_PAIR_OVERRIDES[a]
    return f'{a}USD'


# ---------------------------------------------------------------------------
# Candle helpers (pure, never raise)
# ---------------------------------------------------------------------------

def _parse_candles(candles):
    """Normalize to [(ts, o, h, l, c)] floats, sorted by ts, bad rows
    dropped. Returns [] on any bad input — never raises."""
    try:
        rows = []
        for r in (candles or []):
            try:
                ts, o, h, l, c = r[0], r[1], r[2], r[3], r[4]
                rows.append((int(ts), float(o), float(h), float(l), float(c)))
            except (TypeError, ValueError, IndexError):
                continue
        rows.sort(key=lambda x: x[0])
        return rows
    except Exception:
        return []


def _body(c):
    return abs(c[4] - c[1])


def _range(c):
    return c[2] - c[3]


def atr_series(candles):
    """Wilder's ATR(14), aligned with `candles` (None until index 14:
    the first ATR is the mean of the first 14 true ranges)."""
    n = len(candles)
    atr = [None] * n
    if n < ATR_PERIOD + 1:
        return atr
    trs = [0.0] * n
    for i in range(1, n):
        prev_c = candles[i - 1][4]
        trs[i] = max(candles[i][2] - candles[i][3],
                     abs(candles[i][2] - prev_c),
                     abs(candles[i][3] - prev_c))
    atr[ATR_PERIOD] = sum(trs[1:ATR_PERIOD + 1]) / ATR_PERIOD
    for i in range(ATR_PERIOD + 1, n):
        atr[i] = (atr[i - 1] * (ATR_PERIOD - 1) + trs[i]) / ATR_PERIOD
    return atr


def _swing_high(candles, i, lookback=SWING_LOOKBACK):
    """Highest high of the `lookback` closed candles BEFORE index i.
    None when fewer than `lookback` candles precede i."""
    if i < lookback:
        return None
    return max(c[2] for c in candles[i - lookback:i])


def _swing_low(candles, i, lookback=SWING_LOOKBACK):
    """Lowest low of the `lookback` closed candles BEFORE index i."""
    if i < lookback:
        return None
    return min(c[3] for c in candles[i - lookback:i])


def _is_swing_low(candles, j):
    """True when candle j's low is the lowest low of the 20 candles
    ending at j (the pivot definition used by the formation rules)."""
    if j < SWING_LOOKBACK - 1:
        return False
    lo = candles[j][3]
    return lo <= min(c[3] for c in candles[j - SWING_LOOKBACK + 1:j + 1])


def _is_swing_high(candles, j):
    if j < SWING_LOOKBACK - 1:
        return False
    hi = candles[j][2]
    return hi >= max(c[2] for c in candles[j - SWING_LOOKBACK + 1:j + 1])


def _tag(pattern, direction, fired_ts, strength):
    return {'pattern': pattern, 'direction': direction,
            'timeframe': TIMEFRAME, 'fired_ts': int(fired_ts),
            'strength': round(float(strength), 3),
            'confluence_id': None}


# ---------------------------------------------------------------------------
# §1a — classic single/double-candle (firing candle = latest closed)
# ---------------------------------------------------------------------------

def _r_hammer(c, atr, i):
    """lw >= 2x body, body <= 0.35x range, uw <= 0.15x range, low within
    0.25x ATR of the 20-candle swing low, prior candle red."""
    try:
        a = atr[i]
        if a is None or a <= 0:
            return None
        cur, prev = c[i], c[i - 1]
        body, rng = _body(cur), _range(cur)
        if body <= 0 or rng <= 0:
            return None  # doji / flat candle: no hammer
        uw = cur[2] - max(cur[1], cur[4])
        lw = min(cur[1], cur[4]) - cur[3]
        if lw < 2 * body or body > 0.35 * rng or uw > 0.15 * rng:
            return None
        swl = _swing_low(c, i)
        if swl is None or abs(cur[3] - swl) > 0.25 * a:
            return None
        if not prev[4] < prev[1]:  # prior candle red
            return None
        return _tag('Hammer', 'LONG', cur[0], lw / body)
    except (TypeError, IndexError):
        return None


def _r_shooting_star(c, atr, i):
    """Mirror of the hammer at the 20-candle swing high, prior green."""
    try:
        a = atr[i]
        if a is None or a <= 0:
            return None
        cur, prev = c[i], c[i - 1]
        body, rng = _body(cur), _range(cur)
        if body <= 0 or rng <= 0:
            return None
        uw = cur[2] - max(cur[1], cur[4])
        lw = min(cur[1], cur[4]) - cur[3]
        if uw < 2 * body or body > 0.35 * rng or lw > 0.15 * rng:
            return None
        swh = _swing_high(c, i)
        if swh is None or abs(cur[2] - swh) > 0.25 * a:
            return None
        if not prev[4] > prev[1]:  # prior candle green
            return None
        return _tag('Shooting Star', 'SHORT', cur[0], uw / body)
    except (TypeError, IndexError):
        return None


def _r_bull_engulfing(c, atr, i):
    """Prev red with body >= 0.4x ATR; current green, fully engulfing
    the prior body; current body >= 0.5x ATR."""
    try:
        a = atr[i]
        if a is None or a <= 0:
            return None
        cur, prev = c[i], c[i - 1]
        if not (prev[4] < prev[1] and cur[4] > cur[1]):
            return None
        if _body(prev) < 0.4 * a or _body(cur) < 0.5 * a:
            return None
        if not (cur[1] <= prev[4] and cur[4] >= prev[1]):
            return None
        return _tag('Bullish Engulfing', 'LONG', cur[0], _body(cur) / a)
    except (TypeError, IndexError):
        return None


def _r_bear_engulfing(c, atr, i):
    """Mirror of bullish engulfing."""
    try:
        a = atr[i]
        if a is None or a <= 0:
            return None
        cur, prev = c[i], c[i - 1]
        if not (prev[4] > prev[1] and cur[4] < cur[1]):
            return None
        if _body(prev) < 0.4 * a or _body(cur) < 0.5 * a:
            return None
        if not (cur[1] >= prev[4] and cur[4] <= prev[1]):
            return None
        return _tag('Bearish Engulfing', 'SHORT', cur[0], _body(cur) / a)
    except (TypeError, IndexError):
        return None


def _r_morning_star(c, atr, i):
    """Long red (>= 0.6x ATR), small middle (<= 0.3x the first body),
    long green (>= 0.6x ATR) closing above the first body's midpoint."""
    try:
        a = atr[i]
        if a is None or a <= 0:
            return None
        c0, c1, c2 = c[i - 2], c[i - 1], c[i]
        if not (c0[4] < c0[1] and _body(c0) >= 0.6 * a):
            return None
        if _body(c1) > 0.3 * _body(c0):
            return None
        if not (c2[4] > c2[1] and _body(c2) >= 0.6 * a):
            return None
        mid = (c0[1] + c0[4]) / 2
        if not c2[4] > mid:
            return None
        return _tag('Morning Star', 'LONG', c2[0], _body(c2) / a)
    except (TypeError, IndexError):
        return None


def _r_evening_star(c, atr, i):
    """Mirror of the morning star."""
    try:
        a = atr[i]
        if a is None or a <= 0:
            return None
        c0, c1, c2 = c[i - 2], c[i - 1], c[i]
        if not (c0[4] > c0[1] and _body(c0) >= 0.6 * a):
            return None
        if _body(c1) > 0.3 * _body(c0):
            return None
        if not (c2[4] < c2[1] and _body(c2) >= 0.6 * a):
            return None
        mid = (c0[1] + c0[4]) / 2
        if not c2[4] < mid:
            return None
        return _tag('Evening Star', 'SHORT', c2[0], _body(c2) / a)
    except (TypeError, IndexError):
        return None


# ---------------------------------------------------------------------------
# §1b — multi-candle formations (confirmation on the latest close)
# ---------------------------------------------------------------------------

def _r_double_bottom(c, atr, i):
    """Two 20-candle swing lows within 0.5x ATR, >= 10 candles apart,
    intervening high >= 1.0x ATR above the lows; confirmed on the
    latest close above the intervening high (fresh break only)."""
    try:
        a = atr[i]
        if a is None or a <= 0:
            return None
        for b in range(i - 1, max(SWING_LOOKBACK, i - 60) - 1, -1):
            if not _is_swing_low(c, b):
                continue
            for p in range(b - 10, max(0, i - 80) - 1, -1):
                if not _is_swing_low(c, p):
                    continue
                lo_p, lo_b = c[p][3], c[b][3]
                if abs(lo_p - lo_b) > 0.5 * a:
                    continue
                h = max(x[2] for x in c[p + 1:b])
                if h - max(lo_p, lo_b) < 1.0 * a:
                    continue
                if c[i][4] > h and c[i - 1][4] <= h:
                    return _tag('Double Bottom', 'LONG', c[i][0],
                                (c[i][4] - h) / a)
        return None
    except (TypeError, IndexError, ValueError):
        return None


def _r_double_top(c, atr, i):
    """Mirror of the double bottom at swing highs."""
    try:
        a = atr[i]
        if a is None or a <= 0:
            return None
        for b in range(i - 1, max(SWING_LOOKBACK, i - 60) - 1, -1):
            if not _is_swing_high(c, b):
                continue
            for p in range(b - 10, max(0, i - 80) - 1, -1):
                if not _is_swing_high(c, p):
                    continue
                hi_p, hi_b = c[p][2], c[b][2]
                if abs(hi_p - hi_b) > 0.5 * a:
                    continue
                lo = min(x[3] for x in c[p + 1:b])
                if min(hi_p, hi_b) - lo < 1.0 * a:
                    continue
                if c[i][4] < lo and c[i - 1][4] >= lo:
                    return _tag('Double Top', 'SHORT', c[i][0],
                                (lo - c[i][4]) / a)
        return None
    except (TypeError, IndexError, ValueError):
        return None


def _r_bull_flag(c, atr, i):
    """Pole: >= 3.0% rise in <= 12 candles; consolidation: >= 6 candles,
    range <= 40% of pole height, flat-to-down drift; confirmed on the
    latest close above the consolidation high (fresh break only)."""
    try:
        a = atr[i]
        if a is None or a <= 0:
            return None
        for b in range(i - 7, max(0, i - 50) - 1, -1):
            # Pole ending at b: best (largest) rise over <= 12 candles.
            best = None
            for s in range(max(0, b - 12), b):
                if c[s][4] <= 0:
                    continue
                rise = (c[b][4] - c[s][4]) / c[s][4]
                if best is None or rise > best[0]:
                    best = (rise, c[b][4] - c[s][4])
            if best is None or best[0] < 0.03:
                continue
            pole_h = best[1]
            cons = c[b + 1:i]
            if len(cons) < 6:
                continue
            cons_hi = max(x[2] for x in cons)
            cons_lo = min(x[3] for x in cons)
            if cons_hi - cons_lo > 0.4 * pole_h:
                continue
            if cons[-1][4] > c[b][4]:  # drifted up, not flat-to-down
                continue
            if c[i][4] > cons_hi and c[i - 1][4] <= cons_hi:
                return _tag('Bullish Flag', 'LONG', c[i][0],
                            (c[i][4] - cons_hi) / a)
        return None
    except (TypeError, IndexError, ValueError):
        return None


def _r_bear_flag(c, atr, i):
    """Mirror of the bullish flag."""
    try:
        a = atr[i]
        if a is None or a <= 0:
            return None
        for b in range(i - 7, max(0, i - 50) - 1, -1):
            best = None
            for s in range(max(0, b - 12), b):
                if c[s][4] <= 0:
                    continue
                drop = (c[s][4] - c[b][4]) / c[s][4]
                if best is None or drop > best[0]:
                    best = (drop, c[s][4] - c[b][4])
            if best is None or best[0] < 0.03:
                continue
            pole_h = best[1]
            cons = c[b + 1:i]
            if len(cons) < 6:
                continue
            cons_hi = max(x[2] for x in cons)
            cons_lo = min(x[3] for x in cons)
            if cons_hi - cons_lo > 0.4 * pole_h:
                continue
            if cons[-1][4] < c[b][4]:
                continue
            if c[i][4] < cons_lo and c[i - 1][4] >= cons_lo:
                return _tag('Bearish Flag', 'SHORT', c[i][0],
                            (cons_lo - c[i][4]) / a)
        return None
    except (TypeError, IndexError, ValueError):
        return None


def _r_inv_head_shoulders(c, atr, i):
    """Three swing lows: head lower than both shoulders by >= 0.5x ATR,
    shoulders within 0.5x ATR; neckline = higher of the two intervening
    highs; confirmed on the latest close above the neckline. Pivots
    need at least one candle between them (an intervening high needs
    a candle to exist in)."""
    try:
        a = atr[i]
        if a is None or a <= 0:
            return None
        for l3 in range(i - 1, max(SWING_LOOKBACK, i - 60) - 1, -1):
            if not _is_swing_low(c, l3):
                continue
            for l2 in range(l3 - 1, max(0, i - 90) - 1, -1):
                if not _is_swing_low(c, l2):
                    continue
                for l1 in range(l2 - 1, max(0, i - 100) - 1, -1):
                    if not _is_swing_low(c, l1):
                        continue
                    if l2 - l1 < 2 or l3 - l2 < 2:
                        continue
                    lo1, lo2, lo3 = c[l1][3], c[l2][3], c[l3][3]
                    if abs(lo1 - lo3) > 0.5 * a:
                        continue
                    if lo2 > lo1 - 0.5 * a or lo2 > lo3 - 0.5 * a:
                        continue
                    h1 = max(x[2] for x in c[l1 + 1:l2])
                    h2 = max(x[2] for x in c[l2 + 1:l3])
                    neck = max(h1, h2)
                    if c[i][4] > neck and c[i - 1][4] <= neck:
                        return _tag('Inverse Head & Shoulders', 'LONG',
                                    c[i][0], (c[i][4] - neck) / a)
        return None
    except (TypeError, IndexError, ValueError):
        return None


def _r_head_shoulders(c, atr, i):
    """Mirror of the inverse head & shoulders at swing highs."""
    try:
        a = atr[i]
        if a is None or a <= 0:
            return None
        for l3 in range(i - 1, max(SWING_LOOKBACK, i - 60) - 1, -1):
            if not _is_swing_high(c, l3):
                continue
            for l2 in range(l3 - 1, max(0, i - 90) - 1, -1):
                if not _is_swing_high(c, l2):
                    continue
                for l1 in range(l2 - 1, max(0, i - 100) - 1, -1):
                    if not _is_swing_high(c, l1):
                        continue
                    if l2 - l1 < 2 or l3 - l2 < 2:
                        continue
                    hi1, hi2, hi3 = c[l1][2], c[l2][2], c[l3][2]
                    if abs(hi1 - hi3) > 0.5 * a:
                        continue
                    if hi2 < hi1 + 0.5 * a or hi2 < hi3 + 0.5 * a:
                        continue
                    n1 = min(x[3] for x in c[l1 + 1:l2])
                    n2 = min(x[3] for x in c[l2 + 1:l3])
                    neck = min(n1, n2)
                    if c[i][4] < neck and c[i - 1][4] >= neck:
                        return _tag('Head & Shoulders', 'SHORT',
                                    c[i][0], (neck - c[i][4]) / a)
        return None
    except (TypeError, IndexError, ValueError):
        return None


def _r_triple_bottom(c, atr, i):
    """Three swing lows within 0.5x ATR of each other, each pair >= 8
    candles apart; a 3-touch extension of rule 7, so each intervening
    high must sit >= 1.0x ATR above the lows; confirmed on the latest
    close above the higher of the two intervening highs (fresh break
    only)."""
    try:
        a = atr[i]
        if a is None or a <= 0:
            return None
        for t3 in range(i - 1, max(SWING_LOOKBACK, i - 60) - 1, -1):
            if not _is_swing_low(c, t3):
                continue
            for t2 in range(t3 - 8, max(0, i - 90) - 1, -1):
                if not _is_swing_low(c, t2):
                    continue
                for t1 in range(t2 - 8, max(0, i - 100) - 1, -1):
                    if not _is_swing_low(c, t1):
                        continue
                    lows = [c[t1][3], c[t2][3], c[t3][3]]
                    if max(lows) - min(lows) > 0.5 * a:
                        continue
                    h1 = max(x[2] for x in c[t1 + 1:t2])
                    h2 = max(x[2] for x in c[t2 + 1:t3])
                    if h1 - max(lows) < 1.0 * a:
                        continue
                    if h2 - max(lows) < 1.0 * a:
                        continue
                    h = max(h1, h2)
                    if c[i][4] > h and c[i - 1][4] <= h:
                        return _tag('Triple Bottom', 'LONG', c[i][0],
                                    (c[i][4] - h) / a)
        return None
    except (TypeError, IndexError, ValueError):
        return None


def _r_triple_top(c, atr, i):
    """Mirror of the triple bottom at swing highs (each intervening
    low >= 1.0x ATR below the highs, per rule 8)."""
    try:
        a = atr[i]
        if a is None or a <= 0:
            return None
        for t3 in range(i - 1, max(SWING_LOOKBACK, i - 60) - 1, -1):
            if not _is_swing_high(c, t3):
                continue
            for t2 in range(t3 - 8, max(0, i - 90) - 1, -1):
                if not _is_swing_high(c, t2):
                    continue
                for t1 in range(t2 - 8, max(0, i - 100) - 1, -1):
                    if not _is_swing_high(c, t1):
                        continue
                    highs = [c[t1][2], c[t2][2], c[t3][2]]
                    if max(highs) - min(highs) > 0.5 * a:
                        continue
                    n1 = min(x[3] for x in c[t1 + 1:t2])
                    n2 = min(x[3] for x in c[t2 + 1:t3])
                    if min(highs) - n1 < 1.0 * a:
                        continue
                    if min(highs) - n2 < 1.0 * a:
                        continue
                    lo = min(n1, n2)
                    if c[i][4] < lo and c[i - 1][4] >= lo:
                        return _tag('Triple Top', 'SHORT', c[i][0],
                                    (lo - c[i][4]) / a)
        return None
    except (TypeError, IndexError, ValueError):
        return None


# ---------------------------------------------------------------------------
# §1c — ICT/SMC set
# ---------------------------------------------------------------------------

def _bos_at(c, atr, j):
    """Break of structure at candle j: close beyond the prior
    20-candle swing high/low by >= 0.25x ATR with a displacement
    candle body >= 0.5x ATR. Returns 'LONG' / 'SHORT' / None."""
    try:
        a = atr[j]
        if a is None or a <= 0:
            return None
        if _body(c[j]) < 0.5 * a:
            return None
        swh = _swing_high(c, j)
        swl = _swing_low(c, j)
        if swh is not None and c[j][4] > swh + 0.25 * a:
            return 'LONG'
        if swl is not None and c[j][4] < swl - 0.25 * a:
            return 'SHORT'
        return None
    except (TypeError, IndexError):
        return None


def _r_bos(c, atr, i):
    """BOS on the latest candle (break direction)."""
    d = _bos_at(c, atr, i)
    if d is None:
        return None
    a = atr[i]
    swh = _swing_high(c, i)
    swl = _swing_low(c, i)
    ref = swh if d == 'LONG' else swl
    strength = abs(c[i][4] - ref) / a if a and ref is not None else 0
    return _tag('BOS', d, c[i][0], strength)


def _r_fvg(c, atr, i):
    """Fair value gap: bullish low[i] > high[i-2] with gap >= 0.25x ATR,
    unfilled at detection (no candle since has traded through the
    gap). Bearish mirror."""
    try:
        a = atr[i]
        if a is None or a <= 0:
            return None
        gap_up = c[i][3] - c[i - 2][2]
        if gap_up >= 0.25 * a and c[i - 1][3] >= c[i - 2][2]:
            return _tag('FVG', 'LONG', c[i][0], gap_up / a)
        gap_dn = c[i - 2][3] - c[i][2]
        if gap_dn >= 0.25 * a and c[i - 1][2] <= c[i - 2][3]:
            return _tag('FVG', 'SHORT', c[i][0], gap_dn / a)
        return None
    except (TypeError, IndexError):
        return None


def _r_liquidity_sweep(c, atr, i):
    """Wick pierces the prior 20-candle swing high by >= 0.2x ATR but
    the candle closes back below it -> SHORT (bearish sweep).
    Mirror -> LONG."""
    try:
        a = atr[i]
        if a is None or a <= 0:
            return None
        swh = _swing_high(c, i)
        swl = _swing_low(c, i)
        if swh is not None and c[i][2] >= swh + 0.2 * a \
                and c[i][4] < swh:
            return _tag('Liquidity Sweep', 'SHORT', c[i][0],
                        (c[i][2] - swh) / a)
        if swl is not None and c[i][3] <= swl - 0.2 * a \
                and c[i][4] > swl:
            return _tag('Liquidity Sweep', 'LONG', c[i][0],
                        (swl - c[i][3]) / a)
        return None
    except (TypeError, IndexError):
        return None


def _r_ote(c, atr, i):
    """Optimal trade entry: impulse >= 2.5% in <= 24 candles, then a
    retracement tap of the 62-79% Fib zone (wick or close within
    0.2x ATR) on a rejection candle (close in the top 30% of its
    range for LONG, bottom 30% for SHORT)."""
    try:
        a = atr[i]
        if a is None or a <= 0:
            return None
        cur = c[i]
        rng = _range(cur)
        if rng <= 0:
            return None
        for j in range(i - 1, max(0, i - 24) - 1, -1):
            for s in range(max(0, j - 24), j):
                if c[s][4] <= 0:
                    continue
                rise = (c[j][4] - c[s][4]) / c[s][4]
                drop = (c[s][4] - c[j][4]) / c[s][4]
                seg = c[s:j + 1]
                hi = max(x[2] for x in seg)
                lo = min(x[3] for x in seg)
                span = hi - lo
                if span <= 0:
                    continue
                if rise >= 0.025:  # up impulse -> LONG retracement
                    zb = hi - 0.79 * span
                    zt = hi - 0.62 * span
                    taps = cur[3] <= zt + 0.2 * a and cur[2] >= zb - 0.2 * a
                    reject = cur[4] >= cur[3] + 0.7 * rng
                    if taps and reject and cur[4] < hi:
                        depth = (hi - cur[4]) / span
                        return _tag('OTE', 'LONG', cur[0], depth)
                if drop >= 0.025:  # down impulse -> SHORT retracement
                    zb = lo + 0.62 * span
                    zt = lo + 0.79 * span
                    taps = cur[2] >= zb - 0.2 * a and cur[3] <= zt + 0.2 * a
                    reject = cur[4] <= cur[3] + 0.3 * rng
                    if taps and reject and cur[4] > lo:
                        depth = (cur[4] - lo) / span
                        return _tag('OTE', 'SHORT', cur[0], depth)
        return None
    except (TypeError, IndexError, ValueError):
        return None


def _r_order_block(c, atr, i):
    """Order block (pinned definition, spec §1c #18): the last
    opposite-direction candle before a BOS (body >= 0.5x ATR); zone =
    its open-to-close range; signal on the FIRST retest (wick or close
    touches the zone) after the BOS."""
    try:
        a = atr[i]
        if a is None or a <= 0:
            return None
        cur = c[i]
        for j in range(i - 1, max(0, i - 24) - 1, -1):
            d = _bos_at(c, atr, j)
            if d is None:
                continue
            aj = atr[j]
            if aj is None or aj <= 0:
                continue
            # Last opposite-direction candle before the BOS.
            ob = None
            for k in range(j - 1, max(0, j - 20) - 1, -1):
                if d == 'LONG' and c[k][4] < c[k][1] \
                        and _body(c[k]) >= 0.5 * aj:
                    ob = c[k]
                    break
                if d == 'SHORT' and c[k][4] > c[k][1] \
                        and _body(c[k]) >= 0.5 * aj:
                    ob = c[k]
                    break
            if ob is None:
                continue
            zlo, zhi = min(ob[1], ob[4]), max(ob[1], ob[4])
            # First retest: no candle between the BOS and now touched it.
            touched_before = False
            for m in range(j + 1, i):
                if c[m][3] <= zhi and c[m][2] >= zlo:
                    touched_before = True
                    break
            if touched_before:
                continue  # already retested — this BOS is spent
            if cur[3] <= zhi and cur[2] >= zlo:
                return _tag('Order Block', d, cur[0], _body(ob) / aj)
            # BOS found but no retest on the latest candle — an older
            # BOS may still be retesting now, so keep scanning.
        return None
    except (TypeError, IndexError):
        return None


_RULES = [
    _r_hammer, _r_shooting_star,
    _r_bull_engulfing, _r_bear_engulfing,
    _r_morning_star, _r_evening_star,
    _r_double_bottom, _r_double_top,
    _r_bull_flag, _r_bear_flag,
    _r_inv_head_shoulders, _r_head_shoulders,
    _r_triple_bottom, _r_triple_top,
    _r_bos, _r_fvg, _r_liquidity_sweep, _r_ote, _r_order_block,
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect(candles):
    """[PatternTag] firing on the latest closed candle in `candles`
    (list of [ts, o, h, l, c]). [] when fewer than MIN_CANDLES, when
    nothing fires, or when the data is unusable. Pure — never raises.

    Confluence (§3): when >= 2 same-direction patterns fire, each of
    those tags carries the same confluence_id
    ('conf-1h-{direction}-{fired_ts}'); otherwise confluence_id is None.
    """
    try:
        c = _parse_candles(candles)
        if len(c) < MIN_CANDLES:
            return []
        atr = atr_series(c)
        i = len(c) - 1
        tags = []
        for rule in _RULES:
            try:
                t = rule(c, atr, i)
            except Exception:
                t = None
            if t is not None:
                tags.append(t)
        by_dir = {}
        for t in tags:
            by_dir.setdefault(t['direction'], []).append(t)
        for direction, group in by_dir.items():
            if len(group) >= 2:
                cid = (f"conf-{TIMEFRAME}-{direction}-"
                       f"{group[0]['fired_ts']}")
                for t in group:
                    t['confluence_id'] = cid
        return tags
    except Exception:
        return []


def detect_for_universe(pattern_candles):
    """{asset: [PatternTag]} for a pattern_candles store. One bad symbol
    never blocks the rest. Pure — never raises."""
    out = {}
    try:
        for asset, candles in (pattern_candles or {}).items():
            try:
                tags = detect(candles)
            except Exception:
                tags = []
            if tags:
                out[asset] = tags
    except Exception:
        return out
    return out


def fetch_ohlc(fetch_json, symbols, max_symbols=MAX_SYMBOLS_PER_RUN):
    """{asset: [[ts, o, h, l, c], ...]} from Kraken public OHLC
    (interval=60, keyless), one call per symbol, bounded to
    `max_symbols` in the given order. The LAST row returned is the
    current forming hour — it is dropped, so only closed candles are
    ever stored. Unmapped pairs, endpoint errors, and malformed rows
    are skipped fail-open. Never raises; never retries (a retry storm
    on a public endpoint is how the 2026-10-20 throttling happened)."""
    out = {}
    try:
        for sym in list(symbols or [])[:max_symbols]:
            pair = kraken_pair(sym)
            if not pair:
                continue
            try:
                data = fetch_json(KRAKEN_OHLC_URL.format(pair=pair))
            except Exception:
                continue
            try:
                result = (data or {}).get('result') or {}
                rows = result.get(pair)
                if not isinstance(rows, list):
                    continue
                candles = []
                for r in rows:
                    try:
                        candles.append([int(r[0]), float(r[1]),
                                        float(r[2]), float(r[3]),
                                        float(r[4])])
                    except (TypeError, ValueError, IndexError):
                        continue
                candles.sort(key=lambda x: x[0])
                if candles:
                    candles = candles[:-1]  # drop the forming hour
                candles = candles[-MAX_CANDLES_STORED:]
                if candles:
                    out[sym] = candles
            except Exception:
                continue
    except Exception:
        return out
    return out


def update_pattern_candles(prior, fetched):
    """Merge this run's fetched candles into the stored
    `pattern_candles` ({asset: [[ts, o, h, l, c], ...]}): dedup by
    timestamp, sorted, last MAX_CANDLES_STORED per symbol. Symbols
    with no fresh fetch keep their prior candles (fail-open). Returns
    a NEW dict; never raises. This is the shared store the
    manipulation track reads — one pull, two consumers, no second
    fetch."""
    new = {}
    try:
        for asset, pts in (prior or {}).items():
            try:
                new[asset] = [[p[0], p[1], p[2], p[3], p[4]]
                              for p in (pts or [])]
            except (TypeError, IndexError):
                continue
        for asset, pts in (fetched or {}).items():
            try:
                merged = {p[0]: [p[0], p[1], p[2], p[3], p[4]]
                          for p in (new.get(asset) or [])}
                for p in (pts or []):
                    merged[p[0]] = [p[0], p[1], p[2], p[3], p[4]]
                new[asset] = sorted(merged.values(),
                                    key=lambda x: x[0])[-MAX_CANDLES_STORED:]
            except (TypeError, IndexError):
                continue
    except Exception:
        return new
    return new
