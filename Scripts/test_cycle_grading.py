"""Cycle research track, Phase 2 — shadow grading tests.

Covers the spec §4 grading math (+4.2% gross / -2.1% adverse, EXPIRED
rule, $28 / -$35 on $1,000 paper), the 24h/7d candidate shapes (one
row per variable per asset-day), the dominance direction rules
(DOM_UP BTC-only, DOM_DOWN altcoin-only, DOM_FLAT control on all),
the FNG_XG contrarian SHORT, the daily-row dedup (no open-block for
7d rows), and the cycle scoreboard (base rate, control rate, Wilson
lower bound, expectancy, drawdown, halving disclosure).

Run: python3 -m pytest Scripts/test_cycle_grading.py
"""
import sys
import os
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intraday

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def _state(**over):
    st = {'date': '2026-11-05', 'halving_band': 'H5', 'halving_day': 930,
          'month': 'November', 'dom_band': 'DOM_DOWN',
          'dom_change_pts': -1.5, 'fng_band': 'FNG_F', 'fng_value': 32,
          'dow': 'Thursday', 'dd_band': 'DD_MID', 'dd_pct': 15.0}
    st.update(over)
    return st


def _mkt(price, assets=('BTC', 'ETH')):
    return {a: {'price': price, 'high_24h': price * 1.05,
                'low_24h': price * 0.95} for a in assets}


def _chart(points, asset='BTC'):
    """points: [(hours_after_T0, price)] -> {asset: [[ts_ms, price]]}."""
    return {asset: [[int((T0 + timedelta(hours=h)).timestamp() * 1000), p]
                    for h, p in points]}


# ---------------------------------------------------------------- candidates

def test_cycle_candidate_shapes_altcoin():
    cands = intraday.proof_cycle_candidates(
        _state(), ['ETH'], _mkt(100.0), {'ETH': 'Ethereum'}, T0, 'neutral')
    # 6 variables (DOM_DOWN grades alts) x 2 horizons.
    assert len(cands) == 12
    assert {c['kind'] for c in cands} == {'cycle'}
    by_var = {}
    for c in cands:
        by_var.setdefault(c['variable'], []).append(c)
    assert set(by_var) == {'halving', 'month', 'dom', 'fng', 'dow', 'dd'}
    for var, group in by_var.items():
        assert {c['horizon'] for c in group} == {'24h', '7d'}
        assert {c['direction'] for c in group} == {'LONG'}
    assert by_var['halving'][0]['band'] == 'H5'
    assert by_var['month'][0]['band'] == 'November'
    assert by_var['dom'][0]['band'] == 'DOM_DOWN'
    assert by_var['fng'][0]['band'] == 'FNG_F'
    assert by_var['dd'][0]['band'] == 'DD_MID'
    assert by_var['halving'][0]['id'] != by_var['month'][0]['id']
    assert by_var['halving'][0]['entry'] == 100.0
    assert by_var['halving'][0]['take_profit'] == pytest.approx(104.2)
    assert by_var['halving'][0]['stop_loss'] == pytest.approx(97.9)
    assert by_var['halving'][0]['expected_return_pct'] == 2.8
    assert by_var['halving'][0]['max_loss_pct'] == 3.5


def test_cycle_candidate_btc_skips_dom_down():
    cands = intraday.proof_cycle_candidates(
        _state(), ['BTC'], _mkt(100.0), {}, T0, 'neutral')
    # DOM_DOWN grades altcoins only: 5 variables x 2 horizons.
    assert len(cands) == 10
    assert {c['variable'] for c in cands} == \
        {'halving', 'month', 'fng', 'dow', 'dd'}


def test_cycle_candidate_dom_up_btc_only():
    st = _state(dom_band='DOM_UP', dom_change_pts=1.2)
    btc = intraday.proof_cycle_candidates(st, ['BTC'], _mkt(100.0), {},
                                          T0, 'neutral')
    assert len(btc) == 12  # all 6 variables incl. DOM_UP
    assert {c['variable'] for c in btc} == \
        {'halving', 'month', 'dom', 'fng', 'dow', 'dd'}
    eth = intraday.proof_cycle_candidates(st, ['ETH'], _mkt(100.0), {},
                                          T0, 'neutral')
    assert len(eth) == 10  # DOM_UP does not grade alts
    assert 'dom' not in {c['variable'] for c in eth}


def test_cycle_candidate_dom_flat_control_all():
    st = _state(dom_band='DOM_FLAT', dom_change_pts=0.2)
    for asset in ('BTC', 'ETH'):
        cands = intraday.proof_cycle_candidates(st, [asset], _mkt(100.0),
                                                {}, T0, 'neutral')
        assert len(cands) == 12
        dom = [c for c in cands if c['variable'] == 'dom']
        assert len(dom) == 2 and dom[0]['direction'] == 'LONG'


def test_cycle_candidate_fng_extreme_greed_short():
    st = _state(fng_band='FNG_XG', fng_value=82)
    cands = intraday.proof_cycle_candidates(st, ['ETH'], _mkt(100.0), {},
                                            T0, 'neutral')
    fng = [c for c in cands if c['variable'] == 'fng']
    assert len(fng) == 2
    assert {c['direction'] for c in fng} == {'SHORT'}
    assert fng[0]['take_profit'] == pytest.approx(95.8)
    assert fng[0]['stop_loss'] == pytest.approx(102.1)
    # Everything else stays LONG.
    assert {c['direction'] for c in cands if c['variable'] != 'fng'} == \
        {'LONG'}


def test_cycle_candidate_missing_bands_skipped():
    st = _state(dom_band=None, dom_change_pts=None, fng_band=None,
                fng_value=None, dd_band=None, dd_pct=None)
    cands = intraday.proof_cycle_candidates(st, ['ETH'], _mkt(100.0), {},
                                            T0, 'neutral')
    assert {c['variable'] for c in cands} == {'halving', 'month', 'dow'}
    assert intraday.proof_cycle_candidates(None, ['ETH'], _mkt(100.0),
                                           {}, T0, 'neutral') == []
    assert intraday.proof_cycle_candidates(_state(), ['ETH'], {}, {},
                                           T0, 'neutral') == []
    assert intraday.proof_cycle_candidates(_state(), [], _mkt(100.0), {},
                                           T0, 'neutral') == []


# ---------------------------------------------------------------- grading

def _entry(horizon='24h', opened_hours_ago=0, asset='BTC',
           variable='halving', direction='LONG'):
    now_open = T0 - timedelta(hours=opened_hours_ago)
    cands = intraday.proof_cycle_candidates(
        _state(), [asset], _mkt(100.0, (asset,)), {}, now_open, 'neutral')
    return next(c for c in cands
                if c['horizon'] == horizon and c['variable'] == variable
                and c['direction'] == direction)


def test_cycle_win_on_target_touch():
    e = _entry()
    intraday.proof_grade([e], _mkt(100.0), _chart([(1, 104.5)]),
                         T0 + timedelta(hours=1))
    assert e['status'] == 'won'
    assert e['result_pct'] == 2.8
    assert e['paper_pnl'] == 28.0


def test_cycle_loss_when_adverse_hits_first():
    e = _entry()
    intraday.proof_grade([e], _mkt(105.0),
                         _chart([(1, 97.8), (2, 105.0)]),
                         T0 + timedelta(hours=2))
    assert e['status'] == 'lost'
    assert e['result_pct'] == -3.5
    assert e['paper_pnl'] == -35.0


def test_cycle_short_mirror():
    # FNG_XG state needed for a SHORT fng row; build it directly.
    st = _state(fng_band='FNG_XG', fng_value=82)
    cands = intraday.proof_cycle_candidates(st, ['BTC'], _mkt(100.0), {},
                                            T0, 'neutral')
    e = next(c for c in cands if c['variable'] == 'fng'
             and c['horizon'] == '24h')
    intraday.proof_grade([e], _mkt(95.0), _chart([(1, 95.5)]),
                         T0 + timedelta(hours=1))
    assert e['status'] == 'won' and e['paper_pnl'] == 28.0


def test_cycle_24h_stays_open_before_horizon():
    e = _entry()
    intraday.proof_grade([e], _mkt(101.0), _chart([(1, 101.0)]),
                         T0 + timedelta(hours=1))
    assert e['status'] == 'open'


def test_cycle_7d_stays_open_past_24h():
    # A 7d row opened 2 days ago, +1%: still open (no 24h expiry).
    e = _entry(horizon='7d', opened_hours_ago=48)
    intraday.proof_grade([e], _mkt(101.0),
                         _chart([(49, 101.0)]), T0)
    assert e['status'] == 'open'


def test_cycle_7d_expired_win_when_drift_covers_costs():
    # 7d horizon, opened 10 days ago: window over (exit_by + 2h grace
    # and the 8-day hard cap both passed).
    e = _entry(horizon='7d', opened_hours_ago=240)
    intraday.proof_grade([e], _mkt(101.8),
                         _chart([(239, 101.0), (240, 101.8)]), T0)
    assert e['status'] == 'won'
    assert e['cycle_expired'] is True
    assert e['result_pct'] == pytest.approx(0.4)
    assert e['paper_pnl'] == pytest.approx(4.0)


def test_cycle_7d_expired_loss_when_drift_does_not_cover_costs():
    e = _entry(horizon='7d', opened_hours_ago=240)
    intraday.proof_grade([e], _mkt(100.5), _chart([(240, 100.5)]), T0)
    assert e['status'] == 'lost'
    assert e['cycle_expired'] is True
    assert e['result_pct'] == pytest.approx(-0.9)
    assert e['paper_pnl'] == pytest.approx(-9.0)


def test_cycle_7d_not_cut_short_by_7d_cap():
    # Opened 7 days + 1h ago: exit_by (7d) has passed but the +2h
    # grace has not — the row must stay open (the pattern track's
    # 7-day cap would have closed it here).
    e = _entry(horizon='7d', opened_hours_ago=169)
    intraday.proof_grade([e], _mkt(101.0), _chart([(169, 101.0)]), T0)
    assert e['status'] == 'open'


def test_cycle_green_at_exit_is_not_trailed():
    e = _entry(horizon='7d', opened_hours_ago=240)
    intraday.proof_grade([e], _mkt(103.0), _chart([(240, 103.0)]), T0)
    assert e['status'] == 'won'  # horizon close, not an extension
    assert e['result_pct'] == pytest.approx(1.6)
    assert 'trail_count' not in e


def test_cycle_grade_never_raises_on_missing_data():
    e = _entry()
    intraday.proof_grade([e], {}, {}, T0 + timedelta(hours=1))
    assert e['status'] == 'open'


# ---------------------------------------------------------------- daily-row dedup

def test_record_cycle_daily_rows():
    rec = []
    cands = intraday.proof_cycle_candidates(_state(), ['ETH'],
                                            _mkt(100.0), {}, T0, 'neutral')
    assert intraday.proof_record_cycle(rec, cands, T0) == 12
    # Same state an hour later: all 12 deduped (24h rule).
    cands2 = intraday.proof_cycle_candidates(
        _state(), ['ETH'], _mkt(100.0), {}, T0 + timedelta(hours=1),
        'neutral')
    assert intraday.proof_record_cycle(rec, cands2,
                                       T0 + timedelta(hours=1)) == 0
    # Next day: all 12 record again — even the 7d rows whose previous
    # entries are still open (spec §4: one row per asset-day).
    cands3 = intraday.proof_cycle_candidates(
        _state(), ['ETH'], _mkt(100.0), {}, T0 + timedelta(days=1),
        'neutral')
    assert intraday.proof_record_cycle(rec, cands3,
                                       T0 + timedelta(days=1)) == 12
    assert len(rec) == 24


def test_record_cycle_variables_are_distinct():
    # Different variables for the same asset+horizon never dedup each
    # other (the variable joins the dedup key).
    rec = []
    cands = intraday.proof_cycle_candidates(_state(), ['ETH'],
                                            _mkt(100.0), {}, T0, 'neutral')
    halving = [c for c in cands if c['variable'] == 'halving']
    assert intraday.proof_record_cycle(rec, halving, T0) == 2
    month = [c for c in cands if c['variable'] == 'month']
    assert intraday.proof_record_cycle(rec, month, T0) == 2


def test_record_new_variable_aware_keeps_patterns_unchanged():
    # The variable-aware dedup in proof_record_new must not change
    # pattern behavior (pattern entries carry no variable).
    rec = []
    cands = intraday.proof_pattern_candidates(
        {'BTC': [{'pattern': 'Hammer', 'direction': 'LONG',
                  'timeframe': '1h', 'fired_ts': int(T0.timestamp()),
                  'strength': 1.0, 'confluence_id': None}]},
        _mkt(100.0, ('BTC',)), {}, T0, 'neutral')
    assert intraday.proof_record_new(rec, cands, T0) == 2
    cands2 = intraday.proof_pattern_candidates(
        {'BTC': [{'pattern': 'Hammer', 'direction': 'LONG',
                  'timeframe': '1h', 'fired_ts': int(T0.timestamp()),
                  'strength': 1.0, 'confluence_id': None}]},
        _mkt(100.0, ('BTC',)), {}, T0 + timedelta(hours=1), 'neutral')
    assert intraday.proof_record_new(rec, cands2,
                                     T0 + timedelta(hours=1)) == 0


# ---------------------------------------------------------------- scoreboard

def _closed(variable, band, direction, horizon, status, pnl,
            closed='2026-11-05T10:00:00+00:00', expired=False):
    e = {'kind': 'cycle', 'variable': variable, 'band': band,
         'direction': direction, 'horizon': horizon, 'status': status,
         'paper_pnl': pnl, 'ts_closed': closed,
         'result_pct': pnl / 10.0}
    if expired:
        e['cycle_expired'] = True
    return e


def test_cycle_scoreboard_math_and_baselines():
    rec = [_closed('fng', 'FNG_F', 'LONG', '24h', 'won', 28.0),
           _closed('fng', 'FNG_F', 'LONG', '24h', 'won', 28.0),
           _closed('fng', 'FNG_F', 'LONG', '24h', 'lost', -35.0),
           _closed('fng', 'FNG_N', 'LONG', '24h', 'lost', -35.0),
           _closed('fng', 'FNG_N', 'LONG', '24h', 'lost', -35.0),
           _closed('fng', 'FNG_N', 'LONG', '24h', 'won', 28.0),
           _closed('halving', 'H5', 'LONG', '7d', 'won', 28.0,
                   expired=True)]
    rows = intraday.cycle_scoreboard(rec)
    by_key = {(r['variable'], r['band'], r['horizon']): r for r in rows}
    fear = by_key[('fng', 'FNG_F', '24h')]
    assert fear['graded'] == 3 and fear['wins'] == 2
    assert fear['win_rate'] == pytest.approx(66.7, abs=0.1)
    assert fear['wilson_lower_95'] == pytest.approx(20.8, abs=0.5)
    assert fear['expectancy_per_1000'] == pytest.approx(7.0)
    assert fear['meets_sample_n100'] is False
    assert fear['meets_expectancy_20'] is False
    # All-days base rate for 24h: 3 wins / 6 graded (won+lost) = 50%.
    assert fear['base_rate'] == pytest.approx(50.0)
    # Control band FNG_N: 1 win / 3 = 33.3% — fear beats both.
    assert fear['control_rate'] == pytest.approx(33.3, abs=0.1)
    assert fear['beats_base'] is True
    assert fear['beats_control'] is True
    neutral = by_key[('fng', 'FNG_N', '24h')]
    assert neutral['beats_control'] is False  # control vs itself
    assert neutral['beats_base'] is False
    # Halving rows carry the cross-asset disclosure + slow-accumulator.
    h5 = by_key[('halving', 'H5', '7d')]
    assert h5['graded'] == 1 and h5['expired'] == 1
    assert 'Cross-asset evidence only' in h5['note']
    assert 'Slow accumulator' in h5['frequency_note']
    assert h5['control_rate'] is None  # halving has no control band
    assert h5['beats_control'] is None
    assert h5['max_drawdown_pct'] == pytest.approx(0.0)


def test_cycle_scoreboard_merges_cumulative():
    rec = [_closed('month', 'November', 'LONG', '24h', 'won', 28.0)]
    cum = {'month|November|LONG|24h': {'graded': 9, 'wins': 5, 'losses': 4,
                                       'expired': 0,
                                       'paper_pnl_total': 0.0}}
    rows = intraday.cycle_scoreboard(rec, cum)
    assert rows[0]['graded'] == 10
    assert rows[0]['wins'] == 6
    assert rows[0]['base_rate'] == pytest.approx(60.0)
    assert rows[0]['max_drawdown_pct'] is not None  # visible leg only


def test_roll_cycle_cumulative():
    dropped = [_closed('dom', 'DOM_DOWN', 'LONG', '24h', 'won', 28.0),
               _closed('dom', 'DOM_DOWN', 'LONG', '24h', 'lost', -35.0),
               {'kind': 'pattern', 'status': 'won', 'paper_pnl': 10.0}]
    out = intraday.proof_roll_cycle_cumulative({}, dropped)
    assert out == {'dom|DOM_DOWN|LONG|24h': {
        'graded': 2, 'wins': 1, 'losses': 1, 'expired': 0,
        'paper_pnl_total': -7.0}}


def test_proof_stats_cycle_scoreboard_placeholder():
    st = intraday.proof_stats([])
    assert st['cycle_scoreboard'] is None
    assert st['pattern_scoreboard'] is None


# ---------------------------------------------------------------- end-to-end chain

def test_cycle_chain_record_grade_scoreboard():
    # One day of cycle rows for two assets, graded a day later: a
    # slow +2.4% drift expires the 24h rows as wins (drift covers the
    # 1.4% costs, below the +4.2% win level); the 7d rows stay open.
    rec = []
    cands = intraday.proof_cycle_candidates(
        _state(), ['ETH', 'BTC'], _mkt(100.0), {}, T0, 'neutral')
    assert len(cands) == 22  # ETH 12 + BTC 10 (DOM_DOWN skips BTC)
    assert intraday.proof_record_cycle(rec, cands, T0) == 22
    eth_chart = {'ETH': [[int((T0 + timedelta(hours=h)).timestamp()
                              * 1000), 100.0 + h * 0.1]
                         for h in range(1, 25)]}
    btc_chart = {'BTC': [[int((T0 + timedelta(hours=h)).timestamp()
                              * 1000), 100.0 + h * 0.1]
                         for h in range(1, 25)]}
    charts = dict(eth_chart)
    charts.update(btc_chart)
    mkt = {'ETH': {'price': 102.4}, 'BTC': {'price': 102.4}}
    # 27h after entry: past exit_by (24h) + the 2h grace.
    intraday.proof_grade(rec, mkt, charts, T0 + timedelta(hours=27))
    graded_24h = [e for e in rec if e['horizon'] == '24h']
    open_7d = [e for e in rec if e['horizon'] == '7d']
    assert len(graded_24h) == 11 and all(
        e['status'] == 'won' for e in graded_24h)
    assert all(e.get('cycle_expired') for e in graded_24h)
    assert all(e['result_pct'] == pytest.approx(1.0)
               for e in graded_24h)
    assert len(open_7d) == 11 and all(
        e['status'] == 'open' for e in open_7d)
    rows = intraday.cycle_scoreboard(rec)
    assert rows  # 24h cells present with 100% win rate
    assert all(r['win_rate'] == 100.0 for r in rows)
    assert all(r['horizon'] == '24h' for r in rows)
