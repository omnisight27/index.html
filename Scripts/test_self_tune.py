"""Tests for the shadow-mode self-tuning loop (Scripts/self_tune.py).

Covers: Wilson interval math, calibration error math, finite-difference
gradient direction on synthetic data, holdout split ordering, paper-grade
band accounting, fail-open behavior, proposal JSON schema, and the
per-variable significance ranking.
"""

import json
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import self_tune as st


def _row(feats, win, ret=None, cls="crypto", ts="2026-10-20T00:00:00+00:00"):
    return {"ts": ts, "features": list(feats) + [0.0] * (st.N_ALL - len(feats)),
            "win": win, "return_pct": (1.0 if win else -1.0) if ret is None else ret,
            "cls": cls}


# ---------------------------------------------------------- Wilson interval

def test_wilson_known_value():
    lo, hi = st.wilson_interval(50, 100)
    assert lo == pytest.approx(0.4038, abs=0.005)
    assert hi == pytest.approx(0.5962, abs=0.005)


def test_wilson_edges():
    lo, hi = st.wilson_interval(0, 10)
    assert lo == 0.0
    assert hi == pytest.approx(0.278, abs=0.01)  # not (0,0): honest uncertainty
    lo, hi = st.wilson_interval(10, 10)
    assert hi == 1.0
    assert lo == pytest.approx(0.722, abs=0.01)
    assert st.wilson_interval(0, 0) == (0.0, 0.0)


def test_wilson_contains_p_and_shrinks_with_n():
    for wins, n in [(30, 100), (7, 20), (95, 100)]:
        lo, hi = st.wilson_interval(wins, n)
        p = wins / n
        assert lo <= p <= hi
    _, hi_small = st.wilson_interval(5, 10)
    lo_small, _ = st.wilson_interval(5, 10)
    lo_big, hi_big = st.wilson_interval(500, 1000)
    assert (hi_big - lo_big) < (hi_small - lo_small)  # more data -> tighter


# ---------------------------------------------------------- calibration math

def test_perfect_calibration_zero_error():
    # every pick: conf 65 in the 60s band... build via score instead:
    # score s.t. conf lands in band and win rate matches mean conf.
    rows = [_row([4.0], True) for _ in range(10)]   # conf 56, band 50-59... WR 100%
    stats = st.hard_band_stats(rows, [1.0] + [0.0] * (st.N_FEATURES - 1))
    assert stats["50-59"]["n"] == 10
    assert stats["50-59"]["win_rate"] == pytest.approx(1.0)
    # error is |56 - 100| = 44, not zero -- so construct the true zero case:
    rows2 = [_row([0.0], True) for _ in range(5)] + [_row([0.0], False) for _ in range(5)]
    stats2 = st.hard_band_stats(rows2, [0.0] * st.N_FEATURES)
    assert stats2["50-59"]["n"] == 10
    assert st.calibration_error(stats2) == pytest.approx(0.0)  # conf 50, WR 50%


def test_known_miscalibration_value():
    rows = [_row([10.0], False) for _ in range(10)]  # conf 65, band 60-69, WR 0%
    stats = st.hard_band_stats(rows, [1.0] + [0.0] * (st.N_FEATURES - 1))
    assert st.calibration_error(stats) == pytest.approx(65.0)


def test_band_assignment_edges():
    # band_of rounds first, matching the live pipeline (stored confidence is
    # round(conf), an integer, banded by CONF_BANDS integer ranges).
    assert st.band_of(50.0) == "50-59"
    assert st.band_of(59.4) == "50-59"   # rounds to 59
    assert st.band_of(59.9) == "60-69"   # rounds to 60
    assert st.band_of(60.0) == "60-69"
    assert st.band_of(79.4) == "70-79"   # rounds to 79
    assert st.band_of(79.99) == "80+"    # rounds to 80
    assert st.band_of(80.0) == "80+"
    assert st.band_of(97.0) == "80+"


# ---------------------------------------------------------- gradient / tuning

def test_gradient_direction_synthetic():
    # All winners with a positive feature: raising w0 raises confidence
    # toward the 100% win rate, so d(error)/d(w0) < 0.
    rows = [_row([3.0], True) for _ in range(20)]
    w = [0.1] + [0.0] * (st.N_FEATURES - 1)
    grad = st.finite_diff_gradient(rows, w)
    assert len(grad) == st.N_FEATURES
    assert all(math.isfinite(g) for g in grad)
    assert grad[0] < 0


def test_soft_error_smooth_in_weights():
    rows = [_row([3.0], True) for _ in range(10)] + [_row([1.0], False) for _ in range(10)]
    w = [0.5] + [0.0] * (st.N_FEATURES - 1)
    e0 = st.soft_band_error(rows, w)
    w2 = list(w); w2[0] += 1e-6
    e1 = st.soft_band_error(rows, w2)
    assert abs(e1 - e0) < 1e-3  # tiny nudge -> tiny change (smooth)
    assert math.isfinite(e0)


def test_propose_never_worsens_train_surrogate():
    rows = ([_row([3.0], True) for _ in range(15)] +
            [_row([0.5], False) for _ in range(15)])
    w = [0.4] + [0.0] * (st.N_FEATURES - 1)
    grad = st.finite_diff_gradient(rows, w)
    prop = st.propose_weights(rows, w, grad)
    assert prop["err_after"] <= prop["err_before"] + 1e-9
    assert len(prop["weights"]) == st.N_FEATURES
    # max +/-50% move per weight
    for b, p in zip(w, prop["weights"]):
        if b != 0:
            assert 0.5 <= p / b <= 1.5


def test_holdout_split_is_last_by_time():
    runs = []
    for d in range(10):
        runs.append({"date": f"2026-10-{10+d:02d}T00:00:00+00:00", "scored": True,
                     "longs": [{"features": [1.0] * 22, "win": True, "return_pct": 1.0,
                                "class": "crypto"}],
                     "shorts": []})
    train, hold = st.split_runs(runs, frac=0.2)
    assert len(train) + len(hold) == 10
    assert len(hold) >= 1
    assert hold[0]["date"] > train[-1]["date"]  # holdout is the newest slice


def test_paper_grade_band_counts_sum():
    rows = [_row([float(i % 5)], i % 2 == 0) for i in range(40)]
    stats = st.hard_band_stats(rows, [1.0] + [0.0] * (st.N_FEATURES - 1))
    assert sum(d["n"] for d in stats.values()) == 40


# ---------------------------------------------------------- significance ranking

def test_significance_ranking_orders_predictive_first():
    # f0 perfectly separates confidence from outcome; f1 is dead (weight 0).
    rows = [_row([20.0, 0.5], True) for _ in range(10)]
    w = [0.5, 0.0] + [0.0] * (st.N_ALL - 2)
    ranking, base_err, base_acc = st.significance_ranking(rows, w)
    assert len(ranking) == st.N_ALL
    # sorted by delta_calib_pp descending
    deltas = [r["delta_calib_pp"] for r in ranking]
    assert deltas == sorted(deltas, reverse=True)
    by_name = {r["feature"]: r for r in ranking}
    f0 = by_name[st.FEATURE_NAMES[0]]
    f1 = by_name[st.FEATURE_NAMES[1]]
    assert f0["delta_calib_pp"] > 0      # removing f0 hurts -> earns its place
    assert f1["delta_calib_pp"] == pytest.approx(0.0)  # dead weight
    assert ranking[0]["feature"] == st.FEATURE_NAMES[0]


def test_ablation_delta_fields_present():
    rows = [_row([2.0], True) for _ in range(6)] + [_row([2.0], False) for _ in range(6)]
    ranking, _, _ = st.significance_ranking(rows, [0.3] * st.N_ALL)
    for r in ranking:
        assert set(r) == {"feature", "index", "weight",
                          "delta_calib_pp", "delta_accuracy_pp", "note"}
        assert math.isfinite(r["delta_calib_pp"])


def test_candidate_screening_predictive_gets_positive_delta():
    # Candidate dim 22 (= first econ var) perfectly predicts return direction.
    rows = [_row([0.0] * st.N_FEATURES + [3.0], True, ret=5.0) for _ in range(8)]
    rows += [_row([0.0] * st.N_FEATURES + [-3.0], False, ret=-5.0) for _ in range(8)]
    w = [0.0] * st.N_ALL
    ranking, _, _ = st.significance_ranking(rows, w, train_rows=rows)
    by_name = {r["feature"]: r for r in ranking}
    cand = by_name[st.ECON_FEATURE_NAMES[0]]
    assert cand["note"] == "candidate"
    assert cand["delta_calib_pp"] > 0  # adding it helps -> earns candidacy
    assert cand["weight"] != 0.0  # univariate weight was fit


def test_candidate_no_data_labeled_awaiting():
    rows = [_row([1.0], True) for _ in range(6)] + [_row([1.0], False) for _ in range(6)]
    ranking, _, _ = st.significance_ranking(rows, [0.0] * st.N_ALL, train_rows=rows)
    cands = [r for r in ranking if r["note"] == "awaiting data"]
    assert len(cands) == st.N_ECON
    assert all(r["delta_calib_pp"] == 0.0 for r in cands)


def test_univariate_weight_zero_variance():
    rows = [_row([1.0], True) for _ in range(4)]
    assert st._univariate_weight(rows, 0) == 0.0
    assert st._univariate_weight(rows[:1], 0) == 0.0


def test_graded_rows_extend_with_econ_features():
    run = {"scored": True, "date": "2026-10-31T00:00:00+00:00",
           "longs": [{"features": [1.0] * st.N_FEATURES, "win": True,
                      "return_pct": 2.0, "class": "crypto",
                      "econ_features": [0.5] + [None] * (st.N_ECON - 1)}],
           "shorts": []}
    rows = st.graded_rows_from_runs([run])
    assert len(rows) == 1
    assert len(rows[0]["features"]) == st.N_ALL
    assert rows[0]["features"][st.N_FEATURES] == pytest.approx(0.5)
    assert rows[0]["features"][-1] == 0.0  # None padded to 0.0


# ---------------------------------------------------------- fail-open

def test_fail_open_missing_ledger(tmp_path):
    assert st.load_history(tmp_path / "nope.json") is None
    assert st.run_tuning(tmp_path / "nope.json") is None


def test_fail_open_malformed_ledger(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    assert st.load_history(p) is None


def test_fail_open_too_few_rows(tmp_path):
    p = tmp_path / "tiny.json"
    p.write_text(json.dumps({"picks": [
        {"date": "2026-10-20T00:00:00+00:00", "scored": True,
         "longs": [{"features": [1.0] * 22, "win": True, "return_pct": 1.0,
                    "class": "crypto"}],
         "shorts": []}]}))
    assert st.run_tuning(p) is None  # exits quietly, no exception


def test_excluded_picks_skipped():
    runs = [{"date": "2026-10-20T00:00:00+00:00", "scored": True,
             "longs": [{"features": [1.0] * 22, "win": True, "return_pct": 9.0,
                        "class": "crypto", "excluded": True}],
             "shorts": []}]
    assert st.graded_rows_from_runs(runs) == []


def test_raw_confidence_bounds():
    assert st.raw_confidence(0.0, "crypto") == 50.0
    assert st.raw_confidence(1e9, "crypto") == 100.0
    assert st.raw_confidence(-1e9, "equity") == 100.0
    assert 0.0 <= st.raw_confidence(3.3, "equity") <= 100.0


def test_display_names_disambiguate_duplicates():
    names = st._display_names()
    assert len(names) == st.N_ALL
    assert len(set(names)) == st.N_ALL  # no two rows share a label
    assert names[8] == "rsi (#8)" and names[13] == "rsi (#13)"
    assert names[7] == "zscore (#7)" and names[19] == "zscore (#19)"
    assert names[6] == "acceleration"  # unique names stay bare
    assert names[st.N_FEATURES] == st.ECON_FEATURE_NAMES[0]  # candidates appended
