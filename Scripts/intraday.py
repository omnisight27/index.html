#!/usr/bin/env python3
"""
NexIntel Intraday Engine — hourly buy/sell signals for crypto.
Runs separately from the swing engine (nexintel.py).

Strategy: hourly momentum + mean-reversion on top 50 coins by market cap.
Horizon: 1-4 hours. Tighter stops than swing (0.8x hourly vol).
"""
import json, os, math, urllib.request, subprocess
from datetime import datetime, timezone

try:
    import funding as funding_feed  # Feed #2: Binance funding rates (fail-open)
except ImportError:  # engine must never die on a feed import
    funding_feed = None
try:
    import oi_flow  # Feed #3: open-interest flow (fail-open)
except ImportError:
    oi_flow = None

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '..', 'data')
# Resolve relative to repo root
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, 'data')

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
    if direction == 'LONG':
        stop = price * (1 - vol / 100 * 0.8)
        target = price * (1 + vol / 100 * 1.2)
    else:
        stop = price * (1 + vol / 100 * 0.8)
        target = price * (1 - vol / 100 * 1.2)
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
    entry = price
    stop = entry * (1 - vol / 100 * 0.6)
    target = entry * (1 + vol / 100 * 1.2)
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
    back to a WATCH card instead of invented numbers."""
    from datetime import timedelta
    if not price or not vol:
        return None
    if range_high and price < range_high <= price * 1.05:
        trigger = range_high
    else:
        trigger = price * (1 + vol / 100 * 0.5)
    target = trigger * (1 + vol / 100 * 1.2)
    stop = trigger * (1 - vol / 100 * 0.6)
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
# one that touches its stop first is 'lost' (-max_loss_pct); one that
# reaches its exit time + 2h grace untouched is 'expired' and graded at
# the actual price drift. Anything unresolved after 7 days expires too.
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
                     tags=None, gate_src=None):
    """Normalize a signal thesis / mover plan / accum trigger plan into
    a ledger entry. horizon_h matches the builders: signals exit at +2h
    (build_intraday_thesis), mover and accum plans at +3h
    (build_mover_plan / build_accum_plan). `tags` carries the scenario
    cell tags ({'band': ..., 'regime': ...}) stamped at entry.
    `gate_src` carries the source object's own gate inputs at this
    moment ({confidence, tradable, news_flag, chg_1h, chg_24h} —
    whichever the family really has) for the shadow codeword gate;
    the gate is measurement only and never feeds back into the
    candidate itself."""
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
    entry_out = {
        'id': f'{kind}:{asset}:{int(now.timestamp())}',
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
    must not flood the book."""
    fresh = []
    for cand in candidates:
        if not cand:
            continue
        dup = False
        for e in record:
            if e.get('asset') != cand['asset'] or e.get('kind') != cand['kind']:
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

def proof_grade(record, mkt_by_sym, charts_by_sym, now):
    """Grade every open ledger entry against fresh market data.
    mkt_by_sym: {sym: {'price', 'high_24h', 'low_24h'}} from the markets
    payload; charts_by_sym: {sym: [[ts_ms, price], ...]} hourly points —
    main() tops this up so every open call's asset is covered."""
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
                        for k in ('signal', 'mover', 'setup')}
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
#                creation) names coinbase and/or cryptocom.
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
    else:
        checks['tradable'] = any(v in GATE_VENUES for v in tradable)
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
                      'candidate': cand, 'recommendation_only': True})
    return flags

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

def main():
    print('INTRADAY: hourly signals...')
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
    # Feed #2: Binance funding rates — crowded-long detection. One bulk
    # public call; any failure yields {} and the engine runs unchanged.
    fund_map = {}
    if funding_feed is not None:
        try:
            fund_map = funding_feed.funding_by_asset(
                {(c['symbol'] or '').upper() for c in scanned}, fetch_json)
        except Exception:
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
    mkts = universe[:200]
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
        cid = c['id']
        sym = (c['symbol'] or '').upper()
        if sym in BLOCKED:
            continue
        mcap = c.get('market_cap') or 0
        if mcap < 100_000_000:
            continue
        # Hourly chart (24h)
        chart = fetch_json(f'https://api.coingecko.com/api/v3/coins/{cid}/market_chart'
            '?vs_currency=usd&days=1&interval=hourly')
        if not chart or not chart.get('prices'):
            continue
        f = hourly_features(chart['prices'])
        if not f:
            continue
        charts_by_sym[sym] = chart['prices']
        score = score_intraday(f)
        conf = min(95, max(40, 50 + abs(score) * 3))
        # Only signals with meaningful score
        if abs(score) < 3:
            continue
        thesis = build_intraday_thesis(sym, f, score)
        signals.append({
            'asset': sym, 'score': round(score, 2), 'confidence': round(conf),
            'direction': thesis['direction'], 'thesis': thesis,
            'name': c.get('name') or sym,
            'info_url': f'https://www.coingecko.com/en/coins/{cid}',
            'tradable': tradable_on(sym),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })

    signals.sort(key=lambda s: -abs(s['score']))
    longs = [s for s in signals if s['direction'] == 'LONG'][:5]
    shorts = [s for s in signals if s['direction'] == 'SHORT'][:5]

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
        return {
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
    scanned = [c for c in universe
               if (c.get('market_cap') or 0) >= 100_000_000
               and (c['symbol'] or '').upper() not in BLOCKED]
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
        # (median fallback included); plan stays None only when no
        # volatility exists anywhere — the alert then shows WATCH.
        a['plan'] = build_accum_plan(a['price'],
                                     vol_by_id.get(c.get('id')) or median_vol,
                                     c.get('high_24h'))
        accum.append(a)
    accum.sort(key=lambda a: (not a['tradable'], -a['vol_growth_pct']))
    accum = accum[:8]
    print(f'  Accumulation scan: {len(accum)} setting up')

    # --- 📒 Proof Book: grade yesterday's calls before recording today's
    # (a call is never graded in the same run that creates it, so the
    # trailing-24h windows can't credit a pre-entry move) ---
    proof = list(prior_proof)
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
    # number of open calls).
    id_by_sym = {}
    for c in universe:
        s = (c['symbol'] or '').upper()
        if s and s not in id_by_sym:
            id_by_sym[s] = c['id']
    for e in proof:
        # Open calls need a chart for grading; recently-exited calls
        # need one for the after-exit tracker (24h post-exit sampling).
        if e.get('status') != 'open' and \
                not _proof_post_exit_active(e, proof_now):
            continue
        sym = e['asset']
        if sym in charts_by_sym or sym not in id_by_sym:
            continue
        ch = fetch_json(f'https://api.coingecko.com/api/v3/coins/{id_by_sym[sym]}/market_chart'
                        '?vs_currency=usd&days=1&interval=hourly')
        if ch and ch.get('prices'):
            charts_by_sym[sym] = ch['prices']
    proof_grade(proof, mkt_by_sym, charts_by_sym, proof_now)
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
    n_added = proof_record_new(proof, proof_cands, proof_now)
    proof, dropped = _proof_trim(proof)
    cumulative = proof_roll_cumulative(prior_cumulative, dropped)
    pstats = proof_stats(proof)
    pstats['cells'] = proof_cells(proof, cumulative)
    pstats['flagged_cells'] = proof_flagged_cells(pstats['cells'])
    print(f"  Proof book: +{n_added} new calls, {pstats['open']} open, "
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
        'proof': {'record': proof, 'stats': pstats, 'cumulative': cumulative},
        'coins_scanned': len(mkts),
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, 'intraday.json'), 'w') as f:
        json.dump(out, f, indent=2)
    print(f'  Intraday complete: {len(longs)} longs, {len(shorts)} shorts from {len(mkts)} coins')

if __name__ == '__main__':
    main()
