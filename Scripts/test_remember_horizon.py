"""Tests for the grading horizon in remember() (2026-11-05): a swing pick
is graded only once it reaches the horizon its thesis promised
(projected_exit = entry + 3 days), not on the next loop run ~6h after
entry. Grading at 6h measured a 6-hour move against a 3-day target, so
MAE was dominated by the horizon gap (daily MAE 8-13.7, median signed
error -6 to -8) rather than model skill. Runs stay open until every
non-excluded pick in them has been graded.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nexintel


@pytest.fixture(autouse=True)
def _restore_data_dir():
    orig = nexintel.DATA_DIR
    yield
    nexintel.DATA_DIR = orig


def _pick(asset, price, exp_ret, projected_exit=None, direction='LONG'):
    thesis = {'direction': direction,
              'expected_return_pct': exp_ret,
              'stop_loss': price * 0.97,
              'take_profit': price * 1.04}
    if projected_exit is not None:
        thesis['projected_exit'] = projected_exit
    return {'asset': asset, 'price': price, 'market': 'crypto',
            'score': 17.0, 'confidence': 65, 'thesis': thesis}


def _seed(tmp_path, runs, error=None, stats=None):
    hist = {'picks': runs,
            'stats': stats or {'wins': 0, 'losses': 0, 'total': 0},
            'error': error if error is not None else
                     {'sum_abs_err': 0.0, 'n': 0, 'within_5pct': 0,
                      'metric': 'return_pct_v1'}}
    (tmp_path / 'history.json').write_text(json.dumps(hist))
    nexintel.DATA_DIR = str(tmp_path)
    return hist


def _run(data, signals):
    return nexintel.remember(data, signals)


def _data(prices):
    return {'crypto': {sym.lower(): {'symbol': sym, 'usd': px}
                       for sym, px in prices.items()},
            'stocks': {}}


def _signals():
    return {'longs': [], 'shorts': [],
            'psychology': {'fear_greed': 50}, 'regime': 'NEUTRAL'}


def test_pick_before_projected_exit_stays_unscored(tmp_path):
    # BTC bought at 100, now 102 — but the thesis projects exit in the
    # far future, so nothing is graded yet: no return, no win/loss, no
    # error counted, and the run stays open.
    run = {'date': '2026-11-05T00:00:00+00:00', 'scored': False,
           'longs': [_pick('BTC', 100.0, 4.0, '2999-01-01')], 'shorts': []}
    _seed(tmp_path, [run])
    hist = _run(_data({'BTC': 102.0}), _signals())
    p = hist['picks'][0]['longs'][0]
    assert 'return_pct' not in p and 'abs_error' not in p
    assert hist['picks'][0]['scored'] is False
    assert hist['stats']['total'] == 0
    assert hist['error']['n'] == 0


def test_pick_at_projected_exit_scores(tmp_path):
    # Projected exit reached (past date): graded exactly as before.
    run = {'date': '2026-11-02T00:00:00+00:00', 'scored': False,
           'longs': [_pick('BTC', 100.0, 4.0, '2000-01-01')], 'shorts': []}
    _seed(tmp_path, [run])
    hist = _run(_data({'BTC': 102.0}), _signals())
    p = hist['picks'][0]['longs'][0]
    assert p['return_pct'] == 2.0 and p['win'] is True
    assert p['abs_error'] == 2.0
    assert hist['picks'][0]['scored'] is True
    assert hist['error']['n'] == 1


def test_mixed_run_scores_matured_only_and_does_not_double_count(tmp_path):
    # One pick past its horizon, one still inside it: the matured pick
    # grades, the run stays open, and a later run grades the second pick
    # without re-scoring (and re-counting) the first.
    run = {'date': '2026-11-02T00:00:00+00:00', 'scored': False,
           'longs': [_pick('BTC', 100.0, 4.0, '2000-01-01'),
                     _pick('ETH', 100.0, 4.0, '2999-01-01')], 'shorts': []}
    _seed(tmp_path, [run])
    hist = _run(_data({'BTC': 102.0, 'ETH': 103.0}), _signals())
    assert hist['picks'][0]['longs'][0]['return_pct'] == 2.0
    assert 'return_pct' not in hist['picks'][0]['longs'][1]
    assert hist['picks'][0]['scored'] is False
    assert hist['stats']['total'] == 1 and hist['error']['n'] == 1
    # Second pass: ETH's horizon is now (artificially) reached — rewrite
    # its projected exit to the past and re-run. BTC must not double-count.
    hist['picks'][0]['longs'][1]['thesis']['projected_exit'] = '2000-01-01'
    (tmp_path / 'history.json').write_text(json.dumps(hist))
    hist2 = _run(_data({'BTC': 102.0, 'ETH': 103.0}), _signals())
    assert hist2['picks'][0]['longs'][1]['return_pct'] == 3.0
    assert hist2['picks'][0]['scored'] is True
    assert hist2['stats']['total'] == 2, hist2['stats']
    assert hist2['error']['n'] == 2


def test_pick_without_projected_exit_scores_immediately(tmp_path):
    # Legacy fallback: pre-thesis-era picks carry no projected_exit and
    # keep the old behavior — graded on the next run.
    pick = _pick('XRP', 100.0, 4.0)  # no projected_exit key at all
    run = {'date': '2026-10-26T00:00:00+00:00', 'scored': False,
           'longs': [pick], 'shorts': []}
    _seed(tmp_path, [run])
    hist = _run(_data({'XRP': 110.0}), _signals())
    assert hist['picks'][0]['longs'][0]['return_pct'] == 10.0
    assert hist['picks'][0]['scored'] is True


def test_unparseable_projected_exit_scores_immediately(tmp_path):
    pick = _pick('SOL', 100.0, 4.0, 'not-a-date')
    run = {'date': '2026-10-26T00:00:00+00:00', 'scored': False,
           'longs': [pick], 'shorts': []}
    _seed(tmp_path, [run])
    hist = _run(_data({'SOL': 96.0}), _signals())
    assert hist['picks'][0]['longs'][0]['return_pct'] == -4.0
    assert hist['picks'][0]['scored'] is True
