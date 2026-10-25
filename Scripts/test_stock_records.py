#!/usr/bin/env python3
"""Stock picks in the graded record (Nic: "keep it simple but sophisticated").

The surface stays plain — one line per pick, same columns as crypto, no new
dashboards. The rigor lives underneath: stock swing picks are graded by the
same rules as crypto (target-first / stop-first / expiry via the engine's
own scoring), in the same history.json ledger, filterable by market.

Two defects this file locks in:

1. Symbol collision in remember(): crypto and stock tickers collide
   (CVX = Convex Finance AND Chevron; META = MetaDAO AND Meta
   Platforms). A single symbol-keyed price dict let the stock quote
   overwrite the crypto price, so crypto CVX longs scored at +9,000%
   — nine fake wins in the graded record. Prices are now keyed by
   market, and each pick is scored against its own market's price.

2. Direction-blind swing card: swing_urgent_card rendered every pick as
   "BUY ... Buy $X · Sell $Y · Stop $Z", so a stock SHORT (take-profit
   below entry, stop above — correct for a short) read as an inverted
   buy. Shorts now render as SHORT cards, and the urgent channel
   never claims a short as a buy.

Run: python3 -m pytest Scripts/test_stock_records.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nexintel


# ------------------------------------------------------------- thesis plans

def test_short_thesis_plan_is_not_inverted():
    # Chevron-style short: day down, negative score -> SHORT.
    t = nexintel.build_thesis('CVX', -2.0, -4.0, 233.21, -5.0, 80,
                              'NEUTRAL', 50, 0.0, 'equity')
    assert t['direction'] == 'SHORT'
    assert t['take_profit'] < 233.21 < t['stop_loss'], (
        f"short plan must read cover < entry < stop, got "
        f"tp={t['take_profit']} entry=233.21 sl={t['stop_loss']}")
    assert t['risk_reward'] == 1.33


def test_long_thesis_plan_direction_unchanged():
    # Crypto long — the existing behavior, locked so the stock fix
    # cannot quietly change it.
    t = nexintel.build_thesis('COMP', 3.0, 5.0, 24.35, 5.0, 78,
                              'NEUTRAL', 50, 0.0, 'crypto')
    assert t['direction'] == 'LONG'
    assert t['stop_loss'] < 24.35 < t['take_profit']


# ------------------------------------------------------------- market-aware scoring

def _fixture_history(tmp_path):
    hist = {
        'picks': [{
            'date': '2026-10-24T12:00:00+00:00', 'scored': False,
            'fear_greed': 50, 'regime': 'NEUTRAL',
            'longs': [
                {'asset': 'CVX', 'class': 'crypto', 'market': 'crypto',
                 'price': 2.40, 'score': 5.0, 'confidence': 78},
                {'asset': 'META', 'class': 'crypto', 'market': 'crypto',
                 'price': 5.00, 'score': 5.0, 'confidence': 78},
            ],
            'shorts': [
                {'asset': 'CVX', 'class': 'equity', 'market': 'stock',
                 'price': 233.21, 'score': -5.0, 'confidence': 80},
                {'asset': 'META', 'class': 'equity', 'market': 'stock',
                 'price': 700.00, 'score': -5.0, 'confidence': 80},
            ],
        }],
        'stats': {'wins': 0, 'losses': 0, 'total': 0},
        'error': {'sum_abs_err': 0.0, 'n': 0, 'within_5pct': 0},
    }
    (tmp_path / 'history.json').write_text(json.dumps(hist))
    return hist


def _market_data():
    return {
        'crypto': {
            'convex-finance': {'usd': 2.50, 'symbol': 'CVX'},
            'meta-dao': {'usd': 5.10, 'symbol': 'META'},
        },
        'stocks': {
            'CVX': {'c': 230.00},
            'META': {'c': 695.00},
        },
    }


def test_remember_scores_each_market_with_its_own_price(tmp_path, monkeypatch):
    _fixture_history(tmp_path)
    monkeypatch.setattr(nexintel, 'DATA_DIR', str(tmp_path))
    signals = {'longs': [], 'shorts': [], 'psychology': {'fear_greed': 50}}
    out = nexintel.remember(_market_data(), signals)
    run = out['picks'][0]
    assert run['scored'] is True

    by_key = {(p['asset'], p.get('market')): p
              for p in run['longs'] + run['shorts']}
    # Crypto CVX long: (2.50 - 2.40) / 2.40 = +4.17% — NOT +9,000%.
    cvx_c = by_key[('CVX', 'crypto')]
    assert abs(cvx_c['return_pct'] - 4.17) < 0.01, cvx_c
    assert cvx_c['win'] is True
    # Stock CVX short: price fell 233.21 -> 230.00, short gains +1.38%.
    cvx_s = by_key[('CVX', 'stock')]
    assert abs(cvx_s['return_pct'] - 1.38) < 0.01, cvx_s
    assert cvx_s['win'] is True
    # META, both markets, same discipline.
    meta_c = by_key[('META', 'crypto')]
    assert abs(meta_c['return_pct'] - 2.0) < 0.01, meta_c
    meta_s = by_key[('META', 'stock')]
    assert abs(meta_s['return_pct'] - 0.71) < 0.01, meta_s
    assert out['stats'] == {'wins': 4, 'losses': 0, 'total': 4,
                            'win_rate': 100.0}


def test_remember_falls_back_to_class_for_legacy_picks(tmp_path, monkeypatch):
    # Picks written before the market field existed carry class only.
    hist = {
        'picks': [{
            'date': '2026-10-24T12:00:00+00:00', 'scored': False,
            'fear_greed': 50, 'regime': 'NEUTRAL',
            'longs': [],
            'shorts': [
                {'asset': 'CVX', 'class': 'equity',
                 'price': 233.21, 'score': -5.0, 'confidence': 80},
            ],
        }],
        'stats': {'wins': 0, 'losses': 0, 'total': 0},
        'error': {'sum_abs_err': 0.0, 'n': 0, 'within_5pct': 0},
    }
    (tmp_path / 'history.json').write_text(json.dumps(hist))
    monkeypatch.setattr(nexintel, 'DATA_DIR', str(tmp_path))
    signals = {'longs': [], 'shorts': [], 'psychology': {'fear_greed': 50}}
    out = nexintel.remember(_market_data(), signals)
    p = out['picks'][0]['shorts'][0]
    assert abs(p['return_pct'] - 1.38) < 0.01, p  # scored vs stock price
    assert p['win'] is True


# ------------------------------------------------------------- market field

def test_think_emits_market_field():
    # Minimal fixture: one crypto long, one stock short. The market field
    # is what the graded ledger filters on — it must not silently drop.
    data = {
        'fear_greed': [{'value': 50}, {'value': 50}],
        'metals': {}, 'forex': {}, 'macro': {},
        'listings': {'coinbase': {'CVX', 'COMP'}, 'cryptocom': {'CVX'}},
        'crypto': {
            'convex-finance': {'usd': 2.49, 'symbol': 'CVX',
                'usd_24h_change': 1.0, 'usd_7d_change': 2.0,
                'usd_1h_change': 0.5, 'usd_14d_change': 1.5,
                'usd_30d_change': 3.0, 'market_cap': 200_000_000,
                'name': 'Convex Finance'},
        },
        'stocks': {'CVX': {'c': 233.21, 'dp': -1.5}},
    }
    history = {'picks': [], 'stats': {'win_rate': 50}, 'calibration': []}
    sig = nexintel.think(data, history)
    crypto_picks = [p for p in sig['longs'] + sig['shorts']
                    if p.get('class') == 'crypto']
    stock_picks = [p for p in sig['longs'] + sig['shorts']
                   if p.get('class') == 'equity']
    assert crypto_picks, 'fixture produced no crypto pick'
    assert stock_picks, 'fixture produced no stock pick'
    assert all(p.get('market') == 'crypto' for p in crypto_picks)
    assert all(p.get('market') == 'stock' for p in stock_picks)


# ------------------------------------------------------------- watchlist (NRR + SCHD, 2026-10-25)

def test_watchlist_includes_nrr_and_schd():
    # Nic asked for the Bitwise NEAR ETF and the Schwab dividend ETF to be
    # scored every run like the other ETFs. The universe is a module-level
    # constant now so it cannot drift from the ingest loop.
    assert 'NRR' in nexintel.STOCK_SYMBOLS
    assert 'SCHD' in nexintel.STOCK_SYMBOLS
    assert len(nexintel.STOCK_SYMBOLS) == 46
    assert len(set(nexintel.STOCK_SYMBOLS)) == 46  # no duplicates


def test_think_scores_nrr_on_a_strong_day():
    # NRR up 5% on the day must surface as an equity long with its real
    # display name — the same path every other ETF takes.
    data = {
        'fear_greed': [{'value': 50}, {'value': 50}],
        'metals': {}, 'forex': {}, 'macro': {},
        'listings': {'coinbase': set(), 'cryptocom': set()},
        'crypto': {},
        'stocks': {'NRR': {'c': 26.0, 'dp': 5.0},
                   'SCHD': {'c': 35.27, 'dp': 1.0}},
    }
    history = {'picks': [], 'stats': {'win_rate': 50}, 'calibration': []}
    sig = nexintel.think(data, history)
    nrr = [p for p in sig['longs'] + sig['shorts'] if p.get('asset') == 'NRR']
    assert nrr, sig['longs'] + sig['shorts']
    assert nrr[0].get('class') == 'equity'
    assert nrr[0].get('name') == 'Bitwise NEAR ETF'
