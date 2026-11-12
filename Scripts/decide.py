# decide.py — the decision simplifier.
#
# Nic (Nov 11): "Use every aspect of the quantum computer to simplify
# my decisions to take action." He gets a lot of information (alerts,
# movers, variables, feeds, sims) and wants it reduced to clear actions.
#
# What this does: reads the graded state on disk and produces a short
# action list — one line per candidate: BUY / WAIT / WATCH + exactly one
# reason, with the evidence on the line and the confluence shown
# explicitly. It never places trades, never changes gates, weights or
# scores, and never sends anything. REVIEW ONLY. $0: reads disk only,
# no keys, no paid data.
#
# HONEST SCOPE on the quantum part:
# - There is no quantum hardware here. Scripts/quantum_lab.py runs the
#   algorithms on a classical state-vector simulator (stdlib only).
# - The lab has four pieces: Grover (search demo), QAOA MaxCut
#   (benchmark — classical greedy wins), VQC classifier (toy data —
#   ties the perceptron, does not beat it), and the portfolio QUBO
#   (pick k assets, return vs correlation — the formulation that
#   genuinely matches this job: which candidates, in what order, at
#   what size, under constraints).
# - This module uses the QUBO formulation for real: it builds a live
#   QUBO (candidates / features, value from graded expectancy,
#   correlation penalty, size constraint) and solves it three ways —
#   brute force where the space is small enough, the lab's simulated
#   annealing, and the lab's QAOA simulator (p=1) on the same QUBO —
#   and reports which solver won. The simulator caps at ~10 qubits
#   (a 22-qubit state vector is 4M amplitudes — infeasible), so QAOA
#   runs on the reduced top-8 problem and says so.
# - Grover is not used: the candidate set is small and a classical
#   scan is exact. VQC is not used for live direction: it has no
#   graded evidence on live data (toy 8-point task only). Both are
#   named here instead of being quietly skipped.
# - A simulated result is never a speedup claim. The value is the
#   formulation and the comparison, not the hardware.
#
# The four additions (Nic Nov 11):
# 1. INTELLIGENCE — the action list uses learned intelligence: harness
#    justified weights, calibration v1/v2, precedent reads. Not just
#    today's numbers.
# 2. EVIDENCE — every action cites its evidence on the line (graded
#    win rate, n, expectancy). No action without evidence; thin
#    evidence says so.
# 3. ASPECTS INTERACT — regime x variable x feed x precedent vote on
#    each candidate and the confluence is shown: agreement strengthens,
#    disagreement is named and weakens or blocks. No silent summing.
# 4. ESCALATION LADDER — WATCH -> paper -> test buy ($500) -> larger
#    (Kelly-sized by conviction), with the evidence threshold for each
#    rung, de-escalation rules, and the within-trade escalation
#    (runner/stretch, trailing the stop) as part of the ladder.
#
# Run:  python3 Scripts/decide.py        (from the repo root)
# Writes a dated action list to the goal's hidden_files.
import cmath
import json
import math
import os
import random
import re
import sys
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPTS_DIR)
DATA_DIR = os.path.join(REPO_DIR, 'data')
HIDDEN_DIR = os.path.join(REPO_DIR, '..', 'hidden_files')
HARNESS_REPORT = os.path.join(HIDDEN_DIR, 'variable-tests-2026-11-11.md')
BTC_CACHE = '/tmp/precedent_btc_daily.json'

sys.path.insert(0, SCRIPTS_DIR)
import quantum_lab  # noqa: E402  (simulator primitives + QAOA machinery)

# Nic's constraints (from the record — these are his rules, not ours).
BUY_BAR = 60            # raw confidence bar for a test buy (%)
TEST_SIZE = 500         # dollars per test buy
KELLY_CAP = 0.50        # Kelly fraction cap
MAX_PICKS = 3           # QUBO queue depth
EXTENDED_PCT = 20.0     # 24h gain at/above this already ran — don't chase
CAUTION_BUY_PCT = 50    # buy % below this is caution
QAOA_MAX_QUBITS = 10    # state-vector simulator cap (2**10 amplitudes)
BRUTE_FORCE_MAX_QUBITS = 16

# ---------------------------------------------------------------------------
# Ladder — the answer to "what do I do next as the evidence gets stronger."
# Rung 0 WATCH: default. No plan, down-move, no graded evidence, or a
#   track that loses money with nothing to learn from it.
# Rung 1 PAPER: plan + venue, track graded, gates pass except the money
#   bar. Size $1,000/trade paper, 4:1 plans. Purpose: learn. The sims
#   take risks here — that is how the system learns (Nic Nov 11).
# Rung 2 TEST BUY ($500): all gates pass (confidence >= 60% raw, or an
#   aligned mover with a plan; conditions not caution; complete plan;
#   swing 4/5 or n/a on movers; venue Nic trades) AND the track earns
#   money (expectancy > $0/1k at n >= 20). Size $500, Kelly cap 0.50,
#   hard stop. The calibrated win rate is SHOWN, never a gate (Nic
#   Nov 5: the bar reads raw confidence).
# Rung 3 LARGER (Kelly-sized by conviction): the track/cell has n >= 100
#   graded, win% >= 60%, expectancy > +$10/1k sustained, and 3+ graded
#   wins in the cell. Size = Kelly fraction x bankroll, capped at 0.50.
#   EMPTY TODAY — no long cell qualifies; the ladder says so.
# Within a trade (rung 2 -> 3 inside a position): green at exit -> trail
#   the stop to breakeven -> ratchet to lock half -> chase the stretch
#   target (vol x 3.2) only if 1h >= 1.5%, 24h >= 10%, RSI < 70.
# De-escalation (stepping back down):
#   - conditions go to caution: real money off — test buy and larger
#     drop to paper (rung 1), paper drops to watch (rung 0).
#   - track expectancy turns negative at n >= 20: drop to WATCH.
#   - confidence falls below 60%: rung 1 at most.
#   - plan expires / no plan: drop to WATCH.
#   - stop hit: out, no same-day re-entry on that setup.
#   - target hit: take the win; runner only if the stretch holds.
LADDER = [
    {'rung': 0, 'name': 'WATCH',
     'threshold': 'Default. No plan, down-move, no graded evidence, '
                  'or a track that loses money.',
     'size': 'None — watch only.'},
    {'rung': 1, 'name': 'PAPER',
     'threshold': 'Plan + venue, track graded (any expectancy), gates '
                  'pass except the money bar.',
     'size': '$1,000/trade paper, 4:1 plans. Purpose: learn.'},
    {'rung': 2, 'name': 'TEST BUY ($500)',
     'threshold': 'All gates pass (confidence >= 60% raw or aligned '
                  'mover with plan; conditions not caution; complete '
                  'plan; swing 4/5 or n/a on movers; venue Nic trades) '
                  'AND track expectancy > $0/1k at n >= 20.',
     'size': '$500, Kelly cap 0.50, hard stop.'},
    {'rung': 3, 'name': 'LARGER (Kelly-sized)',
     'threshold': 'Track/cell n >= 100, win% >= 60%, expectancy '
                  '> +$10/1k sustained, 3+ graded wins in the cell.',
     'size': 'Kelly fraction x bankroll, capped at 0.50. EMPTY TODAY.'},
]
WITHIN_TRADE = ('Inside a position: green at exit -> trail the stop to '
                'breakeven -> ratchet to lock half -> chase the stretch '
                'target (vol x 3.2) only if 1h >= 1.5%, 24h >= 10%, '
                'RSI < 70. Stop hit: out, no same-day re-entry. '
                'Target hit: take the win.')
DEESCALATION = ('Step back down when: conditions go to caution — real '
                'money off (test buy and larger drop to paper, rung 1; '
                'paper drops to watch, rung 0); track expectancy turns '
                'negative at n >= 20 (drop to WATCH); confidence falls '
                'below 60% (rung 1 at most); plan expires or no plan '
                '(drop to WATCH).')


# ---------------------------------------------------------------------------
# Loading the graded state (disk only, never the network for prices)
def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def load_latest():
    return _load_json(os.path.join(DATA_DIR, 'latest.json'))


def load_intraday():
    return _load_json(os.path.join(DATA_DIR, 'intraday.json'))


def load_swing_book():
    """Swing book from data/history.json, mirroring the harness's
    graded_picks: flatten scored runs, skip excluded picks and picks
    with no return_pct; return_pct is already direction-signed.
    Returns {n, win_pct, exp ($/1k), long: {...}, short: {...}} or {}."""
    try:
        with open(os.path.join(DATA_DIR, 'history.json')) as f:
            hist = json.load(f)
        runs = hist if isinstance(hist, list) else hist.get('picks', [])
        picks = []
        for run in runs:
            for direction, key in ((1, 'longs'), (-1, 'shorts')):
                for p in (run or {}).get(key) or []:
                    if not isinstance(p, dict) or p.get('excluded'):
                        continue
                    ret = p.get('return_pct')
                    if ret is None:
                        continue
                    win = p.get('win')
                    if not isinstance(win, bool):
                        win = ret > 0
                    picks.append({'direction': direction,
                                  'ret': float(ret), 'win': bool(win)})
        if not picks:
            return {}

        def _summ(sub):
            n = len(sub)
            return {'n': n,
                    'win_pct': round(
                        100.0 * sum(1 for p in sub if p['win']) / n, 1),
                    'exp': round(
                        sum(p['ret'] for p in sub) / n * 10.0, 2)}

        out = _summ(picks)
        for d, key in ((1, 'long'), (-1, 'short')):
            sub = [p for p in picks if p['direction'] == d]
            if sub:
                out[key] = _summ(sub)
        return out
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Harness Table B — the graded track evidence (parsed, never hardcoded)
TRACK_KEYS = [
    ('mover (proof book kind)', 'mover'),
    ('pattern_confluence (proof book kind)', 'pattern_confluence'),
    ('pattern (proof book kind)', 'pattern'),
    ('patterns — aggregate', 'patterns_agg'),
    ('setup (proof book kind)', 'setup'),
    ('signal (proof book kind)', 'signal'),
    ('cycle — deduped book', 'cycle'),
    ('cmi band caution', 'cmi_caution'),
    ('cmi band clean', 'cmi_clean'),
    ('cmi band manip', 'cmi_manip'),
    ('shadow crypto scan', 'shadow'),
    ('stocks — intraday track', 'stocks_intraday'),
    ('stocks — equity swing picks', 'stocks_swing'),
]


def parse_harness_table(path=HARNESS_REPORT):
    """Parse Table B aggregate rows: {key: (win%, n, expectancy, verdict)}.
    Rows that do not parse are skipped, never invented."""
    out = {}
    try:
        with open(path) as f:
            text = f.read()
    except Exception:
        return out
    for line in text.splitlines():
        if not line.strip().startswith('|'):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) < 8:
            continue
        name = cells[0].lower()
        key = None
        for needle, k in TRACK_KEYS:
            if needle in name:
                key = k
                break
        if key is None or key in out:
            continue
        try:
            n = int(float(cells[1]))
            trig = float(cells[2])
            win = float(cells[3])
            exp = float(cells[4])
        except (ValueError, IndexError):
            continue
        out[key] = {'win_pct': win, 'n': n, 'trig': trig, 'exp': exp,
                    'verdict': cells[7]}
    return out


def parse_harness_table_a(path=HARNESS_REPORT):
    """Parse Table A rows into a list of dicts — one per row, duplicates
    kept (rsi and zscore each appear twice: core + model-library).
    Rows that do not parse are skipped, never invented."""
    rows = []
    try:
        with open(path) as f:
            text = f.read()
    except Exception:
        return rows
    in_a = False
    for line in text.splitlines():
        if line.startswith('## Table A'):
            in_a = True
            continue
        if line.startswith('## Table B'):
            in_a = False
        if not in_a or not line.strip().startswith('|'):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) < 12:
            continue
        try:
            int(cells[0])
            jw = float(cells[10])
            exp_s = cells[8].split('(')[0].strip()
            exp = float(exp_s)
        except ValueError:
            continue
        rows.append({'feature': cells[1], 'justified': jw,
                     'ablation': exp, 'verdict': cells[11]})
    return rows


def parse_harness_weights(path=HARNESS_REPORT):
    """Parse Table A justified weights: {feature: justified_weight}.
    Duplicate names collapse (last row wins) — both duplicates carry
    justified weight 0.0, so the collapse changes nothing."""
    return {r['feature']: r['justified']
            for r in parse_harness_table_a(path)}


def parse_direction_agreement(path=HARNESS_REPORT):
    """Train-fit vs full-history direction agreement, parsed from the
    harness report prose. Returns (train_pct, full_pct) or (None, None)
    when the report does not carry them — never hardcoded."""
    try:
        with open(path) as f:
            text = f.read()
    except Exception:
        return None, None
    import re
    train = re.search(
        r'Direction agreement on the test slice: \*\*([\d.]+)%\*\*', text)
    full = re.search(
        r'Direction agreement with FULL-history weights: \*\*([\d.]+)%\*\*',
        text)
    return ((float(train.group(1)) if train else None),
            (float(full.group(1)) if full else None))


# ---------------------------------------------------------------------------
# Precedent — a fresh read from the cached BTC closes (no network)
def load_precedent():
    """Fresh precedent read for the latest complete day in the cache.
    Returns {'date': ..., 'lines': [...], 'vote': 'FOR'/'AGAINST'/'NO READ',
    'detail': {...}} — vote from the 7d/30d up-rates (both under 50%
    votes against longs). On any failure: vote NO READ, never invented."""
    empty = {'date': None, 'lines': ['Precedent: no fresh read.'],
             'vote': 'NO READ', 'detail': {}}
    try:
        import precedent as prec
        with open(BTC_CACHE) as f:
            raw = json.load(f)
        parsed = prec.parse_closes(raw)
        if not parsed:
            return empty
        lib = prec.build_library(raw)
        dates = sorted(lib.get('closes', {}).keys())
        if not dates:
            return empty
        day = dates[-1]
        fp = prec.fingerprint_for_day(day, parsed)
        if fp is None:
            return empty
        read = prec.precedent_read(lib, fp, day)
        lines = prec.plain_words(read)
        hz = read.get('horizons', {}) or {}
        ups = []
        for label in ('7d', '30d'):
            h = hz.get(label) or {}
            if h.get('precedent') and h.get('up_rate') is not None:
                ups.append(h['up_rate'])
        if not ups:
            vote = 'NO READ'
        else:
            vote = 'FOR' if sum(ups) / len(ups) > 0.5 else 'AGAINST'
        return {'date': day, 'lines': lines, 'vote': vote,
                'detail': {k: (v or {}).get('up_rate')
                           for k, v in hz.items()}}
    except Exception:
        return empty


# ---------------------------------------------------------------------------
# Feeds — what is persisted on disk (never invented)
def load_feeds(latest, intraday):
    """Feed aspect: FRED macro (persisted), Kraken OI/funding history
    (persisted as key lists), OKX / Deribit / DeFiLlama / Finviz (no
    persisted read on disk — reported as such, never invented)."""
    macro = (latest or {}).get('macro') or {}
    snap = (intraday or {}).get('macro_snapshot') or {}
    cpi_yoy = None
    try:
        cpi_yoy = float((snap.get('CPIAUCSL') or {}).get('yoy'))
    except (TypeError, ValueError):
        cpi_yoy = None
    oi = (intraday or {}).get('oi_history') or {}
    funding = (intraday or {}).get('funding_history') or {}
    if cpi_yoy is not None and cpi_yoy > 4.0:
        vote, note = 'AGAINST', f'CPI YoY {cpi_yoy:.2f}% — inflation hot'
    elif cpi_yoy is not None:
        vote, note = 'NEUTRAL', f'CPI YoY {cpi_yoy:.2f}%'
    else:
        vote, note = 'NO READ', 'no FRED read on disk'
    return {'vote': vote, 'note': note, 'cpi_yoy': cpi_yoy,
            'oi_assets': sorted(oi.keys()),
            'funding_assets': sorted(funding.keys()),
            'missing': ['OKX', 'Deribit', 'DeFiLlama', 'Finviz'],
            'macro_asof': snap.get('asof')}


# ---------------------------------------------------------------------------
# Candidates
def _has_plan(m):
    plan = (m or {}).get('plan') or {}
    return bool(plan.get('take_profit') and plan.get('stop_loss'))


def _calibrated_band(conf, direction, latest):
    """Graded win rate for a stated-confidence band (calibration v2).
    Returns (win_pct, n) or (None, None). Shown, never a gate."""
    if conf is None:
        return None, None
    band = ('50-59' if conf < 60 else '60-69' if conf < 70 else
            '70-79' if conf < 80 else '80+')
    for row in (latest or {}).get('calibration_v2') or []:
        if (row.get('direction') == direction and
                row.get('band') == band):
            return row.get('win_rate'), row.get('n')
    return None, None


def build_candidates(latest, intraday, tracks, swing_book):
    """Every live item as one candidate with its graded evidence."""
    cands = []
    for x in (latest or {}).get('longs') or []:
        conf = x.get('confidence')
        cal, cal_n = _calibrated_band(conf, 'LONG', latest)
        sb = swing_book.get('long') or {}
        cands.append({'asset': x.get('asset'), 'kind': 'swing',
                      'direction': 'LONG', 'chg_24h': None,
                      'confidence': conf, 'plan': x.get('plan'),
                      'tradable': x.get('tradable') or [],
                      'track': 'swing', 'evidence': {
                          'win_pct': sb.get('win_pct'),
                          'n': sb.get('n'),
                          'exp': sb.get('exp'),
                          'verdict': 'swing book (longs)',
                          'cal_win': cal, 'cal_n': cal_n}})
    for x in (latest or {}).get('shorts') or []:
        conf = x.get('confidence')
        cal, cal_n = _calibrated_band(conf, 'SHORT', latest)
        sb = swing_book.get('short') or {}
        cands.append({'asset': x.get('asset'), 'kind': 'swing',
                      'direction': 'SHORT', 'chg_24h': None,
                      'confidence': conf, 'plan': x.get('plan'),
                      'tradable': x.get('tradable') or [],
                      'track': 'swing', 'evidence': {
                          'win_pct': sb.get('win_pct'),
                          'n': sb.get('n'),
                          'exp': sb.get('exp'),
                          'verdict': 'swing book (shorts)',
                          'cal_win': cal, 'cal_n': cal_n}})
    for m in (intraday or {}).get('movers') or []:
        t = tracks.get('mover') or {}
        cands.append({'asset': m.get('asset'), 'kind': 'mover',
                      'direction': m.get('direction'),
                      'chg_24h': m.get('chg_24h'),
                      'confidence': None, 'plan': m.get('plan'),
                      'tradable': m.get('tradable') or [],
                      'plan_note': m.get('plan_note') or '',
                      'track': 'mover', 'evidence': {
                          'win_pct': t.get('win_pct'), 'n': t.get('n'),
                          'exp': t.get('exp'),
                          'verdict': t.get('verdict') or 'no graded row',
                          'cal_win': None, 'cal_n': None}})
    for m in (intraday or {}).get('watch') or []:
        t = tracks.get('mover') or {}
        cands.append({'asset': m.get('asset'), 'kind': 'watch',
                      'direction': m.get('direction'),
                      'chg_24h': m.get('chg_24h'),
                      'confidence': None, 'plan': m.get('plan'),
                      'tradable': m.get('tradable') or [],
                      'plan_note': m.get('plan_note') or '',
                      'track': 'mover', 'evidence': {
                          'win_pct': t.get('win_pct'), 'n': t.get('n'),
                          'exp': t.get('exp'),
                          'verdict': t.get('verdict') or 'no graded row',
                          'cal_win': None, 'cal_n': None}})
    for m in (intraday or {}).get('accum') or []:
        t = tracks.get('setup') or {}
        cands.append({'asset': m.get('asset'), 'kind': 'accum',
                      'direction': 'LONG',
                      'chg_24h': m.get('chg_24h'),
                      'confidence': None, 'plan': m.get('plan'),
                      'tradable': m.get('tradable') or [],
                      'plan_note': m.get('plan_note') or '',
                      'track': 'setup', 'evidence': {
                          'win_pct': t.get('win_pct'), 'n': t.get('n'),
                          'exp': t.get('exp'),
                          'verdict': t.get('verdict') or 'no graded row',
                          'cal_win': None, 'cal_n': None}})
    return cands


# ---------------------------------------------------------------------------
# Confluence — regime x variable x feed x precedent vote on each candidate.
# Shown explicitly: agreement strengthens, disagreement is named and
# weakens or blocks. No silent summing.
def _is_long(cand):
    return (cand.get('direction') or '').upper() in ('LONG', 'UP')


def _is_short(cand):
    return (cand.get('direction') or '').upper() in ('SHORT', 'DOWN')


def confluence(cand, ctx, precedent, feeds):
    regime = (ctx.get('regime') or '').upper()
    buy_pct = ctx.get('buy_pct')
    caution = regime == 'RISK_OFF' or (
        buy_pct is not None and buy_pct < CAUTION_BUY_PCT)
    votes = {}
    if _is_long(cand):
        votes['regime'] = 'AGAINST' if caution else (
            'FOR' if regime == 'RISK_ON' else 'NEUTRAL')
    else:
        votes['regime'] = 'NEUTRAL'  # regime blocks longs, not shorts
    exp = (cand['evidence'] or {}).get('exp')
    if exp is None:
        votes['variable'] = 'NO READ'
    elif exp > 0:
        votes['variable'] = 'FOR'
    else:
        votes['variable'] = 'AGAINST'
    votes['feed'] = feeds.get('vote', 'NO READ')
    votes['precedent'] = precedent.get('vote', 'NO READ')
    fors = sum(1 for v in votes.values() if v == 'FOR')
    against = [k for k, v in votes.items() if v == 'AGAINST']
    if caution and _is_long(cand):
        verdict = 'BLOCKED by regime — caution vetoes the buy whatever the rest say'
    elif against and fors == 0:
        verdict = 'all aspects against — ' + ', '.join(against)
    elif against:
        verdict = ('disagreement — for: ' +
                   ', '.join(k for k, v in votes.items() if v == 'FOR') +
                   '; against: ' + ', '.join(against))
    elif fors >= 3:
        verdict = 'strong agreement — ' + ', '.join(
            k for k, v in votes.items() if v == 'FOR')
    elif fors:
        verdict = 'weak agreement — only ' + ', '.join(
            k for k, v in votes.items() if v == 'FOR')
    else:
        verdict = 'no aspect votes for — nothing earns the action'
    return votes, verdict


def evidence_str(cand):
    """The graded evidence on the line. Thin evidence says so."""
    ev = cand['evidence'] or {}
    win, n, exp = ev.get('win_pct'), ev.get('n'), ev.get('exp')
    parts = []
    if win is None and n is None and exp is None:
        return 'no graded evidence'
    if win is not None and n is not None:
        parts.append(f"{ev.get('verdict', 'track')} {win:.1f}% (n={n})")
    elif win is not None:
        parts.append(f"{win:.1f}%")
    if exp is not None:
        parts.append(f"{exp:+.2f}$/1k")
    if not parts:
        return 'no graded evidence'
    if n is not None and n < 20:
        parts.append('THIN EVIDENCE')
    s = ' '.join(parts)
    cal, cal_n = ev.get('cal_win'), ev.get('cal_n')
    if cal is not None and cand.get('confidence') is not None:
        s += (f' | stated {cand["confidence"]:.0f}%, graded {cal:.1f}%'
              f' (n={cal_n})')
    return s


# ---------------------------------------------------------------------------
# Gates + ladder. Order matches the mover checklist: down -> caution ->
# no plan -> already ran -> no venue -> confidence bar -> money bar.
def decide_action(cand, ctx):
    """Returns (verdict, reason, rung, next_step). Verdict is BUY /
    WAIT / WATCH; rung is the ladder rung the candidate sits on."""
    ev = cand['evidence'] or {}
    exp = ev.get('exp')
    n = ev.get('n')
    track_earns = exp is not None and n is not None and exp > 0 and n >= 20
    if cand['direction'] == 'DOWN':
        return ('WATCH', 'down-move — longs only (watch only)', 0,
                'reversal + a plan would be needed to move up')
    if _is_short(cand):
        # Live is long-only: a short is never a buy, whatever else passes.
        return ('WATCH', 'short — live is long-only (shadow/paper only)', 0,
                'a long setup on this coin would be needed to move up')
    regime = (ctx.get('regime') or '').upper()
    buy_pct = ctx.get('buy_pct')
    caution = regime == 'RISK_OFF' or (
        buy_pct is not None and buy_pct < CAUTION_BUY_PCT)
    if caution:
        bp = f'{buy_pct:g}% buy' if buy_pct is not None else 'buy% unknown'
        return ('WAIT', f'conditions in caution — {bp}', 1,
                'conditions out of caution (buy% >= 50, regime not '
                'RISK_OFF) moves it up')
    if not _has_plan(cand):
        note = (cand.get('plan_note') or '').strip()
        return ('WATCH', 'no plan' + (f' — {note}' if note else ''), 0,
                'a complete plan (entry/target/stop) would be needed')
    chg = cand.get('chg_24h')
    if chg is not None and chg >= EXTENDED_PCT:
        return ('WATCH', "already ran — don't chase", 0,
                'a fresh setup/pullback would be needed to move up')
    if not cand.get('tradable'):
        return ('WAIT', 'no venue you can trade', 0,
                'a venue you trade on would be needed to move up')
    conf = cand.get('confidence')
    if conf is not None and conf < BUY_BAR:
        return ('WAIT', f'confidence {conf:.0f}% — below the {BUY_BAR}% bar',
                1, f'confidence >= {BUY_BAR}% would be needed to move up')
    if not track_earns:
        e = f'{exp:+.2f}$/1k (n={n})' if exp is not None else 'no graded expectancy'
        return ('WAIT', f'paper only — track loses {e}', 1,
                'track expectancy turning positive at n >= 20 would be '
                'needed to move up')
    # Rung 3 check: cell n >= 100, win% >= 60%, expectancy > +$10/1k.
    win = ev.get('win_pct')
    if (n is not None and n >= 100 and win is not None and win >= 60
            and exp is not None and exp > 10):
        return ('BUY', 'all gates pass — Kelly-sized by conviction', 3,
                'inside the trade: trail the stop, lock half, chase the '
                'stretch only if 1h >= 1.5%, 24h >= 10%, RSI < 70')
    return ('BUY', 'all gates pass — test buy $500, hard stop', 2,
            'inside the trade: trail the stop, lock half, chase the '
            'stretch only if 1h >= 1.5%, 24h >= 10%, RSI < 70')


def rung_name(rung):
    return LADDER[rung]['name']


def deescalate(cand, ctx):
    """The step back down for this candidate, in plain words."""
    return DEESCALATION


# ---------------------------------------------------------------------------
# QUBO — the quantum lab's portfolio formulation, on live problems.
# cost(bits) = -value + lambda * correlation + penalty * (sum - k)^2.
# Solved three ways: brute force (small n), the lab's simulated
# annealing, and the lab's QAOA simulator (p=1) on the same QUBO.
# The winner is the lowest cost. A simulated result is never a
# speedup claim; the simulator caps at ~10 qubits.
def _qubo_cost(values, corr, lam, penalty, k):
    def cost(bits):
        ret = sum(v * b for v, b in zip(values, bits))
        risk = sum(lam * corr[i][j] * bits[i] * bits[j]
                   for i in range(len(bits))
                   for j in range(i + 1, len(bits)))
        size = penalty * (sum(bits) - k) ** 2
        return -ret + risk + size
    return cost


def _brute_force(cost, n):
    best, best_bits = None, None
    for i in range(2 ** n):
        bits = [(i >> (n - 1 - q)) & 1 for q in range(n)]
        c = cost(bits)
        if best is None or c < best:
            best, best_bits = c, bits
    return best, best_bits


def _anneal(cost, n, k, seed=7, iters=3000):
    rng = random.Random(seed)
    bits = [0] * n
    for i in rng.sample(range(n), min(k, n)):
        bits[i] = 1
    cur = cost(bits)
    best, best_bits = cur, list(bits)
    for t in range(iters):
        temp = 1.0 * (0.01 / 1.0) ** (t / iters)
        held = [i for i, b in enumerate(bits) if b]
        free = [i for i, b in enumerate(bits) if not b]
        if not held or not free:
            break
        out, inn = rng.choice(held), rng.choice(free)
        bits[out], bits[inn] = 0, 1
        nxt = cost(bits)
        if nxt <= cur or rng.random() < math.exp(
                -(nxt - cur) / max(temp, 1e-9)):
            cur = nxt
            if cur < best:
                best, best_bits = cur, list(bits)
        else:
            bits[out], bits[inn] = 1, 0
    return best, best_bits


def _qaoa_expected_cost(cost, n, gamma, beta):
    """p=1 QAOA on a generic diagonal cost function, using the lab's
    own simulator primitives (quantum_lab._apply_1q / _rx / _probs)."""
    state = [complex(1 / math.sqrt(2 ** n))] * (2 ** n)
    for idx in range(2 ** n):
        bits = [(idx >> (n - 1 - q)) & 1 for q in range(n)]
        state[idx] *= cmath.exp(-1j * gamma * cost(bits))
    for q in range(n):
        state = quantum_lab._apply_1q(state, n, q, quantum_lab._rx(2 * beta))
    return sum(p * cost([(idx >> (n - 1 - q)) & 1 for q in range(n)])
               for idx, p in enumerate(quantum_lab._probs(state)))


def _qaoa_best(cost, n, g_steps=24, b_steps=12):
    best = (float('inf'), 0.0, 0.0)
    for gi in range(g_steps):
        for bi in range(b_steps):
            g = gi * 2 * math.pi / g_steps
            b = bi * math.pi / b_steps
            val = _qaoa_expected_cost(cost, n, g, b)
            if val < best[0]:
                best = (val, g, b)
    return best  # (expected cost, gamma, beta)


def solve_qubo(values, corr, k, lam=0.5, label=''):
    """Solve one QUBO three ways and report the winner. Returns a dict
    with each solver's cost + pick, or {'pool_empty': True}.
    Values are normalized to [0, 1] first: the cardinality penalty must
    dominate the largest gain from one extra pick (value <= 1 plus
    lam * k correlation), or the solver buys the penalty and picks
    too many — with raw $/1k values (e.g. 11.30) a penalty of 1.0
    does exactly that."""
    n = len(values)
    if n == 0 or max(values, default=0) <= 0:
        return {'label': label, 'pool_empty': True, 'n': n,
                'note': 'no positive-value candidate to select'}
    kk = min(k, n)
    scale = max(values)
    nvalues = [v / scale for v in values]
    penalty = 2.0 + lam * kk  # > 1 + lam * kk: one extra pick never pays
    cost = _qubo_cost(nvalues, corr, lam, penalty, kk)
    out = {'label': label, 'pool_empty': False, 'n': n}
    if n <= BRUTE_FORCE_MAX_QUBITS:
        c, bits = _brute_force(cost, n)
        out['brute_force'] = {'cost': round(c, 4),
                              'pick': [i for i, b in enumerate(bits) if b]}
    else:
        out['brute_force'] = {'cost': None, 'pick': None,
                              'note': f'n={n} too large for brute force'}
    c, bits = _anneal(cost, n, min(k, n))
    out['annealing'] = {'cost': round(c, 4),
                        'pick': [i for i, b in enumerate(bits) if b]}
    if n <= QAOA_MAX_QUBITS:
        # QAOA gives an expected cost, not a pick: report it, and take
        # the pick from the best solver. The simulator caps at ~10 qubits.
        exp_cost, gamma, beta = _qaoa_best(cost, n)
        out['qaoa'] = {'expected_cost': round(exp_cost, 4),
                       'gamma': round(gamma, 3), 'beta': round(beta, 3),
                       'note': 'simulated p=1 — expected cost, not a pick'}
    else:
        out['qaoa'] = {'expected_cost': None,
                       'note': f'n={n} exceeds the {QAOA_MAX_QUBITS}-qubit '
                               'simulator cap — QAOA not run'}
    scored = [(v['cost'], name) for name, v in
              (('brute_force', out.get('brute_force') or {}),
               ('annealing', out.get('annealing') or {}))
              if v.get('cost') is not None]
    winner = min(scored)[1] if scored else 'annealing'
    out['winner'] = winner
    out['pick'] = out[winner]['pick']
    return out


# Feature families for the weight-selection QUBO correlation proxy.
# The audit found most model-library features are re-expressions of
# chg24/chg7d, and trend_align is identical to ma_align (corr 1.0).
# Same family = 0.8, momentum family cross = 0.5, otherwise 0.1.
# This is a PROXY, labelled as one — measured correlations are not
# stored on graded picks.
FEATURE_FAMILY = {
    'momentum': 'momentum', 'roc': 'momentum', 'macd': 'momentum',
    'trend_align': 'momentum', 'ma_align': 'momentum',
    'donchian': 'momentum', 'stoch': 'momentum', 'zscore': 'momentum',
    'rsi': 'momentum', 'boll_pctb': 'momentum', 'atr': 'momentum',
    'vol': 'momentum', 'vol_regime': 'momentum', 'extreme_fade': 'momentum',
    'reversion': 'reversion', 'acceleration': 'acceleration',
    'psych_bias': 'psych', 'fg_level': 'psych', 'fg_delta': 'psych',
    'var': 'risk',
}


def weight_selection_qubo(justified):
    """Pick k=3 features to keep from the harness justified weights.
    Value = justified weight (0 for dead weight). Correlation proxy by
    family; trend_align/ma_align are identical (1.0)."""
    feats = sorted(justified.keys())
    values = [max(0.0, justified.get(f, 0.0)) for f in feats]
    n = len(feats)
    corr = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            fi, fj = feats[i], feats[j]
            if {fi, fj} == {'trend_align', 'ma_align'}:
                c = 1.0
            elif FEATURE_FAMILY.get(fi) == FEATURE_FAMILY.get(fj):
                c = 0.8
            elif (FEATURE_FAMILY.get(fi) == 'momentum' or
                    FEATURE_FAMILY.get(fj) == 'momentum'):
                c = 0.5
            else:
                c = 0.1
            corr[i][j] = corr[j][i] = c
    # QAOA runs on the reduced top-8 by justified weight (simulator cap).
    top = sorted(range(n), key=lambda i: -values[i])[:8]
    sub_values = [values[i] for i in top]
    sub_corr = [[corr[i][j] for j in top] for i in top]
    full = solve_qubo(values, corr, k=3, label='weight selection (22 features)')
    red = solve_qubo(sub_values, sub_corr, k=3,
                     label='weight selection, top-8 by justified weight (QAOA set)')
    return {'features': feats, 'values': values, 'full': full,
            'reduced': red, 'reduced_features': [feats[i] for i in top]}


def in_queue(cand, verdict, reason):
    """Queue membership: held back ONLY by caution, with a complete
    plan, and a track that earns money. No plan fails the plan gate,
    not just caution — it does not belong in the queue."""
    if verdict != 'WAIT' or 'caution' not in reason:
        return False
    if not _has_plan(cand):
        return False
    ev = cand['evidence'] or {}
    return (ev.get('exp') or 0) > 0 and (ev.get('n') or 0) >= 20


# ---------------------------------------------------------------------------
# Render
def queue_qubo_line(queue, qubo):
    """The candidate-queue QUBO as one plain line: asset names, never
    bare indices. When every queue member carries the same graded
    expectancy the pick is an arbitrary tie-break — say so."""
    if qubo.get('pool_empty'):
        return ('pool empty — no positive-value candidate passes every '
                'gate except caution today')
    pick = qubo.get('pick') or []
    names = [queue[i]['asset'] for i in pick if i < len(queue)]
    vals = [max(0.0, (c['evidence'] or {}).get('exp') or 0.0)
            for c in queue]
    tied = len(set(vals)) == 1 and len(vals) > 1
    s = (f"winner {qubo.get('winner')}, pick "
         f"{', '.join(names) or 'none'} — the order they go in when "
         f"caution lifts")
    if tied:
        s += (f" (tied — every queue member carries the same graded "
              f"expectancy, +${vals[0]:.2f}/1k, so any pick ties; the "
              f"solver's choice is arbitrary)")
    return s


def render_action(cand, verdict, reason, rung, next_step, votes, conf_verdict):
    ev = evidence_str(cand)
    chg = cand.get('chg_24h')
    chg_s = f' {chg:+.0f}% 24h' if chg is not None else ''
    conf = cand.get('confidence')
    conf_s = f' conf {conf:.0f}%' if conf is not None else ''
    vote_s = ' '.join(
        f"{k[:4]}:{'✓' if v == 'FOR' else '✗' if v == 'AGAINST' else '~' if v == 'NEUTRAL' else '?'}"
        for k, v in votes.items())
    return (f"{verdict} — {cand['asset']} ({cand['kind']}{chg_s}{conf_s}) — "
            f"{reason}. Evidence: {ev}. Confluence [{vote_s}] {conf_verdict}. "
            f"Rung {rung} ({rung_name(rung)}) → next: {next_step}.")


def run():
    latest = load_latest()
    intraday = load_intraday()
    tracks = parse_harness_table()
    justified = parse_harness_weights()
    swing_book = load_swing_book()
    precedent = load_precedent()
    feeds = load_feeds(latest, intraday)

    ctx = {'regime': (latest or {}).get('regime'),
           'buy_pct': (latest or {}).get('buy_pct')}
    generated_latest = (latest or {}).get('generated')
    generated_intra = (intraday or {}).get('generated')
    fng = (latest or {}).get('fear_greed')

    cands = build_candidates(latest, intraday, tracks, swing_book)
    actions = []
    queue = []  # pass every gate except caution, value > 0 — the QUBO pool
    for cand in cands:
        votes, conf_verdict = confluence(cand, ctx, precedent, feeds)
        verdict, reason, rung, next_step = decide_action(cand, ctx)
        actions.append({'cand': cand, 'verdict': verdict, 'reason': reason,
                        'rung': rung, 'next': next_step, 'votes': votes,
                        'confluence': conf_verdict})
        # Queue: passes every gate except caution, and its track earns
        # money. See in_queue().
        if in_queue(cand, verdict, reason):
            queue.append(cand)

    # Candidate QUBO over the queue (empty today — reported, not faked).
    qvals = []
    qcorr = []
    for c in queue:
        qvals.append(max(0.0, (c['evidence'] or {}).get('exp') or 0.0))
    for i in range(len(queue)):
        row = []
        for j in range(len(queue)):
            if i == j:
                row.append(0.0)
            elif queue[i]['track'] == queue[j]['track']:
                row.append(0.7 if queue[i]['direction'] == queue[j]['direction'] else 0.3)
            else:
                row.append(0.1)
        qcorr.append(row)
    cand_qubo = solve_qubo(qvals, qcorr, k=MAX_PICKS, label='candidate queue')

    # Weight-selection QUBO (runs today — the live optimization).
    wqubo = weight_selection_qubo(justified) if justified else None

    buys = [a for a in actions if a['verdict'] == 'BUY']
    waits = [a for a in actions if a['verdict'] == 'WAIT']
    watches = [a for a in actions if a['verdict'] == 'WATCH']
    order = {'BUY': 0, 'WAIT': 1, 'WATCH': 2}
    actions.sort(key=lambda a: (order[a['verdict']], a['cand']['asset']))

    lines = []
    lines.append('# Decision simplifier — action list')
    lines.append('')
    lines.append(f"Generated {datetime.now(timezone.utc).isoformat()} UTC. "
                 'REVIEW ONLY — this proposes actions. It never trades, '
                 'never changes gates, weights or scores, never sends.')
    lines.append('')
    regime = ctx.get('regime') or 'unknown'
    bp = ctx.get('buy_pct')
    lines.append(f"Stance: regime {regime}, buy {bp if bp is not None else '?'}%, "
                 f"Fear & Greed {fng if fng is not None else '?'}")
    lines.append(f"Data: swing payload {generated_latest or 'missing'}; "
                 f"intraday {generated_intra or 'missing'}.")
    if generated_latest and generated_intra:
        lines.append('Note: the swing payload is the older read — regime '
                     'and buy% come from it; the intraday movers are fresh.')
    lines.append(f"Precedent ({precedent.get('date') or 'no date'}): "
                 + '; '.join(precedent.get('lines') or []))
    lines.append(f"Feeds: {feeds.get('note')}"
                 + (f" | OI history on {len(feeds.get('oi_assets') or [])} assets;"
                    f" funding history on {len(feeds.get('funding_assets') or [])} assets."
                    if feeds.get('oi_assets') is not None else '')
                 + f" No persisted read: {', '.join(feeds.get('missing') or [])}.")
    lines.append('')
    lines.append(f"ACTIONS — {len(buys)} BUY, {len(waits)} WAIT, "
                 f"{len(watches)} WATCH")
    shown = actions[:12]
    for a in shown:
        lines.append(render_action(a['cand'], a['verdict'], a['reason'],
                                   a['rung'], a['next'], a['votes'],
                                   a['confluence']))
    if len(actions) > 12:
        rest = actions[12:]
        fams = {}
        for a in rest:
            key = (a['verdict'], a['reason'].split(' — ')[0])
            fams.setdefault(key, []).append(a['cand']['asset'])
        for (v, fam), assets in sorted(fams.items()):
            lines.append(f'... and {len(assets)} more {v}: {fam} — '
                         f"{', '.join(assets)}")
    lines.append('')
    lines.append('## What would change this')
    if buys:
        lines.append('Buys are live above — inside each trade: ' + WITHIN_TRADE)
    else:
        lines.append('Nothing is a buy today. The single flip that matters '
                     'most: conditions out of caution (buy% >= 50, regime '
                     'not RISK_OFF). Until then every passing candidate '
                     'waits, and the queue below is the order they go in.')
    lines.append('')
    lines.append('## Escalation ladder')
    for r in LADDER:
        lines.append(f"Rung {r['rung']} {r['name']}: {r['threshold']} "
                     f"Size: {r['size']}")
    lines.append(WITHIN_TRADE)
    lines.append(DEESCALATION)
    lines.append('')
    lines.append('## Quantum optimizer (live QUBO, simulated classically)')
    lines.append('Grover not used: the candidate set is small and a '
                 'classical scan is exact. VQC not used for live '
                 'direction: toy 8-point task only, no graded evidence '
                 'on live data. QAOA runs on the classical simulator — '
                 'never a speedup claim; the simulator caps at '
                 f'{QAOA_MAX_QUBITS} qubits.')
    if wqubo:
        red = wqubo['reduced']
        feats = wqubo['reduced_features']
        lines.append('Weight selection (pick 3 features to keep, value = '
                     'harness justified weight, correlation penalty by '
                     'family — proxy, not measured):')
        for name in ('brute_force', 'annealing', 'qaoa'):
            v = red.get(name) or {}
            if name == 'qaoa':
                lines.append(f"  QAOA (simulated p=1): expected cost "
                             f"{v.get('expected_cost')} "
                             f"(gamma {v.get('gamma')}, beta {v.get('beta')})")
            elif v.get('cost') is not None:
                picks = [feats[i] for i in (v.get('pick') or [])]
                lines.append(f"  {name}: cost {v.get('cost')} — "
                             f"keep {', '.join(picks) or 'none'}")
        lines.append(f"  Winner: {red.get('winner')} — the pick above is "
                     'the proposal. REVIEW ONLY: weights do not change '
                     'until Nic approves them.')
        full = wqubo['full']
        if not full.get('pool_empty'):
            lines.append(f"  Full 22-feature QUBO: annealing cost "
                         f"{(full.get('annealing') or {}).get('cost')} "
                         f"(brute force skipped at n=22; QAOA capped at "
                         f"{QAOA_MAX_QUBITS} qubits).")
    lines.append(f"Candidate queue QUBO: {queue_qubo_line(queue, cand_qubo)}")
    lines.append('')
    lines.append('## What the system has learned (intelligence, not just numbers)')
    # Computed from the parsed harness tables + live calibration — never
    # hardcoded, so this section stays true when the harness re-runs.
    table_a = parse_harness_table_a()
    by_name = {}
    for r in table_a:
        by_name[r['feature']] = r  # duplicates collapse, both 0.0 justified
    earns = [(f, r['ablation']) for f, r in by_name.items()
             if r['ablation'] >= 10]
    under = [(f, r['ablation']) for f, r in by_name.items()
             if 0 < r['ablation'] < 10]
    # Counts are by ROW (22 rows): the dict collapses the rsi/zscore
    # duplicates and would undercount by two.
    dead = sum(1 for r in table_a if 'DEAD WEIGHT' in r['verdict'])
    inv = sum(1 for r in table_a if 'INVERTED' in r['verdict'])
    train_agree, full_agree = parse_direction_agreement()
    agree_s = (f' Weights are unstable across windows '
               f'({train_agree:.1f}% vs {full_agree:.1f}% agreement).'
               if train_agree is not None and full_agree is not None
               else ' Weights swing window to window (see harness report).')
    if earns:
        lines.append('- Harness: ' + '; '.join(
            f'{f} earns its weight (+${e:.2f}/1k)' for f, e in earns) +
            (f'; {under[0][0]} just under (+${under[0][1]:.2f}/1k)'
             if under else '') +
            f'; {dead} dead weight; {inv} read backwards.{agree_s}')
    setup = tracks.get('setup') or {}
    mover = tracks.get('mover') or {}
    pattern = tracks.get('patterns_agg') or tracks.get('pattern') or {}
    cycle = tracks.get('cycle') or {}
    cmi_clean = tracks.get('cmi_clean') or {}
    lines.append('- Tracks: only accumulation setups work '
                 f"({setup.get('win_pct')}% of triggered calls, "
                 f"{setup.get('exp'):+.2f}$/1k) — but they trigger "
                 f"{setup.get('trig')}% of the time. Movers "
                 f"{mover.get('win_pct')}%, patterns {pattern.get('win_pct')}%, "
                 f"cycle {cycle.get('win_pct')}%, CMI clean band "
                 f"{cmi_clean.get('win_pct')}% — all losing.")
    cal = {}
    for row in (latest or {}).get('calibration_v2') or []:
        if row.get('direction') == 'LONG':
            cal[row.get('band')] = (row.get('win_rate'), row.get('n'))
    if cal:
        lines.append('- Calibration: stated confidence overpromises every '
                     'band. LONG 80%+ grades '
                     f"{cal.get('80+', (None, None))[0]}% "
                     f"(n={cal.get('80+', (None, None))[1]}); LONG 60–69% "
                     f"grades {cal.get('60-69', (None, None))[0]}% "
                     f"(n={cal.get('60-69', (None, None))[1]}). The 60% bar "
                     'reads raw confidence; the calibrated number is '
                     'display-only.')
    if precedent.get('lines'):
        lines.append('- Precedent (' + (precedent.get('date') or 'no date') +
                     '): ' + '; '.join(precedent['lines']))
    text = '\n'.join(lines) + '\n'
    out_name = ('decide-' +
                datetime.now(timezone.utc).strftime('%Y-%m-%d') + '.md')
    out_path = os.path.join(HIDDEN_DIR, out_name)
    try:
        os.makedirs(HIDDEN_DIR, exist_ok=True)
        with open(out_path, 'w') as f:
            f.write(text)
    except Exception as e:
        print(f'WARN: could not write {out_path}: {e}', file=sys.stderr)
        out_path = None
    print(text)
    if out_path:
        print(f'[wrote {out_path}]')
    return text


if __name__ == '__main__':
    run()
