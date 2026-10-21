#!/usr/bin/env python3
"""Regression tests for the intraday engine's mover/watch trade plans.

2026-10-20 defect: build_mover_plan made a separate CoinGecko
market_chart call per coin to get hourly volatility. Free-tier
throttling failed those calls across the board, so every UP mover
shipped with plan: null ("Plan unavailable — price feed hiccup."),
which also blocked Proof Book mover recording and the verdicts
BUY-via-mover rule.

Fix under test: volatility comes from the sparkline embedded in the
markets payload the engine already fetches (sparkline=true), with a
universe-median fallback, and plan: null only when no volatility
exists anywhere.

Run: python3 -m pytest Scripts/test_intraday.py
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intraday


# ---------------------------------------------------------------- fixtures

def sparkline_from_returns(start, rets_pct):
    """Price series built from a list of hourly % returns."""
    prices = [start]
    for r in rets_pct:
        prices.append(prices[-1] * (1 + r / 100))
    return prices


def rms(rets_pct):
    return (sum(r * r for r in rets_pct) / len(rets_pct)) ** 0.5


def coin(cid, sym, price, chg_1h, chg_24h, spark=None, mcap=500_000_000):
    c = {
        'id': cid,
        'symbol': sym,
        'name': sym.title(),
        'current_price': price,
        'market_cap': mcap,
        'total_volume': 50_000_000,
        'price_change_percentage_1h_in_currency': chg_1h,
        'price_change_percentage_24h_in_currency': chg_24h,
        'price_change_percentage_24h': chg_24h,
        'high_24h': price * 1.05,
        'low_24h': price * 0.95,
    }
    if spark is not None:
        c['sparkline_in_7d'] = {'price': spark}
    return c


# A calm ~1%/h RMS series and a wild ~4%/h RMS series.
CALM_RETS = [1.0, -1.0] * 24
WILD_RETS = [4.0, -4.0] * 24


def fixture_universe(with_sparklines=True):
    calm = sparkline_from_returns(100.0, CALM_RETS)
    wild = sparkline_from_returns(2.0, WILD_RETS)
    return [
        coin('up-coin', 'UPC', 26.42, 0.97, 22.89,
             spark=calm if with_sparklines else None),
        coin('down-coin', 'DNC', 15.94, -7.11, -28.71,
             spark=wild if with_sparklines else None),
        coin('watch-coin', 'WTC', 0.48, 3.5, -2.0,
             spark=wild if with_sparklines else None),
        coin('flat-coin', 'FLC', 10.0, 0.4, 1.0,
             spark=calm if with_sparklines else None),
    ]


def run_main(monkeypatch, tmp_path, universe):
    """Run intraday.main() fully mocked: markets pages serve the fixture,
    every market_chart call 'fails' (throttled free tier), listings empty.
    Returns (published_dict, list_of_fetched_urls)."""
    calls = []

    def fake_fetch(url, timeout=30):
        calls.append(url)
        if 'coins/markets' in url:
            if 'page=1' in url:
                return universe
            return []
        if 'market_chart' in url:
            return None  # the 2026-10-20 throttle: every chart call fails
        return [] if 'coinbase' in url else {}

    monkeypatch.setattr(intraday, 'fetch_json', fake_fetch)
    monkeypatch.setattr(intraday, 'DATA_DIR', str(tmp_path))
    intraday.main()
    with open(os.path.join(str(tmp_path), 'intraday.json')) as f:
        return json.load(f), calls


# ------------------------------------------------------- unit: sparkline_vol

def test_sparkline_vol_matches_rms_math():
    rets = [2.0, -1.0, 0.5, -0.5] * 12  # 48 returns -> 49 prices
    prices = sparkline_from_returns(50.0, rets)
    # Default window is the last 48 prices = the last 47 returns.
    assert intraday.sparkline_vol(prices) == pytest.approx(max(rms(rets[-47:]), 0.3))
    # Widening the window to the whole series uses every return.
    assert intraday.sparkline_vol(prices, points=len(prices)) == \
        pytest.approx(max(rms(rets), 0.3))


def test_sparkline_vol_floor_and_short_series():
    flat = [100.0] * 60  # zero returns -> floored, like hourly_features
    assert intraday.sparkline_vol(flat) == 0.3
    assert intraday.sparkline_vol([1.0, 2.0]) is None
    assert intraday.sparkline_vol([]) is None
    assert intraday.sparkline_vol(None) is None


# ------------------------------------------- (a) plans with ZERO network use

def test_mover_plans_from_sparkline_no_network(monkeypatch):
    """Every UP mover gets a full plan from the sparkline map while
    fetch_json is rigged to explode on ANY call — proving the mover
    plan path makes no network calls at all (no market_chart)."""
    def boom(url, timeout=30):
        raise AssertionError(f'network call in mover plan path: {url}')
    monkeypatch.setattr(intraday, 'fetch_json', boom)

    universe = fixture_universe()
    vol_by_id, median_vol = intraday.sparkline_vol_map(universe)
    assert median_vol is not None

    for c in universe:
        vol = vol_by_id.get(c['id']) or median_vol
        plan = intraday.build_mover_plan(c['current_price'], vol)
        assert plan is not None, c['id']
        assert set(plan) >= {'entry', 'take_profit', 'stop_loss',
                             'exit_by', 'horizon'}
        assert plan['entry'] == c['current_price']
        assert plan['stop_loss'] < plan['entry'] < plan['take_profit']
        # stop = vol x 0.8, target = vol x 1.2 (same ratios as the thesis)
        assert plan['max_loss_pct'] == round(vol * 0.8, 2)
        assert plan['expected_return_pct'] == round(vol * 1.2, 2)


def test_mover_plan_subcent_rounding():
    plan = intraday.build_mover_plan(0.00000456, 2.0)
    assert plan is not None
    assert plan['entry'] == 0.00000456  # 8-decimal rounding, not $0.00
    assert plan['stop_loss'] > 0


# --------------------------- (b) missing sparkline -> universe median fallback

def test_missing_sparkline_falls_back_to_median():
    universe = fixture_universe()
    # A mover with NO sparkline of its own, among sparked peers.
    universe.append(coin('no-spark', 'NSP', 5.0, 1.0, 18.0, spark=None))
    vol_by_id, median_vol = intraday.sparkline_vol_map(universe)
    assert 'no-spark' not in vol_by_id
    assert median_vol is not None

    vol = vol_by_id.get('no-spark') or median_vol
    plan = intraday.build_mover_plan(5.0, vol)
    assert plan is not None
    assert plan['max_loss_pct'] == round(median_vol * 0.8, 2)
    assert plan['expected_return_pct'] == round(median_vol * 1.2, 2)


# --------------------- (c) no volatility anywhere -> null plan (honest null)

def test_no_vol_anywhere_returns_none():
    universe = fixture_universe(with_sparklines=False)
    vol_by_id, median_vol = intraday.sparkline_vol_map(universe)
    assert vol_by_id == {}
    assert median_vol is None
    assert intraday.build_mover_plan(26.42, median_vol) is None
    assert intraday.build_mover_plan(26.42, None) is None


# ----------------------- end-to-end: the 2026-10-20 production failure mode

def test_main_throttled_charts_still_plans_up_movers(monkeypatch, tmp_path):
    """Full main() with every market_chart call failing (what production
    hit on 2026-10-20): UP movers and the top watch item must STILL get
    plans from sparklines; DOWN movers keep the long-only note."""
    out, calls = run_main(monkeypatch, tmp_path, fixture_universe())
    chart_calls = [u for u in calls if 'market_chart' in u]
    assert chart_calls  # the signal path did try charts — and all failed

    movers = {m['asset']: m for m in out['movers']}
    up = movers['UPC']
    assert up['plan'] is not None
    assert up['plan']['entry'] == 26.42
    assert up['plan']['stop_loss'] < 26.42 < up['plan']['take_profit']
    assert 'plan_note' not in up

    down = movers['DNC']
    assert down['plan'] is None
    assert 'long-only' in down['plan_note']

    watch = {w['asset']: w for w in out['watch']}
    assert watch['WTC']['plan'] is not None

    # Proof Book can record the mover now that a plan exists.
    kinds = {(e['kind'], e['asset']) for e in out['proof']['record']}
    assert ('mover', 'UPC') in kinds


def test_main_no_sparklines_anywhere_keeps_honest_null(monkeypatch, tmp_path):
    out, _ = run_main(monkeypatch, tmp_path,
                      fixture_universe(with_sparklines=False))
    movers = {m['asset']: m for m in out['movers']}
    assert movers['UPC']['plan'] is None
    assert movers['UPC']['plan_note'] == 'Plan unavailable — price feed hiccup.'


# ----------------- sub-cent precision: rounding must never collapse a plan
# (2026-10-21: a $0.0024 mover's plan displayed as tgt $0.0024 / stop
# $0.0023 — target == entry — after 4-decimal rounding.)

def test_mover_plan_subcent_strict_order():
    plan = intraday.build_mover_plan(0.0024, 1.5)
    assert plan is not None
    assert plan['stop_loss'] < plan['entry'] < plan['take_profit']
    assert plan['take_profit'] != plan['entry']


def test_thesis_subcent_strict_order():
    f = {'vol': 2.0, 'price': 0.0062, 'chg_1h': 1.5,
         'rsi': 50.0, 'pos_in_range': 0.5}
    th = intraday.build_intraday_thesis('ATH', f, 5.0)
    assert th['direction'] == 'LONG'
    assert th['stop_loss'] < th['entry'] < th['take_profit']
    th = intraday.build_intraday_thesis('ATH', f, -5.0)
    assert th['direction'] == 'SHORT'
    assert th['take_profit'] < th['entry'] < th['stop_loss']


def test_round_price_mirrors_swing_engine():
    assert intraday.round_price(2507.96) == 2507.96
    assert intraday.round_price(5.17) == 5.17
    assert intraday.round_price(0.7101) == 0.7101
    assert intraday.round_price(0.00244704) == 0.00244704
    assert intraday.round_price(0) == 0


# ----------------------- accumulation (setting-up) trigger plans

def test_accum_plan_uses_range_high_within_5pct():
    plan = intraday.build_accum_plan(5.17, 2.0, range_high=5.30)
    assert plan is not None
    assert plan['trigger_price'] == 5.30  # the breakout level itself
    assert plan['entry'] == plan['trigger_price']
    assert plan['stop_loss'] < plan['trigger_price'] < plan['take_profit']
    # mover ratios off the trigger: 1.2x / 0.8x of 2% hourly vol
    assert plan['expected_return_pct'] == round(2.0 * 1.2, 2)
    assert plan['max_loss_pct'] == round(2.0 * 0.8, 2)
    assert plan['exit_by'].endswith('UTC')


def test_accum_plan_vol_step_when_range_high_far_or_missing():
    # Range high 30% above spot is not a near-term level -> vol step.
    plan = intraday.build_accum_plan(5.17, 2.0, range_high=6.72)
    assert plan['trigger_price'] == intraday.round_price(5.17 * 1.01)
    plan = intraday.build_accum_plan(5.17, 2.0, range_high=None)
    assert plan['trigger_price'] == intraday.round_price(5.17 * 1.01)
    # Price already at/above the range high -> vol step too.
    plan = intraday.build_accum_plan(5.17, 2.0, range_high=5.10)
    assert plan['trigger_price'] == intraday.round_price(5.17 * 1.01)


def test_accum_plan_none_without_vol_and_subcent_order():
    assert intraday.build_accum_plan(5.17, None) is None
    assert intraday.build_accum_plan(5.17, 0) is None
    assert intraday.build_accum_plan(0, 2.0) is None
    plan = intraday.build_accum_plan(0.0041, 2.0, range_high=0.0043)
    assert plan['trigger_price'] == 0.0043
    assert plan['stop_loss'] < plan['trigger_price'] < plan['take_profit']


def test_main_accum_entry_gets_trigger_plan(monkeypatch, tmp_path):
    """End-to-end: seed 2 prior observations for the flat coin so the
    current run's 3rd observation (volume +25% vs ~3h ago, price flat)
    trips pattern A — the published accum entry must carry a plan built
    from its sparkline vol, with the 24h high as the trigger."""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    prior = {'accum_history': {'FLC': [
        {'t': (now - _dt.timedelta(hours=3)).isoformat(),
         'price': 10.0, 'vol24h': 40_000_000, 'vol_ratio': 0.08},
        {'t': (now - _dt.timedelta(hours=1)).isoformat(),
         'price': 10.0, 'vol24h': 45_000_000, 'vol_ratio': 0.09},
    ]}, 'proof': {'record': []}}
    with open(os.path.join(str(tmp_path), 'intraday.json'), 'w') as f:
        json.dump(prior, f)
    out, _ = run_main(monkeypatch, tmp_path, fixture_universe())
    accum = {a['asset']: a for a in out['accum']}
    assert 'FLC' in accum, f"accum assets: {sorted(accum)}"
    plan = accum['FLC']['plan']
    assert plan is not None
    # fixture high_24h = price x 1.05 — right at the 5% boundary.
    assert plan['trigger_price'] == 10.5
    assert plan['stop_loss'] < plan['trigger_price'] < plan['take_profit']
