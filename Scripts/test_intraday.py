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
