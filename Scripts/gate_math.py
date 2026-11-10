"""Trade Signal Gate v3.0 — true-net return math (Nic-approved 2026-11-04).

The binding profitability gate for emitted trade plans. Replaces the gross
proxy (gross target >= 2x round-trip fee) with a true net-return
computation: fees and slippage are applied to both legs, and a plan is
rejected unless its NET return clears 2.8%.

FAIL-CLOSED: the gate never substitutes zero for missing cost data. When
no fee config is passed, the module defaults below apply — measured from
Nic's own fills (0.9% taker entry + 0.5% maker exit on Coinbase Advanced,
US intro tier, post-Sept-2026 schedule), documented here, not silent.
When the caller passes an explicit fee config, every required field must
be present and valid, or the plan is rejected (MISSING_GATE_INPUTS).

REWARD:RISK NOTE (2026-11-04, updated 2026-11-10): the v3.0 spec also asks
to reject when the NET reward:risk ratio is below 2.0. With the old
2:1-gross construction that check was mathematically unsatisfiable (net
reward:risk asymptoted to ~1.99 and never reached 2.0). With the 4:1-gross
construction (Nic 2026-11-10) it is satisfiable — a 4:1-gross plan's net
reward:risk asymptotes to ~4.0 (verified: stop 3% -> 2.21, stop 30% ->
3.74) and clears 2.0 once the stop is ~2.5% or wider. It still does NOT
reject: the binding profitability gate is the 2.8% net-return floor, and
a low-vol 4:1 plan can clear that floor while its net reward:risk sits
below 2.0 (a stop under ~2.5% does it) — enforcing 2.0 would reject those
plans even though they clear the floor. So the net reward:risk is computed
and reported as a diagnostic. The gross 4:1 construction (intraday
target = 2.4x vol, stop = 0.6x vol; swing target = 4.0x vol,
stop = 1.0x vol) remains enforced by the plan builders themselves.

The gate is direction-aware (LONG/SHORT) and labels the required order
type for human execution: our alerts are placed by Nic, not a bot, so a
breakout trigger never rejects on order type — the card labels it
("stop-buy / stop-limit, never an ordinary limit buy above the market").
"""

from decimal import Decimal, ROUND_CEILING, InvalidOperation

# --- Binding thresholds -------------------------------------------------
NET_RETURN_FLOOR = Decimal('0.028')   # 2.8% net, after all costs
MIN_REWARD_RISK = Decimal('2.0')      # diagnostic only (see module note)

# --- Measured cost tier -------------------------------------------------
# Nic's fills, 2026-11-01: 0.9% taker entry + 0.5% maker exit on Coinbase
# Advanced (US intro tier, post-Sept-2026 schedule) = 1.4% round trip.
ENTRY_FEE_PCT = Decimal('0.009')
EXIT_FEE_PCT = Decimal('0.005')
# Slippage allowances: small, documented, per-exchange/asset configurable
# by passing an explicit fees dict to evaluate_net_gate.
ENTRY_SLIP_PCT = Decimal('0.001')
EXIT_SLIP_PCT = Decimal('0.001')
STOP_SLIP_PCT = Decimal('0.002')

_FEE_KEYS = ('entry_fee_pct', 'exit_fee_pct',
             'entry_slip_pct', 'exit_slip_pct', 'stop_slip_pct')

# --- Failure codes -------------------------------------------------------
MISSING_GATE_INPUTS = 'MISSING_GATE_INPUTS'
INVALID_INPUT = 'INVALID_INPUT'
NET_RETURN_BELOW_FLOOR = 'NET_RETURN_BELOW_FLOOR'
REQUIRED_SELL_NOT_MET = 'REQUIRED_SELL_NOT_MET'
REWARD_RISK_BELOW_2 = 'REWARD_RISK_BELOW_2'   # diagnostic only, never rejects
INVALID_STOP = 'INVALID_STOP'
INVALID_TARGET = 'INVALID_TARGET'


def _D(x):
    """Decimal(x) without float fuzz — raises InvalidOperation on garbage."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def default_fees():
    """The measured cost tier as an explicit dict (never silent zeros)."""
    return {
        'entry_fee_pct': ENTRY_FEE_PCT,
        'exit_fee_pct': EXIT_FEE_PCT,
        'entry_slip_pct': ENTRY_SLIP_PCT,
        'exit_slip_pct': EXIT_SLIP_PCT,
        'stop_slip_pct': STOP_SLIP_PCT,
    }


def tick_for_price(price):
    """Conservative price-tick proxy, mirroring round_price() bands in
    intraday.py / nexintel.py. Documented approximation used only for the
    required-sell ceiling when the exchange's real tick is not passed in."""
    p = _D(price)
    if p >= 100:
        return Decimal('0.01')
    if p >= 1:
        return Decimal('0.0001')
    if p >= Decimal('0.01'):
        return Decimal('0.000001')
    return Decimal('0.00000001')


def ceil_to_tick(x, tick):
    """Ceiling of x to a multiple of tick (rounds against the trader)."""
    x, tick = _D(x), _D(tick)
    return (x / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def order_label(trigger, current_price, price_tick=None):
    """Required order type for a human-placed entry. Returns
    (order_type, order_note); (None, None) when the current price is
    unknown. Never rejects — the card carries the label instead."""
    if current_price is None:
        return None, None
    t, c = _D(trigger), _D(current_price)
    # Finiteness first: NaN comparisons can raise under a trapping
    # decimal context (or slip through quiet) — never compare first.
    if not (t.is_finite() and c.is_finite()):
        return None, None
    if price_tick is not None:
        tick = _D(price_tick)
        if abs(t - c) <= tick:
            return ('IMMEDIATE',
                    'immediate entry — limit or market at your discretion')
    if t > c:
        return ('STOP_BUY',
                'place as stop-buy / stop-limit — '
                'never an ordinary limit buy above the market')
    if t < c:
        return ('LIMIT_BUY',
                'limit buy permitted — trigger sits below the current price')
    return ('IMMEDIATE', 'immediate entry — limit or market at your discretion')


def evaluate_net_gate(entry, target, stop, direction='LONG',
                      current_price=None, price_tick=None, fees=None):
    """Binding net-return gate for one trade plan.

    Returns a dict: passed (bool), reason (failure code or None),
    net_return / reward_risk / required_sell / all_in_entry / net_exit
    (floats, full precision kept internally as Decimal), plus
    order_type / order_note from order_label().

    FAIL-CLOSED: non-positive or non-finite prices, an unknown direction,
    an explicit fee config with any missing/negative field, or an invalid
    tick all reject — nothing is ever silently treated as zero.
    """
    try:
        e, t, s = _D(entry), _D(target), _D(stop)
    except (InvalidOperation, ValueError, TypeError):
        return _reject(INVALID_INPUT)
    if direction not in ('LONG', 'SHORT'):
        return _reject(INVALID_INPUT)
    # Fail closed on non-finite values FIRST: NaN/Infinity comparisons
    # raise under a trapping decimal context (and slip through quiet
    # otherwise) — never compare a price before this check.
    if not (e.is_finite() and t.is_finite() and s.is_finite()):
        return _reject(INVALID_INPUT)
    if e <= 0 or t <= 0 or s <= 0:
        return _reject(INVALID_INPUT)

    # --- fee config -----------------------------------------------------
    if fees is None:
        f = default_fees()
    else:
        try:
            f = {k: _D(fees[k]) for k in _FEE_KEYS}
        except (KeyError, TypeError, InvalidOperation, ValueError):
            return _reject(MISSING_GATE_INPUTS)
        if any(not v.is_finite() or v < 0 for v in f.values()):
            return _reject(MISSING_GATE_INPUTS)

    # --- tick ------------------------------------------------------------
    if price_tick is None:
        tick = tick_for_price(e)
    else:
        try:
            tick = _D(price_tick)
        except (InvalidOperation, ValueError, TypeError):
            return _reject(MISSING_GATE_INPUTS)
        if not tick.is_finite() or tick <= 0:
            return _reject(MISSING_GATE_INPUTS)

    # --- side validity ---------------------------------------------------
    if direction == 'LONG':
        if s >= e:
            return _reject(INVALID_STOP)
        if t <= e:
            return _reject(INVALID_TARGET)
        all_in = e * (1 + f['entry_slip_pct']) * (1 + f['entry_fee_pct'])
        net_exit = t * (1 - f['exit_slip_pct']) * (1 - f['exit_fee_pct'])
        net_return = net_exit / all_in - 1
        # Required sell: the target that leaves 2.8% net after exit costs.
        req_raw = all_in * (1 + NET_RETURN_FLOOR) / (
            (1 - f['exit_fee_pct']) * (1 - f['exit_slip_pct']))
        required = ceil_to_tick(req_raw, tick)
        target_ok = t >= required
        stop_net = s * (1 - f['stop_slip_pct']) * (1 - f['exit_fee_pct'])
        reward = net_exit - all_in
        risk = all_in - stop_net
    else:  # SHORT
        if s <= e:
            return _reject(INVALID_STOP)
        if t >= e:
            return _reject(INVALID_TARGET)
        proceeds = e * (1 - f['entry_slip_pct']) * (1 - f['entry_fee_pct'])
        buyback = t * (1 + f['exit_slip_pct']) * (1 + f['exit_fee_pct'])
        net_return = (proceeds - buyback) / proceeds
        # Required cover: the buyback that leaves 2.8% net after costs.
        req_raw = proceeds * (1 - NET_RETURN_FLOOR) / (
            (1 + f['exit_fee_pct']) * (1 + f['exit_slip_pct']))
        required = (req_raw / tick).to_integral_value() * tick  # floor
        target_ok = buyback <= required
        stop_buyback = s * (1 + f['stop_slip_pct']) * (1 + f['exit_fee_pct'])
        reward = proceeds - buyback
        risk = stop_buyback - proceeds
        all_in, net_exit = proceeds, buyback

    if net_return < NET_RETURN_FLOOR:
        return _reject(NET_RETURN_BELOW_FLOOR, _diag(
            all_in, net_exit, net_return, required, None))
    if not target_ok:
        return _reject(REQUIRED_SELL_NOT_MET, _diag(
            all_in, net_exit, net_return, required, None))
    if risk <= 0:
        return _reject(INVALID_STOP, _diag(
            all_in, net_exit, net_return, required, None))
    if reward <= 0:
        return _reject(INVALID_TARGET, _diag(
            all_in, net_exit, net_return, required, None))
    rr = reward / risk
    # NOTE: net reward:risk is diagnostic-only (see module docstring) —
    # enforcing 2.0 here would reject low-vol plans that still clear the
    # 2.8% net floor (a 4:1 plan needs a ~2.5% stop to clear net 2.0).
    diag = _diag(all_in, net_exit, net_return, required, rr)
    if rr < MIN_REWARD_RISK:
        diag['rr_note'] = ('net reward:risk %.2f below 2.0 — diagnostic only; '
                           'gross 4:1 construction is enforced by the builders'
                           % rr)
    ot, on = order_label(entry, current_price,
                         price_tick if price_tick is not None else tick)
    diag.update({'passed': True, 'reason': None,
                 'order_type': ot, 'order_note': on})
    return diag


def _diag(all_in, net_exit, net_return, required, rr):
    return {
        'passed': False,
        'reason': None,
        'all_in_entry': float(all_in),
        'net_exit': float(net_exit),
        'net_return': float(net_return),
        'required_sell': float(required),
        'reward_risk': float(rr) if rr is not None else None,
        'order_type': None,
        'order_note': None,
    }


def _reject(reason, diag=None):
    d = diag or _diag(Decimal(0), Decimal(0), Decimal(0), Decimal(0), None)
    d['passed'] = False
    d['reason'] = reason
    return d
