"""Tests for the two 2026-10-28 review fixes in the swing engine:

1. Thesis vol cap (build_thesis): a meme-coin +273% day used to project
   a +546% swing target in 1-5 days — not a credible thesis, and it blew
   up the model-error MAE when the coin reverted (Q, 2026-10-27:
   expected +545.86%, realized -66.83%, abs error 612.69 — the single
   biggest contributor to MAE rising 12.39 -> 18.68). The daily-move
   input is now capped at 25%, so expected return tops out at 50%.

2. Intra-crypto symbol collision (remember/price_for): a $0.03 token
   sharing BTC's symbol overwrote Bitcoin's price in the symbol-keyed
   map, scoring a BTC long at $0.028489 (return -100%). The map now
   keeps the largest market cap per symbol, and picks carry their
   CoinGecko id so scoring can match exactly.
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


# --- Thesis vol cap -------------------------------------------------------

def test_thesis_caps_meme_spike_long():
    # The real Q pick, 2026-10-27T02:58: +272.9% in 24h at $0.205159.
    # Pre-fix the thesis expected +545.86% with a $1.32 take-profit.
    t = nexintel.build_thesis('Q', 272.9, 55.3, 0.205159, 64.2, 75,
                              'NEUTRAL', 51, -0.01, 'crypto')
    assert t['direction'] == 'LONG'
    assert t['expected_return_pct'] == 50.0, t
    assert t['max_loss_pct'] == 37.5, t
    assert t['take_profit'] == pytest.approx(0.205159 * 1.5, abs=1e-6), t
    assert t['stop_loss'] == pytest.approx(0.205159 * 0.625, abs=1e-6), t


def test_thesis_caps_meme_crash_short():
    t = nexintel.build_thesis('X', -40.0, -50.0, 10.0, -30.0, 70,
                              'NEUTRAL', 51, 0.0, 'crypto')
    assert t['direction'] == 'SHORT'
    assert t['expected_return_pct'] == 50.0, t
    assert t['take_profit'] == pytest.approx(5.0), t
    assert t['stop_loss'] == pytest.approx(13.75), t


def test_thesis_normal_move_unchanged():
    # A +7.1% day stays exactly as before: vol 7.1 -> +14.2% target.
    t = nexintel.build_thesis('ARB', 7.1, 0.7, 100.0, 14.8, 64,
                              'NEUTRAL', 51, 0.0, 'crypto')
    assert t['expected_return_pct'] == 14.2, t
    assert t['take_profit'] == pytest.approx(114.2), t
    assert t['stop_loss'] == pytest.approx(89.35), t
    assert t['max_loss_pct'] == 10.65, t


# --- Intra-crypto symbol collision -----------------------------------------

def _pick(asset, price, coin_id=None):
    p = {'asset': asset, 'price': price, 'market': 'crypto',
         'score': 12.0, 'confidence': 46,
         'thesis': {'direction': 'LONG', 'expected_return_pct': 4.0,
                    'stop_loss': price * 0.97,
                    'take_profit': price * 1.04}}
    if coin_id:
        p['coin_id'] = coin_id
    return p


def _seed(tmp_path, runs):
    hist = {'picks': runs,
            'stats': {'wins': 0, 'losses': 0, 'total': 0},
            'error': {'sum_abs_err': 0.0, 'n': 0, 'within_5pct': 0,
                      'metric': 'return_pct_v1'}}
    (tmp_path / 'history.json').write_text(json.dumps(hist))
    nexintel.DATA_DIR = str(tmp_path)


def _signals():
    return {'longs': [], 'shorts': [],
            'psychology': {'fear_greed': 50}, 'regime': 'NEUTRAL'}


def test_duplicate_symbol_scores_largest_cap(tmp_path):
    # Bitcoin ($79,788, ~$1.5T cap) vs a $0.03 token sharing the BTC
    # symbol: the BTC long must score against Bitcoin, not the impostor.
    run = {'date': '2026-10-27T02:58:08+00:00', 'scored': False,
           'longs': [_pick('BTC', 79788.0)], 'shorts': []}
    _seed(tmp_path, [run])
    data = {'crypto': {
                'bitcoin': {'symbol': 'BTC', 'usd': 80000.0,
                            'market_cap': 1_500_000_000_000},
                'btc-token': {'symbol': 'BTC', 'usd': 0.028489,
                              'market_cap': 50_000}},
            'stocks': {}}
    hist = nexintel.remember(data, _signals())
    p = hist['picks'][0]['longs'][0]
    assert p['exit'] == 80000.0, p
    assert p['return_pct'] == pytest.approx(0.27, abs=0.01), p
    assert p['win'] is True


def test_coin_id_beats_symbol_map(tmp_path):
    # Even if a same-symbol impostor had the bigger market cap, a pick
    # carrying its CoinGecko id must score against its own coin.
    run = {'date': '2026-10-27T02:58:08+00:00', 'scored': False,
           'longs': [_pick('BTC', 79788.0, coin_id='bitcoin')],
           'shorts': []}
    _seed(tmp_path, [run])
    data = {'crypto': {
                'bitcoin': {'symbol': 'BTC', 'usd': 80000.0,
                            'market_cap': 1_500_000_000_000},
                'btc-impostor': {'symbol': 'BTC', 'usd': 0.03,
                                 'market_cap': 2_000_000_000_000}},
            'stocks': {}}
    hist = nexintel.remember(data, _signals())
    p = hist['picks'][0]['longs'][0]
    assert p['exit'] == 80000.0, p
    assert p['win'] is True
