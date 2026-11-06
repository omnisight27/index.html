"""Manipulation research track — Phase 1 computation (spec DRAFT
2026-11-05, ~/workspace/goals/build-out-omnisight/hidden_files/
manipulation-research-track-spec-2026-11-05.md).

DISPLAY-ONLY + SHADOW-GRADED. This module never touches a buy gate: it
does not import, call, or consult gate_math.py, gate_circuit.py, or the
60% live bar. CMI bands and confluence tags are annotations, never
inputs. No alert-tier output, no confidence influence, no trades. The
CMI > 0.50 exit trigger is a display-only exit SUGGESTION — it never
auto-closes engine plans.

What it does:
  * Our OWN CMI implementation (spec §1a — NOT the video's
    closed-source math): CMI = 1 - (S + R) / 2, where S is the
    |correlation| of detrended log-closes with their best sine fit
    (dominant cycle via zero-crossing count, period clamped [4, 40])
    and R is the R^2 of log-close on time, both over a 10-candle
    reference window, smoothed with a 5-candle moving average. High
    CMI = price follows neither its own cycle nor a predictable
    trend — a PROXY for deviation from predictable price behavior,
    not a window into institutional intent. Never claim it "detects
    institutions."
  * SuperTrend (spec §1c, TradingView defaults): ATR period 10,
    multiplier 3.0, Wilder's ATR. Bullish = close above the line.
  * Confluence (spec §1c): a SuperTrend flip with CMI < 0.30 at the
    flip close is a confluence signal; a flip with CMI >= 0.30 is a
    trap candidate, graded separately.
  * Reads the SHARED pattern_candles store (intraday.json
    ['pattern_candles'], maintained by pattern_feed.py) — one Kraken
    OHLC pull serves both tracks. This module makes NO fetch of its
    own. If pattern_candles is absent/empty, fail open (no CMI, no
    signals, no exceptions).

Conventions (conservative display-only defaults, flagged for Nic):
  * Closed 1h candles only; needs >= 20 closed candles per asset
    (10 for the window + 10 for sine-fit stability, spec §1a).
    Fewer -> asset skipped silently.
  * compute_cmi() reports the smoothed CMI (mean of the raw CMI over
    the last 5 window positions) and the S / R of the LATEST window
    position — so the reported identity CMI = 1 - (S + R) / 2 holds
    only approximately (the smoothed value averages five raw values).
    cmi_series() exposes the per-candle smoothed series the exit-
    trigger grader walks.
  * Zero-variance edge: a perfectly flat price series has no
    deviation to measure — S = 1 and R = 1 (CMI 0, CLEAN) rather
    than undefined. Real candles are never exactly flat.
  * No zero crossings in the detrended window -> no measurable cycle
    -> the sine period defaults to the 40-candle clamp (the fit then
    scores near zero, which is the honest reading: no cycle found).
  * SuperTrend needs >= 12 closed candles (11 to seed Wilder's
    ATR(10) + 1 to confirm a direction). The first trend is seeded
    from the first computable candle (close vs the lower band) — a
    known SuperTrend property: a series that starts mid-downtrend
    reads bullish until price falls a full 3x ATR below the band
    anchor. The flip, not the seed, is what confluence grades.
  * Bands (frozen, spec §1b): CLEAN < 0.30, CAUTION 0.30-0.70,
    MANIP >= 0.70. Exit trigger (frozen, spec §1b): CMI > 0.50.
  * Every rule function is pure and never raises: bad data returns
    None / no tag, never an exception.
"""

import math

MIN_CANDLES = 20        # spec §1a minimum (10 window + 10 stability)
WINDOW = 10             # sine reference period (crypto setting)
SMOOTHING = 5           # final smoothing, candles
SINE_PERIOD_MIN = 4
SINE_PERIOD_MAX = 40

BAND_CLEAN_MAX = 0.30   # CMI < 0.30 -> CLEAN
BAND_MANIP_MIN = 0.70   # CMI >= 0.70 -> MANIP
EXIT_TRIGGER = 0.50     # CMI > 0.50 -> exit suggestion (display only)

ST_ATR_PERIOD = 10      # SuperTrend ATR period (TradingView default)
ST_MULTIPLIER = 3.0     # SuperTrend multiplier (TradingView default)
ST_MIN_CANDLES = ST_ATR_PERIOD + 2  # 12: ATR seed + 1 direction candle

BANDS = ('CLEAN', 'CAUTION', 'MANIP')


def band_for(cmi):
    """'CLEAN' / 'CAUTION' / 'MANIP' for a CMI value (spec §1b
    frozen thresholds). None when the value is missing."""
    try:
        if cmi is None:
            return None
        v = float(cmi)
        if v < BAND_CLEAN_MAX:
            return 'CLEAN'
        if v < BAND_MANIP_MIN:
            return 'CAUTION'
        return 'MANIP'
    except (TypeError, ValueError):
        return None


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


def _log_closes(candles):
    """[log(close)] for parsed candles, or None when any close is
    not positive (log is undefined there)."""
    try:
        out = []
        for c in candles:
            if c[4] <= 0:
                return None
            out.append(math.log(c[4]))
        return out
    except Exception:
        return None


def _pearson(x, y):
    """Pearson correlation, or None when either series has no
    variance. Pure — never raises."""
    try:
        n = len(x)
        if n != len(y) or n < 2:
            return None
        mx = sum(x) / n
        my = sum(y) / n
        num = sum((a - mx) * (b - my) for a, b in zip(x, y))
        dx = sum((a - mx) ** 2 for a in x)
        dy = sum((b - my) ** 2 for b in y)
        if dx <= 0 or dy <= 0:
            return None
        return num / math.sqrt(dx * dy)
    except Exception:
        return None


def _linfit(y):
    """(intercept, slope) least-squares fit of y on t = 0..n-1, or
    (None, None) when degenerate. Pure — never raises."""
    try:
        n = len(y)
        if n < 2:
            return None, None
        mt = (n - 1) / 2.0
        my = sum(y) / n
        den = sum((t - mt) ** 2 for t in range(n))
        if den <= 0:
            return None, None
        slope = sum((t - mt) * (v - my) for t, v in enumerate(y)) / den
        return my - slope * mt, slope
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# §1a — CMI components (one rule function per computation)
# ---------------------------------------------------------------------------

def sine_score(log_closes):
    """|corr(detrended log-closes, best sine fit)| over the window,
    or None when the window is unusable. The dominant cycle comes
    from the zero-crossing count of the detrended series (period =
    2 * (n - 1) / crossings, clamped to [4, 40]); no crossings ->
    the 40-candle clamp (no cycle found). A flat window scores 1.0
    (no deviation to measure — see module docstring)."""
    try:
        y = list(log_closes or [])
        n = len(y)
        if n < 2:
            return None
        a, b = _linfit(y)
        if a is None:
            return None
        detrended = [v - (a + b * t) for t, v in enumerate(y)]
        if sum(v * v for v in detrended) <= 1e-18:
            return 1.0
        crossings = sum(1 for i in range(n - 1)
                        if detrended[i] * detrended[i + 1] < 0)
        if crossings > 0:
            period = 2.0 * (n - 1) / crossings
        else:
            period = float(SINE_PERIOD_MAX)
        period = max(float(SINE_PERIOD_MIN),
                     min(float(SINE_PERIOD_MAX), period))
        w = 2.0 * math.pi / period
        ss = sum(math.sin(w * t) ** 2 for t in range(n))
        cc = sum(math.cos(w * t) ** 2 for t in range(n))
        sc = sum(math.sin(w * t) * math.cos(w * t) for t in range(n))
        sy = sum(math.sin(w * t) * v for t, v in enumerate(detrended))
        cy = sum(math.cos(w * t) * v for t, v in enumerate(detrended))
        det = ss * cc - sc * sc
        if abs(det) < 1e-12:
            return 0.0
        coef_a = (sy * cc - cy * sc) / det
        coef_b = (cy * ss - sy * sc) / det
        fitted = [coef_a * math.sin(w * t) + coef_b * math.cos(w * t)
                  for t in range(n)]
        r = _pearson(detrended, fitted)
        return abs(r) if r is not None else 0.0
    except Exception:
        return None


def regression_score(log_closes):
    """R^2 of log-close on time over the window, clamped to [0, 1],
    or None when the window is unusable. A flat window scores 1.0
    (perfectly predictable — no deviation to measure)."""
    try:
        y = list(log_closes or [])
        n = len(y)
        if n < 2:
            return None
        a, b = _linfit(y)
        if a is None:
            return None
        my = sum(y) / n
        sst = sum((v - my) ** 2 for v in y)
        if sst <= 1e-18:
            return 1.0
        sse = sum((v - (a + b * t)) ** 2 for t, v in enumerate(y))
        return max(0.0, min(1.0, 1.0 - sse / sst))
    except Exception:
        return None


def _raw_cmi_at(log_closes, end_idx):
    """Raw (unsmoothed) CMI for the WINDOW ending at `end_idx`
    (inclusive), or None when the window is unusable. Returns
    (cmi, sine_score, reg_score)."""
    try:
        if end_idx < WINDOW - 1:
            return None
        window = log_closes[end_idx - WINDOW + 1:end_idx + 1]
        if len(window) != WINDOW:
            return None
        s = sine_score(window)
        r = regression_score(window)
        if s is None or r is None:
            return None
        cmi = 1.0 - (s + r) / 2.0
        return max(0.0, min(1.0, cmi)), s, r
    except Exception:
        return None


def compute_cmi(candles):
    """{cmi, sine_score, reg_score, band} for the latest closed
    candle, or None when fewer than MIN_CANDLES or the data is
    unusable. `cmi` is the 5-candle smoothed value (mean of the raw
    CMI over the last SMOOTHING window positions); `sine_score` /
    `reg_score` are the latest window's components. Pure — never
    raises."""
    try:
        c = _parse_candles(candles)
        if len(c) < MIN_CANDLES:
            return None
        logc = _log_closes(c)
        if logc is None:
            return None
        n = len(logc)
        raws = []
        for end in range(n - SMOOTHING, n):
            raw = _raw_cmi_at(logc, end)
            if raw is None:
                return None
            raws.append(raw)
        cmi = sum(r[0] for r in raws) / len(raws)
        latest = raws[-1]
        return {'cmi': round(cmi, 4),
                'sine_score': round(latest[1], 4),
                'reg_score': round(latest[2], 4),
                'band': band_for(cmi)}
    except Exception:
        return None


def cmi_series(candles):
    """[[ts_ms, cmi], ...] — the smoothed CMI ending at each candle
    from the first computable position (index MIN_CANDLES - 1)
    onward. The exit-trigger grader walks this series: a CMI > 0.50
    crossing closes the position at that candle's close. [] when
    fewer than MIN_CANDLES or the data is unusable. Pure — never
    raises."""
    out = []
    try:
        c = _parse_candles(candles)
        if len(c) < MIN_CANDLES:
            return out
        logc = _log_closes(c)
        if logc is None:
            return out
        n = len(logc)
        for end in range(MIN_CANDLES - 1, n):
            raws = []
            ok = True
            for e in range(end - SMOOTHING + 1, end + 1):
                raw = _raw_cmi_at(logc, e)
                if raw is None:
                    ok = False
                    break
                raws.append(raw[0])
            if not ok:
                continue
            out.append([int(c[end][0]) * 1000,
                        round(sum(raws) / len(raws), 4)])
        return out
    except Exception:
        return out


# ---------------------------------------------------------------------------
# §1c — SuperTrend (TradingView defaults: ATR 10 x 3.0, Wilder's ATR)
# ---------------------------------------------------------------------------

def _atr_series(candles):
    """Wilder's ATR(ST_ATR_PERIOD), aligned with `candles` (None
    until index ST_ATR_PERIOD: the first ATR is the mean of the
    first ST_ATR_PERIOD true ranges)."""
    n = len(candles)
    atr = [None] * n
    if n < ST_ATR_PERIOD + 1:
        return atr
    trs = [0.0] * n
    for i in range(1, n):
        prev_c = candles[i - 1][4]
        trs[i] = max(candles[i][2] - candles[i][3],
                     abs(candles[i][2] - prev_c),
                     abs(candles[i][3] - prev_c))
    atr[ST_ATR_PERIOD] = (sum(trs[1:ST_ATR_PERIOD + 1])
                          / ST_ATR_PERIOD)
    for i in range(ST_ATR_PERIOD + 1, n):
        atr[i] = (atr[i - 1] * (ST_ATR_PERIOD - 1) + trs[i]) \
            / ST_ATR_PERIOD
    return atr


def supertrend(candles):
    """{direction, flipped, value, atr} for the latest closed candle,
    or None when fewer than ST_MIN_CANDLES or the data is unusable.
    direction is 'bullish' (close above the line) or 'bearish';
    flipped is True when the direction changed on the latest candle.
    The trailing bands never retreat toward price (TradingView
    form): the lower band only rises while bullish, the upper only
    falls while bearish — so a gradual trend change flips the line
    once price crosses the anchored band. Pure — never raises."""
    try:
        c = _parse_candles(candles)
        if len(c) < ST_MIN_CANDLES:
            return None
        atr = _atr_series(c)
        n = len(c)
        fup = [None] * n
        fdn = [None] * n
        trend = [None] * n
        start = ST_ATR_PERIOD
        for i in range(start, n):
            if atr[i] is None or atr[i] <= 0:
                return None
            hl2 = (c[i][2] + c[i][3]) / 2.0
            bu = hl2 + ST_MULTIPLIER * atr[i]
            bd = hl2 - ST_MULTIPLIER * atr[i]
            if i == start:
                fup[i] = bu
                fdn[i] = bd
                trend[i] = ('bullish' if c[i][4] > bd else 'bearish')
            else:
                if c[i - 1][4] < fup[i - 1]:
                    fup[i] = min(bu, fup[i - 1])
                else:
                    fup[i] = bu
                if c[i - 1][4] > fdn[i - 1]:
                    fdn[i] = max(bd, fdn[i - 1])
                else:
                    fdn[i] = bd
                if trend[i - 1] == 'bullish' \
                        and c[i][4] < fdn[i - 1]:
                    trend[i] = 'bearish'
                elif trend[i - 1] == 'bearish' \
                        and c[i][4] > fup[i - 1]:
                    trend[i] = 'bullish'
                else:
                    trend[i] = trend[i - 1]
        last = n - 1
        value = fdn[last] if trend[last] == 'bullish' else fup[last]
        return {'direction': trend[last],
                'flipped': bool(trend[last] != trend[last - 1]),
                'value': round(value, 6),
                'atr': round(atr[last], 6)}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API — per-asset entry, universe scan, regime pulse
# ---------------------------------------------------------------------------

def compute_entry(candles):
    """{cmi, band, sine_score, reg_score, supertrend, supertrend_value,
    supertrend_flipped, confluence, trap} for one asset's closed 1h
    candles, or None when fewer than MIN_CANDLES or the data is
    unusable. confluence is 'LONG' / 'SHORT' when the SuperTrend
    flipped on the latest candle AND CMI < 0.30 (spec §1c); trap is
    True when it flipped with CMI >= 0.30 (a trap candidate, graded
    separately — never a signal). Pure — never raises."""
    try:
        cm = compute_cmi(candles)
        st = supertrend(candles)
        if cm is None or st is None:
            return None
        cmi = cm['cmi']
        confluence = None
        trap = False
        if st['flipped']:
            if cmi < BAND_CLEAN_MAX:
                confluence = ('LONG' if st['direction'] == 'bullish'
                              else 'SHORT')
            else:
                trap = True
        return {'cmi': cm['cmi'],
                'band': cm['band'],
                'sine_score': cm['sine_score'],
                'reg_score': cm['reg_score'],
                'supertrend': st['direction'],
                'supertrend_value': st['value'],
                'supertrend_flipped': st['flipped'],
                'confluence': confluence,
                'trap': trap}
    except Exception:
        return None


def detect_for_universe(pattern_candles):
    """{asset: entry} for a pattern_candles store (the shared Kraken
    OHLC store — this module fetches nothing). One bad symbol never
    blocks the rest. Pure — never raises."""
    out = {}
    try:
        for asset, candles in (pattern_candles or {}).items():
            try:
                entry = compute_entry(candles)
            except Exception:
                entry = None
            if entry is not None:
                out[asset] = entry
    except Exception:
        return out
    return out


def regime_pulse(cmi_map):
    """Aggregate regime pulse (spec §3): {total, counts, clean_pct,
    caution_pct, manip_pct} — the share of scanned assets per band.
    All zero when nothing was scanned. Pure — never raises."""
    try:
        counts = {b: 0 for b in BANDS}
        for entry in (cmi_map or {}).values():
            b = (entry or {}).get('band')
            if b in counts:
                counts[b] += 1
        total = sum(counts.values())
        if not total:
            return {'total': 0, 'counts': counts,
                    'clean_pct': 0.0, 'caution_pct': 0.0,
                    'manip_pct': 0.0}
        return {'total': total, 'counts': counts,
                'clean_pct': round(counts['CLEAN'] / total * 100, 1),
                'caution_pct': round(counts['CAUTION'] / total * 100, 1),
                'manip_pct': round(counts['MANIP'] / total * 100, 1)}
    except Exception:
        return {'total': 0, 'counts': {b: 0 for b in BANDS},
                'clean_pct': 0.0, 'caution_pct': 0.0, 'manip_pct': 0.0}


_BAND_WORDS = {'CLEAN': 'Clean', 'CAUTION': 'Caution', 'MANIP': 'Manipulated'}


def pulse_line(cmi_map):
    """One plain-words line for the Picks page, e.g. "62% clean / 28%
    caution / 10% manipulated (29 assets)". '' when nothing was
    scanned. Pure — never raises."""
    try:
        pulse = regime_pulse(cmi_map)
        if not pulse['total']:
            return ''
        return (f"{pulse['clean_pct']}% clean / "
                f"{pulse['caution_pct']}% caution / "
                f"{pulse['manip_pct']}% manipulated "
                f"({pulse['total']} assets)")
    except Exception:
        return ''


def entry_line(asset, entry):
    """One plain-words line for a card, e.g. "BAT — CMI 0.18 (Clean)
    · SuperTrend bullish". '' when there is no entry. Pure — never
    raises."""
    try:
        if not entry:
            return ''
        band = _BAND_WORDS.get(entry.get('band'), 'no data')
        line = (f"{asset} — CMI {entry.get('cmi')} ({band}) · "
                f"SuperTrend {entry.get('supertrend')}")
        if entry.get('confluence'):
            line += f" · confluence {entry['confluence']}"
        elif entry.get('trap'):
            line += ' · SuperTrend flip in caution — trap watch'
        return line
    except Exception:
        return ''
