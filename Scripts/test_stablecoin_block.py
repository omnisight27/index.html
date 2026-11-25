"""Tests for the stablecoin block (2026-10-23 USDT incident, extended
2026-10-25): pegged and money-market tokens are cash, not trades. Five of
them leaked into the 2026-10-25 top longs (RLUSD, U, EURSAFO, EUTBL,
THBILL) because the blocklist only named the 15 classic stablecoins; hours
later a second wave (EURCV, OUSD, USD3) leaked the same way, which is why
the name block is now backed by a behavioral flat-coin filter."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nexintel

PEGGED = ['RLUSD', 'U', 'EURSAFO', 'EUTBL', 'THBILL', 'USDT', 'USDC',
          'EURCV', 'USDCV', 'OUSD', 'USD3', 'A7A5']


def _coin(cid, sym, chg24, chg7d, vec):
    return cid, {'symbol': sym, 'name': sym, 'usd': 1.0,
                 'usd_24h_change': chg24, 'usd_7d_change': chg7d,
                 'usd_1h_change': vec[0], 'usd_14d_change': vec[3],
                 'usd_30d_change': vec[4], 'market_cap': 200_000_000}


def test_pegged_tokens_never_picked():
    # Give every pegged coin the strongest momentum in the set: without the
    # block they would top the longs. Two ordinary coins carry real moves so
    # the lists are non-empty and the test is meaningful.
    coins = [_coin('aaa', 'AAA', 12.0, 20.0, [2, 12, 20, 25, 30]),
             _coin('bbb', 'BBB', 11.0, 18.0, [1, 11, 18, 24, 29]),
             _coin('ccc', 'CCC', -8.0, -12.0, [-2, -8, -12, -15, -18])]
    for sym in PEGGED:
        coins.append(_coin(sym.lower(), sym, 15.0, 25.0, [3, 15, 25, 30, 35]))
    data = {
        'fear_greed': [{'value': 50}],
        'crypto': dict(coins),
        'listings': {'coinbase': {'AAA', 'BBB', 'CCC'} | set(PEGGED),
                     'cryptocom': set()},
        'metals': {}, 'forex': {}, 'stocks': {}, 'contracts': [],
        'headlines': [], 'newsletter_digest': {}, 'macro': {},
    }
    history = {'stats': {}, 'picks': [], 'calibration': []}
    signals = nexintel.think(data, history)
    picked = [p['asset'] for p in signals['longs'] + signals['shorts']]
    for sym in PEGGED:
        assert sym not in picked, f'{sym} leaked into picks: {picked}'
    # Sanity: the lists are non-empty and meaningful. Do NOT pin a
    # specific synthetic coin to longs: the live corrected weights move
    # these coins around (2026-11-12 t=1 put SLOW short; t=2 leaves AAA
    # out of longs) — the pegged-not-picked assertions above are the
    # test's substance, and they are weight-independent.
    assert signals['longs'] or signals['shorts'], signals


def _think(coins):
    data = {
        'fear_greed': [{'value': 50}],
        'crypto': dict(coins),
        'listings': {'coinbase': {'AAA', 'BBB', 'CCC'},
                     'cryptocom': set()},
        'metals': {}, 'forex': {}, 'stocks': {}, 'contracts': [],
        'headlines': [], 'newsletter_digest': {}, 'macro': {},
    }
    history = {'stats': {}, 'picks': [], 'calibration': []}
    return nexintel.think(data, history)


def test_flat_coins_never_picked():
    # Behavioral filter: an unknown coin that has barely moved on EVERY
    # window (1h/24h/7d/14d/30d all < 1%) is cash, not a trade — it must
    # never be picked even though no name blocklist covers it.
    coins = [_coin('aaa', 'AAA', 12.0, 20.0, [2, 12, 20, 25, 30]),
             _coin('bbb', 'BBB', 11.0, 18.0, [1, 11, 18, 24, 29]),
             _coin('ccc', 'CCC', -8.0, -12.0, [-2, -8, -12, -15, -18]),
             _coin('flat1', 'FLAT1', 0.1, -0.2, [0.0, 0, 0, 0.1, 0.3]),
             _coin('flat2', 'FLAT2', 0.5, 0.2, [0.1, 0, 0, -0.4, 0.6])]
    signals = _think(coins)
    picked = [p['asset'] for p in signals['longs'] + signals['shorts']]
    assert 'FLAT1' not in picked, f'flat coin leaked: {picked}'
    assert 'FLAT2' not in picked, f'flat coin leaked: {picked}'


def test_flat_short_window_but_real_30d_move_can_pick():
    # The filter looks at ALL windows: a coin flat on 24h/7d but with a
    # real 30d move (e.g. LEO, quiet this week) stays eligible.
    coins = [_coin('aaa', 'AAA', 12.0, 20.0, [2, 12, 20, 25, 30]),
             _coin('bbb', 'BBB', 11.0, 18.0, [1, 11, 18, 24, 29]),
             _coin('ccc', 'CCC', -8.0, -12.0, [-2, -8, -12, -15, -18]),
             _coin('slow', 'SLOW', 0.1, 0.2, [0.0, 0, 0, 3.0, 8.0])]
    signals = _think(coins)
    picked = [p['asset'] for p in signals['longs'] + signals['shorts']]
    # Guard the filter, not the score sign: SLOW's tiny +0.1%/+0.2%
    # momentum can land on either side as weights drift (2026-11-12 the
    # corrected F t=1 weights put it short at score -0.0) — what matters
    # is that the flat-coin filter did not drop it.
    assert 'SLOW' in picked, f'real 30d mover wrongly filtered: {picked}'


def test_blocked_pick_is_excluded_not_graded(tmp_path=None):
    # Regression (2026-11-25): A7A5 was blocked from new picks on Nov 24,
    # but a Nov 22 A7A5 pick matured Nov 25 and remember() graded it at
    # the $0.00177 feed-glitch exit (+85.14% fake win) because the
    # blocklist lived only in think(). remember() must exclude an
    # ungraded blocked-symbol pick instead of scoring it, while a
    # normal pick in the same run still grades.
    import json
    import tempfile
    from pathlib import Path
    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp())
    def pick(asset, price):
        return {'asset': asset, 'price': price, 'market': 'crypto',
                'coin_id': asset.lower(), 'confidence': 52, 'conf_raw': 60,
                'thesis': {'direction': 'SHORT', 'expected_return_pct': 50.0,
                           'projected_exit': '2000-01-01'}}
    run = {'date': '2026-11-22T04:08:54+00:00', 'scored': False,
           'longs': [], 'shorts': [pick('A7A5', 0.011936), pick('BTC', 100.0)]}
    hist = {'picks': [run], 'stats': {'wins': 0, 'losses': 0, 'total': 0},
            'error': {'sum_abs_err': 0.0, 'n': 0, 'within_5pct': 0,
                      'metric': 'return_pct_v1'}}
    (tmp_path / 'history.json').write_text(json.dumps(hist))
    orig = nexintel.DATA_DIR
    nexintel.DATA_DIR = str(tmp_path)
    try:
        data = {'crypto': {'a7a5': {'symbol': 'A7A5', 'usd': 0.00177393},
                           'btc': {'symbol': 'BTC', 'usd': 90.0}},
                'stocks': {}}
        signals = {'longs': [], 'shorts': [],
                   'psychology': {'fear_greed': 50}, 'regime': 'NEUTRAL'}
        out = nexintel.remember(data, signals)
    finally:
        nexintel.DATA_DIR = orig
    a7a5, btc = out['picks'][0]['shorts']
    assert a7a5.get('excluded') is True and 'return_pct' not in a7a5, a7a5
    assert btc['return_pct'] == 10.0 and btc['win'] is True, btc
    assert out['stats'] == {'wins': 1, 'losses': 0, 'total': 1, 'win_rate': 100.0}
    assert out['error']['n'] == 1
    assert out['picks'][0]['scored'] is True


if __name__ == '__main__':
    for name, fn in sorted(list(globals().items())):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'{name}: PASS')
    print('all stablecoin block tests pass')
