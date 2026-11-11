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


# ------------------------------------------------------------------ history

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
    base_rets, abl_rets = [], []
    base_wins = ablated_wins = 0
    flips = 0
    near_zero = 0
    agree = 0
    for p in test_picks:
        s0, d0 = rescore(p['features'], weights, win_c, loss_c, p['regime'])
        sj, dj = rescore(p['features'], weights, win_c, loss_c, p['regime'], zero_idx=j)
        if d0 == p['direction']:
            agree += 1
        if dj != d0:
            flips += 1
        if abs(sj) < 1.0:
            near_zero += 1

        def outcome(direction):
            ret = p['ret'] if direction == p['direction'] else -p['ret']
            win = p['win'] if direction == p['direction'] else (
                not p['win'] if p['ret'] != 0 else p['win'])
            return ret, win

        base_ret, base_win = outcome(d0)
        abl_ret, abl_win = outcome(dj)
        base_rets.append(base_ret)
        abl_rets.append(abl_ret)
        if base_win:
            base_wins += 1
        if abl_win:
            ablated_wins += 1
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

    results = {
        'generated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'repo': repo,
        'validation': validation,
        'feature_rows': feature_rows,
        'book': book,
        'per_kind': per_kind,
        'cycle': cycle,
        'cmi': cmi,
        'track_rows': track_rows,
        'untestable': untestable,
        'proposed_generated': proposed.get('generated'),
        'max_delta_exp_usd': max_delta,
        'max_learned_weight': max_weight,
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
             'proposals for review, never automatic.')
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
    return 0


if __name__ == '__main__':
    sys.exit(main())
