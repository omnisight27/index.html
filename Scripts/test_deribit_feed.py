"""Deribit options feed. Pure-function tests + fake-fetch injection;
no disk, no network."""
from datetime import date

import pytest

import deribit_feed

TODAY = date(2026, 11, 7)
EXP_30D = date(2026, 12, 7)   # exactly 30 days after TODAY

# --- Saved sample response (real Deribit shape, probed 2026-11-07) ---

SAMPLE_BOOK = {
    'jsonrpc': '2.0',
    'result': [
        {'instrument_name': 'BTC-25DEC26-104000-C', 'mark_iv': 43.13,
         'open_interest': 183.1, 'mark_price': 0.00165584,
         'bid_price': 0.0016, 'ask_price': 0.0018, 'volume': 0.3,
         'underlying_price': 76412.84, 'interest_rate': 0.02,
         'base_currency': 'BTC', 'quote_currency': 'USD'},
        {'instrument_name': 'BTC-25DEC26-104000-P', 'mark_iv': 44.02,
         'open_interest': 210.5, 'mark_price': 0.0021,
         'bid_price': 0.002, 'ask_price': 0.0022, 'volume': 0.1,
         'underlying_price': 76412.84, 'interest_rate': 0.02},
        # Bad instrument name -> dropped
        {'instrument_name': 'BTC-PERPETUAL', 'mark_iv': 40.0,
         'open_interest': 5.0, 'underlying_price': 76412.84},
        # Zero IV -> dropped
        {'instrument_name': 'BTC-25DEC26-90000-C', 'mark_iv': 0.0,
         'open_interest': 5.0, 'underlying_price': 76412.84},
        # Missing OI -> dropped
        {'instrument_name': 'BTC-25DEC26-90000-P', 'mark_iv': 41.0,
         'underlying_price': 76412.84},
    ],
    'usIn': 1794050000000000,
    'usOut': 1794050000000000,
    'usDiff': 100,
}


def _opt(kind, strike, iv, oi, expiry=EXP_30D, rate=0.0, under=100.0):
    """One parsed option row (the shape parse_book produces)."""
    return {'currency': 'BTC', 'expiry': expiry, 'strike': float(strike),
            'kind': kind, 'iv': float(iv), 'oi': float(oi),
            'mark_price': 0.01, 'underlying': under, 'rate': rate}


def _fake_fetch(mapping):
    """fetch_json stub: URL substring -> canned response (or raise)."""
    calls = []

    def fetch(url):
        calls.append(url)
        for needle, resp in mapping.items():
            if needle in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return None

    fetch.calls = calls
    return fetch


# --- Parsers ---

def test_parse_instrument():
    assert deribit_feed.parse_instrument('BTC-25DEC26-104000-C') == (
        'BTC', date(2026, 12, 25), 104000.0, 'C')
    assert deribit_feed.parse_instrument('ETH-25JUN27-2900-P') == (
        'ETH', date(2027, 6, 25), 2900.0, 'P')
    assert deribit_feed.parse_instrument('BTC-PERPETUAL') is None
    assert deribit_feed.parse_instrument('') is None
    assert deribit_feed.parse_instrument(None) is None
    assert deribit_feed.parse_instrument('BTC-99XXX99-100-C') is None


def test_parse_book_keeps_only_good_rows():
    opts = deribit_feed.parse_book(SAMPLE_BOOK)
    assert len(opts) == 2
    assert opts[0]['currency'] == 'BTC'
    assert opts[0]['expiry'] == date(2026, 12, 25)
    assert opts[0]['strike'] == pytest.approx(104000.0)
    assert opts[0]['iv'] == pytest.approx(43.13)
    assert opts[0]['oi'] == pytest.approx(183.1)
    assert opts[0]['underlying'] == pytest.approx(76412.84)
    assert deribit_feed.parse_book(None) == []
    assert deribit_feed.parse_book({}) == []
    assert deribit_feed.parse_book({'result': 'nope'}) == []


def test_underlying_price_median():
    opts = deribit_feed.parse_book(SAMPLE_BOOK)
    assert deribit_feed.underlying_price(opts) == pytest.approx(76412.84)
    assert deribit_feed.underlying_price([]) is None


# --- Expiry selection ---

def test_nearest_expiry():
    opts = [
        _opt('C', 100, 50, 1, expiry=date(2026, 11, 17)),  # 10d
        _opt('C', 100, 50, 1, expiry=date(2026, 12, 2)),   # 25d
        _opt('C', 100, 50, 1, expiry=date(2026, 12, 17)),  # 40d
    ]
    assert deribit_feed.nearest_expiry(opts, 30, TODAY) == date(2026, 12, 2)
    # Tie (20d vs 40d) breaks toward the later expiry
    opts = [
        _opt('C', 100, 50, 1, expiry=date(2026, 11, 27)),  # 20d
        _opt('C', 100, 50, 1, expiry=date(2026, 12, 17)),  # 40d
    ]
    assert deribit_feed.nearest_expiry(opts, 30, TODAY) == date(2026, 12, 17)
    # Expired options are ignored
    opts = [_opt('C', 100, 50, 1, expiry=date(2026, 11, 6))]
    assert deribit_feed.nearest_expiry(opts, 30, TODAY) is None
    assert deribit_feed.nearest_expiry([], 30, TODAY) is None


# --- Black-Scholes delta ---

def test_bs_delta_bounds():
    # ATM call ~0.5, deep ITM put ~-1, deep OTM call ~0
    assert deribit_feed.bs_delta('C', 100, 100, 30, 50) == pytest.approx(
        0.53, abs=0.05)
    assert deribit_feed.bs_delta('P', 100, 100, 30, 50) == pytest.approx(
        -0.47, abs=0.05)
    assert deribit_feed.bs_delta('P', 100, 200, 30, 50) < -0.95
    assert deribit_feed.bs_delta('C', 100, 200, 30, 50) < 0.05
    assert deribit_feed.bs_delta('C', 100, 100, 0, 50) is None
    assert deribit_feed.bs_delta('C', 100, 100, 30, 0) is None
    assert deribit_feed.bs_delta('C', 0, 100, 30, 50) is None


# --- Metrics on synthetic books (spot 100, 30d expiry) ---

def test_atm_iv_averages_both_sides():
    opts = [
        _opt('C', 95, 48, 10), _opt('P', 95, 49, 10),
        _opt('C', 100, 50, 10), _opt('P', 100, 52, 10),
        _opt('C', 105, 51, 10), _opt('P', 105, 53, 10),
    ]
    assert deribit_feed.atm_iv(opts, EXP_30D, 100.0) == pytest.approx(51.0)
    # One side alone is used when the other has no quote
    assert deribit_feed.atm_iv([_opt('C', 100, 50, 10)], EXP_30D, 100.0) \
        == pytest.approx(50.0)
    assert deribit_feed.atm_iv(opts, EXP_30D, None) is None
    assert deribit_feed.atm_iv([], EXP_30D, 100.0) is None


def test_skew_25d_picks_nearest_delta_legs():
    # S=100, T=30d, r=0. Put K=92 IV 55 -> delta ~-0.27; call K=111
    # IV 45 -> delta ~+0.23. Decoys sit further from 0.25.
    opts = [
        _opt('P', 80, 60, 5),    # delta ~-0.45 (decoy)
        _opt('P', 92, 55, 10),   # ~-0.27 -> the put leg
        _opt('P', 98, 52, 10),
        _opt('C', 102, 48, 10),
        _opt('C', 111, 45, 10),  # ~+0.23 -> the call leg
        _opt('C', 130, 40, 5),   # delta ~+0.08 (decoy)
    ]
    sk = deribit_feed.skew_25d(opts, EXP_30D, 100.0, TODAY)
    assert sk is not None
    assert sk['skew'] == pytest.approx(10.0, abs=0.01)
    assert sk['put_iv'] == pytest.approx(55.0)
    assert sk['call_iv'] == pytest.approx(45.0)
    assert sk['put_delta'] == pytest.approx(-0.27, abs=0.04)
    assert sk['call_delta'] == pytest.approx(0.23, abs=0.04)
    # Missing a leg -> no data, never a guess
    assert deribit_feed.skew_25d(
        [_opt('C', 111, 45, 10)], EXP_30D, 100.0, TODAY) is None
    assert deribit_feed.skew_25d(opts, EXP_30D, None, TODAY) is None


def test_put_call_oi_ratio():
    opts = [_opt('P', 90, 50, 150), _opt('C', 110, 50, 100)]
    assert deribit_feed.put_call_oi_ratio(opts) == pytest.approx(1.5)
    assert deribit_feed.put_call_oi_ratio(
        [_opt('C', 110, 50, 100)]) == pytest.approx(0.0)
    assert deribit_feed.put_call_oi_ratio(
        [_opt('P', 90, 50, 10)]) is None  # no call OI
    assert deribit_feed.put_call_oi_ratio([]) is None


def test_max_pain_minimizes_intrinsic():
    # Calls OI {90:100, 100:10, 110:0}, puts OI {90:0, 100:10,
    # 110:100}. Intrinsic totals: 90->2100, 100->2000, 110->2100.
    opts = [
        _opt('C', 90, 50, 100), _opt('C', 100, 50, 10),
        _opt('C', 110, 50, 0),
        _opt('P', 90, 50, 0), _opt('P', 100, 50, 10),
        _opt('P', 110, 50, 100),
    ]
    mp = deribit_feed.max_pain(opts, EXP_30D, 100.0)
    assert mp['strike'] == pytest.approx(100.0)
    assert mp['distance_pct'] == pytest.approx(0.0)
    # Max pain above spot reads negative distance. Calls OI
    # {90:0, 100:10, 110:200, 120:0}, puts OI {90:0, 100:0, 110:0,
    # 120:100}: intrinsic totals 90->3000, 100->2000, 110->1100,
    # 120->2200 — unique minimum at 110.
    opts2 = [
        _opt('C', 90, 50, 0), _opt('C', 100, 50, 10),
        _opt('C', 110, 50, 200), _opt('C', 120, 50, 0),
        _opt('P', 90, 50, 0), _opt('P', 100, 50, 0),
        _opt('P', 110, 50, 0), _opt('P', 120, 50, 100),
    ]
    mp2 = deribit_feed.max_pain(opts2, EXP_30D, 100.0)
    assert mp2['strike'] == pytest.approx(110.0)
    assert mp2['distance_pct'] == pytest.approx(-10.0)
    assert deribit_feed.max_pain([], EXP_30D, 100.0) is None


# --- Fetch (fake network) ---

def test_fetch_currency_all_fields():
    fetch = _fake_fetch({'get_book_summary_by_currency': SAMPLE_BOOK})
    row = deribit_feed.fetch_currency(fetch, 'BTC', today=TODAY)
    assert row['currency'] == 'BTC'
    assert row['options'] == 2
    assert row['underlying'] == pytest.approx(76412.84)
    # Only expiry in the sample is 25DEC26 -> 48 days out
    assert row['expiry'] == '2026-12-25'
    assert row['dte'] == 48
    assert row['atm_iv'] == pytest.approx((43.13 + 44.02) / 2, abs=0.01)
    assert row['put_call_oi'] == pytest.approx(210.5 / 183.1, abs=0.001)
    assert row['error'] is None
    assert 'currency=BTC' in fetch.calls[0]
    assert 'kind=option' in fetch.calls[0]


def test_fetch_currency_fail_open():
    fetch = _fake_fetch(
        {'get_book_summary_by_currency': ConnectionError('boom')})
    row = deribit_feed.fetch_currency(fetch, 'BTC', today=TODAY)
    assert row['atm_iv'] is None
    assert row['options'] == 0
    assert row['error'] is not None
    # Empty book -> error, still a row
    fetch = _fake_fetch({'get_book_summary_by_currency': {'result': []}})
    row = deribit_feed.fetch_currency(fetch, 'ETH', today=TODAY)
    assert row['error'] == 'empty book'


def test_fetch_all_never_raises():
    def boom(url):
        raise RuntimeError('network is gone')
    out = deribit_feed.fetch_all(boom, ['BTC', 'ETH'], today=TODAY)
    assert set(out) == {'BTC', 'ETH'}
    assert all(r['error'] is not None for r in out.values())
    # Dedup + order kept
    fetch = _fake_fetch({'get_book_summary_by_currency': SAMPLE_BOOK})
    out = deribit_feed.fetch_all(fetch, ['ETH', 'BTC', 'BTC'],
                                 today=TODAY)
    assert list(out) == ['ETH', 'BTC']


# --- Pulse line ---

def test_pulse_line():
    assert deribit_feed.pulse_line({}) == 'Deribit no data'
    line = deribit_feed.pulse_line({'BTC': {
        'atm_iv': 43.1, 'skew_25d': 4.2, 'put_call_oi': 0.87,
        'max_pain_strike': 76000, 'max_pain_distance_pct': -0.5}})
    assert 'BTC 30d ATM IV 43.1%' in line
    assert 'skew +4.2' in line
    assert 'P/C OI 0.87' in line
    assert 'max pain 76,000 (-0.5%)' in line
    assert '1/1 currencies' in line
