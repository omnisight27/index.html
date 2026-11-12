#!/usr/bin/env python3
"""Tests for the live corrected weights (F(t)) in nexintel.py.

The correction loop writes repo/data/corrected_weights.json each
grading run; the live scoring path reads it INSTEAD of the
least-squares fit. Fail-open: a missing or invalid file changes
nothing — scoring falls back to learn_weights().
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nexintel

FIFTY = [0.01, -0.02, 0.005, 0.0, 0.0, 0.01, 0.009, 0.0, 0.0, -0.01,
         0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.044, 0.0]


def _write_weights(tmp_path, weights=FIFTY, t=1):
    p = tmp_path / 'corrected_weights.json'
    p.write_text(json.dumps({
        't': t, 'timestamp': '2026-11-11T00:00:00+00:00',
        'weights': weights, 'source': 'test',
        'proof': {'fifty_rule_exp_usd': -3.58,
                  'current_exp_usd': 12.27}}))
    return str(p)


def test_load_valid_file(tmp_path):
    path = _write_weights(tmp_path)
    w, info = nexintel.load_corrected_weights(path)
    assert w == FIFTY and info['t'] == 1
    assert info['proof']['fifty_rule_exp_usd'] == -3.58


def test_load_missing_file_falls_back(tmp_path):
    w, reason = nexintel.load_corrected_weights(
        str(tmp_path / 'nope.json'))
    assert w is None and 'no corrected weights' in reason


def test_load_wrong_length_rejected(tmp_path):
    p = tmp_path / 'bad.json'
    p.write_text(json.dumps({'t': 1, 'weights': [0.1] * 10}))
    w, reason = nexintel.load_corrected_weights(str(p))
    assert w is None and 'invalid' in reason


def test_load_non_finite_rejected(tmp_path):
    bad = list(FIFTY)
    bad[0] = float('inf')
    p = tmp_path / 'bad2.json'
    p.write_text(json.dumps({'t': 1, 'weights': bad}))
    w, _ = nexintel.load_corrected_weights(str(p))
    assert w is None


def test_load_malformed_json_rejected(tmp_path):
    p = tmp_path / 'bad3.json'
    p.write_text('{not json')
    w, _ = nexintel.load_corrected_weights(str(p))
    assert w is None


def test_f_terms_sum_to_tilted_score():
    feats = [1.0] * 22
    w = [0.1] * 22
    # dot = 2.2, geo x 10 = 0.5 -> raw pre-tilt 2.7; tilted 3.0 -> a = 0.3
    terms = nexintel.f_terms_for(feats, w, 0.05, 2.7, 3.0)
    total = terms['a'] + terms['b'] + terms['c'] + terms['d']
    assert abs(total - 3.0) < 0.01
    assert terms['fe'] is None and terms['unknown'] is None
    # tilted > 0: why is the largest positive contributor
    assert terms['why'] == max(('a', 'b', 'c', 'd'),
                               key=lambda k: terms[k])


def test_f_terms_why_is_largest_negative_for_shorts():
    feats = [0.0] * 22
    feats[6] = -10.0  # acceleration deeply negative -> d carries the short
    w = [0.0] * 22
    w[6] = 0.5
    terms = nexintel.f_terms_for(feats, w, 0.0, -5.0, -5.0)
    assert terms['d'] == -5.0
    assert terms['why'] == 'd'


def _coin(cid, sym, chg24, chg7d):
    return cid, {'symbol': sym, 'name': sym, 'usd': 1.0,
                 'usd_24h_change': chg24, 'usd_7d_change': chg7d,
                 'usd_1h_change': 2.0, 'usd_14d_change': 25.0,
                 'usd_30d_change': 30.0, 'market_cap': 200_000_000}


def _think_data():
    coins = [_coin('aaa', 'AAA', 12.0, 20.0),
             _coin('bbb', 'BBB', 11.0, 18.0),
             _coin('ccc', 'CCC', -8.0, -12.0)]
    return {
        'fear_greed': [{'value': 50}],
        'crypto': dict(coins),
        'listings': {'coinbase': {'AAA', 'BBB', 'CCC'},
                     'cryptocom': set()},
        'metals': {}, 'forex': {}, 'stocks': {}, 'contracts': [],
        'headlines': [], 'newsletter_digest': {}, 'macro': {},
    }


def test_think_uses_corrected_weights_and_stamps_f(tmp_path, monkeypatch):
    path = _write_weights(tmp_path)
    monkeypatch.setattr(nexintel, 'CORRECTED_WEIGHTS_FILE', path)
    signals = nexintel.think(_think_data(),
                             {'stats': {}, 'picks': [], 'calibration': []})
    assert signals['weights_source'] == 'corrected'
    assert signals['weights'] == [round(x, 4) for x in FIFTY]
    assert 'corrected 50%-rule weights' in signals['weight_info']['note']
    assert signals['longs'], 'expected longs under corrected weights'
    for p in signals['longs']:
        assert p['f_verdict'] == 'BUY'
        assert p['f_why'] in ('a', 'b', 'c', 'd')
        terms = p['f_terms']
        assert set(terms) == {'a', 'b', 'c', 'd', 'fe', 'unknown'}
        assert terms['fe'] is None and terms['unknown'] is None
        total = terms['a'] + terms['b'] + terms['c'] + terms['d']
        assert abs(total - p['score']) < 0.2  # rounding on both sides
    for p in signals['shorts']:
        assert p['f_verdict'] == 'NO'


def test_think_without_corrected_file_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(nexintel, 'CORRECTED_WEIGHTS_FILE',
                        str(tmp_path / 'missing.json'))
    signals = nexintel.think(_think_data(),
                             {'stats': {}, 'picks': [], 'calibration': []})
    assert signals['weights_source'] == 'learned'
    assert signals['weights'] != [round(x, 4) for x in FIFTY]
    for p in signals['longs'] + signals['shorts']:
        assert 'f_verdict' not in p  # no F verdict without corrected weights


def test_think_with_invalid_corrected_file_falls_back(tmp_path, monkeypatch):
    p = tmp_path / 'bad.json'
    p.write_text(json.dumps({'t': 1, 'weights': [0.1] * 5}))
    monkeypatch.setattr(nexintel, 'CORRECTED_WEIGHTS_FILE', str(p))
    signals = nexintel.think(_think_data(),
                             {'stats': {}, 'picks': [], 'calibration': []})
    assert signals['weights_source'] == 'learned'


def test_no_order_placement_in_nexintel():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'nexintel.py')).read()
    for banned in ('place_order', 'submit_order', 'create_order',
                   'stripe', 'plaid'):
        assert banned not in src
    assert 'NEVER places an order' in src
