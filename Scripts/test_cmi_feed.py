"""Manipulation research track, Phase 1 — CMI + SuperTrend tests.

Covers the spec §1 definitions with synthetic fixtures: clean sine +
trend = low CMI (CLEAN), iid noise = high CMI (no cycle, no trend to
fit), random-walk distribution sits above the clean fixture, known
SuperTrend flips (up-then-down, sharp drop, steady trends), and the
band edges 0.30 / 0.50 / 0.70. Also the fail-open rules (>= 20 candles,
never raises on garbage) and the shared-store scan (no fetch of its
own — detect_for_universe reads pattern_candles only).

Run: python3 -m pytest Scripts/test_cmi_feed.py
"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cmi_feed


def _candles(closes, base_ts=1700000000, spread=0.005):
    """[ts, o, h, l, c] closed hourly candles from a close series."""
    out = []
    for i, c in enumerate(closes):
        out.append([base_ts + i * 3600, c, c * (1 + spread),
                    c * (1 - spread), c])
    return out


def _sine_trend_closes(n=30):
    """Strong trend + small period-10 sine: both models fit, so CMI
    must read CLEAN."""
    return [100 * math.exp(0.01 * t + 0.005 * math.sin(2 * math.pi * t / 10))
            for t in range(n)]


def _noise_closes(seed, n=30):
    """Iid lognormal closes: no cycle to fit, no trend to fit."""
    rng = random.Random(seed)
    return [100 * math.exp(rng.gauss(0, 0.02)) for _ in range(n)]


def _walk_closes(seed, n=30):
    rng = random.Random(seed)
    p = 100.0
    out = []
    for _ in range(n):
        p *= math.exp(rng.gauss(0, 0.01))
        out.append(p)
    return out


# ---------------------------------------------------------------- band edges

def test_band_edges():
    assert cmi_feed.band_for(0.0) == 'CLEAN'
    assert cmi_feed.band_for(0.2999) == 'CLEAN'
    assert cmi_feed.band_for(0.30) == 'CAUTION'
    assert cmi_feed.band_for(0.50) == 'CAUTION'
    assert cmi_feed.band_for(0.6999) == 'CAUTION'
    assert cmi_feed.band_for(0.70) == 'MANIP'
    assert cmi_feed.band_for(1.0) == 'MANIP'
    assert cmi_feed.band_for(None) is None
    assert cmi_feed.band_for('junk') is None
    assert cmi_feed.EXIT_TRIGGER == 0.50


# ---------------------------------------------------------------- sine score

def test_sine_score_clean_sine_near_one():
    logc = [math.log(c) for c in _sine_trend_closes()]
    # Detrended window is a near-pure period-10 sine. The spec's
    # zero-crossing period estimator is coarse on a 10-candle window
    # (1 crossing -> period 18 vs the true 10), so the fit reads
    # ~0.82, not ~1.0 — that coarseness is the frozen method's own
    # property, and the CMI still lands CLEAN (see compute_cmi).
    s = cmi_feed.sine_score(logc[-10:])
    assert s is not None and s > 0.8


def test_sine_score_noise_below_clean_on_average():
    # A 2-parameter sine fit to 10 noise points can correlate by
    # chance (one seed reads 0.79), so S alone is not the
    # discriminator for noise — the R^2 leg is. What must hold is
    # that noise fits worse than a true cycle on average.
    clean = cmi_feed.sine_score(
        [math.log(c) for c in _sine_trend_closes()][-10:])
    scores = [cmi_feed.sine_score([math.log(c) for c in _noise_closes(s)][-10:])
              for s in (42, 1, 7, 123, 99)]
    assert sum(scores) / len(scores) < clean


def test_sine_score_flat_is_one():
    assert cmi_feed.sine_score([math.log(100.0)] * 10) == 1.0


def test_sine_score_never_raises():
    assert cmi_feed.sine_score(None) is None
    assert cmi_feed.sine_score([]) is None
    assert cmi_feed.sine_score([1.0]) is None


# ---------------------------------------------------------------- regression score

def test_regression_score_pure_trend_near_one():
    logc = [math.log(100 * math.exp(0.01 * t)) for t in range(10)]
    assert cmi_feed.regression_score(logc) > 0.99


def test_regression_score_noise_low():
    scores = [cmi_feed.regression_score([math.log(c) for c in _noise_closes(s)][-10:])
              for s in (42, 1, 7)]
    assert all(v is not None and v < 0.5 for v in scores)


def test_regression_score_flat_is_one():
    assert cmi_feed.regression_score([math.log(100.0)] * 10) == 1.0


# ---------------------------------------------------------------- compute_cmi

def test_compute_cmi_clean_sine_trend():
    res = cmi_feed.compute_cmi(_candles(_sine_trend_closes()))
    assert res is not None
    assert res['cmi'] < 0.30
    assert res['band'] == 'CLEAN'
    assert res['sine_score'] > 0.8
    assert 0.0 <= res['cmi'] <= 1.0


def test_compute_cmi_pure_trend_zero():
    closes = [100 * math.exp(0.01 * t) for t in range(30)]
    res = cmi_feed.compute_cmi(_candles(closes))
    assert res is not None and res['cmi'] < 0.05
    assert res['band'] == 'CLEAN'


def test_compute_cmi_noise_high():
    for seed in (42, 1, 7):
        res = cmi_feed.compute_cmi(_candles(_noise_closes(seed)))
        assert res is not None
        assert res['cmi'] > 0.50, (seed, res)
        assert res['band'] in ('CAUTION', 'MANIP')


def test_compute_cmi_random_walk_above_clean_on_average():
    clean = cmi_feed.compute_cmi(_candles(_sine_trend_closes()))['cmi']
    walks = [cmi_feed.compute_cmi(_candles(_walk_closes(s)))['cmi']
             for s in range(20)]
    assert sum(walks) / len(walks) > clean + 0.2


def test_compute_cmi_needs_20_candles():
    assert cmi_feed.compute_cmi(_candles(_sine_trend_closes(19))) is None
    assert cmi_feed.compute_cmi(_candles(_sine_trend_closes(20))) is not None
    assert cmi_feed.compute_cmi([]) is None
    assert cmi_feed.compute_cmi(None) is None


def test_compute_cmi_never_raises_on_garbage():
    assert cmi_feed.compute_cmi([['x', 'y']]) is None
    assert cmi_feed.compute_cmi([[1, 2, 3]]) is None  # short row
    assert cmi_feed.compute_cmi([[1, 0, 0, 0, 0]] * 30) is None  # log(0)


def test_cmi_series_shape_and_monotone_ts():
    series = cmi_feed.cmi_series(_candles(_sine_trend_closes(30)))
    assert len(series) == 11  # indices 19..29
    assert all(series[i][0] < series[i + 1][0] for i in range(len(series) - 1))
    assert all(0.0 <= v <= 1.0 for _, v in series)
    assert series[-1][1] == cmi_feed.compute_cmi(
        _candles(_sine_trend_closes(30)))['cmi']
    assert cmi_feed.cmi_series(_candles(_sine_trend_closes(19))) == []
    assert cmi_feed.cmi_series(None) == []


# ---------------------------------------------------------------- SuperTrend

def test_supertrend_steady_uptrend_bullish():
    closes = [100 + i * 0.5 for i in range(30)]
    st = cmi_feed.supertrend(_candles(closes))
    assert st is not None
    assert st['direction'] == 'bullish'
    assert st['flipped'] is False
    assert st['value'] < closes[-1]  # line trails below price
    assert st['atr'] > 0


def test_supertrend_up_then_down_flips_bearish():
    closes = [100 + i * 0.5 for i in range(15)] + \
             [107.5 - i * 1.0 for i in range(15)]
    st = cmi_feed.supertrend(_candles(closes))
    assert st is not None
    assert st['direction'] == 'bearish'
    # The flip fired mid-series (candle 19): truncate the series at
    # the flip candle and the latest-candle flag must read True.
    st_flip = cmi_feed.supertrend(_candles(closes[:20]))
    assert st_flip is not None
    assert st_flip['direction'] == 'bearish'
    assert st_flip['flipped'] is True


def test_supertrend_sharp_drop_flips_on_the_drop_candle():
    closes = [100.0] * 15 + [92.0] + [92.0] * 14
    st = cmi_feed.supertrend(_candles(closes))
    assert st is not None
    assert st['direction'] == 'bearish'
    # The flip fired on the drop candle (index 15): truncate there.
    st_flip = cmi_feed.supertrend(_candles(closes[:16]))
    assert st_flip is not None
    assert st_flip['direction'] == 'bearish'
    assert st_flip['flipped'] is True


def test_supertrend_steady_downtrend_ends_bearish():
    closes = [100 - i * 0.5 for i in range(30)]
    st = cmi_feed.supertrend(_candles(closes))
    assert st is not None
    assert st['direction'] == 'bearish'


def test_supertrend_flip_is_on_latest_candle_only():
    # Flip happened, then price kept falling: the flip flag must be
    # False once the flip candle is no longer the latest.
    closes = [100 + i * 0.5 for i in range(15)] + \
             [107.5 - i * 1.0 for i in range(25)]
    st = cmi_feed.supertrend(_candles(closes))
    assert st is not None
    assert st['direction'] == 'bearish'
    assert st['flipped'] is False


def test_supertrend_needs_12_candles():
    assert cmi_feed.supertrend(_candles([100.0] * 11)) is None
    assert cmi_feed.supertrend(_candles([100.0] * 12)) is not None
    assert cmi_feed.supertrend(None) is None
    assert cmi_feed.supertrend([]) is None


# ---------------------------------------------------------------- entry / universe / pulse

def test_compute_entry_confluence_on_clean_flip():
    # Clean sine+trend candles (CMI < 0.30) with a sharp drop on the
    # last candle: SuperTrend flips bearish on a CLEAN market.
    closes = _sine_trend_closes(29) + [_sine_trend_closes(29)[-1] * 0.90]
    entry = cmi_feed.compute_entry(_candles(closes))
    assert entry is not None
    assert entry['band'] == 'CLEAN'
    assert entry['supertrend'] == 'bearish'
    assert entry['supertrend_flipped'] is True
    assert entry['confluence'] == 'SHORT'
    assert entry['trap'] is False


def test_compute_entry_trap_on_noisy_flip():
    # Noisy candles (CMI >= 0.30) with a sharp drop: the flip is a
    # trap candidate, never a confluence signal.
    closes = _noise_closes(42, 29) + [_noise_closes(42, 29)[-1] * 0.90]
    entry = cmi_feed.compute_entry(_candles(closes))
    assert entry is not None
    assert entry['band'] in ('CAUTION', 'MANIP')
    assert entry['supertrend_flipped'] is True
    assert entry['confluence'] is None
    assert entry['trap'] is True


def test_compute_entry_no_flip_no_signal():
    entry = cmi_feed.compute_entry(_candles(_sine_trend_closes()))
    assert entry is not None
    assert entry['confluence'] is None
    assert entry['trap'] is False


def test_compute_entry_needs_20_candles():
    assert cmi_feed.compute_entry(_candles(_sine_trend_closes(19))) is None


def test_detect_for_universe_reads_shared_store_only():
    store = {'BAT': _candles(_sine_trend_closes()),
             'JUNK': [['x']],
             'ETH': _candles(_noise_closes(42))}
    out = cmi_feed.detect_for_universe(store)
    assert set(out) == {'BAT', 'ETH'}  # bad symbol skipped, rest kept
    assert out['BAT']['band'] == 'CLEAN'
    assert detect_has_no_fetch()
    assert cmi_feed.detect_for_universe(None) == {}
    assert cmi_feed.detect_for_universe({}) == {}


def detect_has_no_fetch():
    """This module defines no fetch function at all — one Kraken OHLC
    pull (pattern_feed) serves both tracks. Guard against a second
    fetch creeping in."""
    import inspect
    src = inspect.getsource(cmi_feed)
    assert 'api.kraken.com' not in src
    assert 'fetch_ohlc' not in src
    assert 'urllib' not in src
    return True


def test_regime_pulse_shares():
    pulse = cmi_feed.regime_pulse({
        'A': {'band': 'CLEAN'}, 'B': {'band': 'CLEAN'},
        'C': {'band': 'CAUTION'}, 'D': {'band': 'MANIP'},
        'E': {'band': 'nonsense'}, 'F': {}})
    assert pulse['total'] == 4
    assert pulse['counts'] == {'CLEAN': 2, 'CAUTION': 1, 'MANIP': 1}
    assert pulse['clean_pct'] == 50.0
    assert pulse['caution_pct'] == 25.0
    assert pulse['manip_pct'] == 25.0
    assert cmi_feed.pulse_line({'A': {'band': 'CLEAN'}}) == \
        '100.0% clean / 0.0% caution / 0.0% manipulated (1 assets)'
    assert cmi_feed.pulse_line({}) == ''
    assert cmi_feed.pulse_line(None) == ''
    empty = cmi_feed.regime_pulse({})
    assert empty['total'] == 0 and empty['clean_pct'] == 0.0


def test_entry_line_plain_words():
    entry = {'cmi': 0.18, 'band': 'CLEAN', 'supertrend': 'bullish',
             'confluence': None, 'trap': False}
    assert cmi_feed.entry_line('BAT', entry) == \
        'BAT — CMI 0.18 (Clean) · SuperTrend bullish'
    conf = dict(entry, confluence='LONG')
    assert 'confluence LONG' in cmi_feed.entry_line('BAT', conf)
    trap = dict(entry, band='CAUTION', cmi=0.45, trap=True)
    assert 'trap watch' in cmi_feed.entry_line('BAT', trap)
    assert cmi_feed.entry_line('BAT', None) == ''
