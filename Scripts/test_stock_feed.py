#!/usr/bin/env python3
"""Regression tests for the stocks research track, Phase 1 (2026-11-05).

Yahoo Finance v8 chart API (keyless), a fixed 30-ticker liquid US
large-cap universe, US market-hours handling, and a stock-specific
shadow fee tier (commission-free, 0.5% net floor). Stocks are
display-only + shadow-graded (kind='stock', separate ledger) — they
never join longs/shorts, never touch a buy gate, never alert.

Run: python3 -m pytest Scripts/test_stock_feed.py
"""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intraday
import stock_feed


# ---------------------------------------------------------------- fixtures

def yahoo_payload(closes, vols=None, t0=1_793_367_000):
    """Minimal Yahoo v8 chart result from hourly closes."""
    if vols is None:
        vols = [1_000_000] * len(closes)
    return {'chart': {'result': [{
        'meta': {'currency': 'USD', 'symbol': 'AAPL',
                 'exchangeName': 'NMS', 'instrumentType': 'EQUITY'},
        'timestamp': [t0 + i * 3600 for i in range(len(closes))],
        'indicators': {'quote': [{'close': closes, 'volume': vols}]},
    }]}}


def closes_for(rets, start=100.0):
    """Hourly closes from hourly % returns."""
    out = [start]
    for r in rets:
        out.append(out[-1] * (1 + r / 100))
    return out


# ---------------------------------------------------------------- universe

def test_universe_is_30_unique_liquid_names():
    u = stock_feed.STOCK_UNIVERSE
    assert len(u) == 30
    assert len(set(u)) == 30
    assert all(isinstance(s, str) and s.isalpha() and s.isupper()
               for s in u)
    for must in ('AAPL', 'MSFT', 'NVDA', 'JPM', 'XOM'):
        assert must in u


# ---------------------------------------------------------------- market hours

def _et(hour, minute=0, y=2026, m=11, d=5):
    """An aware UTC datetime for a given ET wall time (Nov = EST, -5)."""
    from zoneinfo import ZoneInfo
    et = datetime(y, m, d, hour, minute, tzinfo=ZoneInfo('America/New_York'))
    return et.astimezone(timezone.utc)


def test_market_open_weekday_mid_session():
    assert stock_feed.us_market_open_now(_et(10, 0)) is True
    assert stock_feed.us_market_open_now(_et(15, 59)) is True


def test_market_closed_before_after_and_weekend():
    assert stock_feed.us_market_open_now(_et(9, 29)) is False
    assert stock_feed.us_market_open_now(_et(8, 0)) is False
    assert stock_feed.us_market_open_now(_et(16, 0)) is False
    assert stock_feed.us_market_open_now(_et(20, 0)) is False
    # Saturday 2026-11-07
    assert stock_feed.us_market_open_now(_et(10, 0, d=7)) is False
    # Sunday 2026-11-08
    assert stock_feed.us_market_open_now(_et(10, 0, d=8)) is False


def test_market_closed_on_holidays():
    # New Year's Day 2026 (Thu) and observed July 4th 2026 (Fri Jul 3)
    assert stock_feed.us_market_open_now(_et(10, 0, y=2026, m=1, d=1)) is False
    assert stock_feed.us_market_open_now(_et(10, 0, y=2026, m=7, d=3)) is False
    # Thanksgiving 2026 (Thu Nov 26)
    assert stock_feed.us_market_open_now(_et(10, 0, y=2026, m=11, d=26)) is False


# ---------------------------------------------------------------- Yahoo parse

def test_fetch_parses_yahoo_payload_and_drops_forming_candle(monkeypatch):
    closes = closes_for([0.1] * 24, start=340.0)  # 25 bars
    vols = [5_000_000] * 24 + [0]  # last bar: forming hour, volume 0
    payload = yahoo_payload(closes, vols)

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            import json
            return json.dumps(payload).encode()

    def fake_urlopen(req, timeout=30):
        return Resp()

    monkeypatch.setattr(stock_feed.urllib.request, 'urlopen', fake_urlopen)
    out = stock_feed.fetch_stock_candles('AAPL')
    assert out is not None
    # Forming candle dropped: 25 bars in, 24 out.
    assert len(out['closes']) == 24
    assert out['last'] == pytest.approx(closes[-2], rel=1e-9)


def test_fetch_returns_none_on_bad_payload(monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"chart": {"result": []}}'

    monkeypatch.setattr(stock_feed.urllib.request, 'urlopen',
                        lambda req, timeout=30: Resp())
    assert stock_feed.fetch_stock_candles('AAPL') is None


# ---------------------------------------------------------------- stock fee tier

def test_stock_fee_floor_is_stock_sized_not_crypto_sized():
    # Crypto floor (2.8%) would need hourly vol >= 2.33%. The stock
    # floor (0.2% target) needs vol >= ~0.17% — a normal large-cap hour.
    assert stock_feed.clears_stock_fee_floor(0.5) is True
    assert stock_feed.clears_stock_fee_floor(0.2) is True
    assert stock_feed.clears_stock_fee_floor(0.1) is False
    assert stock_feed.clears_stock_fee_floor(None) is False


def test_stock_net_gate_passes_typical_stock_move():
    # vol 0.55%/h -> target +0.66%, net ~+0.56% after stock costs.
    ok, net, reason = stock_feed.stock_net_gate(100, 100.66, 99.56, 'LONG')
    assert ok is True and reason is None
    assert net >= 0.5
    # vol 0.2%/h -> target +0.24%, net ~+0.14% — below the 0.5% floor.
    ok, _net, reason = stock_feed.stock_net_gate(100, 100.24, 99.84, 'LONG')
    assert ok is False and reason == 'NET_RETURN_BELOW_FLOOR'
    # SHORT direction mirrors.
    ok, net, _ = stock_feed.stock_net_gate(100, 99.34, 100.44, 'SHORT')
    assert ok is True and net >= 0.5
    # Invalid inputs fail closed.
    assert stock_feed.stock_net_gate(0, 100.6, 99.6, 'LONG')[0] is False
    assert stock_feed.stock_net_gate(100, 99.0, 99.6, 'LONG')[0] is False


def test_stock_net_gate_never_touches_crypto_gate():
    # gate_math.py must not be imported by the stock module, and the
    # stock module must never call the crypto net gate or the circuit
    # breaker hook. Checked by AST (mentions in comments/docstrings
    # don't count — only real imports and calls do).
    import ast
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'stock_feed.py')).read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name.split('.')[0] not in ('gate_math', 'intraday')
        if isinstance(node, ast.ImportFrom):
            assert (node.module or '').split('.')[0] not in (
                'gate_math', 'intraday')
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, 'id', '') or getattr(f, 'attr', '')
            assert name not in ('evaluate_net_gate', '_record_gate_rejection')


# ---------------------------------------------------------------- thesis + scan

def _features(chg_1h=0.6, vol=0.55):
    return {'chg_1h': chg_1h, 'chg_4h': 1.0, 'chg_24h': 1.5, 'vol': vol,
            'rsi': 55.0, 'pos_in_range': 0.6, 'accel': 0.1, 'price': 340.0}


def test_build_stock_thesis_typical_move():
    th = stock_feed.build_stock_thesis('AAPL', _features(), 2.0)
    assert th is not None
    assert th['direction'] == 'LONG'
    assert th['take_profit'] > th['entry'] > th['stop_loss']
    assert th['order_note'].startswith('Stock shadow')
    assert th['net_return_pct'] >= 0.5


def test_build_stock_thesis_rejects_low_vol():
    assert stock_feed.build_stock_thesis('AAPL', _features(vol=0.1),
                                         2.0) is None
    # Short direction builds the mirror plan.
    th = stock_feed.build_stock_thesis(
        'AAPL', _features(chg_1h=-0.6, vol=0.55), -2.0)
    assert th is not None and th['direction'] == 'SHORT'
    assert th['stop_loss'] > th['entry'] > th['take_profit']


def test_scan_stock_with_rising_series():
    # Volatile uptrend: alternating +0.6/-0.5 moves (hourly vol ~0.6%),
    # finishing with a strong up push — scores LONG with a real thesis.
    rets = ([0.6, -0.5, 0.7, -0.4] * 5) + [1.2, 0.9, 1.1, 1.0]
    closes = closes_for(rets, start=340.0)
    pts = [[1_793_367_000 * 1000 + i * 3600000, c]
           for i, c in enumerate(closes)]
    vols = [5_000_000] * len(pts)
    c7 = closes_for([0.05] * 48, start=335.0)
    sig = stock_feed.scan_stock_with(
        'AAPL', pts, intraday.hourly_features, intraday.score_intraday,
        intraday.balance_score, volumes=vols, closes_7d=c7)
    assert sig is not None
    assert sig['asset'] == 'AAPL'
    assert sig['direction'] == 'LONG'
    assert 40 <= sig['confidence'] <= 95
    assert sig['thesis']['entry'] > 0
    assert sig['info_url'].startswith('https://finance.yahoo.com/quote/AAPL')


def test_scan_stock_with_flat_series_scores_nothing():
    closes = closes_for([0.0] * 24, start=340.0)
    pts = [[1_793_367_000 * 1000 + i * 3600000, c]
           for i, c in enumerate(closes)]
    sig = stock_feed.scan_stock_with(
        'AAPL', pts, intraday.hourly_features, intraday.score_intraday,
        intraday.balance_score, volumes=[5_000_000] * len(pts),
        closes_7d=closes_for([0.0] * 48, start=340.0))
    assert sig is None


def test_scan_stock_with_too_few_bars():
    assert stock_feed.scan_stock_with(
        'AAPL', [[1, 100.0], [2, 101.0]], intraday.hourly_features,
        intraday.score_intraday, intraday.balance_score) is None
