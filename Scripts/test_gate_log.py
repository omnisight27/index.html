#!/usr/bin/env python3
"""Tests for Scripts/gate_log.py — the gate log (F(t) verdicts)."""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate_log as gl  # noqa: E402


def _rec(t=1, verdict=False, why='a', outcome=None, asset='BTC'):
    return gl.new_record(
        t=t, run='2026-11-11T18:49:52Z', asset=asset, kind='accum',
        verdict=verdict, why=why,
        terms={'a': 0.38, 'b': 11.30, 'c': -0.42, 'd': 1.0,
               'fe': None, 'unknown': None},
        outcome=outcome)


def test_record_shape_has_fixed_fields():
    r = _rec()
    for field in ('t', 'run', 'timestamp', 'candidate_id', 'asset', 'kind',
                  'verdict', 'why', 'terms', 'outcome', 'outcome_time',
                  'correction_applied'):
        assert field in r, f'missing field {field}'
    assert r['verdict'] is False
    assert r['why'] == 'a'
    assert r['outcome'] is None  # unknown until graded, never assumed
    assert set(r['terms']) == {'a', 'b', 'c', 'd', 'fe', 'unknown'}
    assert r['t'] == 1


def test_verdict_is_binary():
    for bad in ('maybe', 1, None, 'TRUE'):
        try:
            gl.new_record(1, 'r', 'BTC', 'accum', bad, 'a', {})
        except ValueError:
            continue
        raise AssertionError(f'verdict {bad!r} should have been rejected')


def test_why_must_be_a_term():
    try:
        gl.new_record(1, 'r', 'BTC', 'accum', True, 'push-through', {})
    except ValueError:
        return
    raise AssertionError('why must be a term, not the push-through itself')


def test_record_and_read_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'gate-log.jsonl')
        gl.record(path, _rec(asset='BTC'))
        gl.record(path, _rec(asset='ETH', verdict=True, why='d'))
        rows = gl.read(path)
        assert len(rows) == 2
        assert rows[0]['asset'] == 'BTC' and rows[0]['verdict'] is False
        assert rows[1]['asset'] == 'ETH' and rows[1]['verdict'] is True
        # append-only: a second read sees the same two lines, in order
        assert [r['candidate_id'] for r in gl.read(path)] == [
            r['candidate_id'] for r in rows]


def test_read_missing_file_is_empty():
    assert gl.read('/tmp/does-not-exist-gate-log.jsonl') == []


def _graded(verdict, why, wins, losses):
    recs = []
    for i in range(wins):
        recs.append(_rec(verdict=verdict, why=why, outcome='win',
                         asset=f'W{i}'))
    for i in range(losses):
        recs.append(_rec(verdict=verdict, why=why, outcome='loss',
                         asset=f'L{i}'))
    return recs


def test_adjust_passed_won_strengthens():
    recs = _graded(True, 'd', 15, 5)
    adjs = gl.aggregate_adjustments(recs)
    assert len(adjs) == 1
    a = adjs[0]
    assert a['direction'] == 'strengthen' and a['multiplier_delta'] == 0.05
    assert a['term'] == 'd' and a['n'] == 20


def test_adjust_passed_lost_weakens():
    recs = _graded(True, 'c', 5, 15)
    a = gl.aggregate_adjustments(recs)[0]
    assert a['direction'] == 'weaken' and a['multiplier_delta'] == -0.05


def test_adjust_blocked_would_have_won_loosens():
    recs = _graded(False, 'a', 16, 4)
    a = gl.aggregate_adjustments(recs)[0]
    assert a['direction'] == 'weaken' and a['multiplier_delta'] == -0.05
    assert a['applies_to'] == 'caution-threshold'


def test_adjust_blocked_would_have_lost_confirms_no_change():
    recs = _graded(False, 'a', 4, 16)
    a = gl.aggregate_adjustments(recs)[0]
    assert a['direction'] == 'confirm' and a['multiplier_delta'] == 0.0


def test_adjust_thin_group_moves_nothing():
    recs = _graded(True, 'd', 12, 3)  # n=15 < 20
    a = gl.aggregate_adjustments(recs)[0]
    assert a['direction'] == 'none' and a['multiplier_delta'] == 0.0


def test_adjust_ungraded_records_are_ignored():
    recs = _graded(True, 'd', 15, 5) + [_rec(outcome=None, asset='U1')]
    a = gl.aggregate_adjustments(recs)[0]
    assert a['n'] == 20  # the null-outcome record never counts


def test_apply_strengthens_term_multiplier_and_clamps():
    mult, caution, applied = gl.apply_adjustments(
        {'c': 1.48, 'd': 1.0}, 50.0,
        [{'term': 'c', 'direction': 'strengthen', 'n': 20, 'wins': 15,
          'losses': 5, 'multiplier_delta': 0.05,
          'reason': 'x', 'applies_to': 'term-multiplier'}])
    assert mult['c'] == 1.5  # clamped at TERM_MAX
    assert applied[0]['applied'] is True


def test_apply_loosens_caution_threshold():
    mult, caution, applied = gl.apply_adjustments(
        {'c': 1.0, 'd': 1.0}, 50.0,
        [{'term': 'a', 'direction': 'weaken', 'n': 20, 'wins': 16,
          'losses': 4, 'multiplier_delta': -0.05,
          'reason': 'x', 'applies_to': 'caution-threshold'}])
    assert caution == 47.5
    assert applied[0]['applied'] is True


def test_apply_record_only_terms_change_nothing():
    mult, caution, applied = gl.apply_adjustments(
        {'c': 1.0, 'd': 1.0}, 50.0,
        [{'term': 'b', 'direction': 'strengthen', 'n': 20, 'wins': 15,
          'losses': 5, 'multiplier_delta': 0.05,
          'reason': 'x', 'applies_to': 'record-only'}])
    assert mult == {'c': 1.0, 'd': 1.0} and caution == 50.0
    assert applied[0]['applied'] is False
