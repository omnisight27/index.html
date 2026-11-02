#!/usr/bin/env python3
"""
self_tune.py -- shadow-mode self-tuning calibration loop for the NexIntel engine.

SHADOW MODE. Read-only against live data. This script:
  - NEVER writes to live scoring paths (data/intraday.json, data/history.json)
  - NEVER changes alerts, buy gates, scoring weights, or verdicts
  - NEVER makes network calls, sends, submits, or contacts anyone
  - NEVER trades

What it does:
  1. Loads the graded-pick ledger (data/history.json): features, confidence,
     win/loss per scored pick.
  2. Splits scored runs by time: first 80% train / last 20% holdout.
  3. Baseline weights = nexintel.learn_weights() on the train slice (the exact
     code the live trainer uses), else HEURISTIC_W.
  4. Calibration error per confidence band (50-59 / 60-69 / 70-79 / 80+ --
     the track-record bands): n-weighted mean |mean confidence - win rate|,
     in percentage points.
  5. The "derivative" step: a smooth soft-band surrogate of the calibration
     error (differentiable in the weights; hard band edges would make finite
     differences pure noise) -> central finite-difference gradient over the
     22 feature weights -> one elasticity-scaled gradient step with a small
     line search -> proposed weights.
  6. Paper-grades baseline vs proposed on the holdout (hard bands), reporting
     per-band win rates WITH 95% Wilson score confidence intervals.
  7. Ablates each scoring variable (zeroes its weight) on the holdout and
     ranks variables by significance -- Nic's definition: "Significance is
     predictability based on variables used for an outcome." Significance =
     how much outcome predictability (calibration error, plus directional
     accuracy alongside) changes when the variable is removed.
  8. Writes data/proposed_weights.json (REVIEW ONLY -- the live engine must
     never read it) and a dated report under hidden_files/.

Approximations, stated plainly (shadow paper-grading, not the live pipeline):
  - Confidence is recomputed from the weight-driven score only, via the raw
    mapping conf = clamp(50 + |score| * k) (k = 1.5 crypto, 2.0 equity --
    the same mapping nexintel.py uses before docks). The geometric edge,
    regime tilt, timeframe/gold/risk docks, and the band-shrink calibration
    layer are HELD FIXED, not re-simulated. Baseline and proposed are
    evaluated through the identical mapping, so the before/after delta is
    attributable purely to the weight change.

Fail-open: any missing/unparseable/insufficient data -> warning printed,
minimal report written, exit code 0. No changes are ever made.
"""

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from nexintel import (  # noqa: E402  (same trainer/scoring code the live engine uses)
    learn_weights, HEURISTIC_W, N_FEATURES, FEATURE_NAMES,
    _excluded_pick, _pad, clamp, CONF_BANDS,
)
from econ_features import ECON_FEATURE_NAMES, N_ECON  # noqa: E402  (milestone-2 candidates)
from insider import INSIDER_FEATURE_NAMES, N_INSIDER  # noqa: E402  (insider-flow candidates)

# Candidate (econ + insider) variables ride AFTER the live 22 dims. They are
# recorded per pick by the daily engine as `econ_features` (ordered like
# ECON_FEATURE_NAMES) and `insider_features` (INSIDER_FEATURE_NAMES order);
# older picks predate them and pad to 0.0 — the same fail-open convention
# as _pad. The live engine never sees these dims: its scoring slices the
# first N_FEATURES, so the baseline below reproduces live behavior exactly
# (candidate weights start at 0.0).
N_ALL = N_FEATURES + N_ECON + N_INSIDER

LEDGER_PATH = REPO_ROOT / "data" / "history.json"
PROPOSED_PATH = REPO_ROOT / "data" / "proposed_weights.json"
REPORT_DIR = Path(os.environ.get(
    "SELF_TUNE_REPORT_DIR",
    str(Path.home() / "workspace/goals/build-out-omnisight/hidden_files")))

HOLDOUT_FRAC = 0.20
MIN_GRADED = 50       # need at least this many graded picks overall
MIN_HOLDOUT = 10      # need at least this many holdout picks
TAU = 5.0             # soft-band temperature, in confidence points
Z95 = 1.96            # 95% two-sided normal quantile
LR_GRID = [0.02, 0.05, 0.1, 0.2, 0.5, 1.0]
MAX_REL_MOVE = 0.5    # a single shadow step may move a weight at most +/-50%

BAND_LABELS = [label for label, _lo, _hi in CONF_BANDS]
BAND_CENTERS = {"50-59": 54.5, "60-69": 64.5, "70-79": 74.5, "80+": 90.0}

REVIEW_ONLY_NOTE = ("REVIEW ONLY -- shadow-mode proposal. The live engine must "
                    "never read this file. Nothing here is applied without "
                    "Nic's explicit approval.")


def warn(msg):
    print(f"[self-tune] {msg} (fail-open: no changes made)")


# ------------------------------------------------------------------ loading

def load_history(path=LEDGER_PATH):
    """Return the ledger dict, or None (fail-open) if missing/unparseable."""
    try:
        return json.loads(Path(path).read_text())
    except Exception as e:  # missing file, bad JSON, unreadable -- all fail open
        warn(f"ledger unreadable at {path}: {e}")
        return None


def _econ_pad(ef):
    """Pad/truncate a pick's econ_features to N_ECON. None/missing -> 0.0
    (fail open, same convention as the live _pad)."""
    out = []
    for i in range(N_ECON):
        try:
            v = ef[i] if ef else None
        except (TypeError, IndexError):
            v = None
        out.append(0.0 if v is None else float(v))
    return out


def _insider_pad(inf):
    """Pad/truncate a pick's insider_features to N_INSIDER. None/missing ->
    0.0 (fail open). Stock picks recorded before the insider feed carry no
    insider_features and pad to zero — labeled 'awaiting data' in ranking."""
    out = []
    for i in range(N_INSIDER):
        try:
            v = inf[i] if inf else None
        except (TypeError, IndexError):
            v = None
        out.append(0.0 if v is None else float(v))
    return out


def graded_rows_from_runs(runs):
    """Flatten scored runs into graded rows, honoring the exclusion list."""
    rows = []
    for run in runs:
        if not run.get("scored"):
            continue
        ts = run.get("date") or ""
        for p in list(run.get("longs", [])) + list(run.get("shorts", [])):
            if _excluded_pick(p):
                continue
            f, win, rp = p.get("features"), p.get("win"), p.get("return_pct")
            if not f or win is None or rp is None:
                continue
            rows.append({
                "ts": ts,
                "features": (_pad(f) + _econ_pad(p.get("econ_features"))
                             + _insider_pad(p.get("insider_features"))),
                "win": bool(win),
                "return_pct": float(rp),
                "cls": p.get("class") or "crypto",
            })
    return rows


def split_runs(runs, frac=HOLDOUT_FRAC):
    """Time-ordered split: oldest runs -> train, newest ~frac of picks -> holdout."""
    scored = sorted([r for r in runs if r.get("scored")],
                    key=lambda r: r.get("date") or "")

    def n_graded(run):
        return sum(1 for p in list(run.get("longs", [])) + list(run.get("shorts", []))
                   if not _excluded_pick(p) and p.get("features")
                   and p.get("win") is not None and p.get("return_pct") is not None)

    counts = [n_graded(r) for r in scored]
    target = sum(counts) * frac
    hold_n, cut = 0, len(scored)
    for i in range(len(scored) - 1, -1, -1):
        hold_n += counts[i]
        cut = i
        if hold_n >= target:
            break
    return scored[:cut], scored[cut:]


# ------------------------------------------------------------------ scoring

def raw_confidence(score, cls):
    """The raw score->confidence mapping from nexintel.py, before any docks."""
    k = 2.0 if cls == "equity" else 1.5
    return clamp(50.0 + abs(score) * k)


def band_of(conf):
    # Round first: the live pipeline stores round(conf) (integer), and the
    # track-record bands are integer ranges -- this keeps shadow banding
    # identical to the ledger's convention.
    c = int(round(conf))
    for label, lo, hi in CONF_BANDS:
        if lo <= c <= hi:
            return label
    return BAND_LABELS[-1] if c > 80 else BAND_LABELS[0]


def hard_band_stats(rows, weights):
    """Per-band {n, win_rate, mean_conf} under hard band assignment."""
    bands = {label: {"n": 0, "wins": 0, "conf_sum": 0.0} for label in BAND_LABELS}
    for r in rows:
        s = sum(w * f for w, f in zip(weights, r["features"]))
        c = raw_confidence(s, r["cls"])
        d = bands[band_of(c)]
        d["n"] += 1
        d["wins"] += 1 if r["win"] else 0
        d["conf_sum"] += c
    out = {}
    for label, d in bands.items():
        n = d["n"]
        out[label] = {"n": n,
                      "win_rate": (d["wins"] / n) if n else 0.0,
                      "mean_conf": (d["conf_sum"] / n) if n else 0.0}
    return out


def calibration_error(band_stats):
    """n-weighted mean |mean_conf - 100*win_rate|, in percentage points."""
    tot = sum(d["n"] for d in band_stats.values())
    if not tot:
        return 0.0
    return sum(d["n"] * abs(d["mean_conf"] - 100.0 * d["win_rate"])
               for d in band_stats.values()) / tot


def directional_accuracy(rows, weights):
    """Fraction of picks where sign(score) matched the realized direction."""
    ok = n = 0
    for r in rows:
        s = sum(w * f for w, f in zip(weights, r["features"]))
        ok += ((s > 0) == (r["return_pct"] > 0))
        n += 1
    return (ok / n) if n else 0.0


def soft_band_error(rows, weights, tau=TAU):
    """Smooth (differentiable) surrogate of calibration_error.

    Hard band edges make finite differences pure noise (a weight nudge either
    moves nobody across an edge -> zero gradient, or a few edge picks ->
    jumpy gradient). Softmax band membership keeps the derivative honest.
    Used ONLY for the gradient step; all reporting uses hard bands.
    """
    agg = {label: [0.0, 0.0, 0.0] for label in BAND_LABELS}  # w, conf*w, win*w
    for r in rows:
        s = sum(w * f for w, f in zip(weights, r["features"]))
        c = raw_confidence(s, r["cls"])
        exps = [math.exp(-((c - BAND_CENTERS[l]) ** 2) / (2.0 * tau * tau))
                for l in BAND_LABELS]
        tot_e = sum(exps) or 1e-12
        y = 1.0 if r["win"] else 0.0
        for l, e in zip(BAND_LABELS, exps):
            q = e / tot_e
            a = agg[l]
            a[0] += q
            a[1] += q * c
            a[2] += q * y
    n_tot = sum(a[0] for a in agg.values()) or 1e-12
    err = 0.0
    for a in agg.values():
        if a[0] > 1e-12:
            err += a[0] * abs(a[1] / a[0] - 100.0 * a[2] / a[0])
    return err / n_tot


# ------------------------------------------------------------------ Wilson CI

def wilson_interval(wins, n, z=Z95):
    """95% Wilson score interval for a binomial proportion. Returns (lo, hi)."""
    if n <= 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


# ------------------------------------------------------------------ tuning

def finite_diff_gradient(rows, weights, err_fn=soft_band_error, h_rel=1e-4):
    """Central finite differences: d(error)/d(w_i) for each weight."""
    grad = []
    for i, w in enumerate(weights):
        h = h_rel * max(abs(w), 1e-3)
        wp = list(weights); wp[i] = w + h
        wm = list(weights); wm[i] = w - h
        grad.append((err_fn(rows, wp) - err_fn(rows, wm)) / (2.0 * h))
    return grad


def propose_weights(rows, weights, grad, err_fn=soft_band_error):
    """One elasticity-scaled gradient step with line search.

    Step is scale-free: weight i moves by factor (1 - lr * w_i * grad_i / E),
    i.e. proportional to its error-elasticity, clipped to +/-MAX_REL_MOVE so
    a single shadow step can never blow a weight up. The lr=0 candidate
    (no change) is always in the running, so the proposal never worsens the
    train surrogate.
    """
    e0 = err_fn(rows, weights)
    best = {"weights": list(weights), "lr": 0.0,
            "err_before": e0, "err_after": e0, "grad": grad, "tried": []}
    for lr in LR_GRID:
        new = []
        for i, w in enumerate(weights):
            factor = 1.0 - lr * w * grad[i] / (e0 + 1e-12)
            factor = min(1.0 + MAX_REL_MOVE, max(1.0 - MAX_REL_MOVE, factor))
            new.append(w * factor)
        e = err_fn(rows, new)
        best["tried"].append({"lr": lr, "err": e})
        if e < best["err_after"]:
            best["weights"], best["lr"], best["err_after"] = new, lr, e
    return best


def _univariate_weight(rows, i):
    """Best single-variable least-squares weight for feature i on rows:
    w = cov(f_i, return_pct) / var(f_i). 0.0 when the column has no variation
    (fail open — e.g. a candidate with no recorded data yet). return_pct is
    direction-adjusted (a winning short counts positive), so a positive
    weight means "higher feature value -> better outcome"."""
    n = len(rows)
    if n < 2:
        return 0.0
    xs = [r["features"][i] for r in rows]
    ys = [r["return_pct"] for r in rows]
    mx = sum(xs) / n
    my = sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    if var < 1e-12:
        return 0.0
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / var


def _column_has_data(rows, i):
    """Any nonzero observation of feature i (guards 'awaiting data')."""
    return any(r["features"][i] != 0.0 for r in rows)


def significance_ranking(rows, weights, train_rows=None):
    """Ablate each variable (zero its weight); rank by predictability delta.

    Nic's definition: "Significance is predictability based on variables used
    for an outcome." Significance of variable i = how much outcome
    predictability changes when it is removed: delta = error(without i) -
    error(with i), on the holdout, in percentage points.
      delta > 0  -> removing it HURTS: the variable earns its place
      delta ~ 0  -> dead weight
      delta < 0  -> removing it HELPS: it is pushing confidence the wrong way
    Directional-accuracy delta is reported alongside for the second angle.

    Candidate variables (dims >= N_FEATURES — the milestone-2 econ pack and
    the insider-flow pack) start
    at weight 0.0, so plain ablation would always read zero. Instead each
    candidate is screened at its univariate-optimal weight (fit on the TRAIN
    slice, never the holdout): delta = base_err - err(with candidate). The
    sign convention and verdict thresholds are identical to the live vars.
    A candidate with no recorded observations yet is labeled "awaiting data"
    — never "dead weight".
    """
    base_err = calibration_error(hard_band_stats(rows, weights))
    base_acc = directional_accuracy(rows, weights)
    names = _display_names()
    ranking = []
    for i, name in enumerate(names):
        note = ""
        if i < N_FEATURES:
            # Live variable: ablate (zero its weight) — the original logic.
            w_test = list(weights)
            w_test[i] = 0.0
            disp_w = 0.0
        else:
            # Candidate: screen at its univariate-optimal train weight.
            if train_rows is None or not _column_has_data(train_rows + rows, i):
                ranking.append({
                    "feature": name, "index": i, "weight": 0.0,
                    "delta_calib_pp": 0.0, "delta_accuracy_pp": 0.0,
                    "note": "awaiting data",
                })
                continue
            wu = _univariate_weight(train_rows, i)
            w_test = list(weights)
            w_test[i] = wu
            disp_w = wu
            note = "candidate"
        e = calibration_error(hard_band_stats(rows, w_test))
        a = directional_accuracy(rows, w_test)
        ranking.append({
            "feature": name,
            "index": i,
            "weight": disp_w,
            "delta_calib_pp": e - base_err,
            "delta_accuracy_pp": 100.0 * (a - base_acc),
            "note": note,
        })
    ranking.sort(key=lambda d: d["delta_calib_pp"], reverse=True)
    return ranking, base_err, base_acc


# ------------------------------------------------------------------ reporting

def _display_names():
    """Feature names, disambiguated with (#index) where the engine reuses
    a name (e.g. base 'rsi' vs model-library 'rsi')."""
    names = (list(FEATURE_NAMES[:N_FEATURES]) + list(ECON_FEATURE_NAMES)
             + list(INSIDER_FEATURE_NAMES))
    while len(names) < N_FEATURES:
        names.append(f"f{len(names)}")
    counts = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1
    return [f"{n} (#{i})" if counts[n] > 1 else n
            for i, n in enumerate(names)]


def _band_line(label, st):
    n = st["n"]
    if n == 0:
        return f"{label}: n=0 -- no holdout picks landed here"
    wins = int(round(st["win_rate"] * n))
    lo, hi = wilson_interval(wins, n)
    err = abs(st["mean_conf"] - 100.0 * st["win_rate"])
    return (f"{label}: n={n}, mean conf {st['mean_conf']:.1f}%, "
            f"measured {100*st['win_rate']:.1f}% "
            f"(95% CI {100*lo:.0f}-{100*hi:.0f}%), |err| {err:.1f}pp")


def _ci_honesty_line(label, st):
    n = st["n"]
    if n == 0:
        return f"{label}: no data -- no precision claim possible."
    wins = int(round(st["win_rate"] * n))
    lo, hi = wilson_interval(wins, n)
    return (f"{label}: at 95% confidence the true win rate is {100*lo:.0f}-"
            f"{100*hi:.0f}% -- claiming precision above {100*hi:.0f}% is "
            f"not supported.")


def build_report(ctx):
    L = []
    A = L.append
    A(f"# Self-tune calibration report -- {ctx['date']}")
    A("")
    A("**SHADOW MODE.** Read-only against live data. Nothing here touches "
      "scoring, alerts, buy gates, or verdicts. The proposed weights are "
      "review material only -- the live engine does not read "
      "`data/proposed_weights.json`.")
    A("")
    A(f"- Ledger: `{ctx['ledger']}` -- {ctx['n_graded']} graded picks "
      f"({ctx['n_wins']}W/{ctx['n_graded']-ctx['n_wins']}L)")
    A(f"- Split: train {ctx['n_train']} picks "
      f"({ctx['train_from']} .. {ctx['train_to']}), holdout {ctx['n_holdout']} "
      f"picks ({ctx['hold_from']} .. {ctx['hold_to']}) -- last "
      f"{int(HOLDOUT_FRAC*100)}% by time")
    A(f"- Baseline weights: {ctx['baseline_source']}")
    A(f"- Train surrogate error: {ctx['train_err_before']:.3f}pp -> "
      f"{ctx['train_err_after']:.3f}pp (lr={ctx['lr']})")
    A("")
    A("## Track-record reference (stored confidence, all-time)")
    A("")
    A("The ledger's own calibration bands, with 95% Wilson intervals -- "
      "the interval IS the significance claim.")
    A("")
    for b in ctx["reference_bands"]:
        n = b["n"]
        wins = int(round(b["win_rate"] / 100.0 * n)) if n else 0
        lo, hi = wilson_interval(wins, n)
        A(f"- {b['band']}: measured {b['win_rate']:.1f}% "
          f"(95% CI {100*lo:.0f}-{100*hi:.0f}%, n={n})")
    A("")
    A("## Holdout paper-grade: baseline vs proposed (hard bands)")
    A("")
    A("Confidence recomputed from the weight-driven score through the raw "
      "mapping (geometric edge, regime tilt, and docks held fixed -- "
      "identical for both, so the delta is pure weight effect).")
    A("")
    A(f"**Baseline** holdout calibration error: {ctx['base_err']:.2f}pp, "
      f"directional accuracy {100*ctx['base_acc']:.1f}%")
    for label in BAND_LABELS:
        A(f"- {_band_line(label, ctx['base_stats'][label])}")
    A("")
    A(f"**Proposed** holdout calibration error: {ctx['new_err']:.2f}pp, "
      f"directional accuracy {100*ctx['new_acc']:.1f}%")
    for label in BAND_LABELS:
        A(f"- {_band_line(label, ctx['new_stats'][label])}")
    A("")
    d_err = ctx['new_err'] - ctx['base_err']
    d_acc = 100 * (ctx['new_acc'] - ctx['base_acc'])
    A(f"**Delta (proposed - baseline):** calibration error "
      f"{d_err:+.2f}pp, directional accuracy {d_acc:+.1f}pp.")
    A("")
    A("### 95% honesty check per band (proposed weights, holdout)")
    A("")
    for label in BAND_LABELS:
        A(f"- {_ci_honesty_line(label, ctx['new_stats'][label])}")
    A("")
    A("## Proposed weight deltas")
    A("")
    A("| # | variable | baseline | proposed | delta | delta% |")
    A("|---|----------|----------|----------|-------|--------|")
    names = _display_names()
    for i, name in enumerate(names):
        b, p = ctx["w0"][i], ctx["w1"][i]
        d = p - b
        dpct = (100.0 * d / b) if b != 0 else 0.0
        A(f"| {i} | {name} | {b:.4f} | {p:.4f} | {d:+.4f} | {dpct:+.1f}% |")
    A("")
    A("## Significance ranking (per-variable ablation on the holdout)")
    A("")
    A('Nic\'s definition: "Significance is predictability based on variables '
      'used for an outcome." Live variables: each weight was set to zero and '
      'the holdout re-graded — the delta is how much outcome predictability '
      'changed. Candidate variables (econ + insider packs, marked '
      '"candidate"): screened at their univariate-optimal weight fit on the '
      'TRAIN slice — the delta is how much the holdout error drops when the '
      'candidate joins the live baseline. Positive = removing it HURTS '
      '(earns its place); near zero = dead weight; negative = removing it '
      'HELPS (pushing confidence the wrong way — candidate for review, not '
      'automatic removal). "awaiting data" = no recorded observations yet, '
      'not a verdict. Thresholds (+/-0.5pp) are a labeling heuristic; small '
      'deltas are within holdout noise.')
    A("")
    A("| rank | variable | weight | d(calib err) pp | d(accuracy) pp | verdict |")
    A("|------|----------|--------|-----------------|----------------|---------|")
    for r, row in enumerate(ctx["ranking"], 1):
        d = row["delta_calib_pp"]
        note = row.get("note") or ""
        if note == "awaiting data":
            verdict = "awaiting data"
        else:
            base_verdict = ("earns its place" if d > 0.5 else
                            "dead weight" if d >= -0.5 else "review: removal helps")
            verdict = (f"candidate: {base_verdict}" if note == "candidate"
                       else base_verdict)
        A(f"| {r} | {row['feature']} | {row['weight']:.4f} | {d:+.3f} | "
          f"{row['delta_accuracy_pp']:+.2f} | {verdict} |")
    A("")
    A("## Caveats")
    A("")
    A("- Paper grading recomputes confidence from the linear score only; the "
      "geometric layer, regime tilt, timeframe/gold/risk docks, and the "
      "band-shrink calibration layer are held fixed.")
    A("- The gradient step optimizes a smooth soft-band surrogate on the "
      "train slice; reported errors use hard bands on unseen holdout data.")
    A("- Holdout CIs are wide by construction (small n per band) -- that is "
      "the honest precision picture, not a defect in the method.")
    A("- Paper-graded confidences cluster in the 50-59 band because the raw "
      "mapping omits the docks and band-shrink that spread stored "
      "confidences across bands in production; the holdout comparison is "
      "therefore effectively single-band.")
    A("- One conservative step (max +/-50% per weight, line-searched). "
      "Nothing is applied to the live engine.")
    A("- Candidate (econ + insider) variables start at weight 0.0, so the shadow "
      "gradient step cannot move them (multiplicative update); they are "
      "screened by the univariate ranking above, and promotion to the live "
      "vector needs Nic's explicit approval.")
    A("")
    return "\n".join(L) + "\n"


def write_outputs(ctx):
    """Write the dated report and the review-only proposed-weights JSON."""
    date = ctx["date"]
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        rp = REPORT_DIR / f"self-tune-{date}.md"
        rp.write_text(build_report(ctx))
        # Verify the write: a silently-missing report once left the
        # weights updated with no matching human-readable record.
        if not rp.exists() or rp.stat().st_size == 0:
            raise OSError(f"report missing or empty after write: {rp}")
        print(f"[self-tune] report -> {rp}")
    except Exception as e:
        warn(f"could not write report: {type(e).__name__}: {e}")

    names = _display_names()
    payload = {
        "_note": REVIEW_ONLY_NOTE,
        "generated": datetime.now(timezone.utc).isoformat(),
        "mode": "shadow",
        "ledger": str(ctx["ledger"]),
        "feature_names": names,
        "baseline": {"source": ctx["baseline_source"], "weights": ctx["w0"]},
        "proposed": {
            "weights": ctx["w1"],
            "deltas": [p - b for b, p in zip(ctx["w0"], ctx["w1"])],
            "line_search_lr": ctx["lr"],
            "train_surrogate_err_before_pp": ctx["train_err_before"],
            "train_surrogate_err_after_pp": ctx["train_err_after"],
        },
        "holdout": {
            "n": ctx["n_holdout"],
            "baseline_calib_err_pp": ctx["base_err"],
            "proposed_calib_err_pp": ctx["new_err"],
            "baseline_accuracy": ctx["base_acc"],
            "proposed_accuracy": ctx["new_acc"],
        },
        "significance_ranking": ctx["ranking"],
    }
    try:
        # Review material only -- written next to the ledger, never read by
        # the engine (verified: no engine module references this filename).
        PROPOSED_PATH.write_text(json.dumps(payload, indent=1))
        print(f"[self-tune] proposed weights (REVIEW ONLY) -> {PROPOSED_PATH}")
    except Exception as e:
        warn(f"could not write proposed weights: {e}")


def run_tuning(ledger_path=LEDGER_PATH):
    """Full shadow loop. Returns a context dict, or None (fail-open)."""
    hist = load_history(ledger_path)
    if hist is None:
        return None
    runs = hist.get("picks") or []
    train_runs, hold_runs = split_runs(runs)
    train_rows = graded_rows_from_runs(train_runs)
    hold_rows = graded_rows_from_runs(hold_runs)
    n_all = len(train_rows) + len(hold_rows)
    if n_all < MIN_GRADED or len(hold_rows) < MIN_HOLDOUT or not train_rows:
        warn(f"insufficient graded data (total {n_all}, holdout {len(hold_rows)})")
        return None

    w0, info = learn_weights({"picks": train_runs})
    w0 = list(w0)  # copy -- never mutate the module-level HEURISTIC_W
    # Candidate (econ + insider) dims start at 0.0: they contribute nothing
    # to the baseline score, so base_err/base_acc below reproduce the LIVE
    # engine exactly. Candidates are screened separately in the ranking.
    w0 = w0 + [0.0] * (N_ECON + N_INSIDER)
    baseline_source = (f"trained on {info.get('samples')} train picks"
                       if info.get("trained") else
                       f"heuristic (warming up: {info.get('samples')} samples)")

    grad = finite_diff_gradient(train_rows, w0)
    prop = propose_weights(train_rows, w0, grad)
    w1 = prop["weights"]

    base_stats = hard_band_stats(hold_rows, w0)
    new_stats = hard_band_stats(hold_rows, w1)
    base_err, new_err = calibration_error(base_stats), calibration_error(new_stats)
    base_acc, new_acc = directional_accuracy(hold_rows, w0), directional_accuracy(hold_rows, w1)
    ranking, _rank_err, _rank_acc = significance_ranking(hold_rows, w1, train_rows)

    def ts_of(rows):
        ts = [r["ts"] for r in rows if r["ts"]]
        return (min(ts)[:10], max(ts)[:10]) if ts else ("?", "?")

    t_from, t_to = ts_of(train_rows)
    h_from, h_to = ts_of(hold_rows)
    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "ledger": str(ledger_path),
        "n_graded": n_all,
        "n_wins": sum(1 for r in train_rows + hold_rows if r["win"]),
        "n_train": len(train_rows), "n_holdout": len(hold_rows),
        "train_from": t_from, "train_to": t_to,
        "hold_from": h_from, "hold_to": h_to,
        "baseline_source": baseline_source,
        "w0": w0, "w1": w1,
        "lr": prop["lr"],
        "train_err_before": prop["err_before"],
        "train_err_after": prop["err_after"],
        "grad": grad,
        "base_stats": base_stats, "new_stats": new_stats,
        "base_err": base_err, "new_err": new_err,
        "base_acc": base_acc, "new_acc": new_acc,
        "ranking": ranking,
        "reference_bands": hist.get("calibration") or [],
    }


def main():
    ctx = run_tuning()
    if ctx is None:
        # Fail-open: write a minimal report so the run is on record.
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            REPORT_DIR.mkdir(parents=True, exist_ok=True)
            (REPORT_DIR / f"self-tune-{date}.md").write_text(
                f"# Self-tune calibration report -- {date}\n\n"
                "**SHADOW MODE.** Run aborted fail-open: ledger missing, "
                "unparseable, or insufficient graded data. No changes made; "
                "no weights proposed.\n")
        except Exception as e:
            warn(f"could not write fail-open report: {e}")
        return 0
    write_outputs(ctx)
    print(f"[self-tune] holdout calibration error: {ctx['base_err']:.2f}pp "
          f"-> {ctx['new_err']:.2f}pp "
          f"({ctx['new_err']-ctx['base_err']:+.2f}pp), "
          f"accuracy {100*ctx['base_acc']:.1f}% -> {100*ctx['new_acc']:.1f}%")
    top = ctx["ranking"][0]
    print(f"[self-tune] most significant variable: {top['feature']} "
          f"(d_calib {top['delta_calib_pp']:+.3f}pp)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
