#!/usr/bin/env python3
"""Fast-correction actuator — OmniSight, Nic Nov 11.

F(t) is the function at time t: its weights, its proof, its unknown AS
OF that grading run. F(t+1) = correct(F(t), grades) — each grading run
is a new t, forever ("shadow forever, infinity loop until correct").
The loop's TARGET is 100% accuracy as its direction, even though 100%
could seem impossible — the direction is the point, not the arrival.
>95% graded proof = a VERY ACCURATE read; 95% is a READ, never a gate
— no candidate is ever blocked for being under 95%.

"Trial and error — but the loop must correct itself immediately on
graded evidence." (variable audit §O.) At each grading run this step
reads the latest graded ledgers and APPLIES corrections automatically
— no per-correction approval, no review-only hold (Nic Nov 11: "No
make the action to correct I don't need to approve if it correct or
incorrect"). A correction applies whether it turns out right or wrong:
the grades judge it, the log shows it, and the next iteration corrects
again — including REVERSING a wrong correction when the grades say so.
A wrong correction is not a failure; the loop learns from being wrong
too.

What one iteration does (F(t) -> F(t+1)):
  1. Recomputes the 50%-rule signed weights from the latest grades and
     SETS the feature weights to them (the 50% rule governs; fresh
     grades recompute the weights each run, so a wrong weight is
     overwritten by the next run's evidence — that is the reversal).
  2. Down-weights any track cell crossing a losing line (win <= 35%
     OR negative expectancy at n >= 20) by a factor proportional to
     its graded performance.
  3. Runs the gate log's one-step adjust (passed+won strengthens the
     carrying term, passed+lost weakens it, blocked+would-have-won
     loosens the blocker, blocked+would-have-lost confirms the block),
     folded into the term multipliers and the shadow caution line.
  4. Writes the new F(t) state (versioned by t), the live weight file,
     and a correction-log entry for EVERY change with its graded
     evidence; on later runs it grades each prior correction as
     helped / hurt / neutral from the fresh grades.

Losing line (either leg trips it, at n >= 20 graded):
  - win rate <= 35%, OR
  - negative expectancy (dollars per $1,000).
95% (Wilson) is DISPLAYED ONLY, never a gate — every change below
carries its Wilson lower bound and its distance to 95% as readings,
and neither ever changes the decision.

Down-weight math (weights proportional to graded performance):
  - Track cells (Proof Book cells, cycle deduped, CMI bands): the
    shadow multiplier starts at 1.0 (full weight). factor =
    min(win_pct / 50, max(0, 1 + exp_usd / 20)) — a 50%-win cell
    keeps full weight, a 25%-win cell keeps half, and an expectancy
    of -$20/1k or worse drives the factor to 0. A cell that trips
    only the win-rate leg keeps its expectancy leg at 1.0 (and vice
    versa); the worse leg sets the factor.
  - Core features: the graded performance is the ablation delta
    (dollars per $1,000 the model gains by keeping the feature).
    Losing = ablation delta < 0, OR standalone top-tercile win <= 35%
    while the delta is below the +$10 earns bar. Exempt when the
    feature EARNS weight (delta >= +$10/1k) — an earning feature is
    never down-weighted for a weak standalone read. factor =
    min(1 + delta / 10, top_win / 35) over the legs that trip, and
    the new shadow weight is the current shadow weight x factor
    (magnitude only — a negative weight moves toward zero, it never
    flips sign). A step smaller than 0.005 in weight is reported as
    immaterial, not as a change: single-feature deltas are tiny
    (worst losing delta today is -$0.88/1k), so the honest feature
    steps are small and the report says so instead of dressing
    them up.

GUARDS (the loop's guards, updated for the settled scope):
  (a) corrections apply automatically — IN SHADOW and LIVE, no
      per-correction approval (Nic: autonomous correction includes
      the live weights/gates);
  (b) the 50% rule and the n>=20 minimum govern every weight change;
  (c) the actuator moves weights ONLY through this loop and logs every
      correction with its graded outcome — right and wrong both visible;
  (d) NOTHING ELSE touches a live gate, bar, or weight — and the system
      NEVER places an order, spends, or touches an account. A buy is
      the CALL/ALERT; Nic executes the trade himself. This boundary is
      stated at every live change point in this module.

State (all in the goal's hidden_files, unless noted):
  - f-state.jsonl — append-only, one F(t) snapshot per line (weights +
    proof + unknown as of that run); any t can be read back.
  - shadow-state.json — the current F(t), rewritten each run.
  - correction-log.jsonl — every correction with its graded evidence
    and, on later runs, whether it helped or hurt.
  - gate-log.jsonl — the gate log (see Scripts/gate_log.py).
  - data/corrected_weights.json (in the repo) — the LIVE weight state
    the engine reads each run (see nexintel.load_corrected_weights).
Safe to call from the daily grading step:
  python3 Scripts/variable_tests.py && python3 Scripts/shadow_actuator.py
Free data only: it reads the ledgers already on disk, no keys, no
network. Exit 0 when the state, log, and dated files are written.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import variable_tests as vt  # noqa: E402  (harness — single source of truth)
import gate_log as gl  # noqa: E402  (gate log — verdicts + one-step adjust)

LOSE_WIN_PCT = 35.0          # win rate at/below this is a losing line
MIN_N = 20                   # below this n, a cell is too thin to judge
EXP_FLOOR_USD = 20.0         # expectancy at/below -$20/1k drives factor to 0
DELTA_SCALE_USD = 10.0       # feature ablation delta scale (earns bar)
MIN_MATERIAL_DELTA = 0.005   # feature weight step below this is immaterial
WIN_FULL_WEIGHT_PCT = 50.0   # track win rate that keeps full shadow weight
TARGET_ACCURACY_PCT = 95.0   # >95% graded proof = a VERY ACCURATE read (READ, never a gate)
TARGET_DIRECTION_PCT = 100.0  # the loop corrects TOWARD 100% accuracy; it never arrives
LIVE_WEIGHTS_FILENAME = 'corrected_weights.json'  # live weight state, in repo/data/


# ------------------------------------------------------------------ factors

def track_factor(win_pct, exp_usd):
    """Shadow multiplier for a losing track cell, 0..1, plus reasons.

    factor = min(win leg, expectancy leg). A leg that does not trip
    contributes 1.0 (no cut from that leg). The Wilson bound is NOT
    an input — it is displayed only, by design.
    """
    factors = []
    reasons = []
    if win_pct is not None:
        factors.append(max(0.0, win_pct / WIN_FULL_WEIGHT_PCT))
        if win_pct <= LOSE_WIN_PCT:
            reasons.append(f'win {win_pct:.1f}% <= {LOSE_WIN_PCT:.0f}% line')
    if exp_usd is not None and exp_usd < 0:
        factors.append(max(0.0, 1.0 + exp_usd / EXP_FLOOR_USD))
        reasons.append(f'expectancy {exp_usd:+.2f} $/1k < 0')
    if not factors:
        return 1.0, reasons
    return round(min(1.0, min(factors)), 3), reasons


def is_losing_track(win_pct, exp_usd, n):
    """The losing line: win <= 35% OR negative expectancy, at n >= 20."""
    if n is None or n < MIN_N:
        return False
    if win_pct is not None and win_pct <= LOSE_WIN_PCT:
        return True
    return exp_usd is not None and exp_usd < 0


def feature_factor(row):
    """(is_losing, factor, reasons) for one core-feature harness row.

    Losing = ablation delta < 0, OR standalone top-tercile win <= 35%
    while the delta is below the +$10 earns bar. EARNS-weight
    features are exempt: a feature the ablation says earns its place
    is never down-weighted for a weak standalone read (acceleration
    reads 34.8% standalone and earns +$12.19/1k — the standalone
    leg does not trip it).
    """
    ab = row['ablation']
    st = row['standalone']
    delta = ab['delta_exp_usd']
    top_win = st['top_win_pct']
    if row['verdict'].startswith('EARNS'):
        return False, 1.0, ['earns weight — exempt']
    factors = []
    reasons = []
    if delta is not None and delta < 0:
        factors.append(max(0.0, 1.0 + delta / DELTA_SCALE_USD))
        reasons.append(f'ablation delta {delta:+.2f} $/1k < 0')
    if top_win is not None and top_win <= LOSE_WIN_PCT and \
            (delta is None or delta < vt.EARN_DELTA_EXP):
        factors.append(max(0.0, top_win / LOSE_WIN_PCT))
        reasons.append(f'standalone top-tercile win {top_win:.1f}% '
                       f'<= {LOSE_WIN_PCT:.0f}% line')
    if not reasons:
        return False, 1.0, []
    return True, round(min(factors), 3), reasons


def wilson_display(wins, n):
    """Wilson 95% lower bound in percent, for display only."""
    lo = vt.wilson_lower_95(wins, n) if n else None
    return round(lo * 100, 1) if lo is not None else None


# ------------------------------------------------------------------ propose

def propose(results):
    """Proposed shadow changes from one harness run's results.

    Returns a dict with feature_changes, track_changes, immaterial
    (losing lines crossed with a sub-0.005 step), skipped_thin
    (cells below n=20), and counts. Nothing here writes anything.
    """
    shadow = results['shadow']
    shadow_w = shadow['shadow_weights']
    feature_changes = []
    immaterial = []
    checked_features = 0
    for row in results['feature_rows']:
        checked_features += 1
        losing, factor, reasons = feature_factor(row)
        if not losing:
            continue
        old = shadow_w[row['index']]
        new = round(old * factor, 3)
        entry = {
            'feature': row['name'],
            'index': row['index'],
            'old_shadow_weight': old,
            'factor': factor,
            'new_shadow_weight': new,
            'reasons': reasons,
            'ablation_delta_exp_usd': row['ablation']['delta_exp_usd'],
            'standalone_top_win_pct': row['standalone']['top_win_pct'],
            'n_test_picks': shadow['base_grade']['n'],
            'verdict': row['verdict'],
        }
        if abs(new - old) < MIN_MATERIAL_DELTA:
            immaterial.append(entry)
        else:
            feature_changes.append(entry)

    track_changes = []
    skipped_thin = []
    checked_cells = 0

    def add_track(name, kind, n, wins, losses, win_pct, exp_usd,
                  reads, mechanism):
        nonlocal checked_cells
        checked_cells += 1
        if n is None or n < MIN_N:
            skipped_thin.append({'name': name, 'n': n,
                                 'reason': f'n={n} < {MIN_N} — too thin'})
            return
        if not is_losing_track(win_pct, exp_usd, n):
            return
        factor, reasons = track_factor(win_pct, exp_usd)
        track_changes.append({
            'name': name,
            'kind': kind,
            'n': n,
            'wins': wins,
            'losses': losses,
            'win_pct': win_pct,
            'expectancy_usd': exp_usd,
            'wilson_lower_95_pct': wilson_display(wins, wins + losses),
            'old_shadow_multiplier': 1.0,
            'factor': factor,
            'new_shadow_multiplier': factor,
            'reasons': reasons,
            'reads': reads,
            'mechanism': mechanism,
        })

    # Main-book cells (kind, direction, band, regime) — finest grain.
    for (kind, direction, band, regime), c in sorted(
            results['cells'].items()):
        wl = c['wins'] + c['losses']
        add_track(
            f'{kind} {direction} band={band} regime={regime}', kind,
            wl, c['wins'], c['losses'], c['win_rate'],
            c['expectancy_usd'],
            reads=('Proof Book plans, fixed +$28 win / -$35 loss levels' if
                   kind in ('mover', 'pattern', 'pattern_confluence',
                            'setup', 'signal')
                   else 'Proof Book plans'),
            mechanism=('Mechanism unproven — the grade shows this cell '
                       'loses; which condition inside the cell drives the '
                       'losses is not established by the grades.'))
    # Cycle, deduped (distinct trades — the per-variable rows are five
    # copies of one trade and are never actuated on).
    cyc = results['cycle_deduped_scoreboard']
    ov = cyc['overall']
    add_track('cycle — DEDUPED (distinct outcomes)', 'cycle',
              ov['graded'], ov['wins'], ov['losses'], ov['win_rate'],
              ov['expectancy_per_1000'],
              reads='one outcome per asset-day-horizon, LONG almost everywhere',
              mechanism=('Explained in part: the track calls LONG in nearly '
                         'every band by design, so in a risk-off falling '
                         'window it loses by construction — the direction '
                         'is the mechanism, not a mystery. Which cycle '
                         'variable (if any) would call the turn is unproven: '
                         'all five labels agree 100%, so no variable can '
                         'be credited or blamed separately.'))
    for row in cyc['rows']:
        add_track(f"cycle — DEDUPED {row['horizon']} {row['direction']}",
                  'cycle', row['graded'], row['wins'], row['losses'],
                  row['win_rate'], row['expectancy_per_1000'],
                  reads='one outcome per asset-day-horizon',
                  mechanism=('Same as the deduped overall: LONG-only calls '
                             'in a falling window.'))
    # CMI bands (cmi_regime rows).
    for band, t in sorted(results['cmi']['by_band'].items()):
        wl = t['wins'] + t['losses']
        add_track(f'CMI band {band}', 'cmi', wl, t['wins'], t['losses'],
                  t['win_rate'], t['expectancy_usd'],
                  reads='own sine-fit + regression CMI on Kraken 1h closes',
                  mechanism=('Mechanism unproven and backwards from the '
                             'name: the band called CLEAN (supposedly safe '
                             'to trade) is the worst performer, so either '
                             'the bands do not measure what their names '
                             'claim or "clean" price action is where longs '
                             'get run over in this window. The grade stands; '
                             'the why does not — yet.'))
    # Shadow / stock ledgers (not in the main-book cells). Wins/losses
    # come from the ledgers themselves, never back-computed from a
    # percentage — the track rows carry no wins/losses split.
    proof = vt.load_proof(results['repo'])
    for name, totals, reads in (
            ('shadow crypto scan',
             vt._ledger_totals(proof.get('shadow_record') or []),
             'shadow ledger entries (own record)'),
            ('stocks — intraday track',
             vt._ledger_totals(proof.get('stock_record') or []),
             'shadow ledger entries (own record)')):
        add_track(name, 'shadow/stock', totals['wins'] + totals['losses'],
                  totals['wins'], totals['losses'], totals['win_rate'],
                  totals['expectancy_usd'], reads=reads,
                  mechanism=('Mechanism unproven — the grade shows the '
                             'track loses at this n; the why is not '
                             'established.'))
    # Equity swing picks (history.json, class == equity).
    history = vt.load_history(results['repo'])
    eq_picks = [p for p in vt.graded_picks(
        [r for r in (history.get('picks') or []) if r.get('scored')])
        if p['cls'] == 'equity']
    eq_wins = sum(1 for p in eq_picks if p['win'])
    eq_exp = vt.expectancy_usd([p['ret'] for p in eq_picks])
    eq_win_pct = (round(eq_wins / len(eq_picks) * 100, 1)
                  if eq_picks else None)
    eq_exp_r = round(eq_exp, 2) if eq_exp is not None else None
    eq_win_txt = f'{eq_win_pct}%' if eq_win_pct is not None else 'n/a'
    eq_exp_txt = (f'{eq_exp_r:+.2f} $/1k' if eq_exp_r is not None
                  else 'n/a')
    add_track('stocks — equity swing picks (history.json)', 'shadow/stock',
              len(eq_picks), eq_wins, len(eq_picks) - eq_wins,
              eq_win_pct, eq_exp_r,
              reads='swing engine on Yahoo/FINNHUB equity picks',
              mechanism=(f'Mixed evidence, flagged not proven: the win rate '
                         f'({eq_win_txt}) trips the losing line but the '
                         f'expectancy is positive ({eq_exp_txt}) — '
                         f'the OR rule down-weights on the win-rate leg '
                         f'alone. Whether the low win rate or the dollars '
                         f'is the truer read is not established; the '
                         f'15-minute Yahoo delay is a known suspect, not a '
                         f'proven cause.'))
    return {
        'generated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'repo': results['repo'],
        'losing_line': {
            'win_pct_at_or_below': LOSE_WIN_PCT,
            'or_negative_expectancy_usd_per_1k': True,
            'min_n': MIN_N,
            'wilson_95': 'displayed only, never a gate',
        },
        'checked': {'features': checked_features,
                    'track_cells': checked_cells},
        'feature_changes': feature_changes,
        'feature_immaterial': immaterial,
        'track_changes': track_changes,
        'skipped_thin': skipped_thin,
        'fifty_rule_shadow_set': fifty_rule_block(results),
    }


def distance_to_95(win_pct):
    """Points short of the 95% very-accurate line. 95% is a READ,
    never a gate — the distance is shown, it never blocks anything."""
    if win_pct is None:
        return None
    return round(TARGET_ACCURACY_PCT - win_pct, 1)


# ------------------------------------------------------------------ F(t) state

def state_file_paths(out_dir):
    return {
        'f_state': os.path.join(out_dir, 'f-state.jsonl'),
        'shadow_state': os.path.join(out_dir, 'shadow-state.json'),
        'correction_log': os.path.join(out_dir, 'correction-log.jsonl'),
        'gate_log': os.path.join(out_dir, 'gate-log.jsonl'),
    }


def load_prev_state(out_dir):
    """Last F(t) snapshot, or None on the first run (t starts at 1)."""
    path = state_file_paths(out_dir)['f_state']
    if not os.path.exists(path):
        return None
    prev = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    prev = json.loads(line)
                except json.JSONDecodeError:
                    continue
    return prev


def load_correction_log(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def save_correction_log(path, entries):
    """Rewrite the log with all entries. Entries are never deleted;
    outcomes are filled in on later runs, so a correction's whole
    history — what changed, the evidence, whether it helped or hurt —
    stays in one place."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w') as f:
        for e in entries:
            f.write(json.dumps(e) + '\n')


def all_track_multipliers(results, proposal, prev_mult):
    """Multiplier for every checked cell: losing -> its factor (fresh
    from full weight each run, so a recovered cell returns to 1.0 on
    its own), too-thin -> carry the previous value, everything else
    -> 1.0."""
    losing = {t['name']: t['factor'] for t in proposal['track_changes']}
    thin = {s['name'] for s in proposal['skipped_thin']}
    names = []
    for (kind, direction, band, regime) in sorted(results['cells']):
        names.append(f'{kind} {direction} band={band} regime={regime}')
    cyc = results['cycle_deduped_scoreboard']
    names.append('cycle — DEDUPED (distinct outcomes)')
    for row in cyc['rows']:
        names.append(f"cycle — DEDUPED {row['horizon']} {row['direction']}")
    for band in sorted(results['cmi']['by_band']):
        names.append(f'CMI band {band}')
    names += ['shadow crypto scan', 'stocks — intraday track',
              'stocks — equity swing picks (history.json)']
    mult = {}
    for name in names:
        if name in losing:
            mult[name] = losing[name]
        elif name in thin:
            mult[name] = (prev_mult or {}).get(name, 1.0)
        else:
            mult[name] = 1.0
    return mult


def build_state(t, results, proposal, term_mult, caution_buy_pct, prev):
    """The F(t) snapshot: weights + proof + unknown as of this run.
    Weights are the 50%-rule signed weights from the latest grades,
    folded with the gate-log term multipliers (c scales the psychology
    features 2/3/4, d scales acceleration 6 and the setup cells)."""
    fr = results.get('fifty_rule') or {}
    weights = list(fr.get('weights') or [0.0] * vt.N_FEATURES)
    for j in (2, 3, 4):
        weights[j] = round(weights[j] * term_mult.get('c', 1.0), 4)
    weights[6] = round(weights[6] * term_mult.get('d', 1.0), 4)
    prev_mult = (prev or {}).get('track_multipliers') or {}
    mult = all_track_multipliers(results, proposal, prev_mult)
    d_mult = term_mult.get('d', 1.0)
    if d_mult != 1.0:
        for name in mult:
            if name.startswith('setup '):
                mult[name] = round(mult[name] * d_mult, 4)
    res = results.get('residual') or {}
    misses = [c['dollar_miss_per_1000']
              for c in (res.get('calls_lost') or []) + (res.get('fades_won') or [])]
    return {
        't': t,
        'timestamp': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'weights': [round(w, 4) for w in weights],
        'weights_source': ('50%-rule signed weights from latest grades, '
                           'folded with gate-log term multipliers'),
        'track_multipliers': mult,
        'term_multipliers': {k: round(v, 4) for k, v in term_mult.items()},
        'caution_buy_pct': caution_buy_pct,
        'proof': {
            'fifty_rule_exp_usd': (fr.get('grade') or {}).get('exp_usd'),
            'current_exp_usd': results['shadow']['base_grade'].get('exp_usd'),
            'ablation_exp_usd': results['shadow']['shadow_grade'].get('exp_usd'),
            'n_test_picks': (fr.get('grade') or {}).get('n'),
            'scale': fr.get('scale'),
        },
        'unknown': {
            'residual_calls_lost': len(res.get('calls_lost') or []),
            'residual_fades_won': len(res.get('fades_won') or []),
            'biggest_miss_usd_per_1k': max(misses) if misses else 0.0,
        },
    }


def _track_evidence(results, proposal, name):
    """Graded evidence for one track cell by name (win%, n, expectancy,
    Wilson, distance to 95%). Returns None when the cell cannot be
    resolved from this run's results."""
    ch = next((t for t in proposal['track_changes'] if t['name'] == name),
              None)
    if ch:
        return {'win_pct': ch['win_pct'], 'n': ch['n'],
                'wins': ch['wins'], 'losses': ch['losses'],
                'expectancy_usd': ch['expectancy_usd'],
                'wilson_lower_95_pct': ch['wilson_lower_95_pct'],
                'distance_to_95': distance_to_95(ch['win_pct'])}
    import re as _re
    m = _re.match(r'^(\w+) (LONG|SHORT) band=(\S+) regime=(\S+)$', name)
    if m:
        c = results['cells'].get((m.group(1), m.group(2), m.group(3),
                                  m.group(4)))
        if c:
            wl = c['wins'] + c['losses']
            return {'win_pct': c['win_rate'], 'n': wl,
                    'wins': c['wins'], 'losses': c['losses'],
                    'expectancy_usd': c['expectancy_usd'],
                    'wilson_lower_95_pct': wilson_display(c['wins'], wl),
                    'distance_to_95': distance_to_95(c['win_rate'])}
    cyc = results['cycle_deduped_scoreboard']
    if name == 'cycle — DEDUPED (distinct outcomes)':
        ov = cyc['overall']
        return {'win_pct': ov['win_rate'], 'n': ov['graded'],
                'wins': ov['wins'], 'losses': ov['losses'],
                'expectancy_usd': ov['expectancy_per_1000'],
                'wilson_lower_95_pct': wilson_display(ov['wins'],
                                                      ov['graded']),
                'distance_to_95': distance_to_95(ov['win_rate'])}
    for row in cyc['rows']:
        if name == f"cycle — DEDUPED {row['horizon']} {row['direction']}":
            return {'win_pct': row['win_rate'], 'n': row['graded'],
                    'wins': row['wins'], 'losses': row['losses'],
                    'expectancy_usd': row['expectancy_per_1000'],
                    'wilson_lower_95_pct': wilson_display(row['wins'],
                                                          row['graded']),
                    'distance_to_95': distance_to_95(row['win_rate'])}
    if name.startswith('CMI band '):
        band = name[len('CMI band '):]
        t = results['cmi']['by_band'].get(band)
        if t:
            wl = t['wins'] + t['losses']
            return {'win_pct': t['win_rate'], 'n': wl,
                    'wins': t['wins'], 'losses': t['losses'],
                    'expectancy_usd': t['expectancy_usd'],
                    'wilson_lower_95_pct': wilson_display(t['wins'], wl),
                    'distance_to_95': distance_to_95(t['win_rate'])}
    return None


def diff_state(prev, state, t, results, proposal, applied_gate):
    """Every change from F(t-1) to F(t) as correction-log entries.
    First run (no prev): every weight/multiplier is a change from the
    train-fit baseline the loop replaces. Each entry carries its graded
    evidence and a null outcome — the outcome is graded on later runs."""
    entries = []
    ts = state['timestamp']
    fr = results.get('fifty_rule') or {}
    feat_rows = {r['name']: r for r in fr.get('feature_rows') or []}
    prev_w = (prev or {}).get('weights')
    for j, name in enumerate(vt.FEATURE_NAMES):
        new = state['weights'][j]
        old = prev_w[j] if prev_w is not None else None
        if old is not None and round(new - old, 4) == 0:
            continue
        row = feat_rows.get(name) or {}
        top_win = row.get('train_top_win_pct')
        entries.append({
            't': t, 'timestamp': ts, 'scope': 'shadow', 'kind': 'feature',
            'target': name, 'old_value': old, 'new_value': new,
            'evidence': {
                'train_top_win_pct': top_win,
                'rule_set_delta_exp_usd': row.get('rule_set_delta_exp_usd'),
                'n_test_picks': (fr.get('grade') or {}).get('n'),
                'distance_to_95': distance_to_95(top_win),
                'note': ('50%-rule signed weight from train proof; '
                         'fresh grades recompute it each run')},
            'outcome': None, 'outcome_t': None, 'outcome_evidence': None})
    prev_mult = (prev or {}).get('track_multipliers') or {}
    for name, new in sorted(state['track_multipliers'].items()):
        old = prev_mult.get(name, 1.0)
        if round(new - old, 4) == 0:
            continue
        ev = _track_evidence(results, proposal, name) or {}
        entries.append({
            't': t, 'timestamp': ts, 'scope': 'shadow', 'kind': 'track',
            'target': name, 'old_value': old, 'new_value': new,
            'evidence': {**ev, 'note': 'losing-line down-weight factor'},
            'outcome': None, 'outcome_t': None, 'outcome_evidence': None})
    prev_term = (prev or {}).get('term_multipliers') or {}
    for k in ('c', 'd'):
        new = state['term_multipliers'].get(k, 1.0)
        old = prev_term.get(k, 1.0)
        if round(new - old, 4) == 0:
            continue
        gate = next((a for a in (applied_gate or [])
                     if a['term'] == k and a.get('applied')), None)
        entries.append({
            't': t, 'timestamp': ts, 'scope': 'shadow', 'kind': 'term',
            'target': f'term {k}', 'old_value': old, 'new_value': new,
            'evidence': {'gate_reason': (gate or {}).get('reason'),
                         'gate_n': (gate or {}).get('n'),
                         'note': 'gate-log one-step adjust'},
            'outcome': None, 'outcome_t': None, 'outcome_evidence': None})
    prev_caution = (prev or {}).get('caution_buy_pct', 50.0)
    if round(state['caution_buy_pct'] - prev_caution, 4) != 0:
        gate = next((a for a in (applied_gate or [])
                     if a['term'] == 'a' and a.get('applied')), None)
        entries.append({
            't': t, 'timestamp': ts, 'scope': 'shadow', 'kind': 'caution',
            'target': 'shadow caution line (buy%)',
            'old_value': prev_caution,
            'new_value': state['caution_buy_pct'],
            'evidence': {'gate_reason': (gate or {}).get('reason'),
                         'gate_n': (gate or {}).get('n'),
                         'note': ('shadow caution line only — the live '
                                  'caution gate moves when the shadow '
                                  "line's move is itself graded as helping")},
            'outcome': None, 'outcome_t': None, 'outcome_evidence': None})
    # The live weight file is rewritten every run from the same state —
    # one entry per run so any bad live correction is visible and
    # reversible by the next iteration.
    entries.append({
        't': t, 'timestamp': ts, 'scope': 'live', 'kind': 'live-weights',
        'target': 'repo/data/corrected_weights.json',
        'old_value': prev_w,
        'new_value': state['weights'],
        'evidence': {
            'fifty_rule_exp_usd': state['proof']['fifty_rule_exp_usd'],
            'current_exp_usd': state['proof']['current_exp_usd'],
            'n_test_picks': state['proof']['n_test_picks'],
            'note': ('live scoring now uses these weights; the OOS grade '
                     'above is the evidence — if it is worse than current, '
                     'the next iteration corrects again')},
        'outcome': None, 'outcome_t': None, 'outcome_evidence': None})
    return entries


def _current_track_exp(results, name):
    """Current expectancy ($/1k) for a track cell by name, for grading
    a prior correction. None when unresolvable."""
    import re as _re
    m = _re.match(r'^(\w+) (LONG|SHORT) band=(\S+) regime=(\S+)$', name)
    if m:
        c = results['cells'].get((m.group(1), m.group(2), m.group(3),
                                  m.group(4)))
        return c['expectancy_usd'] if c else None
    cyc = results['cycle_deduped_scoreboard']
    if name == 'cycle — DEDUPED (distinct outcomes)':
        return cyc['overall']['expectancy_per_1000']
    for row in cyc['rows']:
        if name == f"cycle — DEDUPED {row['horizon']} {row['direction']}":
            return row['expectancy_per_1000']
    if name.startswith('CMI band '):
        t = results['cmi']['by_band'].get(name[len('CMI band '):])
        return t['expectancy_usd'] if t else None
    return None


def grade_prior_corrections(log, results, t):
    """Fill in outcomes for corrections from earlier iterations, from
    the FRESH grades: helped / hurt / neutral. Right and wrong must
    BOTH be visible — a wrong correction is not a failure, it is what
    the next iteration corrects (including reversing it). Entries with
    no resolvable evidence stay pending (outcome null)."""
    fr = results.get('fifty_rule') or {}
    deltas = {r['name']: r.get('rule_set_delta_exp_usd')
              for r in fr.get('feature_rows') or []}
    setup_exp = ((results.get('per_kind') or {}).get('setup') or {}
                 .get('expectancy_usd'))
    cur_fifty = (fr.get('grade') or {}).get('exp_usd')
    for e in log:
        if e.get('outcome') is not None or e.get('t', 0) >= t:
            continue
        kind = e.get('kind')
        ev = e.get('evidence') or {}
        verdict = None
        out_ev = None
        if kind == 'feature':
            d = deltas.get(e['target'])
            if d is not None:
                verdict = 'helped' if d > 0 else 'hurt' if d < 0 else 'neutral'
                out_ev = {'rule_set_delta_exp_usd': d,
                          'note': ('the signed weight earns its keep in '
                                   'the fresh grades' if d > 0 else
                                   'the signed weight loses money in the '
                                   'fresh grades' if d < 0 else
                                   'the signed weight is breakeven in the '
                                   'fresh grades')}
        elif kind == 'track':
            cur = _current_track_exp(results, e['target'])
            old = ev.get('expectancy_usd')
            if cur is not None and old is not None:
                verdict = ('helped' if cur > old else
                           'hurt' if cur < old else 'neutral')
                out_ev = {'expectancy_usd_then': old,
                          'expectancy_usd_now': cur}
        elif kind == 'term':
            d = deltas.get('psych_bias' if e['target'] == 'term c'
                           else 'acceleration')
            if d is not None:
                verdict = 'helped' if d > 0 else 'hurt' if d < 0 else 'neutral'
                out_ev = {'folded_feature_delta_exp_usd': d}
        elif kind == 'caution':
            old = ev.get('setup_exp_usd_then')
            if setup_exp is not None and old is not None:
                verdict = ('helped' if setup_exp > old else
                           'hurt' if setup_exp < old else 'neutral')
                out_ev = {'setup_exp_usd_then': old,
                          'setup_exp_usd_now': setup_exp}
        elif kind == 'live-weights':
            old = ev.get('fifty_rule_exp_usd')
            if cur_fifty is not None and old is not None:
                verdict = ('helped' if cur_fifty > old else
                           'hurt' if cur_fifty < old else 'neutral')
                out_ev = {'fifty_rule_exp_usd_then': old,
                          'fifty_rule_exp_usd_now': cur_fifty}
        if verdict is not None:
            e['outcome'] = verdict
            e['outcome_t'] = t
            e['outcome_evidence'] = out_ev
    return log


def next_t_preview(results, proposal):
    """What the NEXT grading run (t+1) is expected to change, from the
    current grades: the features nearest 50% proof (a small grade move
    flips their signed weight's sign) and the cells nearest the losing
    line (win% just above 35% or expectancy just below $0). A forecast
    from current grades, not a promise — fresh grades decide."""
    fr = results.get('fifty_rule') or {}
    feats = sorted(fr.get('feature_rows') or [],
                   key=lambda r: abs((r.get('train_top_win_pct') or 50) - 50))
    near_feats = [{
        'feature': r['name'],
        'train_top_win_pct': r['train_top_win_pct'],
        'signed_weight': r['signed_weight'],
        'distance_to_50_pp': round(abs(r['train_top_win_pct'] - 50), 1),
        'why': 'closest to 50% proof — a small grade move flips its sign',
    } for r in feats[:3]]
    names = []
    for (kind, direction, band, regime) in sorted(results['cells']):
        names.append(f'{kind} {direction} band={band} regime={regime}')
    cyc = results['cycle_deduped_scoreboard']
    names.append('cycle — DEDUPED (distinct outcomes)')
    for row in cyc['rows']:
        names.append(f"cycle — DEDUPED {row['horizon']} {row['direction']}")
    for band in sorted(results['cmi']['by_band']):
        names.append(f'CMI band {band}')
    losing_names = {t['name'] for t in proposal['track_changes']}
    near_cells = []
    for name in names:
        if name in losing_names:
            continue
        ev = _track_evidence(results, proposal, name) or {}
        win, exp, n = ev.get('win_pct'), ev.get('expectancy_usd'), ev.get('n')
        if win is None or n is None or n < MIN_N:
            continue
        if 35 < win <= 40 or (exp is not None and -5 <= exp < 0):
            near_cells.append({
                'name': name, 'win_pct': win, 'n': n,
                'expectancy_usd': exp,
                'distance_to_95': distance_to_95(win),
                'why': ('win% just above the 35% losing line'
                        if win <= 40 else
                        'expectancy just below $0/1k')})
    near_cells.sort(key=lambda c: (c['win_pct'],
                                   c['expectancy_usd'] or 0))
    return {'features_near_50': near_feats,
            'cells_near_losing_line': near_cells[:3]}


def write_live_weights(repo, state):
    """Write the LIVE weight state the engine reads each run
    (nexintel.load_corrected_weights). NEVER places an order — this
    file only changes the SCORING weights the engine uses to CALL a
    buy; Nic executes every trade himself."""
    path = os.path.join(repo, 'data', LIVE_WEIGHTS_FILENAME)
    payload = {'t': state['t'], 'timestamp': state['timestamp'],
               'weights': state['weights'],
               'source': state['weights_source'],
               'proof': state['proof']}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(payload, f, indent=2)
    return path


def fifty_rule_block(results):
    """Nic's 50% rule applied to today's grades (variable_tests.run
    computes it): signed weights for the next shadow run. Above 50%
    is a positive signal, below 50% a negative one, magnitude scaling
    with distance from 50%. Absent (older harness output) -> None,
    and the report says so instead of inventing weights."""
    fr = results.get('fifty_rule')
    if not fr:
        return None
    return {
        'rule': fr['rule'],
        'scale': fr['scale'],
        'max_distance_from_50': fr['max_distance_from_50'],
        'grade_vs_current': {
            'fifty_rule_exp_usd': fr['grade']['exp_usd'],
            'current_exp_usd': results['shadow']['base_grade'].get('exp_usd'),
            'n': fr['grade']['n'],
        },
        'features': [{'feature': row['name'],
                      'train_top_win_pct': row['train_top_win_pct'],
                      'signed_weight': row['signed_weight'],
                      'rule_set_delta_exp_usd': row['rule_set_delta_exp_usd']}
                     for row in fr['feature_rows']],
        'tracks': [{'name': t['name'], 'win_pct': t['win_pct'],
                    'n': t['n'], 'signed_weight': t['signed_weight'],
                    'original_exp_usd': t['exp_usd'],
                    'mirror_exp_usd': (t['inverted']['expectancy_usd']
                                       if t['inverted'] else None),
                    'rule_verdict': t['rule_verdict']}
                   for t in fr['tracks']],
    }


# ------------------------------------------------------------------ report

def render_report(p):
    L = []
    t = p.get('f_t')
    L.append('# Shadow actuator — corrections APPLIED (F(t))')
    L.append('')
    L.append(f"Generated {p['generated_utc']} (UTC). APPLIED at t={t} — "
             'F(t+1) = correct(F(t), grades): this run APPLIED the '
             'corrections below to the shadow state and, per the settled '
             'scope, to the live weights (repo/data/corrected_weights.json). '
             'No per-correction approval — the grades judge each change, '
             'the correction log shows right and wrong, and the next '
             'iteration corrects again. The system NEVER places an order, '
             'spends, or touches an account: a buy is the CALL/ALERT, Nic '
             'executes the trade himself.')
    L.append('')
    L.append('## Loop form and guards')
    L.append('')
    L.append('- F(t+1) = correct(F(t), graded evidence), one iteration per '
             'grading run, forever. Direction: 100% accuracy (never '
             'arrives, keeps correcting).')
    L.append('- >95% graded proof = a VERY ACCURATE read. 95% is a READ, '
             'never a gate — no candidate is blocked for being under 95%.')
    L.append('- The 50% rule and the n≥20 minimum govern every weight change.')
    L.append('- Every correction is logged with its graded outcome '
             '(correction-log.jsonl); a wrong correction is reversed by '
             'the next iteration, never hidden.')
    L.append('')
    if p.get('applied_corrections') is not None:
        applied = p['applied_corrections']
        L.append(f"## Applied at t={t} ({len(applied)} correction(s))")
        L.append('')
        if not applied:
            L.append('No weight or multiplier moved this run — the fresh '
                     'grades recomputed the same F(t) as the last run.')
        for e in applied:
            ev = e.get('evidence') or {}
            L.append(f"- **{e['kind']} {e['target']}** ({e['scope']}): "
                     f"{e['old_value']} → {e['new_value']}"
                     + (f" — win {ev.get('win_pct')}%, n={ev.get('n')}, "
                        f"exp {ev.get('expectancy_usd')} $/1k, "
                        f"{ev.get('distance_to_95')} pts short of 95%"
                        if ev.get('win_pct') is not None else '')
                     + (f" — {ev.get('note', '')}"
                        if ev.get('note') else ''))
        L.append('')
    prior = p.get('prior_outcomes') or []
    if prior:
        L.append('## Prior corrections — graded outcomes (helped / hurt visible)')
        L.append('')
        for e in prior:
            L.append(f"- t={e['t']} **{e['kind']} {e['target']}** "
                     f"({e['scope']}): **{e['outcome']}** at t={e['outcome_t']} "
                     f"— {e.get('outcome_evidence') or ''}")
        L.append('')
    gate_adj = p.get('gate_adjustments') or []
    if gate_adj:
        L.append('## Gate-log one-step adjust (this run)')
        L.append('')
        for a in gate_adj:
            L.append(f"- term {a['term']}: {a['direction']} "
                     f"(n={a['n']}, {a['wins']}W/{a['losses']}L) — "
                     f"{a['reason']}")
        L.append('')
    preview = p.get('next_t_preview')
    if preview:
        L.append('## Next t — what the next grading run is expected to change')
        L.append('')
        L.append('A forecast from the current grades, not a promise — '
                 'fresh grades decide.')
        for f in preview.get('features_near_50') or []:
            L.append(f"- **{f['feature']}**: train proof "
                     f"{f['train_top_win_pct']}% (signed "
                     f"{f['signed_weight']}) — {f['why']}.")
        for c in preview.get('cells_near_losing_line') or []:
            L.append(f"- **{c['name']}**: win {c['win_pct']}% (n={c['n']}), "
                     f"exp {c['expectancy_usd']} $/1k, "
                     f"{c['distance_to_95']} pts short of 95% — {c['why']}.")
        L.append('')
    L.append('## Losing line')
    L.append('')
    L.append(f"- Win rate ≤ **{p['losing_line']['win_pct_at_or_below']:.0f}%** "
             'OR negative expectancy (dollars per $1,000), at '
             f"n ≥ **{p['losing_line']['min_n']}** graded.")
    L.append('- 95% (Wilson) is DISPLAYED ONLY, never a gate — every '
             'change below carries its Wilson lower bound and its distance '
             'to 95% as readings.')
    L.append('- Weights proportional to graded performance (see the '
             'module docstring for the factor math). Each run recomputes '
             'the weights fresh from the latest grades — a wrong '
             "correction is overwritten by the next run's evidence.")
    L.append('')
    L.append(f"Checked: {p['checked']['features']} core features, "
             f"{p['checked']['track_cells']} track cells/rows. "
             f"Changes proposed: {len(p['feature_changes'])} feature, "
             f"{len(p['track_changes'])} track. "
             f"{len(p['feature_immaterial'])} feature line-crossings were "
             'immaterial (< 0.005 weight step) and '
             f"{len(p['skipped_thin'])} cells were too thin (n < 20) to judge.")
    L.append('')
    L.append('## Track changes (shadow multiplier 1.0 → factor)')
    L.append('')
    if not p['track_changes']:
        L.append('None — no track cell crosses a losing line at n ≥ 20.')
    L.append('')
    for t in p['track_changes']:
        L.append(f"### {t['name']}")
        L.append('')
        L.append(f"- Reads: {t['reads']}.")
        L.append(f"- Graded: n={t['n']} ({t['wins']}W / {t['losses']}L), "
                 f"win **{t['win_pct']}%**, expectancy "
                 f"**{t['expectancy_usd']:+.2f} $/1k**, Wilson lower 95% "
                 f"{t['wilson_lower_95_pct']}% (display only), "
                 f"{distance_to_95(t['win_pct'])} pts short of 95% "
                 f"(read only).")
        L.append(f"- Crossed: {'; '.join(t['reasons'])}.")
        L.append(f"- Change: shadow multiplier "
                 f"{t['old_shadow_multiplier']} → "
                 f"**{t['new_shadow_multiplier']}** (factor {t['factor']}).")
        L.append(f"- Why: {t['mechanism']}")
        L.append('')
    L.append('## Feature changes (shadow weight × factor)')
    L.append('')
    if not p['feature_changes']:
        L.append('None material — see the immaterial crossings below.')
    L.append('')
    for f in p['feature_changes']:
        L.append(f"### {f['feature']}")
        L.append('')
        L.append(f"- Graded: ablation delta "
                 f"{f['ablation_delta_exp_usd']:+.2f} $/1k, standalone "
                 f"top-tercile win {f['standalone_top_win_pct']}%, "
                 f"n={f['n_test_picks']} test picks ({f['verdict']}).")
        L.append(f"- Crossed: {'; '.join(f['reasons'])}.")
        L.append(f"- Change: shadow weight {f['old_shadow_weight']} → "
                 f"**{f['new_shadow_weight']}** (factor {f['factor']}).")
        L.append('')
    L.append('## Immaterial feature crossings (line crossed, step < 0.005)')
    L.append('')
    L.append('Single-feature ablation deltas are tiny (worst losing delta '
             'is under $1/1k), so the proportional steps are tiny too. '
             'These crossed a losing line but the weight barely moves — '
             'listed so the crossing is visible, not hidden.')
    L.append('')
    if not p['feature_immaterial']:
        L.append('None.')
    for f in p['feature_immaterial']:
        L.append(f"- **{f['feature']}**: shadow weight "
                 f"{f['old_shadow_weight']} → {f['new_shadow_weight']} "
                 f"(factor {f['factor']}; ablation delta "
                 f"{f['ablation_delta_exp_usd']:+.2f} $/1k, standalone "
                 f"top win {f['standalone_top_win_pct']}%).")
    L.append('')
    L.append('## Too thin to judge (n < 20)')
    L.append('')
    if not p['skipped_thin']:
        L.append('None.')
    for s in p['skipped_thin']:
        L.append(f"- {s['name']}: {s['reason']}.")
    L.append('')
    L.append('## 50%-rule shadow set (Nic\'s rule, today\'s grades)')
    L.append('')
    fr = p.get('fifty_rule_shadow_set')
    if not fr:
        L.append('Not available — this run predates the 50% rule '
                 '(re-run variable_tests.py first).')
    else:
        L.append(f"Rule: {fr['rule']} Scale {fr['scale']} (largest "
                 f"distance from 50%: {fr['max_distance_from_50']}). "
                 f"Out-of-sample grade: {fr['grade_vs_current']['fifty_rule_exp_usd']:+.2f} "
                 f"$/1k vs current "
                 f"{vt.fmt(fr['grade_vs_current']['current_exp_usd'], 2)} $/1k "
                 f"(n={fr['grade_vs_current']['n']}). These signed weights "
                 f"are the proposed shadow set for the next run — above 50% "
                 f"positive, below 50% negative, 50% itself zero.")
        L.append('')
        L.append('### Features (train proof → signed weight)')
        L.append('')
        for f in fr['features']:
            L.append(f"- **{f['feature']}**: train top-win "
                     f"{f['train_top_win_pct']}% → signed weight "
                     f"{f['signed_weight']} (rule-set Δexp "
                     f"{f['rule_set_delta_exp_usd']:+.2f} $/1k).")
        L.append('')
        L.append('### Tracks (win rate → signed weight → mirror grade)')
        L.append('')
        for t in fr['tracks']:
            mirror_txt = ('n/a' if t['mirror_exp_usd'] is None
                          else f"{t['mirror_exp_usd']:+.2f} $/1k")
            L.append(f"- **{t['name']}**: win {t['win_pct']}% (n={t['n']}) "
                     f"→ signed weight {t['signed_weight']}; original "
                     f"{vt.fmt(t['original_exp_usd'], 2)} $/1k, mirror "
                     f"{mirror_txt} "
                     f"— {t['rule_verdict']}.")
    L.append('')
    return '\n'.join(L) + '\n'


def default_out_dir(repo):
    return os.path.join(os.path.dirname(os.path.abspath(repo)), 'hidden_files')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo', default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help='repo root (default: derived from this file)')
    ap.add_argument('--out', default=None,
                    help='state/log output dir (default: <goal>/hidden_files)')
    ap.add_argument('--no-live', action='store_true',
                    help='skip writing the live weight file (tests only — '
                         'the daily run always writes it)')
    args = ap.parse_args()
    results, _md = vt.run(args.repo)  # read-only on the ledgers
    proposal = propose(results)
    out_dir = args.out or default_out_dir(args.repo)
    os.makedirs(out_dir, exist_ok=True)
    paths = state_file_paths(out_dir)
    prev = load_prev_state(out_dir)
    t = (prev['t'] + 1) if prev else 1
    # Gate-log one-step adjust (n >= 20 groups only; nothing graded yet
    # on the first run, so no adjustment — correct, not missing).
    gate_records = gl.read(paths['gate_log'])
    adjustments = gl.aggregate_adjustments(gate_records)
    prev_term = (prev or {}).get('term_multipliers') or {'c': 1.0, 'd': 1.0}
    prev_caution = (prev or {}).get('caution_buy_pct', 50.0)
    term_mult, caution, applied_gate = gl.apply_adjustments(
        prev_term, prev_caution, adjustments)
    # F(t): weights from the fresh grades, folded with the gate adjust.
    state = build_state(t, results, proposal, term_mult, caution, prev)
    entries = diff_state(prev, state, t, results, proposal, applied_gate)
    # Grade the prior corrections from the FRESH grades, then log the new ones.
    log = load_correction_log(paths['correction_log'])
    log = grade_prior_corrections(log, results, t)
    log.extend(entries)
    save_correction_log(paths['correction_log'], log)
    # Live weight state (settled scope: autonomous correction includes
    # live). NEVER places an order — scoring weights only; Nic executes.
    live_path = None
    if not args.no_live:
        live_path = write_live_weights(args.repo, state)
    # F(t) snapshot, versioned by t — any t can be read back.
    with open(paths['f_state'], 'a') as f:
        f.write(json.dumps(state) + '\n')
    with open(paths['shadow_state'], 'w') as f:
        json.dump(state, f, indent=2)
    # Dated report + machine-readable snapshot.
    proposal['f_t'] = t
    proposal['applied_corrections'] = entries
    proposal['prior_outcomes'] = [e for e in log
                                  if e.get('outcome') is not None]
    proposal['gate_adjustments'] = applied_gate
    proposal['next_t_preview'] = next_t_preview(results, proposal)
    proposal['live_weights_path'] = live_path
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    md_path = os.path.join(out_dir, f'shadow-actuator-{today}.md')
    json_path = os.path.join(out_dir, f'shadow-weights-{today}.json')
    with open(md_path, 'w') as f:
        f.write(render_report(proposal))
    with open(json_path, 'w') as f:
        json.dump(proposal, f, indent=2)
    helped = sum(1 for e in log if e.get('outcome') == 'helped')
    hurt = sum(1 for e in log if e.get('outcome') == 'hurt')
    print(f'wrote {md_path}')
    print(f'wrote {json_path}')
    print(f'F(t={t}) applied: {len(entries)} correction(s) '
          f'({len(proposal["feature_changes"])} feature, '
          f'{len(proposal["track_changes"])} track, '
          f'{len(proposal["feature_immaterial"])} immaterial, '
          f'{len(proposal["skipped_thin"])} too thin)')
    print(f'correction log: {len(log)} entries, {helped} helped / '
          f'{hurt} hurt so far')
    if live_path:
        print(f'live weights written: {live_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
