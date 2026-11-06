"""Manipulation research track, Phase 2 — shadow grading tests.

Covers the spec §4 grading math (+4.2% gross / -2.1% adverse, EXPIRED
rule, $28 / -$35 on $1,000 paper), the three kinds (cmi_regime 24h/7d
with MANIP graded as both LONG and SHORT-bias cells; cmi_confluence
6h/24h with the CMI > 0.50 exit trigger; cmi_trap 6h/24h in the flip
direction), the daily-row dedup (no open-block for 7d rows), and the
CMI scoreboard (Wilson lower bound, expectancy, drawdown, cmi_exits,
frequency flag).

Run: python3 -m pytest Scripts/test_cmi_grading.py
"""
import sys
import os
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intraday

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def _cmi_map(**over):
    row = {'cmi': 0.18, 'band': 'CLEAN', 'sine_score': 0.9,
           'reg_score': 0.9, 'supertrend': 'bullish',
           'supertrend_value': 95.0, 'supertrend_flipped': False,
           'confluence': None, 'trap': False}
    row.update(over)
    return {'BTC': row, 'ETH': dict(row)}


def _mkt(price, assets=('BTC', 'ETH')):
    return {a: {'price': price, 'high_24h': price * 1.05,
                'low_24h': price * 0.95} for a in assets}


def _chart(points, asset='BTC'):
    """points: [(hours_after_T0, price)] -> {asset: [[ts_ms, price]]}."""
    return {asset: [[int((T0 + timedelta(hours=h)).timestamp() * 1000), p]
                    for h, p in points]}


def _cmi_series(points, asset='BTC'):
    """points: [(hours_after_T0, cmi)] -> {asset: [[ts_ms, cmi]]}."""
    return {asset: [[int((T0 + timedelta(hours=h)).timestamp() * 1000), v]
                    for h, v in points]}


# ---------------------------------------------------------------- candidates

def test_regime_candidate_shapes_clean():
    cands = intraday.proof_cmi_regime_candidates(
        _cmi_map(), ['BTC'], _mkt(100.0), {}, T0, 'neutral')
    assert len(cands) == 2  # LONG x (24h, 7d)
    assert {c['kind'] for c in cands} == {'cmi_regime'}
    assert {c['horizon'] for c in cands} == {'24h', '7d'}
    assert {c['direction'] for c in cands} == {'LONG'}
    assert all(c['band'] == 'CLEAN' for c in cands)
    assert cands[0]['entry'] == 100.0
    assert cands[0]['take_profit'] == pytest.approx(104.2)
    assert cands[0]['stop_loss'] == pytest.approx(97.9)
    assert cands[0]['expected_return_pct'] == 2.8
    assert cands[0]['max_loss_pct'] == 3.5


def test_regime_candidate_manip_grades_both_directions():
    m = _cmi_map(band='MANIP', cmi=0.82)
    cands = intraday.proof_cmi_regime_candidates(
        m, ['BTC'], _mkt(100.0), {}, T0, 'neutral')
    # MANIP: LONG cell + SHORT-bias cell, each x (24h, 7d).
    assert len(cands) == 4
    assert {(c['direction'], c['horizon']) for c in cands} == \
        {('LONG', '24h'), ('LONG', '7d'),
         ('SHORT', '24h'), ('SHORT', '7d')}
    assert len({c['id'] for c in cands}) == 4  # ids stay distinct
    short = next(c for c in cands if c['direction'] == 'SHORT'
                 and c['horizon'] == '24h')
    assert short['take_profit'] == pytest.approx(95.8)
    assert short['stop_loss'] == pytest.approx(102.1)
    assert all(c['band'] == 'MANIP' for c in cands)


def test_regime_candidate_caution_long_only():
    m = _cmi_map(band='CAUTION', cmi=0.45)
    cands = intraday.proof_cmi_regime_candidates(
        m, ['BTC'], _mkt(100.0), {}, T0, 'neutral')
    assert len(cands) == 2
    assert {c['direction'] for c in cands} == {'LONG'}


def test_regime_candidate_skips_missing_bands():
    m = {'BTC': {'cmi': None, 'band': None}, 'ETH': {'band': 'CLEAN'}}
    cands = intraday.proof_cmi_regime_candidates(
        m, ['BTC', 'ETH'], _mkt(100.0), {}, T0, 'neutral')
    assert len(cands) == 2  # ETH only
    assert all(c['asset'] == 'ETH' for c in cands)
    assert intraday.proof_cmi_regime_candidates(
        {}, ['BTC'], _mkt(100.0), {}, T0, 'neutral') == []
    assert intraday.proof_cmi_regime_candidates(
        _cmi_map(), ['BTC'], {}, {}, T0, 'neutral') == []


def test_confluence_candidate_shapes():
    m = _cmi_map(confluence='LONG', supertrend_flipped=True)
    cands = intraday.proof_cmi_confluence_candidates(
        m, _mkt(100.0), {}, T0, 'neutral')
    # Both assets flip LONG in the fixture map: 2 assets x 2 horizons.
    assert len(cands) == 4
    assert {c['kind'] for c in cands} == {'cmi_confluence'}
    assert {c['horizon'] for c in cands} == {'6h', '24h'}
    assert {c['direction'] for c in cands} == {'LONG'}
    assert all(c['band'] == 'CLEAN' for c in cands)


def test_confluence_candidate_short_and_skips_non_flips():
    m = _cmi_map()
    m['BTC'] = dict(m['BTC'], confluence='SHORT',
                    supertrend='bearish', supertrend_flipped=True)
    cands = intraday.proof_cmi_confluence_candidates(
        m, _mkt(100.0), {}, T0, 'neutral')
    assert len(cands) == 2  # BTC only (ETH has no flip)
    assert {c['direction'] for c in cands} == {'SHORT'}
    assert all(c['asset'] == 'BTC' for c in cands)
    assert intraday.proof_cmi_confluence_candidates(
        _cmi_map(), _mkt(100.0), {}, T0, 'neutral') == []


def test_trap_candidate_shapes():
    m = _cmi_map()
    m['BTC'] = dict(m['BTC'], band='CAUTION', cmi=0.45, trap=True,
                    supertrend='bearish', supertrend_flipped=True)
    cands = intraday.proof_cmi_trap_candidates(
        m, _mkt(100.0), {}, T0, 'neutral')
    assert len(cands) == 2  # BTC only, direction = flip direction
    assert {c['kind'] for c in cands} == {'cmi_trap'}
    assert {c['direction'] for c in cands} == {'SHORT'}
    assert {c['horizon'] for c in cands} == {'6h', '24h'}
    assert intraday.proof_cmi_trap_candidates(
        _cmi_map(), _mkt(100.0), {}, T0, 'neutral') == []


# ---------------------------------------------------------------- grading: regime / trap (fixed levels)

def _regime_entry(horizon='24h', opened_hours_ago=0, asset='BTC',
                  direction='LONG', band='CLEAN'):
    now_open = T0 - timedelta(hours=opened_hours_ago)
    m = _cmi_map(band=band, cmi=0.18 if band == 'CLEAN' else 0.82)
    cands = intraday.proof_cmi_regime_candidates(
        m, [asset], _mkt(100.0, (asset,)), {}, now_open, 'neutral')
    return next(c for c in cands
                if c['horizon'] == horizon
                and c['direction'] == direction)


def test_regime_win_on_target_touch():
    e = _regime_entry()
    intraday.proof_grade([e], _mkt(100.0), _chart([(1, 104.5)]),
                         T0 + timedelta(hours=1))
    assert e['status'] == 'won'
    assert e['result_pct'] == 2.8
    assert e['paper_pnl'] == 28.0


def test_regime_loss_when_adverse_hits_first():
    e = _regime_entry()
    intraday.proof_grade([e], _mkt(105.0),
                         _chart([(1, 97.8), (2, 105.0)]),
                         T0 + timedelta(hours=2))
    assert e['status'] == 'lost'
    assert e['result_pct'] == -3.5
    assert e['paper_pnl'] == -35.0


def test_regime_manip_short_mirror():
    e = _regime_entry(direction='SHORT', band='MANIP')
    intraday.proof_grade([e], _mkt(95.0), _chart([(1, 95.5)]),
                         T0 + timedelta(hours=1))
    assert e['status'] == 'won' and e['paper_pnl'] == 28.0


def test_regime_24h_stays_open_before_horizon():
    e = _regime_entry()
    intraday.proof_grade([e], _mkt(101.0), _chart([(1, 101.0)]),
                         T0 + timedelta(hours=1))
    assert e['status'] == 'open'


def test_regime_7d_expired_win_when_drift_covers_costs():
    e = _regime_entry(horizon='7d', opened_hours_ago=240)
    intraday.proof_grade([e], _mkt(101.8),
                         _chart([(239, 101.0), (240, 101.8)]), T0)
    assert e['status'] == 'won'
    assert e['cmi_expired'] is True
    assert e['result_pct'] == pytest.approx(0.4)
    assert e['paper_pnl'] == pytest.approx(4.0)


def test_regime_7d_not_cut_short_by_7d_cap():
    # Opened 7 days + 1h ago: exit_by (7d) passed but the +2h grace
    # has not — the row must stay open (the pattern track's 7-day
    # cap would have closed it here).
    e = _regime_entry(horizon='7d', opened_hours_ago=169)
    intraday.proof_grade([e], _mkt(101.0), _chart([(169, 101.0)]), T0)
    assert e['status'] == 'open'


def test_regime_green_at_exit_is_not_trailed():
    e = _regime_entry(horizon='7d', opened_hours_ago=240)
    intraday.proof_grade([e], _mkt(103.0), _chart([(240, 103.0)]), T0)
    assert e['status'] == 'won'  # horizon close, not an extension
    assert e['result_pct'] == pytest.approx(1.6)
    assert 'trail_count' not in e


def test_trap_grades_on_fixed_levels():
    m = _cmi_map()
    m['BTC'] = dict(m['BTC'], band='CAUTION', cmi=0.45, trap=True,
                    supertrend='bearish', supertrend_flipped=True)
    cands = intraday.proof_cmi_trap_candidates(
        m, _mkt(100.0, ('BTC',)), {}, T0, 'neutral')
    e = next(c for c in cands if c['horizon'] == '6h')
    intraday.proof_grade([e], _mkt(95.0), _chart([(1, 95.5)]),
                         T0 + timedelta(hours=1))
    assert e['status'] == 'won' and e['paper_pnl'] == 28.0


def test_cmi_grade_never_raises_on_missing_data():
    e = _regime_entry()
    intraday.proof_grade([e], {}, {}, T0 + timedelta(hours=1))
    assert e['status'] == 'open'


# ---------------------------------------------------------------- grading: confluence exit trigger

def _confluence_entry(horizon='6h', direction='LONG'):
    m = _cmi_map(confluence=direction, supertrend_flipped=True)
    cands = intraday.proof_cmi_confluence_candidates(
        m, _mkt(100.0, ('BTC',)), {}, T0, 'neutral')
    return next(c for c in cands if c['horizon'] == horizon)


def test_confluence_trigger_closes_at_trigger_candle():
    # +1% drift, CMI crosses 0.50 at hour 2: closes at 101.0, drift
    # does not cover the 1.4% costs -> lost, cmi_exit set.
    e = _confluence_entry()
    series = _cmi_series([(1, 0.40), (2, 0.55), (3, 0.60)])
    intraday.proof_grade([e], _mkt(101.0),
                         _chart([(1, 100.5), (2, 101.0), (3, 101.0)]),
                         T0 + timedelta(hours=3), cmi_by_sym=series)
    assert e['status'] == 'lost'
    assert e['cmi_exit'] is True
    assert e['cmi_expired'] is True
    assert e['result_pct'] == pytest.approx(-0.4)
    assert e['paper_pnl'] == pytest.approx(-4.0)


def test_confluence_trigger_win_when_drift_covers_costs():
    e = _confluence_entry()
    series = _cmi_series([(1, 0.40), (2, 0.55)])
    intraday.proof_grade([e], _mkt(102.0),
                         _chart([(1, 101.0), (2, 102.0)]),
                         T0 + timedelta(hours=2), cmi_by_sym=series)
    assert e['status'] == 'won'
    assert e['cmi_exit'] is True
    assert e['result_pct'] == pytest.approx(0.6)
    assert e['paper_pnl'] == pytest.approx(6.0)


def test_confluence_target_before_trigger_wins():
    # Price touches +4.2% at hour 1, CMI crosses only at hour 2:
    # the level decides first — a target actually touched is a win.
    e = _confluence_entry()
    series = _cmi_series([(1, 0.40), (2, 0.55)])
    intraday.proof_grade([e], _mkt(105.0),
                         _chart([(1, 104.5), (2, 105.0)]),
                         T0 + timedelta(hours=2), cmi_by_sym=series)
    assert e['status'] == 'won'
    assert e['result_pct'] == 2.8
    assert e.get('cmi_exit') is not True


def test_confluence_no_trigger_below_threshold():
    # CMI stays under 0.50: no trigger, plain fixed-level grading —
    # still open before the horizon.
    e = _confluence_entry()
    series = _cmi_series([(1, 0.40), (2, 0.49)])
    intraday.proof_grade([e], _mkt(101.0),
                         _chart([(1, 100.5), (2, 101.0)]),
                         T0 + timedelta(hours=2), cmi_by_sym=series)
    assert e['status'] == 'open'
    assert e.get('cmi_exit') is None


def test_confluence_missing_series_degrades_to_fixed_levels():
    # No CMI series at all: the trigger simply never fires, and the
    # row still grades on the fixed levels (fail-open).
    e = _confluence_entry()
    intraday.proof_grade([e], _mkt(100.0), _chart([(1, 104.5)]),
                         T0 + timedelta(hours=1))
    assert e['status'] == 'won' and e['result_pct'] == 2.8


def test_confluence_short_trigger_mirror():
    e = _confluence_entry(direction='SHORT')
    series = _cmi_series([(1, 0.40), (2, 0.55)])
    intraday.proof_grade([e], _mkt(98.0),
                         _chart([(1, 99.0), (2, 98.0)]),
                         T0 + timedelta(hours=2), cmi_by_sym=series)
    assert e['status'] == 'won'
    assert e['cmi_exit'] is True
    assert e['result_pct'] == pytest.approx(0.6)


# ---------------------------------------------------------------- daily-row dedup

def test_record_cmi_daily_rows():
    rec = []
    cands = intraday.proof_cmi_regime_candidates(
        _cmi_map(), ['BTC'], _mkt(100.0), {}, T0, 'neutral')
    assert intraday.proof_record_cmi(rec, cands, T0) == 2
    # Same state an hour later: deduped (24h rule).
    cands2 = intraday.proof_cmi_regime_candidates(
        _cmi_map(), ['BTC'], _mkt(100.0), {}, T0 + timedelta(hours=1),
        'neutral')
    assert intraday.proof_record_cmi(rec, cands2,
                                     T0 + timedelta(hours=1)) == 0
    # Next day: records again — even the 7d rows whose previous
    # entries are still open (one row per asset-day).
    cands3 = intraday.proof_cmi_regime_candidates(
        _cmi_map(), ['BTC'], _mkt(100.0), {}, T0 + timedelta(days=1),
        'neutral')
    assert intraday.proof_record_cmi(rec, cands3,
                                     T0 + timedelta(days=1)) == 2
    assert len(rec) == 4


def test_record_cmi_manip_cells_are_distinct():
    # MANIP LONG and SHORT rows for the same asset+horizon never
    # dedup each other (the cell joins the dedup key).
    rec = []
    m = _cmi_map(band='MANIP', cmi=0.82)
    cands = intraday.proof_cmi_regime_candidates(
        m, ['BTC'], _mkt(100.0), {}, T0, 'neutral')
    assert intraday.proof_record_cmi(rec, cands, T0) == 4
    assert intraday.proof_record_cmi(
        rec, intraday.proof_cmi_regime_candidates(
            m, ['BTC'], _mkt(100.0), {}, T0 + timedelta(hours=1),
            'neutral'),
        T0 + timedelta(hours=1)) == 0


# ---------------------------------------------------------------- scoreboard

def _closed(kind, band, direction, horizon, status, pnl,
            closed='2026-11-05T10:00:00+00:00', expired=False,
            cmi_exit=False):
    e = {'kind': kind, 'band': band, 'direction': direction,
         'horizon': horizon, 'status': status, 'paper_pnl': pnl,
         'ts_closed': closed, 'result_pct': pnl / 10.0}
    if expired:
        e['cmi_expired'] = True
    if cmi_exit:
        e['cmi_exit'] = True
    return e


def test_cmi_scoreboard_math():
    rec = [_closed('cmi_regime', 'CLEAN', 'LONG', '24h', 'won', 28.0),
           _closed('cmi_regime', 'CLEAN', 'LONG', '24h', 'won', 28.0),
           _closed('cmi_regime', 'CLEAN', 'LONG', '24h', 'lost', -35.0),
           _closed('cmi_confluence', 'CLEAN', 'LONG', '6h', 'won', 6.0,
                   expired=True, cmi_exit=True),
           _closed('cmi_trap', 'CAUTION', 'SHORT', '6h', 'lost', -35.0)]
    rows = intraday.cmi_scoreboard(rec)
    by_key = {(r['kind'], r['band'], r['direction'], r['horizon']): r
              for r in rows}
    regime = by_key[('cmi_regime', 'CLEAN', 'LONG', '24h')]
    assert regime['graded'] == 3 and regime['wins'] == 2
    assert regime['win_rate'] == pytest.approx(66.7, abs=0.1)
    assert regime['wilson_lower_95'] == pytest.approx(20.8, abs=0.5)
    assert regime['expectancy_per_1000'] == pytest.approx(7.0)
    assert regime['cmi_exits'] == 0
    assert regime['meets_sample_n100'] is False
    assert regime['meets_expectancy_20'] is False
    assert regime['insufficient_frequency'] is None  # too young
    conf = by_key[('cmi_confluence', 'CLEAN', 'LONG', '6h')]
    assert conf['graded'] == 1 and conf['cmi_exits'] == 1
    assert conf['expired'] == 1
    assert conf['expectancy_per_1000'] == pytest.approx(6.0)
    trap = by_key[('cmi_trap', 'CAUTION', 'SHORT', '6h')]
    assert trap['graded'] == 1 and trap['wins'] == 0


def test_cmi_scoreboard_merges_cumulative():
    rec = [_closed('cmi_regime', 'CLEAN', 'LONG', '24h', 'won', 28.0)]
    cum = {'cmi_regime|CLEAN|LONG|24h': {
        'graded': 9, 'wins': 5, 'losses': 4, 'expired': 0,
        'cmi_exits': 2, 'paper_pnl_total': 0.0}}
    rows = intraday.cmi_scoreboard(rec, cum)
    assert rows[0]['graded'] == 10
    assert rows[0]['wins'] == 6
    assert rows[0]['cmi_exits'] == 2
    assert rows[0]['max_drawdown_pct'] is not None  # visible leg only


def test_cmi_scoreboard_frequency_flag():
    # A cell spanning 21 days with 2 graded rows: 0.67/week -> the
    # insufficient-frequency flag fires (spec §4 frequency floor).
    old = '2026-10-15T10:00:00+00:00'
    new = '2026-11-05T10:00:00+00:00'
    rec = [_closed('cmi_regime', 'MANIP', 'SHORT', '7d', 'won', 28.0,
                   closed=old),
           _closed('cmi_regime', 'MANIP', 'SHORT', '7d', 'lost', -35.0,
                   closed=new)]
    # ts_open is what the frequency clock reads — set it explicitly.
    rec[0]['ts_open'] = old
    rec[1]['ts_open'] = new
    rows = intraday.cmi_scoreboard(rec)
    assert len(rows) == 1
    assert rows[0]['occurrences_per_week'] == pytest.approx(0.67, abs=0.05)
    assert rows[0]['insufficient_frequency'] is True


def test_roll_cmi_cumulative():
    dropped = [_closed('cmi_regime', 'CLEAN', 'LONG', '24h', 'won', 28.0),
               _closed('cmi_confluence', 'CLEAN', 'LONG', '6h', 'lost',
                       -35.0, expired=True, cmi_exit=True),
               {'kind': 'cycle', 'status': 'won', 'paper_pnl': 10.0}]
    out = intraday.proof_roll_cmi_cumulative({}, dropped)
    assert out == {
        'cmi_regime|CLEAN|LONG|24h': {
            'graded': 1, 'wins': 1, 'losses': 0, 'expired': 0,
            'cmi_exits': 0, 'paper_pnl_total': 28.0},
        'cmi_confluence|CLEAN|LONG|6h': {
            'graded': 1, 'wins': 0, 'losses': 1, 'expired': 1,
            'cmi_exits': 1, 'paper_pnl_total': -35.0}}


def test_proof_stats_cmi_scoreboard_placeholder():
    st = intraday.proof_stats([])
    assert st['cmi_scoreboard'] is None
    assert st['cycle_scoreboard'] is None
    assert st['pattern_scoreboard'] is None


# ---------------------------------------------------------------- end-to-end chain

def test_cmi_chain_record_grade_scoreboard():
    # One day of CMI rows for BTC (CLEAN) + ETH (MANIP), graded a
    # day later: a slow +2.4% drift expires the 24h rows as wins
    # (drift covers the 1.4% costs, below the +4.2% win level); the
    # 7d rows stay open.
    rec = []
    m = _cmi_map()
    m['ETH'] = dict(m['ETH'], band='MANIP', cmi=0.82)
    cands = intraday.proof_cmi_regime_candidates(
        m, ['BTC', 'ETH'], _mkt(100.0), {}, T0, 'neutral')
    assert len(cands) == 6  # BTC 2 + ETH 4 (MANIP both directions)
    assert intraday.proof_record_cmi(rec, cands, T0) == 6
    charts = {}
    for asset in ('BTC', 'ETH'):
        charts[asset] = [[int((T0 + timedelta(hours=h)).timestamp()
                              * 1000), 100.0 + h * 0.1]
                         for h in range(1, 25)]
    mkt = {'BTC': {'price': 102.4}, 'ETH': {'price': 102.4}}
    # 27h after entry: past exit_by (24h) + the 2h grace.
    intraday.proof_grade(rec, mkt, charts, T0 + timedelta(hours=27))
    graded_24h = [e for e in rec if e['horizon'] == '24h']
    open_7d = [e for e in rec if e['horizon'] == '7d'
               and e['status'] == 'open']
    assert len(graded_24h) == 3
    # LONG rows win on the +2.4% drift; the MANIP SHORT row loses.
    longs = [e for e in graded_24h if e['direction'] == 'LONG']
    shorts = [e for e in graded_24h if e['direction'] == 'SHORT']
    assert len(longs) == 2 and all(e['status'] == 'won' for e in longs)
    assert all(e.get('cmi_expired') for e in longs)
    assert all(e['result_pct'] == pytest.approx(1.0) for e in longs)
    assert len(shorts) == 1 and shorts[0]['status'] == 'lost'
    # The SHORT 7d row already stopped out at hour 21 (price +2.4%
    # vs its -2.1% adverse level); only the LONG 7d rows stay open.
    assert len(open_7d) == 2 and all(
        e['status'] == 'open' for e in open_7d)
    assert all(e['direction'] == 'LONG' for e in open_7d)
    short_7d = [e for e in rec if e['horizon'] == '7d'
                and e['direction'] == 'SHORT']
    assert len(short_7d) == 1 and short_7d[0]['status'] == 'lost'
    rows = intraday.cmi_scoreboard(rec)
    assert rows
    by_key = {(r['kind'], r['band'], r['direction'], r['horizon']): r
              for r in rows}
    # The closed SHORT 7d row is in the scoreboard too.
    assert by_key[('cmi_regime', 'MANIP', 'SHORT', '7d')]['graded'] == 1
    assert by_key[('cmi_regime', 'MANIP', 'SHORT', '7d')]['win_rate'] == 0.0
    assert by_key[('cmi_regime', 'CLEAN', 'LONG', '24h')]['win_rate'] == 100.0
