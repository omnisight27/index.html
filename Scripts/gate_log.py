#!/usr/bin/env python3
"""Gate log — OmniSight, Nic Nov 11.

F(t) is the function at time t: its weights, its proof, its unknown AS
OF that grading run. F(t+1) = correct(F(t), grades) — each grading run
is a new t. The loop never arrives at 100% accuracy; that is the
direction, not the destination (Nic Nov 11: "Keep correcting yes to
100% accuracy which could seem impossible").

Every candidate F evaluates gets ONE record: a TRUE/FALSE verdict
(passed the gate / blocked), the WHY (which term carried it through
or blocked it — a, b, c, d, (f-e), or the unknown), and the graded
outcome once known (win/loss/expired). The verdict is binary — true
or false, no maybe, no partial. The why is separate from the verdict.

An F-buy pushed through the gate in caution is verdict TRUE — the why
is the term that carried it (e.g. d, order flow), not the push-through
itself.

Record shape (fixed fields, one JSON object per line, append-only):
  t: int — grading-run index the verdict belongs to (1 = first run, Nov 11 2026)
  run: str — ISO timestamp of the source run (decide list / engine run)
  timestamp: str — ISO timestamp the record was written
  candidate_id: str — asset + kind + run (unique per candidate per run)
  asset, kind: str
  verdict: bool — true = passed the gate (F-buy called), false = blocked
  why: str — the single decisive term: 'a' | 'b' | 'c' | 'd' | 'fe' | 'unknown'
  terms: {a, b, c, d, fe, unknown} — the six term values (null when unknown)
  outcome: null | 'win' | 'loss' | 'expired' — null until graded, never assumed
  outcome_time: str | null
  correction_applied: str | null — what the loop changed because of this record

THE WHY ADJUSTS F (one step, no human in the middle):
  passed + won            -> strengthen what carried it
  passed + lost           -> weaken what carried it
  blocked + would've won  -> loosen what blocked it
  blocked + would've lost -> confirm the block was right (no change, record it)
"Would have won/lost" for blocked candidates comes from the shadow
grading of blocked candidates (the ledger keeps grading excluded
cells). If a blocked candidate has no graded outcome, outcome stays
null — unknown, never assumed.

Guards (same as the correction loop): the 50% rule and the n>=20
minimum govern every weight change; corrections apply automatically
(in shadow and, per the settled scope, live) and are logged; the
system NEVER places an order — a buy is the CALL/ALERT, Nic executes
the trade himself. 95% is a READ, never a gate: a cell under 95% is
never blocked for being under 95%.

The adjust step lives here as pure functions; the actuator calls
aggregate_adjustments() once per grading run, right after its own
correction step, and folds the result into the F(t+1) state.
"""

import json
import os
from datetime import datetime, timezone

TERMS = ('a', 'b', 'c', 'd', 'fe', 'unknown')
WHY_TERMS = ('a', 'b', 'c', 'd', 'fe', 'unknown')
OUTCOMES = (None, 'win', 'loss', 'expired')

TERM_STEP = 0.05      # one-step adjust: 5% relative per (why, outcome) group
TERM_MIN = 0.5        # term multiplier floor
TERM_MAX = 1.5        # term multiplier ceiling
MIN_N = 20            # a (why, outcome) group needs n >= 20 graded records to move a weight


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def new_record(t, run, asset, kind, verdict, why, terms,
               outcome=None, outcome_time=None, correction_applied=None,
               timestamp=None, candidate_id=None):
    """Build one gate-log record. `why` must be one of the six terms;
    `terms` is a dict with the six term values (null when unknown).
    `candidate_id` overrides the default asset_kind_date id (used when
    one asset is two candidates in one run, e.g. QFI as swing long and
    as intraday mover)."""
    if why not in WHY_TERMS:
        raise ValueError(f"why must be one of {WHY_TERMS}, got {why!r}")
    if not isinstance(verdict, bool):
        raise ValueError(f"verdict must be true/false, got {verdict!r}")
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
    full_terms = {k: (terms or {}).get(k) for k in TERMS}
    return {
        't': int(t),
        'run': run,
        'timestamp': timestamp or utcnow(),
        'candidate_id': candidate_id or f"{asset}_{kind}_{(run or '')[:10]}",
        'asset': asset,
        'kind': kind,
        'verdict': bool(verdict),
        'why': why,
        'terms': full_terms,
        'outcome': outcome,
        'outcome_time': outcome_time,
        'correction_applied': correction_applied,
    }


def record(path, rec):
    """Append one record to the JSONL log (creates parent dirs)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'a') as f:
        f.write(json.dumps(rec) + '\n')
    return rec


def read(path):
    """Read all records (tolerates a missing file -> []). Skips bad lines."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def grade_outcomes(records):
    """Count graded outcomes by (verdict, why, outcome). Returns a dict
    keyed (verdict, why, outcome) -> {'n', 'wins', 'losses'}."""
    groups = {}
    for r in records:
        if r.get('outcome') is None:
            continue
        key = (bool(r.get('verdict')), r.get('why'), r.get('outcome'))
        g = groups.setdefault(key, {'n': 0, 'wins': 0, 'losses': 0})
        g['n'] += 1
        if r['outcome'] == 'win':
            g['wins'] += 1
        elif r['outcome'] in ('loss', 'expired'):
            g['losses'] += 1
    return groups


def aggregate_adjustments(records, min_n=MIN_N, step=TERM_STEP):
    """One-step adjust: read the log (true/false + why + graded
    outcome) and return the adjustments F(t+1) must apply.

    Groups are (verdict, why) over GRADED records only; a group needs
    n >= min_n to move anything — thinner groups are reported as
    too-thin, never adjusted on. Returns a list of:
      {'term', 'direction', 'n', 'wins', 'losses',
       'multiplier_delta', 'reason', 'applies_to'}
    where direction is 'strengthen' | 'weaken' | 'confirm' and
    applies_to names the fold target ('term-multiplier' for c/d,
    'caution-threshold' for a, 'record-only' for b/fe/unknown —
    b IS the fifty-rule recompute, fe/unknown have no weight to move).
    """
    by_group = {}
    for r in records:
        if r.get('outcome') is None:
            continue
        key = (bool(r.get('verdict')), r.get('why'))
        g = by_group.setdefault(key, {'n': 0, 'wins': 0, 'losses': 0})
        g['n'] += 1
        if r['outcome'] == 'win':
            g['wins'] += 1
        elif r['outcome'] in ('loss', 'expired'):
            g['losses'] += 1
    adjustments = []
    for (verdict, why), g in sorted(by_group.items()):
        if why not in WHY_TERMS:
            continue
        if g['n'] < min_n:
            adjustments.append({
                'term': why, 'direction': 'none', 'n': g['n'],
                'wins': g['wins'], 'losses': g['losses'],
                'multiplier_delta': 0.0,
                'reason': (f"{'passed' if verdict else 'blocked'} + "
                           f"{g['wins']}W/{g['losses']}L on n={g['n']} < {min_n} — "
                           'too thin to adjust'),
                'applies_to': 'record-only',
            })
            continue
        if verdict and g['wins'] > g['losses']:
            direction, delta = 'strengthen', step
            reason = (f"passed + won {g['wins']}W/{g['losses']}L (n={g['n']}) — "
                      f"strengthen what carried it ({why})")
        elif verdict:
            direction, delta = 'weaken', -step
            reason = (f"passed + lost {g['wins']}W/{g['losses']}L (n={g['n']}) — "
                      f"weaken what carried it ({why})")
        elif g['wins'] > g['losses']:
            direction, delta = 'weaken', -step
            reason = (f"blocked + would have won {g['wins']}W/{g['losses']}L "
                      f"(n={g['n']}) — loosen what blocked it ({why})")
        else:
            direction, delta = 'confirm', 0.0
            reason = (f"blocked + would have lost {g['wins']}W/{g['losses']}L "
                      f"(n={g['n']}) — block confirmed right, no change")
        applies_to = ('caution-threshold' if why == 'a' else
                      'term-multiplier' if why in ('c', 'd') else
                      'record-only')
        adjustments.append({
            'term': why, 'direction': direction, 'n': g['n'],
            'wins': g['wins'], 'losses': g['losses'],
            'multiplier_delta': round(delta, 4),
            'reason': reason,
            'applies_to': applies_to,
        })
    return adjustments


def apply_adjustments(term_multipliers, caution_buy_pct, adjustments,
                      step=TERM_STEP):
    """Fold adjustments into the F(t) state. Returns
    (new_multipliers, new_caution_buy_pct, applied_list) where each
    applied entry records what changed and why. Term multipliers are
    clamped to [TERM_MIN, TERM_MAX]; the shadow caution line moves
    2.5 points per step (step x 50) and is clamped to [35, 65].
    Record-only terms (b, fe, unknown) and confirmations change
    nothing but are listed so the log shows they were considered."""
    mult = dict(term_multipliers or {})
    caution = float(caution_buy_pct)
    applied = []
    for adj in adjustments:
        term, direction = adj['term'], adj['direction']
        if direction == 'none' or direction == 'confirm':
            applied.append({**adj, 'applied': False,
                            'note': 'no weight moved — ' + adj['reason']})
            continue
        if adj['applies_to'] == 'term-multiplier':
            old = float(mult.get(term, 1.0))
            new = min(TERM_MAX, max(TERM_MIN,
                                    old * (1.0 + adj['multiplier_delta'])))
            mult[term] = round(new, 4)
            applied.append({**adj, 'applied': True,
                            'old_value': old, 'new_value': round(new, 4),
                            'note': f"term {term} multiplier {old} -> {round(new, 4)}"})
        elif adj['applies_to'] == 'caution-threshold':
            old = caution
            # weaken the blocker = lower the caution line; strengthen = raise it
            shift = -step * 50.0 if direction == 'weaken' else step * 50.0
            caution = min(65.0, max(35.0, caution + shift))
            applied.append({**adj, 'applied': True,
                            'old_value': old, 'new_value': round(caution, 1),
                            'note': (f"shadow caution line {old} -> "
                                     f"{round(caution, 1)}% buy")})
        else:
            applied.append({**adj, 'applied': False,
                            'note': ('record-only — ' + adj['reason'] +
                                     (' (b is the fifty-rule recompute; '
                                      'fe/unknown have no weight to move)'))})
    return mult, round(caution, 1), applied
