#!/usr/bin/env python3
"""Tests for the Auction Market balance score (Nic 2026-10-31).

Auction Market Theory: markets alternate between BALANCE (rotational chop)
and IMBALANCE (directional). balance_score in [0,1]: 1 = tightly coiled
(compressed range, steady volume, choppy) — breakout watch; 0 = already in
imbalance. Display only — never a buy input.

Run: python3 -m pytest Scripts/test_balance_score.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intraday


# ---------------------------------------------------------------- fixtures

def chop(n, base=100.0, amp_pct=0.2):
    """Rotational chop: oscillates +/-amp_pct around base, ends near start."""
    return [base * (1 + amp_pct / 100 * (((i % 4) - 1.5) / 1.5))
            for i in range(n)]


def trend(n, base=100.0, step_pct=0.5):
    """Directional drift: step_pct per hour, compounding."""
    return [base * (1 + step_pct / 100) ** i for i in range(n)]


def steady_vol(n, v=1_000_000.0):
    return [v] * n


def wide_7d():
    """168 hourly closes with a wide range (~100 -> ~150)."""
    return trend(168, base=100.0, step_pct=0.25)


# ---------------------------------------------------------------- tests

def test_weights_sum_to_one():
    assert abs(sum(intraday.BALANCE_WEIGHTS.values()) - 1.0) < 1e-9
    assert abs(sum(intraday.BALANCE_WEIGHTS_NOVOL.values()) - 1.0) < 1e-9


def test_coiled_market_scores_high_and_flags():
    closes = chop(24, amp_pct=0.2)          # tiny 24h range
    score, parts = intraday.balance_score(
        closes, volumes=steady_vol(24), closes_7d=wide_7d())
    assert score is not None
    assert score >= intraday.COILED_THRESHOLD
    assert parts['compression'] > 0.9      # 24h range << 7d range
    assert parts['efficiency'] > 0.9       # chop ends where it started
    assert parts['volume'] == 1.0          # volume perfectly steady


def test_trending_market_scores_low():
    closes = trend(24, step_pct=0.8)        # +21% directional day
    score, parts = intraday.balance_score(
        closes, volumes=steady_vol(24), closes_7d=wide_7d())
    assert score is not None
    assert score < 0.5                     # well under the coiled line
    assert not (score >= intraday.COILED_THRESHOLD)
    assert parts['efficiency'] < 0.15      # net ~= path: pure direction


def test_dead_market_is_not_coiled():
    # Range compressed BUT volume collapsed: dead, not coiled.
    closes = chop(24, amp_pct=0.1)
    vols = steady_vol(24)
    vols[-6:] = [v * 0.05 for v in vols[-6:]]   # volume died in last 6h
    score, parts = intraday.balance_score(
        closes, volumes=vols, closes_7d=wide_7d())
    assert score is not None
    assert parts['volume'] < 0.2
    assert score < intraday.COILED_THRESHOLD


def test_volume_surge_still_counts_as_alive():
    # Volume surging while range stays tight = accumulation, not dead.
    closes = chop(24, amp_pct=0.2)
    vols = steady_vol(24)
    vols[-6:] = [v * 3.0 for v in vols[-6:]]
    score, parts = intraday.balance_score(
        closes, volumes=vols, closes_7d=wide_7d())
    assert parts['volume'] == 1.0
    assert score >= intraday.COILED_THRESHOLD


def test_no_volumes_renormalizes_weights():
    closes = chop(24, amp_pct=0.2)
    score, parts = intraday.balance_score(closes, closes_7d=wide_7d())
    assert score is not None
    assert parts['volume'] is None
    # 0.60*comp + 0.40*eff with comp~1, eff~1 -> ~1.0
    assert score >= intraday.COILED_THRESHOLD


def test_missing_data_fails_open():
    assert intraday.balance_score([100.0] * 5, closes_7d=wide_7d()) == (None, {})
    assert intraday.balance_score(chop(24), closes_7d=[100.0] * 10) == (None, {})
    assert intraday.balance_score(chop(24)) == (None, {})          # no 7d ref
    assert intraday.balance_score([]) == (None, {})
    assert intraday.balance_score(None) == (None, {})
    assert intraday.balance_score(chop(24), closes_7d=[100.0] * 168) == (None, {})  # flat 7d


def test_flat_closes_are_maximum_balance():
    closes = [100.0] * 24
    score, _ = intraday.balance_score(
        closes, volumes=steady_vol(24), closes_7d=wide_7d())
    assert score == 1.0


def test_score_always_in_unit_interval():
    import random
    rng = random.Random(42)
    for _ in range(50):
        closes = [100 * (1 + rng.uniform(-0.05, 0.05)) for _ in range(24)]
        vols = [abs(rng.gauss(1e6, 3e5)) for _ in range(24)]
        c7 = [100 * (1 + rng.uniform(-0.2, 0.2)) for _ in range(168)]
        score, parts = intraday.balance_score(closes, volumes=vols, closes_7d=c7)
        assert score is None or 0.0 <= score <= 1.0
        for k, v in parts.items():
            assert v is None or 0.0 <= v <= 1.0


def test_none_values_in_series_are_ignored():
    closes = chop(24)
    closes[5] = None
    vols = steady_vol(24)
    vols[9] = None
    c7 = wide_7d()
    c7[100] = None
    score, _ = intraday.balance_score(closes, volumes=vols, closes_7d=c7)
    assert score is not None and score >= intraday.COILED_THRESHOLD
