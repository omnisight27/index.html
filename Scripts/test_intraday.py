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


# --------------------------- runner / stretch target (Nic 2026-10-29:
# HYPE printed its +1.6% target then ran to +6% — "a little quick on the sell")

def test_stretch_target_appears_on_strong_momentum():
    """HYPE 2026-10-29 shape: +1.84% last hour, +13.6% 24h, RSI cool — the
    plan should carry a runner strictly above the base target."""
    plan = intraday.build_mover_plan(90.82, 1.34, chg_1h=1.84,
                                     chg_24h=13.62, rsi=58)
    assert plan['stretch_target'] > plan['take_profit'] > plan['entry'] > plan['stop_loss']
    assert plan['stretch_return_pct'] == round(1.34 * 2.0, 2)
    assert 'momentum supports a runner' in plan['stretch_note']


def test_stretch_absent_on_weak_hour():
    plan = intraday.build_mover_plan(90.82, 1.34, chg_1h=0.4,
                                     chg_24h=13.62, rsi=58)
    assert 'stretch_target' not in plan


def test_stretch_absent_when_overbought():
    plan = intraday.build_mover_plan(90.82, 1.34, chg_1h=2.5,
                                     chg_24h=15.0, rsi=76)
    assert 'stretch_target' not in plan


def test_stretch_absent_on_small_day():
    plan = intraday.build_mover_plan(90.82, 1.34, chg_1h=2.5,
                                     chg_24h=6.0, rsi=58)
    assert 'stretch_target' not in plan


def test_stretch_missing_rsi_fails_open():
    """No RSI (short sparkline) — the momentum gates still bind, so a
    runner is allowed; nothing is gated on data we don't have."""
    plan = intraday.build_mover_plan(90.82, 1.34, chg_1h=1.84,
                                     chg_24h=13.62, rsi=None)
    assert plan['stretch_target'] > plan['take_profit']


def test_stretch_backward_compatible_without_momentum():
    """Old two-arg call (all existing callers/tests) never gains a runner;
    the base plan math is untouched."""
    plan = intraday.build_mover_plan(90.82, 1.34)
    assert 'stretch_target' not in plan
    assert plan['stop_loss'] < plan['entry'] < plan['take_profit']


def _series_from_rets(start, rets):
    p, out = start, [start]
    for r in rets:
        p = p * (1 + r / 100)
        out.append(p)
    return out


def test_sparkline_rsi_direction():
    up = _series_from_rets(100.0, [2.0] * 10 + [-1.0] * 4)
    down = _series_from_rets(100.0, [-2.0] * 10 + [1.0] * 4)
    assert intraday.sparkline_rsi(up) > 70
    assert intraday.sparkline_rsi(down) < 30


def test_sparkline_rsi_too_short_is_none():
    assert intraday.sparkline_rsi([100.0] * 5) is None
    assert intraday.sparkline_rsi([]) is None
    assert intraday.sparkline_rsi(None) is None


# --------------------------- Feed #3: OI-gated runner (2026-10-29)

def test_runner_suppressed_on_adverse_oi_flow():
    """Momentum + calm funding want a runner, but OI is piling in against
    the move — withheld, with the reason named."""
    plan = intraday.build_mover_plan(90.82, 1.34, chg_1h=1.84,
                                     chg_24h=13.62, rsi=58,
                                     funding_score=0.0,
                                     oi_score=-0.8, oi_chg_1h_pct=22.0)
    assert 'stretch_target' not in plan
    assert plan['stretch_suppressed'] is True
    assert 'adverse OI flow' in plan['stretch_note']
    assert '+22%/1h' in plan['stretch_note']
    assert plan['stop_loss'] < plan['entry'] < plan['take_profit']


def test_runner_suppressed_names_both_blocks():
    plan = intraday.build_mover_plan(90.82, 1.34, chg_1h=1.84,
                                     chg_24h=13.62, rsi=58,
                                     funding_score=-0.8,
                                     funding_ann_pct=85.0,
                                     oi_score=-0.8, oi_chg_1h_pct=22.0)
    assert plan['stretch_suppressed'] is True
    assert 'crowded longs' in plan['stretch_note']
    assert 'adverse OI flow' in plan['stretch_note']


def test_runner_healthy_oi_flow_allowed():
    plan = intraday.build_mover_plan(90.82, 1.34, chg_1h=1.84,
                                     chg_24h=13.62, rsi=58,
                                     funding_score=0.0,
                                     oi_score=0.76, oi_chg_1h_pct=15.0)
    assert plan['stretch_target'] > plan['take_profit']
    assert 'stretch_suppressed' not in plan


def test_runner_missing_oi_fails_open():
    plan = intraday.build_mover_plan(90.82, 1.34, chg_1h=1.84,
                                     chg_24h=13.62, rsi=58,
                                     funding_score=0.0,
                                     oi_score=None, oi_chg_1h_pct=None)
    assert plan['stretch_target'] > plan['take_profit']


# --------------------------- Feed #2: funding-gated runner (2026-10-29)

def test_runner_suppressed_on_crowded_longs():
    """Strong momentum wants a runner, but funding says the longs are
    crowded — the runner is withheld and the plan says why."""
    plan = intraday.build_mover_plan(90.82, 1.34, chg_1h=1.84,
                                     chg_24h=13.62, rsi=58,
                                     funding_score=-0.8,
                                     funding_ann_pct=85.0)
    assert 'stretch_target' not in plan
    assert plan['stretch_suppressed'] is True
    assert 'crowded longs' in plan['stretch_note']
    assert '+85%/yr' in plan['stretch_note']
    # Base plan untouched — still a valid graded target.
    assert plan['stop_loss'] < plan['entry'] < plan['take_profit']


def test_runner_allowed_on_neutral_funding():
    plan = intraday.build_mover_plan(90.82, 1.34, chg_1h=1.84,
                                     chg_24h=13.62, rsi=58,
                                     funding_score=0.0,
                                     funding_ann_pct=12.0)
    assert plan['stretch_target'] > plan['take_profit']
    assert 'stretch_suppressed' not in plan


def test_runner_missing_funding_fails_open():
    """Feed #2 down: momentum gates still bind, runner allowed."""
    plan = intraday.build_mover_plan(90.82, 1.34, chg_1h=1.84,
                                     chg_24h=13.62, rsi=58,
                                     funding_score=None,
                                     funding_ann_pct=None)
    assert plan['stretch_target'] > plan['take_profit']


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


# ----------------------- Proof Book: scenario cells + setups (2026-10-21)
#
# Nic: "running as many scenarios as possible to bring the win/loss
# ratio higher." The book now records setting-up scenarios alongside
# signals and movers, tags every record into a scenario cell (type x
# direction x confidence band x regime), reports win/loss per cell,
# and keeps all-time per-cell counters in `cumulative` so the record
# survives the 100-entry ledger cap.

import datetime as _dt

BASE = _dt.datetime(2026, 10, 21, 12, 0, tzinfo=_dt.timezone.utc)

SETUP_PLAN = {'trigger_price': 105.0, 'entry': 105.0,
              'take_profit': 115.5, 'stop_loss': 99.75,
              'expected_return_pct': 10.0, 'max_loss_pct': 5.0}


def setup_entry(tags=None):
    e = intraday._proof_candidate('setup', 'SET', 'Setup Coin', 'LONG',
                                  dict(SETUP_PLAN), BASE, 3,
                                  tags=tags or {'band': 'na',
                                                'regime': 'risk_on'})
    assert e is not None
    return e


def chart_from(base, points):
    """points: [(hours_after_base, price)] -> CoinGecko-style [[ms, px]]."""
    return [[(base + _dt.timedelta(hours=h)).timestamp() * 1000, p]
            for h, p in points]


def test_conf_band_mapping():
    assert intraday.conf_band(None) == 'na'
    assert intraday.conf_band(40) == '40s'
    assert intraday.conf_band(55) == '50s'
    assert intraday.conf_band(65) == '60s'
    assert intraday.conf_band(75) == '70s'
    assert intraday.conf_band(80) == '80+'
    assert intraday.conf_band(95) == '80+'


def test_setup_never_triggered_expires_never_a_loss():
    e = setup_entry()
    rec = [e]
    # Price hovers at 100 (below the 105 trigger) past exit_by + grace.
    charts = {'SET': chart_from(BASE, [(1, 100.0), (3, 101.0), (5, 100.5)])}
    mkt = {'SET': {'price': 100.5, 'high_24h': 102.0, 'low_24h': 98.0}}
    intraday.proof_grade(rec, mkt, charts, BASE + _dt.timedelta(hours=6))
    assert e['status'] == 'expired'
    assert e['never_triggered'] is True
    assert e['result_pct'] == 0
    assert e['paper_pnl'] == 0
    # Expired is its own bucket: the win rate ignores it entirely.
    st = intraday.proof_stats(rec)
    assert st['expired'] == 1 and st['losses'] == 0 and st['win_rate'] is None


def test_setup_untriggered_inside_window_stays_open():
    e = setup_entry()
    rec = [e]
    charts = {'SET': chart_from(BASE, [(0.5, 100.0)])}
    mkt = {'SET': {'price': 100.0, 'high_24h': 101.0, 'low_24h': 99.0}}
    intraday.proof_grade(rec, mkt, charts, BASE + _dt.timedelta(hours=1))
    assert e['status'] == 'open'
    assert e['triggered'] is False


def test_setup_trigger_then_target_wins():
    e = setup_entry()
    rec = [e]
    charts = {'SET': chart_from(BASE, [(0.5, 101.0), (1, 106.0), (2, 116.0)])}
    mkt = {'SET': {'price': 116.0, 'high_24h': 116.0, 'low_24h': 100.0}}
    intraday.proof_grade(rec, mkt, charts, BASE + _dt.timedelta(hours=2))
    assert e['triggered'] is True
    assert e['status'] == 'won'
    assert e['result_pct'] == 10.0
    assert e['paper_pnl'] == 100.0


def test_setup_trigger_then_stop_loses():
    e = setup_entry()
    rec = [e]
    charts = {'SET': chart_from(BASE, [(0.5, 106.0), (1, 99.0)])}
    mkt = {'SET': {'price': 99.0, 'high_24h': 106.0, 'low_24h': 99.0}}
    intraday.proof_grade(rec, mkt, charts, BASE + _dt.timedelta(hours=2))
    assert e['triggered'] is True
    assert e['status'] == 'lost'
    assert e['result_pct'] == -5.0


def test_setup_pretrigger_dip_below_stop_is_not_a_loss():
    # The stop (99.75) sits between spot (~100) and the trigger (105)
    # by construction; a dip to 98 BEFORE the trigger fills must not
    # grade — only post-trigger observations count.
    e = setup_entry()
    rec = [e]
    charts = {'SET': chart_from(BASE, [(0.5, 98.0), (1, 106.0), (2, 116.0)])}
    mkt = {'SET': {'price': 116.0, 'high_24h': 116.0, 'low_24h': 98.0}}
    intraday.proof_grade(rec, mkt, charts, BASE + _dt.timedelta(hours=2))
    assert e['status'] == 'won'


def test_setup_triggered_then_window_end_grades_at_drift():
    e = setup_entry()
    rec = [e]
    # Triggers at 106, then drifts to 108 (above trigger, below
    # target) by the time the window closes -> expired at drift.
    charts = {'SET': chart_from(BASE, [(1, 106.0), (3, 108.0), (5, 108.0)])}
    mkt = {'SET': {'price': 108.0, 'high_24h': 109.0, 'low_24h': 100.0}}
    intraday.proof_grade(rec, mkt, charts, BASE + _dt.timedelta(hours=6))
    assert e['status'] == 'expired'
    assert e['never_triggered'] is False
    assert e['result_pct'] == pytest.approx((108.0 - 105.0) / 105.0 * 100, abs=0.01)


def test_proof_cells_grouping_bands_and_regimes():
    def entry(kind, direction, band, regime, status, pnl=0.0):
        return {'kind': kind, 'direction': direction, 'band': band,
                'regime': regime, 'status': status,
                'result_pct': pnl, 'paper_pnl': pnl * 10,
                'never_triggered': False}
    rec = [
        entry('signal', 'LONG', '70s', 'risk_on', 'won', 2.0),
        entry('signal', 'LONG', '70s', 'risk_on', 'lost', -1.0),
        entry('signal', 'LONG', '70s', 'risk_on', 'open'),
        entry('mover', 'LONG', 'na', 'risk_off', 'won', 1.0),
        # Legacy entry: no tags at all -> band 'na', regime 'unknown'.
        {'kind': 'mover', 'direction': 'LONG', 'status': 'lost',
         'result_pct': -1.0, 'paper_pnl': -10.0},
    ]
    rows = {(r['type'], r['direction'], r['band'], r['regime']): r
            for r in intraday.proof_cells(rec)}
    sig = rows[('signal', 'LONG', '70s', 'risk_on')]
    assert sig['graded'] == 2 and sig['wins'] == 1 and sig['losses'] == 1
    assert sig['open'] == 1 and sig['win_rate'] == 50.0
    assert rows[('mover', 'LONG', 'na', 'risk_off')]['win_rate'] == 100.0
    legacy = rows[('mover', 'LONG', 'na', 'unknown')]
    assert legacy['losses'] == 1 and legacy['win_rate'] == 0.0


def test_cumulative_counters_survive_ledger_roll():
    # 105 closed entries in one cell; the cap trims the oldest 5.
    rec = []
    for i in range(105):
        rec.append({'kind': 'mover', 'direction': 'LONG', 'band': 'na',
                    'regime': 'neutral',
                    'status': 'won' if i % 2 == 0 else 'lost',
                    'result_pct': 1.0, 'paper_pnl': 10.0,
                    'never_triggered': False})
    before = intraday.proof_cells(rec)
    trimmed, dropped = intraday._proof_trim(rec, cap=100)
    assert len(trimmed) == 100 and len(dropped) == 5
    cumulative = intraday.proof_roll_cumulative({}, dropped)
    after = intraday.proof_cells(trimmed, cumulative)
    assert after == before  # nothing was lost to the cap


def test_flagged_cells_need_n20_and_extreme_rate():
    def row(graded, wins, losses):
        wl = wins + losses
        return {'type': 'mover', 'direction': 'LONG', 'band': 'na',
                'regime': 'neutral', 'graded': graded, 'wins': wins,
                'losses': losses, 'expired': graded - wl,
                'never_triggered': 0, 'open': 0,
                'win_rate': round(wins / wl * 100, 1) if wl else None,
                'paper_pnl_total': 0.0}
    cells = [row(20, 13, 7),    # 65% at n=20 -> include candidate
             row(19, 19, 0),    # 100% but n<20 -> no flag
             row(30, 9, 21),    # 30% at n=30 -> exclude candidate
             row(40, 20, 20)]   # 50% -> mid, no flag
    flags = intraday.proof_flagged_cells(cells)
    assert len(flags) == 2
    assert flags[0]['candidate'] == 'include' and flags[0]['win_rate'] == 65.0
    assert flags[1]['candidate'] == 'exclude' and flags[1]['win_rate'] == 30.0
    assert all(f['recommendation_only'] for f in flags)


def test_backfill_regimes_from_swing_runs():
    runs = [(BASE - _dt.timedelta(days=2), 'RISK_OFF'),
            (BASE - _dt.timedelta(hours=1), 'RISK_ON')]
    old = {'kind': 'mover', 'direction': 'LONG', 'status': 'lost',
           'ts_open': (BASE - _dt.timedelta(days=1)).isoformat()}
    mid = {'kind': 'signal', 'direction': 'SHORT', 'status': 'won',
           'ts_open': BASE.isoformat()}
    ancient = {'kind': 'signal', 'direction': 'LONG', 'status': 'won',
               'ts_open': (BASE - _dt.timedelta(days=30)).isoformat()}
    tagged = {'kind': 'signal', 'direction': 'LONG', 'status': 'open',
              'regime': 'neutral', 'ts_open': BASE.isoformat()}
    rec = [old, mid, ancient, tagged]
    n = intraday.proof_backfill_regimes(rec, runs)
    assert n == 2
    assert old['regime'] == 'risk_off'      # run in force a day ago
    assert mid['regime'] == 'risk_on'       # latest run at/before open
    assert 'regime' not in ancient          # predates every run
    assert tagged['regime'] == 'neutral'    # never overwritten


def test_main_records_setup_scenarios_and_publishes_cells(monkeypatch, tmp_path):
    """End-to-end (same seeded-accum fixture as the trigger-plan test):
    the published payload must carry the setup in the Proof Book, the
    per-cell table, and the cumulative block."""
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
    kinds = {(e['kind'], e['asset']) for e in out['proof']['record']}
    assert ('setup', 'FLC') in kinds
    setup = next(e for e in out['proof']['record'] if e['kind'] == 'setup')
    assert setup['trigger_price'] == 10.5
    assert setup['triggered'] is False
    assert setup['band'] == 'na'
    assert 'cumulative' in out['proof']
    cells = out['proof']['stats']['cells']
    setup_cells = [c for c in cells if c['type'] == 'setup']
    assert setup_cells and setup_cells[0]['open'] == 1
    assert 'flagged_cells' in out['proof']['stats']


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


# ---------------------------------------------------------------------------
# After-exit tracker (2026-10-22): for every won/lost call, watch the
# price for 24h AFTER the exit. Winners that keep running answer
# "too quick to sell?"; stops that recover are whipsaw evidence.
# Measurement only — grading is untouched by these tests' subject.
# ---------------------------------------------------------------------------

def closed_entry(status, direction='LONG', entry=100.0, tp=110.0, sl=95.0,
                 exit_h=0.0, exp=10.0, maxl=5.0, asset='TST'):
    """A ledger entry closed at BASE + exit_h with grading's own
    conventions: won credits expected_return_pct, lost debits
    max_loss_pct; ts_closed is the exit time."""
    closed = BASE + _dt.timedelta(hours=exit_h)
    return {
        'id': f'signal:{asset}:x', 'kind': 'signal', 'asset': asset,
        'name': 'Test Coin', 'direction': direction, 'entry': entry,
        'take_profit': tp, 'stop_loss': sl, 'status': status,
        'result_pct': exp if status == 'won' else -maxl,
        'paper_pnl': (exp if status == 'won' else -maxl) * 10,
        'expected_return_pct': exp, 'max_loss_pct': maxl,
        'ts_open': (closed - _dt.timedelta(hours=3)).isoformat(),
        'ts_closed': closed.isoformat(),
        'band': '70s', 'regime': 'risk_on',
    }


def test_post_exit_target_exit_runs_further():
    e = closed_entry('won')  # LONG 100 -> target 110, captured 10%
    rec = [e]
    chart = {'TST': chart_from(BASE, [(-1, 500.0),   # pre-exit: ignored
                                      (1, 114.0), (2, 116.0),
                                      (10, 118.0), (24, 112.0),
                                      (25, 120.0)])}  # post-end: not an extreme
    intraday.proof_track_post_exit(rec, {}, chart,
                                   BASE + _dt.timedelta(hours=2))
    pe = e['post_exit']
    assert pe['exit_price'] == 110.0 and pe['exit_kind'] == 'target'
    assert pe['samples'] == 2 and pe['complete'] is False
    assert pe['post_exit_high_pct'] == pytest.approx(5.45, abs=0.01)
    # Re-running the same window must not double-fold chart points.
    intraday.proof_track_post_exit(rec, {}, chart,
                                   BASE + _dt.timedelta(hours=2))
    assert e['post_exit']['samples'] == 2
    n = intraday.proof_track_post_exit(rec, {}, chart,
                                       BASE + _dt.timedelta(hours=25))
    assert n == 1
    pe = e['post_exit']
    assert pe['complete'] is True and pe['samples'] == 4
    # High is the +10h print (118): (118-110)/110 = 7.27%.
    assert pe['post_exit_high_pct'] == pytest.approx(7.27, abs=0.01)
    # Close is the price AT +24h (112), not the +25h print.
    assert pe['post_exit_close_pct'] == pytest.approx(1.82, abs=0.01)
    assert pe['ran_further'] is True       # 7.27 >= 50% of captured 10%
    assert pe['left_on_table_pct'] == pytest.approx(7.27, abs=0.01)
    # Grading untouched.
    assert e['status'] == 'won' and e['result_pct'] == 10.0


def test_post_exit_stop_exit_recovers_is_whipsaw():
    e = closed_entry('lost')  # LONG 100 -> stop 95
    rec = [e]
    chart = {'TST': chart_from(BASE, [(1, 93.0), (3, 97.0), (5, 101.0),
                                      (24, 99.0)])}
    intraday.proof_track_post_exit(rec, {}, chart,
                                   BASE + _dt.timedelta(hours=6))
    pe = e['post_exit']
    assert pe['exit_price'] == 95.0 and pe['exit_kind'] == 'stop'
    assert pe['recovered'] is True         # 101 >= entry 100 in-window
    assert pe['post_exit_low_pct'] == pytest.approx(-2.11, abs=0.01)
    intraday.proof_track_post_exit(rec, {}, chart,
                                   BASE + _dt.timedelta(hours=25))
    assert e['post_exit']['complete'] is True
    assert e['post_exit']['samples'] == 4
    st = intraday.proof_stats(rec)['post_exit']
    assert st['stop_exits'] == 1 and st['recovered_after_stop'] == 1


def test_post_exit_stop_exit_keeps_falling_no_recovery():
    e = closed_entry('lost')
    rec = [e]
    chart = {'TST': chart_from(BASE, [(1, 94.0), (6, 90.0),
                                      (23, 88.0), (24, 88.0)])}
    intraday.proof_track_post_exit(rec, {}, chart,
                                   BASE + _dt.timedelta(hours=12))
    intraday.proof_track_post_exit(rec, {}, chart,
                                   BASE + _dt.timedelta(hours=25))
    pe = e['post_exit']
    assert pe['complete'] is True
    assert pe['recovered'] is False        # never got back to entry 100
    assert pe['post_exit_high_pct'] == pytest.approx(-1.05, abs=0.01)
    assert pe['post_exit_low_pct'] == pytest.approx(-7.37, abs=0.01)
    assert pe['post_exit_close_pct'] == pytest.approx(-7.37, abs=0.01)


def test_post_exit_short_direction_flags():
    # SHORT target exit: entry 100, target 90 (exit 90, captured 10%).
    w = closed_entry('won', direction='SHORT', tp=90.0, sl=105.0,
                     asset='SHW')
    chart_w = {'SHW': chart_from(BASE, [(3, 84.0), (24, 86.0)])}
    intraday.proof_track_post_exit([w], {}, chart_w,
                                   BASE + _dt.timedelta(hours=4))
    intraday.proof_track_post_exit([w], {}, chart_w,
                                   BASE + _dt.timedelta(hours=25))
    pe = w['post_exit']
    # Price fell a further (84-90)/90 = -6.67% past the exit: for a
    # short that is a +6.67% favorable run, >= 50% of the captured 10%.
    assert pe['ran_further'] is True
    assert pe['left_on_table_pct'] == pytest.approx(6.67, abs=0.01)
    # SHORT stop exit: entry 100, stop 105 (exit 105). Recovery = the
    # price falling back to/below entry inside the window.
    s = closed_entry('lost', direction='SHORT', tp=90.0, sl=105.0,
                     exp=10.0, maxl=5.0, asset='SHL')
    chart_s = {'SHL': chart_from(BASE, [(2, 108.0), (8, 99.0),
                                        (24, 102.0)])}
    intraday.proof_track_post_exit([s], {}, chart_s,
                                   BASE + _dt.timedelta(hours=9))
    intraday.proof_track_post_exit([s], {}, chart_s,
                                   BASE + _dt.timedelta(hours=25))
    assert s['post_exit']['recovered'] is True


def test_post_exit_incomplete_window_and_untracked_records():
    e = closed_entry('won')
    rec = [e]
    chart = {'TST': chart_from(BASE, [(1, 112.0)])}
    intraday.proof_track_post_exit(rec, {}, chart,
                                   BASE + _dt.timedelta(hours=3))
    assert e['post_exit']['complete'] is False
    # An open window is not yet a tracked reading.
    assert intraday.proof_stats(rec)['post_exit']['tracked'] == 0
    # Expired calls are never tracked (no target/stop exit to judge).
    x = closed_entry('expired')
    x['result_pct'] = 1.5
    intraday.proof_track_post_exit([x], {}, chart, BASE)
    assert 'post_exit' not in x
    # A record whose window fully elapsed before tracking existed is
    # left alone — forward-only, never backfilled from thin air.
    old = closed_entry('won', exit_h=-30.0)
    intraday.proof_track_post_exit([old], {}, chart, BASE)
    assert 'post_exit' not in old


def test_post_exit_snapshot_only_window_still_completes():
    e = closed_entry('won')
    rec = [e]
    # No chart at all: one current-price snapshot inside the window,
    # then silence. Completion falls back to the last observation.
    mkt = {'TST': {'price': 111.0, 'high_24h': None, 'low_24h': None}}
    intraday.proof_track_post_exit(rec, mkt, {},
                                   BASE + _dt.timedelta(hours=1))
    assert e['post_exit']['samples'] == 1
    n = intraday.proof_track_post_exit(rec, {}, {},
                                       BASE + _dt.timedelta(hours=26))
    assert n == 1
    pe = e['post_exit']
    assert pe['complete'] is True and pe['samples'] == 1
    assert pe['post_exit_close_pct'] == pytest.approx(0.91, abs=0.01)


def test_post_exit_stats_block_aggregation():
    a = closed_entry('won', asset='AAA')            # runs further
    b = closed_entry('won', asset='BBB')            # barely runs
    c = closed_entry('lost', asset='CCC')           # recovers
    charts = {
        'AAA': chart_from(BASE, [(1, 114.0), (10, 118.0), (24, 112.0)]),
        'BBB': chart_from(BASE, [(2, 111.0), (24, 110.5)]),
        'CCC': chart_from(BASE, [(1, 93.0), (5, 101.0), (24, 99.0)]),
    }
    rec = [a, b, c]
    intraday.proof_track_post_exit(rec, {}, charts,
                                   BASE + _dt.timedelta(hours=12))
    intraday.proof_track_post_exit(rec, {}, charts,
                                   BASE + _dt.timedelta(hours=25))
    st = intraday.proof_stats(rec)
    # Existing stats shape is intact alongside the new block.
    assert st['wins'] == 2 and st['losses'] == 1
    pe = st['post_exit']
    assert pe['tracked'] == 3
    assert pe['target_exits'] == 2 and pe['ran_further'] == 1
    # (7.27 for AAA + 0.91 for BBB) / 2 = 4.09 over target exits.
    assert pe['avg_post_exit_high_pct'] == pytest.approx(4.09, abs=0.01)
    assert pe['stop_exits'] == 1 and pe['recovered_after_stop'] == 1


# ------------------------------------------------- codeword gate (shadow)
# 2026-10-23: the gate stamps each new Proof Book record with six
# parity checks computed ONLY from fields its family really carries
# (missing input -> None, never guessed) and aggregates a pass/fail
# split in proof.stats['codeword_gate']. Measurement only: no alert,
# verdict or grading path reads it. These tests pin the check math,
# the punctured-pass rule (>= 4 evaluable checks, all True), and the
# aggregation — the live-behavior guarantee is that nothing else in
# the suite moved.

def gate_signal(**over):
    """All-pass signal gate kwargs; `over` breaks one thing at a time."""
    kw = dict(kind='signal', direction='LONG', entry=100.0,
              take_profit=110.0, stop_loss=95.0, regime='risk_on',
              confidence=75, tradable=['coinbase'])
    kw.update(over)
    return kw


def test_gate_all_checks_pass_signal():
    g = intraday.proof_gate(**gate_signal())
    assert g['pass'] is True
    assert g['checks'] == {'confidence': True, 'alignment': None,
                           'tradable': True, 'news_clean': True,
                           'plan_complete': True, 'regime_ok': True}


def test_gate_each_check_failing_flips_pass():
    # confidence below the proven band
    g = intraday.proof_gate(**gate_signal(confidence=65))
    assert g['checks']['confidence'] is False and g['pass'] is False
    # not tradable on Nic's venues
    g = intraday.proof_gate(**gate_signal(tradable=[]))
    assert g['checks']['tradable'] is False and g['pass'] is False
    g = intraday.proof_gate(**gate_signal(tradable=['kraken']))
    assert g['checks']['tradable'] is False and g['pass'] is False
    # news-flagged
    g = intraday.proof_gate(**gate_signal(news_flag='SEC headline'))
    assert g['checks']['news_clean'] is False and g['pass'] is False
    # plan inverted: target below entry on a LONG
    g = intraday.proof_gate(**gate_signal(take_profit=90.0))
    assert g['checks']['plan_complete'] is False and g['pass'] is False
    # caution regime at record time
    g = intraday.proof_gate(**gate_signal(regime='risk_off'))
    assert g['checks']['regime_ok'] is False and g['pass'] is False


def test_gate_short_plan_mirror_and_alignment_mover():
    # SHORT: target < entry < stop is the sane order.
    g = intraday.proof_gate(**gate_signal(direction='SHORT',
                                          take_profit=90.0,
                                          stop_loss=105.0))
    assert g['checks']['plan_complete'] is True and g['pass'] is True
    g = intraday.proof_gate(**gate_signal(direction='SHORT'))
    assert g['checks']['plan_complete'] is False and g['pass'] is False
    # Movers carry no confidence (punctured check) but DO carry the
    # 1h/24h agreement check from their own stored fields.
    kw = dict(kind='mover', direction='LONG', entry=100.0,
              take_profit=110.0, stop_loss=95.0, regime='neutral',
              tradable=['cryptocom'], chg_1h=6.0, chg_24h=18.0)
    g = intraday.proof_gate(**kw)
    assert g['checks']['confidence'] is None
    assert g['checks']['alignment'] is True and g['pass'] is True
    # 1h turned against the 24h move -> alignment fails, pass flips.
    g = intraday.proof_gate(**dict(kw, chg_1h=-1.5))
    assert g['checks']['alignment'] is False and g['pass'] is False


def test_gate_unknowns_and_min_evaluable():
    # Band stands in for confidence exactly (backfill path): >= 70
    # bands pass, below fails, 'na'/missing stays unknown.
    assert intraday.proof_gate(**gate_signal(confidence=None,
                                             band='70s')
                               )['checks']['confidence'] is True
    assert intraday.proof_gate(**gate_signal(confidence=None,
                                             band='60s')
                               )['checks']['confidence'] is False
    assert intraday.proof_gate(**gate_signal(confidence=None,
                                             band='na')
                               )['checks']['confidence'] is None
    # An unreadable regime is unknown, not benign.
    g = intraday.proof_gate(**gate_signal(regime='unknown'))
    assert g['checks']['regime_ok'] is None and g['pass'] is False
    # Too few evaluable checks can never pass, even with no failure:
    # a bare setup source (tradable only) evaluates tradable +
    # news_clean + plan_complete + regime_ok = 4 -> can pass...
    g = intraday.proof_gate('setup', 'LONG', 100.0, 110.0, 95.0,
                            'risk_on', tradable=['coinbase'])
    assert g['pass'] is True
    # ...but strip the regime too and only 3 checks remain evaluable.
    g = intraday.proof_gate('setup', 'LONG', 100.0, 110.0, 95.0,
                            'unknown', tradable=['coinbase'])
    assert g['pass'] is False


def test_gate_stamped_on_candidate():
    plan = {'entry': 100.0, 'take_profit': 110.0, 'stop_loss': 95.0,
            'expected_return_pct': 10.0, 'max_loss_pct': 5.0}
    e = intraday._proof_candidate(
        'signal', 'GST', 'Gate Coin', 'LONG', dict(plan), BASE, 2,
        tags={'band': '70s', 'regime': 'risk_on'},
        gate_src={'confidence': 75, 'tradable': ['coinbase']})
    assert e['gate']['pass'] is True
    assert e['gate']['checks']['confidence'] is True
    # No gate_src: the record still carries a gate, computed from its
    # own fields alone — source-only checks read unknown.
    e2 = setup_entry()
    assert e2['gate']['checks']['tradable'] is None
    assert e2['gate']['checks']['plan_complete'] is True
    assert e2['gate']['pass'] is False  # only 3 evaluable checks


def test_gate_stats_aggregation():
    def rec(gate, status, pnl):
        e = {'kind': 'signal', 'direction': 'LONG', 'status': status,
             'result_pct': pnl, 'paper_pnl': pnl}
        if gate is not None:
            e['gate'] = {'pass': gate, 'checks': {}}
        return e
    recs = [rec(True, 'won', 50.0), rec(True, 'lost', -20.0),
            rec(True, 'open', None), rec(False, 'won', 10.0),
            rec(False, 'expired', 0.0), rec(None, 'lost', -5.0)]
    cg = intraday.proof_stats(recs)['codeword_gate']
    assert cg['evaluated'] == 5 and cg['unevaluated'] == 1
    assert cg['pass']['graded'] == 2 and cg['pass']['wins'] == 1
    assert cg['pass']['losses'] == 1 and cg['pass']['open'] == 1
    assert cg['pass']['win_rate'] == 50.0
    assert cg['pass']['paper_pnl_total'] == 30.0
    assert cg['fail']['graded'] == 2 and cg['fail']['expired'] == 1
    assert cg['fail']['paper_pnl_total'] == 10.0
    # Pre-existing stats are untouched by the new block.
    st = intraday.proof_stats(recs)
    assert st['graded'] == 5 and st['wins'] == 2 and st['losses'] == 2
