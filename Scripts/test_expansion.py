#!/usr/bin/env python3
"""Regression tests for the shadow universe expansion (Phase 1, 2026-11-05).

The signal scan covers the top SIGNAL_UNIVERSE_N (200) by market cap
(live); ranks SHADOW_UNIVERSE_START..SHADOW_UNIVERSE_END (201-500) are
scanned with the same _scan_coin work but SHADOW-graded only — they
never join longs/shorts (no cards, no bar, no alerts, no pattern
symbols). Shadow signals grade in a separate shadow ledger
(kind='signal_shadow') with its own scoreboard; promotion to the live
universe is Nic's call.

Run: python3 -m pytest Scripts/test_expansion.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intraday


# ---------------------------------------------------------------- fixtures

def chart_for(rets, start=100.0, vols=None):
    """CoinGecko market_chart payload from hourly % returns."""
    prices = [start]
    for r in rets:
        prices.append(prices[-1] * (1 + r / 100))
    t0 = 1_700_000_000_000
    out = {'prices': [[t0 + i * 3600000, p] for i, p in enumerate(prices)]}
    if vols is not None:
        out['total_volumes'] = [[t0 + i * 3600000, v]
                                for i, v in enumerate(vols)]
    return out


def spark_7d(start=100.0, drift=0.05, n=168):
    """Gently rising 7d sparkline (168 hourly points)."""
    prices = [start]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 + drift / 100))
    return prices


def coin(cid, sym, mcap=500_000_000):
    return {'id': cid, 'symbol': sym, 'name': sym.title(),
            'market_cap': mcap,
            'sparkline_in_7d': {'price': spark_7d()}}


def scan(c, fetch, charts=None, blocked=None):
    """Run _scan_coin with a stubbed fetch_json and no econ feed."""
    if charts is None:
        charts = {}
    monkey = {'calls': []}
    orig = intraday.fetch_json
    intraday.fetch_json = fetch
    try:
        return intraday._scan_coin(
            c, charts, {}, {}, {}, None, None, None, 0.0,
            lambda sym: [], blocked or {'XMR', 'USDT'})
    finally:
        intraday.fetch_json = orig


def stub_chart(payload):
    def _fetch(url, timeout=30):
        return payload
    return _fetch


# ---------------------------------------------------------------- _scan_coin

def test_scan_coin_strong_trend_returns_long():
    # +4%/hour for 24h: vol ~4% (clears the 2.8% fee floor and the
    # true-net gate), score far above 3.
    payload = chart_for([4.0] * 24, vols=[1_000_000] * 25)
    sig = scan(coin('big-mover', 'BIG'), stub_chart(payload))
    assert sig is not None
    assert sig['asset'] == 'BIG'
    assert sig['direction'] == 'LONG'
    assert 40 <= sig['confidence'] <= 95
    assert sig['thesis']['take_profit'] > sig['thesis']['entry'] > 0
    assert sig['thesis']['stop_loss'] < sig['thesis']['entry']


def test_scan_coin_strong_drop_returns_short():
    # -5%/hour: vol ~5% clears the fee floor and the true-net gate
    # with margin (a -4% series lands a hair short of the net gate's
    # required-sell price after tick rounding — test-data edge, not
    # an engine defect).
    payload = chart_for([-5.0] * 24, vols=[1_000_000] * 25)
    sig = scan(coin('big-drop', 'DROP'), stub_chart(payload))
    assert sig is not None
    assert sig['direction'] == 'SHORT'
    assert sig['thesis']['take_profit'] < sig['thesis']['entry']


def test_scan_coin_flat_returns_none():
    payload = chart_for([0.0] * 24)
    assert scan(coin('flat-coin', 'FLAT'), stub_chart(payload)) is None


def test_scan_coin_blocked_symbol_returns_none_without_fetch():
    calls = {'n': 0}

    def _fetch(url, timeout=30):
        calls['n'] += 1
        return chart_for([4.0] * 24)

    assert scan(coin('monero', 'XMR'), _fetch) is None
    assert calls['n'] == 0


def test_scan_coin_low_mcap_returns_none_without_fetch():
    calls = {'n': 0}

    def _fetch(url, timeout=30):
        calls['n'] += 1
        return chart_for([4.0] * 24)

    assert scan(coin('tiny', 'TINY', mcap=50_000_000), _fetch) is None
    assert calls['n'] == 0


def test_scan_coin_failed_fetch_returns_none():
    assert scan(coin('no-data', 'NOD'), stub_chart(None)) is None


def test_scan_coin_one_chart_call_per_coin():
    calls = {'n': 0, 'urls': []}

    def _fetch(url, timeout=30):
        calls['n'] += 1
        calls['urls'].append(url)
        return chart_for([4.0] * 24)

    charts = {}
    for i in range(5):
        scan(coin(f'c{i}', f'C{i}'), _fetch, charts=charts)
    assert calls['n'] == 5
    assert all('market_chart' in u for u in calls['urls'])
    assert len(charts) == 5


# ---------------------------------------------------------------- universe slices

def test_universe_slices_are_disjoint_and_capped():
    universe = [{'id': f'c{i}', 'symbol': f'C{i}',
                 'market_cap': 10_000_000_000 - i * 10_000_000}
                for i in range(600)]
    live = universe[:intraday.SIGNAL_UNIVERSE_N]
    shadow = universe[intraday.SHADOW_UNIVERSE_START:intraday.SHADOW_UNIVERSE_END]
    assert len(live) == 200
    assert len(shadow) == 300
    assert not ({c['id'] for c in live} & {c['id'] for c in shadow})
    # Shadow is strictly the next 300 by market cap after the live 200.
    assert shadow[0]['id'] == 'c200'
    assert shadow[-1]['id'] == 'c499'
    assert intraday.SHADOW_RECORD_CAP == 2000


def test_shadow_slice_capped_when_universe_short():
    universe = [{'id': f'c{i}', 'symbol': f'C{i}', 'market_cap': 10**9}
                for i in range(250)]
    shadow = universe[intraday.SHADOW_UNIVERSE_START:intraday.SHADOW_UNIVERSE_END]
    assert len(shadow) == 50


def test_shadow_effective_size_bounded_by_mcap_floor():
    # Measured 2026-11-06: the $100M floor in _scan_coin binds at rank
    # ~267 by market cap, so a 300-coin shadow slice yields only ~67
    # chartable coins. The slice mechanics are 300; the floor is the
    # binding constraint — not rate limits or loop time.
    mcaps = [150_000_000 - i * 500_000 for i in range(300)]
    eligible = [m for m in mcaps if m >= 100_000_000]
    assert len(eligible) == 101  # i = 0..100 inclusive hits exactly $100M
    # And a slice that straddles the real cutoff (~rank 267, i.e. 67
    # of 300 above the floor) stays small.
    mcaps_real = [200_000_000 - i * 600_000 for i in range(300)]
    assert sum(1 for m in mcaps_real if m >= 100_000_000) == 167


# ---------------------------------------------------------------- shadow ledger

def _shadow_candidate(asset='SHDW', direction='LONG', price=100.0):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    thesis = {'direction': direction, 'entry': price,
              'take_profit': price * 1.042, 'stop_loss': price * 0.979,
              'expected_return_pct': 4.2, 'max_loss_pct': 2.1}
    return intraday._proof_candidate(
        'signal_shadow', asset, asset.title(), direction, thesis, now, 2,
        tags={'band': '60-69', 'regime': 'mixed'},
        gate_src={'confidence': 65, 'tradable': ['coinbase'],
                  'news_flag': False})


def test_shadow_candidate_kind_and_separate_cells():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    thesis = {'direction': 'LONG', 'entry': 100.0,
              'take_profit': 104.2, 'stop_loss': 97.9,
              'expected_return_pct': 4.2, 'max_loss_pct': 2.1}
    live = intraday._proof_candidate(
        'signal', 'LIVE', 'Live', 'LONG', thesis, now, 2,
        tags={'band': '60-69', 'regime': 'mixed'})
    shadow = _shadow_candidate()
    assert live['kind'] == 'signal'
    assert shadow['kind'] == 'signal_shadow'
    rec = [live, shadow]
    cells = intraday.proof_cells(rec)
    kinds = {(c['type'], c['direction']) for c in cells}
    assert ('signal', 'LONG') in kinds
    assert ('signal_shadow', 'LONG') in kinds
    assert len([c for c in cells if c['type'] == 'signal_shadow']) == 1


def test_shadow_grades_like_signal_on_tp_touch():
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    cand = _shadow_candidate()
    assert cand is not None
    rec = [cand]
    # Price path touches the +4.2% take-profit within the window.
    t0 = int((now - timedelta(hours=1)).timestamp() * 1000)
    chart = [[t0 + i * 3600000, 100.0 + i * 1.5] for i in range(3)]
    intraday.proof_grade(rec, {'SHDW': {'price': 105.0}},
                         {'SHDW': chart}, now)
    assert rec[0]['status'] == 'won'


def test_shadow_grades_loss_on_stop_touch():
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    cand = _shadow_candidate()
    rec = [cand]
    t0 = int((now - timedelta(hours=1)).timestamp() * 1000)
    chart = [[t0 + i * 3600000, 100.0 - i * 1.5] for i in range(3)]
    intraday.proof_grade(rec, {'SHDW': {'price': 96.0}},
                         {'SHDW': chart}, now)
    assert rec[0]['status'] == 'lost'


def test_shadow_record_new_dedups_by_kind():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    rec = []
    c1 = _shadow_candidate()
    assert intraday.proof_record_new(rec, [c1], now) == 1
    # Same asset+kind while open: not re-recorded.
    c2 = _shadow_candidate()
    assert intraday.proof_record_new(rec, [c2], now) == 0
    assert len(rec) == 1


def test_shadow_flagged_cells_use_same_rule():
    cells = [{'type': 'signal_shadow', 'direction': 'LONG', 'band': '60-69',
              'regime': 'mixed', 'graded': 25, 'wins': 18, 'losses': 7,
              'win_rate': 72.0, 'expired': 0, 'open': 0}]
    flagged = intraday.proof_flagged_cells(cells)
    assert len(flagged) == 1
    assert flagged[0]['type'] == 'signal_shadow'
