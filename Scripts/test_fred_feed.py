"""Tests for fred_feed.py — FRED keyless CSV macro feed.

Pure-parser tests use synthetic CSV; fetch tests use a fake
fetch_text. No network access in the test suite.
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fred_feed as fred

D = datetime.date


def _csv(series_id, rows):
    """rows: [(date_str, value_str), ...] -> fredgraph.csv body."""
    lines = ['observation_date,%s' % series_id]
    lines += ['%s,%s' % (d, v) for d, v in rows]
    return '\n'.join(lines) + '\n'


def test_parse_csv_basic_and_missing():
    text = _csv('DGS10', [
        ('2026-11-04', '4.90'),
        ('2026-11-05', '4.84'),
        ('2026-11-06', '.'),
        ('2026-11-07', 'garbage'),
        ('not-a-date', '4.00'),
        ('2026-11-08', '4.86'),
    ])
    rows = fred.parse_csv(text)
    assert rows == [(D(2026, 11, 4), 4.90),
                    (D(2026, 11, 5), 4.84),
                    (D(2026, 11, 8), 4.86)]


def test_parse_csv_bad_input():
    assert fred.parse_csv(None) == []
    assert fred.parse_csv('') == []
    assert fred.parse_csv('observation_date,DGS10\n') == []
    assert fred.parse_csv('no header at all') == []


def test_parse_csv_sorts_oldest_first():
    text = _csv('T10Y2Y', [
        ('2026-11-06', '0.50'),
        ('2026-11-04', '0.42'),
    ])
    rows = fred.parse_csv(text)
    assert [d for d, _ in rows] == [D(2026, 11, 4), D(2026, 11, 6)]


def test_series_url_is_date_bounded():
    url = fred.series_url('DGS10', 45, today=D(2026, 11, 10))
    assert url.startswith('https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10')
    assert 'cosd=2026-09-26' in url
    assert 'coed=2026-11-10' in url


def _daily_series(start, n_days, start_v, step):
    out = []
    v = start_v
    for i in range(n_days):
        out.append((start + datetime.timedelta(days=i), round(v, 4)))
        v += step
    return out


def test_value_days_ago_and_pct_change():
    s = _daily_series(D(2026, 10, 1), 40, 100.0, 1.0)
    # Latest is 2026-11-09 @ 139.0; 7 days back target 2026-11-02 @ 132.0
    old = fred.value_days_ago(s, 7)
    assert old == (D(2026, 11, 2), 132.0)
    assert fred.pct_change(s, 7) == round((139.0 - 132.0) / 132.0 * 100, 2)
    assert fred.pp_change(s, 7) == 7.0
    # Window older than the series -> None, never a guess
    assert fred.value_days_ago(s, 60) is None
    assert fred.pct_change(s, 60) is None
    assert fred.pp_change(s, 60) is None
    assert fred.pct_change([], 7) is None


def test_compute_snapshot_derived_variables():
    today = D(2026, 11, 9)
    curve = _daily_series(D(2026, 10, 1), 40, 0.30, 0.01)   # ends 0.69
    d10 = _daily_series(D(2026, 10, 1), 40, 4.50, 0.01)     # ends 4.89
    cpi = _daily_series(D(2025, 10, 1), 405, 300.0, 0.05)   # YoY ~ +6%
    dollar = _daily_series(D(2026, 10, 1), 40, 120.0, -0.10)  # falling
    walcl = [(D(2026, 10, 5) + datetime.timedelta(days=7 * i),
              7000000.0 + 10000.0 * i) for i in range(6)]     # weekly, rising
    hy = _daily_series(D(2026, 10, 1), 40, 3.00, 0.02)
    snap = fred.compute_snapshot({
        'T10Y2Y': curve, 'DGS10': d10, 'FEDFUNDS': [(today, 4.25)],
        'CPIAUCSL': cpi, 'DTWEXBGS': dollar, 'WALCL': walcl,
        'BAMLH0A0HYM2': hy})
    assert snap['yield_curve_10y2y'] == 0.69
    assert snap['curve'] == 'normal'
    assert snap['dgs10'] == 4.89
    assert snap['cpi_yoy_pct'] is not None and snap['cpi_yoy_pct'] > 0
    assert snap['real_rate_proxy'] == round(4.89 - snap['cpi_yoy_pct'], 2)
    assert snap['dollar_7d_pct'] < 0  # dollar falling
    assert snap['dollar_30d_pct'] < 0
    assert snap['fed_liquidity_wow_pct'] > 0
    assert snap['liquidity'] == 'expanding'
    assert snap['hy_spread'] == round(3.00 + 0.02 * 39, 2)


def test_compute_snapshot_curve_labels():
    base = {'DGS10': [], 'FEDFUNDS': [], 'CPIAUCSL': [], 'DTWEXBGS': [],
            'WALCL': [], 'BAMLH0A0HYM2': []}
    today = D(2026, 11, 9)
    assert fred.compute_snapshot(dict(base, T10Y2Y=[(today, -0.50)]))['curve'] == 'inverted'
    assert fred.compute_snapshot(dict(base, T10Y2Y=[(today, 0.10)]))['curve'] == 'flat'
    assert fred.compute_snapshot(dict(base, T10Y2Y=[(today, 0.80)]))['curve'] == 'normal'
    assert fred.compute_snapshot(dict(base, T10Y2Y=[]))['curve'] is None
    # Empty everything -> all None, no crash
    snap = fred.compute_snapshot({})
    assert snap['real_rate_proxy'] is None
    assert snap['liquidity'] is None
    assert fred.pulse_line(snap) == 'FRED: no data'
    assert fred.pulse_line({}) == 'FRED: no data'


def test_compute_snapshot_cpi_yoy_with_lagging_monthly_prints():
    # CPI is monthly and its latest print lags ~2 months: a 480-day
    # window must still yield a YoY (regression: a 400-day window
    # dropped the year-ago print and real_rate_proxy read None).
    cpi = []
    d = D(2025, 7, 1)
    v = 310.0
    while d <= D(2026, 9, 1):
        cpi.append((d, round(v, 3)))
        v *= 1.0025
        d = D(d.year + (d.month == 12), d.month % 12 + 1, 1)
    snap = fred.compute_snapshot({'CPIAUCSL': cpi,
                                  'DGS10': [(D(2026, 11, 6), 4.86)]})
    assert snap['cpi_yoy_pct'] is not None
    assert snap['cpi_yoy_pct'] > 0
    assert snap['real_rate_proxy'] == round(4.86 - snap['cpi_yoy_pct'], 2)


def test_pulse_line():
    snap = {'yield_curve_10y2y': 0.42, 'curve': 'normal', 'dgs10': 4.86,
            'real_rate_proxy': 1.9, 'liquidity': 'flat',
            'fed_liquidity_wow_pct': 0.01, 'hy_spread': 3.12}
    line = fred.pulse_line(snap)
    assert line.startswith('FRED: ')
    assert 'curve +0.42 normal' in line
    assert '10y 4.86%' in line
    assert 'real ~1.90%' in line
    assert 'liquidity flat (WALCL +0.01% WoW)' in line
    assert 'HY 3.12' in line


def test_fetch_all_fail_open_with_fake_fetcher():
    bodies = {
        'T10Y2Y': _csv('T10Y2Y', [('2026-11-09', '0.42')]),
        'DGS10': _csv('DGS10', [('2026-11-09', '4.86')]),
    }

    def fake_fetch_text(url):
        for sid, body in bodies.items():
            if 'id=%s' % sid in url:
                return body
        raise RuntimeError('boom')

    out = fred.fetch_all(fake_fetch_text, ['T10Y2Y', 'DGS10', 'WALCL'])
    assert out['T10Y2Y'] == [(D(2026, 11, 9), 0.42)]
    assert out['DGS10'] == [(D(2026, 11, 9), 4.86)]
    assert out['WALCL'] == []  # failed series -> [], never an exception
    snap = fred.compute_snapshot(out)
    assert snap['yield_curve_10y2y'] == 0.42
    assert snap['liquidity'] is None
