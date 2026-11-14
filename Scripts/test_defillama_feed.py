"""Tests for defillama_feed.py — DeFiLlama stablecoin / TVL feed.

Pure-parser tests use synthetic data; fetch tests use a fake
fetch_json. No network access in the test suite.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import defillama_feed as dl

NOW = 1794009600  # 2026-11-07 00:00 UTC (a Saturday)


def _sample_stablecoins():
    return {
        'peggedAssets': [
            {'id': '1', 'name': 'Tether', 'symbol': 'USDT',
             'pegType': 'peggedUSD',
             'circulating': {'peggedUSD': 176781434062.09}},
            {'id': '2', 'name': 'USDC', 'symbol': 'USDC',
             'pegType': 'peggedUSD',
             'circulating': {'peggedUSD': 70641518051.24}},
            {'id': '3', 'name': 'Dai', 'symbol': 'DAI',
             'pegType': 'peggedUSD',
             'circulating': {'peggedUSD': 5000000000.0}},
            {'id': '4', 'name': 'Euro Coin', 'symbol': 'EURC',
             'pegType': 'peggedEUR',
             'circulating': {'peggedEUR': 100000000.0}},
            {'id': '5', 'name': 'Broken', 'symbol': 'BRK',
             'pegType': 'peggedUSD',
             'circulating': {}},
        ],
        'chains': ['Ethereum'],
    }


def _daily_series(base_ts, base_v, daily_growth_pct, n_days):
    """[(ts, v), ...] oldest first, growing daily_growth_pct per day."""
    out = []
    v = base_v
    for i in range(n_days):
        out.append((base_ts + i * 86400, round(v, 2)))
        v *= (1 + daily_growth_pct / 100.0)
    return out


def test_parse_stablecoins_totals_and_dominance():
    row = dl.parse_stablecoins(_sample_stablecoins())
    assert row is not None
    # Total sums only the peggedUSD circulating values (EURC excluded).
    assert row['total_usd'] == 176781434062.09 + 70641518051.24 + 5e9
    assert row['usdt'] == 176781434062.09
    assert row['usdc'] == 70641518051.24
    assert row['usdt_usdc'] == 176781434062.09 + 70641518051.24
    assert row['dominance'] == round(row['usdt_usdc'] / row['total_usd'], 4)
    assert row['count'] == 3  # USDT, USDC, DAI (EURC + broken excluded)


def test_parse_stablecoins_bad_input():
    assert dl.parse_stablecoins(None) is None
    assert dl.parse_stablecoins({}) is None
    assert dl.parse_stablecoins({'peggedAssets': []}) is None
    assert dl.parse_stablecoins({'peggedAssets': 'nope'}) is None
    assert dl.parse_stablecoins({'peggedAssets': [
        {'symbol': 'X', 'circulating': {'peggedUSD': 'abc'}}]}) is None


def test_parse_stablecoin_charts():
    data = [
        {'date': '1794009600',
         'totalCirculatingUSD': {'peggedUSD': 292281027860.37}},
        {'date': '1793404800',
         'totalCirculatingUSD': {'peggedUSD': 290000000000.0}},
        {'date': 'garbage',
         'totalCirculatingUSD': {'peggedUSD': 1.0}},
        {'date': '1793923200', 'totalCirculatingUSD': {}},
    ]
    series = dl.parse_stablecoin_charts(data)
    assert series == [(1793404800, 290000000000.0),
                      (1794009600, 292281027860.37)]
    assert dl.parse_stablecoin_charts(None) == []
    assert dl.parse_stablecoin_charts({}) == []


def test_supply_changes_from_daily_series():
    # 30 days of +0.1%/day growth ending at NOW.
    series = _daily_series(NOW - 29 * 86400, 280e9, 0.1, 30)
    chg = dl.supply_changes(series, NOW)
    assert chg['total_chg_7d_pct'] is not None
    # 7 days of +0.1% compounding ≈ +0.70%.
    assert abs(chg['total_chg_7d_pct'] - 0.70) < 0.05
    assert abs(chg['total_chg_24h_pct'] - 0.10) < 0.02
    # Too-short series -> None, never a guess.
    short = _daily_series(NOW - 86400, 280e9, 0.1, 2)
    chg2 = dl.supply_changes(short, NOW)
    assert chg2['total_chg_7d_pct'] is None
    assert chg2['total_chg_24h_pct'] is not None


def test_parse_chains_total_and_top():
    data = [
        {'name': 'Ethereum', 'tvl': 50000000000.0, 'chainId': 1},
        {'name': 'Solana', 'tvl': 10000000000.0, 'chainId': 101},
        {'name': 'Empty', 'tvl': 0, 'chainId': 2},
        {'name': 'Bad', 'tvl': 'n/a', 'chainId': 3},
        {'name': 'NoTvl', 'chainId': 4},
    ]
    row = dl.parse_chains(data)
    assert row is not None
    assert row['tvl_total'] == 60000000000.0
    assert row['chains'][0][0] == 'Ethereum'
    assert row['chains'][1][0] == 'Solana'
    assert dl.parse_chains(None) is None
    assert dl.parse_chains([]) is None
    assert dl.parse_chains([{'name': 'X', 'tvl': 0}]) is None


def test_parse_tvl_history():
    data = [
        {'date': 1794009600, 'tvl': 81044199972},
        {'date': 1793404800, 'tvl': 80000000000},
        {'date': 'bad', 'tvl': 1},
        {'date': 1793923200, 'tvl': None},
    ]
    series = dl.parse_tvl_history(data)
    assert series == [(1793404800, 80000000000.0),
                      (1794009600, 81044199972.0)]
    assert dl.parse_tvl_history(None) == []


def test_tvl_changes():
    series = _daily_series(NOW - 29 * 86400, 80e9, -0.2, 30)
    chg = dl.tvl_changes(series, NOW)
    assert chg['tvl_chg_7d_pct'] is not None
    assert abs(chg['tvl_chg_7d_pct'] - (-1.39)) < 0.05
    assert abs(chg['tvl_chg_24h_pct'] - (-0.20)) < 0.02


def test_supply_history_warmup_then_change():
    # Empty store: both changes None (warming up).
    hist = dl.update_supply_history({}, 247423000000.0, NOW - 7 * 86400)
    assert dl.usdt_usdc_changes(hist, NOW)['usdt_usdc_chg_7d_pct'] is None
    # Add a point 7 days ago and one now: 7d change computable.
    hist = dl.update_supply_history(hist, 250000000000.0, NOW)
    chg = dl.usdt_usdc_changes(hist, NOW)
    assert chg['usdt_usdc_chg_7d_pct'] is not None
    assert abs(chg['usdt_usdc_chg_7d_pct']
               - (250000000000.0 - 247423000000.0) / 247423000000.0 * 100) < 0.01
    # 24h change needs a point ~24h old — with only a 7d-old base
    # and now, the 24h window has no base point, so it stays None.
    assert chg['usdt_usdc_chg_24h_pct'] is None
    # A None reading leaves the store untouched; cap respected.
    hist2 = dl.update_supply_history(hist, None, NOW + 60)
    assert hist2 == hist


def test_fetch_current_with_fake_fetch():
    def fake(url):
        if 'stablecoins.llama.fi/stablecoins' in url:
            return _sample_stablecoins()
        if 'api.llama.fi/v2/chains' in url:
            return [{'name': 'Ethereum', 'tvl': 50000000000.0},
                    {'name': 'Solana', 'tvl': 10000000000.0}]
        raise AssertionError('unexpected url ' + url)

    row = dl.fetch_current(fake)
    assert row['error'] is None
    assert row['total_usd'] == 176781434062.09 + 70641518051.24 + 5e9
    assert row['tvl_total'] == 60000000000.0
    assert row['top_chains'][0][0] == 'Ethereum'


def test_fetch_current_fail_open():
    def boom(url):
        raise RuntimeError('network down')

    row = dl.fetch_current(boom)
    assert row['total_usd'] is None
    assert row['tvl_total'] is None
    assert row['error'] is not None

    def partial(url):
        if 'stablecoins' in url:
            return _sample_stablecoins()
        raise RuntimeError('chains down')

    row2 = dl.fetch_current(partial)
    assert row2['total_usd'] is not None
    assert row2['tvl_total'] is None
    assert row2['error'] is not None  # one leg failed, honestly noted


def test_fetch_daily_changes_with_fake_fetch():
    def fake(url):
        if 'stablecoincharts' in url:
            return [{'date': str(NOW - i * 86400),
                     'totalCirculatingUSD': {'peggedUSD': 290e9 + i * 1e8}}
                    for i in range(30, -1, -1)]
        if 'historicalChainTvl' in url:
            return [{'date': NOW - i * 86400, 'tvl': 80e9 + i * 1e8}
                    for i in range(30, -1, -1)]
        raise AssertionError('unexpected url ' + url)

    # The fake series is pinned to NOW (2026-11-07); pass it in —
    # fetch_daily_changes defaults to wall-clock time, which has
    # drifted past the fake series end + slack.
    out = dl.fetch_daily_changes(fake, NOW)
    assert out['error'] is None
    assert out['total_chg_7d_pct'] is not None
    assert out['tvl_chg_7d_pct'] is not None


def test_fetch_snapshot_fail_open():
    def boom(url):
        raise RuntimeError('all down')

    snap = dl.fetch_snapshot(boom, {}, NOW)
    assert snap['current']['total_usd'] is None
    assert snap['daily']['total_chg_7d_pct'] is None
    assert snap['usdt_usdc']['usdt_usdc_chg_7d_pct'] is None


def test_pulse_line():
    assert dl.pulse_line({}) == 'DeFiLlama: stables no data'
    assert dl.pulse_line(None) == 'DeFiLlama: stables no data'
    line = dl.pulse_line({
        'current': {'total_usd': 292281027860.37, 'dominance': 0.841,
                    'tvl_total': 81044199972.0},
        'daily': {'total_chg_7d_pct': 0.8, 'tvl_chg_7d_pct': -1.2}})
    assert 'stables $292.3B' in line
    assert '(+0.8%/7d)' in line
    assert 'USDT+USDC 84.1%' in line
    assert 'TVL $81.0B' in line
    assert '(-1.2%/7d)' in line
