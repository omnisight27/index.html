#!/usr/bin/env python3
"""
NexIntel Intraday Engine — hourly buy/sell signals for crypto.
Runs separately from the swing engine (nexintel.py).

Strategy: hourly momentum + mean-reversion on top 50 coins by market cap.
Horizon: 1-4 hours. Tighter stops than swing (0.8x hourly vol).
"""
import json, os, math, sys, tempfile, urllib.request, subprocess
from datetime import datetime, timezone

try:
    import funding as funding_feed  # Feed #2: Binance funding rates (fail-open)
except ImportError:  # engine must never die on a feed import
    funding_feed = None
try:
    import oi_flow  # Feed #3: open-interest flow (fail-open)
except ImportError:
    oi_flow = None
try:
    import econ_features as econ_feed  # Milestone 2: econ candidate pack (display only)
except ImportError:
    econ_feed = None
try:
    import pattern_feed  # Price-action research track, Phase 1 (display only)
except ImportError:
    pattern_feed = None
try:
    import cycle_feed  # Cycle research track, Phase 1 (display only)
except ImportError:
    cycle_feed = None
try:
    import cmi_feed  # Manipulation research track, Phase 1 (display only)
except ImportError:
    cmi_feed = None
try:
    import stock_feed  # Stocks research track, Phase 1 (display only)
except ImportError:
    stock_feed = None
try:
    import okx_feed  # OKX derivatives track, Phase 1 (display only)
except ImportError:
    okx_feed = None
try:
    import deribit_feed  # Deribit options track, Phase 1 (display only)
except ImportError:
    deribit_feed = None
try:
    import defillama_feed  # DeFiLlama stablecoin/TVL track (display only)
except ImportError:
    defillama_feed = None

# Trade Signal Gate v3.0 (Nic 2026-11-04): true-net return math + circuit
# breaker. First-party modules in this directory — hard import on purpose:
# if they are ever absent something is deeply wrong, and failing loudly
# beats silently running the weaker gross-only gate while v3.0 is believed
# live.
from gate_math import evaluate_net_gate, tick_for_price
import gate_circuit

# --- Trade Signal Gate v3.0: rejection events ---------------------------
# Events accumulate in-memory during a run; main() merges them with the
# prior run's events carried in intraday.json['gate_events'] (the
# committed file — ephemeral runners would lose a separate state file)
# and stamps the computed breaker state in out['gate_circuit'].
_GATE_EVENTS = []


def _record_gate_rejection(reason):
    """Append one gate-rejection event for this run. Only systemic
    reasons (missing cost data, stale feeds) can trip the breaker;
    per-plan profitability rejections are the gate working as designed
    and are diagnostic-only (see gate_circuit). In-memory, never raises,
    never touches the filesystem (safe inside unit tests)."""
    global _GATE_EVENTS
    try:
        _GATE_EVENTS = gate_circuit.record_event(
            _GATE_EVENTS, reason,
            systemic=(reason in gate_circuit.SYSTEMIC_REASONS))
    except Exception:
        pass  # bookkeeping must never kill the engine


def _apply_net_gate(entry, target, stop, direction, current_price):
    """Binding net-return gate for a raw (unrounded) plan. Returns
    (gate, order_type, order_note); on rejection the failure is recorded
    for the circuit breaker and (None, None, None) is returned.
    clears_fee_floor stays the cheap pre-check; this is the binding one."""
    gate = evaluate_net_gate(entry, target, stop, direction=direction,
                             current_price=current_price,
                             price_tick=tick_for_price(entry))
    if not gate['passed']:
        _record_gate_rejection(gate['reason'])
        return None, None, None
    return gate, gate['order_type'], gate['order_note']

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '..', 'data')
# Resolve relative to repo root
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, 'data')
# --check (dry run): all file I/O is redirected to a throwaway temp dir, so a
# local smoke test can never overwrite the data/intraday.json the scheduled
# loop publishes (same guard the swing engine got in the 2026-11-02 review).
CHECK_MODE = '--check' in sys.argv[1:]

def fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': 'NexIntel-Intraday/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f'  [warn] {url[:70]}... failed: {e}')
        return None

def hourly_features(prices):
    """Extract intraday features from hourly price series (24 points)."""
    if len(prices) < 5:
        return None
    closes = [p[1] for p in prices]
    now = closes[-1]
    chg_1h = (now - closes[-2]) / closes[-2] * 100 if closes[-2] else 0
    chg_4h = (now - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 and closes[-5] else 0
    chg_24h = (now - closes[0]) / closes[0] * 100 if closes[0] else 0

    # Hourly volatility (std of hourly returns)
    rets = [(closes[i] - closes[i-1]) / closes[i-1] * 100 for i in range(1, len(closes)) if closes[i-1]]
    vol = (sum(r*r for r in rets) / len(rets)) ** 0.5 if rets else 1.0

    # RSI proxy (14h)
    gains = [max(r, 0) for r in rets[-14:]]
    losses = [abs(min(r, 0)) for r in rets[-14:]]
    avg_g = sum(gains) / 14 if gains else 0
    avg_l = sum(losses) / 14 if losses else 0.001
    rsi = 100 - 100 / (1 + avg_g / avg_l) if avg_l else 50

    # Distance from 24h high/low (breakout signal)
    hi = max(closes)
    lo = min(closes)
    rng = hi - lo if hi != lo else 1
    pos_in_range = (now - lo) / rng  # 0 = at low, 1 = at high

    # Acceleration: is momentum speeding up?
    accel = chg_1h - (sum(rets[-4:-1]) / 3 if len(rets) >= 4 else 0)

    return {
        'chg_1h': chg_1h, 'chg_4h': chg_4h, 'chg_24h': chg_24h,
        'vol': max(vol, 0.3), 'rsi': rsi,
        'pos_in_range': pos_in_range, 'accel': accel, 'price': now,
    }

def score_intraday(f):
    """Score: positive = long, negative = short. Weighted intraday signals."""
    score = 0
    # Hourly momentum (strongest signal intraday)
    score += f['chg_1h'] * 3.0
    score += f['chg_4h'] * 1.5
    # RSI mean-reversion at extremes
    if f['rsi'] < 30: score += (30 - f['rsi']) * 0.5   # oversold bounce
    if f['rsi'] > 70: score -= (f['rsi'] - 70) * 0.5    # overbought fade
    # Breakout: near highs with momentum = continuation
    if f['pos_in_range'] > 0.9 and f['chg_1h'] > 0: score += 2.0
    if f['pos_in_range'] < 0.1 and f['chg_1h'] < 0: score -= 2.0
    # Acceleration confirms
    score += f['accel'] * 2.0
    # Don't fight the 24h trend too hard
    if (score > 0) != (f['chg_24h'] > 0):
        score *= 0.6
    return score

# ------------------------------------------- AUCTION MARKET BALANCE SCORE
# Auction Market Theory (Nic 2026-10-31 — "anticipate the spike before it
# happens"): markets alternate between BALANCE (both sides agree on price —
# rotational, choppy) and IMBALANCE (one side wins — directional, trending).
# The edge is the transition. A "coiled" setup = narrow recent range +
# steady volume + low directional efficiency = the readable pre-breakout
# state.
#
# DISPLAY ONLY. balance_score never feeds a buy gate, alert threshold, or
# verdict — changing what triggers a buy is Nic's decision, never the
# engine's. Missing/insufficient data fails open: returns (None, {}) and the
# asset ships balance_score: null, coiled: false.
BALANCE_WEIGHTS = {'compression': 0.45, 'efficiency': 0.30, 'volume': 0.25}
BALANCE_WEIGHTS_NOVOL = {'compression': 0.60, 'efficiency': 0.40}
COILED_THRESHOLD = 0.70
BALANCE_MIN_POINTS = 12  # need at least 12 hourly closes for a real read

def _clamp01(x):
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0

def balance_score(closes, volumes=None, closes_7d=None):
    """Auction-market balance score in [0,1].

    1.0 = tightly coiled balance (compressed range, steady volume, choppy
    rotation) — breakout watch. 0.0 = already in imbalance (trending /
    expanded range). Components:
      compression (0.45): 24h range / 7d range; a low ratio = coiled.
      efficiency  (0.30): |net 24h move| / path length of hourly moves;
                          low = rotational chop, high = directional.
      volume      (0.25): mean volume of the last 6h / mean 24h volume;
                          ~1 = steady (accumulation), collapsed = dead
                          market, not coiled.
    Without hourly volumes the weights renormalize to 0.60 / 0.40
    (documented in BALANCE_WEIGHTS_NOVOL).
    Dead-market veto: when hourly volume has collapsed (recent-6h mean <=
    30% of the 24h mean) the market is quiet, not coiled — the score is
    capped at 0.50 regardless of compression, so dead markets can never
    read as coiled.
    Returns (score, parts) or (None, {}) when data is insufficient —
    fail open, never a misleading number.
    """
    closes = [x for x in (closes or []) if x is not None]
    c7 = [x for x in (closes_7d or []) if x is not None]
    if len(closes) < BALANCE_MIN_POINTS or len(c7) < 48:
        return None, {}
    try:
        rng24 = max(closes) - min(closes)
        rng7 = max(c7) - min(c7)
        if rng7 <= 0:
            return None, {}
        comp = _clamp01(1.0 - (rng24 / rng7) / 0.5)
        diffs = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
        path = sum(diffs)
        net = abs(closes[-1] - closes[0])
        eff = _clamp01(1.0 - (net / path if path > 0 else 0.0))
        parts = {'compression': round(comp, 3), 'efficiency': round(eff, 3),
                 'volume': None}
        vols = [x for x in (volumes or []) if x is not None]
        vol_raw = None
        if len(vols) >= BALANCE_MIN_POINTS:
            base = sum(vols) / len(vols)
            recent = sum(vols[-6:]) / 6
            vratio = recent / base if base > 0 else 0.0
            vol_raw = _clamp01((vratio - 0.3) / 0.7)
            parts['volume'] = round(vol_raw, 3)
            w = BALANCE_WEIGHTS
            score = (w['compression'] * comp + w['efficiency'] * eff
                     + w['volume'] * vol_raw)
        else:
            w = BALANCE_WEIGHTS_NOVOL
            score = w['compression'] * comp + w['efficiency'] * eff
        score = _clamp01(score)
        if vol_raw == 0.0:
            # Dead-market veto: volume collapsed — quiet, not coiled.
            score = min(score, 0.5)
        return round(score, 3), parts
    except (TypeError, ValueError, ZeroDivisionError):
        return None, {}

def round_price(p):
    """Adaptive price precision — mirrors nexintel.py's round_price():
    sub-cent coins keep enough decimals that entry/target/stop stay
    distinct (a flat round(…, 4) collapses a $0.0024 coin's plan to
    target == entry == stop, which reads as 'no plan' downstream)."""
    if not p: return 0
    if p >= 100: return round(p, 2)
    if p >= 1: return round(p, 4)
    if p >= 0.01: return round(p, 6)
    return round(p, 8)

def _round_plan(entry, target, stop):
    """Round a plan's three levels with round_price(); if rounding still
    collapses them (values no longer distinct, or the target/stop sides
    flipped), fall back to 10 decimals so the order survives."""
    e, t, s = round_price(entry), round_price(target), round_price(stop)
    def _sign(x):
        return (x > 0) - (x < 0)
    if (len({e, t, s}) < 3
            or _sign(t - e) != _sign(target - entry)
            or _sign(s - e) != _sign(stop - entry)):
        e, t, s = round(entry, 10), round(target, 10), round(stop, 10)
    return e, t, s

def build_intraday_thesis(sym, f, score):
    direction = 'LONG' if score > 0 else 'SHORT'
    vol = f['vol']
    price = f['price']
    if not clears_fee_floor(vol):
        # Same fee gate as build_mover_plan: a sub-fee target is a
        # guaranteed net loser even when the direction call is right.
        _record_gate_rejection('FEE_FLOOR_GROSS')
        return None
    if direction == 'LONG':
        stop = price * (1 - vol / 100 * 0.8)
        target = price * (1 + vol / 100 * 1.2)
    else:
        stop = price * (1 + vol / 100 * 0.8)
        target = price * (1 - vol / 100 * 1.2)
    gate, order_type, order_note = _apply_net_gate(
        price, target, stop, direction, current_price=price)
    if gate is None:
        # Net gate (binding): sub-2.8%-net or required-sell miss — the
        # caller shows the fee note, same as the gross floor.
        return None
    entry_r, target_r, stop_r = _round_plan(price, target, stop)

    now = datetime.now(timezone.utc)
    # Intraday horizon: 2 hours typical
    from datetime import timedelta
    exit_time = now + timedelta(hours=2)

    exp_ret = abs(target - price) / price * 100
    max_loss = abs(price - stop) / price * 100

    bull, bear = [], []
    if f['chg_1h'] > 1: bull.append(f"+{f['chg_1h']:.1f}% last hour momentum")
    if f['chg_1h'] < -1: bear.append(f"{f['chg_1h']:.1f}% last hour drop")
    if f['rsi'] < 30: bull.append(f"RSI {f['rsi']:.0f} — oversold bounce setup")
    if f['rsi'] > 70: bear.append(f"RSI {f['rsi']:.0f} — overbought, fade setup")
    if f['pos_in_range'] > 0.9: bull.append("breaking 24h highs")
    if f['pos_in_range'] < 0.1: bear.append("breaking 24h lows")

    return {
        'direction': direction,
        'entry': entry_r,
        'stop_loss': stop_r,
        'take_profit': target_r,
        'entry_time': now.strftime('%H:%M UTC'),
        'exit_by': exit_time.strftime('%H:%M UTC'),
        'horizon': '1-4 hours',
        'expected_return_pct': round(exp_ret, 2),
        'max_loss_pct': round(max_loss, 2),
        'order_type': order_type,
        'order_note': order_note,
        'net_return_pct': round(gate['net_return'] * 100, 2),
        'rsi': round(f['rsi'], 0),
        'chg_1h': round(f['chg_1h'], 2),
        'bull_case': bull or ['intraday momentum alignment'],
        'bear_case': bear or ['intraday momentum divergence'],
    }

def sparkline_vol(prices, points=48):
    """Hourly volatility (%) from a CoinGecko sparkline price series —
    the same RMS-of-hourly-returns math as hourly_features, floored at
    0.3. Uses the last `points` prices (~48h of hourly points). Returns
    None when the series is missing or too short to trust."""
    if not prices:
        return None
    closes = [p for p in prices[-points:] if p]
    if len(closes) < 5:
        return None
    rets = [(closes[i] - closes[i-1]) / closes[i-1] * 100
            for i in range(1, len(closes)) if closes[i-1]]
    if not rets:
        return None
    vol = (sum(r * r for r in rets) / len(rets)) ** 0.5
    return max(vol, 0.3)

def sparkline_rsi(prices, period=14):
    """Hourly RSI(14) from a CoinGecko sparkline price series (168 hourly
    points in sparkline_in_7d). Same Wilder's RSI math as hourly_features.
    Returns None when the series is too short to trust (needs period + 1
    closes)."""
    if not prices:
        return None
    closes = [p for p in prices[-(period + 1):] if p]
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    rsi = 100 - 100 / (1 + avg_g / avg_l) if avg_l else 50
    return rsi

def sparkline_vol_map(universe):
    """{coin_id: hourly vol %} from each coin's sparkline embedded in the
    markets payload, plus the universe median — the fallback for coins
    whose own sparkline is missing or too short. Returns ({}, None) when
    no coin in the universe carries a usable sparkline."""
    vol_by_id = {}
    for c in universe:
        v = sparkline_vol((c.get('sparkline_in_7d') or {}).get('price'))
        if v:
            vol_by_id[c.get('id')] = v
    vols = sorted(vol_by_id.values())
    median = vols[len(vols) // 2] if vols else None
    return vol_by_id, median

# --- Fee-aware plan floor (Nic 2026-11-01: a +1.02% gross winner netted
# -$1.90 after $6.96 round-trip fees on ~$500 — 1.4% cost drag).
# No plan is emitted whose gross target can't clear twice the round-trip
# cost (his 2:1 doctrine, applied to the certain cost). Measured from his
# fills: 0.9% taker entry + 0.5% maker exit on Coinbase Advanced (US intro
# tier, post-Sept-2026 schedule). If he commits to maker-only entries,
# drop FEE_ROUNDTRIP_PCT to 1.0.
FEE_ROUNDTRIP_PCT = 1.4
FEE_TARGET_MULT = 2.0
FEE_TARGET_FLOOR_PCT = FEE_ROUNDTRIP_PCT * FEE_TARGET_MULT  # 2.8

def clears_fee_floor(vol):
    """True when the plan's gross target (vol x 1.2) clears the fee floor.

    vol is hourly RMS %; both plan builders target vol x 1.2. A plan that
    can't gross twice its round-trip cost is a guaranteed net loser even
    when the direction call is right — so it is never emitted."""
    return vol is not None and vol * 1.2 >= FEE_TARGET_FLOOR_PCT

# --- Runner / stretch target (Nic 2026-10-29: HYPE printed its +1.6% target
# then ran to +6% — "a little quick on the sell"). When hourly momentum is
# strong and the coin is NOT overbought, the plan carries a second, farther
# target: sell half at the base target, ride the rest to the runner. The base
# target stays the graded one (Proof Book scores against take_profit); the
# runner is a bonus line, shown only when the tape supports it.
STRETCH_1H_PCT = 1.5    # hourly momentum strong enough to support a runner
STRETCH_24H_PCT = 10     # already a genuine mover, not a twitch
STRETCH_RSI_CAP = 70     # not overbought — no runner into an RSI ceiling
STRETCH_VOL_MULT = 2.0   # runner sits one more vol-step past the base target

def build_mover_plan(price, vol, chg_1h=None, chg_24h=None, rsi=None,
                     funding_score=None, funding_ann_pct=None,
                     oi_score=None, oi_chg_1h_pct=None):
    """LONG trade plan for an UP mover, using the same hourly-vol math as
    build_intraday_thesis (stop = entry x (1 - vol x 0.6), target = entry x
    (1 + vol x 1.2) — 2:1 reward:risk; Nic 2026-10-30: 1.5:1 was too much
    risk for the reward). Volatility comes from the markets payload's sparkline
    (sparkline_vol_map; the caller applies the universe-median fallback) —
    never a per-coin chart fetch: CoinGecko's free tier throttles those,
    and on 2026-10-20 every UP mover plan silently died that way. Returns
    None when no volatility is available from anywhere, so the caller can
    show the honest hiccup note instead of a fabricated plan.

    Runner: when chg_1h/chg_24h/rsi are passed and the stretch gate holds
    (hourly momentum >= STRETCH_1H_PCT, 24h >= STRETCH_24H_PCT, RSI below
    STRETCH_RSI_CAP — a missing RSI fails open, the momentum gates still
    bind), the plan also carries stretch_target = entry x (1 + vol x 2.0).
    The base take_profit remains the graded target; the runner is bonus.

    Funding gate (Feed #2): when funding_score is passed and the longs are
    crowded past the suppression line, the runner is withheld — no riding
    into a crowded-long ceiling. OI gate (Feed #3): when oi_score is passed
    and open-interest flow is strongly adverse, the runner is withheld too.
    The plan records stretch_suppressed=True with an explanatory
    stretch_note so the card can say why. Missing feed data fails open
    (runner allowed; momentum gates still bind)."""
    from datetime import timedelta
    if not price or not vol:
        return None
    if not clears_fee_floor(vol):
        # Target (vol x 1.2) can't clear twice the round-trip fee — emitting
        # it would be a guaranteed net loser. Caller shows the fee note.
        _record_gate_rejection('FEE_FLOOR_GROSS')
        return None
    entry = price
    stop = entry * (1 - vol / 100 * 0.6)
    target = entry * (1 + vol / 100 * 1.2)
    gate, order_type, order_note = _apply_net_gate(
        entry, target, stop, 'LONG', current_price=price)
    if gate is None:
        # Net gate (binding): sub-2.8%-net or required-sell miss.
        return None
    exit_time = datetime.now(timezone.utc) + timedelta(hours=3)
    # Adaptive precision (round_price, as in the swing engine) so sub-cent
    # plans keep entry < target / entry > stop visibly distinct.
    entry_r, target_r, stop_r = _round_plan(entry, target, stop)
    plan = {
        'entry': entry_r,
        'take_profit': target_r,
        'stop_loss': stop_r,
        'exit_by': exit_time.strftime('%H:%M UTC'),
        'horizon': '1-4 hours',
        'expected_return_pct': round(abs(target - entry) / entry * 100, 2),
        'max_loss_pct': round(abs(entry - stop) / entry * 100, 2),
        'order_type': order_type,
        'order_note': order_note,
        'net_return_pct': round(gate['net_return'] * 100, 2),
    }
    # Runner: only when the tape supports reaching higher.
    momentum_ok = (chg_1h is not None and chg_1h >= STRETCH_1H_PCT
                   and chg_24h is not None and chg_24h >= STRETCH_24H_PCT)
    not_overbought = rsi is None or rsi < STRETCH_RSI_CAP
    if funding_feed is not None:
        funding_ok = funding_feed.runner_allowed(funding_score)
    else:
        funding_ok = True  # feed unavailable — fail open
    if oi_flow is not None:
        oi_ok = oi_flow.runner_ok(oi_score)
    else:
        oi_ok = True  # feed unavailable — fail open
    if momentum_ok and not_overbought and funding_ok and oi_ok:
        stretch = entry * (1 + vol / 100 * STRETCH_VOL_MULT)
        _, stretch_r, _ = _round_plan(entry, stretch, stop)
        # Guard: the runner must sit strictly above the base target.
        if stretch_r > target_r:
            plan['stretch_target'] = stretch_r
            plan['stretch_return_pct'] = round(
                abs(stretch - entry) / entry * 100, 2)
            bits = [f"+{chg_1h:.1f}% last hour"]
            if rsi is not None:
                bits.append(f"RSI {rsi:.0f}")
            plan['stretch_note'] = (", ".join(bits)
                                    + " — momentum supports a runner")
    elif momentum_ok and not_overbought and not (funding_ok and oi_ok):
        # The tape wanted a runner but positioning says don't ride it.
        blocks = []
        if not funding_ok:
            ann = (f"{funding_ann_pct:+.0f}%/yr"
                   if funding_ann_pct is not None else "high")
            blocks.append(f"crowded longs (funding {ann})")
        if not oi_ok:
            ox = (f"{oi_chg_1h_pct:+.0f}%/1h"
                  if oi_chg_1h_pct is not None else "adverse")
            blocks.append(f"adverse OI flow (OI {ox})")
        plan['stretch_suppressed'] = True
        plan['stretch_note'] = "; ".join(blocks)
    return plan

def build_accum_plan(price, vol, range_high=None):
    """Trigger plan for a setting-up (accumulation) entry — the levels a
    resting limit buy needs BEFORE the move starts (Nic 2026-10-21: "I
    need to be before the jump up"). Same hourly-vol machinery as mover
    plans: target = trigger x (1 + vol x 1.2), stop = trigger x
    (1 - vol x 0.6) — 2:1 reward:risk (Nic 2026-10-30), exit +3h like mover plans.

    Trigger: the recent range high (24h high) when it sits within 5%
    above spot — a genuine near-term breakout level — otherwise spot x
    (1 + 0.5 x hourly vol), one half-vol step above spot as the
    confirmation level. Returns None when no volatility is available
    from anywhere, so the caller ships plan: null and the alert falls
    back to a WATCH card instead of invented numbers. Also returns None
    when the gross target (vol x 1.2) can't clear the fee floor — same
    gate as thesis/mover plans (Nic 2026-11-01: a sub-fee target is a
    guaranteed net loser even when the direction call is right)."""
    from datetime import timedelta
    if not price or not vol:
        return None
    if not clears_fee_floor(vol):
        # Target (vol x 1.2) can't clear twice the round-trip fee —
        # emitting it would be a guaranteed net loser. Caller shows WATCH.
        _record_gate_rejection('FEE_FLOOR_GROSS')
        return None
    if range_high and price < range_high <= price * 1.05:
        trigger = range_high
    else:
        trigger = price * (1 + vol / 100 * 0.5)
    target = trigger * (1 + vol / 100 * 1.2)
    stop = trigger * (1 - vol / 100 * 0.6)
    gate, order_type, order_note = _apply_net_gate(
        trigger, target, stop, 'LONG', current_price=price)
    if gate is None:
        # Net gate (binding): sub-2.8%-net or required-sell miss — the
        # trigger sits above spot, so this labels STOP_BUY when it passes.
        return None
    exit_time = datetime.now(timezone.utc) + timedelta(hours=3)
    trigger_r, target_r, stop_r = _round_plan(trigger, target, stop)
    return {
        'trigger_price': trigger_r,
        'entry': trigger_r,  # a fill happens at the trigger
        'take_profit': target_r,
        'stop_loss': stop_r,
        'exit_by': exit_time.strftime('%H:%M UTC'),
        'horizon': 'pre-move setup',
        'expected_return_pct': round(abs(target - trigger) / trigger * 100, 2),
        'max_loss_pct': round(abs(trigger - stop) / trigger * 100, 2),
        'order_type': order_type,
        'order_note': order_note,
        'net_return_pct': round(gate['net_return'] * 100, 2),
    }

def _sig(v):
    """Round to 6 significant digits — keeps accum_history payloads lean."""
    return float(f'{v:.6g}') if v else 0

def accum_reason(hist, vol_ratio, vol_q90, chg_1h, now):
    """Pre-move accumulation detector (pure function — unit-tested).
    hist = stored observations for one coin INCLUDING the current run's
    observation as the last element. Needs >= 3 observations spanning
    >= 2 hours; returns (reason, vol_growth_pct, price_drift_pct) or None.
    Patterns:
      A) Quiet volume build — 24h volume up >= 20% vs the observation ~3h
         ago while price over the same span stayed within +/-2.5%.
      B) Ratio climb — vol_ratio in the top 10% of the universe AND above
         the previous run's value, with |chg_1h| < 2% (price still calm).
    """
    if len(hist) < 3:
        return None
    def ts(o):
        return datetime.fromisoformat(o['t'])
    if (ts(hist[-1]) - ts(hist[0])).total_seconds() / 3600 < 2:
        return None
    cur = hist[-1]
    # Reference observation ~3h back: oldest candidates are >= 2h old,
    # pick the one whose age is closest to 3h.
    cands = [o for o in hist[:-1] if (ts(cur) - ts(o)).total_seconds() / 3600 >= 2]
    if not cands:
        return None
    ref = min(cands, key=lambda o: abs((ts(cur) - ts(o)).total_seconds() / 3600 - 3))
    ref_age_h = (ts(cur) - ts(ref)).total_seconds() / 3600
    growth = drift = None
    if ref.get('vol24h'):
        growth = (cur['vol24h'] - ref['vol24h']) / ref['vol24h'] * 100
        drift = (cur['price'] - ref['price']) / ref['price'] * 100 if ref.get('price') else 0
        if growth >= 20 and abs(drift) <= 2.5:
            return (f"Volume building {growth:+.0f}% in {ref_age_h:.0f}h, price still quiet"
                    " — accumulation pattern", round(growth, 1), round(drift, 2))
    prev = hist[-2]
    if vol_ratio >= vol_q90 and vol_ratio > (prev.get('vol_ratio') or 0) and abs(chg_1h) < 2:
        return ("Volume in top 10% for its size and still climbing, price quiet"
                " — accumulation pattern",
                round(growth, 1) if growth is not None else 0.0,
                round(drift, 2) if drift is not None else 0.0)
    return None

def strong_day(chg24, chg1, vol_ratio, vol_med):
    """Strong-day watch predicate (pure function — unit-tested): a coin
    grinding >= 8% over 24h in steps — no single explosive hour — counts
    as setting up IF the current hour is still moving the same direction
    (>= +0.5% / <= -0.5%) AND its volume ratio is at least the universe
    median. Case study: CVX +9.24% 24h on 2026-10-20 tripped no tier."""
    if chg24 >= 8:
        return chg1 >= 0.5 and vol_ratio >= vol_med
    if chg24 <= -8:
        return chg1 <= -0.5 and vol_ratio >= vol_med
    return False

# ---------------------------------------------------------------------------
# 📒 Proof Book — a public paper-trading ledger of every intraday call.
#
# Persistence route: the intraday workflow commits ONLY data/intraday.json
# (see .github/workflows/intraday-loop.yml, `git add data/intraday.json`),
# so a separate data/proof.json would never survive a production run. The
# ledger therefore rides inside data/intraday.json under the `proof` key —
# the same precedent as accum_history above. The Reports page reads it
# from there.
#
# Conventions: every call is graded as if $1,000 rode it (paper money).
# A call that touches its target first is 'won' (+expected_return_pct);
# one that touches its stop first is 'lost' (-max_loss_pct) — EXCEPT a
# stop trailed to breakeven or better: the Oct 30 ride-the-win trail
# can put the stop past entry (max_loss_pct <= 0), so a stop touch
# there locks a profit (or a scratch) and grades 'won' at the locked-in
# result. The W/L label must agree with the P&L column: a profitable
# exit is never a loss. One that reaches its exit time + 2h grace
# untouched is 'expired' and graded at the actual price drift. Anything
# unresolved after 7 days expires too.
# Grading walks the hourly price path SINCE ENTRY (main() tops up chart
# fetches so every open call's asset has one, not just the top-50 signal
# universe); first touch in time order decides. If no chart is
# available, the fallback is extremes actually observed run over run
# (current-price snapshots accumulated on the entry) — the markets
# payload's trailing-24h high/low is trusted ONLY once a call is >= 24h
# old, because before that the window mostly predates the call (a local
# end-to-end test graded 17/17 fresh calls instantly against pre-entry
# movement — exactly what a proof book must never do). Where both levels
# are crossed inside one observation gap, order is unknowable and the
# call is graded a loss: the book never credits an ambiguous win.
# Simplification: the hourly path is hourly closes, so a spike that
# fully reverted inside a single hour can be missed.
#
# Scenario cells (2026-10-21): every record is tagged type x direction
# x confidence band x regime at entry; proof_cells reports win/loss
# per cell, all-time, by merging the visible ledger with `cumulative`
# — per-cell counters stored in this same payload that absorb graded
# entries when the 100-record cap trims them, so the cell record
# never stops growing. Setting-up (accum) entries carrying a trigger
# plan record as kind 'setup': a setup whose trigger never fills
# inside its window expires at result 0 (never a loss — no position
# was taken); once triggered it grades like a LONG call entered at
# the trigger.
# ---------------------------------------------------------------------------
PAPER_USD = 1000

def _iso_parse(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None

# --- Scenario cells -------------------------------------------------------
# Every Proof Book record is tagged into a scenario cell:
#   type (signal / mover / setup) x direction x confidence band x regime.
# Win/loss is reported PER CELL (proof_cells) so the daily review can see
# WHICH scenario families actually pay — the aggregate win rate hides
# exactly the thing Nic asked to improve (2026-10-21: "running as many
# scenarios as possible to bring the win/loss ratio higher"). Live alerts
# are never changed by this build; cells only feed the review's
# recommendation log.
def conf_band(conf):
    """Confidence band label for a 0-100 confidence value. Movers and
    setups carry no confidence — their band is 'na'."""
    if conf is None:
        return 'na'
    if conf < 50: return '40s'
    if conf < 60: return '50s'
    if conf < 70: return '60s'
    if conf < 80: return '70s'
    return '80+'

# Swing-engine regime tokens -> Proof Book cell tags (lowercase). The
# DISPLAY labels live in nexintel.REGIME_LABELS and are applied at
# render time — raw codes never reach the page (site language rule).
PROOF_REGIME_TAGS = {'RISK_ON': 'risk_on', 'NEUTRAL': 'neutral',
                     'RISK_OFF': 'risk_off'}

def current_regime_tag(now):
    """The market regime in force, read from the swing engine's
    data/latest.json (published every ~6h): the intraday engine keeps
    no regime model of its own, so a new scenario is tagged with the
    swing regime at entry. 'unknown' when the file is missing or
    older than 12h."""
    try:
        with open(os.path.join(DATA_DIR, 'latest.json')) as f:
            latest = json.load(f)
        gen = _iso_parse(latest.get('generated') or '')
        if gen and (now - gen).total_seconds() <= 12 * 3600:
            return PROOF_REGIME_TAGS.get(latest.get('regime'), 'unknown')
    except Exception:
        pass
    return 'unknown'

def proof_cell_key(e):
    """(type, direction, band, regime) for a ledger entry. Legacy
    entries predate the tags: missing band reads 'na', missing regime
    reads 'unknown' — never guessed after the fact."""
    return (e.get('kind') or 'signal',
            e.get('direction') or 'LONG',
            e.get('band') or 'na',
            e.get('regime') or 'unknown')

def _proof_candidate(kind, asset, name, direction, plan, now, horizon_h,
                     tags=None, gate_src=None, extra=None):
    """Normalize a signal thesis / mover plan / accum trigger plan into
    a ledger entry. horizon_h matches the builders: signals exit at +2h
    (build_intraday_thesis), mover and accum plans at +3h
    (build_mover_plan / build_accum_plan). `tags` carries the scenario
    cell tags ({'band': ..., 'regime': ...}) stamped at entry.
    `gate_src` carries the source object's own gate inputs at this
    moment ({confidence, tradable, news_flag, chg_1h, chg_24h} —
    whichever the family really has) for the shadow codeword gate;
    the gate is measurement only and never feeds back into the
    candidate itself. `extra` stamps research-track fields (pattern
    kinds carry {'pattern'/'patterns', 'horizon': '6h'/'24h'}); the
    horizon also joins the entry id so a 6h and a 24h candidate for
    the same asset in the same run stay distinct entries."""
    from datetime import timedelta
    if not plan:
        return None
    try:
        entry = float(plan.get('entry'))
        tp = float(plan.get('take_profit'))
        sl = float(plan.get('stop_loss'))
    except (TypeError, ValueError):
        return None
    if not entry or entry <= 0:
        return None
    exp_ret = plan.get('expected_return_pct')
    if exp_ret is None:
        exp_ret = round(abs(tp - entry) / entry * 100, 2)
    max_loss = plan.get('max_loss_pct')
    if max_loss is None:
        max_loss = round(abs(entry - sl) / entry * 100, 2)
    entry_id = f'{kind}:{asset}:{int(now.timestamp())}'
    if extra and extra.get('horizon'):
        entry_id += f":{extra['horizon']}"
    if extra and extra.get('variable'):
        entry_id += f":{extra['variable']}"
    if extra and extra.get('cell'):
        # CMI regime rows: MANIP grades as both a LONG cell and a
        # SHORT-bias cell for the same asset+horizon — the cell joins
        # the id so the two rows stay distinct entries.
        entry_id += f":{extra['cell']}"
    entry_out = {
        'id': entry_id,
        'ts_open': now.isoformat(),
        'kind': kind,
        'asset': asset,
        'name': name or asset,
        'direction': direction or 'LONG',
        'entry': entry,
        'take_profit': tp,
        'stop_loss': sl,
        'exit_by': (now + timedelta(hours=horizon_h)).isoformat(),
        'expected_return_pct': exp_ret,
        'max_loss_pct': max_loss,
        'status': 'open',
        'result_pct': None,
        'paper_pnl': None,
        'ts_closed': None,
        'last_price': entry,
    }
    tags = tags or {}
    entry_out['band'] = tags.get('band') or 'na'
    entry_out['regime'] = tags.get('regime') or 'unknown'
    if extra:
        # Research-track fields only (pattern kinds). Plain dict merge
        # — never overwrites the core fields above (no shared keys).
        for k, v in extra.items():
            entry_out.setdefault(k, v)
    if kind == 'setup':
        # A setup is a resting limit buy at the trigger: entry IS the
        # trigger price, and grading must know whether price ever got
        # there (see _proof_grade_setup).
        entry_out['trigger_price'] = plan.get('trigger_price') or entry
        entry_out['triggered'] = False
        entry_out['never_triggered'] = False
    src = gate_src or {}
    entry_out['gate'] = proof_gate(
        kind, entry_out['direction'], entry, tp, sl,
        entry_out['regime'],
        confidence=src.get('confidence'), band=entry_out.get('band'),
        tradable=src.get('tradable'), news_flag=src.get('news_flag'),
        chg_1h=src.get('chg_1h'), chg_24h=src.get('chg_24h'))
    return entry_out

def proof_record_new(record, candidates, now):
    """Prepend fresh calls to the ledger (kept newest-first). Dedup: the
    same asset+kind is never re-recorded while an entry is still open,
    nor within 24h of a previous entry — movers re-fire every hour and
    must not flood the book. Pattern kinds carry two horizons per
    firing (6h and 24h), so the dedup key includes the horizon: a 6h
    and a 24h candidate for the same asset are distinct entries."""
    fresh = []
    for cand in candidates:
        if not cand:
            continue
        dup = False
        for e in record:
            if e.get('asset') != cand['asset'] or e.get('kind') != cand['kind']:
                continue
            if (e.get('horizon') or '') != (cand.get('horizon') or ''):
                continue
            # Cycle rows carry a variable: a halving row and a month row
            # for the same asset+kind+horizon are distinct entries.
            if (e.get('variable') or '') != (cand.get('variable') or ''):
                continue
            if e.get('status') == 'open':
                dup = True
                break
            opened = _iso_parse(e.get('ts_open') or '')
            if opened and (now - opened).total_seconds() < 24 * 3600:
                dup = True
                break
        if not dup:
            fresh.append(cand)
    record[:] = fresh + record
    return len(fresh)

def _proof_grade_setup(e, mkt, charts_by_sym, now, opened, cur):
    """Grade one setup (accumulation trigger) entry — a resting limit
    buy at the trigger, LONG-only. Returns (status, result_pct) or
    (None, None) to stay open.

    Phase 1 — not triggered: if the plan window closes (exit_by + 2h
    grace, the same convention as every other call; 7-day hard cap)
    with price never reaching the trigger, the scenario EXPIRES with
    result 0 and never_triggered set. No position was ever taken, so
    it is never a loss — 'expired' is its own bucket and stays out of
    the win-rate denominator.

    Phase 2 — triggered: from the first observation at/above the
    trigger, target/stop first-touch decides, exactly like a LONG call
    entered at the trigger; window end grades at the exit drift from
    the trigger. Once triggered, only observations at/after the
    trigger count — a pre-trigger dip can never grade as a stop-out
    (the stop sits between spot and trigger by construction)."""
    from datetime import timedelta
    tp, sl = e['take_profit'], e['stop_loss']
    trigger = e.get('trigger_price') or e['entry']
    exit_by = _iso_parse(e.get('exit_by') or '')
    window_over = bool(exit_by and now > exit_by + timedelta(hours=2)) or \
        bool(opened and (now - opened).total_seconds() > 7 * 86400)
    pts = []
    chart = charts_by_sym.get(e['asset'])
    if chart and opened:
        opened_ms = opened.timestamp() * 1000
        pts = [pr for ts, pr in chart if ts >= opened_ms]
        if e.get('triggered') and e.get('ts_triggered'):
            trig = _iso_parse(e['ts_triggered'])
            if trig:
                trig_ms = trig.timestamp() * 1000
                pts = [pr for ts, pr in chart if ts >= trig_ms]
    series = pts + ([cur] if cur else [])
    if not e.get('triggered'):
        if series:
            hit_at = next((i for i, p in enumerate(series) if p >= trigger), None)
            if hit_at is None:
                if window_over:
                    e['never_triggered'] = True
                    return 'expired', 0.0
                return None, None
            e['triggered'] = True
            e['ts_triggered'] = now.isoformat()
            series = series[hit_at:]
        else:
            # No price path at all: extremes actually observed since
            # open (same trust rules as the main grader).
            if not cur:
                return None, None
            hi = max(e.get('obs_hi') or cur, cur)
            lo = min(e.get('obs_lo') or cur, cur)
            if opened and (now - opened).total_seconds() >= 24 * 3600:
                if mkt.get('high_24h') is not None:
                    hi = max(hi, mkt['high_24h'])
                if mkt.get('low_24h') is not None:
                    lo = min(lo, mkt['low_24h'])
            e['obs_hi'], e['obs_lo'] = hi, lo
            if hi < trigger:
                if window_over:
                    e['never_triggered'] = True
                    return 'expired', 0.0
                return None, None
            e['triggered'] = True
            e['ts_triggered'] = now.isoformat()
            # Only the current observation is provably post-trigger —
            # restart the extremes here so pre-trigger dips can never
            # grade as a stop-out.
            e['obs_hi'] = e['obs_lo'] = cur
            series = [cur]
    # Triggered: first touch of target / stop from the trigger onward.
    for p in series:
        if p >= tp:
            return 'won', e['expected_return_pct']
        if p <= sl:
            return 'lost', -e['max_loss_pct']
    if not pts and cur:
        # Chart-less run: extremes observed since the trigger (reset
        # at flip time); both crossed in the gap = conservative loss,
        # the same rule as the main grader.
        hi = max(e.get('obs_hi') or cur, cur)
        lo = min(e.get('obs_lo') or cur, cur)
        e['obs_hi'], e['obs_lo'] = hi, lo
        if hi >= tp and lo <= sl:
            return 'lost', -e['max_loss_pct']
        if hi >= tp:
            return 'won', e['expected_return_pct']
        if lo <= sl:
            return 'lost', -e['max_loss_pct']
    if window_over:
        last = series[-1] if series else (e.get('last_price') or trigger)
        return 'expired', (last - trigger) / trigger * 100
    return None, None

# --- Price-action pattern track, Phase 2 (shadow grading, spec §4) ---
# A pattern candidate is a fixed-horizon, cost-aware paper bet: entry =
# the price at detection, direction per the pattern, two fixed horizons
# (6h and 24h). WIN if the +4.2% gross level is touched before the
# -2.1% adverse level inside the horizon (4.2% = 2.8% net target +
# 1.4% round-trip costs); LOSS if the adverse level hits first.
# Dollars ($1,000 paper, matching book convention): WIN -> +$28
# (2.8% net), LOSS -> -$35 (2.1% adverse + 1.4% costs).
# EXPIRED (neither level hit by the horizon end, exit_by + 2h grace —
# the same window convention as every other call, and the same
# projected-exit discipline as the Nov 5 swing grading-horizon fix):
# graded at the horizon close — a WIN iff the direction-signed drift
# covers costs (LONG: close >= entry x 1.014), else a LOSS, with
# result_pct = drift_pct - 1.4, i.e. $1,000 x (close/entry - 1 - 0.014).
# The entry carries pattern_expired=True so the horizon close stays
# distinguishable from a target/stop exit in the record. The ride-the-
# win trail logic of the main grader does NOT apply to pattern entries
# (their levels are the spec's fixed test, not a live plan).
# Grading walks hourly closes since entry (the same simplification as
# the main grader: an intra-hour spike that fully reverts is missed).
PATTERN_WIN_GROSS_PCT = 4.2
PATTERN_ADVERSE_PCT = 2.1
PATTERN_COST_PCT = 1.4
PATTERN_KINDS = ('pattern', 'pattern_confluence')
PATTERN_HORIZONS_H = (6, 24)


def _fixed_level_grade(e, charts_by_sym, now, opened, cur, max_age_days,
                       expired_flag):
    """Shared fixed-level grader for the research tracks (pattern and
    cycle — both spec §4): WIN if the +4.2% gross level is touched
    before the -2.1% adverse level inside the horizon; LOSS if the
    adverse level hits first; EXPIRED (neither level hit by exit_by +
    2h grace, or the `max_age_days` hard cap) grades at the horizon
    close — a WIN iff the direction-signed drift covers the 1.4%
    costs. The entry carries `{expired_flag}=True` so the horizon
    close stays distinguishable from a target/stop exit. Returns
    (status, result_pct) or (None, None) to stay open. Never raises."""
    from datetime import timedelta
    try:
        entry = float(e['entry'])
        is_long = e.get('direction') != 'SHORT'
        if not entry or entry <= 0:
            return None, None
        win_px = entry * (1 + PATTERN_WIN_GROSS_PCT / 100) if is_long \
            else entry * (1 - PATTERN_WIN_GROSS_PCT / 100)
        adv_px = entry * (1 - PATTERN_ADVERSE_PCT / 100) if is_long \
            else entry * (1 + PATTERN_ADVERSE_PCT / 100)
        pts = []
        chart = charts_by_sym.get(e['asset'])
        if chart and opened:
            opened_ms = opened.timestamp() * 1000
            pts = [pr for ts, pr in chart if ts >= opened_ms]
        series = pts + ([cur] if cur else [])
        for p in series:
            if is_long:
                if p >= win_px:
                    return 'won', PATTERN_WIN_GROSS_PCT - PATTERN_COST_PCT
                if p <= adv_px:
                    return 'lost', -(PATTERN_ADVERSE_PCT + PATTERN_COST_PCT)
            else:
                if p <= win_px:
                    return 'won', PATTERN_WIN_GROSS_PCT - PATTERN_COST_PCT
                if p >= adv_px:
                    return 'lost', -(PATTERN_ADVERSE_PCT + PATTERN_COST_PCT)
        exit_by = _iso_parse(e.get('exit_by') or '')
        window_over = bool(exit_by and now > exit_by + timedelta(hours=2)) or \
            bool(opened and (now - opened).total_seconds() > max_age_days * 86400)
        if not window_over:
            return None, None
        last = series[-1] if series else (e.get('last_price') or entry)
        drift = ((last - entry) / entry * 100) if is_long \
            else ((entry - last) / entry * 100)
        result = round(drift - PATTERN_COST_PCT, 2)
        e[expired_flag] = True
        return ('won' if result >= 0 else 'lost'), result
    except Exception:
        return None, None


def _proof_grade_pattern(e, charts_by_sym, now, opened, cur):
    """Grade one pattern / pattern_confluence entry. Returns
    (status, result_pct) or (None, None) to stay open. Never raises."""
    return _fixed_level_grade(e, charts_by_sym, now, opened, cur, 7,
                              'pattern_expired')


def _proof_grade_cycle(e, charts_by_sym, now, opened, cur):
    """Grade one cycle entry (same fixed levels as the pattern track,
    spec §4). The hard cap is 8 days, not 7: a 7d-horizon entry must
    survive until exit_by + 2h grace, which a 7-day cap would cut
    ~2h short. Returns (status, result_pct) or (None, None) to stay
    open. Never raises."""
    return _fixed_level_grade(e, charts_by_sym, now, opened, cur, 8,
                              'cycle_expired')


def _proof_pattern_plan(entry, direction):
    """The spec §4 plan for a pattern candidate: +4.2% gross target /
    -2.1% adverse stop, with the cost-aware paper outcomes baked in
    as expected_return_pct (+2.8 net) and max_loss_pct (-3.5)."""
    if direction == 'SHORT':
        return {'entry': entry,
                'take_profit': entry * (1 - PATTERN_WIN_GROSS_PCT / 100),
                'stop_loss': entry * (1 + PATTERN_ADVERSE_PCT / 100),
                'expected_return_pct': round(
                    PATTERN_WIN_GROSS_PCT - PATTERN_COST_PCT, 2),
                'max_loss_pct': round(
                    PATTERN_ADVERSE_PCT + PATTERN_COST_PCT, 2)}
    return {'entry': entry,
            'take_profit': entry * (1 + PATTERN_WIN_GROSS_PCT / 100),
            'stop_loss': entry * (1 - PATTERN_ADVERSE_PCT / 100),
            'expected_return_pct': round(
                PATTERN_WIN_GROSS_PCT - PATTERN_COST_PCT, 2),
            'max_loss_pct': round(
                PATTERN_ADVERSE_PCT + PATTERN_COST_PCT, 2)}


def proof_pattern_candidates(patterns_by_asset, mkt_by_sym, name_by_sym,
                            now, regime_tag):
    """Ledger candidates for one run's pattern firings. One candidate
    per (asset, direction, horizon): two horizons (6h, 24h) per firing.
    Confluence rule (spec §3 + §4): when >= 2 same-direction patterns
    fire on an asset, the firing is graded ONLY as a cluster
    (kind='pattern_confluence', tagged with the pattern set) — the
    single-pattern cells are never polluted by cluster membership.
    A lone firing grades as kind='pattern'. Returns [candidates]."""
    cands = []
    for asset, tags in (patterns_by_asset or {}).items():
        if not tags:
            continue
        mkt = (mkt_by_sym or {}).get(asset) or {}
        try:
            entry = float(mkt.get('price') or 0)
        except (TypeError, ValueError):
            entry = 0
        if not entry or entry <= 0:
            continue
        by_dir = {}
        for t in tags:
            by_dir.setdefault((t or {}).get('direction') or 'LONG',
                              []).append(t)
        for direction, dtags in sorted(by_dir.items()):
            pnames = sorted({(t or {}).get('pattern') or '?'
                             for t in dtags})
            if len(dtags) >= 2:
                kind = 'pattern_confluence'
                extra_base = {'pattern': '+'.join(pnames),
                              'patterns': pnames}
            else:
                kind = 'pattern'
                extra_base = {'pattern': pnames[0], 'patterns': pnames}
            for horizon_h in PATTERN_HORIZONS_H:
                extra = dict(extra_base)
                extra['horizon'] = f'{horizon_h}h'
                cand = _proof_candidate(
                    kind, asset, (name_by_sym or {}).get(asset) or asset,
                    direction, _proof_pattern_plan(entry, direction),
                    now, horizon_h,
                    tags={'band': 'na', 'regime': regime_tag},
                    gate_src={},
                    extra=extra)
                if cand:
                    cands.append(cand)
    return cands


def wilson_lower(wins, n, z=1.96):
    """Wilson score 95% CI lower bound on a win rate (fraction, not
    percent). Mirrors self_tune.wilson_interval — duplicated here so
    the intraday engine does not import the tuning module. None when
    n == 0. Recorded honestly in the scoreboard; per Nic (2026-11-05)
    the 95% belt is loosened, so it is NEVER a gate in Phase 1/2."""
    if not n:
        return None
    try:
        import math as _math
        p = wins / n
        denom = 1.0 + z * z / n
        center = (p + z * z / (2.0 * n)) / denom
        half = z * _math.sqrt(p * (1.0 - p) / n
                             + z * z / (4.0 * n * n)) / denom
        return max(0.0, center - half)
    except Exception:
        return None


def pattern_cell_key(e):
    """(pattern-or-cluster, direction, horizon) for a pattern ledger
    entry. The cluster key is the '+'-joined sorted pattern set."""
    names = e.get('patterns')
    if not names:
        names = [e.get('pattern')] if e.get('pattern') else ['?']
    return ('+'.join(sorted(names)),
            e.get('direction') or 'LONG',
            e.get('horizon') or '?')

def pattern_scoreboard(record, cumulative=None):
    """Per-pattern-cell scoreboard (spec §4 cells, §5 promotion bar,
    reported — never gated). Each row: pattern, direction, horizon,
    graded, wins, losses, expired-horizon closes, win rate, Wilson
    95% CI lower bound (percent), expectancy in $ per $1,000 paper
    (mean paper P&L), and max drawdown on a running $1,000 bankroll
    per cell (each graded outcome added in close order; the spec does
    not define the drawdown denominator, so this is the conservative
    display-only choice — Nic can redefine it). Drawdown is computed
    from the visible ledger only: cumulative rows carry no time
    series, so their drawdown is None and the row says so.
    Promotion flags (sample n >= 100, expectancy >= +$20) are reported
    as facts; promotion itself is Nic's call, never automatic."""
    cells = {}

    def bucket(key):
        return cells.setdefault(key, {'graded': 0, 'wins': 0, 'losses': 0,
                                      'expired': 0, 'paper_pnl_total': 0.0,
                                      'pnls': []})
    for e in record or []:
        if e.get('kind') not in PATTERN_KINDS:
            continue
        if e.get('status') not in ('won', 'lost', 'expired'):
            continue
        b = bucket(pattern_cell_key(e))
        b['graded'] += 1
        if e.get('status') == 'won':
            b['wins'] += 1
        elif e.get('status') == 'lost':
            b['losses'] += 1
        if e.get('pattern_expired'):
            b['expired'] += 1
        b['paper_pnl_total'] += e.get('paper_pnl') or 0
        b['pnls'].append((e.get('ts_closed') or '', e.get('paper_pnl') or 0))
    for key_str, c in (cumulative or {}).items():
        parts = key_str.split('|')
        if len(parts) != 3:
            continue
        b = bucket(tuple(parts))
        for f in ('graded', 'wins', 'losses', 'expired'):
            b[f] += c.get(f, 0)
        b['paper_pnl_total'] += c.get('paper_pnl_total', 0)
    rows = []
    for (pattern, direction, horizon), b in sorted(cells.items()):
        wl = b['wins'] + b['losses']
        lo = wilson_lower(b['wins'], wl)
        dd = None
        if b['pnls']:
            bankroll, peak, worst = 1000.0, 1000.0, 0.0
            for _, pnl in sorted(b['pnls']):
                bankroll += pnl
                peak = max(peak, bankroll)
                worst = max(worst, (peak - bankroll) / peak * 100
                            if peak > 0 else 0.0)
            dd = round(worst, 1)
        rows.append({
            'pattern': pattern, 'direction': direction, 'horizon': horizon,
            'graded': b['graded'], 'wins': b['wins'], 'losses': b['losses'],
            'expired': b['expired'],
            'win_rate': round(b['wins'] / wl * 100, 1) if wl else None,
            'wilson_lower_95': round(lo * 100, 1) if lo is not None else None,
            'expectancy_per_1000': round(b['paper_pnl_total'] / b['graded'], 2)
                                   if b['graded'] else None,
            'paper_pnl_total': round(b['paper_pnl_total'], 2),
            'max_drawdown_pct': dd,
            'drawdown_visible_only': True,
            'meets_sample_n100': b['graded'] >= 100,
            'meets_expectancy_20': (b['paper_pnl_total'] / b['graded'] >= 20
                                    if b['graded'] else False),
        })
    return rows


def proof_roll_pattern_cumulative(cumulative, dropped):
    """Fold closed pattern entries that are about to roll off the
    capped ledger into persistent per-pattern-cell counters (same
    shape as proof_roll_cumulative, keyed by pattern_cell_key). Open
    entries are never rolled."""
    out = {k: dict(v) for k, v in (cumulative or {}).items()}
    for e in dropped:
        if e.get('kind') not in PATTERN_KINDS:
            continue
        st = e.get('status')
        if st not in ('won', 'lost', 'expired'):
            continue
        key = '|'.join(pattern_cell_key(e))
        c = out.setdefault(key, {'graded': 0, 'wins': 0, 'losses': 0,
                                 'expired': 0, 'paper_pnl_total': 0.0})
        c['graded'] += 1
        c['wins' if st == 'won' else 'losses' if st == 'lost' else 'expired'] += 1
        if e.get('pattern_expired'):
            c['expired'] += 1
        c['paper_pnl_total'] = round(c['paper_pnl_total'] +
                                     (e.get('paper_pnl') or 0), 2)
    return out


# --- Cycle research track, Phase 2 (shadow grading, spec §4) ---
# A cycle candidate is a fixed-horizon, cost-aware paper bet: entry =
# the asset's close on the observation day, direction per spec §1
# (cycle_feed.direction_for), two fixed horizons (24h and 7d). Same
# levels as the pattern track: WIN if +4.2% gross is touched before
# -2.1% adverse; LOSS if the adverse level hits first; EXPIRED grades
# at the horizon close (win iff the drift covers the 1.4% costs).
# Dollars ($1,000 paper): WIN -> +$28, LOSS -> -$35.
# Cells: variable x band x horizon x direction. One asset-day
# contributes one row per variable (seven rows per asset-day) — cells
# are never mixed across variables. Rows live in a SEPARATE cycle
# ledger (proof.cycle_record, cap 2000), not the 100-entry main
# ledger: 14 rows per asset-day would flood it and evict live calls.
# The separate ledger is graded by the same proof_grade machinery.
# Universe is bounded (BTC first, then this run's card assets, cap 20
# — see CYCLE_MAX_ASSETS): the full scanned universe at 14 rows/day
# would still flood even the separate ledger.
# Base rates (spec §4): every cell is graded against the all-days
# base rate for its horizon AND its variable's control band
# (FNG_N for fear bands, DOM_FLAT for dominance). Variables without
# a control band grade against the all-days base rate alone.
CYCLE_KINDS = ('cycle',)
CYCLE_HORIZONS_H = (24, 168)   # labels '24h' and '7d'
CYCLE_MAX_ASSETS = 20          # BTC + card assets per run (see above)
CYCLE_RECORD_CAP = 2000        # separate cycle ledger cap


def proof_cycle_candidates(cycle_state, assets, mkt_by_sym, name_by_sym,
                           now, regime_tag):
    """Ledger candidates for one day's cycle state. One candidate per
    (asset, variable, horizon): seven variables x two horizons per
    asset-day, minus the cells direction_for() rules out (DOM_UP on
    altcoins, DOM_DOWN on BTC, any variable whose band is "no data").
    The plan is the spec §4 fixed-level plan (+4.2% / -2.1%,
    +$28 / -$35) — _proof_pattern_plan, shared with the pattern
    track. Returns [candidates]."""
    cands = []
    if not cycle_state:
        return cands
    variables = (cycle_feed.VARIABLES if cycle_feed is not None
                 else ('halving', 'month', 'dom', 'fng', 'dow', 'dd'))
    for asset in (assets or []):
        mkt = (mkt_by_sym or {}).get(asset) or {}
        try:
            entry = float(mkt.get('price') or 0)
        except (TypeError, ValueError):
            entry = 0
        if not entry or entry <= 0:
            continue
        for variable in variables:
            band = (cycle_feed.state_band(cycle_state, variable)
                    if cycle_feed is not None else None)
            if band is None:
                continue
            direction = (cycle_feed.direction_for(variable, band, asset)
                         if cycle_feed is not None else None)
            if direction is None:
                continue
            for horizon_h in CYCLE_HORIZONS_H:
                label = '24h' if horizon_h == 24 else '7d'
                cand = _proof_candidate(
                    'cycle', asset,
                    (name_by_sym or {}).get(asset) or asset,
                    direction, _proof_pattern_plan(entry, direction),
                    now, horizon_h,
                    tags={'band': band, 'regime': regime_tag},
                    gate_src={},
                    extra={'variable': variable, 'horizon': label})
                if cand:
                    cands.append(cand)
    return cands


def proof_record_cycle(record, candidates, now):
    """Prepend fresh cycle rows to the cycle ledger (newest-first).
    Dedup: the same asset+variable+horizon is never re-recorded
    within 24h of a previous entry. Unlike proof_record_new there is
    NO open-block: spec §4 wants one row per asset-day per variable,
    so overlapping 7d windows are intended — a 7d row opened
    yesterday does not block today's 7d row for the same cell."""
    fresh = []
    for cand in candidates:
        if not cand:
            continue
        dup = False
        for e in record:
            if e.get('asset') != cand['asset'] or e.get('kind') != cand['kind']:
                continue
            if (e.get('variable') or '') != (cand.get('variable') or ''):
                continue
            if (e.get('horizon') or '') != (cand.get('horizon') or ''):
                continue
            opened = _iso_parse(e.get('ts_open') or '')
            if opened and (now - opened).total_seconds() < 24 * 3600:
                dup = True
                break
        if not dup:
            fresh.append(cand)
    record[:] = fresh + record
    return len(fresh)


def cycle_cell_key(e):
    """(variable, band, direction, horizon) for a cycle ledger entry."""
    return (e.get('variable') or '?', e.get('band') or 'na',
            e.get('direction') or 'LONG', e.get('horizon') or '?')


def cycle_scoreboard(record, cumulative=None):
    """Per-cycle-cell scoreboard (spec §4 cells, §5 promotion bar,
    reported — never gated). Each row: variable, band, direction,
    horizon, graded, wins, losses, expired-horizon closes, win rate,
    Wilson 95% CI lower bound (percent), expectancy in $ per $1,000
    paper (mean paper P&L), max drawdown on a running $1,000 bankroll
    per cell (visible ledger only — cumulative rows carry no time
    series, so their drawdown is None), the all-days base rate for
    the horizon, the control-band rate for the variable (None where
    the variable has no control band), and whether the cell beats
    each. Promotion flags (sample n >= 100, expectancy >= +$20) are
    reported as facts; promotion itself is Nic's call, never
    automatic. Per Nic (2026-11-05) the 95% belt is loosened: the
    Wilson lower bound is shown, never a gate. Halving rows carry the
    cross-asset disclosure (BTC itself contributes n<=4 per band)."""
    cells = {}

    def bucket(key):
        return cells.setdefault(key, {'graded': 0, 'wins': 0, 'losses': 0,
                                      'expired': 0, 'paper_pnl_total': 0.0,
                                      'pnls': []})
    for e in record or []:
        if e.get('kind') not in CYCLE_KINDS:
            continue
        if e.get('status') not in ('won', 'lost', 'expired'):
            continue
        b = bucket(cycle_cell_key(e))
        b['graded'] += 1
        if e.get('status') == 'won':
            b['wins'] += 1
        elif e.get('status') == 'lost':
            b['losses'] += 1
        if e.get('cycle_expired'):
            b['expired'] += 1
        b['paper_pnl_total'] += e.get('paper_pnl') or 0
        b['pnls'].append((e.get('ts_closed') or '', e.get('paper_pnl') or 0))
    for key_str, c in (cumulative or {}).items():
        parts = key_str.split('|')
        if len(parts) != 4:
            continue
        b = bucket(tuple(parts))
        for f in ('graded', 'wins', 'losses', 'expired'):
            b[f] += c.get(f, 0)
        b['paper_pnl_total'] += c.get('paper_pnl_total', 0)

    def _win_rate(wins, losses):
        wl = wins + losses
        return round(wins / wl * 100, 1) if wl else None

    # All-days base rate per horizon (all cycle cells at that horizon).
    base = {}
    for (variable, band, direction, horizon), b in cells.items():
        h = base.setdefault(horizon, {'wins': 0, 'losses': 0})
        h['wins'] += b['wins']
        h['losses'] += b['losses']
    base_rate = {h: _win_rate(v['wins'], v['losses'])
                 for h, v in base.items()}
    # Control-band rate per (variable, horizon).
    control = {}
    for (variable, band, direction, horizon), b in cells.items():
        ctrl = (cycle_feed.CONTROL_BANDS.get(variable)
                if cycle_feed is not None else None)
        if ctrl and band == ctrl:
            c = control.setdefault((variable, horizon),
                                   {'wins': 0, 'losses': 0})
            c['wins'] += b['wins']
            c['losses'] += b['losses']
    control_rate = {k: _win_rate(v['wins'], v['losses'])
                    for k, v in control.items()}

    rows = []
    for (variable, band, direction, horizon), b in sorted(cells.items()):
        wl = b['wins'] + b['losses']
        lo = wilson_lower(b['wins'], wl)
        dd = None
        if b['pnls']:
            bankroll, peak, worst = 1000.0, 1000.0, 0.0
            for _, pnl in sorted(b['pnls']):
                bankroll += pnl
                peak = max(peak, bankroll)
                worst = max(worst, (peak - bankroll) / peak * 100
                            if peak > 0 else 0.0)
            dd = round(worst, 1)
        wr = _win_rate(b['wins'], b['losses'])
        br = base_rate.get(horizon)
        cr = control_rate.get((variable, horizon))
        exp = (round(b['paper_pnl_total'] / b['graded'], 2)
               if b['graded'] else None)
        row = {
            'variable': variable, 'band': band, 'direction': direction,
            'horizon': horizon,
            'graded': b['graded'], 'wins': b['wins'],
            'losses': b['losses'], 'expired': b['expired'],
            'win_rate': wr,
            'wilson_lower_95': round(lo * 100, 1) if lo is not None else None,
            'expectancy_per_1000': exp,
            'paper_pnl_total': round(b['paper_pnl_total'], 2),
            'max_drawdown_pct': dd,
            'drawdown_visible_only': True,
            'base_rate': br,
            'control_rate': cr,
            'beats_base': (wr is not None and br is not None
                           and wr > br),
            'beats_control': (None if cr is None else
                              (wr is not None and wr > cr)),
            'meets_sample_n100': b['graded'] >= 100,
            'meets_expectancy_20': (exp is not None and exp >= 20),
        }
        if variable == 'halving':
            row['note'] = ('Cross-asset evidence only — BTC itself '
                           'contributes n≤4 per band; never "the halving '
                           'cycle works".')
            row['frequency_note'] = ('Slow accumulator — each band '
                                     'recurs once per 4-year cycle.')
        rows.append(row)
    return rows


def proof_roll_cycle_cumulative(cumulative, dropped):
    """Fold closed cycle entries that are about to roll off the capped
    cycle ledger into persistent per-cycle-cell counters (same shape
    as proof_roll_pattern_cumulative, keyed by cycle_cell_key). Open
    entries are never rolled."""
    out = {k: dict(v) for k, v in (cumulative or {}).items()}
    for e in dropped:
        if e.get('kind') not in CYCLE_KINDS:
            continue
        st = e.get('status')
        if st not in ('won', 'lost', 'expired'):
            continue
        key = '|'.join(cycle_cell_key(e))
        c = out.setdefault(key, {'graded': 0, 'wins': 0, 'losses': 0,
                                 'expired': 0, 'paper_pnl_total': 0.0})
        c['graded'] += 1
        c['wins' if st == 'won' else 'losses' if st == 'lost' else 'expired'] += 1
        if e.get('cycle_expired'):
            c['expired'] += 1
        c['paper_pnl_total'] = round(c['paper_pnl_total'] +
                                     (e.get('paper_pnl') or 0), 2)
    return out


# --- Manipulation research track, Phase 2 (shadow grading, spec §4) ---
# Three kinds, one separate ledger (proof.cmi_record, cap 2000) — the
# same shape as the cycle ledger, so the 100-entry main ledger is
# never flooded by research rows.
#   kind='cmi_regime': one candidate per asset-day per band, LONG in
#     CLEAN and CAUTION, horizons 24h + 7d. MANIP grades TWICE — as a
#     LONG cell and as a SHORT-bias cell (the video's contrarian
#     claim) — both reported, neither assumed.
#   kind='cmi_confluence': one candidate per SuperTrend flip with
#     CMI < 0.30, direction per the flip, horizons 6h + 24h. The
#     CMI > 0.50 exit trigger applies during the horizon: if CMI
#     crosses 0.50 before either level hits, the position closes at
#     that candle's close and grades as EXPIRED at that price
#     (cmi_exit=True). This tests the video's exit rule directly.
#     At a candle where a level AND the trigger both hit, the level
#     decides first (conservative: a target actually touched is a
#     win, not an exit-suggestion close).
#   kind='cmi_trap': SuperTrend flips with CMI >= 0.30, direction =
#     the flip direction, horizons 6h + 24h — the video claims these
#     reverse; the grading tests whether fading them or simply
#     avoiding them is the profitable response.
# Levels and dollars match the pattern/cycle tracks (+4.2% / -2.1%,
# +$28 / -$35 on $1,000 paper; EXPIRED grades at the horizon close,
# win iff the direction-signed drift covers the 1.4% costs).
# The exit trigger needs a CMI series, not just prices: main()
# builds cmi_by_sym = {asset: [[ts_ms, cmi], ...]} from the shared
# pattern_candles store (cmi_feed.cmi_series) and passes it to
# proof_grade. A missing series degrades to plain fixed-level
# grading (the trigger simply never fires) — fail-open, never an
# exception.
CMI_KINDS = ('cmi_regime', 'cmi_confluence', 'cmi_trap')
CMI_REGIME_HORIZONS_H = (24, 168)   # labels '24h' and '7d'
CMI_SIGNAL_HORIZONS_H = (6, 24)     # labels '6h' and '24h'
CMI_RECORD_CAP = 2000               # separate CMI ledger cap
CMI_EXIT_TRIGGER = 0.50             # spec §1b: CMI > 0.50 exit trigger

# Shadow universe expansion (Phase 1, 2026-11-05): the signal scan covers
# the top SIGNAL_UNIVERSE_N by market cap (live); ranks
# SHADOW_UNIVERSE_START..SHADOW_UNIVERSE_END are scanned the same way
# but SHADOW-graded only — they never join longs/shorts (no cards, no
# bar, no alerts, no pattern symbols). Promotion to the live universe
# is Nic's call, on the shadow scoreboard's evidence. Measured cost:
# ~0.53s per coin (CoinGecko market_chart, 199/200 ok, no throttling
# at ~2 calls/sec), so +300 coins is ~+160s on an hourly loop.
# MEASURED (2026-11-06 check run): the slice is 300 coins but the
# $100M floor in _scan_coin admits only ~67 of them — the floor binds
# at rank ~267 by market cap, so ranks 201-500 yield 62 charted coins
# (233 under the floor, 4 symbol collisions with live, 1 failed fetch).
# Extending the slice to 1,000 adds nothing at this floor (still 67).
# DECIDED (Nic, 2026-11-05): shadow floor lowered to $50M — at $50M
# the effective shadow universe is ~238 coins (measured in the
# expansion report). The LIVE floor stays $100M; only the shadow scan
# passes min_mcap=SHADOW_MCAP_FLOOR to _scan_coin.
SIGNAL_UNIVERSE_N = 200             # live signal universe (top N by mcap)
SHADOW_UNIVERSE_START = 200         # shadow starts where live ends
SHADOW_UNIVERSE_END = 500           # shadow covers ranks 201-500
SHADOW_MCAP_FLOOR = 50_000_000      # shadow floor (Nic 2026-11-05);
                                    # live floor stays $100M
SHADOW_RECORD_CAP = 2000            # separate shadow ledger cap

# Stocks research track, Phase 1: separate stock ledger cap (same
# pattern as the shadow ledger — stock rows must not flood the
# 100-entry main ledger).
STOCK_RECORD_CAP = 2000


def _proof_grade_cmi_confluence(e, charts_by_sym, cmi_by_sym, now,
                                opened, cur):
    """Grade one cmi_confluence entry: the spec §4 fixed levels plus
    the CMI > 0.50 exit trigger. Walks hourly closes since entry in
    time order; at each observation the win/adverse levels decide
    first, then the trigger (the latest CMI value at or before that
    observation). A trigger close grades as EXPIRED at that candle's
    close (win iff the direction-signed drift covers the 1.4%
    costs) with cmi_exit=True. Past the window (exit_by + 2h grace
    or the 7-day hard cap) with neither level nor trigger hit, the
    plain horizon-close rule applies. Returns (status, result_pct)
    or (None, None) to stay open. Never raises."""
    from datetime import timedelta
    try:
        entry = float(e['entry'])
        is_long = e.get('direction') != 'SHORT'
        if not entry or entry <= 0:
            return None, None
        win_px = entry * (1 + PATTERN_WIN_GROSS_PCT / 100) if is_long \
            else entry * (1 - PATTERN_WIN_GROSS_PCT / 100)
        adv_px = entry * (1 - PATTERN_ADVERSE_PCT / 100) if is_long \
            else entry * (1 + PATTERN_ADVERSE_PCT / 100)
        pts = []
        chart = charts_by_sym.get(e['asset'])
        if chart and opened:
            opened_ms = opened.timestamp() * 1000
            pts = [(ts, pr) for ts, pr in chart if ts >= opened_ms]
        series = list(pts)
        if cur:
            series.append((int(now.timestamp() * 1000), cur))
        cmi_pts = sorted((cmi_by_sym or {}).get(e['asset']) or [])
        cmi_at = {}
        for ts, px in series:
            # Latest CMI value at or before this observation.
            val = None
            for cts, cv in cmi_pts:
                if cts <= ts:
                    val = cv
                else:
                    break
            cmi_at[ts] = val
        for ts, p in series:
            if is_long:
                if p >= win_px:
                    return 'won', PATTERN_WIN_GROSS_PCT - PATTERN_COST_PCT
                if p <= adv_px:
                    return 'lost', -(PATTERN_ADVERSE_PCT + PATTERN_COST_PCT)
            else:
                if p <= win_px:
                    return 'won', PATTERN_WIN_GROSS_PCT - PATTERN_COST_PCT
                if p >= adv_px:
                    return 'lost', -(PATTERN_ADVERSE_PCT + PATTERN_COST_PCT)
            cmi = cmi_at.get(ts)
            if cmi is not None and cmi > CMI_EXIT_TRIGGER:
                drift = ((p - entry) / entry * 100) if is_long \
                    else ((entry - p) / entry * 100)
                result = round(drift - PATTERN_COST_PCT, 2)
                e['cmi_expired'] = True
                e['cmi_exit'] = True
                return ('won' if result >= 0 else 'lost'), result
        exit_by = _iso_parse(e.get('exit_by') or '')
        window_over = bool(exit_by and now > exit_by + timedelta(hours=2)) or \
            bool(opened and (now - opened).total_seconds() > 7 * 86400)
        if not window_over:
            return None, None
        last = series[-1][1] if series else (e.get('last_price') or entry)
        drift = ((last - entry) / entry * 100) if is_long \
            else ((entry - last) / entry * 100)
        result = round(drift - PATTERN_COST_PCT, 2)
        e['cmi_expired'] = True
        return ('won' if result >= 0 else 'lost'), result
    except Exception:
        return None, None


def _proof_grade_cmi_regime(e, charts_by_sym, now, opened, cur):
    """Grade one cmi_regime entry (24h/7d fixed levels). Hard cap 8
    days, like the cycle track — a 7d-horizon entry must survive
    until exit_by + 2h grace. Returns (status, result_pct) or
    (None, None). Never raises."""
    return _fixed_level_grade(e, charts_by_sym, now, opened, cur, 8,
                              'cmi_expired')


def _proof_grade_cmi_trap(e, charts_by_sym, now, opened, cur):
    """Grade one cmi_trap entry (6h/24h fixed levels, 7-day hard
    cap like the pattern track). Returns (status, result_pct) or
    (None, None). Never raises."""
    return _fixed_level_grade(e, charts_by_sym, now, opened, cur, 7,
                              'cmi_expired')


def proof_cmi_regime_candidates(cmi_map, assets, mkt_by_sym, name_by_sym,
                                now, regime_tag):
    """Ledger candidates for one run's CMI bands. One candidate per
    (asset, direction, horizon): LONG in CLEAN/CAUTION, and in MANIP
    BOTH a LONG row and a SHORT-bias row (same band, opposite
    directions — the 'cell' extra keeps their entry ids distinct).
    Horizons 24h + 7d (regime is a slow variable — spec §8 default).
    Returns [candidates]."""
    cands = []
    for asset in (assets or []):
        band = ((cmi_map or {}).get(asset) or {}).get('band')
        if band not in ('CLEAN', 'CAUTION', 'MANIP'):
            continue
        mkt = (mkt_by_sym or {}).get(asset) or {}
        try:
            entry = float(mkt.get('price') or 0)
        except (TypeError, ValueError):
            entry = 0
        if not entry or entry <= 0:
            continue
        directions = ('LONG',) if band != 'MANIP' else ('LONG', 'SHORT')
        for direction in directions:
            for horizon_h in CMI_REGIME_HORIZONS_H:
                label = '24h' if horizon_h == 24 else '7d'
                cand = _proof_candidate(
                    'cmi_regime', asset,
                    (name_by_sym or {}).get(asset) or asset,
                    direction, _proof_pattern_plan(entry, direction),
                    now, horizon_h,
                    tags={'band': band, 'regime': regime_tag},
                    gate_src={},
                    extra={'horizon': label,
                           'cell': f'{band}:{direction}'})
                if cand:
                    cands.append(cand)
    return cands


def proof_cmi_confluence_candidates(cmi_map, mkt_by_sym, name_by_sym,
                                    now, regime_tag):
    """Ledger candidates for one run's confluence flips (SuperTrend
    flip + CMI < 0.30 at the flip close). One candidate per (asset,
    horizon): 6h + 24h, direction per the flip. Returns [candidates]."""
    cands = []
    for asset, row in (cmi_map or {}).items():
        direction = (row or {}).get('confluence')
        if direction not in ('LONG', 'SHORT'):
            continue
        mkt = (mkt_by_sym or {}).get(asset) or {}
        try:
            entry = float(mkt.get('price') or 0)
        except (TypeError, ValueError):
            entry = 0
        if not entry or entry <= 0:
            continue
        band = (row or {}).get('band') or 'na'
        for horizon_h in CMI_SIGNAL_HORIZONS_H:
            cand = _proof_candidate(
                'cmi_confluence', asset,
                (name_by_sym or {}).get(asset) or asset,
                direction, _proof_pattern_plan(entry, direction),
                now, horizon_h,
                tags={'band': band, 'regime': regime_tag},
                gate_src={},
                extra={'horizon': f'{horizon_h}h',
                       'cell': 'confluence'})
            if cand:
                cands.append(cand)
    return cands


def proof_cmi_trap_candidates(cmi_map, mkt_by_sym, name_by_sym,
                              now, regime_tag):
    """Ledger candidates for one run's trap flips (SuperTrend flip
    with CMI >= 0.30). Direction = the flip direction, horizons
    6h + 24h. Returns [candidates]."""
    cands = []
    for asset, row in (cmi_map or {}).items():
        if not (row or {}).get('trap'):
            continue
        direction = 'LONG' if (row or {}).get('supertrend') == 'bullish' \
            else 'SHORT'
        mkt = (mkt_by_sym or {}).get(asset) or {}
        try:
            entry = float(mkt.get('price') or 0)
        except (TypeError, ValueError):
            entry = 0
        if not entry or entry <= 0:
            continue
        band = (row or {}).get('band') or 'na'
        for horizon_h in CMI_SIGNAL_HORIZONS_H:
            cand = _proof_candidate(
                'cmi_trap', asset,
                (name_by_sym or {}).get(asset) or asset,
                direction, _proof_pattern_plan(entry, direction),
                now, horizon_h,
                tags={'band': band, 'regime': regime_tag},
                gate_src={},
                extra={'horizon': f'{horizon_h}h', 'cell': 'trap'})
            if cand:
                cands.append(cand)
    return cands


def proof_record_cmi(record, candidates, now):
    """Prepend fresh CMI rows to the CMI ledger (newest-first).
    Dedup: the same asset+kind+cell+horizon is never re-recorded
    within 24h of a previous entry. Like the cycle ledger there is
    NO open-block: one row per asset-day is intended, so overlapping
    7d windows do not block each other."""
    fresh = []
    for cand in candidates:
        if not cand:
            continue
        dup = False
        for e in record:
            if e.get('asset') != cand['asset'] or e.get('kind') != cand['kind']:
                continue
            if (e.get('cell') or '') != (cand.get('cell') or ''):
                continue
            if (e.get('horizon') or '') != (cand.get('horizon') or ''):
                continue
            opened = _iso_parse(e.get('ts_open') or '')
            if opened and (now - opened).total_seconds() < 24 * 3600:
                dup = True
                break
        if not dup:
            fresh.append(cand)
    record[:] = fresh + record
    return len(fresh)


def cmi_cell_key(e):
    """(kind, band, direction, horizon) for a CMI ledger entry."""
    return (e.get('kind') or '?', e.get('band') or 'na',
            e.get('direction') or 'LONG', e.get('horizon') or '?')


def cmi_scoreboard(record, cumulative=None):
    """Per-CMI-cell scoreboard (spec §4 cells, §5 promotion bar,
    reported — never gated). Each row: kind, band, direction,
    horizon, graded, wins, losses, expired-horizon closes (of which
    cmi_exits closed by the CMI > 0.50 trigger), win rate, Wilson
    95% CI lower bound (percent), expectancy in $ per $1,000 paper
    (mean paper P&L), max drawdown on a running $1,000 bankroll per
    cell (visible ledger only), occurrences per week + the
    insufficient-frequency flag (spec §4: a band averaging < 1
    occurrence/week — judged only once a cell spans >= 14 days, so
    a young cell reads None, not a false flag). Promotion flags
    (sample n >= 100, expectancy >= +$20) are reported as facts;
    promotion itself is Nic's call, never automatic. Per Nic
    (2026-11-05) the 95% belt is loosened: the Wilson lower bound
    is shown, never a gate."""
    cells = {}

    def bucket(key):
        return cells.setdefault(key, {'graded': 0, 'wins': 0, 'losses': 0,
                                      'expired': 0, 'cmi_exits': 0,
                                      'paper_pnl_total': 0.0, 'pnls': [],
                                      'opened': []})
    for e in record or []:
        if e.get('kind') not in CMI_KINDS:
            continue
        if e.get('status') not in ('won', 'lost', 'expired'):
            continue
        b = bucket(cmi_cell_key(e))
        b['graded'] += 1
        if e.get('status') == 'won':
            b['wins'] += 1
        elif e.get('status') == 'lost':
            b['losses'] += 1
        if e.get('cmi_expired'):
            b['expired'] += 1
        if e.get('cmi_exit'):
            b['cmi_exits'] += 1
        b['paper_pnl_total'] += e.get('paper_pnl') or 0
        b['pnls'].append((e.get('ts_closed') or '', e.get('paper_pnl') or 0))
        opened = _iso_parse(e.get('ts_open') or '')
        if opened:
            b['opened'].append(opened)
    for key_str, c in (cumulative or {}).items():
        parts = key_str.split('|')
        if len(parts) != 4:
            continue
        b = bucket(tuple(parts))
        for f in ('graded', 'wins', 'losses', 'expired', 'cmi_exits'):
            b[f] += c.get(f, 0)
        b['paper_pnl_total'] += c.get('paper_pnl_total', 0)
    rows = []
    for (kind, band, direction, horizon), b in sorted(cells.items()):
        wl = b['wins'] + b['losses']
        lo = wilson_lower(b['wins'], wl)
        dd = None
        if b['pnls']:
            bankroll, peak, worst = 1000.0, 1000.0, 0.0
            for _, pnl in sorted(b['pnls']):
                bankroll += pnl
                peak = max(peak, bankroll)
                worst = max(worst, (peak - bankroll) / peak * 100
                            if peak > 0 else 0.0)
            dd = round(worst, 1)
        freq = None
        insufficient = None
        if len(b['opened']) >= 2:
            span_days = (max(b['opened']) - min(b['opened'])) \
                .total_seconds() / 86400
            if span_days >= 14:
                freq = round(b['graded'] / (span_days / 7), 2)
                insufficient = freq < 1.0
        exp = (round(b['paper_pnl_total'] / b['graded'], 2)
               if b['graded'] else None)
        rows.append({
            'kind': kind, 'band': band, 'direction': direction,
            'horizon': horizon,
            'graded': b['graded'], 'wins': b['wins'],
            'losses': b['losses'], 'expired': b['expired'],
            'cmi_exits': b['cmi_exits'],
            'win_rate': round(b['wins'] / wl * 100, 1) if wl else None,
            'wilson_lower_95': round(lo * 100, 1) if lo is not None else None,
            'expectancy_per_1000': exp,
            'paper_pnl_total': round(b['paper_pnl_total'], 2),
            'max_drawdown_pct': dd,
            'drawdown_visible_only': True,
            'occurrences_per_week': freq,
            'insufficient_frequency': insufficient,
            'meets_sample_n100': b['graded'] >= 100,
            'meets_expectancy_20': (exp is not None and exp >= 20),
        })
    return rows


def proof_roll_cmi_cumulative(cumulative, dropped):
    """Fold closed CMI entries that are about to roll off the capped
    CMI ledger into persistent per-CMI-cell counters (same shape as
    proof_roll_pattern_cumulative, keyed by cmi_cell_key). Open
    entries are never rolled."""
    out = {k: dict(v) for k, v in (cumulative or {}).items()}
    for e in dropped:
        if e.get('kind') not in CMI_KINDS:
            continue
        st = e.get('status')
        if st not in ('won', 'lost', 'expired'):
            continue
        key = '|'.join(cmi_cell_key(e))
        c = out.setdefault(key, {'graded': 0, 'wins': 0, 'losses': 0,
                                 'expired': 0, 'cmi_exits': 0,
                                 'paper_pnl_total': 0.0})
        c['graded'] += 1
        c['wins' if st == 'won' else 'losses' if st == 'lost' else 'expired'] += 1
        if e.get('cmi_expired'):
            c['expired'] += 1
        if e.get('cmi_exit'):
            c['cmi_exits'] += 1
        c['paper_pnl_total'] = round(c['paper_pnl_total'] +
                                     (e.get('paper_pnl') or 0), 2)
    return out


def proof_grade(record, mkt_by_sym, charts_by_sym, now,
                cmi_by_sym=None):
    """Grade every open ledger entry against fresh market data.
    mkt_by_sym: {sym: {'price', 'high_24h', 'low_24h'}} from the markets
    payload; charts_by_sym: {sym: [[ts_ms, price], ...]} hourly points —
    main() tops this up so every open call's asset is covered.
    cmi_by_sym: {sym: [[ts_ms, cmi], ...]} — the CMI series the
    confluence exit-trigger grader walks (main() builds it from the
    shared pattern_candles store; None degrades confluence rows to
    plain fixed-level grading, trigger never fires)."""
    from datetime import timedelta
    for e in record:
        if e.get('status') != 'open':
            continue
        sym = e['asset']
        is_long = e.get('direction') != 'SHORT'
        tp, sl, entry = e['take_profit'], e['stop_loss'], e['entry']
        opened = _iso_parse(e.get('ts_open') or '')
        mkt = mkt_by_sym.get(sym) or {}
        cur = mkt.get('price')
        if cur:
            e['last_price'] = cur
        if e.get('kind') in PATTERN_KINDS:
            # Pattern entries grade on the spec §4 fixed levels, not the
            # plain since-entry path — see _proof_grade_pattern.
            p_status, p_result = _proof_grade_pattern(
                e, charts_by_sym, now, opened, cur)
            if p_status:
                e['status'] = p_status
                e['result_pct'] = round(p_result, 2)
                e['paper_pnl'] = round(p_result * PAPER_USD / 100, 2)
                e['ts_closed'] = now.isoformat()
            continue
        if e.get('kind') in CYCLE_KINDS:
            # Cycle entries grade on the spec §4 fixed levels, not the
            # plain since-entry path — see _proof_grade_cycle.
            c_status, c_result = _proof_grade_cycle(
                e, charts_by_sym, now, opened, cur)
            if c_status:
                e['status'] = c_status
                e['result_pct'] = round(c_result, 2)
                e['paper_pnl'] = round(c_result * PAPER_USD / 100, 2)
                e['ts_closed'] = now.isoformat()
            continue
        if e.get('kind') in CMI_KINDS:
            # CMI entries grade on the spec §4 fixed levels, not the
            # plain since-entry path. Confluence rows additionally
            # walk the CMI > 0.50 exit trigger — see
            # _proof_grade_cmi_confluence.
            if e.get('kind') == 'cmi_confluence':
                m_status, m_result = _proof_grade_cmi_confluence(
                    e, charts_by_sym, cmi_by_sym or {}, now, opened, cur)
            elif e.get('kind') == 'cmi_regime':
                m_status, m_result = _proof_grade_cmi_regime(
                    e, charts_by_sym, now, opened, cur)
            else:
                m_status, m_result = _proof_grade_cmi_trap(
                    e, charts_by_sym, now, opened, cur)
            if m_status:
                e['status'] = m_status
                e['result_pct'] = round(m_result, 2)
                e['paper_pnl'] = round(m_result * PAPER_USD / 100, 2)
                e['ts_closed'] = now.isoformat()
            continue
        if e.get('kind') == 'setup':
            # Setups grade on the trigger mechanic, not the plain
            # since-entry path — see _proof_grade_setup.
            s_status, s_result = _proof_grade_setup(e, mkt, charts_by_sym,
                                                    now, opened, cur)
            if s_status:
                e['status'] = s_status
                e['result_pct'] = round(s_result, 2)
                e['paper_pnl'] = round(s_result * PAPER_USD / 100, 2)
                e['ts_closed'] = now.isoformat()
            continue
        status = result = None
        if cur:
            pts = []
            chart = charts_by_sym.get(sym)
            if chart and opened:
                opened_ms = opened.timestamp() * 1000
                # A trailed stop only guards prices AFTER the trail —
                # pre-trail dips must not retro-trigger the new stop.
                trail_ms = e.get('trail_ts_ms') or 0
                pts = [pr for ts, pr in chart
                       if ts >= max(opened_ms, trail_ms)]
            if pts:
                # Walk in time order — first touch decides. Current price
                # is the final observation.
                for p in pts + [cur]:
                    if is_long:
                        if p >= tp:
                            status = 'won'
                            break
                        if p <= sl:
                            status = 'lost'
                            break
                    else:
                        if p <= tp:
                            status = 'won'
                            break
                        if p >= sl:
                            status = 'lost'
                            break
            else:
                # No hourly path: extremes actually observed since entry
                # (accumulated snapshots), plus the trailing-24h window
                # once it lies entirely after entry (age >= 24h).
                hi = max(e.get('obs_hi') or cur, cur)
                lo = min(e.get('obs_lo') or cur, cur)
                if opened and (now - opened).total_seconds() >= 24 * 3600:
                    if mkt.get('high_24h') is not None:
                        hi = max(hi, mkt['high_24h'])
                    if mkt.get('low_24h') is not None:
                        lo = min(lo, mkt['low_24h'])
                e['obs_hi'], e['obs_lo'] = hi, lo
                if is_long:
                    hit_tp, hit_sl = hi >= tp, lo <= sl
                else:
                    hit_tp, hit_sl = lo <= tp, hi >= sl
                if hit_tp and hit_sl:
                    status = 'lost'  # order unknowable — conservative
                elif hit_tp:
                    status = 'won'
                elif hit_sl:
                    status = 'lost'
            if status == 'won':
                result = e['expected_return_pct']
            elif status == 'lost':
                result = -e['max_loss_pct']
                if result >= 0:
                    # The stop was trailed to breakeven or better, so
                    # touching it locks a profit (or a scratch), not a
                    # loss — the W/L label follows the realized result
                    # (same rule as the fixed-level graders' expired
                    # branch: result >= 0 is a win). A7A5 2026-11-06
                    # exited +0.41% via a ratcheted stop and was
                    # recorded as a loss before this fix.
                    status = 'won'
        if status is None:
            exit_by = _iso_parse(e.get('exit_by') or '')
            age_days = (now - opened).total_seconds() / 86400 if opened else 99
            drift_from = None
            if exit_by and now > exit_by + timedelta(hours=2):
                drift_from = cur or e.get('last_price')
            elif age_days > 7:
                drift_from = e.get('last_price') or entry
            if drift_from:
                # Nic 2026-10-30: ride the win — a green position at exit
                # time is NOT force-sold. Trail the stop and extend the
                # window instead of closing at market. Flat/red still exits.
                px = cur or drift_from
                green = (px > entry * 1.002) if is_long else (px < entry * 0.998)
                if green:
                    trails = e.get('trail_count') or 0
                    if trails == 0:
                        new_stop = entry  # breakeven on the first trail
                    else:
                        # Ratchet: lock half the open profit.
                        new_stop = (entry + 0.5 * (px - entry)) if is_long \
                            else (entry - 0.5 * (entry - px))
                    e['stop_loss'] = new_stop
                    if is_long:
                        e['max_loss_pct'] = round((entry - new_stop) / entry * 100, 2)
                    else:
                        e['max_loss_pct'] = round((new_stop - entry) / entry * 100, 2)
                    if e.get('stretch_target'):
                        # Ride toward the stretch target now.
                        e['take_profit'] = e['stretch_target']
                        e['expected_return_pct'] = e.get(
                            'stretch_return_pct', e['expected_return_pct'])
                    e['exit_by'] = (now + timedelta(hours=24)).isoformat()
                    e['trail_count'] = trails + 1
                    e['trail_ts_ms'] = now.timestamp() * 1000
                    e['obs_hi'] = e['obs_lo'] = None  # re-observe post-trail
                    e['trail_note'] = (
                        f"green at exit ({px}) — stop trailed to {new_stop}, "
                        f"+24h window (trail #{trails + 1})")
                else:
                    status = 'expired'
                    result = ((drift_from - entry) / entry * 100) if is_long \
                        else ((entry - drift_from) / entry * 100)
        if status:
            e['status'] = status
            e['result_pct'] = round(result, 2)
            e['paper_pnl'] = round(result * PAPER_USD / 100, 2)
            e['ts_closed'] = now.isoformat()

# --- After-exit tracker (2026-10-22) --------------------------------------
# Nic's question — "too quick to sell?" — is answered with measurement,
# not opinion: for every call that actually EXITED (status 'won' =
# target exit, 'lost' = stop exit), keep watching the price for 24h
# AFTER the exit and record how far it ran without us. Expired calls
# are not tracked: a drift/timeout exit has no target or stop to judge
# the exit against, and never-triggered setups never held a position.
#
# Exit reference: grading credits a win at exactly the take-profit and
# a loss at exactly the stop, so the exit price is take_profit (won)
# or stop_loss (lost) and ts_closed is the exit time. The 24h window
# is sampled on subsequent hourly runs from the same hourly charts the
# grader uses (main() tops up chart fetches for tracked assets too):
# every chart point inside (exit, window_end] is folded into running
# extremes exactly once (`folded_through` guards re-folding), plus the
# run's current-price snapshot when the chart has no fresh point.
# Extremes never include points past the window end; the close is the
# first chart point at/after the end inside a 3h grace, else the
# current price inside the grace, else the last in-window observation.
# `samples` counts folded observations, so a window built from sparse
# runs reads as sparse — best-effort is labelled, never hidden.
#
# Seeding: a record whose window is still open when the tracker first
# sees it (exit < 24h ago) is seeded from the trailing-24h chart,
# which covers its whole post-exit window so far — real engine data,
# not reconstruction. A record whose window already fully elapsed
# before tracking existed is left untracked (forward-only; the 55
# records graded before this build carry no post-exit data).
#
# Derived flags (direction-aware; percentages are PRICE moves vs the
# exit price, signed):
#   target exit — captured gain is expected_return_pct, i.e.
#   |take_profit - entry| / entry x 100. The favorable post-exit run
#   is post_exit_high_pct for a LONG, -post_exit_low_pct for a SHORT.
#   left_on_table_pct is that favorable run; ran_further is True when
#   it reached at least +50% of the captured gain (the price kept
#   running at least half again what we took).
#   stop exit — recovered is True when price returned to the entry
#   price (or beyond) inside the window: whipsaw evidence.
POST_EXIT_WINDOW_H = 24
POST_EXIT_CLOSE_GRACE_H = 3

def _proof_post_exit_ref(e):
    """(exit_price, exit_kind) for a closed entry, or (None, None).
    Only target ('won') and stop ('lost') exits have a reference."""
    if e.get('status') == 'won':
        return e.get('take_profit'), 'target'
    if e.get('status') == 'lost':
        return e.get('stop_loss'), 'stop'
    return None, None

def _proof_post_exit_active(e, now):
    """True while an entry still needs hourly price samples: either it
    carries an incomplete post_exit block, or it exited (won/lost)
    recently enough that its window is still open and tracking can
    still start."""
    pe = e.get('post_exit')
    if pe is not None:
        return not pe.get('complete')
    exit_price, _kind = _proof_post_exit_ref(e)
    if not exit_price:
        return False
    closed = _iso_parse(e.get('ts_closed') or '')
    if not closed:
        return False
    from datetime import timedelta
    return closed <= now <= closed + timedelta(hours=POST_EXIT_WINDOW_H)

def _proof_post_exit_derive(e, pe):
    """Recompute the percentage fields and derived flags from the
    running extremes. Called after every fold, so a live window shows
    live values; completion freezes them."""
    exit_px = pe['exit_price']
    is_long = e.get('direction') != 'SHORT'
    hi, lo = pe.get('hi'), pe.get('lo')
    if hi is not None:
        pe['post_exit_high_pct'] = round((hi - exit_px) / exit_px * 100, 2)
    if lo is not None:
        pe['post_exit_low_pct'] = round((lo - exit_px) / exit_px * 100, 2)
    if pe.get('last') is not None:
        pe['post_exit_close_pct'] = round(
            (pe['last'] - exit_px) / exit_px * 100, 2)
    if pe['exit_kind'] == 'target':
        captured = e.get('expected_return_pct') or 0
        if is_long:
            favorable = pe.get('post_exit_high_pct')
        else:
            low_pct = pe.get('post_exit_low_pct')
            favorable = -low_pct if low_pct is not None else None
        if favorable is not None:
            pe['left_on_table_pct'] = favorable
            pe['ran_further'] = bool(captured and
                                     favorable >= 0.5 * captured)
    elif pe['exit_kind'] == 'stop':
        entry = e.get('entry')
        if entry:
            if is_long:
                pe['recovered'] = bool(hi is not None and hi >= entry)
            else:
                pe['recovered'] = bool(lo is not None and lo <= entry)

def proof_track_post_exit(record, mkt_by_sym, charts_by_sym, now):
    """Advance every after-exit window by one run. Initializes blocks
    for entries that exited inside their still-open window, folds new
    observations, and completes windows whose 24h have elapsed.
    Returns the number of windows completed by this call. Pure
    measurement — grading, statuses and results are never touched."""
    from datetime import timedelta
    window = timedelta(hours=POST_EXIT_WINDOW_H)
    grace_ms = POST_EXIT_CLOSE_GRACE_H * 3600 * 1000
    now_ms = now.timestamp() * 1000
    n_completed = 0
    for e in record:
        pe = e.get('post_exit')
        if pe is None:
            exit_price, kind = _proof_post_exit_ref(e)
            closed = _iso_parse(e.get('ts_closed') or '')
            if not exit_price or not closed:
                continue
            if not (closed <= now <= closed + window):
                continue  # window already gone before tracking existed
            pe = {
                'exit_price': exit_price,
                'exit_kind': kind,
                'ts_exit': e.get('ts_closed'),
                'window_end': (closed + window).isoformat(),
                'samples': 0,
                'hi': None, 'lo': None, 'last': None,
                'folded_through': None,
                'post_exit_high_pct': None, 'post_exit_low_pct': None,
                'post_exit_close_pct': None,
                'ran_further': None, 'left_on_table_pct': None,
                'recovered': None,
                'complete': False,
            }
            e['post_exit'] = pe
        if pe.get('complete'):
            continue
        closed = _iso_parse(pe.get('ts_exit') or '')
        if not closed:
            pe['complete'] = True
            continue
        end = closed + window
        end_ms = end.timestamp() * 1000
        closed_ms = closed.timestamp() * 1000
        chart = sorted(charts_by_sym.get(e['asset']) or [])
        folded_ms = None
        if pe.get('folded_through'):
            ft = _iso_parse(pe['folded_through'])
            folded_ms = ft.timestamp() * 1000 if ft else None
        newest_folded = folded_ms
        for ts, px in chart:
            if ts <= closed_ms or ts > now_ms:
                continue
            if folded_ms is not None and ts <= folded_ms:
                continue
            if ts <= end_ms:
                pe['hi'] = px if pe['hi'] is None else max(pe['hi'], px)
                pe['lo'] = px if pe['lo'] is None else min(pe['lo'], px)
                pe['last'] = px
                pe['samples'] += 1
            newest_folded = ts if newest_folded is None \
                else max(newest_folded, ts)
        if newest_folded is not None:
            pe['folded_through'] = datetime.fromtimestamp(
                newest_folded / 1000, timezone.utc).isoformat()
        # Snapshot fallback: the run's current price is an observation
        # when the chart offered nothing within the last 5 minutes.
        mkt = mkt_by_sym.get(e['asset']) or {}
        cur = mkt.get('price')
        if cur and now <= end:
            last_chart_ms = newest_folded
            if last_chart_ms is None or now_ms - last_chart_ms > 5 * 60 * 1000:
                pe['hi'] = cur if pe['hi'] is None else max(pe['hi'], cur)
                pe['lo'] = cur if pe['lo'] is None else min(pe['lo'], cur)
                pe['last'] = cur
                pe['samples'] += 1
        _proof_post_exit_derive(e, pe)
        if now >= end:
            # Close: first chart point at/after the window end inside
            # the grace, else the current price inside the grace, else
            # the last in-window observation (already in close_pct).
            close_px = None
            for ts, px in chart:
                if end_ms <= ts <= end_ms + grace_ms:
                    close_px = px
                    break
            if close_px is None and cur and \
                    (now - end).total_seconds() <= POST_EXIT_CLOSE_GRACE_H * 3600:
                close_px = cur
            if close_px is not None:
                pe['last'] = close_px
                pe['post_exit_close_pct'] = round(
                    (close_px - pe['exit_price']) / pe['exit_price'] * 100, 2)
            pe['complete'] = True
            n_completed += 1
    return n_completed

def _proof_post_exit_stats(record):
    """Aggregate after-exit readings over COMPLETED windows with at
    least one sample (same visible-ledger scope as proof_stats).
    avg_post_exit_high_pct is averaged over target exits — it is the
    'how much more did our winners run' number the review reads."""
    done = [e['post_exit'] for e in record
            if e.get('post_exit') and e['post_exit'].get('complete')
            and e['post_exit'].get('samples')]
    targets = [pe for pe in done if pe['exit_kind'] == 'target']
    stops = [pe for pe in done if pe['exit_kind'] == 'stop']
    highs = [pe['post_exit_high_pct'] for pe in targets
             if pe.get('post_exit_high_pct') is not None]
    return {
        'tracked': len(done),
        'target_exits': len(targets),
        'ran_further': sum(1 for pe in targets if pe.get('ran_further')),
        'avg_post_exit_high_pct': round(sum(highs) / len(highs), 2)
            if highs else None,
        'stop_exits': len(stops),
        'recovered_after_stop': sum(1 for pe in stops
                                    if pe.get('recovered')),
    }

def proof_stats(record):
    def agg(entries):
        closed = [e for e in entries if e.get('status') in ('won', 'lost', 'expired')]
        wins = sum(1 for e in closed if e['status'] == 'won')
        losses = sum(1 for e in closed if e['status'] == 'lost')
        expired = sum(1 for e in closed if e['status'] == 'expired')
        return {
            'graded': len(closed),
            'wins': wins,
            'losses': losses,
            'expired': expired,
            'open': sum(1 for e in entries if e.get('status') == 'open'),
            'win_rate': round(wins / (wins + losses) * 100, 1) if (wins + losses) else None,
            'paper_pnl_total': round(sum(e.get('paper_pnl') or 0 for e in closed), 2),
        }
    stats = agg(record)
    stats['by_kind'] = {k: agg([e for e in record if e.get('kind') == k])
                        for k in ('signal', 'mover', 'setup',
                                  'pattern', 'pattern_confluence')}
    stats['pattern_scoreboard'] = None  # filled by main() (needs cumulative)
    stats['cycle_scoreboard'] = None  # filled by main() (needs cumulative)
    stats['cmi_scoreboard'] = None  # filled by main() (needs cumulative)
    stats['post_exit'] = _proof_post_exit_stats(record)
    stats['codeword_gate'] = _proof_gate_stats(record, agg)
    return stats

# --- Codeword gate (shadow evaluation, 2026-10-23) ------------------------
# Nic's framing, borrowed from error-correcting codes: a buy candidate
# is a "valid codeword" only if it passes EVERY independent parity
# check — a setup that fails one check is not "mostly a buy", it is
# not a codeword at all. This block MEASURES that gate against the
# Proof Book; it changes nothing live. No alert, verdict, WhatsApp
# rule, grading outcome, or exit reads `gate` — the gate is stamped
# on each new record at creation and aggregated in proof.stats as
# `codeword_gate`, so weeks of evidence can say whether the gate
# would have helped before anyone considers wiring it in.
#
# Six checks, each computed ONLY from fields the family genuinely
# carries (a check whose input does not exist reads None — unknown,
# never guessed — the same honesty rule as band 'na' / regime
# 'unknown'):
#   confidence   signals: stored confidence >= 70 (the proven band).
#                Where only the derived band survives (backfill),
#                band '70s'/'80+' stands in exactly: conf_band maps
#                >= 70 to those two labels and nothing else.
#                Movers/setups carry no confidence -> None.
#   alignment    movers: the 1h and 24h moves agree with the plan's
#                direction (chg_1h and chg_24h, both stored on the
#                mover at creation; LONG: both > 0). Signals store
#                no timeframe-alignment field (their score's sign IS
#                the direction) and setups carry none either -> None.
#                No swing-sourced records exist in this ledger, so
#                the swing tf_align string is never read here.
#   tradable     the source's tradable list (built by tradable_on at
#                creation) names coinbase and/or cryptocom. A value
#                that is not a list (stock signals stamp True —
#                equities trade via brokerage) reads None: the
#                crypto-venue check does not apply to stocks.
#   news_clean   news_flag false or ABSENT. The intraday engine has
#                no news_flag field at all (it exists only on swing
#                picks in nexintel.py), so every intraday record
#                reads True here — stated plainly, not hidden.
#   plan_complete entry / take_profit / stop_loss all present, > 0,
#                and sanely ordered (LONG: stop < entry < target;
#                SHORT mirrored).
#   regime_ok    the record's regime tag is not the caution regime
#                ('risk_off'). 'unknown' -> None: a regime that
#                could not be read cannot be certified benign.
# A family that never carries a check is a PUNCTURED position (the
# coding term): pass = every evaluable check is True AND at least
# GATE_MIN_EVALUABLE checks were evaluable, so a record cannot pass
# on two trivial checks alone. One check is REQUIRED, not punctured:
# regime_ok — every record carries a regime tag, and an 'unknown'
# regime cannot be certified non-caution, so it never passes.
GATE_VENUES = ('coinbase', 'cryptocom')
GATE_MIN_EVALUABLE = 4

def proof_gate_checks(kind, direction, entry, take_profit, stop_loss,
                      regime, confidence=None, band=None, tradable=None,
                      news_flag=None, chg_1h=None, chg_24h=None):
    """The six parity checks for one candidate. True/False where the
    family's own fields decide, None where the field does not exist.
    Pure function — unit-tested."""
    checks = {}
    if confidence is not None:
        checks['confidence'] = confidence >= 70
    elif band not in (None, 'na'):
        checks['confidence'] = band in ('70s', '80+')
    else:
        checks['confidence'] = None
    if kind == 'mover' and chg_1h is not None and chg_24h is not None:
        if direction == 'SHORT':
            checks['alignment'] = chg_1h < 0 and chg_24h < 0
        else:
            checks['alignment'] = chg_1h > 0 and chg_24h > 0
    else:
        checks['alignment'] = None
    if tradable is None:
        checks['tradable'] = None
    elif isinstance(tradable, (list, tuple, set)):
        checks['tradable'] = any(v in GATE_VENUES for v in tradable)
    else:
        # Not a venue list (a stock signal stamps `tradable: True` —
        # equities trade via brokerage, not on crypto venues): the
        # check is unknown, never guessed. Without this guard a bool
        # here crashes the whole run (2026-11-06 intraday failure:
        # TypeError 'bool' object is not iterable, on the first
        # market-hours run that produced stock candidates).
        checks['tradable'] = None
    checks['news_clean'] = not news_flag
    try:
        e = float(entry)
        t = float(take_profit)
        s = float(stop_loss)
        if direction == 'SHORT':
            checks['plan_complete'] = bool(e > 0 and t < e < s)
        else:
            checks['plan_complete'] = bool(e > 0 and s < e < t)
    except (TypeError, ValueError):
        checks['plan_complete'] = False
    if regime in ('risk_on', 'neutral'):
        checks['regime_ok'] = True
    elif regime == 'risk_off':
        checks['regime_ok'] = False
    else:
        checks['regime_ok'] = None
    return checks

def proof_gate(kind, direction, entry, take_profit, stop_loss, regime,
               **src):
    """{'pass': bool, 'checks': {...}} for one candidate. Extra inputs
    (confidence, band, tradable, news_flag, chg_1h, chg_24h) arrive via
    **src — see proof_gate_checks for what each family really has."""
    checks = proof_gate_checks(kind, direction, entry, take_profit,
                               stop_loss, regime, **src)
    evaluable = [v for v in checks.values() if v is not None]
    return {'pass': len(evaluable) >= GATE_MIN_EVALUABLE
            and all(evaluable)
            and checks.get('regime_ok') is True,
            'checks': checks}

def _proof_gate_stats(record, agg):
    """Pass/fail split of graded outcomes over records carrying a
    gate block (same visible-ledger scope as proof_stats itself;
    records predating the gate count as unevaluated, never assumed
    to have passed or failed)."""
    gated = [e for e in record if isinstance(e.get('gate'), dict)]
    passed = [e for e in gated if e['gate'].get('pass')]
    failed = [e for e in gated if not e['gate'].get('pass')]
    return {'evaluated': len(gated),
            'unevaluated': len(record) - len(gated),
            'pass': agg(passed),
            'fail': agg(failed)}

def proof_cells(record, cumulative=None):
    """Win/loss PER SCENARIO CELL, all-time: aggregates over the
    visible ledger MERGED with the cumulative counters of records that
    already rolled off the 100-entry cap. Cumulative only ever holds
    closed outcomes (an entry joins it at trim time, after closing),
    so open counts come from the ledger alone and no closed outcome
    is ever double-counted. Each row carries its n — a win rate
    without its sample size is not published anywhere in this book."""
    cells = {}
    def bucket(key):
        return cells.setdefault(key, {'graded': 0, 'wins': 0, 'losses': 0,
                                      'expired': 0, 'never_triggered': 0,
                                      'open': 0, 'paper_pnl_total': 0.0})
    for e in record:
        b = bucket(proof_cell_key(e))
        st = e.get('status')
        if st == 'open':
            b['open'] += 1
        elif st in ('won', 'lost', 'expired'):
            b['graded'] += 1
            b['wins' if st == 'won' else 'losses' if st == 'lost' else 'expired'] += 1
            if e.get('never_triggered'):
                b['never_triggered'] += 1
            b['paper_pnl_total'] += e.get('paper_pnl') or 0
    for key, c in (cumulative or {}).items():
        parts = key.split('|')
        if len(parts) != 4:
            continue
        b = bucket(tuple(parts))
        for f in ('graded', 'wins', 'losses', 'expired', 'never_triggered'):
            b[f] += c.get(f, 0)
        b['paper_pnl_total'] += c.get('paper_pnl_total', 0)
    rows = []
    for (kind, direction, band, regime), b in sorted(cells.items()):
        wl = b['wins'] + b['losses']
        rows.append({
            'type': kind, 'direction': direction, 'band': band,
            'regime': regime,
            'graded': b['graded'], 'wins': b['wins'], 'losses': b['losses'],
            'expired': b['expired'], 'never_triggered': b['never_triggered'],
            'open': b['open'],
            'win_rate': round(b['wins'] / wl * 100, 1) if wl else None,
            'paper_pnl_total': round(b['paper_pnl_total'], 2),
        })
    return rows

# Daily-review flag rule: a cell with real evidence (n >= 20 graded)
# at an extreme win rate is a CANDIDATE for alert inclusion/exclusion.
# Recommendation only — this build never changes a live threshold,
# alert rule, or verdict; the morning review logs the flag and a
# human decides.
CELL_FLAG_MIN_N = 20
CELL_FLAG_WIN_HI = 60
CELL_FLAG_WIN_LO = 35

def proof_flagged_cells(cells):
    flags = []
    for row in cells:
        wr = row.get('win_rate')
        if row.get('graded', 0) < CELL_FLAG_MIN_N or wr is None:
            continue
        if wr >= CELL_FLAG_WIN_HI:
            cand = 'include'
        elif wr <= CELL_FLAG_WIN_LO:
            cand = 'exclude'
        else:
            continue
        flags.append({'type': row['type'], 'direction': row['direction'],
                      'band': row['band'], 'regime': row['regime'],
                      'graded': row['graded'], 'win_rate': wr,
                      'paper_pnl_total': row.get('paper_pnl_total', 0.0),
                      'candidate': cand, 'recommendation_only': True})
    return flags

# Proof Book ALERT exclusions — scenario cells Nic has reviewed and
# decided must never trigger a WhatsApp alert. Declared HERE, next to
# the flag machinery that recommends them; honored ONLY at
# alert-eligibility time (scripts/intraday-sms.py reads this list).
# Nothing else consults it: scoring, gates, verdicts, the Picks page,
# the daily email, and the Proof Book recording itself are untouched —
# an excluded cell stays in the book and keeps grading, so a recovery
# would show up in the next review.
# Cell key format: (kind, direction, band, regime) — identical to
# proof_cell_key(). Movers are always recorded direction='LONG',
# band='na' (see the proof_cands loop), so the mover cell covers
# every mover recorded while that regime is in force.
PROOF_ALERT_EXCLUDED_CELLS = [
    # Nic 2026-11-01: mover·LONG·caution — 17.6% over 48 graded,
    # -$1,026 paper P&L. The mover·LONG·mixed cell (35.0%, +$44.73)
    # was deliberately NOT excluded.
    ('mover', 'LONG', 'na', 'risk_off'),
]

def proof_cell_excluded(kind, direction, band, regime):
    """True when the scenario cell (kind, direction, band, regime) is
    on Nic's alert-exclusion list. Exact match only — an uncertain or
    unrecorded tag never excludes (fail-open: alerts flow)."""
    try:
        key = (str(kind), str(direction), str(band), str(regime))
    except Exception:
        return False
    return key in PROOF_ALERT_EXCLUDED_CELLS

def proof_roll_cumulative(cumulative, dropped):
    """Fold closed entries that are about to roll off the capped
    ledger into the persistent per-cell counters (stored in the proof
    payload alongside the record), so all-time win/loss knowledge
    keeps growing even though the visible ledger stays at 100.
    Open entries are never rolled — they are still the ledger's job."""
    out = {k: dict(v) for k, v in (cumulative or {}).items()}
    for e in dropped:
        st = e.get('status')
        if st not in ('won', 'lost', 'expired'):
            continue
        key = '|'.join(proof_cell_key(e))
        c = out.setdefault(key, {'graded': 0, 'wins': 0, 'losses': 0,
                                 'expired': 0, 'never_triggered': 0,
                                 'paper_pnl_total': 0.0})
        c['graded'] += 1
        c['wins' if st == 'won' else 'losses' if st == 'lost' else 'expired'] += 1
        if e.get('never_triggered'):
            c['never_triggered'] += 1
        c['paper_pnl_total'] = round(c['paper_pnl_total'] +
                                     (e.get('paper_pnl') or 0), 2)
    return out

def proof_backfill_regimes(record, regime_runs):
    """Tag legacy entries (recorded before cell tags existed) with the
    swing regime in force at their ts_open, from history.json runs —
    (run_datetime, regime_token) pairs, any order. This is recorded
    fact, not reconstruction: the regime the engine itself published
    at that hour. Entries older than every run keep no tag (their
    cells read 'unknown'); confidence was never stored per record and
    is NOT backfilled — legacy bands stay 'na'. Idempotent: only
    entries missing a regime are touched. Returns the count tagged."""
    runs = sorted((ts, tok) for ts, tok in regime_runs if ts and tok)
    n = 0
    for e in record:
        if e.get('regime'):
            continue
        opened = _iso_parse(e.get('ts_open') or '')
        if not opened:
            continue
        tag = None
        for ts, tok in runs:
            if ts <= opened:
                tag = PROOF_REGIME_TAGS.get(tok)
            else:
                break
        if tag:
            e['regime'] = tag
            n += 1
    return n

def _proof_trim(record, cap=100):
    """Cap the published ledger at the newest 100 entries, dropping the
    oldest CLOSED entries first so open calls are never silently lost.
    Returns (trimmed, dropped): the caller folds `dropped` into the
    cumulative per-cell counters (proof_roll_cumulative) BEFORE the
    trimmed list is published, so a dropped entry's graded outcome is
    never lost to the all-time record."""
    if len(record) <= cap:
        return record, []
    drop = set()
    for i in range(len(record) - 1, -1, -1):  # newest-first: walk oldest back
        if len(record) - len(drop) <= cap:
            break
        if record[i].get('status') != 'open':
            drop.add(i)
    kept = [e for i, e in enumerate(record) if i not in drop]
    trimmed = kept[:cap]
    dropped = [e for i, e in enumerate(record) if i in drop] + kept[cap:]
    return trimmed, dropped


def _scan_coin(c, charts_by_sym, fund_map, fund_hist, oi_hist, macro_snap,
               btc_chg24, econ_feed, run_ts, tradable_on, blocked,
               min_mcap=100_000_000):
    """One coin of the signal scan: hourly chart (24h) -> features ->
    balance/econ (display only) -> score -> thesis. Returns the signal
    dict or None (filtered, failed fetch, weak score, or fee floor).
    Shared by the live scan (top SIGNAL_UNIVERSE_N) and the shadow
    scan (ranks SHADOW_UNIVERSE_START..SHADOW_UNIVERSE_END) so the two
    universes are scored identically; the caller decides what a
    returned signal becomes (live pick vs shadow-graded candidate).
    min_mcap is the market-cap floor for THIS scan: $100M for live
    (Nic's live floor, unchanged), SHADOW_MCAP_FLOOR ($50M, Nic
    2026-11-05) for the shadow scan — the shadow universe is wider
    than the live one by design, and the floor is the only difference
    between the two scans.
    charts_by_sym is mutated: the chart is retained for Proof Book
    grading (hourly points). Fail-open throughout: a failed fetch or
    econ pack just skips the coin, never the run."""
    cid = c['id']
    sym = (c['symbol'] or '').upper()
    if sym in blocked:
        return None
    mcap = c.get('market_cap') or 0
    if mcap < min_mcap:
        return None
    # Hourly chart (24h)
    chart = fetch_json(f'https://api.coingecko.com/api/v3/coins/{cid}/market_chart'
        '?vs_currency=usd&days=1&interval=hourly')
    if not chart or not chart.get('prices'):
        return None
    f = hourly_features(chart['prices'])
    if not f:
        return None
    charts_by_sym[sym] = chart['prices']
    # Auction-market balance: coiled-range read from the same 24h
    # hourly chart (prices + volumes ride free in one response) and
    # the 7d sparkline already in the markets payload. Display only —
    # never touches the score above.
    _closes = [p[1] for p in chart['prices'] if p and p[1] is not None]
    _vols = [v[1] for v in (chart.get('total_volumes') or [])
             if v and v[1] is not None]
    _spark7 = [p for p in ((c.get('sparkline_in_7d') or {}).get('price') or [])
               if p is not None]
    bs, bs_parts = balance_score(_closes, volumes=_vols, closes_7d=_spark7)
    # Milestone 2 econ pack (display only — never a buy input). Coil
    # delta = compression now vs prior 24h, from the 7d sparkline.
    econ_display = None
    if econ_feed is not None:
        try:
            _coil = None
            if _spark7 and len(_spark7) >= 48:
                _b_now, _ = balance_score(_spark7[-24:], closes_7d=_spark7)
                _b_prev, _ = balance_score(_spark7[-48:-24], closes_7d=_spark7)
                if _b_now is not None and _b_prev is not None:
                    _coil = round(_b_now - _b_prev, 3)
            _frow = (fund_map or {}).get(sym) or {}
            _, econ_display = econ_feed.compute_econ({
                'mode': 'full', 'asset': sym, 'price': _closes[-1],
                'closes': _closes, 'volumes': _vols,
                'funding_score': _frow.get('score'),
                'fund_hist': fund_hist, 'oi_hist': oi_hist,
                'chg_1h': f.get('chg_1h'), 'now_ts': run_ts,
                'coil_delta': _coil, 'macro_snap': macro_snap,
                'btc_chg24': btc_chg24, 'fg_now': None,
            })
        except Exception as e:
            print(f'  [warn] econ pack failed for {sym}: {e}')
            econ_display = None
    score = score_intraday(f)
    conf = min(95, max(40, 50 + abs(score) * 3))
    # Only signals with meaningful score
    if abs(score) < 3:
        return None
    thesis = build_intraday_thesis(sym, f, score)
    if thesis is None:
        return None  # fee floor: target can't clear round-trip costs
    return {
        'asset': sym, 'score': round(score, 2), 'confidence': round(conf),
        'direction': thesis['direction'], 'thesis': thesis,
        'name': c.get('name') or sym,
        'info_url': f'https://www.coingecko.com/en/coins/{cid}',
        'tradable': tradable_on(sym),
        'timestamp': datetime.now(timezone.utc).isoformat(),
        # Auction-market balance (display only — never a buy input)
        'balance_score': bs, 'balance_parts': bs_parts,
        'coiled': bool(bs is not None and bs >= COILED_THRESHOLD),
        # Milestone 2 econ pack (display only — never a buy input)
        'econ': econ_display,
    }


def main():
    global DATA_DIR, _GATE_EVENTS
    if CHECK_MODE:
        # Redirect every data write to a throwaway dir — reads of
        # data/intraday.json fall back to their graceful empty defaults.
        DATA_DIR = tempfile.mkdtemp(prefix='intraday-check-')
        print(f'=== CHECK MODE: no writes to the repo data dir (using {DATA_DIR}) ===')
    print('INTRADAY: hourly signals...')
    # Fresh event list per run (main() may be called twice in-process in
    # tests).
    _GATE_EVENTS = []
    # Circuit-breaker cross-run memory (Gate v3.0): rejection events
    # ride inside the committed feed like accum/oi/funding history.
    prior_gate_events = []
    try:
        with open(os.path.join(DATA_DIR, 'intraday.json')) as f:
            prior_gate_events = (json.load(f) or {}).get('gate_events') or []
    except Exception:
        pass
    # Cross-run memory for the accumulation detector: the engine itself is
    # stateless, but data/intraday.json is committed every run, so the
    # observation history rides inside it (see accum_history below).
    prior_history = {}
    try:
        with open(os.path.join(DATA_DIR, 'intraday.json')) as f:
            prior_history = (json.load(f) or {}).get('accum_history') or {}
    except Exception:
        pass
    # Feed #3 cross-run memory: OI history rides in the feed too.
    prior_oi_history = {}
    try:
        with open(os.path.join(DATA_DIR, 'intraday.json')) as f:
            prior_oi_history = (json.load(f) or {}).get('oi_history') or {}
    except Exception:
        pass
    # OKX derivatives track, Phase 1: OI-USD history rides in the feed
    # the same way (last 30 hourly points per coin).
    prior_okx_oi_history = {}
    try:
        with open(os.path.join(DATA_DIR, 'intraday.json')) as f:
            prior_okx_oi_history = \
                (json.load(f) or {}).get('okx_oi_history') or {}
    except Exception:
        pass
    # DeFiLlama stablecoin/TVL track, Phase 1: the USDT+USDC supply
    # history (no per-asset history endpoint exists — the store warms
    # up over the first week) and the day-cache for the heavy history
    # endpoints (stablecoin charts + TVL history, once a day).
    prior_defillama_history = {}
    prior_defillama_cache = {}
    try:
        with open(os.path.join(DATA_DIR, 'intraday.json')) as f:
            _prior = json.load(f) or {}
            prior_defillama_history = _prior.get('defillama_history') or {}
            prior_defillama_cache = _prior.get('defillama_cache') or {}
    except Exception:
        pass
    # Milestone 2 cross-run memory: funding-score history (for fund_mom) and
    # the day-cached macro snapshot (for real_yield / fed_stance).
    prior_fund_history = {}
    prior_macro_snap = None
    try:
        with open(os.path.join(DATA_DIR, 'intraday.json')) as f:
            _prior = json.load(f) or {}
            prior_fund_history = _prior.get('funding_history') or {}
            prior_macro_snap = _prior.get('macro_snapshot')
    except Exception:
        pass
    # Same persistence trick for the 📒 Proof Book ledger (see proof block).
    prior_proof = []
    prior_cumulative = {}
    try:
        with open(os.path.join(DATA_DIR, 'intraday.json')) as f:
            _prior = (json.load(f) or {}).get('proof') or {}
            prior_proof = _prior.get('record') or []
            prior_cumulative = _prior.get('cumulative') or {}
    except Exception:
        pass
    # Price-action pattern track, Phase 1: OHLC history + per-pattern
    # cumulative counters ride in the feed the same way.
    prior_pattern_candles = {}
    prior_pattern_cumulative = {}
    try:
        with open(os.path.join(DATA_DIR, 'intraday.json')) as f:
            _prior = json.load(f) or {}
            prior_pattern_candles = _prior.get('pattern_candles') or {}
            prior_pattern_cumulative = \
                (_prior.get('proof') or {}).get('pattern_cumulative') or {}
    except Exception:
        pass
    # Cycle research track, Phase 1/2: daily BTC closes, the
    # dominance/FNG day-cache, the dominance history (30d lookback),
    # and the separate cycle ledger + cumulative counters.
    prior_cycle_btc_daily = []
    prior_cycle_cache = {}
    prior_cycle_dom_history = {}
    prior_cycle_record = []
    prior_cycle_cumulative = {}
    try:
        with open(os.path.join(DATA_DIR, 'intraday.json')) as f:
            _prior = json.load(f) or {}
            prior_cycle_btc_daily = _prior.get('cycle_btc_daily') or []
            prior_cycle_cache = _prior.get('cycle_cache') or {}
            prior_cycle_dom_history = _prior.get('cycle_dom_history') or {}
            prior_cycle_record = \
                (_prior.get('proof') or {}).get('cycle_record') or []
            prior_cycle_cumulative = \
                (_prior.get('proof') or {}).get('cycle_cumulative') or {}
    except Exception:
        pass
    # Manipulation research track, Phase 1/2: the separate CMI ledger
    # + cumulative counters. The CMI feed reads the shared
    # pattern_candles store — no prior state of its own to load.
    prior_cmi_record = []
    prior_cmi_cumulative = {}
    try:
        with open(os.path.join(DATA_DIR, 'intraday.json')) as f:
            _prior = json.load(f) or {}
            prior_cmi_record = \
                (_prior.get('proof') or {}).get('cmi_record') or []
            prior_cmi_cumulative = \
                (_prior.get('proof') or {}).get('cmi_cumulative') or {}
    except Exception:
        pass
    # Shadow universe expansion, Phase 1: the separate shadow ledger
    # + cumulative counters (same pattern as cycle/CMI — the shadow
    # rows must not flood the 100-entry main ledger).
    prior_shadow_record = []
    prior_shadow_cumulative = {}
    try:
        with open(os.path.join(DATA_DIR, 'intraday.json')) as f:
            _prior = json.load(f) or {}
            prior_shadow_record = \
                (_prior.get('proof') or {}).get('shadow_record') or []
            prior_shadow_cumulative = \
                (_prior.get('proof') or {}).get('shadow_cumulative') or {}
    except Exception:
        pass
    # Stocks research track, Phase 1: the separate stock ledger +
    # cumulative counters (same pattern as the shadow ledger — stock
    # rows must not flood the 100-entry main ledger).
    prior_stock_record = []
    prior_stock_cumulative = {}
    try:
        with open(os.path.join(DATA_DIR, 'intraday.json')) as f:
            _prior = json.load(f) or {}
            prior_stock_record = \
                (_prior.get('proof') or {}).get('stock_record') or []
            prior_stock_cumulative = \
                (_prior.get('proof') or {}).get('stock_cumulative') or {}
    except Exception:
        pass
    # Coins not tradable on major US exchanges (delisted / regulatory)
    BLOCKED = {'XMR',  # Monero — delisted from Coinbase
               # Stablecoins are cash, not trades — never pick or alert them (USDT incident 2026-10-23)
               'USDT', 'USDC', 'DAI', 'TUSD', 'FDUSD', 'PYUSD', 'USDP', 'GUSD', 'FRAX', 'USDD', 'LUSD', 'SUSD', 'USDE', 'CRVUSD', 'EURC',
               # Pegged / money-market tokens that leaked into the 2026-10-25 longs (all ~$1, no upside)
               'RLUSD', 'U', 'EURSAFO', 'EUTBL', 'THBILL',
               # Second wave, same morning: SG-FORGE stables (EURCV/USDCV),
               # Origin Dollar (OUSD), Web 3 Dollar (USD3)
               'EURCV', 'USDCV', 'OUSD', 'USD3'}
    # Exchange listings — which coins Nic can actually trade
    listings_cb, listings_cdc = set(), set()
    try:
        cb_prods = fetch_json('https://api.exchange.coinbase.com/products') or []
        listings_cb = {p['base_currency'] for p in cb_prods
            if p.get('quote_currency') == 'USD' and p.get('status') == 'online' and p.get('base_currency')}
    except Exception:
        pass
    try:
        cdc = fetch_json('https://api.crypto.com/exchange/v1/public/get-instruments') or {}
        listings_cdc = {i['base_ccy'] for i in (cdc.get('result', {}).get('data', []) or [])
            if i.get('inst_type') == 'CCY_PAIR' and i.get('quote_ccy') in ('USD', 'USDT') and i.get('base_ccy')}
    except Exception:
        pass
    def tradable_on(sym):
        out = []
        if sym in listings_cb: out.append('coinbase')
        if sym in listings_cdc: out.append('cryptocom')
        return out
    # Broad universe for the movers scan: top 1,000 by market cap (4 pages).
    # Page 1 doubles as the top-50 universe for the signal scan below.
    universe = []
    for page in range(1, 5):
        mkts_page = fetch_json('https://api.coingecko.com/api/v3/coins/markets'
            f'?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}'
            '&price_change_percentage=1h,24h&sparkline=true')
        if not mkts_page:
            break
        universe.extend(mkts_page)
    if not universe:
        print('  FAILED: no market data')
        return
    # Core market data for this run is in hand — stamp the pull time so the
    # frontend can show "Data pulled … ET" for cross-referencing.
    data_pulled_at = datetime.now(timezone.utc).isoformat()
    # Hourly volatility per coin from the sparklines embedded in the
    # markets payload — zero extra calls. Mover/watch plans read vol from
    # this map (median fallback included); per-coin chart fetches are NOT
    # used for plans (free-tier throttling killed them, 2026-10-20).
    vol_by_id, median_vol = sparkline_vol_map(universe)
    # RSI per coin from the same sparkline series — feeds the runner/stretch
    # gate in build_mover_plan (missing/short series -> None -> gate fails
    # open on RSI, momentum gates still bind).
    rsi_by_id = {}
    for c in universe:
        r = sparkline_rsi((c.get('sparkline_in_7d') or {}).get('price'))
        if r is not None:
            rsi_by_id[c.get('id')] = r
    # Exchange-tradable, $100M+ universe used by the mover/watch scans below.
    # Defined here (before the feeds) so Feed #2/#3 can map against it —
    # it was previously defined after them, and Python scoping turned the
    # funding call into a silent no-op every run (fail-open swallowed it).
    scanned = [c for c in universe
               if (c.get('market_cap') or 0) >= 100_000_000
               and (c['symbol'] or '').upper() not in BLOCKED]
    # Feed #2: Binance funding rates — crowded-long detection. One bulk
    # public call; any failure yields {} and the engine runs unchanged.
    fund_map = {}
    if funding_feed is not None:
        try:
            fund_map = funding_feed.funding_by_asset(
                {(c['symbol'] or '').upper() for c in scanned}, fetch_json)
        except Exception as e:
            # Fail open, but SAY so — a silent swallow hid a dead feed
            # for days (scoping bug, fixed 2026-10-31).
            print(f'  [warn] funding feed failed: {e}')
            fund_map = {}
    # Feed #3: open-interest flow — what traders did in the last hour.
    # OI rides free inside the Feed #2 call; history persists in the feed.
    run_ts = datetime.now(timezone.utc).timestamp()
    oi_hist = prior_oi_history
    if oi_flow is not None:
        try:
            oi_hist = oi_flow.update_oi_history(prior_oi_history, fund_map,
                                                run_ts)
        except Exception:
            oi_hist = prior_oi_history
    # Milestone 2: funding-score history (for the fund_mom predecessor).
    # Same persistence trick: {ASSET: [[ts, score], ...]}, last 30.
    fund_hist = {a: list(pts) for a, pts in prior_fund_history.items()}
    for _a, _row in (fund_map or {}).items():
        try:
            _s = float((_row or {}).get('score'))
        except (TypeError, ValueError):
            continue
        _pts = fund_hist.get(_a, [])
        _pts.append([run_ts, round(_s, 3)])
        fund_hist[_a] = _pts[-30:]
    # Milestone 2: macro snapshot (FRED keyless CSVs, day-cached, fail-open).
    # The official FRED API needs a key Nic doesn't have; the keyless chart
    # CSV is the same endpoint the Economy page fetches in-browser.
    macro_snap = prior_macro_snap
    try:
        _today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        if (econ_feed is not None and
                (not isinstance(macro_snap, dict) or macro_snap.get('asof') != _today)):
            _fresh = econ_feed.refresh_macro_snapshot(macro_snap)
            if _fresh is not None and _fresh is not macro_snap:
                macro_snap = _fresh
                print(f"  [econ] macro snapshot refreshed (asof {_fresh.get('asof')})")
    except Exception as e:
        print(f'  [warn] macro refresh failed: {e}')
    # BTC 24h trend for the liquidity-regime proxy (first BTC/XBT in universe).
    btc_chg24 = None
    for _c in universe:
        if (_c.get('symbol') or '').upper() in ('BTC', 'XBT'):
            btc_chg24 = (_c.get('price_change_percentage_24h_in_currency')
                         if _c.get('price_change_percentage_24h_in_currency') is not None
                         else _c.get('price_change_percentage_24h'))
            break
    mkts = universe[:SIGNAL_UNIVERSE_N]
    # Market lookup for Proof Book grading — universe is market-cap
    # ordered, so the first occurrence of a symbol is its biggest coin.
    mkt_by_sym = {}
    for c in universe:
        s = (c['symbol'] or '').upper()
        if s and s not in mkt_by_sym:
            mkt_by_sym[s] = {'price': c.get('current_price'),
                             'high_24h': c.get('high_24h'),
                             'low_24h': c.get('low_24h')}

    signals = []
    charts_by_sym = {}  # retained for Proof Book grading (hourly points)
    for c in mkts:
        sig = _scan_coin(c, charts_by_sym, fund_map, fund_hist, oi_hist,
                         macro_snap, btc_chg24, econ_feed, run_ts,
                         tradable_on, BLOCKED)
        if sig:
            signals.append(sig)

    signals.sort(key=lambda s: -abs(s['score']))
    longs = [s for s in signals if s['direction'] == 'LONG'][:5]
    shorts = [s for s in signals if s['direction'] == 'SHORT'][:5]

    # --- Shadow universe expansion (Phase 1): ranks 201-500 by market
    # cap, scanned with the same _scan_coin work AFTER the live scan,
    # so live results are in hand before any throttling risk. Shadow
    # signals NEVER join longs/shorts (no cards, no bar, no alerts, no
    # pattern symbols) — the top 5 each way are graded in the Proof
    # Book only, in a separate shadow ledger. Fail-open: a throttled
    # shadow scan just means fewer shadow coins that run.
    shadow_signals = []
    shadow_coins_scanned = 0
    for c in universe[SHADOW_UNIVERSE_START:SHADOW_UNIVERSE_END]:
        _sym = (c.get('symbol') or '').upper()
        if _sym in charts_by_sym:
            # Same symbol already scanned live (bigger coin wins the
            # symbol) — the live chart already covers it for grading.
            continue
        sig = _scan_coin(c, charts_by_sym, fund_map, fund_hist, oi_hist,
                         macro_snap, btc_chg24, econ_feed, run_ts,
                         tradable_on, BLOCKED,
                         min_mcap=SHADOW_MCAP_FLOOR)
        if _sym in charts_by_sym:
            shadow_coins_scanned += 1
        if sig:
            shadow_signals.append(sig)
    shadow_signals.sort(key=lambda s: -abs(s['score']))
    shadow_longs = [s for s in shadow_signals if s['direction'] == 'LONG'][:5]
    shadow_shorts = [s for s in shadow_signals if s['direction'] == 'SHORT'][:5]
    print(f'  Shadow scan: {shadow_coins_scanned} coins, '
          f'{len(shadow_longs)} shadow longs, {len(shadow_shorts)} shadow shorts')

    # --- Stocks research track, Phase 1 (display-only + shadow
    # grading): 30 liquid US large-caps, Yahoo Finance v8 chart API
    # (keyless, verified). Runs ONLY during US market hours
    # (9:30-16:00 ET, Mon-Fri) — outside hours the scan is skipped
    # entirely, no signals from stale data. Stock signals NEVER join
    # longs/shorts (no cards, no bar, no alerts) — the top 5 each way
    # are graded in the Proof Book only, in a separate stock ledger.
    # The stock thesis uses the stock shadow fee tier (commission-free,
    # 0.5% net floor) — see stock_feed; the live crypto gates are
    # untouched. Fail-open: a throttled Yahoo fetch just means fewer
    # stock signals that run.
    stock_longs, stock_shorts = [], []
    stock_symbols_scanned = 0
    stock_diag = {'market_open': False, 'scanned': 0, 'signals': 0,
                  'skipped': None}
    if stock_feed is not None:
        try:
            _mkt_open = stock_feed.us_market_open_now()
        except Exception:
            _mkt_open = False
        stock_diag['market_open'] = _mkt_open
        if _mkt_open:
            _stock_charts = {}
            # Backstop first: if the first symbol's latest candle is
            # >2h old the market is not really open (holiday list
            # stale, half day) — skip the whole scan rather than
            # scoring stale data.
            _probe = stock_feed.fetch_stock_candles(
                stock_feed.STOCK_UNIVERSE[0])
            if _probe:
                _age_h = (datetime.now(timezone.utc).timestamp()
                          - _probe['closes'][-1][0] / 1000) / 3600
                if _age_h > 2:
                    stock_diag['skipped'] = 'stale_candles'
                    _probe = None
            if _probe:
                _stock_charts[stock_feed.STOCK_UNIVERSE[0]] = _probe
                stock_symbols_scanned += 1
                for _sym in stock_feed.STOCK_UNIVERSE[1:]:
                    _cd = stock_feed.fetch_stock_candles(_sym)
                    if not _cd:
                        continue
                    _stock_charts[_sym] = _cd
                    stock_symbols_scanned += 1
            if _stock_charts:
                _stock_signals = []
                for _sym, _cd in _stock_charts.items():
                    _closes = _cd['closes']
                    _vols = [v for _, v in _cd['volumes']]
                    _stock_sig = stock_feed.scan_stock_with(
                        _sym, _closes, hourly_features, score_intraday,
                        balance_score, volumes=_vols, closes_7d=_closes)
                    if _stock_sig:
                        _stock_signals.append(_stock_sig)
                    # Keep the chart for grading (open stock entries
                    # grade on this same trading-hours path).
                    charts_by_sym[_sym] = _closes
                    mkt_by_sym[_sym] = {'price': _cd['last']}
                _stock_signals.sort(key=lambda s: -abs(s['score']))
                stock_longs = [s for s in _stock_signals
                               if s['direction'] == 'LONG'][:stock_feed.STOCK_TOP_N]
                stock_shorts = [s for s in _stock_signals
                                if s['direction'] == 'SHORT'][:stock_feed.STOCK_TOP_N]
                stock_diag['scanned'] = stock_symbols_scanned
                stock_diag['signals'] = len(_stock_signals)
            print(f'  Stock scan: {stock_symbols_scanned} symbols, '
                  f'{len(stock_longs)} stock longs, '
                  f'{len(stock_shorts)} stock shorts')
        else:
            stock_diag['skipped'] = 'market_closed'
            print('  Stock scan: skipped (market closed)')

    # --- Movers scan (two tiers) over the full top-1,000 universe ---
    # 🚀 MOVER: already moving — |1h| >= 5% or |24h| >= 10%, mcap >= $100M.
    # 👀 WATCH: early signs — |1h| >= 3% AND (volume surge OR fresh inflection).
    #   Volume-surge approximation: CoinGecko gives only current 24h volume
    #   (no per-coin baseline), so "surge" = the coin's volume/market-cap
    #   ratio sits in the TOP QUARTILE of the scanned universe — unusually
    #   heavy trading relative to the coin's own size.
    #   Fresh inflection: 1h move >= 3% while the 24h trend is still against
    #   it (24h <= 0 for an up-turn, >= 0 for a down-turn) — the turn is
    #   starting before the daily numbers confirm it.
    def mover_fields(c, direction):
        out = {
            'id': c['id'],
            'asset': (c['symbol'] or '').upper(),
            'name': c.get('name') or (c['symbol'] or '').upper(),
            'price': c.get('current_price') or 0,
            'chg_1h': round(c.get('price_change_percentage_1h_in_currency') or 0, 2),
            'chg_24h': round(c.get('price_change_percentage_24h_in_currency')
                             if c.get('price_change_percentage_24h_in_currency') is not None
                             else (c.get('price_change_percentage_24h') or 0), 2),
            'market_cap': c.get('market_cap') or 0,
            'direction': direction,
            'tradable': tradable_on((c['symbol'] or '').upper()),
            'info_url': f"https://www.coingecko.com/en/coins/{c['id']}",
        }
        # Auction-market balance from the 7d sparkline (no hourly volumes on
        # the markets payload — 2-component read, weights renormalize).
        # Display only — never a buy input.
        _spark7 = [p for p in ((c.get('sparkline_in_7d') or {}).get('price') or [])
                   if p is not None]
        _bs, _bs_parts = balance_score(_spark7[-24:], closes_7d=_spark7)
        out['balance_score'] = _bs
        out['balance_parts'] = _bs_parts
        out['coiled'] = bool(_bs is not None and _bs >= COILED_THRESHOLD)
        # Milestone 2 econ pack, TPO mode (closes only, no hourly volumes —
        # time-at-price zones instead of volume-weighted). Display only.
        out['econ'] = None
        if econ_feed is not None:
            try:
                _coil_m = None
                if _spark7 and len(_spark7) >= 48:
                    _bn, _ = balance_score(_spark7[-24:], closes_7d=_spark7)
                    _bp, _ = balance_score(_spark7[-48:-24], closes_7d=_spark7)
                    if _bn is not None and _bp is not None:
                        _coil_m = round(_bn - _bp, 3)
                _frow_m = (fund_map or {}).get(out['asset']) or {}
                _, _econ_m = econ_feed.compute_econ({
                    'mode': 'tpo', 'asset': out['asset'], 'price': out['price'],
                    'closes': _spark7,
                    'funding_score': _frow_m.get('score'),
                    'fund_hist': fund_hist, 'oi_hist': oi_hist,
                    'chg_1h': out.get('chg_1h'), 'now_ts': run_ts,
                    'coil_delta': _coil_m, 'macro_snap': macro_snap,
                    'btc_chg24': btc_chg24, 'fg_now': None,
                })
                out['econ'] = _econ_m
            except Exception:
                out['econ'] = None
        return out
    # `scanned` is defined above (before the feeds) so the funding/OI
    # calls see it; it anchors the ratio quartile and the scan entries.
    ratios = sorted((c.get('total_volume') or 0) / c['market_cap']
                    for c in scanned if c.get('market_cap'))
    vol_q3 = ratios[int(len(ratios) * 0.75)] if ratios else float('inf')
    vol_q90 = ratios[int(len(ratios) * 0.90)] if ratios else float('inf')
    vol_med = ratios[len(ratios) // 2] if ratios else float('inf')
    # Accumulation history upkeep: append this run's observation for every
    # scanned coin, keep the last 6, prune coins not in the latest scan.
    now_iso = datetime.now(timezone.utc).isoformat()
    new_history = {}
    for c in scanned:
        sym = (c['symbol'] or '').upper()
        obs = {'t': now_iso,
               'price': _sig(c.get('current_price') or 0),
               'vol24h': _sig(c.get('total_volume') or 0),
               'vol_ratio': _sig((c.get('total_volume') or 0) / c['market_cap'])}
        new_history[sym] = ((prior_history.get(sym) or []) + [obs])[-6:]
    movers, watch = [], []
    for c in scanned:
        chg1 = c.get('price_change_percentage_1h_in_currency') or 0
        chg24 = c.get('price_change_percentage_24h_in_currency')
        if chg24 is None:
            chg24 = c.get('price_change_percentage_24h') or 0
        if abs(chg1) >= 5 or abs(chg24) >= 10:
            direction = ('UP' if chg24 > 0 else 'DOWN') if abs(chg24) >= abs(chg1) \
                        else ('UP' if chg1 > 0 else 'DOWN')
            movers.append(mover_fields(c, direction))
            continue  # a mover is never also a watch item
        vol_ratio = (c.get('total_volume') or 0) / c['market_cap'] if c.get('market_cap') else 0
        listed = False
        if abs(chg1) >= 3:
            surge = vol_ratio >= vol_q3
            inflection = (chg1 >= 3 and chg24 <= 0) or (chg1 <= -3 and chg24 >= 0)
            if surge or inflection:
                w = mover_fields(c, 'UP' if chg1 > 0 else 'DOWN')
                w['watch_reason'] = ('volume surge' if surge else '') + \
                    (' + ' if surge and inflection else '') + \
                    ('fresh inflection' if inflection else '')
                watch.append(w)
                listed = True
        # Strong day (additive): +8%+ over 24h in steps, still moving the
        # same way this hour, at least median volume — CVX case 2026-10-20:
        # +9.24% 24h with no +3% hour tripped neither movers nor watch.
        if not listed and strong_day(chg24, chg1, vol_ratio, vol_med):
            w = mover_fields(c, 'UP' if chg24 > 0 else 'DOWN')
            w['watch_reason'] = (f"Strong day building — {chg24:+.0f}% in 24h and still "
                                 f"{'rising' if chg24 > 0 else 'falling'} this hour")
            watch.append(w)
    movers.sort(key=lambda m: -abs(m['chg_24h']))
    watch.sort(key=lambda m: -abs(m['chg_1h']))
    movers, watch = movers[:10], watch[:10]
    print(f'  Movers scan: {len(movers)} movers, {len(watch)} watching (of {len(scanned)} scanned)')

    # --- Trade plans (Nic 2026-10-20: "Where is buy target, sell target and
    # stop loss target?") ---
    # UP movers get a LONG plan from build_mover_plan. DOWN movers get no buy
    # plan (Nic trades long-only) plus a note. Watch items: only the top 3 by
    # |chg_1h| (the list is already sorted that way) get plans the same way;
    # the rest carry plan: null with no note — the plan appears when they
    # graduate to movers.
    def attach_plan(m, with_note=True):
        if m['direction'] != 'UP':
            m['plan'] = None
            if with_note:
                m['plan_note'] = 'Falling mover — no buy plan (long-only). Watch for reversal.'
            return
        vol = vol_by_id.get(m.get('id')) or median_vol
        fentry = fund_map.get(m.get('asset')) or {}
        oflow = None
        if oi_flow is not None:
            try:
                oflow = oi_flow.flow_for(m.get('asset'), oi_hist,
                                         m.get('chg_1h'), run_ts)
            except Exception:
                oflow = None
        m['plan'] = build_mover_plan(m.get('price') or 0, vol,
                                     chg_1h=m.get('chg_1h'),
                                     chg_24h=m.get('chg_24h'),
                                     rsi=rsi_by_id.get(m.get('id')),
                                     funding_score=fentry.get('score'),
                                     funding_ann_pct=fentry.get('ann_pct'),
                                     oi_score=(oflow or {}).get('score'),
                                     oi_chg_1h_pct=(oflow or {}).get('oi_chg_1h_pct'))
        if fentry:
            m['funding'] = {'ann_pct': fentry['ann_pct'],
                            'score': fentry['score']}
        if oflow:
            m['oi_flow'] = oflow
        if m['plan'] is None and with_note:
            if vol and not clears_fee_floor(vol):
                m['plan_note'] = ('Target below fee floor — '
                                  'no plan at this fee tier.')
            else:
                m['plan_note'] = 'Plan unavailable — price feed hiccup.'
    for m in movers:
        attach_plan(m)
    for w in watch[:3]:
        attach_plan(w)
    for w in watch[3:]:
        w['plan'] = None

    # --- Pre-move accumulation (🔭 Setting Up) ---
    # Fires BEFORE the jump: volume building while price is still quiet.
    # Coins already flagged as movers are excluded (they already moved).
    # History warms up over the first ~2-3 hourly runs after deploy —
    # until a coin has >= 3 observations spanning >= 2h it cannot flag.
    mover_syms = {m['asset'] for m in movers}
    now_dt = datetime.now(timezone.utc)
    accum = []
    for c in scanned:
        sym = (c['symbol'] or '').upper()
        if sym in mover_syms:
            continue
        chg1 = c.get('price_change_percentage_1h_in_currency') or 0
        vol_ratio = (c.get('total_volume') or 0) / c['market_cap'] if c.get('market_cap') else 0
        res = accum_reason(new_history.get(sym) or [], vol_ratio, vol_q90, chg1, now_dt)
        if not res:
            continue
        reason, growth, drift = res
        a = mover_fields(c, 'UP' if chg1 >= 0 else 'DOWN')
        a.pop('direction', None)  # accumulation has no direction call yet
        a.update({
            'chg_1h': round(chg1, 2),
            'vol_growth_pct': growth,
            'price_drift_pct': drift,
            'vol_ratio': _sig(vol_ratio),
            'watch_reason': reason,
        })
        # Trigger plan from the same sparkline vol map as mover plans
        # (median fallback included); plan stays None when no
        # volatility exists anywhere or when the gross target can't
        # clear the fee floor (build_accum_plan gates both) — the
        # alert then shows WATCH.
        a['plan'] = build_accum_plan(a['price'],
                                     vol_by_id.get(c.get('id')) or median_vol,
                                     c.get('high_24h'))
        accum.append(a)
    accum.sort(key=lambda a: (not a['tradable'], -a['vol_growth_pct']))
    accum = accum[:8]
    print(f'  Accumulation scan: {len(accum)} setting up')

    # --- Price-action pattern track, Phase 1 (display-only research) ---
    # Kraken public OHLC (keyless, interval=60) for the bounded
    # universe: assets with active plans + movers + watch + setting-up,
    # capped at 30 symbols/run. One pull, two consumers: the candles
    # land in intraday.json['pattern_candles'] for the manipulation
    # track to read without a second fetch. Fail-open throughout: no
    # candles -> no patterns -> no card lines, no exceptions — and
    # this block NEVER records a gate event (diagnostic-only; it must
    # never trip the circuit breaker).
    pattern_candles = prior_pattern_candles
    patterns = {}
    pattern_diag = {'symbols_requested': 0, 'symbols_with_candles': 0,
                    'tags': 0, 'error': None}
    if pattern_feed is not None:
        try:
            _syms = []
            for _src in (longs, shorts, movers, watch, accum):
                for _it in _src:
                    _s = (_it or {}).get('asset')
                    if _s and _s not in _syms:
                        _syms.append(_s)
            _syms = _syms[:pattern_feed.MAX_SYMBOLS_PER_RUN]
            pattern_diag['symbols_requested'] = len(_syms)
            _fetched = pattern_feed.fetch_ohlc(fetch_json, _syms)
            pattern_candles = pattern_feed.update_pattern_candles(
                prior_pattern_candles, _fetched)
            patterns = pattern_feed.detect_for_universe(pattern_candles)
            pattern_diag['symbols_with_candles'] = len(pattern_candles)
            pattern_diag['tags'] = sum(len(v) for v in patterns.values())
            # Display-only tags ride on the cards that carry the asset.
            for _coll in (longs, shorts, movers, watch, accum):
                for _it in _coll:
                    _tg = patterns.get((_it or {}).get('asset'))
                    if _tg:
                        _it['patterns'] = _tg
            print(f"  Patterns: {pattern_diag['tags']} tags on "
                  f"{len(patterns)} assets "
                  f"({pattern_diag['symbols_with_candles']} candle sets)")
        except Exception as e:
            # Diagnostic only — a broken pattern feed degrades to
            # "no patterns", never a dead run, never a gate event.
            pattern_diag['error'] = str(e)[:200]
            patterns = {}
            pattern_candles = prior_pattern_candles
            print(f'  [warn] pattern feed failed (fail-open): {e}')

    # --- OKX derivatives track, Phase 1 (display-only research) ---
    # Second funding/OI venue (Binance 451s / Bybit 403s from here) +
    # the long/short account ratio the engine has never had. Per-coin
    # funding (+z vs its own 33d history), OI (+1h/24h changes from
    # the cross-run store), L/S ratio, and OKX-vs-Kraken funding
    # dispersion. Fail-open throughout: no data -> no OKX section,
    # no exceptions — and this block NEVER records a gate event
    # (diagnostic-only; it must never trip the circuit breaker).
    okx_map = {}
    okx_oi_hist = prior_okx_oi_history
    okx_diag = {'coins_requested': 0, 'coins_with_funding': 0,
                'dispersion': {}, 'error': None}
    if okx_feed is not None:
        try:
            _syms = []
            for _src in (longs, shorts, movers, watch, accum):
                for _it in _src:
                    _s = (_it or {}).get('asset')
                    if _s and _s not in _syms:
                        _syms.append(_s)
            _syms = _syms[:okx_feed.MAX_COINS_PER_RUN]
            okx_diag['coins_requested'] = len(_syms)
            okx_map = okx_feed.fetch_all(fetch_json, _syms)
            okx_oi_hist = okx_feed.update_oi_history(
                prior_okx_oi_history, okx_map, run_ts)
            _disp = okx_feed.dispersion_map(okx_map, fund_map)
            okx_diag['dispersion'] = _disp
            for _asset, _row in okx_map.items():
                _chg = okx_feed.oi_changes(_asset, okx_oi_hist, run_ts)
                if _chg:
                    _row.update(_chg)
                if _asset in _disp:
                    _row['funding_dispersion_pct'] = _disp[_asset]
            okx_diag['coins_with_funding'] = sum(
                1 for r in okx_map.values()
                if (r or {}).get('funding_rate') is not None)
            # Display-only OKX entries ride on the cards that carry
            # the asset (the Picks page reads intraday.json['okx']).
            for _coll in (longs, shorts, movers, watch, accum):
                for _it in _coll:
                    _ok = okx_map.get((_it or {}).get('asset'))
                    if _ok:
                        _it['okx'] = _ok
            print(f"  {okx_feed.pulse_line(okx_map)}")
        except Exception as e:
            # Diagnostic only — a broken OKX feed degrades to
            # "no OKX data", never a dead run, never a gate event.
            okx_diag['error'] = str(e)[:200]
            okx_map = {}
            okx_oi_hist = prior_okx_oi_history
            print(f'  [warn] OKX feed failed (fail-open): {e}')

    # --- Deribit options track, Phase 1 (display-only research) ---
    # The engine's only volatility data: BTC/ETH options from
    # Deribit's public book summary (keyless). 30-day ATM IV,
    # 25-delta put-call skew, put/call OI, and max-pain distance.
    # The DVOL index endpoints 400 from here, so ATM IV is computed
    # from the book (see deribit_feed). Fail-open throughout: no
    # data -> no Deribit section, no exceptions — and this block
    # NEVER records a gate event (diagnostic-only; it must never
    # trip the circuit breaker).
    deribit_map = {}
    deribit_diag = {'currencies_requested': 0, 'currencies_ok': 0,
                    'error': None}
    if deribit_feed is not None:
        try:
            deribit_map = deribit_feed.fetch_all(
                fetch_json, deribit_feed.CURRENCIES)
            deribit_diag['currencies_requested'] = len(
                deribit_feed.CURRENCIES)
            deribit_diag['currencies_ok'] = sum(
                1 for r in deribit_map.values()
                if (r or {}).get('atm_iv') is not None)
            # Display-only Deribit entries ride on the cards that
            # carry the currency (the Picks page reads
            # intraday.json['deribit']).
            for _coll in (longs, shorts, movers, watch, accum):
                for _it in _coll:
                    _db = deribit_map.get((_it or {}).get('asset'))
                    if _db:
                        _it['deribit'] = _db
            print(f"  {deribit_feed.pulse_line(deribit_map)}")
        except Exception as e:
            # Diagnostic only — a broken Deribit feed degrades to
            # "no Deribit data", never a dead run, never a gate event.
            deribit_diag['error'] = str(e)[:200]
            deribit_map = {}
            print(f'  [warn] Deribit feed failed (fail-open): {e}')

    # --- DeFiLlama stablecoin / TVL track, Phase 1 (display-only research) ---
    # The liquidity proxy the engine has never had: circulating
    # stablecoin supply ("dry powder") + DeFi TVL. Current values
    # come from two small calls every run; the 7d/24h changes come
    # from the heavy daily history endpoints, fetched at most once a
    # day and cached (same day-cache trick as the cycle track). The
    # USDT+USDC change rides the cross-run history store — no
    # per-asset history endpoint exists, so it warms up over the
    # first week and reads "no data" until then. Fail-open
    # throughout: no data -> no DeFiLlama section, no exceptions —
    # and this block NEVER records a gate event (diagnostic-only; it
    # must never trip the circuit breaker).
    defillama_snapshot = {'current': {}, 'daily': {}, 'usdt_usdc': {}}
    defillama_hist = prior_defillama_history
    defillama_cache = (prior_defillama_cache
                       if isinstance(prior_defillama_cache, dict) else {})
    defillama_diag = {'daily_cached': False, 'error': None}
    if defillama_feed is not None:
        try:
            _today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            _cur = defillama_feed.fetch_current(fetch_json)
            if defillama_cache.get('date') != _today:
                _daily = defillama_feed.fetch_daily_changes(fetch_json)
                defillama_cache = {'date': _today, 'daily': _daily}
            else:
                defillama_diag['daily_cached'] = True
            defillama_hist = defillama_feed.update_supply_history(
                prior_defillama_history, _cur.get('usdt_usdc'), run_ts)
            _usdt_usdc = defillama_feed.usdt_usdc_changes(
                defillama_hist, run_ts)
            defillama_snapshot = {
                'current': _cur,
                'daily': defillama_cache.get('daily') or {},
                'usdt_usdc': _usdt_usdc}
            defillama_diag['error'] = _cur.get('error')
            print(f"  {defillama_feed.pulse_line(defillama_snapshot)}")
        except Exception as e:
            # Diagnostic only — a broken DeFiLlama feed degrades to
            # "no DeFiLlama data", never a dead run, never a gate event.
            defillama_diag['error'] = str(e)[:200]
            defillama_snapshot = {'current': {}, 'daily': {},
                                  'usdt_usdc': {}}
            defillama_hist = prior_defillama_history
            defillama_cache = (prior_defillama_cache if isinstance(
                prior_defillama_cache, dict) else {})
            print(f'  [warn] DeFiLlama feed failed (fail-open): {e}')

    # --- Cycle research track, Phase 1 (display-only research) ---
    # Today's cycle state: BTC daily closes via Kraken public OHLC
    # (interval=1440, forming day dropped), dominance via CoinGecko
    # /global and Fear & Greed via Alternative.me — both day-cached in
    # intraday.json['cycle_cache'] so the hourly runs make at most one
    # call each per day. The 30-day dominance change reads the cached
    # daily dominance history (warms up over the first month: until a
    # 30-day-old value exists, the DOM band is "no data"). Fail-open
    # throughout: no state -> no pulse, no candidates, no exceptions
    # — and this block NEVER records a gate event (diagnostic-only; it
    # must never trip the circuit breaker).
    cycle_state = None
    cycle_btc_daily = prior_cycle_btc_daily
    cycle_cache = prior_cycle_cache if isinstance(prior_cycle_cache,
                                                  dict) else {}
    cycle_dom_history = prior_cycle_dom_history
    cycle_diag = {'btc_closes': 0, 'dom': None, 'fng': None, 'error': None}
    if cycle_feed is not None:
        try:
            _today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            _fetched_daily = cycle_feed.fetch_btc_daily(fetch_json)
            cycle_btc_daily = cycle_feed.update_btc_daily_store(
                prior_cycle_btc_daily, _fetched_daily)
            cycle_diag['btc_closes'] = len(cycle_btc_daily)
            if cycle_cache.get('date') != _today:
                cycle_cache = {
                    'date': _today,
                    'dominance': cycle_feed.fetch_dominance(fetch_json),
                    'fng': cycle_feed.fetch_fng(fetch_json)}
            _dom_now = cycle_cache.get('dominance')
            _fng_now = cycle_cache.get('fng')
            if _dom_now is not None:
                cycle_dom_history = cycle_feed.update_dom_history(
                    cycle_dom_history, _today, _dom_now)
            _dom_30 = cycle_feed.dom_30d_ago(cycle_dom_history, _today)
            cycle_state = cycle_feed.compute_state(
                _today, cycle_btc_daily, _dom_now, _dom_30, _fng_now)
            cycle_diag['dom'] = _dom_now
            cycle_diag['fng'] = _fng_now
            print(f"  Cycle pulse: {cycle_feed.pulse_line(cycle_state)}")
        except Exception as e:
            # Diagnostic only — a broken cycle feed degrades to
            # "no cycle data", never a dead run, never a gate event.
            cycle_diag['error'] = str(e)[:200]
            cycle_state = None
            cycle_btc_daily = prior_cycle_btc_daily
            print(f'  [warn] cycle feed failed (fail-open): {e}')

    # --- Manipulation research track, Phase 1 (display-only research) ---
    # CMI + SuperTrend per asset, computed from the SHARED
    # pattern_candles store (one Kraken OHLC pull, two consumers —
    # this module makes no fetch of its own). No candles -> no CMI
    # -> no pulse, no candidates, no exceptions — and this block
    # NEVER records a gate event (diagnostic-only; it must never
    # trip the circuit breaker). The CMI > 0.50 exit trigger is a
    # display-only suggestion here; it never auto-closes engine plans.
    cmi = {}
    cmi_pulse = None
    cmi_diag = {'assets': 0, 'confluence': 0, 'traps': 0, 'error': None}
    if cmi_feed is not None:
        try:
            cmi = cmi_feed.detect_for_universe(pattern_candles)
            cmi_pulse = cmi_feed.regime_pulse(cmi)
            cmi_diag['assets'] = len(cmi)
            cmi_diag['confluence'] = sum(
                1 for v in cmi.values() if v.get('confluence'))
            cmi_diag['traps'] = sum(
                1 for v in cmi.values() if v.get('trap'))
            # Display-only CMI entries ride on the cards that carry
            # the asset (the Picks page reads intraday.json['cmi']
            # for the pulse; the per-card copy serves detail pages).
            for _coll in (longs, shorts, movers, watch, accum):
                for _it in _coll:
                    _cm = cmi.get((_it or {}).get('asset'))
                    if _cm:
                        _it['cmi'] = _cm
            print(f"  Manipulation pulse: {cmi_feed.pulse_line(cmi)}")
        except Exception as e:
            # Diagnostic only — a broken CMI feed degrades to
            # "no CMI data", never a dead run, never a gate event.
            cmi_diag['error'] = str(e)[:200]
            cmi = {}
            cmi_pulse = None
            print(f'  [warn] CMI feed failed (fail-open): {e}')

    # --- 📒 Proof Book: grade yesterday's calls before recording today's
    # (a call is never graded in the same run that creates it, so the
    # trailing-24h windows can't credit a pre-entry move) ---
    proof = list(prior_proof)
    cycle_record = list(prior_cycle_record)
    cmi_record = list(prior_cmi_record)
    shadow_record = list(prior_shadow_record)
    stock_record = list(prior_stock_record)
    proof_now = datetime.now(timezone.utc)
    # Legacy entries predate the cell tags: backfill the regime from the
    # swing engine's own run history (data/history.json) — the regime in
    # force at each entry's ts_open. Confidence was never stored per
    # record, so legacy bands stay 'na' rather than being guessed.
    try:
        with open(os.path.join(DATA_DIR, 'history.json')) as f:
            hist_runs = (json.load(f) or {}).get('picks') or []
        regime_runs = [(_iso_parse(r.get('date') or ''), r.get('regime'))
                       for r in hist_runs]
        n_back = proof_backfill_regimes(proof, regime_runs)
        if n_back:
            print(f'  Proof book: backfilled regime on {n_back} legacy entries')
    except Exception:
        pass
    regime_tag = current_regime_tag(proof_now)
    # Grading wants an hourly path for every open call, not just the
    # top-50 signal universe: fetch charts for open entries' assets that
    # the signal scan didn't cover (one fetch per symbol, bounded by the
    # number of open calls). Pattern entries grade over 6h/24h horizons,
    # so theirs fetch days=2 (a 24h horizon needs a 48h lookback); the
    # days=1 chart the signal scan may already hold is refetched.
    id_by_sym = {}
    for c in universe:
        s = (c['symbol'] or '').upper()
        if s and s not in id_by_sym:
            id_by_sym[s] = c['id']
    name_by_sym = {}
    for c in universe:
        s = (c['symbol'] or '').upper()
        if s and s not in name_by_sym:
            name_by_sym[s] = c.get('name') or s
    # Cycle entries grade over 24h/7d horizons, so theirs fetch days=2
    # (24h) or days=8 (7d — a 7d horizon needs an 8-day lookback).
    # CMI entries grade over the same spans (regime 24h/7d, confluence
    # and trap 6h/24h), so their fetches join the same map — days=8
    # for a 7d CMI row, days=2 otherwise. Fetched BEFORE the main
    # loop so the span check below keeps the longer chart instead of
    # overwriting it with a days=1/2 refetch.
    cycle_days_by_sym = {}
    for e in cycle_record:
        if e.get('status') != 'open':
            continue
        _sym = e['asset']
        _need = 8 if e.get('horizon') == '7d' else 2
        cycle_days_by_sym[_sym] = max(cycle_days_by_sym.get(_sym, 0),
                                      _need)
    for e in list(cmi_record):
        if e.get('status') != 'open':
            continue
        _sym = e['asset']
        _need = 8 if e.get('horizon') == '7d' else 2
        cycle_days_by_sym[_sym] = max(cycle_days_by_sym.get(_sym, 0),
                                      _need)
    for _sym, _days in sorted(cycle_days_by_sym.items()):
        if _sym not in id_by_sym:
            continue
        ch = fetch_json(f'https://api.coingecko.com/api/v3/coins/{id_by_sym[_sym]}/market_chart'
                        f'?vs_currency=usd&days={_days}&interval=hourly')
        if ch and ch.get('prices'):
            charts_by_sym[_sym] = ch['prices']
    for e in list(proof) + list(shadow_record):
        # Open calls need a chart for grading; recently-exited calls
        # need one for the after-exit tracker (24h post-exit sampling).
        if e.get('status') != 'open' and \
                not _proof_post_exit_active(e, proof_now):
            continue
        sym = e['asset']
        want_2d = e.get('kind') in PATTERN_KINDS
        days = 2 if want_2d else 1
        if sym in charts_by_sym:
            # Keep a longer chart (e.g. the cycle days=8 fetch) — only
            # refetch when the stored chart is shorter than needed.
            _pts = charts_by_sym[sym]
            _span_h = ((_pts[-1][0] - _pts[0][0]) / 3600000
                       if len(_pts) >= 2 else 0)
            if _span_h >= days * 24 - 1:
                continue
        if sym not in id_by_sym:
            continue
        ch = fetch_json(f'https://api.coingecko.com/api/v3/coins/{id_by_sym[sym]}/market_chart'
                        f'?vs_currency=usd&days={days}&interval=hourly')
        if ch and ch.get('prices'):
            charts_by_sym[sym] = ch['prices']
    proof_grade(proof, mkt_by_sym, charts_by_sym, proof_now)
    proof_grade(cycle_record, mkt_by_sym, charts_by_sym, proof_now)
    # CMI grading: the confluence exit trigger walks a CMI series
    # built from the shared pattern_candles store (fail-open: a
    # missing series degrades to plain fixed-level grading).
    cmi_series_by_sym = {}
    if cmi_feed is not None:
        try:
            _conf_syms = {e['asset'] for e in cmi_record
                          if e.get('status') == 'open'
                          and e.get('kind') == 'cmi_confluence'}
            for _sym in sorted(_conf_syms):
                _series = cmi_feed.cmi_series(
                    (pattern_candles or {}).get(_sym) or [])
                if _series:
                    cmi_series_by_sym[_sym] = _series
        except Exception:
            cmi_series_by_sym = {}
    proof_grade(cmi_record, mkt_by_sym, charts_by_sym, proof_now,
                cmi_by_sym=cmi_series_by_sym)
    # Shadow universe, Phase 1: grade the shadow ledger the same way
    # as live signals (default since-entry path — 'signal_shadow' is
    # not a pattern/cycle/CMI kind, so it grades exactly like 'signal').
    proof_grade(shadow_record, mkt_by_sym, charts_by_sym, proof_now)
    # Stocks research track, Phase 1: grade the stock ledger the same
    # way (default since-entry path — 'stock' is not a pattern/cycle/
    # CMI kind, so it grades exactly like 'signal'). Charts come from
    # Yahoo, not CoinGecko; the scan already stored this run's charts
    # above, this fetch covers open entries whose symbols were not
    # scanned (market closed, failed fetch).
    if stock_feed is not None:
        for e in list(stock_record):
            if e.get('status') != 'open' and \
                    not _proof_post_exit_active(e, proof_now):
                continue
            _sym = e['asset']
            if _sym in charts_by_sym:
                continue
            _cd = stock_feed.fetch_stock_candles(_sym, range_='2d')
            if _cd and _cd.get('closes'):
                charts_by_sym[_sym] = _cd['closes']
                mkt_by_sym[_sym] = {'price': _cd['last']}
    proof_grade(stock_record, mkt_by_sym, charts_by_sym, proof_now)
    n_pe_done = proof_track_post_exit(proof, mkt_by_sym, charts_by_sym,
                                      proof_now)
    if n_pe_done:
        print(f'  Proof book: {n_pe_done} after-exit window(s) completed')
    proof_cands = []
    for s in longs + shorts:
        proof_cands.append(_proof_candidate(
            'signal', s['asset'], s.get('name'), s.get('direction'),
            s.get('thesis'), proof_now, 2,
            tags={'band': conf_band(s.get('confidence')),
                  'regime': regime_tag},
            gate_src={'confidence': s.get('confidence'),
                      'tradable': s.get('tradable'),
                      'news_flag': s.get('news_flag')}))
    for m in movers:
        proof_cands.append(_proof_candidate(
            'mover', m['asset'], m.get('name'), 'LONG',
            m.get('plan'), proof_now, 3,
            tags={'band': 'na', 'regime': regime_tag},
            gate_src={'tradable': m.get('tradable'),
                      'news_flag': m.get('news_flag'),
                      'chg_1h': m.get('chg_1h'),
                      'chg_24h': m.get('chg_24h')}))
    for a in accum:
        # Setting-up scenarios join the book (2026-10-21): every accum
        # entry carrying a trigger plan becomes a graded paper
        # scenario. No plan -> no candidate (never invented numbers).
        proof_cands.append(_proof_candidate(
            'setup', a['asset'], a.get('name'), 'LONG',
            a.get('plan'), proof_now, 3,
            tags={'band': 'na', 'regime': regime_tag},
            gate_src={'tradable': a.get('tradable'),
                      'news_flag': a.get('news_flag')}))
    # Pattern track, Phase 2 (shadow grading only): every firing
    # becomes 6h + 24h paper candidates. No gate, no alert, no
    # confidence input — the book just gains rows.
    n_pattern_cands = 0
    if patterns:
        _pcands = proof_pattern_candidates(
            patterns, mkt_by_sym, name_by_sym, proof_now, regime_tag)
        n_pattern_cands = len(_pcands)
        proof_cands.extend(_pcands)
    # Cycle track, Phase 2 (shadow grading only): one row per variable
    # per asset-day per horizon (24h + 7d). Bounded universe: BTC
    # first, then this run's card assets, capped at CYCLE_MAX_ASSETS
    # — the full scanned universe would flood even the separate cycle
    # ledger (14 rows/asset/day). No gate, no alert, no confidence
    # input — the book just gains rows.
    n_cycle_cands = 0
    if cycle_state:
        _csyms = []
        for _src in (longs, shorts, movers, watch, accum):
            for _it in _src:
                _s = (_it or {}).get('asset')
                if _s and _s not in _csyms:
                    _csyms.append(_s)
        if 'BTC' in _csyms:
            _csyms.remove('BTC')
        _csyms = (['BTC'] + _csyms)[:CYCLE_MAX_ASSETS]
        _ccands = proof_cycle_candidates(
            cycle_state, _csyms, mkt_by_sym, name_by_sym, proof_now,
            regime_tag)
        n_cycle_cands = len(_ccands)
        proof_record_cycle(cycle_record, _ccands, proof_now)
    cycle_record, cycle_dropped = _proof_trim(cycle_record,
                                             cap=CYCLE_RECORD_CAP)
    cycle_cumulative = proof_roll_cycle_cumulative(
        prior_cycle_cumulative, cycle_dropped)
    # Manipulation track, Phase 2 (shadow grading only): one regime
    # row per asset-day per horizon (24h + 7d; MANIP rows twice —
    # LONG and SHORT-bias), one confluence row per flip + CMI < 0.30
    # per horizon (6h + 24h), one trap row per flip + CMI >= 0.30 per
    # horizon (6h + 24h). Universe is the CMI map itself — already
    # bounded by the pattern track's 30-symbol OHLC cap. No gate, no
    # alert, no confidence input — the book just gains rows.
    n_cmi_cands = 0
    if cmi:
        _cmi_cands = (
            proof_cmi_regime_candidates(
                cmi, list(cmi.keys()), mkt_by_sym, name_by_sym,
                proof_now, regime_tag)
            + proof_cmi_confluence_candidates(
                cmi, mkt_by_sym, name_by_sym, proof_now, regime_tag)
            + proof_cmi_trap_candidates(
                cmi, mkt_by_sym, name_by_sym, proof_now, regime_tag))
        n_cmi_cands = len(_cmi_cands)
        proof_record_cmi(cmi_record, _cmi_cands, proof_now)
    cmi_record, cmi_dropped = _proof_trim(cmi_record,
                                         cap=CMI_RECORD_CAP)
    cmi_cumulative = proof_roll_cmi_cumulative(
        prior_cmi_cumulative, cmi_dropped)
    n_added = proof_record_new(proof, proof_cands, proof_now)
    proof, dropped = _proof_trim(proof)
    cumulative = proof_roll_cumulative(prior_cumulative, dropped)
    pattern_cumulative = proof_roll_pattern_cumulative(
        prior_pattern_cumulative, dropped)
    # Shadow universe, Phase 1: the top shadow longs/shorts join the
    # book as kind='signal_shadow' — same +2h horizon and levels as
    # live signals, same band/regime tags, but a separate ledger and
    # scoreboard, so the shadow cells never mix with the live ones.
    # proof_flagged_cells runs over the shadow scoreboard too: a shadow
    # cell at n>=20 with an extreme win rate is flagged for Nic's
    # review (promotion candidate) — recommendation only, never an
    # automatic promotion.
    shadow_cands = []
    for s in shadow_longs + shadow_shorts:
        shadow_cands.append(_proof_candidate(
            'signal_shadow', s['asset'], s.get('name'), s.get('direction'),
            s.get('thesis'), proof_now, 2,
            tags={'band': conf_band(s.get('confidence')),
                  'regime': regime_tag},
            gate_src={'confidence': s.get('confidence'),
                      'tradable': s.get('tradable'),
                      'news_flag': s.get('news_flag')}))
    n_shadow_added = proof_record_new(shadow_record, shadow_cands, proof_now)
    shadow_record, shadow_dropped = _proof_trim(
        shadow_record, cap=SHADOW_RECORD_CAP)
    shadow_cumulative = proof_roll_cumulative(
        prior_shadow_cumulative, shadow_dropped)
    # Stocks research track, Phase 1: the top stock longs/shorts join
    # the book as kind='stock' — same +2h horizon and levels as live
    # signals, same band tags, but a separate ledger and scoreboard,
    # so the stock cells never mix with the live ones. Regime is
    # 'unknown': the swing regime is crypto-based and is never guessed
    # onto stocks. proof_flagged_cells runs over the stock scoreboard
    # too: a stock cell at n>=20 with an extreme win rate is flagged
    # for Nic's review (promotion candidate) — recommendation only,
    # never an automatic promotion.
    stock_cands = []
    for s in stock_longs + stock_shorts:
        stock_cands.append(_proof_candidate(
            'stock', s['asset'], s.get('name'), s.get('direction'),
            s.get('thesis'), proof_now, 2,
            tags={'band': conf_band(s.get('confidence')),
                  'regime': 'unknown'},
            gate_src={'confidence': s.get('confidence'),
                      # Stocks are equities: the crypto-venue tradable
                      # check does not apply (same exemption as the
                      # swing engine's equity branch) — never hand the
                      # gate the stock signal's `tradable: True`.
                      'tradable': None}))
    n_stock_added = proof_record_new(stock_record, stock_cands, proof_now)
    stock_record, stock_dropped = _proof_trim(
        stock_record, cap=STOCK_RECORD_CAP)
    stock_cumulative = proof_roll_cumulative(
        prior_stock_cumulative, stock_dropped)
    pstats = proof_stats(proof)
    pstats['cells'] = proof_cells(proof, cumulative)
    pstats['flagged_cells'] = proof_flagged_cells(pstats['cells'])
    pstats['pattern_scoreboard'] = pattern_scoreboard(
        proof, pattern_cumulative)
    pstats['cycle_scoreboard'] = cycle_scoreboard(
        cycle_record, cycle_cumulative)
    pstats['cmi_scoreboard'] = cmi_scoreboard(
        cmi_record, cmi_cumulative)
    pstats['shadow_scoreboard'] = proof_cells(
        shadow_record, shadow_cumulative)
    pstats['shadow_flagged_cells'] = proof_flagged_cells(
        pstats['shadow_scoreboard'])
    pstats['stock_scoreboard'] = proof_cells(
        stock_record, stock_cumulative)
    pstats['stock_flagged_cells'] = proof_flagged_cells(
        pstats['stock_scoreboard'])
    print(f"  Proof book: +{n_added} new calls "
          f"({n_pattern_cands} pattern, {n_cycle_cands} cycle, "
          f"{n_cmi_cands} CMI, {n_shadow_added} shadow, "
          f"{n_stock_added} stock), "
          f"{pstats['open']} open, "
          f"{pstats['graded']} graded, paper P&L ${pstats['paper_pnl_total']:+.2f}")

    pub_now = datetime.now(timezone.utc).isoformat()
    out = {
        'generated': pub_now,
        'data_pulled_at': data_pulled_at,
        'published_at': pub_now,
        'mode': 'intraday',
        'horizon': '1-4 hours',
        'longs': longs,
        'shorts': shorts,
        'movers': movers,
        'watch': watch,
        'accum': accum,
        'accum_history': new_history,
        'oi_history': oi_hist,
        'funding_history': fund_hist,
        'macro_snapshot': macro_snap,
        # Price-action pattern track, Phase 1/2 (display-only research):
        # the shared OHLC store (also read by the manipulation track),
        # this run's tags, and a small run diagnostic.
        'pattern_candles': pattern_candles,
        'patterns': patterns,
        'pattern_diag': pattern_diag,
        # Cycle research track, Phase 1/2 (display-only research):
        # today's state, the daily BTC closes, the day-cache for
        # dominance/Fear & Greed, and the dominance history.
        'cycle_state': cycle_state,
        'cycle_diag': cycle_diag,
        'cycle_btc_daily': cycle_btc_daily,
        'cycle_cache': cycle_cache,
        'cycle_dom_history': cycle_dom_history,
        # OKX derivatives track, Phase 1 (display-only research):
        # per-coin funding (+z vs its own 33d history), OI (+1h/24h
        # changes), L/S ratio, funding dispersion vs Kraken, the OI
        # history store, and a small run diagnostic.
        'okx': okx_map,
        'okx_diag': okx_diag,
        'okx_oi_history': okx_oi_hist,
        # DeFiLlama stablecoin/TVL track, Phase 1 (display-only
        # research): current supplies + TVL, the day-cached 7d/24h
        # changes, the USDT+USDC change from the cross-run store,
        # and a small run diagnostic.
        'defillama': defillama_snapshot,
        'defillama_diag': defillama_diag,
        'defillama_history': defillama_hist,
        'defillama_cache': defillama_cache,
        # Deribit options track, Phase 1 (display-only research):
        # per-currency 30d ATM IV, 25-delta skew, put/call OI,
        # max-pain distance, and a small run diagnostic.
        'deribit': deribit_map,
        'deribit_diag': deribit_diag,
        # Manipulation research track, Phase 1/2 (display-only
        # research): per-asset CMI + SuperTrend, the aggregate regime
        # pulse, and a small run diagnostic. The candles behind it
        # are the shared pattern_candles store above — no second
        # fetch, no second store.
        'cmi': cmi,
        'cmi_pulse': cmi_pulse,
        'cmi_diag': cmi_diag,
        'proof': {'record': proof, 'stats': pstats, 'cumulative': cumulative,
                  'pattern_cumulative': pattern_cumulative,
                  'cycle_record': cycle_record,
                  'cycle_cumulative': cycle_cumulative,
                  'cmi_record': cmi_record,
                  'cmi_cumulative': cmi_cumulative,
                  'shadow_record': shadow_record,
                  'shadow_cumulative': shadow_cumulative,
                  'stock_record': stock_record,
                  'stock_cumulative': stock_cumulative},
        'coins_scanned': len(mkts),
        # Shadow universe expansion, Phase 1 (display-only): top shadow
        # longs/shorts and how many shadow coins actually got charts.
        'shadow_longs': shadow_longs,
        'shadow_shorts': shadow_shorts,
        'shadow_coins_scanned': shadow_coins_scanned,
        # Stocks research track, Phase 1 (display-only): top stock
        # longs/shorts, how many tickers got charts, and the run
        # diagnostic (market_open / skipped reason).
        'stock_longs': stock_longs,
        'stock_shorts': stock_shorts,
        'stock_symbols_scanned': stock_symbols_scanned,
        'stock_diag': stock_diag,
        # Circuit-breaker state (Trade Signal Gate v3.0): merge this
        # run's rejection events with the prior run's carried events,
        # prune, and stamp the computed breaker state — the alert
        # selector reads this, no separate fetch needed.
        'gate_events': gate_circuit.prune_events(
            prior_gate_events + _GATE_EVENTS),
        'gate_circuit': gate_circuit.status_from_events(
            prior_gate_events + _GATE_EVENTS),
    }
    if out['gate_circuit']['publishing_disabled']:
        gc = out['gate_circuit']
        print(f"  Gate circuit: PUBLISHING_DISABLED — "
              f"{gc['systemic_60min']} systemic failures in 60m "
              f"({', '.join(gc['reasons_60min']) or 'unknown'}). "
              f"Buy-card publishing suppressed until the gate recovers.")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, 'intraday.json'), 'w') as f:
        json.dump(out, f, indent=2)
    print(f'  Intraday complete: {len(longs)} longs, {len(shorts)} shorts from {len(mkts)} coins'
          f' (+{len(shadow_longs)} shadow longs, {len(shadow_shorts)} shadow shorts'
          f' from {shadow_coins_scanned} shadow coins'
          f' +{len(stock_longs)} stock longs, {len(stock_shorts)} stock shorts'
          f' from {stock_symbols_scanned} stock symbols)')

if __name__ == '__main__':
    main()
