"""Tests for the model-error metric (2026-10-26 redefinition): the error is
|thesis expected_return_pct - realized return_pct|, both in percent — not
the old unitless-ensemble-score vs return comparison, which printed MAE
223% and a drifting-down within-5% rate (13.4%) that looked like model
decay but was a units mismatch. Also covers the one-time counter reset
(the migration) that keeps the published series honest.
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


def _pick(asset, price, exp_ret, direction='LONG'):
    return {'asset': asset, 'price': price, 'market': 'crypto',
            'score': 17.0, 'confidence': 65,
            'thesis': {'direction': direction,
                       'expected_return_pct': exp_ret,
                       'stop_loss': price * 0.97,
                       'take_profit': price * 1.04}}


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


def test_error_is_expected_vs_realized_return(tmp_path):
    # Long BTC bought at 100, now 102 (+2%) vs a +4% thesis expectation:
    # error 2.0 — within the 5% target.
    run = {'date': '2026-10-26T00:00:00+00:00', 'scored': False,
           'longs': [_pick('BTC', 100.0, 4.0)], 'shorts': []}
    _seed(tmp_path, [run])
    hist = _run(_data({'BTC': 102.0}), _signals())
    p = hist['picks'][0]['longs'][0]
    assert p['return_pct'] == 2.0 and p['win'] is True
    assert p['abs_error'] == 2.0, p
    assert hist['error']['n'] == 1
    assert hist['error']['within_5pct'] == 1
    assert hist['error']['mae'] == 2.0


def test_short_uses_direction_adjusted_return(tmp_path):
    # Short ETH at 100, now 97 (price fell 3%): direction-adjusted return
    # is +3% vs a +4% expectation -> error 1.0.
    run = {'date': '2026-10-26T00:00:00+00:00', 'scored': False,
           'longs': [], 'shorts': [_pick('ETH', 100.0, 4.0, 'SHORT')]}
    _seed(tmp_path, [run])
    hist = _run(_data({'ETH': 97.0}), _signals())
    p = hist['picks'][0]['shorts'][0]
    assert p['return_pct'] == 3.0 and p['win'] is True
    assert p['abs_error'] == 1.0, p


def test_wrong_direction_is_penalized_not_hidden(tmp_path):
    # Long bought at 100, now 96 (-4%) vs a +4% expectation: the old
    # magnitude-vs-magnitude metric would have scored this error 0.0
    # (|4| - |-4|); the signed comparison scores it 8.0.
    run = {'date': '2026-10-26T00:00:00+00:00', 'scored': False,
           'longs': [_pick('SOL', 100.0, 4.0)], 'shorts': []}
    _seed(tmp_path, [run])
    hist = _run(_data({'SOL': 96.0}), _signals())
    p = hist['picks'][0]['longs'][0]
    assert p['return_pct'] == -4.0 and p['win'] is False
    assert p['abs_error'] == 8.0, p
    assert hist['error']['within_5pct'] == 0


def test_pick_without_thesis_expectation_skips_error_not_scoring(tmp_path):
    pick = _pick('XRP', 100.0, 4.0)
    pick['thesis'] = {}
    run = {'date': '2026-10-26T00:00:00+00:00', 'scored': False,
           'longs': [pick], 'shorts': []}
    _seed(tmp_path, [run])
    hist = _run(_data({'XRP': 110.0}), _signals())
    p = hist['picks'][0]['longs'][0]
    assert p['return_pct'] == 10.0 and p['win'] is True
    assert 'abs_error' not in p
    assert hist['error']['n'] == 0
    assert hist['stats']['total'] == 1


def test_migration_resets_old_counters_once(tmp_path):
    # Old-style counters (no metric marker, inflated by the score-vs-return
    # mismatch) are reset exactly once and the reset is recorded.
    old_error = {'sum_abs_err': 124749.79, 'n': 559, 'within_5pct': 75,
                 'mae': 223.17, 'within_5pct_rate': 13.4}
    run = {'date': '2026-10-26T00:00:00+00:00', 'scored': False,
           'longs': [_pick('BTC', 100.0, 4.0)], 'shorts': []}
    _seed(tmp_path, [run], error=old_error, stats={'wins': 0, 'losses': 0, 'total': 0})
    hist = _run(_data({'BTC': 102.0}), _signals())
    err = hist['error']
    assert err['metric'] == 'return_pct_v1'
    assert err['n'] == 1 and err['within_5pct'] == 1
    assert err['sum_abs_err'] == 2.0 and err['mae'] == 2.0
    corrections = [c for c in hist.get('corrections', [])
                   if 'redefined model-error metric' in c.get('action', '')]
    assert len(corrections) == 1
    assert corrections[0]['before']['error']['sum_abs_err'] == 124749.79
    # A second run must NOT reset again — counters accumulate.
    hist['picks'].append({'date': '2026-10-26T01:00:00+00:00', 'scored': False,
                          'longs': [_pick('ETH', 100.0, 4.0)], 'shorts': []})
    (tmp_path / 'history.json').write_text(json.dumps(hist))
    hist2 = _run(_data({'BTC': 102.0, 'ETH': 103.0}), _signals())
    assert hist2['error']['n'] == 2, hist2['error']
    assert hist2['error']['sum_abs_err'] == 3.0
    assert len([c for c in hist2.get('corrections', [])
                if 'redefined model-error metric' in c.get('action', '')]) == 1
