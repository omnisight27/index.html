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
          'EURCV', 'USDCV', 'OUSD', 'USD3']


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
    assert 'AAA' in [p['asset'] for p in signals['longs']], signals['longs']


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
    longs = [p['asset'] for p in signals['longs']]
    assert 'SLOW' in longs, f'real 30d mover wrongly filtered: {longs}'


if __name__ == '__main__':
    for name, fn in sorted(list(globals().items())):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'{name}: PASS')
    print('all stablecoin block tests pass')
