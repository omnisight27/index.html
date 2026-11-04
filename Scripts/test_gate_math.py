#!/usr/bin/env python3
"""Tests for gate_math.py — Trade Signal Gate v3.0 true-net return math.

Run: python3 -m pytest Scripts/test_gate_math.py
"""
from decimal import Decimal

import gate_math as gm


def zero_fees():
    return {'entry_fee_pct': Decimal('0'), 'exit_fee_pct': Decimal('0'),
            'entry_slip_pct': Decimal('0'), 'exit_slip_pct': Decimal('0'),
            'stop_slip_pct': Decimal('0')}


def test_net_exactly_280_passes():
    # With zero costs, target = entry x 1.028 is exactly 2.80% net.
    g = gm.evaluate_net_gate(100, Decimal('102.80'), 90, 'LONG',
                             current_price=100, price_tick=Decimal('0.01'),
                             fees=zero_fees())
    assert g['passed'] is True
    assert g['reason'] is None
    assert abs(g['net_return'] - 0.028) < 1e-12


def test_one_tick_below_280_fails():
    # One precision unit below the floor must fail — no float fuzz.
    g = gm.evaluate_net_gate(100, Decimal('102.79'), 90, 'LONG',
                             current_price=100, price_tick=Decimal('0.01'),
                             fees=zero_fees())
    assert g['passed'] is False
    assert g['reason'] == gm.NET_RETURN_BELOW_FLOOR


def test_gross_above_280_but_net_below_fails():
    # 3.0% gross looks fine until the 1.4% round-trip cost is applied.
    g = gm.evaluate_net_gate(100, 103, 97, 'LONG', current_price=100,
                             price_tick=Decimal('0.01'), fees=None)
    assert g['passed'] is False
    assert g['reason'] == gm.NET_RETURN_BELOW_FLOOR
    assert g['net_return'] < 0.028


def test_healthy_plan_passes_with_real_costs():
    # vol ~10%: +12% target / -6% stop — clears 2.8% net with real fees.
    g = gm.evaluate_net_gate(100, 112, 94, 'LONG', current_price=100,
                             price_tick=Decimal('0.01'), fees=None)
    assert g['passed'] is True, g
    assert g['net_return'] >= 0.028
    assert g['order_type'] == 'IMMEDIATE'


def test_missing_fee_data_fails_closed():
    # Explicit config with a None field -> REJECT, never silent zero.
    bad = dict(zero_fees())
    bad['exit_fee_pct'] = None
    g = gm.evaluate_net_gate(100, 110, 90, 'LONG', fees=bad)
    assert g['passed'] is False
    assert g['reason'] == gm.MISSING_GATE_INPUTS
    # Missing keys entirely -> same.
    g2 = gm.evaluate_net_gate(100, 110, 90, 'LONG', fees={'entry_fee_pct': 0})
    assert g2['passed'] is False
    assert g2['reason'] == gm.MISSING_GATE_INPUTS
    # Negative fee -> reject.
    neg = dict(zero_fees())
    neg['entry_fee_pct'] = Decimal('-0.001')
    g3 = gm.evaluate_net_gate(100, 110, 90, 'LONG', fees=neg)
    assert g3['passed'] is False
    assert g3['reason'] == gm.MISSING_GATE_INPUTS


def test_invalid_tick_fails_closed():
    g = gm.evaluate_net_gate(100, 110, 90, 'LONG', price_tick=0,
                             fees=zero_fees())
    assert g['passed'] is False
    assert g['reason'] == gm.MISSING_GATE_INPUTS


def test_required_sell_ceiling_binding():
    # target exactly at the gate's required_sell passes; the required
    # sell is the ceiling, so it always leaves >= 2.8% net.
    g = gm.evaluate_net_gate(100, 112, 94, 'LONG', price_tick=Decimal('0.01'),
                             fees=None)
    assert g['passed'] is True
    req = Decimal(str(g['required_sell']))
    g2 = gm.evaluate_net_gate(100, req, 94, 'LONG',
                              price_tick=Decimal('0.01'), fees=None)
    assert g2['passed'] is True, g2
    # One tick under the required sell fails the required-sell check.
    # (net is still >= floor here by a hair — the binding rule is the
    # ceiling itself.)
    g3 = gm.evaluate_net_gate(100, req - Decimal('0.01'), 94, 'LONG',
                              price_tick=Decimal('0.01'), fees=None)
    assert g3['passed'] is False
    assert g3['reason'] in (gm.REQUIRED_SELL_NOT_MET,
                            gm.NET_RETURN_BELOW_FLOOR)


def test_reward_risk_reported_not_rejecting():
    # Net reward:risk below 2.0 is diagnostic-only (see module docstring):
    # a plan clearing the net floor must still pass even when net rr < 2.
    g = gm.evaluate_net_gate(100, 112, 94, 'LONG', fees=None,
                             price_tick=Decimal('0.01'))
    assert g['passed'] is True
    assert g['reward_risk'] is not None
    assert g['reward_risk'] < 2.0  # documents the asymptote reality
    assert 'rr_note' in g


def test_invalid_sides_reject():
    # LONG with stop above entry.
    g = gm.evaluate_net_gate(100, 110, 105, 'LONG', fees=zero_fees())
    assert g['passed'] is False and g['reason'] == gm.INVALID_STOP
    # LONG with target below entry.
    g = gm.evaluate_net_gate(100, 95, 90, 'LONG', fees=zero_fees())
    assert g['passed'] is False and g['reason'] == gm.INVALID_TARGET
    # Non-positive inputs.
    g = gm.evaluate_net_gate(0, 110, 90, 'LONG', fees=zero_fees())
    assert g['passed'] is False and g['reason'] == gm.INVALID_INPUT
    # Unknown direction.
    g = gm.evaluate_net_gate(100, 110, 90, 'SIDEWAYS', fees=zero_fees())
    assert g['passed'] is False and g['reason'] == gm.INVALID_INPUT


def test_short_direction_mirrors():
    # SHORT: +2.8% net means the cover sits 2.8% under net proceeds.
    g = gm.evaluate_net_gate(100, Decimal('97.20'), 110, 'SHORT',
                             current_price=100, price_tick=Decimal('0.01'),
                             fees=zero_fees())
    assert g['passed'] is True, g
    assert abs(g['net_return'] - 0.028) < 1e-12
    # Cover one tick too high -> fails.
    g2 = gm.evaluate_net_gate(100, Decimal('97.21'), 110, 'SHORT',
                              current_price=100, price_tick=Decimal('0.01'),
                              fees=zero_fees())
    assert g2['passed'] is False


def test_order_labels():
    ot, note = gm.order_label(105, 100)
    assert ot == 'STOP_BUY'
    assert 'never an ordinary limit buy' in note
    ot, note = gm.order_label(95, 100)
    assert ot == 'LIMIT_BUY'
    assert 'limit buy permitted' in note
    ot, note = gm.order_label(100, 100)
    assert ot == 'IMMEDIATE'
    ot, note = gm.order_label(105, None)
    assert ot is None and note is None


def test_tick_for_price_bands():
    assert gm.tick_for_price(150) == Decimal('0.01')
    assert gm.tick_for_price(5) == Decimal('0.0001')
    assert gm.tick_for_price(0.5) == Decimal('0.000001')
    assert gm.tick_for_price(0.002) == Decimal('0.00000001')


def test_ceil_to_tick():
    assert gm.ceil_to_tick('1.001', '0.01') == Decimal('1.01')
    assert gm.ceil_to_tick('1.00', '0.01') == Decimal('1.00')


def test_nan_inputs_fail_closed():
    # NaN comparisons are quiet False — the gate must still reject,
    # never let a non-finite value pass.
    nan = float('nan')
    for bad in [(nan, 104.0, 98.0), (100.0, nan, 98.0), (100.0, 104.0, nan)]:
        r = gm.evaluate_net_gate(*bad)
        assert r['passed'] is False and r['reason'] == 'INVALID_INPUT'
    r = gm.evaluate_net_gate(100.0, 104.0, 98.0,
                             fees={**gm.default_fees(), 'entry_fee_pct': nan})
    assert r['passed'] is False and r['reason'] == 'MISSING_GATE_INPUTS'
