"""Price-action pattern track, Phase 2 — shadow grading tests.

Covers the spec §4 grading math (+4.2% gross / -2.1% adverse, EXPIRED
rule, $28 / -$35 on $1,000 paper), the 6h/24h candidate shapes, the
confluence-only-cluster rule, horizon-aware dedup, and the pattern
scoreboard (Wilson lower bound, expectancy, drawdown).

Run: python3 -m pytest Scripts/test_pattern_grading.py
"""
import sys
import os
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intraday

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def _tags(*names_directions):
    return [{'pattern': n, 'direction': d, 'timeframe': '1h',
             'fired_ts': int(T0.timestamp()), 'strength': 1.0,
             'confluence_id': None} for n, d in names_directions]


def _mkt(price):
    return {'BTC': {'price': price, 'high_24h': price * 1.05,
                    'low_24h': price * 0.95}}


def _chart(points):
    """points: [(hours_after_T0, price)] -> [[ts_ms, price], ...]."""
    return {'BTC': [[int((T0 + timedelta(hours=h)).timestamp() * 1000), p]
                    for h, p in points]}


# ---------------------------------------------------------------- candidates

def test_pattern_candidate_plan_levels():
    cands = intraday.proof_pattern_candidates(
        {'BTC': _tags(('Hammer', 'LONG'))}, _mkt(100.0), {'BTC': 'Bitcoin'},
        T0, 'neutral')
    assert len(cands) == 2  # 6h + 24h
    by_h = {c['horizon']: c for c in cands}
    assert set(by_h) == {'6h', '24h'}
    for c in cands:
        assert c['kind'] == 'pattern'
        assert c['pattern'] == 'Hammer'
        assert c['patterns'] == ['Hammer']
        assert c['direction'] == 'LONG'
        assert c['entry'] == 100.0
        assert c['take_profit'] == pytest.approx(104.2)
        assert c['stop_loss'] == pytest.approx(97.9)
        assert c['expected_return_pct'] == 2.8   # +$28 on $1,000 paper
        assert c['max_loss_pct'] == 3.5          # -$35 on $1,000 paper
        assert c['band'] == 'na'
    assert by_h['6h']['id'] != by_h['24h']['id']
    assert by_h['6h']['exit_by'] < by_h['24h']['exit_by']


def test_pattern_candidate_short_levels():
    cands = intraday.proof_pattern_candidates(
        {'BTC': _tags(('Shooting Star', 'SHORT'))}, _mkt(100.0), {}, T0,
        'risk_on')
    assert len(cands) == 2
    assert cands[0]['take_profit'] == pytest.approx(95.8)
    assert cands[0]['stop_loss'] == pytest.approx(102.1)


def test_confluence_grades_as_cluster_only():
    cands = intraday.proof_pattern_candidates(
        {'BTC': _tags(('Hammer', 'LONG'), ('BOS', 'LONG'))},
        _mkt(100.0), {}, T0, 'neutral')
    # Two horizons of ONE cluster candidate — no single-pattern rows.
    assert len(cands) == 2
    assert {c['kind'] for c in cands} == {'pattern_confluence'}
    assert cands[0]['patterns'] == ['BOS', 'Hammer']
    assert cands[0]['pattern'] == 'BOS+Hammer'


def test_confluence_groups_by_direction():
    # One LONG and one SHORT firing: each grades on its own.
    cands = intraday.proof_pattern_candidates(
        {'BTC': _tags(('Hammer', 'LONG'), ('Shooting Star', 'SHORT'))},
        _mkt(100.0), {}, T0, 'neutral')
    assert len(cands) == 4  # 2 directions x 2 horizons
    assert {c['kind'] for c in cands} == {'pattern'}


def test_pattern_candidate_skipped_without_price():
    assert intraday.proof_pattern_candidates(
        {'BTC': _tags(('Hammer', 'LONG'))}, {}, {}, T0, 'neutral') == []
    assert intraday.proof_pattern_candidates({}, _mkt(100.0), {}, T0,
                                            'neutral') == []


# ---------------------------------------------------------------- grading

def _entry(horizon_h=6, opened_hours_ago=0, direction='LONG'):
    now_open = T0 - timedelta(hours=opened_hours_ago)
    cands = intraday.proof_pattern_candidates(
        {'BTC': _tags(('Hammer', direction))}, _mkt(100.0), {}, now_open,
        'neutral')
    return next(c for c in cands if c['horizon'] == f'{horizon_h}h')


def test_pattern_win_on_target_touch():
    e = _entry()
    intraday.proof_grade([e], _mkt(100.0), _chart([(1, 104.5)]),
                         T0 + timedelta(hours=1))
    assert e['status'] == 'won'
    assert e['result_pct'] == 2.8
    assert e['paper_pnl'] == 28.0


def test_pattern_loss_when_adverse_hits_first():
    e = _entry()
    # Dips -2.2% first, then runs to +5%: first touch decides — a loss.
    intraday.proof_grade([e], _mkt(105.0),
                         _chart([(1, 97.8), (2, 105.0)]),
                         T0 + timedelta(hours=2))
    assert e['status'] == 'lost'
    assert e['result_pct'] == -3.5
    assert e['paper_pnl'] == -35.0


def test_pattern_short_mirror():
    e = _entry(direction='SHORT')
    intraday.proof_grade([e], _mkt(95.0), _chart([(1, 95.5)]),
                         T0 + timedelta(hours=1))
    assert e['status'] == 'won' and e['paper_pnl'] == 28.0
    e2 = _entry(direction='SHORT')
    intraday.proof_grade([e2], _mkt(103.0), _chart([(1, 102.5)]),
                         T0 + timedelta(hours=1))
    assert e2['status'] == 'lost' and e2['paper_pnl'] == -35.0


def test_pattern_stays_open_before_horizon():
    e = _entry()
    intraday.proof_grade([e], _mkt(101.0), _chart([(1, 101.0)]),
                         T0 + timedelta(hours=1))
    assert e['status'] == 'open'


def test_pattern_expired_win_when_drift_covers_costs():
    # 6h horizon, opened 10h ago: window over (exit_by + 2h grace).
    e = _entry(opened_hours_ago=10)
    intraday.proof_grade([e], _mkt(101.8), _chart([(9, 101.0), (10, 101.8)]),
                         T0)
    assert e['status'] == 'won'
    assert e['pattern_expired'] is True
    assert e['result_pct'] == pytest.approx(0.4)   # 1.8% drift - 1.4% costs
    assert e['paper_pnl'] == pytest.approx(4.0)


def test_pattern_expired_loss_when_drift_does_not_cover_costs():
    e = _entry(opened_hours_ago=10)
    intraday.proof_grade([e], _mkt(100.5), _chart([(10, 100.5)]), T0)
    assert e['status'] == 'lost'
    assert e['pattern_expired'] is True
    assert e['result_pct'] == pytest.approx(-0.9)
    assert e['paper_pnl'] == pytest.approx(-9.0)


def test_pattern_expired_boundary():
    # Exactly +1.4% drift: costs covered — a win at $0.00.
    e = _entry(opened_hours_ago=10)
    intraday.proof_grade([e], _mkt(101.4), _chart([(10, 101.4)]), T0)
    assert e['status'] == 'won' and e['result_pct'] == pytest.approx(0.0)


def test_pattern_green_at_exit_is_not_trailed():
    # +3% at the horizon close: below the +4.2% win level, but the
    # main grader's ride-the-win trail must NOT extend a pattern entry.
    e = _entry(opened_hours_ago=10)
    intraday.proof_grade([e], _mkt(103.0), _chart([(10, 103.0)]), T0)
    assert e['status'] == 'won'  # horizon close, not an extension
    assert e['result_pct'] == pytest.approx(1.6)
    assert 'trail_count' not in e


def test_pattern_grade_never_raises_on_missing_data():
    e = _entry()
    intraday.proof_grade([e], {}, {}, T0 + timedelta(hours=1))
    assert e['status'] == 'open'


# ---------------------------------------------------------------- dedup

def test_record_new_keeps_both_horizons():
    rec = []
    cands = intraday.proof_pattern_candidates(
        {'BTC': _tags(('Hammer', 'LONG'))}, _mkt(100.0), {}, T0, 'neutral')
    assert intraday.proof_record_new(rec, cands, T0) == 2
    # Same firing again an hour later: both horizons deduped (24h rule).
    cands2 = intraday.proof_pattern_candidates(
        {'BTC': _tags(('Hammer', 'LONG'))}, _mkt(100.0), {}, T0 + timedelta(hours=1),
        'neutral')
    assert intraday.proof_record_new(rec, cands2, T0 + timedelta(hours=1)) == 0


# ---------------------------------------------------------------- scoreboard

def test_wilson_lower_known_values():
    assert intraday.wilson_lower(50, 100) == pytest.approx(0.4038, abs=0.005)
    assert intraday.wilson_lower(0, 10) == 0.0
    assert intraday.wilson_lower(10, 10) == pytest.approx(0.722, abs=0.01)
    assert intraday.wilson_lower(0, 0) is None


def _closed(pattern, direction, horizon, status, pnl, closed_h='2026-11-05T10:00:00+00:00'):
    return {'kind': 'pattern' if '+' not in pattern else 'pattern_confluence',
            'pattern': pattern, 'patterns': pattern.split('+'),
            'direction': direction, 'horizon': horizon, 'status': status,
            'paper_pnl': pnl, 'ts_closed': closed_h, 'result_pct': pnl / 10.0}


def test_pattern_scoreboard_math():
    rec = [_closed('Hammer', 'LONG', '6h', 'won', 28.0),
           _closed('Hammer', 'LONG', '6h', 'won', 28.0),
           _closed('Hammer', 'LONG', '6h', 'lost', -35.0),
           _closed('Hammer', 'LONG', '24h', 'lost', -35.0),
           _closed('BOS+Hammer', 'LONG', '6h', 'won', 28.0)]
    rows = intraday.pattern_scoreboard(rec)
    by_key = {(r['pattern'], r['horizon']): r for r in rows}
    h6 = by_key[('Hammer', '6h')]
    assert h6['graded'] == 3 and h6['wins'] == 2 and h6['losses'] == 1
    assert h6['win_rate'] == pytest.approx(66.7, abs=0.1)
    assert h6['wilson_lower_95'] == pytest.approx(20.8, abs=0.5)
    assert h6['expectancy_per_1000'] == pytest.approx(7.0)
    assert h6['meets_sample_n100'] is False
    assert h6['meets_expectancy_20'] is False
    assert h6['max_drawdown_pct'] == pytest.approx(3.5, abs=0.1)
    assert by_key[('Hammer', '24h')]['graded'] == 1
    assert by_key[('BOS+Hammer', '6h')]['graded'] == 1


def test_pattern_scoreboard_merges_cumulative():
    rec = [_closed('Hammer', 'LONG', '6h', 'won', 28.0)]
    cum = {'Hammer|LONG|6h': {'graded': 9, 'wins': 5, 'losses': 4,
                              'expired': 0, 'paper_pnl_total': 0.0}}
    rows = intraday.pattern_scoreboard(rec, cum)
    assert rows[0]['graded'] == 10
    assert rows[0]['wins'] == 6
    assert rows[0]['max_drawdown_pct'] is not None  # visible leg only


def test_roll_pattern_cumulative():
    dropped = [_closed('Hammer', 'LONG', '6h', 'won', 28.0),
               _closed('Hammer', 'LONG', '6h', 'lost', -35.0),
               {'kind': 'signal', 'status': 'won', 'paper_pnl': 10.0}]
    out = intraday.proof_roll_pattern_cumulative({}, dropped)
    assert out == {'Hammer|LONG|6h': {'graded': 2, 'wins': 1, 'losses': 1,
                                      'expired': 0, 'paper_pnl_total': -7.0}}


def test_proof_stats_by_kind_includes_patterns():
    rec = [_closed('Hammer', 'LONG', '6h', 'won', 28.0)]
    st = intraday.proof_stats(rec)
    assert st['by_kind']['pattern']['graded'] == 1
    assert st['by_kind']['pattern']['paper_pnl_total'] == 28.0
    assert st['by_kind']['pattern_confluence']['graded'] == 0
