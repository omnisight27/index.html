#!/usr/bin/env python3
"""Regime v2 shadow gauge (Nic 2026-11-11, approved).

v1 only moves in 12-point steps (buy_pct = 50 + 12*integer score), so it
read exactly 38% from Oct 24 to Nov 11 while the market moved underneath
it. v2 scores each input continuously in [-1, +1] and blends them with
fixed weights, so the number moves with the market. v2 is shadow/display
only: nothing in decide.py, the gates, or the alert script reads it.

Run: python3 -m pytest Scripts/test_regime_v2.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nexintel


def _neutral_market():
    return {'btc_chg_7d': 0.0, 'btc_chg_24h': 0.0, 'breadth_up_share': 0.5}


def _neutral_macro():
    return {'cpi_yoy': {'value': 3.0},
            'jobless_claims': {'value': 200000, 'prev': 200000},
            'consumer_spending': {'change_pct': 0.2}}


def _v2(fg=50, xau=4100.0, jpy=150.0, macro=None, market=None):
    return nexintel.detect_regime_v2(fg, xau, jpy, macro, market)


def test_all_neutral_gives_50():
    regime, buy_pct, buy_text, comps = _v2(
        macro=_neutral_macro(), market=_neutral_market())
    assert buy_pct == 50
    assert regime == 'NEUTRAL'
    assert 'Mixed conditions (50% buy / 50% caution)' == buy_text
    assert comps['score'] == 0.0
    assert comps['omitted'] == []


def test_fear_greed_sweep_moves_smoothly():
    # Every 5-point Fear & Greed step moves buy_pct by at most ~2 points —
    # never the 12-point jumps v1 is stuck with.
    pcts = []
    for fg in range(0, 101, 5):
        _, buy_pct, _, _ = _v2(fg=fg, macro=_neutral_macro(),
                               market=_neutral_market())
        pcts.append(buy_pct)
    assert pcts[0] < pcts[-1]
    assert all(pcts[i + 1] >= pcts[i] for i in range(len(pcts) - 1))
    steps = [pcts[i + 1] - pcts[i] for i in range(len(pcts) - 1)]
    assert max(steps) <= 3, f'largest FG step jump: {max(steps)} (v1 jumps 12)'
    assert pcts[0] == 38 or pcts[0] == 37  # fg 0 -> score -0.25 -> ~37-38
    assert pcts[-1] == 62  # fg 100 -> score +0.25 -> 62.5 -> 62


def test_btc_trend_moves_it():
    base = dict(macro=_neutral_macro(), market=_neutral_market())
    _, down, _, _ = _v2(market={'btc_chg_7d': -20.0, 'breadth_up_share': 0.5},
                        macro=_neutral_macro())
    _, up, _, _ = _v2(market={'btc_chg_7d': 20.0, 'breadth_up_share': 0.5},
                      macro=_neutral_macro())
    assert down < 50 < up
    assert up - down >= 20  # tanh(2)=0.96 * 0.25 weight * 100 = ~24 pts apart


def test_btc_falls_back_to_24h_then_omits():
    _, pct_24h, _, comps_24h = _v2(
        market={'btc_chg_24h': 10.0, 'breadth_up_share': 0.5},
        macro=_neutral_macro())
    _, pct_7d, _, comps_7d = _v2(
        market={'btc_chg_7d': 10.0, 'btc_chg_24h': -10.0,
                'breadth_up_share': 0.5},
        macro=_neutral_macro())
    # 24h-only uses the 24h change; when 7d is present it wins over 24h.
    assert pct_24h > 50 and pct_7d > 50
    assert comps_24h['values']['btc_trend'] == comps_7d['values']['btc_trend']
    _, _, _, comps_none = _v2(market={'breadth_up_share': 0.5},
                              macro=_neutral_macro())
    assert 'btc_trend' in comps_none['omitted']


def test_breadth_moves_it():
    _, all_down, _, _ = _v2(market={'btc_chg_7d': 0.0, 'breadth_up_share': 0.0},
                            macro=_neutral_macro())
    _, all_up, _, _ = _v2(market={'btc_chg_7d': 0.0, 'breadth_up_share': 1.0},
                          macro=_neutral_macro())
    assert all_down < 50 < all_up
    assert all_up - all_down == 15  # 0.15 weight * 2.0 span * 50 = 15 pts


def test_macro_mean():
    hot = {'cpi_yoy': {'value': 5.0},
           'jobless_claims': {'value': 220000, 'prev': 200000},
           'consumer_spending': {'change_pct': 0.8}}
    _, _, _, comps = _v2(macro=hot, market=_neutral_market())
    # (-1 -1 +1) / 3 = -1/3 (components round to 4dp, so allow 1e-3)
    assert abs(comps['values']['macro'] + 1 / 3) < 1e-3


def test_extremes_clamp_to_5_and_95():
    bear = {'btc_chg_7d': -50.0, 'breadth_up_share': 0.0}
    hot = {'cpi_yoy': {'value': 5.0},
           'jobless_claims': {'value': 220000, 'prev': 200000},
           'consumer_spending': {'change_pct': -1.0}}
    regime, buy_pct, _, _ = _v2(fg=0, xau=5000.0, jpy=170.0, macro=hot, market=bear)
    assert buy_pct == 5
    assert regime == 'RISK_OFF'
    bull = {'btc_chg_7d': 50.0, 'breadth_up_share': 1.0}
    cool = {'cpi_yoy': {'value': 1.0},
            'jobless_claims': {'value': 180000, 'prev': 200000},
            'consumer_spending': {'change_pct': 0.8}}
    regime, buy_pct, _, _ = _v2(fg=100, xau=3000.0, jpy=130.0, macro=cool, market=bull)
    assert buy_pct == 95
    assert regime == 'RISK_ON'


def test_missing_inputs_fail_open_and_renormalize():
    # Everything missing: neutral, nothing crashes, everything omitted.
    regime, buy_pct, _, comps = _v2(None, None, None, None, None)
    assert buy_pct == 50 and regime == 'NEUTRAL'
    assert sorted(comps['omitted']) == sorted(nexintel.REGIME_V2_WEIGHTS)
    # Only Fear & Greed present: its weight renormalizes to 1.0.
    regime, buy_pct, _, comps = _v2(fg=100, xau=None, jpy=None)
    assert buy_pct == 95 and regime == 'RISK_ON'
    assert comps['weights'] == {'fear_greed': 1.0}


def test_v1_behavior_unchanged():
    # Classic stuck-at-38 case: one net caution flag -> 50 - 12 = 38.
    regime, reasons, buy_pct, buy_text = nexintel.detect_regime(50, 4104.0, 150.0, {})
    assert buy_pct == 38
    assert regime == 'RISK_OFF'
    assert buy_text == 'Mixed conditions (38% buy / 62% caution)'
    # Two net caution flags -> 26 (existing language-test case).
    regime, _, buy_pct, _ = nexintel.detect_regime(50, 4200.0, 160.0)
    assert buy_pct == 26
    assert regime == 'RISK_OFF'


def test_v2_at_current_live_inputs():
    # Live inputs ~Nov 11: Fear & Greed 29, gold ~$4,100, BTC 7d about
    # -8% (Nov 4 $80.5k -> Nov 10 ~$73.8k), breadth thin (~35% up).
    # v1 is stuck at 38 here; v2 should read lower and move with BTC.
    _, pct, _, comps = _v2(fg=29, xau=4100.0, jpy=150.0, macro={},
                           market={'btc_chg_7d': -8.3, 'breadth_up_share': 0.35})
    assert 30 <= pct <= 40
    assert comps['values']['btc_trend'] < 0
    _, pct_bounce, _, _ = _v2(fg=29, xau=4100.0, jpy=150.0, macro={},
                              market={'btc_chg_7d': 8.3, 'breadth_up_share': 0.65})
    assert pct_bounce > pct  # same fear/gold, BTC flipped -> gauge moves
