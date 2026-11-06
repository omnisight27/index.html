#!/usr/bin/env python3
"""VE calibration shadow (Nic 2026-11-05, approved).

Variant VE: per-direction tables keyed by RAW band (using conf_raw),
stronger pull (cap 0.8, shrinkage ramp n/(n+20)), hierarchical fallback
to the pooled band at half weight, and a regime-break reset for the
calibration tables when self-tune weights are promoted.

Run: python3 -m pytest Scripts/test_calib_v2.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nexintel


def _pick(asset='COMP', conf_raw=None, conf=78, win=True, excluded=False):
    p = {'asset': asset, 'confidence': conf, 'win': win}
    if conf_raw is not None:
        p['conf_raw'] = conf_raw
    if excluded:
        p['excluded'] = True
        p['exclusion_reason'] = 'test'
    return p


def _rows():
    # Directly-built VE table rows (direction, band, n, win_rate).
    return [
        {'direction': 'LONG', 'band': '60-69', 'n': 20, 'win_rate': 50.0},
        {'direction': 'LONG', 'band': '80+', 'n': 80, 'win_rate': 40.0},
        {'direction': 'SHORT', 'band': '60-69', 'n': 20, 'win_rate': 60.0},
        {'direction': None, 'band': '60-69', 'n': 40, 'win_rate': 55.0},
        {'direction': None, 'band': '80+', 'n': 90, 'win_rate': 45.0},
    ]


def test_v2_table_is_per_direction_raw_banded():
    # conf_raw and confidence deliberately sit in DIFFERENT bands:
    # F is raw 62 (60-69) but stated 55 (50-59); G is raw 58 (50-59)
    # but stated 66 (60-69). The VE table must follow conf_raw.
    runs = [{'date': '2026-10-24T15:05:34+00:00', 'scored': True,
             'longs': [_pick('F', conf_raw=62, conf=55, win=True),
                       _pick('A', conf_raw=75, conf=70, win=False)],
             'shorts': [_pick('G', conf_raw=58, conf=66, win=False),
                        _pick('D', conf_raw=85, conf=80, win=True)]}]
    cal = nexintel.calibration_from_picks_v2(runs)
    assert len(cal) == 12, cal  # 8 directional cells + 4 pooled rows
    by_key = {(c['direction'], c['band']): c for c in cal}
    assert by_key[('LONG', '60-69')]['n'] == 1, by_key
    assert by_key[('LONG', '60-69')]['win_rate'] == 100.0, by_key
    assert by_key[('LONG', '70-79')]['n'] == 1, by_key
    assert by_key[('SHORT', '50-59')]['n'] == 1, by_key
    assert by_key[('SHORT', '80+')]['n'] == 1, by_key
    assert by_key[('SHORT', '80+')]['win_rate'] == 100.0, by_key
    assert by_key[(None, '60-69')]['n'] == 1, by_key  # pooled: F only
    assert by_key[(None, '50-59')]['n'] == 1, by_key  # pooled: G only


def test_v2_conf_raw_falls_back_to_confidence():
    # Pre-Oct-21 picks lack conf_raw: band on stated confidence instead.
    runs = [{'date': '2026-10-20T15:05:34+00:00', 'scored': True,
             'longs': [_pick('OLD', conf_raw=None, conf=72, win=True)],
             'shorts': []}]
    cal = nexintel.calibration_from_picks_v2(runs)
    by_key = {(c['direction'], c['band']): c for c in cal}
    assert by_key[('LONG', '70-79')]['n'] == 1, by_key
    assert by_key[('LONG', '70-79')]['win_rate'] == 100.0, by_key


def test_v2_excluded_picks_ignored():
    runs = [{'date': '2026-10-24T15:05:34+00:00', 'scored': True,
             'longs': [_pick('REAL', conf_raw=65, conf=65, win=True),
                       _pick('FAKE', conf_raw=65, conf=65, win=True, excluded=True)],
             'shorts': []}]
    cal = nexintel.calibration_from_picks_v2(runs)
    by_key = {(c['direction'], c['band']): c for c in cal}
    assert by_key[('LONG', '60-69')]['n'] == 1, by_key


def test_v2_fallback_to_pooled_at_half_weight():
    # LONG 60-69 has no cell in this table -> pooled 60-69 (n=40,
    # win_rate 55) at half weight: w = 0.5 * min(0.8, 40/60) = 0.5*0.667.
    rows = [r for r in _rows()
            if not (r['direction'] == 'LONG' and r['band'] == '60-69')]
    val, label, n, emp = nexintel.calibrate_confidence_v2(65, 'LONG', rows)
    assert (label, n, emp) == ('60-69', 40, 55.0), (label, n, emp)
    # w = 0.5 * min(0.8, 40/60) = 1/3; cal = 65*(2/3) + 55*(1/3) = 61.67
    assert val == 62, val


def test_v2_cap_and_shrinkage_ramp():
    # n=80 cell: w = min(0.8, 80/100) = 0.8 -> 80*0.2 + 40*0.8 = 48.
    val, label, n, emp = nexintel.calibrate_confidence_v2(80, 'LONG', _rows())
    assert (val, label, n, emp) == (48, '80+', 80, 40.0), (val, label, n, emp)
    # n=10 cell: w = min(0.8, 10/30) = 1/3 -> 80*(2/3) + 40*(1/3) = 66.67.
    small = [{'direction': 'LONG', 'band': '80+', 'n': 10, 'win_rate': 40.0}]
    val, _, n, _ = nexintel.calibrate_confidence_v2(80, 'LONG', small)
    assert (val, n) == (67, 10), (val, n)


def test_v2_keeps_raw_when_no_cell():
    # Raw below every band (<50) has no label: raw unchanged.
    val, label, n, emp = nexintel.calibrate_confidence_v2(44, 'LONG', _rows())
    assert (val, label, n, emp) == (44, None, 0, None), (val, label, n, emp)
    # Thin cell AND thin pooled band: raw unchanged.
    thin = [{'direction': 'LONG', 'band': '60-69', 'n': 3, 'win_rate': 50.0},
            {'direction': None, 'band': '60-69', 'n': 5, 'win_rate': 50.0}]
    val, label, n, emp = nexintel.calibrate_confidence_v2(65, 'LONG', thin)
    assert val == 65, val


def test_v1_unchanged_by_v2():
    # The v1 function keeps its 0.5 cap and n/60 ramp.
    rows = [{'band': '70-79', 'n': 60, 'win_rate': 50.0}]
    val, label, n, emp = nexintel.calibrate_confidence(76, rows)
    # w = min(0.5, 60/60) = 0.5 -> 76*0.5 + 50*0.5 = 63.
    assert (val, label, n, emp) == (63, '70-79', 60, 50.0), (val, label, n, emp)


def test_reset_marks_break_and_filters_tables():
    hist = {'picks': [
        {'date': '2026-10-24T15:05:34+00:00', 'scored': True,
         'longs': [_pick('OLD', conf_raw=65, conf=65, win=True)], 'shorts': []},
    ], 'corrections': []}
    nexintel.reset_calibration_tables(hist, 'test promotion')
    assert hist.get('calibration_reset_at'), hist
    assert any(c.get('type') == 'calibration_regime_break'
               for c in hist['corrections']), hist['corrections']
    # Stored tables are cleared.
    assert all(c['n'] == 0 for c in hist['calibration']), hist['calibration']
    assert all(c['n'] == 0 for c in hist['calibration_v2']), hist['calibration_v2']
    # A run dated at the break counts; the earlier run does not.
    hist['picks'].append({
        'date': hist['calibration_reset_at'], 'scored': True,
        'longs': [_pick('NEW', conf_raw=65, conf=65, win=False)], 'shorts': []})
    v2 = nexintel.calibration_from_picks_v2(hist['picks'], hist['calibration_reset_at'])
    by_key = {(c['direction'], c['band']): c for c in v2}
    assert by_key[('LONG', '60-69')]['n'] == 1, by_key
    assert by_key[('LONG', '60-69')]['win_rate'] == 0.0, by_key
    v1 = nexintel.calibration_from_picks(hist['picks'], hist['calibration_reset_at'])
    band = {c['band']: c for c in v1}
    assert band['60-69']['n'] == 1, band


def test_v1_table_unchanged_without_reset():
    # reset_at=None (the default) keeps the old behavior exactly.
    runs = [{'date': '2026-10-24T15:05:34+00:00', 'scored': True,
             'longs': [_pick('A', conf_raw=65, conf=72, win=True)],
             'shorts': [_pick('B', conf_raw=65, conf=72, win=False)]}]
    cal = nexintel.calibration_from_picks(runs)
    band = {c['band']: c for c in cal}
    # v1 buckets by STATED confidence (72 -> 70-79), both picks.
    assert band['70-79']['n'] == 2, band
    assert band['70-79']['win_rate'] == 50.0, band
