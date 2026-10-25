#!/usr/bin/env python3
"""Cleanup of the 9 fake CVX wins (Nic approved 2026-10-24).

The pre-fix symbol collision let Chevron's price score crypto CVX longs
at +8,793% to +9,699% — nine fake wins baked into the graded record.
The fix has two halves:

1. Data: the 9 picks are marked excluded in history.json (original
   values kept for audit) and the cumulative stats / calibration /
   error blocks are recomputed from the non-excluded picks only.
2. Code: every place that consumes history picks skips excluded ones —
   scoring, calibration, weight training, geometry, Kelly sizing — so
   the next engine run cannot re-add them.

Run: python3 -m pytest Scripts/test_record_cleanup.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nexintel


def _pick(asset='COMP', price=24.35, ret=5.0, conf=78, win=True,
          cls='crypto', excluded=False):
    p = {'asset': asset, 'class': cls, 'price': price,
         'return_pct': ret, 'confidence': conf, 'win': win,
         'features': [1.0, 0.5, 0.0, 0.2, 0.1, 0.3, 0.4,
                      0.2, 0.1, 0.0, 0.2, 0.1, 0.3]}
    if excluded:
        p['excluded'] = True
        p['exclusion_reason'] = 'test'
    return p


def _history_with_fake():
    # One scored run: 3 real wins, 3 real losses, 1 fake CVX win at
    # +9,617% marked excluded.
    return {'picks': [{
        'date': '2026-10-24T15:05:34+00:00', 'scored': True,
        'longs': [
            _pick('COMP', 24.35, 5.0, 78, True),
            _pick('SOL', 100.0, 4.0, 76, True),
            _pick('ETH', 2400.0, 3.0, 74, True),
            _pick('CVX', 2.40, 9617.08, 78, True, excluded=True),
        ],
        'shorts': [
            _pick('XRP', 1.30, -3.0, 72, False),
            _pick('DOGE', 0.07, -2.0, 71, False),
            _pick('ADA', 0.40, -4.0, 70, False),
        ],
    }], 'stats': {'wins': 3, 'losses': 3, 'total': 6, 'win_rate': 50.0}}


def test_excluded_pick_ignored_by_calibration():
    h = _history_with_fake()
    cal = nexintel.calibration_from_picks(h['picks'])
    band = {c['band']: c for c in cal}
    # 70-79 band: 3 real wins + 3 real losses only; the fake CVX win
    # (conf 78) must not count.
    assert band['70-79']['n'] == 6, band
    assert band['70-79']['win_rate'] == 50.0, band


def test_excluded_pick_ignored_by_learn_weights():
    h = _history_with_fake()
    _, info = nexintel.learn_weights(h)
    # 7 scored picks, 1 excluded -> 6 training samples.
    assert info['samples'] == 6, info


def test_excluded_pick_ignored_by_geometric_edge():
    h = _history_with_fake()
    _, info = nexintel.geometric_edge([1.0] * 13, h)
    # Win centroid: 3 real wins; loss centroid: 3 real losses.
    assert info == '3W/3L centroids', info


def test_excluded_pick_ignored_by_avg_win_loss():
    h = _history_with_fake()
    avg_w, avg_l = nexintel._avg_win_loss(h)
    assert avg_w == 4.0, avg_w  # (5+4+3)/3 — not the fake +9,617
    assert avg_l == 3.0, avg_l  # (3+2+4)/3


def test_excluded_pick_not_rescored(tmp_path, monkeypatch):
    # An unscored run holding an excluded pick: remember() must leave it
    # untouched and never count it in stats.
    hist = {'picks': [{
        'date': '2026-10-24T12:00:00+00:00', 'scored': False,
        'fear_greed': 50, 'regime': 'NEUTRAL',
        'longs': [
            {'asset': 'CVX', 'class': 'crypto', 'market': 'crypto',
             'price': 2.40, 'score': 5.0, 'confidence': 78,
             'excluded': True,
             'exclusion_reason': 'fake win — scored vs Chevron price'},
            {'asset': 'COMP', 'class': 'crypto', 'market': 'crypto',
             'price': 24.35, 'score': 5.0, 'confidence': 78},
        ],
        'shorts': [],
    }], 'stats': {'wins': 0, 'losses': 0, 'total': 0},
       'error': {'sum_abs_err': 0.0, 'n': 0, 'within_5pct': 0}}
    (tmp_path / 'history.json').write_text(json.dumps(hist))
    monkeypatch.setattr(nexintel, 'DATA_DIR', str(tmp_path))
    data = {'crypto': {'convex-finance': {'usd': 2.50, 'symbol': 'CVX'},
                       'compound': {'usd': 25.00, 'symbol': 'COMP'}},
            'stocks': {}}
    signals = {'longs': [], 'shorts': [], 'psychology': {'fear_greed': 50}}
    out = nexintel.remember(data, signals)
    run = out['picks'][0]
    cvx = [p for p in run['longs'] if p['asset'] == 'CVX'][0]
    assert 'return_pct' not in cvx and 'win' not in cvx
    assert out['stats'] == {'wins': 1, 'losses': 0, 'total': 1,
                            'win_rate': 100.0}, out['stats']
    # Calibration must not include the excluded pick either.
    cal = {c['band']: c for c in out['calibration']}
    assert cal['70-79']['n'] == 1, cal
