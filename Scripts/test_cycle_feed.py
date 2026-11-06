"""Cycle research track, Phase 1 — pure-function tests.

Synthetic fixtures per variable: halving boundaries, month edges,
dominance thresholds, FNG band edges, drawdown bands, missing feeds.
No disk, no network (fetches are tested with a fake fetch_json).

Run: python3 -m pytest Scripts/test_cycle_feed.py
"""
import sys
import os
from datetime import date, timedelta, timezone, datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cycle_feed as cf


def closes_for(dates_prices):
    """[(date, close)] -> [[ts, close]] in Kraken-row shape."""
    out = []
    for d, p in dates_prices:
        ts = int(datetime(d.year, d.month, d.day,
                          tzinfo=timezone.utc).timestamp())
        out.append([ts, p])
    return out


def flat_closes(start, days, price=100.0, end=None):
    end = end or (start + timedelta(days=days - 1))
    d, out = start, []
    while d <= end:
        out.append((d, price))
        d += timedelta(days=1)
    return closes_for(out)


# ---------------------------------------------------------------- halving

def test_halving_boundaries():
    h = date(2024, 4, 19)  # most recent halving
    assert cf.halving_band(h) == ('H1', 0)
    assert cf.halving_band(h + timedelta(days=90)) == ('H1', 90)
    assert cf.halving_band(h + timedelta(days=91)) == ('H2', 91)
    assert cf.halving_band(h + timedelta(days=180)) == ('H2', 180)
    assert cf.halving_band(h + timedelta(days=181)) == ('H3', 181)
    assert cf.halving_band(h + timedelta(days=365)) == ('H3', 365)
    assert cf.halving_band(h + timedelta(days=366)) == ('H4', 366)
    assert cf.halving_band(h + timedelta(days=730)) == ('H4', 730)
    assert cf.halving_band(h + timedelta(days=731)) == ('H5', 731)
    # Spec example: 2025-11-05 is day 565 (H4).
    assert cf.halving_band(date(2025, 11, 5)) == ('H4', 565)


def test_halving_earlier_cycles():
    assert cf.halving_band(date(2016, 7, 9)) == ('H1', 0)
    assert cf.halving_band(date(2020, 5, 11)) == ('H1', 0)
    # Day before a halving belongs to the previous cycle's H5.
    assert cf.halving_band(date(2024, 4, 18))[0] == 'H5'
    assert cf.halving_band(date(2024, 4, 18))[1] > 731


def test_halving_before_first():
    assert cf.halving_band(date(2012, 11, 27)) == (None, None)
    assert cf.halving_band(date(2010, 1, 1)) == (None, None)
    assert cf.halving_band(None) == (None, None)
    assert cf.halving_band('garbage') == (None, None)


# ---------------------------------------------------------------- month / dow

def test_month_and_dow():
    assert cf.month_of(date(2026, 11, 5)) == 'November'
    assert cf.month_of('2026-01-15') == 'January'
    assert cf.month_of(date(2026, 12, 31)) == 'December'
    assert cf.month_of(None) is None
    # 2026-11-05 is a Thursday (verified against the calendar).
    assert cf.dow_of(date(2026, 11, 5)) == 'Thursday'
    assert cf.dow_of(date(2026, 11, 7)) == 'Saturday'
    assert cf.dow_of('not-a-date') is None


# ---------------------------------------------------------------- dominance

def test_dom_band_thresholds():
    assert cf.dom_band(60.0, 59.0) == 'DOM_UP'      # exactly +1.0pt
    assert cf.dom_band(60.0, 61.0) == 'DOM_DOWN'    # exactly -1.0pt
    assert cf.dom_band(60.0, 59.01) == 'DOM_FLAT'   # +0.99pt
    assert cf.dom_band(60.0, 60.99) == 'DOM_FLAT'   # -0.99pt
    assert cf.dom_band(60.0, 55.0) == 'DOM_UP'
    assert cf.dom_band(60.0, 65.0) == 'DOM_DOWN'


def test_dom_band_missing():
    assert cf.dom_band(None, 59.0) is None
    assert cf.dom_band(60.0, None) is None
    assert cf.dom_band(None, None) is None
    assert cf.dom_band('x', 59.0) is None
    assert cf.dom_change_pts(60.5, 59.0) == pytest.approx(1.5)
    assert cf.dom_change_pts(None, 59.0) is None


def test_dom_history_helpers():
    hist = {}
    hist = cf.update_dom_history(hist, '2026-11-05', 60.1)
    assert hist == {'2026-11-05': 60.1}
    # None value leaves history untouched.
    assert cf.update_dom_history(hist, '2026-11-06', None) == hist
    # 30d-ago lookup: exact day, then nearest within +/-3 days.
    hist = cf.update_dom_history(hist, '2026-10-06', 58.0)
    assert cf.dom_30d_ago(hist, '2026-11-05') == pytest.approx(58.0)
    # Too old / too young: no value.
    assert cf.dom_30d_ago(hist, '2026-12-20') is None
    assert cf.dom_30d_ago({}, '2026-11-05') is None
    # Old entries pruned past the 45-day cache window.
    big = {}
    d = date(2026, 9, 1)
    for i in range(60):
        big = cf.update_dom_history(big, (d + timedelta(days=i)).isoformat(),
                                    50.0 + i * 0.1)
    assert len(big) <= 46
    assert '2026-09-01' not in big


# ---------------------------------------------------------------- fear & greed

def test_fng_band_edges():
    assert cf.fng_band(0) == 'FNG_XF'
    assert cf.fng_band(24) == 'FNG_XF'
    assert cf.fng_band(25) == 'FNG_F'
    assert cf.fng_band(44) == 'FNG_F'
    assert cf.fng_band(45) == 'FNG_N'   # neutral control band
    assert cf.fng_band(55) == 'FNG_N'
    assert cf.fng_band(56) == 'FNG_G'
    assert cf.fng_band(74) == 'FNG_G'
    assert cf.fng_band(75) == 'FNG_XG'
    assert cf.fng_band(100) == 'FNG_XG'


def test_fng_band_missing_or_bad():
    assert cf.fng_band(None) is None
    assert cf.fng_band(-1) is None
    assert cf.fng_band(101) is None
    assert cf.fng_band('nope') is None
    assert cf.fng_band('32') == 'FNG_F'  # numeric strings accepted


# ---------------------------------------------------------------- drawdown

def test_dd_bands():
    # Flat at 100, then a drop: high stays 100 (trailing 365d).
    start = date(2025, 1, 1)
    closes = flat_closes(start, 400, price=100.0)
    # Day 400 close is 100 -> 0% drawdown.
    assert cf.dd_band(closes, start + timedelta(days=399)) == \
        ('DD_SHALLOW', 0.0)
    # Drop the last close to 90 -> exactly 10% -> still shallow.
    closes2 = list(closes)
    closes2[-1] = [closes2[-1][0], 90.0]
    assert cf.dd_band(closes2, start + timedelta(days=399)) == \
        ('DD_SHALLOW', 10.0)
    closes2[-1] = [closes2[-1][0], 89.0]
    assert cf.dd_band(closes2, start + timedelta(days=399))[0] == 'DD_MID'
    closes2[-1] = [closes2[-1][0], 75.0]
    assert cf.dd_band(closes2, start + timedelta(days=399)) == \
        ('DD_MID', 25.0)
    closes2[-1] = [closes2[-1][0], 74.0]
    assert cf.dd_band(closes2, start + timedelta(days=399))[0] == 'DD_DEEP'
    closes2[-1] = [closes2[-1][0], 60.0]
    assert cf.dd_band(closes2, start + timedelta(days=399)) == \
        ('DD_DEEP', 40.0)
    closes2[-1] = [closes2[-1][0], 50.0]
    assert cf.dd_band(closes2, start + timedelta(days=399)) == \
        ('DD_EXTREME', 50.0)


def test_dd_trailing_window_only():
    # A high 400 days ago must NOT count: window is trailing 365d.
    start = date(2025, 1, 1)
    rows = [(start + timedelta(days=i), 200.0 if i == 0 else 100.0)
            for i in range(400)]
    closes = closes_for(rows)
    band, pct = cf.dd_band(closes, start + timedelta(days=399))
    assert (band, pct) == ('DD_SHALLOW', 0.0)


def test_dd_missing_data():
    assert cf.dd_band([], date(2026, 11, 5)) == (None, None)
    assert cf.dd_band(None, date(2026, 11, 5)) == (None, None)
    assert cf.dd_band(flat_closes(date(2025, 1, 1), 10), None) == \
        (None, None)
    assert cf.dd_band('garbage', date(2026, 11, 5)) == (None, None)


# ---------------------------------------------------------------- compute_state

def test_compute_state_full():
    closes = flat_closes(date(2025, 1, 1), 400, price=100.0)
    st = cf.compute_state(date(2026, 11, 5), closes, 60.0, 61.5, 32)
    assert st['date'] == '2026-11-05'
    assert st['halving_band'] == 'H5'  # 2024-04-19 + 930 days
    assert st['halving_day'] == 930
    assert st['month'] == 'November'
    assert st['dow'] == 'Thursday'
    assert st['dom_band'] == 'DOM_DOWN'
    assert st['dom_change_pts'] == pytest.approx(-1.5)
    assert st['fng_band'] == 'FNG_F'
    assert st['fng_value'] == 32
    assert st['dd_band'] == 'DD_SHALLOW'


def test_compute_state_missing_feeds():
    st = cf.compute_state(date(2026, 11, 5), [])
    assert st['halving_band'] == 'H5'
    assert st['dom_band'] is None
    assert st['fng_band'] is None
    assert st['fng_value'] is None
    assert st['dd_band'] is None
    assert st['month'] == 'November'


def test_compute_state_never_raises():
    st = cf.compute_state('garbage', 'garbage', 'x', 'y', 'z')
    assert isinstance(st, dict)
    # Unparseable day falls back to today (so halving/month/dow are
    # real); the bad feeds still read "no data".
    assert st['date'] == datetime.now(timezone.utc).date().isoformat()
    assert st['dom_band'] is None
    assert st['fng_band'] is None
    assert st['dd_band'] is None


def test_pulse_line():
    st = cf.compute_state(date(2025, 11, 5),
                          flat_closes(date(2025, 1, 1), 400), 60.0, 61.5,
                          32)
    line = cf.pulse_line(st)
    assert line == ('Halving day 565 (H4) · November · Dominance falling '
                    '· Fear & Greed 32 (Fear) · BTC near its 365-day high')
    assert cf.pulse_line({}) == ('Halving no data · Month no data · '
                                 'Dominance no data · Fear & Greed no data '
                                 '· Drawdown no data')
    assert cf.pulse_line(None) == ('Halving no data · Month no data · '
                                   'Dominance no data · Fear & Greed no data '
                                   '· Drawdown no data')


# ---------------------------------------------------------------- directions

def test_direction_for():
    assert cf.direction_for('halving', 'H4', 'ETH') == 'LONG'
    assert cf.direction_for('month', 'November', 'BTC') == 'LONG'
    assert cf.direction_for('dow', 'Thursday', 'SOL') == 'LONG'
    assert cf.direction_for('dd', 'DD_DEEP', 'BTC') == 'LONG'
    # Dominance: BTC-long on DOM_UP, alt-long on DOM_DOWN, control all.
    assert cf.direction_for('dom', 'DOM_UP', 'BTC') == 'LONG'
    assert cf.direction_for('dom', 'DOM_UP', 'ETH') is None
    assert cf.direction_for('dom', 'DOM_DOWN', 'ETH') == 'LONG'
    assert cf.direction_for('dom', 'DOM_DOWN', 'BTC') is None
    assert cf.direction_for('dom', 'DOM_FLAT', 'BTC') == 'LONG'
    assert cf.direction_for('dom', 'DOM_FLAT', 'ETH') == 'LONG'
    # Fear & Greed: LONG except extreme greed (contrarian SHORT).
    assert cf.direction_for('fng', 'FNG_XF', 'BTC') == 'LONG'
    assert cf.direction_for('fng', 'FNG_N', 'ETH') == 'LONG'
    assert cf.direction_for('fng', 'FNG_XG', 'BTC') == 'SHORT'
    assert cf.direction_for('fng', 'FNG_XG', 'ETH') == 'SHORT'
    assert cf.direction_for('fng', None, 'BTC') is None
    assert cf.direction_for('bogus', 'H1', 'BTC') is None
    assert cf.state_band({'halving_band': 'H4'}, 'halving') == 'H4'
    assert cf.state_band({'month': 'November'}, 'month') == 'November'
    assert cf.state_band({}, 'halving') is None


# ---------------------------------------------------------------- fetches (fake)

def _fake_fetch(mapping):
    def fetch(url, timeout=30):
        for key, val in mapping.items():
            if key in url:
                return val
        return None
    return fetch


def test_fetch_btc_daily_drops_forming_day():
    today = datetime.now(timezone.utc).date()
    t_today = int(datetime(today.year, today.month, today.day,
                           tzinfo=timezone.utc).timestamp())
    t_yday = t_today - 86400
    fetch = _fake_fetch({'kraken.com': {
        'result': {'XXBTZUSD': [
            [t_yday, '90000', '91000', '89000', '90500', '0', '0', 1],
            [t_today, '90500', '91000', '90000', '90800', '0', '0', 1],
        ]}}})
    closes = cf.fetch_btc_daily(fetch)
    assert closes == [[t_yday, 90500.0]]  # forming day dropped


def test_fetch_btc_daily_bad_payload():
    assert cf.fetch_btc_daily(_fake_fetch({})) == []
    assert cf.fetch_btc_daily(lambda u, timeout=30: {'error': ['x']}) == []
    assert cf.fetch_btc_daily(lambda u, timeout=30: (_ for _ in ()).throw(
        RuntimeError('down'))) == []


def test_fetch_dominance_and_fng():
    fetch = _fake_fetch({
        'coingecko.com': {'data': {'market_cap_percentage': {'btc': 58.7}}},
        'alternative.me': {'data': [{'value': '28'}]},
    })
    assert cf.fetch_dominance(fetch) == pytest.approx(58.7)
    assert cf.fetch_fng(fetch) == 28
    assert cf.fetch_dominance(_fake_fetch({})) is None
    assert cf.fetch_fng(_fake_fetch({})) is None
    assert cf.fetch_fng(lambda u, timeout=30: {'data': [{'value': 'xx'}]}) \
        is None


def test_fetch_fng_history():
    fetch = _fake_fetch({'alternative.me': {'data': [
        {'value': '30', 'timestamp': '1517443200'},  # 2018-02-01
        {'value': '28', 'timestamp': '1793923200'},  # 2026-11-06
    ]}})
    hist = cf.fetch_fng_history(fetch)
    assert hist == {'2018-02-01': 30, '2026-11-06': 28}


def test_fetch_coinbase_range():
    fetch = _fake_fetch({'exchange.coinbase.com': [
        [1451606400, 427.0, 437.0, 430.0, 435.66, 100.0],
        [1451692800, 430.0, 440.0, 435.0, 438.0, 110.0],
    ]})
    closes = cf.fetch_btc_daily_range_coinbase(
        fetch, date(2016, 1, 1), date(2016, 1, 2))
    assert closes == [[1451606400, 435.66], [1451692800, 438.0]]
    assert cf.fetch_btc_daily_range_coinbase(
        fetch, date(2016, 1, 3), date(2016, 1, 1)) == []
    assert cf.fetch_btc_daily_range_coinbase(
        _fake_fetch({}), date(2016, 1, 1), date(2016, 1, 2)) == []


# ---------------------------------------------------------------- stores

def test_update_btc_daily_store():
    prior = [[1000, 90.0], [2000, 91.0]]
    fetched = [[2000, 91.5], [3000, 92.0]]
    out = cf.update_btc_daily_store(prior, fetched)
    assert out == [[1000, 90.0], [2000, 91.5], [3000, 92.0]]
    assert cf.update_btc_daily_store(None, None) == []
    assert cf.update_btc_daily_store('junk', 'junk') == []


# ---------------------------------------------------------------- build_history

def test_build_history():
    closes = flat_closes(date(2024, 4, 18), 5, price=100.0)
    hist = cf.build_history(closes, {'2024-04-19': 50})
    assert sorted(hist) == ['2024-04-18', '2024-04-19', '2024-04-20',
                            '2024-04-21', '2024-04-22']
    assert hist['2024-04-18']['halving_band'] == 'H5'  # pre-halving
    assert hist['2024-04-19']['halving_band'] == 'H1'
    assert hist['2024-04-19']['halving_day'] == 0
    assert hist['2024-04-19']['fng_band'] == 'FNG_N'
    assert hist['2024-04-19']['fng_value'] == 50
    assert hist['2024-04-20']['fng_band'] is None  # no FNG that day
    assert hist['2024-04-20']['dom_band'] is None  # never in history
    assert hist['2024-04-20']['dd_band'] == 'DD_SHALLOW'
    assert cf.build_history([]) == {}
    assert cf.build_history(None) == {}
