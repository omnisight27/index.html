"""Precedent / analogue model — tests.

Run: python3 -m pytest Scripts/test_precedent.py
All fixtures are synthetic (no network). The real-history sample read is
a separate demo run, not part of this suite.
"""
import sys
import os
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import precedent as P


# ---------------------------------------------------------------------------
# Fixtures — synthetic closes
# ---------------------------------------------------------------------------

def _closes(start, days, daily_ret):
    """[(date, close)] rising/falling at a fixed daily return."""
    out = []
    px = 100.0
    for i in range(days):
        out.append((start + timedelta(days=i), round(px, 4)))
        px *= (1.0 + daily_ret)
    return out


def _lib_from_closes(closes, cycle_states=None):
    return P.build_library(closes, cycle_states=cycle_states or {})


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_closes_accepts_dict_and_rows():
    d = P.parse_closes({'2021-01-01': 100.0, '2021-01-02': 101.0})
    assert d == [(date(2021, 1, 1), 100.0), (date(2021, 1, 2), 101.0)]
    rows = P.parse_closes([[1609459200, 100.0], [1609545600, 101.0]])
    assert len(rows) == 2 and rows[0][1] == 100.0
    assert P.parse_closes(None) == []
    assert P.parse_closes([['bad', 'nope'], [1609459200, -5]]) == []


def test_parse_day_variants():
    assert P._parse_day('2021-03-04') == date(2021, 3, 4)
    assert P._parse_day(date(2021, 3, 4)) == date(2021, 3, 4)
    assert P._parse_day(1609459200) == date(2021, 1, 1)
    assert P._parse_day('garbage') is None


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def test_fingerprint_needs_180_closes():
    closes = _closes(date(2021, 1, 1), 179, 0.001)
    assert P.fingerprint_for_day(date(2021, 6, 28), closes) is None
    closes = _closes(date(2021, 1, 1), 180, 0.001)
    fp = P.fingerprint_for_day(date(2021, 6, 29), closes)
    assert fp is not None


def test_fingerprint_momentum_dims_known_values():
    # Steady +0.1%/day: r1d 0.1, r7d ~0.702, r30d ~3.045.
    closes = _closes(date(2021, 1, 1), 200, 0.001)
    fp = P.fingerprint_for_day(date(2021, 7, 19), closes)
    assert fp['r1d'] == pytest.approx(0.1, abs=0.01)
    assert fp['r7d'] == pytest.approx(0.702, abs=0.01)
    assert fp['r30d'] == pytest.approx(3.045, abs=0.02)
    # Rising every day: drawdown 0, vol ~0 (constant log returns).
    assert fp['dd'] == pytest.approx(0.0, abs=0.01)
    assert fp['vol30'] == pytest.approx(0.0, abs=0.01)
    # 2021-07-19: days since 2020-05-11 halving = 434 -> 434/1460.
    assert fp['halving'] == pytest.approx(434 / 1460.0, abs=0.005)
    # July: sin(2*pi*6/12)=0, cos=-1.
    assert fp['month_sin'] == pytest.approx(0.0, abs=0.01)
    assert fp['month_cos'] == pytest.approx(-1.0, abs=0.01)
    assert 0.0 <= fp['coil'] <= 1.0


def test_fingerprint_missing_fng_is_none_not_faked():
    closes = _closes(date(2021, 1, 1), 200, 0.001)
    fp = P.fingerprint_for_day(date(2021, 7, 19), closes)
    assert fp['fng'] is None  # no cycle state, no fng argument
    fp2 = P.fingerprint_for_day(date(2021, 7, 19), closes,
                                cycle_state={'fng_value': 32,
                                             'halving_day': 434})
    assert fp2['fng'] == 32.0
    assert fp2['halving'] == pytest.approx(434 / 1460.0, abs=0.005)


def test_fingerprint_drawdown_and_volatility():
    # Fall 20% over 30 days then flat: dd ~20, vol30 > 0.
    closes = []
    px = 100.0
    start = date(2021, 1, 1)
    for i in range(200):
        closes.append((start + timedelta(days=i), round(px, 4)))
        if 150 <= i < 180:
            px *= 0.992  # ~-0.8%/day for 30 days
    fp = P.fingerprint_for_day(start + timedelta(days=199), closes)
    assert fp['dd'] == pytest.approx(20.0, abs=2.0)
    assert fp['vol30'] > 0.0
    assert fp['r30d'] < 0


def test_month_sincos_december_january_neighbours():
    def chord(a, b):
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
    dec = P._month_sincos(date(2021, 12, 15))
    jan = P._month_sincos(date(2022, 1, 15))
    jul = P._month_sincos(date(2021, 7, 15))
    # Adjacent months on the circle: close together (30 deg apart).
    assert chord(dec, jan) < 0.6
    # December vs July: 150 deg apart on the circle, far.
    assert chord(dec, jul) > 1.5


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

def test_build_library_starts_after_180_closes():
    closes = _closes(date(2021, 1, 1), 200, 0.001)
    lib = _lib_from_closes(closes)
    # Fingerprints from day 180 (index 179) onward: 200 - 179 = 21 days.
    assert len(lib['dates']) == 21
    assert lib['dates'][0] == '2021-06-29'
    assert set(lib['stats'].keys()) == set(P.FINGERPRINT_DIMS) - {'fng'}
    # fng has no values at all here -> no stats entry (dim masked).
    assert 'fng' not in lib['stats']
    assert lib['asset'] == 'BTC'


def test_build_library_empty_input():
    lib = _lib_from_closes([])
    assert lib['dates'] == [] and lib['fingerprints'] == {}


# ---------------------------------------------------------------------------
# Distance
# ---------------------------------------------------------------------------

def test_distance_identical_is_zero_and_missing_dims_masked():
    stats = {d: (0.0, 1.0) for d in P.FINGERPRINT_DIMS}
    vec = {d: 1.0 for d in P.FINGERPRINT_DIMS}
    assert P.distance(vec, vec, stats) == 0.0
    # One dim differs by 2 sigma -> distance 2.0.
    vec2 = dict(vec)
    vec2['r1d'] = 3.0
    assert P.distance(vec, vec2, stats) == pytest.approx(2.0)
    # Missing fng in one vector: masked, distance over the other 9 dims.
    vec3 = dict(vec)
    vec3['fng'] = None
    d = P.distance(vec, vec3, stats)
    assert d == pytest.approx(0.0)
    # No shared dims -> None.
    assert P.distance({'r1d': None}, {'r1d': 1.0}, stats) is None


# ---------------------------------------------------------------------------
# Matching — planted analogues in a two-regime synthetic series
# ---------------------------------------------------------------------------

def _two_regime_lib():
    """1600 days spanning the 2020-05-11 halving reset, regimes
    alternating +0.3%/day and -0.3%/day in 200-day blocks."""
    start = date(2019, 1, 1)
    closes = []
    px = 100.0
    for i in range(1600):
        closes.append((start + timedelta(days=i), round(px, 4)))
        px *= (1.003 if (i // 200) % 2 == 0 else 0.997)
    return _lib_from_closes(closes), closes


def _is_falling_day(d):
    """True when date d falls in a -0.3%/day block of _two_regime_lib."""
    i = (d - date(2019, 1, 1)).days
    return (i // 200) % 2 == 1


def test_matching_finds_same_regime_analogues():
    lib, _ = _two_regime_lib()
    # Query: a falling-regime day late in the series.
    qdate = date(2023, 1, 15)
    qvec = lib['fingerprints'][qdate.isoformat()]
    matches = P.find_matches(lib, qvec, qdate, horizon_days=7)
    assert len(matches) >= P.MIN_N
    # All matches must be falling-regime days.
    assert all(_is_falling_day(P._parse_day(d)) for d, _ in matches)
    # Nearest first, all within the distance cap.
    dists = [dist for _, dist in matches]
    assert dists == sorted(dists)
    assert max(dists) <= P.MAX_DIST


def test_matching_thinning_enforces_gap():
    lib, _ = _two_regime_lib()
    qdate = date(2023, 1, 15)
    qvec = lib['fingerprints'][qdate.isoformat()]
    matches = P.find_matches(lib, qvec, qdate, horizon_days=7)
    ds = [P._parse_day(d) for d, _ in matches]
    for i in range(len(ds)):
        for j in range(i + 1, len(ds)):
            assert abs((ds[i] - ds[j]).days) >= P.MIN_GAP_DAYS


def test_matching_horizon_eligibility_no_lookahead():
    lib, _ = _two_regime_lib()
    qdate = date(2022, 1, 15)
    qvec = lib['fingerprints'][qdate.isoformat()]
    # 30d horizon: no match within 30 days of the query.
    matches = P.find_matches(lib, qvec, qdate, horizon_days=30)
    assert all((qdate - P._parse_day(d)).days >= 30 for d, _ in matches)
    # A query 16 days after the first fingerprint day: every earlier
    # fingerprint is inside the 30d window, so no 30d match is eligible.
    lib2, _ = _two_regime_lib()
    early = date(2019, 7, 15)  # first fingerprint day is 2019-06-29
    qvec2 = lib2['fingerprints'][early.isoformat()]
    assert P.find_matches(lib2, qvec2, early, horizon_days=30) == []


def test_matching_max_distance_caps():
    lib, _ = _two_regime_lib()
    qdate = date(2022, 1, 15)
    qvec = lib['fingerprints'][qdate.isoformat()]
    assert P.find_matches(lib, qvec, qdate, horizon_days=7,
                          max_dist=0.0) == []


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def test_read_steady_rise_is_long_everywhere():
    # Steady +0.2%/day for 1600 days: every forward window rises.
    closes = _closes(date(2019, 1, 1), 1600, 0.002)
    lib = _lib_from_closes(closes)
    qdate = date(2023, 1, 15)
    qvec = lib['fingerprints'][qdate.isoformat()]
    read = P.precedent_read(lib, qvec, qdate)
    assert read['asset'] == 'BTC' and read['date'] == '2023-01-15'
    for label in ('1d', '7d', '30d'):
        hz = read['horizons'][label]
        assert hz['precedent'] is True
        assert hz['up_rate'] == 1.0
        assert hz['direction'] == 'LONG'
        assert hz['median_fwd'] > 0
    # 30d forward +6.2% touches +4.2% every time; 1d never does.
    assert read['horizons']['30d']['p_fav'] == 1.0
    assert read['horizons']['1d']['p_fav'] == 0.0
    assert read['horizons']['1d']['p_adv'] == 0.0
    lines = P.plain_words(read)
    assert len(lines) == 3
    assert 'rose' in lines[1] and 'times' in lines[1]


def test_read_no_precedent_when_too_few_matches():
    closes = _closes(date(2021, 1, 1), 200, 0.001)
    lib = _lib_from_closes(closes)
    # Query the second fingerprint day: only 1 earlier day exists.
    qdate = date(2021, 6, 30)
    qvec = lib['fingerprints'][qdate.isoformat()]
    read = P.precedent_read(lib, qvec, qdate)
    for label in ('1d', '7d', '30d'):
        hz = read['horizons'][label]
        assert hz['precedent'] is False
        assert hz['direction'] == 'FLAT'
        assert hz['up_rate'] is None
    assert 'no close precedent' in P.plain_words(read)[0]


def test_forward_outcome_touch_rules():
    closes = {'2021-01-01': 100.0, '2021-01-02': 105.0,
              '2021-01-03': 103.0, '2021-01-08': 110.0}
    oc = P.forward_outcome(closes, '2021-01-01', 7)
    assert oc['fwd_pct'] == pytest.approx(10.0)
    assert oc['touched'] == 'fav'  # +5% on day 2 crosses +4.2% first
    closes2 = {'2021-01-01': 100.0, '2021-01-02': 97.0,
               '2021-01-03': 103.0, '2021-01-08': 110.0}
    oc2 = P.forward_outcome(closes2, '2021-01-01', 7)
    assert oc2['touched'] == 'adv'  # -3% on day 2 crosses -2.1% first
    # Window not fully in data -> None.
    assert P.forward_outcome(closes, '2021-01-08', 7) is None


def test_wilson_lower_information_only():
    assert P.wilson_lower(0, 0) is None
    assert P.wilson_lower(10, 10) == pytest.approx(0.722, abs=0.01)
    assert P.wilson_lower(5, 10) == pytest.approx(0.237, abs=0.01)
    assert P.wilson_lower(0, 10) == 0.0


# ---------------------------------------------------------------------------
# Record + grading
# ---------------------------------------------------------------------------

def test_record_shape_and_context():
    closes = _closes(date(2019, 1, 1), 1600, 0.002)
    lib = _lib_from_closes(closes)
    qdate = date(2023, 1, 15)
    qvec = lib['fingerprints'][qdate.isoformat()]
    read = P.precedent_read(lib, qvec, qdate)
    ctx = {'funding_score': -0.4, 'oi_score': 0.2, 'dom_change': 1.5,
           'chg_1h': 0.8, 'balance_score': 0.65}
    rec = P.precedent_record('BTC', qdate, qvec, read, context=ctx)
    assert rec['kind'] == 'precedent'
    assert rec['asset'] == 'BTC'
    assert rec['context'] == ctx
    assert set(rec['horizons'].keys()) == {'1d', '7d', '30d'}
    assert rec['horizons']['7d']['direction'] == 'LONG'
    assert (len(rec['horizons']['7d']['match_dates'])
            == read['horizons']['7d']['n'])


def test_grade_record_win_loss_plan_expired():
    closes = _closes(date(2019, 1, 1), 1600, 0.002)
    lib = _lib_from_closes(closes)
    qdate = date(2023, 1, 15)
    qvec = lib['fingerprints'][qdate.isoformat()]
    read = P.precedent_read(lib, qvec, qdate)
    rec = P.precedent_record('BTC', qdate, qvec, read)
    # Grade against the same closes (windows fully closed).
    closes_map = {d.isoformat(): c for d, c in closes}
    graded = P.grade_precedent_record(rec, closes_map)
    assert graded['1d']['graded'] is True
    assert graded['1d']['outcome'] == 'win'      # LONG, price rose
    assert graded['1d']['plan'] == 'EXPIRED'     # +0.2% touches neither
    assert graded['30d']['plan'] == 'WIN'        # +6.2% touches +4.2%
    # A SHORT record on the same rising closes loses.
    rec2 = P.precedent_record('BTC', qdate, qvec, read)
    rec2['horizons']['7d']['direction'] = 'SHORT'
    graded2 = P.grade_precedent_record(rec2, closes_map)
    assert graded2['7d']['outcome'] == 'loss'
    # FLAT / no-precedent horizons are not graded.
    rec3 = {'kind': 'precedent', 'asset': 'BTC', 'date': '2022-01-15',
            'horizons': {'7d': {'precedent': False, 'direction': 'FLAT'}}}
    assert P.grade_precedent_record(rec3, closes_map)['7d'] == {
        'graded': False, 'reason': 'no precedent / FLAT direction'}
    # Window not yet closed -> pending. The 30d window ends at index
    # ~1505, so a map cut at 1500 leaves it open (1d/7d still grade).
    short_map = {d.isoformat(): c for d, c in closes[:1500]}
    assert P.grade_precedent_record(rec, short_map)['30d'] == {
        'graded': False, 'reason': 'pending'}
    assert P.grade_precedent_record(rec, short_map)['7d']['graded'] is True


def test_grade_never_raises_on_garbage():
    assert P.grade_precedent_record(None, {}) == {}
    assert P.grade_precedent_record({'kind': 'precedent'}, None) == {}
    empty = P.precedent_read(None, None, '2022-01-15')
    assert all(not hz['precedent']
               for hz in empty['horizons'].values())
    # plain_words on a failed read degrades to no-precedent lines.
    assert P.plain_words(None) == [
        '1d: no close precedent (0 matches found).',
        '7d: no close precedent (0 matches found).',
        '30d: no close precedent (0 matches found).']
