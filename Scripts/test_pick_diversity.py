"""Tests for the correlation-aware top-pick selection (Quantum Lab lesson,
2026-10-25): greedy-by-score over-buys assets that move together, so the
top 3 of each side are chosen with a co-movement haircut on the de-meaned
5-window return vector."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nexintel


def _pick(asset, score, vec=None):
    p = {'asset': asset, 'score': score, 'reason': f'{asset} test pick'}
    if vec is not None:
        p['tf_vector'] = vec
    return p


def test_diverse_top_prefers_uncorrelated():
    # A and B are twins (identical return profiles); C moves differently.
    picks = [_pick('A', 25.0, [1, 2, 3, 4, 5]),
             _pick('B', 24.0, [1, 2, 3, 4, 5]),
             _pick('C', 23.0, [5, -4, 3, -2, 1]),
             _pick('D', 20.0, [-3, 1, 0, 2, -1])]
    out = nexintel.select_diverse_top(picks)
    assert [p['asset'] for p in out[:3]] == ['A', 'C', 'B'], out
    assert out[2]['div_twin'] == 'A'
    assert out[2]['div_dock'] == 2.4


def test_no_vector_no_penalty():
    # Stocks carry no return vector — score order is untouched.
    picks = [_pick('X', 25.0), _pick('Y', 24.0), _pick('Z', 23.0),
             _pick('W', 20.0)]
    out = nexintel.select_diverse_top(picks)
    assert [p['asset'] for p in out] == ['X', 'Y', 'Z', 'W']
    assert all('div_dock' not in p for p in out)


def test_short_side():
    # Negative scores: strength is |score|, the same haircut applies.
    picks = [_pick('A', -30.0, [1, 2, 3, 4, 5]),
             _pick('B', -29.0, [1, 2, 3, 4, 5]),
             _pick('C', -28.0, [5, -4, 3, -2, 1]),
             _pick('D', -20.0, [-3, 1, 0, 2, -1])]
    out = nexintel.select_diverse_top(picks)
    assert [p['asset'] for p in out[:3]] == ['A', 'C', 'B'], out


def test_shared_market_move_not_docked():
    # All three ride the same +10 market move with tiny idiosyncratic
    # parts. De-meaning must strip the shared move — no docks, score
    # order stands. Without de-meaning every pair would read ~1.0.
    picks = [_pick('A', 25.0, [11, 10, 10, 10, 10]),
             _pick('B', 24.0, [10, 11, 10, 10, 10]),
             _pick('C', 23.0, [10, 10, 11, 10, 10]),
             _pick('D', 20.0, [10, 10, 10, 11, 10])]
    out = nexintel.select_diverse_top(picks)
    assert [p['asset'] for p in out[:3]] == ['A', 'B', 'C'], out
    assert all('div_dock' not in p for p in out)


def test_returns_full_list_rest_in_score_order():
    picks = [_pick('A', 25.0, [1, 2, 3, 4, 5]),
             _pick('B', 24.0, [1, 2, 3, 4, 5]),
             _pick('C', 23.0, [5, -4, 3, -2, 1]),
             _pick('D', 20.0, [-3, 1, 0, 2, -1]),
             _pick('E', 18.0, [0, 0, 1, 0, 0])]
    out = nexintel.select_diverse_top(picks)
    assert len(out) == 5
    assert [p['asset'] for p in out[3:]] == ['D', 'E']


def _coin(cid, sym, chg24, chg7d, vec):
    return cid, {'symbol': sym, 'name': sym, 'usd': 10.0,
                 'usd_24h_change': chg24, 'usd_7d_change': chg7d,
                 'usd_1h_change': vec[0], 'usd_14d_change': vec[3],
                 'usd_30d_change': vec[4], 'market_cap': 200_000_000}


def test_think_wires_diversity_and_tf_vector():
    # End-to-end through think() on synthetic data: the twin pair must not
    # occupy both top slots when a genuinely different pick is available,
    # and every crypto pick must carry its 5-window vector.
    data = {
        'fear_greed': [{'value': 50}],
        'crypto': dict([
            _coin('aaa', 'AAA', 12.0, 20.0, [2, 12, 20, 25, 30]),
            _coin('bbb', 'BBB', 11.8, 19.8, [2, 11.8, 19.8, 24, 29]),
            _coin('ccc', 'CCC', 11.0, 18.0, [-1, 11, 18, 6, 3]),
            _coin('eee', 'EEE', 2.0, 3.0, [0, 2, 3, 1, 0]),
            _coin('ddd', 'DDD', -8.0, -12.0, [-2, -8, -12, -15, -18]),
        ]),
        'listings': {'coinbase': {'AAA', 'BBB', 'CCC', 'DDD'},
                     'cryptocom': set()},
        'metals': {}, 'forex': {}, 'stocks': {}, 'contracts': [],
        'headlines': [], 'newsletter_digest': {}, 'macro': {},
    }
    history = {'stats': {}, 'picks': [], 'calibration': []}
    signals = nexintel.think(data, history)
    longs = signals['longs']
    assert all(len(p['tf_vector']) == 5 for p in longs), longs
    top3 = [p['asset'] for p in longs[:3]]
    # AAA and BBB are near-twins; the diversified top 3 must include CCC
    # ahead of whichever twin was docked.
    assert 'CCC' in top3, top3
    assert not (top3[0] in ('AAA', 'BBB') and top3[1] in ('AAA', 'BBB')), top3


if __name__ == '__main__':
    for name, fn in sorted(list(globals().items())):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'{name}: PASS')
    print('all pick diversity tests pass')
