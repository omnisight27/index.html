#!/usr/bin/env python3
"""Per-variable evaluation harness for OmniSight — Nic Nov 11.

"How are we evaluating the score? We need tests ran on each variable.
100% confidence for each one is probably impossible, but it needs to
pull weighted decisions as they read. Test each variable and how much
it weighs in calculation."

For EVERY variable this harness computes, from the graded history the
engine itself published:
  (a) STANDALONE performance — win rate, n, expectancy per $1,000.
  (b) ABLATION delta — the history re-graded with that variable
      zeroed/removed from the score: how much the model loses without
      it. This is the key number.
  (c) CURRENT weight — the weight it actually carries in the
      calculation today (heuristic fallback, weights learned from the
      full history, and the Nov 1 self-tune proposal).

Walk-forward validation: the swing history is split by time — oldest
70% of scored runs train the weights, newest 30% test them — so the
core-feature numbers are out-of-sample, not in-sample. Track rows
(patterns, cycle, CMI, movers, stocks) use their full ledgers, as the
Nov 11 variable audit did, and say so.

Weights from grades: a variable's JUSTIFIED weight is proportional to
its ablation delta (dollars per $1,000 the model gains by keeping it),
clipped at zero, scaled so the best variable carries the largest
current learned weight. No 95% significance gate anywhere — 95% is a
displayed goal only (Nic Nov 11). The output is a dated report.
REVIEW ONLY: this harness never applies a weight, never touches a
live score, gate, or ledger. Nothing here changes the engine.

Rulers (same as the Proof Book — see the Nov 11 variable audit):
  - Swing picks: win = the pick's stored win flag; expectancy =
    mean direction-signed return_pct x $10 (i.e. per $1,000).
  - Proof Book cells: win rate = wins / (wins + losses); expectancy
    = paper_pnl_total / (wins + losses), paper P&L as published by
    the engine (fixed +$28 win / -$35 loss levels for won/lost
    entries, actual drift at close for expired ones).
  - A cell with n < 20 graded is too thin to judge: verdict UNKNOWN.

Corrected shadow weight set (Nov 11, "correct the function" build):
  SHADOW_OVERRIDES below is the shadow-only set from the harness's
  out-of-sample ablation — acceleration 0.225, psych_bias 0.173,
  extreme_fade / reversion / vol_regime / trend_align / ma_align 0,
  everything else keeping its (train-fit) weight. The harness
  re-grades it out-of-sample on the SAME walk-forward split and
  reports its expectancy against the current weights' expectancy,
  plus a joint remove-all-dead ablation (all 21 dead-weight features
  zeroed at once). The Nov 1 self-tune proposal is NOT used anywhere
  here (opposite signs to the ablation); trend_align / ma_align are
  zeroed, never flipped negative. Shadow only: nothing here touches
  a live weight, score, gate, or ledger.

What the ablation does NOT model (stated limits):
  - It re-grades each published pick under its ablated direction; it
    does NOT re-run top-k selection (which picks make the daily top 3).
  - The geometric edge is recomputed from TRAIN centroids; at run time
    the engine used expanding history, so recomputed scores are close
    to — not identical with — the stored scores. The report shows the
    direction-agreement rate so the approximation is visible.
  - The 60% buy bar / confidence tempering is not modeled in the
    ablation; direction flip is the mechanism.

Usage: python3 Scripts/variable_tests.py [--repo PATH] [--out DIR]
Writes variable-tests-YYYY-MM-DD.md to the out dir (default:
<goal>/hidden_files, i.e. ../hidden_files from the repo root).
Exit 0 when the report is written. Free data only: it reads the
ledgers already on disk and nothing else.
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nexintel import (  # noqa: E402  (engine constants — single source of truth)
    FEATURE_NAMES,
    HEURISTIC_W,
    _excluded_pick,
    _pad,
    learn_weights,
    regime_adjustment,
)

N_FEATURES = 22
TRAIN_FRAC = 0.7
MIN_GRADED_JUDGE = 20        # below this n, a cell is too thin to judge
EARN_DELTA_EXP = 10.0        # $/1k: ablation delta at/above this EARNS weight
HURT_DELTA_EXP = -10.0       # $/1k: at/below this the variable HURTS (cut)
INVERT_SPREAD_PP = 5.0       # tercile spread at/below this reads backwards
FIXED_WIN_PNL = 28.0         # documented Proof Book ruler (reference only)
FIXED_LOSS_PNL = 35.0

# Corrected shadow weight set (Nov 11, "correct the function" build).
# Keyed by FEATURE_NAMES index: 0 momentum, 1 reversion, 2 psych_bias,
# 3 fg_level, 4 fg_delta, 5 vol, 6 acceleration, 7 zscore, 8 rsi,
# 9 trend_align, 10 macd, 11 ma_align, 12 donchian, 13 rsi, 14 roc,
# 15 stoch, 16 boll_pctb, 17 atr, 18 vol_regime, 19 zscore, 20
# extreme_fade, 21 var. Values are the harness's out-of-sample
# justified weights (optimization-brief-2026-11-11.md §1): the two
# raises, the five cuts to zero. Everything else keeps its current
# (train-fit) weight — the other 15 dead-weight features are NOT
# zeroed in this set; the joint remove-all-dead ablation is run and
# reported separately so the mass-zeroing decision rests on its
# out-of-sample result, not on an assumption.
SHADOW_OVERRIDES = {6: 0.225, 2: 0.173, 20: 0.0, 1: 0.0, 18: 0.0,
                    9: 0.0, 11: 0.0}
SHADOW_OVERRIDE_NAMES = {6: 'acceleration', 2: 'psych_bias',
                         20: 'extreme_fade', 1: 'reversion',
                         18: 'vol_regime', 9: 'trend_align',
                         11: 'ma_align'}
WILSON_Z = 1.96  # 95% interval — displayed only, never a gate (Nic Nov 11)


# ------------------------------------------------------------------ helpers

def parse_iso(s):
    """Parse an ISO timestamp to an aware datetime; None when unparseable."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_json(path):
    with open(path) as f:
        return json.load(f)


def win_rate_pct(wins, losses):
    wl = wins + losses
    return (wins / wl * 100.0) if wl else None


def expectancy_usd(returns):
    """Mean direction-signed return_pct x $10 == dollars per $1,000."""
    if not returns:
        return None
    return sum(returns) / len(returns) * 10.0


def sem_usd(returns):
    """Standard error of expectancy_usd, $ per $1,000. None when n < 2."""
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    return math.sqrt(var / n) * 10.0


def fmt(x, digits=1):
    return f"{x:.{digits}f}" if x is not None else "—"


def wilson_lower_95(wins, n, z=WILSON_Z):
    """Wilson 95% interval lower bound as a fraction (0..1), or None
    when n is 0. DISPLAY ONLY — it never gates anything (Nic Nov 11:
    95% is a goal to read, not a gate)."""
    if not n:
        return None
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def _regraded_outcomes(test_picks, weights, win_c, loss_c, zero_indices=None):
    """Per-pick (score, direction, direction-signed return, win) under
    `weights`, optionally with `zero_indices` zeroed out of the score.
    Ground truth is the published return/win flag; a direction
    different from the published one earns the mirror outcome
    (return negated, win flipped). Shared by ablate_feature and
    grade_with_weights so the two never drift apart."""
    zero = set(zero_indices or [])
    out = []
    for p in test_picks:
        feats = list(p['features'])
        for j in zero:
            if 0 <= j < len(feats):
                feats[j] = 0.0
        score, direction = rescore(feats, weights, win_c, loss_c, p['regime'])
        ret = p['ret'] if direction == p['direction'] else -p['ret']
        win = p['win'] if direction == p['direction'] else (
            not p['win'] if p['ret'] != 0 else p['win'])
        out.append((score, direction, ret, win))
    return out


def grade_with_weights(test_picks, weights, win_c, loss_c, zero_indices=None):
    """Re-grade the TEST picks under one weight set (optionally with
    `zero_indices` zeroed): win rate and expectancy per $1,000 on the
    SAME walk-forward test slice the ablations use, so any weight set
    — current, shadow, joint-ablated — is comparable on identical
    ground. Returns {n, wins, losses, win_pct, exp_usd, exp_se_usd}."""
    outcomes = _regraded_outcomes(test_picks, weights, win_c, loss_c,
                                  zero_indices)
    n = len(outcomes)
    wins = sum(1 for _s, _d, _r, w in outcomes if w)
    rets = [r for _s, _d, r, _w in outcomes]
    exp = expectancy_usd(rets)
    return {
        'n': n,
        'wins': wins,
        'losses': n - wins,
        'win_pct': round(win_rate_pct(wins, n - wins), 1) if n else None,
        'exp_usd': round(exp, 2) if exp is not None else None,
        'exp_se_usd': round(sem_usd(rets) or 0.0, 2),
    }


def shadow_weights(base_weights, overrides=None):
    """The corrected shadow weight set: `base_weights` with
    SHADOW_OVERRIDES applied. A NEW list; the base is never mutated.
    The Nov 1 self-tune proposal is deliberately NOT an input here."""
    w = [float(x) for x in base_weights]
    for j, v in (overrides if overrides is not None else SHADOW_OVERRIDES).items():
        if 0 <= j < len(w):
            w[j] = float(v)
    return w


# ------------------------------------------------------------------
# Nic's 50% rule (Nov 11): "If you have over 50% proof it signal as
# positive. And <50% as negative and weighs accordingly to
# percentage." Each variable/cell/track takes its graded win rate —
# the proof percentage — and becomes a SIGNED weight: above 50% is a
# positive signal, below 50% is a negative signal, magnitude scaling
# with distance from 50% (50% itself = zero). This is the shadow
# weight set now; the ablation set stays as a comparison benchmark.
# Scaling (one transparent choice, stated everywhere it is used):
# signed weight = (win_rate - 0.50) * SCALE, where SCALE =
# MAX_SIGNED_WEIGHT / max |win_rate - 0.50| over ALL signed items
# (the 22 features on their TRAIN slice + every graded track/cell),
# so the largest distance from 50% maps to the largest current
# learned weight (0.225) — mirroring how the harness scales
# justified weights. The scaling itself is a testable choice.
# ------------------------------------------------------------------

MAX_SIGNED_WEIGHT = 0.225  # largest current learned weight (extreme_fade)


def fifty_rule_scale(distances, cap=MAX_SIGNED_WEIGHT):
    """SCALE for Nic's 50% rule: cap / largest |win_rate - 0.50|.

    `distances` are fractions (0..0.5). Returns 0.0 when every
    distance is 0 (nothing is provably above or below 50%)."""
    biggest = max(distances) if distances else 0.0
    return cap / biggest if biggest > 0 else 0.0


def signed_weight_50(win_rate_frac, scale):
    """Signed weight for one proof percentage: (win_rate - 0.50) x SCALE.

    win_rate_frac is a fraction (0..1). Above 50% -> positive,
    below 50% -> negative, exactly 50% -> zero."""
    return round((win_rate_frac - 0.50) * scale, 3)


def invert_entry(e):
    """Mirror-image outcome of one graded ledger entry: the same trade
    taken on the opposite side. Returns (inv_pnl, inv_win, method).

    - Horizon-close entries (cycle_expired / pattern_expired /
      cmi_expired, or status 'expired'): the payoff is drift, which
      flips sign exactly — inv_pnl = -pnl, win flipped (for
      won/lost) or drift-flipped (for 'expired', where win means the
      drift covered costs: inv wins iff the mirrored drift does).
    - Fixed-level hits (status won at exactly +$28, lost at exactly
      -$35): the opposite side hits the opposite level — won +28
      becomes lost -35, lost -35 becomes won +28. The mirror is NOT
      -pnl here (+28 mirrored is -35, not -28).
    - Anything else (e.g. a breakeven trailing-stop win at $0.00):
      fallback mirror, inv_pnl = -pnl, win flipped — counted as
      'fallback' so the report can say how many entries took it.
    This is a FIRST-ORDER read: the true opposite trade's stop/target
    touch order on a live path can differ from the mirror, so the
    inverted expectancy is an estimate of what betting against the
    cell would have paid, not an exact re-grade.
    """
    pnl = e.get('paper_pnl') or 0.0
    status = e.get('status')
    expired_close = bool(e.get('cycle_expired') or
                         e.get('pattern_expired') or
                         e.get('cmi_expired'))
    if status == 'expired':
        res = e.get('result_pct')
        try:
            inv_win = (res is not None and float(res) <= -1.4)
        except (TypeError, ValueError):
            inv_win = False
        return -pnl, inv_win, 'horizon_close'
    if expired_close:
        return -pnl, (status != 'won'), 'horizon_close'
    if status == 'won' and pnl == FIXED_WIN_PNL:
        return -FIXED_LOSS_PNL, False, 'fixed_level'
    if status == 'lost' and pnl == -FIXED_LOSS_PNL:
        return FIXED_WIN_PNL, True, 'fixed_level'
    return -pnl, (status != 'won'), 'fallback'


def invert_entries(entries):
    """Mirror-image totals for a set of graded ledger entries.

    Returns {n_graded, wins, losses, expired, win_rate,
    expectancy_usd, paper_pnl_total, methods} where wins/losses
    count only won/lost entries (expired-status entries contribute
    dollars only, as in the original ruler). `methods` counts how
    many entries inverted by each method above.
    """
    t = {'n_graded': 0, 'wins': 0, 'losses': 0, 'expired': 0,
         'paper_pnl_total': 0.0,
         'methods': {'fixed_level': 0, 'horizon_close': 0, 'fallback': 0}}
    for e in entries or []:
        if e.get('status') not in ('won', 'lost', 'expired'):
            continue
        inv_pnl, inv_win, method = invert_entry(e)
        t['methods'][method] += 1
        t['n_graded'] += 1
        t['paper_pnl_total'] += inv_pnl
        if e.get('status') == 'expired':
            t['expired'] += 1
        elif inv_win:
            t['wins'] += 1
        else:
            t['losses'] += 1
    wl = t['wins'] + t['losses']
    t['win_rate'] = round(t['wins'] / wl * 100, 1) if wl else None
    t['expectancy_usd'] = (round(t['paper_pnl_total'] / wl, 2)
                           if wl else None)
    t['paper_pnl_total'] = round(t['paper_pnl_total'], 2)
    return t


# ------------------------------------------------------------------ history

def residual_cases(test_picks, weights, win_c, loss_c, top_n=5):
    """Hunt the residual (unknown): the picks where F — the 50%-rule
    corrected function — was most wrong, on the SAME test slice the
    three-way comparison uses. No value is invented for unknown; the
    residual cases ARE the hunt — where F is wrong, the unknown lives.

    Two lists, each ranked by |shadow score| (F's conviction):
      calls_lost — F called it (shadow direction == published) and it
        lost. Dollar miss = the loss per $1,000 (-ret x 10).
      fades_won — F faded the published call (shadow direction opposite
        published) and the published call won anyway. Dollar miss = the
        win F talked us out of (published ret x 10).
    Each case: asset, run date, what F predicted (direction + shadow
    score), what happened (direction-signed return), dollar miss.
    """
    scored = []
    for p in test_picks:
        score, direction = rescore(p['features'], weights, win_c, loss_c,
                                   p['regime'])
        ret = p['ret'] if direction == p['direction'] else -p['ret']
        win = p['win'] if direction == p['direction'] else (
            not p['win'] if p['ret'] != 0 else p['win'])
        scored.append({'pick': p, 'score': score, 'direction': direction,
                       'ret': ret, 'win': win})

    def case(s, kind):
        p = s['pick']
        miss = (-p['ret'] * 10.0 if kind == 'call_lost'
                else p['ret'] * 10.0)
        return {
            'kind': kind,
            'asset': p['asset'],
            'run_date': (p['run_date'].isoformat()[:10]
                         if p.get('run_date') else None),
            'f_direction': 'LONG' if s['direction'] == 1 else 'SHORT',
            'f_score': round(s['score'], 2),
            'published_direction': 'LONG' if p['direction'] == 1 else 'SHORT',
            'outcome': 'lost' if kind == 'call_lost' else 'won',
            # The PUBLISHED call's realized return (what actually
            # happened): negative on a losing call, positive on a win
            # F faded. The shadow-direction return would flip the sign
            # on fades and read backwards — never show that here.
            'published_return_pct': round(p['ret'], 2),
            'dollar_miss_per_1000': round(miss, 2),
        }

    calls_lost = sorted(
        (s for s in scored
         if s['direction'] == s['pick']['direction'] and not s['win']),
        key=lambda s: -abs(s['score']))[:top_n]
    fades_won = sorted(
        (s for s in scored
         if s['direction'] != s['pick']['direction'] and s['pick']['win']),
        key=lambda s: -abs(s['score']))[:top_n]
    return {'calls_lost': [case(s, 'call_lost') for s in calls_lost],
            'fades_won': [case(s, 'fades_won') for s in fades_won]}


def f_form_mapping(results):
    """F = a + b + c + d + (f - e) + unknown — PROPOSED letter mapping
    (Nic confirms the letters; flagged PROPOSED everywhere it appears).
    For each term: what currently proxies it in the graded data and
    whether that proxy is graded or ungraded. unknown is never given a
    value — its proxy is the residual cases.
    """
    setup = (results.get('per_kind') or {}).get('setup') or {}
    fr = results.get('fifty_rule') or {}
    feat_rows = {x['name']: x for x in fr.get('feature_rows') or []}
    accel = feat_rows.get('acceleration') or {}
    psych = feat_rows.get('psych_bias') or {}
    return {
        'mapping': 'PROPOSED — Nic confirms the letters',
        'terms': [
            {'term': 'a', 'name': 'economics',
             'ingredient': 'economics',
             'proxy': ('regime gate — regime_adjustment long_boost / '
                       'short_damp inside every re-grade, plus buy% '
                       'conditions (RISK_OFF, buy 38% today)'),
             'graded': ('PARTLY — the tilt is inside every re-grade, but '
                        'no standalone regime ablation exists'),
             'number': None},
            {'term': 'b', 'name': 'mathematics',
             'ingredient': 'mathematics',
             'proxy': ('the harness itself — walk-forward re-grades, the '
                       '50% rule (win rate -> signed weight), expectancy '
                       '$/1k, fitted weights'),
             'graded': 'YES — this report is b, graded out-of-sample',
             'number': (f"50% rule {fr.get('grade', {}).get('exp_usd')} $/1k "
                        f"vs current "
                        f"{(results.get('shadow') or {}).get('base_grade', {}).get('exp_usd')} $/1k"
                        if fr.get('grade') else None)},
            {'term': 'c', 'name': 'human psychology',
             'ingredient': 'psychology',
             'proxy': ('psych_bias (feature 2, graded) + Fear & Greed '
                       'level / delta (features 3/4, dead weight) + the '
                       "engine's own overconfidence (calibration overstates "
                       'every band)'),
             'graded': ('MIXED — psych_bias graded (signed weight '
                        f"{psych.get('signed_weight')}, rule-set delta "
                        f"{psych.get('rule_set_delta_exp_usd')} $/1k); F&G "
                        'level/delta dead weight, graded as earning nothing'),
             'number': None},
            {'term': 'd', 'name': 'order flow',
             'ingredient': 'economics',
             'proxy': ('FOOTPRINT ONLY — accumulation trigger '
                        f"{setup.get('win_rate')}% when triggered "
                        f"({setup.get('wins')}W/{setup.get('losses')}L, "
                        f"+${setup.get('expectancy_usd')}/1k) + acceleration "
                        '(ablation +$12.19/1k, mechanism UNPROVEN)'),
             'graded': ('FOOTPRINT ONLY — no direct order-flow data: order '
                        'book depth, whale wallets, exchange flows are all '
                        'missing dimensions'),
             'number': None},
            {'term': 'f-e', 'name': 'net flow (in minus out)',
             'ingredient': 'economics',
             'proxy': ('stablecoin supply / TVL changes (DeFiLlama feed, '
                       'display-only)'),
             'graded': ('UNGRADED — no persisted read on graded picks; '
                        'the feed never populated on a graded pick'),
             'number': None},
            {'term': 'unknown', 'name': 'residual',
             'ingredient': 'mathematics',
             'proxy': 'the residual cases — where F is wrong, the unknown lives',
             'graded': ("THE HUNT — the OPEN DOOR: what the loop reaches "
                        "toward but cannot yet measure. No value invented, "
                        "no fake precision. Evidence that would count: the "
                        "residual shrinking over t with no change to the "
                        "measurable terms, or residual cases clustering on "
                        "moves the measurable terms cannot see at all. "
                        "Neither is met — see 'Hunting the residual'"),
             'number': None},
        ],
    }


def distance_to_95(results):
    """Best graded reads against the 95% line (Nic Nov 11: >95% graded
    proof = a VERY ACCURATE read; 95% is a READ, never a gate — nothing
    is blocked for being under 95%). Each entry: name, win%, n, basis,
    distance = 95 - win%. Only n >= 20 reads qualify."""
    reads = []
    setup = (results.get('per_kind') or {}).get('setup') or {}
    if setup.get('win_rate') is not None and setup.get('graded', 0) >= 20:
        reads.append({'name': 'accumulation setups (triggered calls)',
                      'win_pct': setup['win_rate'], 'n': setup['graded'],
                      'basis': 'triggered-call win rate'})
    for t in (results.get('fifty_rule') or {}).get('tracks') or []:
        if t.get('win_pct') is not None and (t.get('n') or 0) >= 20:
            reads.append({'name': t['name'], 'win_pct': t['win_pct'],
                          'n': t['n'], 'basis': 'cell win rate'})
    for x in results.get('feature_rows') or []:
        st = x.get('standalone') or {}
        if st.get('top_win_pct') is not None and (st.get('top_n') or 0) >= 20:
            reads.append({'name': f"{x['name']} (top tercile)",
                          'win_pct': st['top_win_pct'], 'n': st['top_n'],
                          'basis': 'top-tercile win rate'})
    reads.sort(key=lambda r: -r['win_pct'])
    out = []
    for r in reads[:6]:
        out.append({**r, 'distance_to_95': round(95.0 - r['win_pct'], 1)})
    return out


def load_history(repo):
    return load_json(os.path.join(repo, 'data', 'history.json'))


def load_proof(repo):
    return load_json(os.path.join(repo, 'data', 'intraday.json')).get('proof') or {}


def load_proposed_weights(repo):
    try:
        return load_json(os.path.join(repo, 'data', 'proposed_weights.json'))
    except (OSError, ValueError):
        return {}


def walk_forward_split(runs, train_frac=TRAIN_FRAC):
    """Oldest train_frac of SCORED runs train; newest (1 - frac) test.

    Returns (train_runs, test_runs), both date-sorted. Unscored runs
    carry no outcomes and are excluded from both sides.
    """
    scored = [r for r in runs if r.get('scored')]
    scored.sort(key=lambda r: parse_iso(r.get('date') or '') or datetime.min.replace(tzinfo=timezone.utc))
    n_train = int(len(scored) * train_frac)
    return scored[:n_train], scored[n_train:]


def graded_picks(runs):
    """Flatten runs to scored picks with a full feature vector.

    One dict per pick: direction (+1 long / -1 short), direction-signed
    return_pct (already signed by the engine: a short's return_pct is
    the SHORT's return), win flag, 22 features, regime, class.
    Excluded picks (engine corrections) never count.
    """
    out = []
    for run in runs:
        run_date = parse_iso(run.get('date') or '')
        for direction, key in ((1, 'longs'), (-1, 'shorts')):
            for p in run.get(key) or []:
                if _excluded_pick(p):
                    continue
                ret = p.get('return_pct')
                if ret is None:
                    continue
                feats = _pad(list(p.get('features') or []))
                if len(feats) != N_FEATURES:
                    continue
                win = p.get('win')
                if not isinstance(win, bool):
                    win = ret > 0
                out.append({
                    'run_date': run_date,
                    'direction': direction,
                    'asset': p.get('asset'),
                    'ret': float(ret),
                    'win': bool(win),
                    'features': [float(x) for x in feats],
                    'regime': p.get('regime') or 'NEUTRAL',
                    'cls': p.get('class') or 'crypto',
                    'score_stored': p.get('score'),
                })
    return out


# ------------------------------------------------------------------ scoring

def fit_weights(train_runs):
    """Weights learned from TRAIN runs only (walk-forward honest)."""
    return learn_weights({'picks': train_runs})


def centroids(train_picks):
    """Win/loss feature centroids, mirroring nexintel.geometric_edge.

    Returns (win_c, loss_c) or (None, None) when either side has
    fewer than 3 picks (the engine's own warming-up rule).
    """
    wins, losses = [], []
    for p in train_picks:
        (wins if p['win'] else losses).append(p['features'])
    if len(wins) < 3 or len(losses) < 3:
        return None, None
    win_c = [sum(f[i] for f in wins) / len(wins) for i in range(N_FEATURES)]
    loss_c = [sum(f[i] for f in losses) / len(losses) for i in range(N_FEATURES)]
    return win_c, loss_c


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-12
    nb = math.sqrt(sum(y * y for y in b)) or 1e-12
    return dot / (na * nb)


def geo_edge_for(feats, win_c, loss_c):
    """Cosine similarity to the win centroid minus to the loss centroid."""
    if win_c is None or loss_c is None:
        return 0.0
    return _cosine(feats, win_c) - _cosine(feats, loss_c)


def rescore(feats, weights, win_c, loss_c, regime, zero_idx=None):
    """Recompute a pick's tilted score, optionally with one feature zeroed.

    Mirrors the live path: dot(weights, features) + geo_edge x 10,
    then the regime tilt (long_boost / short_damp by score sign).
    Returns (tilted_score, direction) with direction +1 long / -1 short.
    """
    f = list(feats)
    if zero_idx is not None:
        f[zero_idx] = 0.0
    geo = geo_edge_for(f, win_c, loss_c)
    raw = sum(w * x for w, x in zip(weights, f)) + geo * 10.0
    try:
        adj = regime_adjustment(regime)
    except KeyError:
        adj = regime_adjustment('NEUTRAL')
    mult = adj['long_boost'] if raw > 0 else adj['short_damp']
    tilted = raw * mult
    return tilted, (1 if tilted > 0 else -1)


def ablate_feature(test_picks, weights, win_c, loss_c, j):
    """Re-grade the TEST picks with feature j zeroed out of the score.

    The baseline is the TRAIN-fit model re-applied to the test slice
    (recomputed direction d0), NOT the published direction: the
    published picks were scored with expanding-window weights, so
    measuring the ablation against them would mix the feature's effect
    with weight drift. Ground truth is the published return/win flag;
    a direction different from the published one earns the mirror
    outcome (return negated, win flipped). The delta therefore
    isolates feature j inside one consistent model.
    """
    n = len(test_picks)
    base = _regraded_outcomes(test_picks, weights, win_c, loss_c)
    abl = _regraded_outcomes(test_picks, weights, win_c, loss_c,
                             zero_indices=(j,))
    base_rets = [r for _s, _d, r, _w in base]
    abl_rets = [r for _s, _d, r, _w in abl]
    base_wins = sum(1 for _s, _d, _r, w in base if w)
    ablated_wins = sum(1 for _s, _d, _r, w in abl if w)
    flips = sum(1 for (_s0, d0, _r0, _w0), (_sj, dj, _rj, _wj)
                in zip(base, abl) if dj != d0)
    near_zero = sum(1 for (sj, _dj, _rj, _wj) in abl if abs(sj) < 1.0)
    agree = sum(1 for (_s0, d0, _r0, _w0), p in zip(base, test_picks)
                if d0 == p['direction'])
    paired = [b - a for b, a in zip(base_rets, abl_rets)]
    base_win_pct = win_rate_pct(base_wins, n - base_wins)
    abl_win_pct = win_rate_pct(ablated_wins, n - ablated_wins)
    base_exp = expectancy_usd(base_rets) or 0.0
    abl_exp = expectancy_usd(abl_rets) or 0.0
    return {
        'n': n,
        'flips': flips,
        'near_zero': near_zero,
        'direction_agreement_pct': round(agree / n * 100, 1) if n else None,
        'orig_win_pct': base_win_pct,
        'abl_win_pct': abl_win_pct,
        'delta_win_pp': round((base_win_pct or 0.0) - (abl_win_pct or 0.0), 1),
        'orig_exp_usd': round(base_exp, 2),
        'abl_exp_usd': round(abl_exp, 2),
        'delta_exp_usd': round(base_exp - abl_exp, 2),
        'delta_exp_se_usd': round(sem_usd(paired) or 0.0, 2),
    }


def tercile_stats(values, wins):
    """Top-third vs bottom-third win rate for one feature's values.

    Returns (top_win_pct, top_n, bot_win_pct, bot_n, spread_pp).
    Ties at the cut are assigned by sorted order (deterministic).
    """
    order = sorted(range(len(values)), key=lambda i: values[i])
    n = len(order)
    third = n // 3
    if third == 0:
        return None, 0, None, 0, None
    bot_idx = order[:third]
    top_idx = order[-third:]
    bot_w = sum(1 for i in bot_idx if wins[i])
    top_w = sum(1 for i in top_idx if wins[i])
    bot_pct = bot_w / len(bot_idx) * 100.0
    top_pct = top_w / len(top_idx) * 100.0
    return round(top_pct, 1), len(top_idx), round(bot_pct, 1), len(bot_idx), round(top_pct - bot_pct, 1)


def standalone_feature(test_picks, j):
    """One-variable-at-a-time read on the TEST slice (audit's method)."""
    values = [p['features'][j] for p in test_picks]
    wins = [p['win'] for p in test_picks]
    top_pct, top_n, bot_pct, bot_n, spread = tercile_stats(values, wins)
    order = sorted(range(len(values)), key=lambda i: values[i])
    top_idx = order[-(len(order) // 3):] if len(order) // 3 else []
    top_exp = expectancy_usd([test_picks[i]['ret'] for i in top_idx]) if top_idx else None
    return {
        'top_win_pct': top_pct,
        'top_n': top_n,
        'bot_win_pct': bot_pct,
        'bot_n': bot_n,
        'spread_pp': spread,
        'top_exp_usd': round(top_exp, 2) if top_exp is not None else None,
    }


def justified_weight(delta_exp, max_delta_exp, max_weight):
    """Weight the grades justify: ablation delta, clipped at zero,
    scaled so the best variable carries the largest current weight."""
    if max_delta_exp is None or max_delta_exp <= 0:
        return 0.0
    return round(max(0.0, delta_exp) / max_delta_exp * max_weight, 3)


def verdict_feature(delta_exp, delta_win, spread):
    """EARNS / DEAD WEIGHT / HURTS, plus an INVERTED flag when the
    feature reads backwards (top tercile wins less than bottom).

    The bar is dollars: expectancy is the money metric, so a feature
    earns weight when removing it costs $10+/1k even if the win rate
    moves the other way — the win-rate delta stays visible in Table A
    (delta_win is reported, not gated).
    """
    if delta_exp >= EARN_DELTA_EXP:
        v = 'EARNS WEIGHT'
    elif delta_exp <= HURT_DELTA_EXP:
        v = 'HURTS — CUT'
    else:
        v = 'DEAD WEIGHT'
    if spread is not None and spread <= -INVERT_SPREAD_PP:
        v += ' · INVERTED'
    return v


# ------------------------------------------------------------------ book

def cell_key(e):
    """(type, direction, band, regime) — mirrors intraday.proof_cell_key."""
    return (e.get('kind') or 'signal',
            e.get('direction') or 'LONG',
            e.get('band') or 'na',
            e.get('regime') or 'unknown')


def backfill_regimes(record, runs):
    """Tag record entries missing a regime with the swing regime in
    force at their ts_open (latest run at/before open). Mirrors
    intraday.proof_backfill_regimes; regime tokens are lowercased
    (RISK_OFF -> risk_off). Returns the count tagged."""
    pairs = sorted((parse_iso(r.get('date') or ''), (r.get('regime') or '').lower())
                   for r in runs if r.get('date') and r.get('regime'))
    pairs = [(ts, tok) for ts, tok in pairs if ts and tok]
    n = 0
    for e in record:
        if e.get('regime'):
            continue
        opened = parse_iso(e.get('ts_open') or '')
        if not opened:
            continue
        tag = None
        for ts, tok in pairs:
            if ts <= opened:
                tag = tok
            else:
                break
        if tag:
            e['regime'] = tag
            n += 1
    return n


def aggregate_book(record, cumulative, runs):
    """Merge the visible Proof Book ledger with the cumulative counters
    of entries that rolled off the 100-entry cap — exactly the way
    intraday.proof_cells does it. Returns (cells, per_kind, overall).

    Each cell: graded (won+lost+expired), wins, losses, expired,
    never_triggered, open, win_rate (wins / wins+losses), paper_pnl,
    expectancy ($/1k = paper_pnl / (wins+losses)).
    """
    backfill_regimes(record, runs)
    cells = {}

    def bucket(key):
        return cells.setdefault(key, {'graded': 0, 'wins': 0, 'losses': 0,
                                      'expired': 0, 'never_triggered': 0,
                                      'open': 0, 'paper_pnl_total': 0.0})

    for e in record:
        b = bucket(cell_key(e))
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

    def finalize(b):
        wl = b['wins'] + b['losses']
        b['win_rate'] = round(b['wins'] / wl * 100, 1) if wl else None
        b['expectancy_usd'] = round(b['paper_pnl_total'] / wl, 2) if wl else None
        b['paper_pnl_total'] = round(b['paper_pnl_total'], 2)
        return b

    cells = {k: finalize(v) for k, v in cells.items()}
    per_kind = {}
    overall = {'graded': 0, 'wins': 0, 'losses': 0, 'expired': 0,
               'never_triggered': 0, 'open': 0, 'paper_pnl_total': 0.0}
    for (kind, _d, _b, _r), c in cells.items():
        k = per_kind.setdefault(kind, {'graded': 0, 'wins': 0, 'losses': 0,
                                       'expired': 0, 'never_triggered': 0,
                                       'open': 0, 'paper_pnl_total': 0.0})
        for f in ('graded', 'wins', 'losses', 'expired', 'never_triggered', 'open'):
            k[f] += c[f]
            overall[f] += c[f]
        k['paper_pnl_total'] += c['paper_pnl_total']
        overall['paper_pnl_total'] += c['paper_pnl_total']
    for totals in list(per_kind.values()) + [overall]:
        finalize(totals)
    return cells, per_kind, overall


def _ledger_totals(entries):
    """Wins/losses/expired/open/paper_pnl over ledger entries."""
    t = {'graded': 0, 'wins': 0, 'losses': 0, 'expired': 0, 'open': 0,
         'paper_pnl_total': 0.0}
    for e in entries:
        st = e.get('status')
        if st == 'open':
            t['open'] += 1
        elif st in ('won', 'lost', 'expired'):
            t['graded'] += 1
            t['wins' if st == 'won' else 'losses' if st == 'lost' else 'expired'] += 1
            t['paper_pnl_total'] += e.get('paper_pnl') or 0
    wl = t['wins'] + t['losses']
    t['win_rate'] = round(t['wins'] / wl * 100, 1) if wl else None
    t['expectancy_usd'] = round(t['paper_pnl_total'] / wl, 2) if wl else None
    t['paper_pnl_total'] = round(t['paper_pnl_total'], 2)
    return t


def aggregate_cycle(cycle_record):
    """Per-variable cycle totals + deduped distinct-outcome totals.

    The five variables grade the SAME trade five times (identical
    entry/direction/outcome, one label per variable), so the honest n
    is the deduped count keyed by (asset, ts_open, horizon). The
    function verifies all five labels agree on status and reports the
    agreement rate.
    """
    by_var = {}
    by_outcome = {}
    agree = 0
    total_labels = 0
    for e in cycle_record:
        var = e.get('variable') or e.get('id', '').rsplit(':', 1)[-1]
        b = by_var.setdefault(var, [])
        b.append(e)
        key = (e.get('asset'), e.get('ts_open'), e.get('horizon'))
        by_outcome.setdefault(key, []).append(e)
    for entries in by_outcome.values():
        total_labels += len(entries)
        statuses = {e.get('status') for e in entries}
        if len(statuses) == 1:
            agree += len(entries)
    dedup_entries = [v[0] for v in by_outcome.values()]
    return {
        'by_variable': {v: _ledger_totals(es) for v, es in sorted(by_var.items())},
        'deduped': _ledger_totals(dedup_entries),
        'deduped_n_outcomes': len(by_outcome),
        'label_agreement_pct': round(agree / total_labels * 100, 1) if total_labels else None,
    }


def dedupe_cycle_entries(cycle_record):
    """One entry per distinct trade: (asset, ts_open, horizon).

    The five cycle variables grade the SAME trade five times (identical
    entry/direction/outcome, one label per variable), so the honest n
    is the deduped count. Returns (deduped_entries, n_outcomes,
    label_agreement_pct, disagreements): the representative entry is
    the first label seen for the outcome; `disagreements` counts
    labels whose (status, direction) differs from the representative
    — 0 on today's ledgers (100% agreement), and any future
    disagreement is surfaced, not silently resolved."""
    by_outcome = {}
    agree = 0
    total_labels = 0
    disagreements = 0
    for e in cycle_record or []:
        key = (e.get('asset'), e.get('ts_open'), e.get('horizon'))
        total_labels += 1
        prev = by_outcome.get(key)
        if prev is None:
            by_outcome[key] = e
            agree += 1
        elif (prev.get('status'), prev.get('direction')) == \
                (e.get('status'), e.get('direction')):
            agree += 1
        else:
            disagreements += 1
    return (list(by_outcome.values()), len(by_outcome),
            round(agree / total_labels * 100, 1) if total_labels else None,
            disagreements)


def cycle_scoreboard_deduped(cycle_record, cumulative=None):
    """Cycle grading on DISTINCT trades, not five copies of one.

    This is the fix for the cycle double-counting (audit §F): the live
    scoreboard in intraday.cycle_scoreboard counts one row per
    (variable, band, direction, horizon), so five variables grading
    the same asset-day inflate n fivefold (1,485 label rows vs 297
    distinct outcomes today). This harness-path scoreboard dedupes
    first — one outcome per (asset, ts_open, horizon) — then grades
    per (horizon, direction), since the five labels agree 100% on
    outcome and per-variable cells are five copies of the same number.
    It lives here, in the grading/harness path, NOT in the live engine:
    intraday.py is untouched and the live cycle ledger keeps recording
    one row per variable per asset-day.

    Rulers match this harness (win rate = wins / (wins + losses);
    expectancy = paper P&L / (wins + losses)); Wilson 95% lower bound
    is displayed only, never a gate. Per-row max drawdown is the
    running-$1,000-bankroll drawdown on the visible deduped leg only.

    Cumulative counters CANNOT be deduped: they are stored per
    variable-cell, already 5x-inflated, with no per-outcome keys to
    collapse. If `cumulative` is non-empty it is reported under
    'cumulative_excluded' and NOT folded into the totals — folding it
    in would re-inflate n. (Today cycle_cumulative is empty, so
    nothing is excluded.)
    """
    deduped, n_outcomes, agreement, disagreements = dedupe_cycle_entries(
        cycle_record)
    graded = [e for e in deduped
              if e.get('status') in ('won', 'lost', 'expired')]
    inflated = [e for e in (cycle_record or [])
                if e.get('status') in ('won', 'lost', 'expired')]

    def bucket():
        return {'graded': 0, 'wins': 0, 'losses': 0, 'expired': 0,
                'paper_pnl_total': 0.0, 'pnls': []}

    rows_by_key = {}
    overall = bucket()
    for e in graded:
        key = (e.get('horizon') or '?', e.get('direction') or 'LONG')
        b = rows_by_key.setdefault(key, bucket())
        for tgt in (b, overall):
            tgt['graded'] += 1
            if e.get('status') == 'won':
                tgt['wins'] += 1
            elif e.get('status') == 'lost':
                tgt['losses'] += 1
            if e.get('cycle_expired'):
                tgt['expired'] += 1
            tgt['paper_pnl_total'] += e.get('paper_pnl') or 0
            tgt['pnls'].append((e.get('ts_closed') or '',
                                e.get('paper_pnl') or 0))

    def finalize(b, horizon, direction):
        wl = b['wins'] + b['losses']
        dd = None
        if b['pnls']:
            bankroll, peak, worst = 1000.0, 1000.0, 0.0
            for _, pnl in sorted(b['pnls']):
                bankroll += pnl
                peak = max(peak, bankroll)
                worst = max(worst, (peak - bankroll) / peak * 100
                            if peak > 0 else 0.0)
            dd = round(worst, 1)
        return {
            'horizon': horizon, 'direction': direction,
            'graded': b['graded'], 'wins': b['wins'],
            'losses': b['losses'], 'expired': b['expired'],
            'win_rate': round(b['wins'] / wl * 100, 1) if wl else None,
            'wilson_lower_95': (round(wilson_lower_95(b['wins'], wl) * 100, 1)
                                if wl else None),
            'expectancy_per_1000': (round(b['paper_pnl_total'] / wl, 2)
                                    if wl else None),
            'paper_pnl_total': round(b['paper_pnl_total'], 2),
            'max_drawdown_pct': dd,
            'drawdown_visible_only': True,
        }

    rows = [finalize(b, h, d)
            for (h, d), b in sorted(rows_by_key.items())]
    result = {
        'rows': rows,
        'overall': finalize(overall, 'ALL', 'ALL'),
        'deduped_n_outcomes': n_outcomes,
        'deduped_n_graded': len(graded),
        'inflated_n_graded': len(inflated),
        'inflation_factor': (round(len(inflated) / len(graded), 2)
                             if graded else None),
        'label_agreement_pct': agreement,
        'label_disagreements': disagreements,
        'cumulative_excluded': None,
    }
    if cumulative:
        result['cumulative_excluded'] = {
            'cells': len(cumulative),
            'reason': ('per-variable cumulative counters are 5x-inflated '
                       'and carry no per-outcome keys, so they cannot be '
                       'deduped retroactively; excluded from deduped totals'),
        }
    return result


def aggregate_cmi(cmi_record):
    """CMI totals: overall, by kind, and by band for cmi_regime rows."""
    by_kind = {}
    by_band = {}
    for e in cmi_record:
        by_kind.setdefault(e.get('kind') or 'cmi_regime', []).append(e)
        if (e.get('kind') or 'cmi_regime') == 'cmi_regime':
            band = e.get('band')
            if not band:
                parts = (e.get('id') or '').split(':')
                band = parts[-2] if len(parts) >= 2 else 'unknown'
            by_band.setdefault(str(band), []).append(e)
    return {
        'overall': _ledger_totals(cmi_record),
        'by_kind': {k: _ledger_totals(v) for k, v in sorted(by_kind.items())},
        'by_band': {k: _ledger_totals(v) for k, v in sorted(by_band.items())},
    }


def aggregate_pattern_scoreboard(scoreboard):
    """Per-pattern (+direction) totals from the engine's scoreboard,
    summed across horizons. Returns {pattern|direction: totals}."""
    out = {}
    for row in scoreboard or []:
        key = f"{row.get('pattern')}|{row.get('direction')}"
        b = out.setdefault(key, {'graded': 0, 'wins': 0, 'losses': 0,
                                 'expired': 0, 'paper_pnl_total': 0.0})
        b['graded'] += row.get('graded', 0)
        b['wins'] += row.get('wins', 0)
        b['losses'] += row.get('losses', 0)
        b['expired'] += row.get('expired', 0)
        b['paper_pnl_total'] += row.get('paper_pnl_total', 0) or 0
    for b in out.values():
        wl = b['wins'] + b['losses']
        b['win_rate'] = round(b['wins'] / wl * 100, 1) if wl else None
        b['expectancy_usd'] = round(b['paper_pnl_total'] / wl, 2) if wl else None
        b['paper_pnl_total'] = round(b['paper_pnl_total'], 2)
    return out


# ------------------------------------------------------------------ verdicts

def verdict_track(win_pct, exp_usd, n):
    """WORKING / NOT WORKING / UNKNOWN for a track-level row."""
    if n is None or n < MIN_GRADED_JUDGE:
        return 'UNKNOWN — too thin'
    if win_pct is not None and win_pct >= 60 and (exp_usd or 0) > 0:
        return 'WORKING'
    if (exp_usd or 0) < 0:
        return 'NOT WORKING'
    return 'MIXED'


def track_ablation(overall, without):
    """Book/track totals with a component removed: the delta it carries."""
    return {
        'win_pct_without': without['win_rate'],
        'exp_without_usd': without['expectancy_usd'],
        'delta_win_pp': round((overall['win_rate'] or 0.0) - (without['win_rate'] or 0.0), 1)
        if overall['win_rate'] is not None and without['win_rate'] is not None else None,
        'delta_exp_usd': round((overall['expectancy_usd'] or 0.0) - (without['expectancy_usd'] or 0.0), 2)
        if overall['expectancy_usd'] is not None and without['expectancy_usd'] is not None else None,
    }


def _without_totals(a, b):
    """Totals of a minus totals of b (component removal)."""
    t = {f: (a.get(f) or 0) - (b.get(f) or 0)
         for f in ('graded', 'wins', 'losses', 'expired', 'open')}
    t['paper_pnl_total'] = round((a.get('paper_pnl_total') or 0) - (b.get('paper_pnl_total') or 0), 2)
    wl = t['wins'] + t['losses']
    t['win_rate'] = round(t['wins'] / wl * 100, 1) if wl else None
    t['expectancy_usd'] = round(t['paper_pnl_total'] / wl, 2) if wl else None
    return t


# ------------------------------------------------------------------ run

def run(repo):
    """Run the full harness against the ledgers in repo. Returns
    (results, markdown). Reads only; writes nothing."""
    history = load_history(repo)
    proof = load_proof(repo)
    proposed = load_proposed_weights(repo)
    runs = history.get('picks') or []
    train_runs, test_runs = walk_forward_split(runs)
    train_picks = graded_picks(train_runs)
    test_picks = graded_picks(test_runs)
    all_picks = graded_picks([r for r in runs if r.get('scored')])

    weights_train, winfo_train = fit_weights(train_runs)
    weights_full, winfo_full = fit_weights([r for r in runs if r.get('scored')])
    win_c, loss_c = centroids(train_picks)
    prop = proposed.get('proposed') or {}
    prop_w = prop.get('weights') or []
    base_w = (proposed.get('baseline') or {}).get('weights') or []

    # --- validation: do recomputed scores reproduce the stored ones?
    agree_n = 0
    agree_full_n = 0
    score_abs_err = []
    stored_abs = []
    for p in test_picks:
        s0, d0 = rescore(p['features'], weights_train, win_c, loss_c, p['regime'])
        if d0 == p['direction']:
            agree_n += 1
        _sf, df = rescore(p['features'], weights_full, win_c, loss_c, p['regime'])
        if df == p['direction']:
            agree_full_n += 1
        if p.get('score_stored') is not None:
            score_abs_err.append(abs(s0 - float(p['score_stored'])))
            stored_abs.append(abs(float(p['score_stored'])))
    # Published test-slice baseline (the picks as the engine actually
    # published them — the anchor the ablation deltas sit against).
    pub_wins = sum(1 for p in test_picks if p['win'])
    pub_exp = expectancy_usd([p['ret'] for p in test_picks])
    validation = {
        'train_runs': len(train_runs),
        'test_runs': len(test_runs),
        'train_picks': len(train_picks),
        'test_picks': len(test_picks),
        'train_first': train_runs[0].get('date') if train_runs else None,
        'train_last': train_runs[-1].get('date') if train_runs else None,
        'test_first': test_runs[0].get('date') if test_runs else None,
        'test_last': test_runs[-1].get('date') if test_runs else None,
        'weights_train_note': winfo_train.get('note'),
        'weights_full_note': winfo_full.get('note'),
        'direction_agreement_pct': round(agree_n / len(test_picks) * 100, 1) if test_picks else None,
        'direction_agreement_full_pct': round(agree_full_n / len(test_picks) * 100, 1) if test_picks else None,
        'score_mae': round(sum(score_abs_err) / len(score_abs_err), 2) if score_abs_err else None,
        'mean_abs_stored_score': round(sum(stored_abs) / len(stored_abs), 2) if stored_abs else None,
        'published_test_win_pct': round(win_rate_pct(pub_wins, len(test_picks) - pub_wins), 1) if test_picks else None,
        'published_test_exp_usd': round(pub_exp, 2) if pub_exp is not None else None,
    }

    # --- Table A: the 22 core features (out-of-sample, TEST slice)
    feature_rows = []
    max_delta = None
    for j, name in enumerate(FEATURE_NAMES):
        st = standalone_feature(test_picks, j)
        ab = ablate_feature(test_picks, weights_train, win_c, loss_c, j)
        feature_rows.append({
            'index': j, 'name': name,
            'weight_learned': round(weights_full[j], 3),
            'weight_heuristic': round(HEURISTIC_W[j], 3),
            'weight_proposed': round(prop_w[j], 3) if j < len(prop_w) else None,
            'weight_baseline': round(base_w[j], 3) if j < len(base_w) else None,
            'standalone': st, 'ablation': ab,
        })
        if max_delta is None or ab['delta_exp_usd'] > max_delta:
            max_delta = ab['delta_exp_usd']
    max_weight = max((abs(w) for w in weights_full), default=0.0)
    for row in feature_rows:
        ab = row['ablation']
        st = row['standalone']
        row['justified_weight'] = justified_weight(ab['delta_exp_usd'], max_delta, max_weight)
        row['verdict'] = verdict_feature(ab['delta_exp_usd'], ab['delta_win_pp'], st['spread_pp'])
    feature_rows.sort(key=lambda r: r['ablation']['delta_exp_usd'], reverse=True)

    # --- Table B: tracks, feeds, gates (full ledgers)
    record = proof.get('record') or []
    cumulative = proof.get('cumulative') or {}
    cells, per_kind, book = aggregate_book(record, cumulative, runs)
    cycle = aggregate_cycle(proof.get('cycle_record') or [])
    cmi = aggregate_cmi(proof.get('cmi_record') or [])
    pattern_sb = aggregate_pattern_scoreboard((proof.get('stats') or {}).get('pattern_scoreboard') or [])

    track_rows = []
    # main-book kinds: standalone from the merged book, ablation = book without it
    for kind in sorted(per_kind):
        t = per_kind[kind]
        n = t['graded']
        trig = (t['wins'] + t['losses']) / n if n else None
        without = _without_totals(book, t)
        track_rows.append({
            'name': f"{kind} (Proof Book kind)",
            'reads': 'engine plans, fixed +$28/−$35 levels',
            'n': n,
            'trig_pct': round(trig * 100, 1) if trig is not None else None,
            'win_pct': t['win_rate'],
            'exp_usd': t['expectancy_usd'],
            'ablation': track_ablation(book, without),
            'weight': 'shadow-graded track (no live weight)',
            'verdict': verdict_track(t['win_rate'], t['expectancy_usd'], n),
        })
    # patterns aggregate (pattern + pattern_confluence kinds combined)
    pat_tot = {'graded': 0, 'wins': 0, 'losses': 0, 'expired': 0, 'open': 0, 'paper_pnl_total': 0.0}
    for kind in ('pattern', 'pattern_confluence'):
        t = per_kind.get(kind)
        if t:
            for f in ('graded', 'wins', 'losses', 'expired', 'open'):
                pat_tot[f] += t[f]
            pat_tot['paper_pnl_total'] += t['paper_pnl_total']
    wl = pat_tot['wins'] + pat_tot['losses']
    pat_tot['win_rate'] = round(pat_tot['wins'] / wl * 100, 1) if wl else None
    pat_tot['expectancy_usd'] = round(pat_tot['paper_pnl_total'] / wl, 2) if wl else None
    without_pat = _without_totals(book, pat_tot)
    pat_trig = wl / pat_tot['graded'] if pat_tot['graded'] else None
    track_rows.append({
        'name': 'patterns — aggregate (all patterns, both directions)',
        'reads': 'Kraken 1h candles, 19 strict candle-shape rules',
        'n': pat_tot['graded'],
        'trig_pct': round(pat_trig * 100, 1) if pat_trig is not None else None,
        'win_pct': pat_tot['win_rate'],
        'exp_usd': pat_tot['expectancy_usd'],
        'ablation': track_ablation(book, without_pat),
        'weight': 'shadow-graded track (no live weight)',
        'verdict': verdict_track(pat_tot['win_rate'], pat_tot['expectancy_usd'], pat_tot['graded']),
    })
    # per-pattern rows (standalone only — all too thin for ablation)
    for key in sorted(pattern_sb):
        b = pattern_sb[key]
        n = b['wins'] + b['losses'] + b['expired']
        trig = (b['wins'] + b['losses']) / n if n else None
        track_rows.append({
            'name': f"pattern {key}",
            'reads': 'single pattern cell, scoreboard aggregate',
            'n': n,
            'trig_pct': round(trig * 100, 1) if trig is not None else None,
            'win_pct': b['win_rate'],
            'exp_usd': b['expectancy_usd'],
            'ablation': None,
            'weight': 'shadow-graded track (no live weight)',
            'verdict': verdict_track(b['win_rate'], b['expectancy_usd'], n),
        })
    # cycle variables (deduped book + per-variable; ablation degenerate)
    cyc = cycle['deduped']
    for var in sorted(cycle['by_variable']):
        t = cycle['by_variable'][var]
        n = t['graded']
        trig = (t['wins'] + t['losses']) / n if n else None
        track_rows.append({
            'name': f"cycle — {var}",
            'reads': 'calendar / Fear & Greed / BTC closes (see audit §F)',
            'n': n,
            'trig_pct': round(trig * 100, 1) if trig is not None else None,
            'win_pct': t['win_rate'],
            'exp_usd': t['expectancy_usd'],
            'ablation': None,
            'weight': 'shadow-graded track (no live weight)',
            'verdict': verdict_track(t['win_rate'], t['expectancy_usd'], n),
            'note': 'ablation n/a: five labels, one trade (see deduped row)',
        })
    cyc_n = cyc['graded']
    cyc_trig = (cyc['wins'] + cyc['losses']) / cyc_n if cyc_n else None
    track_rows.append({
        'name': 'cycle — DEDUPED book (distinct outcomes, not 5x labels)',
        'reads': 'one outcome per asset-day-horizon',
        'n': cyc_n,
        'trig_pct': round(cyc_trig * 100, 1) if cyc_trig is not None else None,
        'win_pct': cyc['win_rate'],
        'exp_usd': cyc['expectancy_usd'],
        'ablation': None,
        'weight': 'shadow-graded track (no live weight)',
        'verdict': verdict_track(cyc['win_rate'], cyc['expectancy_usd'], cyc_n),
        'note': f"label agreement {cycle['label_agreement_pct']}% across the five variables",
    })
    # CMI bands (cmi_regime rows) + confluence/trap kinds
    for band in sorted(cmi['by_band']):
        t = cmi['by_band'][band]
        n = t['graded']
        trig = (t['wins'] + t['losses']) / n if n else None
        without = _without_totals(cmi['overall'], t)
        track_rows.append({
            'name': f"CMI band {band} (LONG/SHORT, regime rows)",
            'reads': 'own sine-fit + regression CMI on Kraken 1h closes',
            'n': n,
            'trig_pct': round(trig * 100, 1) if trig is not None else None,
            'win_pct': t['win_rate'],
            'exp_usd': t['expectancy_usd'],
            'ablation': track_ablation(cmi['overall'], without),
            'weight': 'shadow-graded track (no live weight)',
            'verdict': verdict_track(t['win_rate'], t['expectancy_usd'], n),
        })
    for kind in sorted(cmi['by_kind']):
        if kind == 'cmi_regime':
            continue
        t = cmi['by_kind'][kind]
        n = t['graded']
        trig = (t['wins'] + t['losses']) / n if n else None
        track_rows.append({
            'name': f"CMI {kind}",
            'reads': 'SuperTrend flip + CMI band (see audit §G)',
            'n': n,
            'trig_pct': round(trig * 100, 1) if trig is not None else None,
            'win_pct': t['win_rate'],
            'exp_usd': t['expectancy_usd'],
            'ablation': None,
            'weight': 'shadow-graded track (no live weight)',
            'verdict': verdict_track(t['win_rate'], t['expectancy_usd'], n),
        })
    # shadow crypto + stock intraday ledgers (standalone only)
    for label, entries in (('shadow crypto scan', proof.get('shadow_record') or []),
                           ('stocks — intraday track', proof.get('stock_record') or [])):
        t = _ledger_totals(entries)
        n = t['graded']
        trig = (t['wins'] + t['losses']) / n if n else None
        track_rows.append({
            'name': label,
            'reads': 'shadow ledger entries (own record)',
            'n': n,
            'trig_pct': round(trig * 100, 1) if trig is not None else None,
            'win_pct': t['win_rate'],
            'exp_usd': t['expectancy_usd'],
            'ablation': None,
            'weight': 'shadow-graded track (no live weight)',
            'verdict': verdict_track(t['win_rate'], t['expectancy_usd'], n),
        })
    # equity swing picks (history.json, class == equity)
    eq_picks = [p for p in all_picks if p['cls'] == 'equity']
    eq_wins = sum(1 for p in eq_picks if p['win'])
    eq_exp = expectancy_usd([p['ret'] for p in eq_picks])
    track_rows.append({
        'name': 'stocks — equity swing picks (history.json)',
        'reads': 'swing engine on Yahoo/FINNHUB equity picks',
        'n': len(eq_picks),
        'trig_pct': None,
        'win_pct': round(eq_wins / len(eq_picks) * 100, 1) if eq_picks else None,
        'exp_usd': round(eq_exp, 2) if eq_exp is not None else None,
        'ablation': None,
        'weight': 'shadow/paper track (no live weight)',
        'verdict': verdict_track(round(eq_wins / len(eq_picks) * 100, 1) if eq_picks else None,
                                 round(eq_exp, 2) if eq_exp is not None else None,
                                 len(eq_picks)),
    })
    # untestable rows — no graded evidence exists, by construction
    untestable = [
        ('balance / coil score', '24h range vs 7d range, chop, hourly volume',
         'display-only; no balance value is stored on graded entries, so no outcome can be tied to it',
         '0.0 (display-only)'),
        ('econ pack — 13 variables', 'price levels, funding, OI, FRED macro',
         'display-only; all 13 sit at weight 0.0 in self-tune and no econ values are stored on graded picks',
         '0.0 (never populated)'),
        ('funding score (Kraken Futures)', 'annualised funding rate, scored [-1, +1]',
         'runner gate only (withholds runner below -0.5); the book never separates withheld vs allowed runner outcomes',
         'gate only, not a weight'),
        ('OI flow (Kraken)', 'open-interest change x price direction',
         'runner veto only (vetoes at <= -0.7); same missing counterfactual as funding',
         'gate only, not a weight'),
        ('OKX derivatives feed', 'funding, OI, long/short ratio',
         'display-only; no thresholds, no grading', '0.0 (display-only)'),
        ('Deribit options feed', 'BTC/ETH IV, skew, put/call, max pain',
         'display-only; no thresholds, no grading', '0.0 (display-only)'),
        ('DeFiLlama feed', 'stablecoin supply, TVL',
         'display-only; no score, no grading', '0.0 (display-only)'),
        ('FRED macro feed (7 series)', 'Fed CSVs, day-cached',
         'display-only; landed Nov 10, no outcome data yet', '0.0 (display-only)'),
        ('Finviz screener (stocks)', 'price, volume, RSI, market cap',
         'display-only; typed rows, never a score', '0.0 (display-only)'),
        ('precedent / analogue track', 'historical precedents 2016–2026',
         'built Nov 11 (commit d030a30); Proof Book grading wiring not yet landed, no graded outcomes',
         '0.0 (not yet graded)'),
    ]

    # --- Corrected shadow weight set: out-of-sample re-grade on the
    # SAME walk-forward split (train-fit base, so the comparison is
    # apples-to-apples), plus the joint remove-all-dead ablation and
    # the raises-only / cuts-only diagnostics that explain the result.
    # The Nov 1 self-tune proposal is NOT used as a base or an input.

    dead_indices = sorted(row['index'] for row in feature_rows
                          if 'DEAD WEIGHT' in row['verdict'])
    shadow_w_train = shadow_weights(weights_train)
    shadow_w_full = shadow_weights(weights_full)
    raises_w = list(weights_train)
    raises_w[6] = SHADOW_OVERRIDES[6]
    raises_w[2] = SHADOW_OVERRIDES[2]
    cuts_w = list(weights_train)
    for j in (20, 1, 18, 9, 11):
        cuts_w[j] = 0.0
    shadow = {
        'base': 'train-fit weights re-applied to the test slice',
        'base_weights': [round(w, 3) for w in weights_train],
        'base_grade': grade_with_weights(test_picks, weights_train,
                                         win_c, loss_c),
        'full_history_base_grade': grade_with_weights(
            test_picks, weights_full, win_c, loss_c),
        'overrides': {SHADOW_OVERRIDE_NAMES[j]: v
                      for j, v in sorted(SHADOW_OVERRIDES.items())},
        'shadow_weights': [round(w, 3) for w in shadow_w_train],
        'shadow_grade': grade_with_weights(test_picks, shadow_w_train,
                                           win_c, loss_c),
        'shadow_on_full_history_grade': grade_with_weights(
            test_picks, shadow_w_full, win_c, loss_c),
        'joint_dead_indices': dead_indices,
        'joint_dead_names': [FEATURE_NAMES[j] for j in dead_indices],
        'joint_dead_grade': grade_with_weights(
            test_picks, weights_train, win_c, loss_c,
            zero_indices=dead_indices),
        'diagnostics': {
            'raises_only_grade': grade_with_weights(
                test_picks, raises_w, win_c, loss_c),
            'cuts_only_grade': grade_with_weights(
                test_picks, cuts_w, win_c, loss_c),
        },
        'note': ('Baseline is the train-fit model re-applied to the test '
                 'slice (the same baseline the ablations use), NOT the '
                 'published picks — published test baseline is '
                 f"{validation['published_test_win_pct']}% / "
                 f"{validation['published_test_exp_usd']:+.2f} $/1k; the "
                 'two differ where the recomputed direction disagrees '
                 'with the published one (direction agreement '
                 f"{validation['direction_agreement_pct']}%)."),
    }

    # --- Nic's 50% rule: signed weights from graded proof.
    # Features: proof percentage = standalone top-tercile win rate on
    # the TRAIN slice (walk-forward discipline — weights derived from
    # train, graded on test; the test slice never sets a weight).
    # Tracks: proof percentage = the cell win rate on its full ledger.
    # One shared scale: the largest distance from 50% across ALL
    # signed items maps to MAX_SIGNED_WEIGHT (0.225).
    train_standalone = [standalone_feature(train_picks, j)
                        for j in range(N_FEATURES)]
    # Track proof percentages (win rate, n judged) for the scale and
    # the signed track weights — main-book cells, cycle deduped,
    # CMI bands, equity swings. Only n >= 20 cells earn a weight.
    track_proofs = []
    for (kind, direction, band, regime), c in sorted(cells.items()):
        wl = c['wins'] + c['losses']
        if wl >= MIN_GRADED_JUDGE:
            track_proofs.append({
                'name': f'{kind} {direction} band={band} regime={regime}',
                'kind': kind, 'win_pct': c['win_rate'], 'n': wl,
                'wins': c['wins'], 'losses': c['losses'],
                'exp_usd': c['expectancy_usd'],
                'entries': [e for e in record
                            if cell_key(e) == (kind, direction, band, regime)],
                'inversion_basis': 'visible ledger only — cumulative counters carry no per-entry path and are excluded',
            })
    cyc_dedup = cycle_scoreboard_deduped(proof.get('cycle_record') or [],
                                         proof.get('cycle_cumulative') or {})
    dedup_entries, _n_out, _agree, _dis = dedupe_cycle_entries(
        proof.get('cycle_record') or [])
    cyc_overall = cyc_dedup['overall']
    track_proofs.append({
        'name': 'cycle — DEDUPED (distinct outcomes)', 'kind': 'cycle',
        'win_pct': cyc_overall['win_rate'], 'n': cyc_overall['graded'],
        'wins': cyc_overall['wins'], 'losses': cyc_overall['losses'],
        'exp_usd': cyc_overall['expectancy_per_1000'],
        'entries': [e for e in dedup_entries
                    if e.get('status') in ('won', 'lost', 'expired')],
        'inversion_basis': 'deduped entries (distinct trades)',
    })
    for row in cyc_dedup['rows']:
        track_proofs.append({
            'name': f"cycle — DEDUPED {row['horizon']} {row['direction']}",
            'kind': 'cycle', 'win_pct': row['win_rate'],
            'n': row['graded'], 'wins': row['wins'],
            'losses': row['losses'],
            'exp_usd': row['expectancy_per_1000'],
            'entries': [e for e in dedup_entries
                        if e.get('status') in ('won', 'lost', 'expired')
                        and (e.get('horizon') or '?') == row['horizon']
                        and (e.get('direction') or 'LONG') == row['direction']],
            'inversion_basis': 'deduped entries (distinct trades)',
        })
    for band, t in sorted(cmi['by_band'].items()):
        wl = t['wins'] + t['losses']
        if wl >= MIN_GRADED_JUDGE:
            track_proofs.append({
                'name': f'CMI band {band}', 'kind': 'cmi',
                'win_pct': t['win_rate'], 'n': wl,
                'wins': t['wins'], 'losses': t['losses'],
                'exp_usd': t['expectancy_usd'],
                'entries': [e for e in (proof.get('cmi_record') or [])
                            if (e.get('kind') or 'cmi_regime') == 'cmi_regime'
                            and str(e.get('band') or '') == band],
                'inversion_basis': 'visible CMI ledger (cumulative excluded — no per-entry path)',
            })
    eq_picks = [p for p in all_picks if p['cls'] == 'equity']
    if eq_picks:
        eq_wins = sum(1 for p in eq_picks if p['win'])
        eq_exp = expectancy_usd([p['ret'] for p in eq_picks])
        track_proofs.append({
            'name': 'stocks — equity swing picks (history.json)',
            'kind': 'equity', 'win_pct': round(eq_wins / len(eq_picks) * 100, 1),
            'n': len(eq_picks), 'wins': eq_wins,
            'losses': len(eq_picks) - eq_wins,
            'exp_usd': round(eq_exp, 2) if eq_exp is not None else None,
            'entries': None,
            'inversion_basis': 'not computed — swing picks carry no fixed-level paper P&L to mirror',
        })
    distances = ([abs(st['top_win_pct'] / 100 - 0.50)
                  for st in train_standalone]
                 + [abs(t['win_pct'] / 100 - 0.50)
                    for t in track_proofs if t['win_pct'] is not None])
    fifty_scale = fifty_rule_scale(distances)
    fifty_weights = [signed_weight_50(st['top_win_pct'] / 100, fifty_scale)
                     for st in train_standalone]
    fifty_grade = grade_with_weights(test_picks, fifty_weights,
                                     win_c, loss_c)
    fifty_geo_only = grade_with_weights(test_picks, [0.0] * N_FEATURES,
                                        win_c, loss_c)
    # Per-feature ablation INSIDE the 50% set: which signed weights
    # help the rule set and which hurt it (delta = rule-set exp minus
    # feature-zeroed exp; positive = the signed weight helps).
    fifty_feature_rows = []
    for j, name in enumerate(FEATURE_NAMES):
        gz = grade_with_weights(test_picks, fifty_weights, win_c, loss_c,
                                zero_indices=[j])
        fifty_feature_rows.append({
            'index': j, 'name': name,
            'train_top_win_pct': train_standalone[j]['top_win_pct'],
            'test_top_win_pct': next(
                x for x in feature_rows if x['index'] == j)['standalone']['top_win_pct'],
            'signed_weight': fifty_weights[j],
            'weight_train_fit': round(weights_train[j], 3),
            'weight_ablation_set': shadow_w_train[j],
            'rule_set_delta_exp_usd': round(
                (fifty_grade['exp_usd'] or 0.0) - (gz['exp_usd'] or 0.0), 2),
            'zeroed_exp_usd': gz['exp_usd'],
        })
    for t in track_proofs:
        t['signed_weight'] = (signed_weight_50(t['win_pct'] / 100, fifty_scale)
                              if t['win_pct'] is not None else None)
        t['inverted'] = (invert_entries(t['entries'])
                         if t['entries'] is not None else None)
        inv = t['inverted']
        # A mirror verdict needs a real mirror: the visible-leg
        # inversion must itself clear n >= 20 judged, or it is a
        # 1–3-entry anecdote (the 100-entry main-book window holds
        # almost no graded entries for most cells today). Cycle and
        # CMI ledgers are fully visible, so their mirrors clear the
        # bar; main-book cells mostly do not, and say so.
        judged = (inv['wins'] + inv['losses']) if inv else 0
        if inv is not None and t['exp_usd'] is not None \
                and inv['expectancy_usd'] is not None \
                and judged >= MIN_GRADED_JUDGE:
            t['inversion_delta_exp_usd'] = round(
                inv['expectancy_usd'] - t['exp_usd'], 2)
            t['rule_verdict'] = ('HELPS — betting against the cell pays more than betting with it'
                                 if inv['expectancy_usd'] > t['exp_usd'] else
                                 'HURTS — betting against the cell pays less than betting with it')
        else:
            t['inversion_delta_exp_usd'] = None
            t['rule_verdict'] = ('UNPROVEN — no mirror inversion on this basis'
                                 if inv is None else
                                 f'UNPROVEN — visible mirror n={judged} < {MIN_GRADED_JUDGE}, too thin to judge the inversion')
        del t['entries']
    fifty_rule = {
        'rule': ("signed weight = (win_rate - 0.50) x SCALE; "
                 "above 50% positive, below 50% negative, 50% = zero"),
        'scale': round(fifty_scale, 4),
        'max_distance_from_50': round(max(distances), 3) if distances else None,
        'cap_weight': MAX_SIGNED_WEIGHT,
        'feature_slice': 'TRAIN slice standalone top-tercile win rates (walk-forward: weights from train, graded on test)',
        'track_slice': 'full ledgers (tracks are not walk-forward split, as in the audit)',
        'weights': fifty_weights,
        'grade': fifty_grade,
        'geo_only_grade': fifty_geo_only,
        'feature_rows': fifty_feature_rows,
        'tracks': track_proofs,
    }
    # Hunt the residual (unknown) with the corrected 50%-rule function,
    # and map F's PROPOSED terms to their graded proxies.
    residual = residual_cases(test_picks, fifty_weights, win_c, loss_c)
    results_pre = {'per_kind': per_kind, 'fifty_rule': fifty_rule,
                   'shadow': shadow}
    f_form = f_form_mapping(results_pre)
    dist95 = distance_to_95({**results_pre, 'feature_rows': feature_rows})
    results = {
        'generated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'repo': repo,
        'validation': validation,
        'feature_rows': feature_rows,
        'shadow': shadow,
        'fifty_rule': fifty_rule,
        'book': book,
        'cells': cells,
        'per_kind': per_kind,
        'cycle': cycle,
        'cycle_deduped_scoreboard': cycle_scoreboard_deduped(
            proof.get('cycle_record') or [],
            proof.get('cycle_cumulative') or {}),
        'cmi': cmi,
        'track_rows': track_rows,
        'untestable': untestable,
        'proposed_generated': proposed.get('generated'),
        'max_delta_exp_usd': max_delta,
        'max_learned_weight': max_weight,
        'residual': residual,
        'f_form': f_form,
        'distance_to_95': dist95,
    }
    return results, render_report(results)


# ------------------------------------------------------------------ report

def render_report(r):
    v = r['validation']
    L = []
    L.append('# Per-variable evaluation — OmniSight')
    L.append('')
    L.append(f"Generated {r['generated_utc']} (UTC). REVIEW ONLY — no weight, "
             'score, gate, or ledger is changed by this harness.')
    L.append('')
    L.append('## How to read this')
    L.append('')
    L.append('- **Standalone** = one variable at a time on the out-of-sample '
             'test slice: top-third vs bottom-third win rate (the Nov 11 '
             'audit\'s method).')
    L.append('- **Ablation delta** = the history re-graded with that variable '
             'zeroed out of the score. Positive dollars = the model loses '
             'that much per $1,000 without it. This is the key number. '
             'The baseline is the train-fit model re-applied to the test '
             'slice (not the published picks), so the delta isolates the '
             'variable inside one consistent model; the published '
             'test-slice baseline is in the validation section below.')
    L.append('- **Justified weight** = ablation delta, clipped at zero, '
             'scaled so the best variable carries the largest current '
             'learned weight. A variable that hurts the model justifies 0.')
    L.append('- No 95% significance gate anywhere. 95% is a displayed goal '
             'only (Nic Nov 11). A variable earns weight by graded '
             'predictive performance, not by passing a significance test.')
    L.append('- Walk-forward split: oldest 70% of scored runs train the '
             f"weights ({v['train_runs']} runs, {v['train_first']} to "
             f"{v['train_last']}, {v['train_picks']} picks); newest 30% test "
             f"them ({v['test_runs']} runs, {v['test_first']} to "
             f"{v['test_last']}, {v['test_picks']} picks).")
    L.append('- Track rows use their full ledgers (not the walk-forward '
             'slice), as the audit did. Rulers: swing picks win = stored '
             'win flag, expectancy = mean direction-signed return x $10; '
             'Proof Book cells win rate = wins / (wins + losses), '
             'expectancy = paper P&L / (wins + losses). n graded counts '
             'wins + losses + expired; Trig% is the share of graded '
             'entries that actually triggered a call — a win rate without '
             'the trigger rate overstates a track that mostly expires.')
    L.append('')
    L.append('## Validation — does the re-scoring reproduce the engine?')
    L.append('')
    L.append(f"- Direction agreement on the test slice: **{v['direction_agreement_pct']}%** "
             '(recomputed direction vs the direction the engine published, '
             'train-fit weights).')
    L.append(f"- Direction agreement with FULL-history weights: "
             f"**{v['direction_agreement_full_pct']}%** — lower than the "
             'train-fit agreement, which is the weight-instability finding: '
             'least-squares weights swing window to window (e.g. '
             'acceleration −0.81 train-fit vs +0.13 full-history), and '
             f"test-slice scores sit within ±2 of zero (mean |score| "
             f"{v['mean_abs_stored_score']}), so the published direction on "
             'near-zero-score picks is fragile. The geo edge is not the '
             'cause (removing it moves agreement by only ~3 points).')
    L.append(f"- Published test-slice baseline (as the engine published it): "
             f"**{fmt(v['published_test_win_pct'])}% win, "
             f"{v['published_test_exp_usd']:+.2f} $/1k** over "
             f"{v['test_picks']} picks.")
    L.append(f"- Score mean absolute error vs stored scores: **{v['score_mae']}** points.")
    L.append(f"- Train weights: {v['weights_train_note']}.")
    L.append(f"- Full-history weights: {v['weights_full_note']}.")
    L.append('- If direction agreement were low, the ablation below would '
             'not be trustworthy — it is the check on the whole method. '
             '77% is moderate: the ablation is still the honest '
             'out-of-sample read, but every delta is measured on a model '
             'whose direction flips on 1 in 4 test picks when the weights '
             'are refit.')
    L.append('')
    L.append('## Table A — the 22 core features (out-of-sample test slice)')
    L.append('')
    L.append('| # | Feature | Current w (learned) | Heuristic w | Proposal Nov 1 | '
             'Standalone top-3rd win% (n) | Spread (pp) | Ablation Δwin (pp) | '
             'Ablation Δexp $/1k (±SE) | Flips | Justified w | Verdict |')
    L.append('|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|')
    for row in r['feature_rows']:
        st, ab = row['standalone'], row['ablation']
        L.append(f"| {row['index']} | {row['name']} | {row['weight_learned']} | "
                 f"{row['weight_heuristic']} | {fmt(row['weight_proposed'], 3)} | "
                 f"{fmt(st['top_win_pct'])} ({st['top_n']}) | {fmt(st['spread_pp'])} | "
                 f"{fmt(ab['delta_win_pp'])} | {fmt(ab['delta_exp_usd'], 2)} "
                 f"(±{fmt(ab['delta_exp_se_usd'], 2)}) | {ab['flips']} | "
                 f"{row['justified_weight']} | {row['verdict']} |")
    L.append('')
    L.append('## Corrected shadow weight set (out-of-sample re-grade)')
    L.append('')
    sh = r['shadow']
    L.append('Shadow-only set from the harness\'s out-of-sample ablation '
             '(optimization brief §1). The Nov 1 self-tune proposal is NOT '
             'used anywhere here — its signs are opposite to the ablation. '
             'Trend_align / ma_align are zeroed, never flipped negative. '
             'Nothing here touches a live weight, score, gate, or ledger.')
    L.append('')
    L.append('| Feature | Train-fit w | Full-history learned w | Shadow w |')
    L.append('|---|---:|---:|---:|')
    rows_by_idx = {x['index']: x for x in r['feature_rows']}
    for idx, _val in sorted(SHADOW_OVERRIDES.items()):
        row = rows_by_idx.get(idx)
        if row is None:
            continue  # render test fixtures may carry a subset of rows
        name = SHADOW_OVERRIDE_NAMES[idx]
        L.append(f"| {name} | {sh['base_weights'][idx]} | "
                 f"{row['weight_learned']} | {sh['shadow_weights'][idx]} |")
    L.append('')
    acc_row = rows_by_idx.get(6)
    L.append('Train-fit w is the weight re-fit on this run\'s train slice '
             '(the base the shadow set is built on); full-history learned w '
             'is the same fit on all 1,432 scored picks, shown because the '
             'justified weights were scaled to that fit\'s largest weight.'
             + (f" The two fits disagree on sign for acceleration "
                f"(train-fit {sh['base_weights'][6]}, full-history "
                f"{acc_row['weight_learned']}) — the weight-instability "
                f"finding, and the reason the re-grade below is the check "
                f"that matters." if acc_row is not None else ''))
    L.append('')
    L.append('| Weight set | Win% | Expectancy $/1k (±SE) | n |')
    L.append('|---|---:|---:|---:|')
    bg, sg = sh['base_grade'], sh['shadow_grade']
    L.append(f"| Current (train-fit, re-applied to test slice) | "
             f"{fmt(bg['win_pct'])} | {fmt(bg['exp_usd'], 2)} "
             f"(±{fmt(bg['exp_se_usd'], 2)}) | {bg['n']} |")
    L.append(f"| Corrected shadow set | {fmt(sg['win_pct'])} | "
             f"{fmt(sg['exp_usd'], 2)} (±{fmt(sg['exp_se_usd'], 2)}) | "
             f"{sg['n']} |")
    L.append(f"| Shadow set on full-history weights | "
             f"{fmt(sh['shadow_on_full_history_grade']['win_pct'])} | "
             f"{fmt(sh['shadow_on_full_history_grade']['exp_usd'], 2)} | "
             f"{sh['shadow_on_full_history_grade']['n']} |")
    L.append(f"| Current (full-history, re-applied) | "
             f"{fmt(sh['full_history_base_grade']['win_pct'])} | "
             f"{fmt(sh['full_history_base_grade']['exp_usd'], 2)} | "
             f"{sh['full_history_base_grade']['n']} |")
    L.append('')
    delta = (sg['exp_usd'] or 0.0) - (bg['exp_usd'] or 0.0)
    L.append(f"Shadow minus current (train-fit base): **{delta:+.2f} $/1k** "
             f"over {sg['n']} test picks. "
             + ('The corrected set BEATS the current weights out-of-sample.'
                if delta > 0 else
                'The corrected set LOSES to the current weights '
                'out-of-sample — the ablation-derived raises do not survive '
                'being applied together (see diagnostics below). Do not '
                'promote this set on the strength of the single-feature '
                'ablations alone.'))
    L.append('')
    L.append('### Joint remove-all-dead ablation')
    L.append('')
    jg = sh['joint_dead_grade']
    L.append(f"Zeroing all {len(sh['joint_dead_indices'])} dead-weight "
             f"features at once (everything except acceleration): "
             f"**{fmt(jg['win_pct'])}% win, {fmt(jg['exp_usd'], 2)} $/1k "
             f"(±{fmt(jg['exp_se_usd'], 2)}) over {jg['n']} picks**, vs "
             f"the current {fmt(bg['exp_usd'], 2)} $/1k. "
             + ('Joint removal BEATS the current weights — mass zeroing '
                'has out-of-sample support, but the decision stays with '
                'the report, not this harness.'
                if (jg['exp_usd'] or 0) > (bg['exp_usd'] or 0) else
                'Joint removal LOSES to the current weights — the '
                'single-feature dead-weight verdicts do NOT add up to a '
                'joint case. Do NOT zero all 21 at once.'))
    L.append('')
    L.append('### Diagnostics — why the shadow set moves the way it does')
    L.append('')
    dg = sh['diagnostics']
    L.append(f"- Raises only (acceleration → 0.225, psych_bias → 0.173, "
             f"no cuts): {fmt(dg['raises_only_grade']['win_pct'])}% win, "
             f"{fmt(dg['raises_only_grade']['exp_usd'], 2)} $/1k.")
    L.append(f"- Cuts only (the five zeroed, no raises): "
             f"{fmt(dg['cuts_only_grade']['win_pct'])}% win, "
             f"{fmt(dg['cuts_only_grade']['exp_usd'], 2)} $/1k.")
    acc_row = next((x for x in r['feature_rows'] if x['index'] == 6), None)
    acc_txt = ''
    if acc_row is not None:
        acc_txt = (f" — and the train-fit weight it was removed from is "
                   f"{sh['base_weights'][6]} for acceleration "
                   f"(full-history learned {acc_row['weight_learned']})")
    L.append('- Read this with the weight-instability finding: the '
             'ablation tested REMOVING each feature (to zero), never the '
             'positive justified weight itself' + acc_txt + ', so '
             '"justified +0.225" is an extrapolation across a sign flip '
             'the data never tested. A justified weight is a proposal for '
             'the next ablation round, not a validated weight.')
    L.append('')
    L.append(sh['note'])
    L.append('')
    L.append('## The 50% rule — signed weights from graded proof (Nic Nov 11)')
    L.append('')
    fr = r['fifty_rule']
    L.append('Nic\'s rule: "If you have over 50% proof it signal as '
             'positive. And <50% as negative and weighs accordingly to '
             'percentage." A+B=C: the graded win rate goes in, a signed '
             'weight comes out, the out-of-sample expectancy is the '
             'outcome. Scaling (one transparent choice): signed weight = '
             f"(win_rate − 0.50) × {fr['scale']} — the largest distance "
             f"from 50% across all signed items "
             f"({fr['max_distance_from_50']:.3f}) maps to the largest "
             f"current learned weight ({fr['cap_weight']}). Features use "
             f"{fr['feature_slice'].lower()}; tracks use "
             f"{fr['track_slice'].lower()}.")
    L.append('')
    L.append('| Weight set | Win% | Expectancy $/1k (±SE) | n |')
    L.append('|---|---:|---:|---:|')
    bg, sg, fg = (sh['base_grade'], sh['shadow_grade'], fr['grade'])
    L.append(f"| Current (train-fit) | {fmt(bg['win_pct'])} | "
             f"{fmt(bg['exp_usd'], 2)} (±{fmt(bg['exp_se_usd'], 2)}) | "
             f"{bg['n']} |")
    L.append(f"| Ablation set (comparison benchmark) | {fmt(sg['win_pct'])} | "
             f"{fmt(sg['exp_usd'], 2)} (±{fmt(sg['exp_se_usd'], 2)}) | "
             f"{sg['n']} |")
    L.append(f"| **50% rule (signed)** | {fmt(fg['win_pct'])} | "
             f"{fmt(fg['exp_usd'], 2)} (±{fmt(fg['exp_se_usd'], 2)}) | "
             f"{fg['n']} |")
    L.append('')
    d50 = (fg['exp_usd'] or 0.0) - (bg['exp_usd'] or 0.0)
    d50a = (fg['exp_usd'] or 0.0) - (sg['exp_usd'] or 0.0)
    L.append(f"50% rule minus current: **{d50:+.2f} $/1k**. 50% rule minus "
             f"ablation set: **{d50a:+.2f} $/1k**. "
             + ('The rule BEATS the current weights out-of-sample.'
                if d50 > 0 else
                'The rule LOSES to the current weights out-of-sample — '
                'signed proof percentages do not beat the least-squares '
                'weights on this split. The rule is graded, not assumed.'))
    L.append('')
    L.append('Why the rule grades the way it does: the TRAIN-slice '
             'standalone win rates cluster at 50% (42.5–52.5% across all '
             '22 features), so the signed weights are tiny (largest '
             '|w| = '
             f"{max(abs(w) for w in fr['weights']):.3f}) and the score is "
             'dominated by the geometric-edge term, not by the weights. '
             'A geo-only score (all weights zero) grades '
             f"{fmt(fr['geo_only_grade']['win_pct'])}% / "
             f"{fmt(fr['geo_only_grade']['exp_usd'], 2)} $/1k on the same "
             'slice — the rule set is barely steering a geo-dominated '
             'model, because the train window gives almost no feature a '
             'proof percentage far from 50%. That is the mechanism, and '
             'it is why the rule cannot beat the current weights here: '
             'there is almost nothing to weigh with.')
    L.append('')
    L.append('### 50% rule — the 22 features (train proof → signed weight)')
    L.append('')
    L.append('| Feature | Train top-win% | Test top-win% | Signed w | '
             'Train-fit w | Ablation-set w | Rule-set Δexp $/1k |')
    L.append('|---|---:|---:|---:|---:|---:|---:|')
    for row in fr['feature_rows']:
        L.append(f"| {row['name']} | {fmt(row['train_top_win_pct'])} | "
                 f"{fmt(row['test_top_win_pct'])} | {row['signed_weight']} | "
                 f"{row['weight_train_fit']} | "
                 f"{row['weight_ablation_set']} | "
                 f"{fmt(row['rule_set_delta_exp_usd'], 2)} |")
    L.append('')
    L.append('Rule-set Δexp: removing that feature from the 50% set — '
             'positive means its signed weight HELPS the rule set, '
             'negative means it HURTS it (threshold ±$1/1k; inside that '
             'is noise). Acceleration, the case Nic asked to settle: '
             'train top-win 51.6% → signed **+0.009** (positive, tiny) — '
             'NOT negative. The negative read (34.8%) comes from the TEST '
             'slice, and deriving weights from the test slice would be '
             'look-ahead, so the rule correctly refuses it. Inside the '
             'rule set, acceleration\'s signed weight is worth '
             f"{fmt(next(x for x in fr['feature_rows'] if x['name'] == 'acceleration')['rule_set_delta_exp_usd'], 2)}"
             ' $/1k — noise either way. The standalone-vs-ablation '
             'tension is resolved in part, not fully: the ablation keeps '
             'acceleration at its NEGATIVE train-fit weight (−0.81) and '
             'earns +$12.19/1k for keeping it — which agrees in SIGN '
             'with the test-slice standalone inversion (high acceleration '
             '→ loses → negative weight helps) — but the ablation does NOT '
             'support the positive justified +0.225. What is still '
             'UNPROVEN is WHY acceleration helps at a negative weight '
             'while its top third wins only 34.8% on the test slice and '
             '51.6% on the train slice: three reads, three answers, no '
             'mechanism. The graded numbers stand; the why does not.')
    L.append('')
    _setup_pk = (r.get('per_kind') or {}).get('setup') or {}
    _setup_win = _setup_pk.get('win_rate')
    _setup_exp = _setup_pk.get('expectancy_usd')
    L.append('Order-flow footprint (Nic\'s framing): accumulation setups '
             f"({_setup_win}% when triggered, +${_setup_exp}/1k) are real "
             'buyers building quietly before the move, and acceleration '
             '(the only feature earning its weight in the ablation) is the '
             'move starting. The footprint is the graded evidence; the '
             'mechanism behind acceleration\'s sign is unproven (see '
             'above).')
    L.append('')
    L.append('### 50% rule — tracks and cells (win rate → signed weight → mirror grade)')
    L.append('')
    L.append('A below-50% LONG cell counts AGAINST the long side in '
             'shadow: its signed weight is negative, and the mirror grade '
             'bets against the cell (same entries, opposite side) to test '
             'whether the inversion pays. Mirror inversion is first-order: '
             'horizon closes flip drift exactly; fixed-level hits swap '
             '+$28/−$35; the true opposite trade\'s touch order can '
             'differ, so the mirrored expectancy is an estimate, not an '
             'exact re-grade.')
    L.append('')
    L.append('| Track / cell | Win% (n) | Signed w | Original exp $/1k | '
             'Mirror win% (mirror n) | Mirror exp $/1k | Rule verdict |')
    L.append('|---|---:|---:|---:|---:|---:|---|')
    for t in fr['tracks']:
        inv = t['inverted']
        inv_win = fmt(inv['win_rate']) if inv else '—'
        inv_n = inv.get('n_graded', '—') if inv else '—'
        inv_exp = fmt(inv['expectancy_usd'], 2) if inv else '—'
        L.append(f"| {t['name']} | {fmt(t['win_pct'])} ({t['n']}) | "
                 f"{t['signed_weight']} | {fmt(t['exp_usd'], 2)} | "
                 f"{inv_win} ({inv_n}) | {inv_exp} | {t['rule_verdict']} |")
    L.append('')
    by_track = {t['name']: t for t in fr['tracks']}
    mirror_txt = ''
    if 'cycle — DEDUPED (distinct outcomes)' in by_track and \
            'CMI band CLEAN' in by_track:
        cyc_t = by_track['cycle — DEDUPED (distinct outcomes)']
        clean_t = by_track['CMI band CLEAN']
        mirror_txt = (f" (cycle deduped: mirror "
                      f"{fmt(cyc_t['inverted']['expectancy_usd'], 2) if cyc_t['inverted'] else '—'}"
                      f" $/1k vs {fmt(cyc_t['exp_usd'], 2)}; CMI CLEAN: mirror "
                      f"{fmt(clean_t['inverted']['expectancy_usd'], 2) if clean_t['inverted'] else '—'}"
                      f" $/1k vs {fmt(clean_t['exp_usd'], 2)})")
    L.append('Read the verdicts per cell, not as a whole. The rule HELPS '
             'exactly where a real mirror pays more than the original — '
             'the losing LONG tracks with full ledgers'
             f"{mirror_txt}. Main-book cells mostly carry NO verdict today: "
             'their mirror runs on the visible 100-entry window only '
             '(pattern LONG has 3 visible graded entries, setups 0), so '
             'the inversion is too thin to judge and the verdict is '
             'UNPROVEN — the setup weight stays positive on the win rate '
             'alone, and its mirror disproof would need the full setup '
             'ledger, which the cumulative counters cannot supply. A 51% '
             'cell is barely positive by construction — its signed weight '
             'is near zero and its mirror is near breakeven either way.')
    L.append('')
    L.append('## Hunting the residual (unknown)')
    L.append('')
    L.append("F = a + b + c + d + (f − e) + unknown (PROPOSED mapping — Nic "
             "confirms the letters). The loop (grade → correct) works on "
             "the MEASURABLE. The unknown is the OPEN DOOR — what the "
             "loop reaches toward but cannot yet measure: beyond math and "
             "physics, what can't be measured yet. It is NOT solved here "
             "and NOT dressed up as math — no invented value, no fake "
             "precision. The residual hunt IS the empirical side of it: "
             "the cases below are where F is most wrong, and that is "
             "where the unknown lives. They are named, not explained away.")
    L.append("")
    L.append("What would count as EVIDENCE for the unknown (the bar — "
             "not claimed met): the residual shrinking over t with no "
             "change to the measurable terms (something unmeasured being "
             "captured), or residual cases clustering around moves the "
             "measurable terms cannot see at all — moves with no flow, "
             "no pattern, no macro trigger, the market moving on "
             "something the instruments don't read. Until the grades say "
             "otherwise, the unknown stays unknown.")
    L.append("")
    L.append("These are the 50%-rule function's biggest misses on the "
             "same test slice (n=466), ranked by |shadow score| — F's "
             "conviction.")
    L.append('')
    res = r.get('residual') or {'calls_lost': [], 'fades_won': []}
    L.append('### Highest-conviction shadow calls that lost')
    L.append('')
    L.append('| Asset | Date | F predicted | F score | Happened | '
             'Return | Dollar miss / $1k |')
    L.append('|---|---|---|---:|---|---:|---:|')
    for c in res['calls_lost']:
        L.append(f"| {c['asset']} | {c['run_date']} | {c['f_direction']} | "
                 f"{c['f_score']} | {c['outcome']} | "
                 f"{fmt(c['published_return_pct'], 2)}% | "
                 f"${c['dollar_miss_per_1000']:.2f} |")
    L.append('')
    L.append('### Strongest shadow fades that won anyway')
    L.append('')
    L.append('| Asset | Date | F faded (shadow) | F score | Published | '
             'Happened | Return | Dollar miss / $1k |')
    L.append('|---|---|---|---:|---|---|---:|---:|')
    for c in res['fades_won']:
        L.append(f"| {c['asset']} | {c['run_date']} | {c['f_direction']} | "
                 f"{c['f_score']} | {c['published_direction']} | "
                 f"{c['outcome']} | "
                 f"{fmt(c['published_return_pct'], 2)}% | "
                 f"${c['dollar_miss_per_1000']:.2f} |")
    L.append('')
    L.append("Read: a call that lost is F's conviction failing — the "
             'unknown is whatever the six terms missed on that pick. A '
             "fade that won is F's caution failing — the unknown is "
             'whatever the published call saw that F faded. The next '
             'iteration of F(t+1) = correct(F(t), grades) starts here: '
             'these cases are the evidence the loop corrects on.')
    L.append('')
    L.append('## F — the formula (PROPOSED mapping)')
    L.append('')
    L.append('F = a + b + c + d + (f − e) + unknown. PROPOSED letter '
             'mapping — Nic confirms the letters. For each term: what '
             'currently proxies it in the graded data, and whether that '
             'proxy is graded or ungraded.')
    L.append('')
    L.append('| Term | Name | Ingredient | Current proxy | Graded? |')
    L.append('|---|---|---|---|---|')
    for t in (r.get('f_form') or {}).get('terms', []):
        L.append(f"| {t['term']} | {t['name']} | {t['ingredient']} | "
                 f"{t['proxy']} | {t['graded']} |")
    L.append('')
    L.append('## Distance to 95% (the target Nic set)')
    L.append('')
    L.append('>95% graded proof = a VERY ACCURATE read. The loop keeps '
             'correcting TOWARD 100% accuracy as its direction, even '
             'though 100% could seem impossible — the direction is the '
             'point, not the arrival. 95% is a READ, never a gate: no '
             'candidate is ever blocked for being under 95%. Closest '
             'read first:')
    L.append('')
    L.append('| Read | Win% (n) | Basis | Distance to 95% |')
    L.append('|---|---:|---|---:|')
    for d in r.get('distance_to_95') or []:
        L.append(f"| {d['name']} | {fmt(d['win_pct'])} ({d['n']}) | "
                 f"{d['basis']} | {fmt(d['distance_to_95'])} pts short |")
    L.append('')
    L.append('## Table B — tracks, feeds, gates')
    L.append('')
    L.append('| Variable / track | n graded | Trig% | Win% | Expectancy $/1k | '
             'Ablation: book/track without it (Δwin pp / Δexp $/1k) | '
             'Current weight / role | Verdict |')
    L.append('|---|---:|---:|---:|---:|---|:---|')
    for t in r['track_rows']:
        ab = t['ablation']
        ab_s = (f"{fmt(ab['delta_win_pp'])} / {fmt(ab['delta_exp_usd'], 2)}"
                if ab else '—')
        extra = f" — {t['note']}" if t.get('note') else ''
        L.append(f"| {t['name']} | {t['n']} | {fmt(t.get('trig_pct'))} | {fmt(t['win_pct'])} | "
                 f"{fmt(t['exp_usd'], 2)} | {ab_s} | {t['weight']} | "
                 f"{t['verdict']}{extra} |")
    L.append('')
    L.append('## Cycle — deduped grading (distinct trades, not 5x labels)')
    L.append('')
    cyc = r['cycle_deduped_scoreboard']
    L.append('FIX (harness path only — intraday.py untouched): the live '
             'cycle scoreboard counts one row per (variable, band, '
             'direction, horizon), so five variables grading the same '
             'asset-day inflate n fivefold. This scoreboard dedupes first '
             '— one outcome per (asset, ts_open, horizon) — then grades '
             'per (horizon, direction), because the five labels agree '
             f"{cyc['label_agreement_pct']}% on outcome and per-variable "
             'cells are five copies of the same number. Wilson 95% is '
             'displayed only, never a gate.')
    L.append('')
    L.append(f"- Distinct outcomes: **{cyc['deduped_n_outcomes']}** "
             f"({cyc['deduped_n_graded']} graded) vs "
             f"**{cyc['inflated_n_graded']}** graded label rows — "
             f"inflation factor **{cyc['inflation_factor']}x**. Label "
             f"disagreements: {cyc['label_disagreements']}.")
    if cyc['cumulative_excluded']:
        L.append(f"- Cumulative counters EXCLUDED from deduped totals: "
                 f"{cyc['cumulative_excluded']['reason']}.")
    L.append('')
    L.append('| Horizon | Direction | Graded | Wins | Losses | Win% | '
             'Wilson lower 95% | Expectancy $/1k | Max drawdown % |')
    L.append('|---|---|---:|---:|---:|---:|---:|---:|---:|')
    for row in cyc['rows']:
        L.append(f"| {row['horizon']} | {row['direction']} | "
                 f"{row['graded']} | {row['wins']} | {row['losses']} | "
                 f"{fmt(row['win_rate'])} | {fmt(row['wilson_lower_95'])} | "
                 f"{fmt(row['expectancy_per_1000'], 2)} | "
                 f"{fmt(row['max_drawdown_pct'])} |")
    ov = cyc['overall']
    L.append(f"| **ALL** | **ALL** | **{ov['graded']}** | **{ov['wins']}** | "
             f"**{ov['losses']}** | **{fmt(ov['win_rate'])}** | "
             f"**{fmt(ov['wilson_lower_95'])}** | "
             f"**{fmt(ov['expectancy_per_1000'], 2)}** | "
             f"**{fmt(ov['max_drawdown_pct'])}** |")
    L.append('')
    L.append('### Untestable — no graded evidence exists (by construction)')
    L.append('')
    for name, reads, why, weight in r['untestable']:
        L.append(f"- **{name}** ({reads}) — {why}. Current weight: {weight}.")
    L.append('')
    L.append('## Plain-words summary')
    L.append('')
    earners = [x for x in r['feature_rows'] if x['verdict'].startswith('EARNS')]
    hurting = [x for x in r['feature_rows'] if x['verdict'].startswith('HURTS')]
    dead = [x for x in r['feature_rows'] if 'DEAD WEIGHT' in x['verdict']]
    inverted = [x for x in r['feature_rows'] if 'INVERTED' in x['verdict']]
    L.append(f"- **Earn their weight ({len(earners)}):** "
             + (', '.join(f"{x['name']} (+${x['ablation']['delta_exp_usd']}/1k, "
                          f"justified {x['justified_weight']})" for x in earners)
                or 'none — no feature clears the +$10/1k bar on the test slice.'))
    L.append(f"- **Hurt the model ({len(hurting)}):** "
             + (', '.join(f"{x['name']} ({x['ablation']['delta_exp_usd']:+.2f}$/1k)"
                          for x in hurting) or 'none.'))
    L.append(f"- **Dead weight ({len(dead)}):** removing them changes the "
             'test-slice result by less than $10 per $1,000 either way — '
             'they carry weight the grades do not justify. Read this with '
             'care: single-feature deltas on this slice are almost all '
             'inside the noise, because the score is a diffuse sum of 22 '
             'small contributions and test-slice scores sit within ±2 of '
             'zero — no single feature CAN move the needle much this way. '
             'The ranking above (which features help most, which hurt) and '
             'the "read backwards" list are the usable signal, not the '
             'absolute dollars.')
    L.append(f"- **Read backwards ({len(inverted)}):** "
             + (', '.join(f"{x['name']} (#{x['index']})" for x in inverted)
                or 'none — no feature\'s top tercile wins 5+ points less than its bottom tercile.'))
    underweighted = [x for x in r['feature_rows']
                     if x['justified_weight'] > abs(x['weight_learned']) + 0.05]
    L.append('- **Under-weighted (justified weight above current weight):** '
             + (', '.join(f"{x['name']} (current {x['weight_learned']}, "
                          f"justified {x['justified_weight']})" for x in underweighted)
                or 'none.'))
    biggest_mismatch = sorted(
        r['feature_rows'],
        key=lambda x: abs(x['weight_learned']) - x['justified_weight'],
        reverse=True)[:3]
    L.append('- **Biggest weight-vs-evidence mismatches (learned weight far '
             'above justified weight):** '
             + ', '.join(f"{x['name']} (current {x['weight_learned']}, "
                         f"justified {x['justified_weight']})" for x in biggest_mismatch)
             + '.')
    L.append(f"- **The weights themselves are unstable:** the train-fit "
             f"model agrees with the published direction on "
             f"{v['direction_agreement_pct']}% of test picks, but weights "
             f"fit on the full history agree on only "
             f"{v['direction_agreement_full_pct']}%. Least-squares weights "
             'swing window to window (acceleration flips sign between '
             'fits), so the model\'s direction on near-zero-score picks '
             'depends on which window the weights were fit on. Any '
             '"weights from grades" proposal should be built to be stable '
             'across windows, not refit from scratch each run.')
    setup_row = next((t for t in r['track_rows']
                      if t['name'].startswith('setup (')), None)
    cyc_row = next((t for t in r['track_rows']
                    if t['name'].startswith('cycle — DEDUPED')), None)
    setup_txt = (f"setups win {setup_row['win_pct']}% of triggered calls "
                 f"but trigger only {setup_row['trig_pct']}% of graded "
                 f"entries" if setup_row else 'setups are the only working family')
    cyc_txt = (f"Cycle's honest n is {cyc_row['n']} distinct graded "
               f"outcomes" if cyc_row else 'Cycle\'s honest n is the '
               'deduped count')
    L.append(f'- **Tracks:** the accumulation setups are still the only '
             f'working family ({setup_txt}); patterns, cycle and CMI lose '
             f'on win rate and dollars at real n. {cyc_txt}, not 1,450 — '
             'the five variables are five labels on one trade.')
    L.append('- **What this does not prove:** a feature can be dead weight '
             'on its own and still matter inside the combination (features '
             'are correlated), and the test slice is one falling window '
             '(Oct 31 – Nov 8, mostly risk-off). Weights from grades are '
             'applied by the correction loop (autonomous, shadow and live '
             'per the settled scope, F(t+1) = correct(F(t), grades)) — '
             'every change is logged with its graded outcome, and a wrong '
             'correction is reversed by the next iteration, never hidden.')
    sh = r['shadow']
    L.append(f"- **Corrected shadow set (re-graded out-of-sample, same "
             f"split):** {fmt(sh['shadow_grade']['win_pct'])}% win, "
             f"{fmt(sh['shadow_grade']['exp_usd'], 2)} $/1k vs current "
             f"{fmt(sh['base_grade']['win_pct'])}% / "
             f"{fmt(sh['base_grade']['exp_usd'], 2)} $/1k "
             f"(n={sh['shadow_grade']['n']}). Joint remove-all-dead: "
             f"{fmt(sh['joint_dead_grade']['win_pct'])}% / "
             f"{fmt(sh['joint_dead_grade']['exp_usd'], 2)} $/1k. See the "
             f"shadow section above for the verdict — do not read the "
             f"single-feature ablations as a joint result.")
    cyc = r['cycle_deduped_scoreboard']
    L.append(f"- **Cycle, deduped:** {cyc['overall']['wins']}W / "
             f"{cyc['overall']['losses']}L over "
             f"{cyc['overall']['graded']} distinct graded outcomes "
             f"({fmt(cyc['overall']['win_rate'])}% win, "
             f"{fmt(cyc['overall']['expectancy_per_1000'], 2)} $/1k) — "
             f"not {cyc['inflated_n_graded']} label rows.")
    fr = r['fifty_rule']
    L.append(f"- **50% rule (Nic's signed weights):** "
             f"{fmt(fr['grade']['win_pct'])}% win, "
             f"{fmt(fr['grade']['exp_usd'], 2)} $/1k vs current "
             f"{fmt(sh['base_grade']['exp_usd'], 2)} $/1k and ablation set "
             f"{fmt(sh['shadow_grade']['exp_usd'], 2)} $/1k "
             f"(n={fr['grade']['n']}). Train proof percentages cluster at "
             f"50%, so the signed weights are tiny and the score stays "
             f"geo-dominated — see the 50% section for the per-feature and "
             f"per-cell verdicts.")
    res = r.get('residual') or {'calls_lost': [], 'fades_won': []}
    if res['calls_lost'] or res['fades_won']:
        worst = res['calls_lost'][0] if res['calls_lost'] else None
        best_fade = res['fades_won'][0] if res['fades_won'] else None
        L.append('- **Residual (unknown) — where F was most wrong:** '
                 + (f"biggest losing call {worst['asset']} "
                    f"({worst['run_date']}, F {worst['f_direction']} "
                    f"score {worst['f_score']}, miss "
                    f"${worst['dollar_miss_per_1000']:.2f}/1k)"
                    if worst else 'no losing calls in the top 5')
                 + (f"; biggest fade-that-won {best_fade['asset']} "
                    f"({best_fade['run_date']}, F faded to "
                    f"{best_fade['f_direction']} score "
                    f"{best_fade['f_score']}, miss "
                    f"${best_fade['dollar_miss_per_1000']:.2f}/1k)"
                    if best_fade else '')
                 + '. No value invented for unknown — these cases ARE the '
                 'hunt. The unknown is the OPEN DOOR, not a solved term: '
                 'evidence that would count is the residual shrinking over '
                 't with no change to the measurable terms, or residual '
                 'cases clustering on moves nothing measurable can see. '
                 'Neither is met — the unknown stays unknown.')
    dist = r.get('distance_to_95') or []
    if dist:
        L.append('- **Distance to 95% (very accurate read):** '
                 + '; '.join(f"{d['name']} {fmt(d['win_pct'])}% "
                             f"({fmt(d['distance_to_95'])} pts short, n={d['n']})"
                             for d in dist[:3])
                 + '. Direction: keep correcting toward 100%.')
    L.append('')
    L.append('## Limits of this harness')
    L.append('')
    L.append('- Ablation re-grades published picks under their ablated '
             'direction; it does not re-run top-k selection.')
    L.append('- The ablation baseline is the train-fit model re-applied to '
             'the test slice, not the published picks — the published '
             'test-slice baseline is in the validation section, and the '
             'two can differ where the recomputed direction disagrees '
             'with the published one.')
    L.append('- The geometric edge is recomputed from TRAIN centroids; the '
             'live engine used expanding history, so recomputed scores are '
             'close to — not identical with — stored scores (see the '
             'direction-agreement check above).')
    L.append('- The 60% buy bar and confidence tempering are not modeled '
             'in the ablation.')
    L.append('- Track rows are full-ledger aggregates, not walk-forward; '
             'their n grows as the book grows, so re-run daily.')
    L.append('- Many features share identical tercile splits on this slice '
             '(a one-direction market ranks momentum features the same '
             'way), so standalone spreads are not independent reads of '
             'each feature.')
    L.append('- Thresholds (+$10/1k earns, −$10/1k hurts, 5pp inverted) '
             'are reporting conventions for this review, not gates.')
    L.append('')
    L.append('## Schedule')
    L.append('')
    L.append('Run daily after the grading run (no cron is created by this '
             'harness — the command below is the schedulable step):')
    L.append('')
    L.append('```')
    L.append('python3 Scripts/variable_tests.py')
    L.append('```')
    L.append('')
    L.append('It reads only the ledgers on disk (free, no keys, no network) '
             'and writes one dated report to the goal\'s hidden_files.')
    return '\n'.join(L) + '\n'


def default_out_dir(repo):
    return os.path.join(os.path.dirname(os.path.abspath(repo)), 'hidden_files')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo', default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help='repo root (default: derived from this file)')
    ap.add_argument('--out', default=None,
                    help='report output dir (default: <goal>/hidden_files)')
    args = ap.parse_args()
    results, markdown = run(args.repo)
    out_dir = args.out or default_out_dir(args.repo)
    os.makedirs(out_dir, exist_ok=True)
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    path = os.path.join(out_dir, f'variable-tests-{today}.md')
    with open(path, 'w') as f:
        f.write(markdown)
    print(f'wrote {path}')
    v = results['validation']
    print(f"test slice: {v['test_picks']} picks, direction agreement "
          f"{v['direction_agreement_pct']}%, score MAE {v['score_mae']}")
    earners = [x for x in results['feature_rows'] if x['verdict'].startswith('EARNS')]
    hurting = [x for x in results['feature_rows'] if x['verdict'].startswith('HURTS')]
    print(f"features earning weight: {len(earners)}; hurting: {len(hurting)}; "
          f"dead weight: {sum(1 for x in results['feature_rows'] if 'DEAD WEIGHT' in x['verdict'])}")
    sh = results['shadow']
    print(f"shadow set: {sh['shadow_grade']['win_pct']}% win, "
          f"{sh['shadow_grade']['exp_usd']:+.2f} $/1k vs current "
          f"{sh['base_grade']['win_pct']}% / "
          f"{sh['base_grade']['exp_usd']:+.2f} $/1k "
          f"(n={sh['shadow_grade']['n']}); joint remove-all-dead: "
          f"{sh['joint_dead_grade']['exp_usd']:+.2f} $/1k")
    cyc = results['cycle_deduped_scoreboard']['overall']
    print(f"cycle deduped: {cyc['graded']} graded, {cyc['win_rate']}% win, "
          f"{cyc['expectancy_per_1000']:+.2f} $/1k")
    fr = results['fifty_rule']
    print(f"50% rule: {fr['grade']['win_pct']}% win, "
          f"{fr['grade']['exp_usd']:+.2f} $/1k vs current "
          f"{sh['base_grade']['exp_usd']:+.2f} $/1k (n={fr['grade']['n']})")
    return 0


if __name__ == '__main__':
    sys.exit(main())
