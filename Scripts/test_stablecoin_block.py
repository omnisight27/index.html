"""Tests for the stablecoin block (2026-10-23 USDT incident, extended
2026-10-25): pegged and money-market tokens are cash, not trades. Five of
them leaked into the 2026-10-25 top longs (RLUSD, U, EURSAFO, EUTBL,
THBILL) because the blocklist only named the 15 classic stablecoins."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nexintel

PEGGED = ['RLUSD', 'U', 'EURSAFO', 'EUTBL', 'THBILL', 'USDT', 'USDC']


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


if __name__ == '__main__':
    for name, fn in sorted(list(globals().items())):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'{name}: PASS')
    print('all stablecoin block tests pass')
