"""Tests for variable_tests.py — the per-variable evaluation harness.

Unit tests use synthetic data (fast, hermetic). Two integration tests
run the harness against the real ledgers (read-only) and verify the
report is produced and the ledgers are untouched.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import variable_tests as vt

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pick(ret, win, feats, direction_regime='NEUTRAL', asset='TEST'):
    return {'asset': asset, 'ret': float(ret), 'win': bool(win),
            'features': [float(x) for x in feats],
            'regime': direction_regime, 'direction': 1, 'cls': 'crypto',
            'run_date': None, 'score_stored': None}


def _run(date, picks_long, picks_short):
    return {'date': date, 'scored': True,
            'longs': picks_long, 'shorts': picks_short}


def _feats(v0=0.0):
    f = [0.0] * 22
    f[0] = v0
    return f


# ------------------------------------------------------------------ units

def test_tercile_split():
    values = [1, 2, 3, 4, 5, 6]
    wins = [False, False, False, True, True, True]
    top, top_n, bot, bot_n, spread = vt.tercile_stats(values, wins)
    assert (top, top_n, bot, bot_n, spread) == (100.0, 2, 0.0, 2, 100.0)


def test_win_rate_and_expectancy():
    assert vt.win_rate_pct(3, 1) == 75.0
    assert vt.win_rate_pct(0, 0) is None
    assert abs(vt.expectancy_usd([4.2, -2.1, 1.0]) - (4.2 - 2.1 + 1.0) / 3 * 10) < 1e-9
    assert vt.expectancy_usd([]) is None
    assert vt.sem_usd([1.0]) is None
    assert vt.sem_usd([1.0, 3.0]) is not None


def test_walk_forward_split_orders_by_date():
    runs = [_run(f'2026-10-{10 + i:02d}T00:00:00+00:00', [], []) for i in range(10)]
    runs[3]['scored'] = False  # unscored runs are excluded from both sides
    train, test = vt.walk_forward_split(runs, train_frac=0.7)
    assert len(train) == 6 and len(test) == 3
    assert all(t['date'] < s['date'] for t in train for s in test)
    assert train[0]['date'] < train[-1]['date']


def test_graded_picks_skips_unscored_and_missing():
    good = {'asset': 'A', 'features': _feats(1.0), 'return_pct': 2.0,
            'win': True, 'regime': 'NEUTRAL'}
    no_ret = {'asset': 'B', 'features': _feats(1.0), 'return_pct': None,
              'win': False, 'regime': 'NEUTRAL'}
    excluded = {'asset': 'C', 'features': _feats(1.0), 'return_pct': -1.0,
                'win': False, 'regime': 'NEUTRAL', 'excluded': True}
    runs = [_run('2026-10-19T00:00:00+00:00', [good, no_ret, excluded], [])]
    picks = vt.graded_picks(runs)
    assert len(picks) == 1 and picks[0]['asset'] == 'A' and picks[0]['direction'] == 1
    runs2 = [_run('2026-10-19T00:00:00+00:00', [], [dict(good, asset='S')])]
    assert vt.graded_picks(runs2)[0]['direction'] == -1


def test_centroids_match_geometric_edge():
    import nexintel
    wins = [{'features': [1.0] + [0.0] * 21, 'return_pct': 2.0, 'win': True}
            for _ in range(4)]
    losses = [{'features': [-1.0] + [0.0] * 21, 'return_pct': -2.0, 'win': False}
              for _ in range(4)]
    run = {'date': '2026-10-19T00:00:00+00:00', 'scored': True,
           'longs': wins, 'shorts': losses}
    picks = [{'features': p['features'], 'win': p['win'], 'ret': p['return_pct']}
            for p in wins + losses]
    win_c, loss_c = vt.centroids(picks)
    hist = {'picks': [run]}
    for feats in ([1.0] + [0.0] * 21, [0.5] + [0.0] * 21, [-1.0] + [0.0] * 21):
        mine = vt.geo_edge_for(feats, win_c, loss_c)
        theirs, _ = nexintel.geometric_edge(feats, hist)
        assert abs(mine - theirs) < 1e-9


def test_rescore_zeroing_changes_score_by_feature_contribution():
    weights = [1.0] + [0.0] * 21
    feats = _feats(10.0)
    s_full, d_full = vt.rescore(feats, weights, None, None, 'NEUTRAL')
    s_zero, d_zero = vt.rescore(feats, weights, None, None, 'NEUTRAL', zero_idx=0)
    assert abs((s_full - s_zero) - 10.0) < 1e-9
    assert d_full == 1 and d_zero == -1  # zero score tilts to short by rule


def test_ablation_baseline_is_recomputed_direction_not_published():
    """When the recomputed direction disagrees with the published one,
    the baseline must be the mirror outcome — otherwise the delta
    mixes the feature's effect with weight drift."""
    weights = [1.0] + [0.0] * 21
    # stored direction +1, but the recomputed score is -10 (feature 0
    # is -10) so the recomputed model would have gone SHORT.
    p = _pick(ret=5.0, win=True, feats=_feats(-10.0))
    out = vt.ablate_feature([p], weights, None, None, 0)
    # baseline = mirror: the recomputed SHORT loses the 5% the LONG won.
    assert out['orig_win_pct'] == 0.0
    assert out['orig_exp_usd'] == -50.0
    # zeroing feature 0 leaves raw 0 -> SHORT still, no flip, delta 0.
    assert out['flips'] == 0
    assert out['delta_exp_usd'] == 0.0
    assert out['delta_win_pp'] == 0.0


def test_ablation_flips_direction_and_negates_return():
    weights = [1.0] + [0.0] * 21
    p = _pick(ret=5.0, win=True, feats=_feats(10.0))
    out = vt.ablate_feature([p], weights, None, None, 0)
    assert out['flips'] == 1
    assert out['orig_win_pct'] == 100.0 and out['abl_win_pct'] == 0.0
    assert out['delta_win_pp'] == 100.0
    assert out['orig_exp_usd'] == 50.0 and out['abl_exp_usd'] == -50.0
    assert out['delta_exp_usd'] == 100.0


def test_ablation_no_flip_when_feature_irrelevant():
    weights = [1.0] + [0.0] * 21
    p = _pick(ret=5.0, win=True, feats=_feats(10.0))
    out = vt.ablate_feature([p], weights, None, None, 5)  # feature 5 is zero
    assert out['flips'] == 0 and out['delta_exp_usd'] == 0.0


def test_justified_weights():
    assert vt.justified_weight(20.0, 20.0, 2.0) == 2.0
    assert vt.justified_weight(10.0, 20.0, 2.0) == 1.0
    assert vt.justified_weight(-5.0, 20.0, 2.0) == 0.0
    assert vt.justified_weight(0.0, 20.0, 2.0) == 0.0
    assert vt.justified_weight(5.0, 0.0, 2.0) == 0.0
    assert vt.justified_weight(5.0, None, 2.0) == 0.0


def test_verdicts():
    assert vt.verdict_feature(15.0, 2.0, 3.0) == 'EARNS WEIGHT'
    assert vt.verdict_feature(0.0, 0.0, 0.0) == 'DEAD WEIGHT'
    assert vt.verdict_feature(-15.0, -3.0, 0.0) == 'HURTS — CUT'
    assert vt.verdict_feature(15.0, 2.0, -6.0) == 'EARNS WEIGHT · INVERTED'
    assert vt.verdict_feature(0.0, 0.0, -5.0) == 'DEAD WEIGHT · INVERTED'
    # dollars are the bar: a feature earns weight when removing it
    # costs $10+/1k even if the win rate moves the other way.
    assert vt.verdict_feature(12.19, -1.1, -13.5) == 'EARNS WEIGHT · INVERTED'


def test_book_aggregation_merges_record_and_cumulative():
    record = [
        {'kind': 'mover', 'direction': 'LONG', 'band': 'na', 'regime': 'risk_off',
         'status': 'won', 'paper_pnl': 28.0},
        {'kind': 'mover', 'direction': 'LONG', 'band': 'na', 'regime': 'risk_off',
         'status': 'lost', 'paper_pnl': -35.0},
        {'kind': 'mover', 'direction': 'LONG', 'band': 'na', 'regime': 'risk_off',
         'status': 'open', 'paper_pnl': None},
        {'kind': 'signal', 'direction': 'SHORT', 'band': '60s', 'regime': 'neutral',
         'status': 'expired', 'paper_pnl': 5.0},
    ]
    cumulative = {'mover|LONG|na|risk_off': {'graded': 10, 'wins': 3, 'losses': 5,
                                             'expired': 2, 'never_triggered': 0,
                                             'paper_pnl_total': -100.0}}
    cells, per_kind, overall = vt.aggregate_book(record, cumulative, [])
    mover = per_kind['mover']
    assert mover['graded'] == 12 and mover['wins'] == 4 and mover['losses'] == 6
    assert mover['expired'] == 2 and mover['open'] == 1
    assert mover['paper_pnl_total'] == round(28.0 - 35.0 - 100.0, 2)
    assert mover['win_rate'] == 40.0
    assert overall['graded'] == 13  # +1 expired signal
    assert overall['win_rate'] == round(4 / 10 * 100, 1)


def test_cycle_dedup_counts_distinct_outcomes():
    rec = []
    for var in ('halving', 'month', 'fng', 'dow', 'dd'):
        rec.append({'id': f'cycle:BTC:1000:24h:{var}', 'asset': 'BTC',
                    'ts_open': '2026-11-01T00:00:00+00:00', 'horizon': '24h',
                    'variable': var, 'status': 'won', 'paper_pnl': 28.0})
        rec.append({'id': f'cycle:BTC:2000:24h:{var}', 'asset': 'BTC',
                    'ts_open': '2026-11-02T00:00:00+00:00', 'horizon': '24h',
                    'variable': var, 'status': 'lost', 'paper_pnl': -35.0})
    out = vt.aggregate_cycle(rec)
    assert out['by_variable']['halving']['graded'] == 2
    assert out['deduped_n_outcomes'] == 2
    assert out['deduped']['wins'] == 1 and out['deduped']['losses'] == 1
    assert out['label_agreement_pct'] == 100.0


def test_wilson_lower_95_known_values():
    # 2 wins / 3 graded: the intraday scoreboard test's 20.8% value.
    assert abs(vt.wilson_lower_95(2, 3) * 100 - 20.8) < 0.5
    assert vt.wilson_lower_95(0, 10) == 0.0
    assert vt.wilson_lower_95(0, 0) is None
    # A perfect small sample still has a wide lower bound (display only).
    assert 0.0 < vt.wilson_lower_95(10, 10) < 1.0
    # More wins at the same rate can only raise the lower bound.
    assert vt.wilson_lower_95(60, 100) > vt.wilson_lower_95(6, 10)


def test_shadow_weights_overrides_only():
    base = [0.5] * 22
    sh = vt.shadow_weights(base)
    assert len(sh) == 22 and base == [0.5] * 22  # base never mutated
    for j, v in vt.SHADOW_OVERRIDES.items():
        assert sh[j] == v
    for j in range(22):
        if j not in vt.SHADOW_OVERRIDES:
            assert sh[j] == 0.5
    # The specified set, by name: two raises, five cuts to zero.
    assert vt.SHADOW_OVERRIDES[6] == 0.225  # acceleration
    assert vt.SHADOW_OVERRIDES[2] == 0.173  # psych_bias
    for j in (20, 1, 18, 9, 11):
        assert vt.SHADOW_OVERRIDES[j] == 0.0
    # No negative weights anywhere in the set (trend/ma are zeroed,
    # never flipped).
    assert all(v >= 0 for v in vt.SHADOW_OVERRIDES.values())


def test_grade_with_weights_matches_ablation_baseline():
    """grade_with_weights on the unablated weights must reproduce the
    ablation baseline (orig_win_pct / orig_exp_usd) exactly."""
    weights = [1.0] + [0.0] * 21
    picks = [_pick(ret=5.0, win=True, feats=_feats(10.0)),
             _pick(ret=-3.0, win=False, feats=_feats(-10.0))]
    g = vt.grade_with_weights(picks, weights, None, None)
    ab = vt.ablate_feature(picks, weights, None, None, 5)
    assert g['n'] == 2 and g['win_pct'] == ab['orig_win_pct']
    assert g['exp_usd'] == ab['orig_exp_usd']
    # Zeroing the only live feature flips both directions.
    gz = vt.grade_with_weights(picks, weights, None, None, zero_indices=[0])
    ab0 = vt.ablate_feature(picks, weights, None, None, 0)
    assert gz['win_pct'] == ab0['abl_win_pct']
    assert gz['exp_usd'] == ab0['abl_exp_usd']


def test_cycle_scoreboard_deduped_counts_distinct_trades():
    rec = []
    for var in ('halving', 'month', 'fng', 'dow', 'dd'):
        rec.append({'id': f'cycle:BTC:1000:24h:{var}', 'asset': 'BTC',
                    'ts_open': '2026-11-01T00:00:00+00:00', 'horizon': '24h',
                    'variable': var, 'direction': 'LONG',
                    'status': 'won', 'paper_pnl': 28.0,
                    'ts_closed': '2026-11-02T00:00:00+00:00'})
        rec.append({'id': f'cycle:BTC:2000:24h:{var}', 'asset': 'BTC',
                    'ts_open': '2026-11-02T00:00:00+00:00', 'horizon': '24h',
                    'variable': var, 'direction': 'LONG',
                    'status': 'lost', 'paper_pnl': -35.0,
                    'ts_closed': '2026-11-03T00:00:00+00:00'})
    out = vt.cycle_scoreboard_deduped(rec)
    assert out['deduped_n_outcomes'] == 2
    assert out['inflated_n_graded'] == 10
    assert out['inflation_factor'] == 5.0
    assert out['label_agreement_pct'] == 100.0
    assert out['label_disagreements'] == 0
    assert out['cumulative_excluded'] is None
    ov = out['overall']
    assert ov['graded'] == 2 and ov['wins'] == 1 and ov['losses'] == 1
    assert ov['win_rate'] == 50.0
    assert ov['expectancy_per_1000'] == -3.5  # (28 - 35) / 2
    assert len(out['rows']) == 1
    assert out['rows'][0]['horizon'] == '24h'
    assert out['rows'][0]['direction'] == 'LONG'


def test_cycle_scoreboard_deduped_excludes_inflated_cumulative():
    rec = [{'id': 'cycle:BTC:1000:24h:halving', 'asset': 'BTC',
            'ts_open': '2026-11-01T00:00:00+00:00', 'horizon': '24h',
            'variable': 'halving', 'direction': 'LONG',
            'status': 'won', 'paper_pnl': 28.0,
            'ts_closed': '2026-11-02T00:00:00+00:00'}]
    cum = {'halving|H5|LONG|24h': {'graded': 100, 'wins': 50,
                                   'losses': 50, 'expired': 0,
                                   'paper_pnl_total': 0.0}}
    out = vt.cycle_scoreboard_deduped(rec, cum)
    assert out['overall']['graded'] == 1  # cumulative NOT folded in
    assert out['cumulative_excluded'] is not None
    assert out['cumulative_excluded']['cells'] == 1


def test_dedupe_cycle_entries_flags_disagreement():
    rec = [
        {'asset': 'BTC', 'ts_open': '2026-11-01T00:00:00+00:00',
         'horizon': '24h', 'variable': 'halving', 'status': 'won',
         'direction': 'LONG'},
        {'asset': 'BTC', 'ts_open': '2026-11-01T00:00:00+00:00',
         'horizon': '24h', 'variable': 'month', 'status': 'lost',
         'direction': 'LONG'},
    ]
    entries, n, agreement, disagreements = vt.dedupe_cycle_entries(rec)
    assert n == 1 and len(entries) == 1
    assert disagreements == 1 and agreement == 50.0


def test_fifty_rule_scale_and_signed_weight():
    # Largest distance 0.40 maps to the 0.225 cap.
    scale = vt.fifty_rule_scale([0.10, 0.40, 0.05])
    assert abs(scale - 0.225 / 0.40) < 1e-9
    assert vt.signed_weight_50(0.90, scale) == 0.225
    assert vt.signed_weight_50(0.10, scale) == -0.225
    assert vt.signed_weight_50(0.50, scale) == 0.0
    assert vt.signed_weight_50(0.60, scale) == round(0.10 * scale, 3)
    assert vt.fifty_rule_scale([]) == 0.0
    assert vt.fifty_rule_scale([0.0, 0.0]) == 0.0


def test_invert_entry_fixed_level_and_horizon_close():
    # Target hit: won +28 mirrors to lost -35 (not -28).
    pnl, win, method = vt.invert_entry({'status': 'won', 'paper_pnl': 28.0})
    assert (pnl, win, method) == (-35.0, False, 'fixed_level')
    # Stop hit: lost -35 mirrors to won +28.
    pnl, win, method = vt.invert_entry({'status': 'lost', 'paper_pnl': -35.0})
    assert (pnl, win, method) == (28.0, True, 'fixed_level')
    # Horizon close: drift flips sign exactly.
    pnl, win, method = vt.invert_entry({'status': 'won', 'paper_pnl': 9.7,
                                        'cycle_expired': True})
    assert (pnl, win, method) == (-9.7, False, 'horizon_close')
    # Expired-status entry: win iff the mirrored drift covers costs.
    pnl, win, method = vt.invert_entry({'status': 'expired',
                                        'paper_pnl': 5.0, 'result_pct': 1.8})
    assert (pnl, win, method) == (-5.0, False, 'horizon_close')
    pnl, win, method = vt.invert_entry({'status': 'expired',
                                        'paper_pnl': -9.0, 'result_pct': -2.0})
    assert (pnl, win, method) == (9.0, True, 'horizon_close')
    # Odd payoff (breakeven trailing win): fallback mirror, counted.
    pnl, win, method = vt.invert_entry({'status': 'won', 'paper_pnl': -0.0})
    assert (pnl, win, method) == (0.0, False, 'fallback')


def test_invert_entries_totals():
    entries = [
        {'status': 'won', 'paper_pnl': 28.0},
        {'status': 'lost', 'paper_pnl': -35.0},
        {'status': 'won', 'paper_pnl': 9.7, 'cycle_expired': True},
        {'status': 'expired', 'paper_pnl': 5.0, 'result_pct': 1.8},
        {'status': 'open', 'paper_pnl': None},  # open entries never count
    ]
    t = vt.invert_entries(entries)
    assert t['n_graded'] == 4
    assert t['wins'] == 1 and t['losses'] == 2 and t['expired'] == 1
    assert t['win_rate'] == round(1 / 3 * 100, 1)
    # -35 + 28 - 9.7 - 5.0 = -21.7 over 3 judged -> -7.23 $/1k.
    assert t['paper_pnl_total'] == -21.7
    assert t['expectancy_usd'] == round(-21.7 / 3, 2)
    assert t['methods'] == {'fixed_level': 2, 'horizon_close': 2,
                            'fallback': 0}
    runs = [
        {'date': '2026-11-01T00:00:00+00:00', 'regime': 'RISK_OFF'},
        {'date': '2026-11-03T00:00:00+00:00', 'regime': 'RISK_ON'},
    ]
    record = [
        {'ts_open': '2026-11-02T00:00:00+00:00'},
        {'ts_open': '2026-11-04T00:00:00+00:00', 'regime': 'neutral'},
        {'ts_open': '2026-10-01T00:00:00+00:00'},
    ]
    n = vt.backfill_regimes(record, runs)
    assert n == 1
    assert record[0]['regime'] == 'risk_off'
    assert record[1]['regime'] == 'neutral'  # untouched
    assert 'regime' not in record[2]  # older than every run: stays untagged


def test_report_renders_tables():
    results = {
        'generated_utc': '2026-11-11T00:00:00+00:00',
        'validation': {'train_runs': 1, 'test_runs': 1, 'train_picks': 10,
                       'test_picks': 5, 'train_first': 'a', 'train_last': 'b',
                       'test_first': 'c', 'test_last': 'd',
                       'weights_train_note': 'n/a', 'weights_full_note': 'n/a',
                       'direction_agreement_pct': 90.0,
                       'direction_agreement_full_pct': 80.0,
                       'score_mae': 1.0, 'mean_abs_stored_score': 1.5,
                       'published_test_win_pct': 50.0,
                       'published_test_exp_usd': -2.0},
        'feature_rows': [{'index': 0, 'name': 'momentum', 'weight_learned': 1.0,
                          'weight_heuristic': 1.0, 'weight_proposed': 0.5,
                          'weight_baseline': 0.4,
                          'standalone': {'top_win_pct': 50.0, 'top_n': 10,
                                         'bot_win_pct': 40.0, 'bot_n': 10,
                                         'spread_pp': 10.0, 'top_exp_usd': 5.0},
                          'ablation': {'delta_win_pp': 1.0, 'delta_exp_usd': 12.0,
                                       'delta_exp_se_usd': 2.0, 'flips': 3},
                          'justified_weight': 1.0, 'verdict': 'EARNS WEIGHT'}],
        'track_rows': [{'name': 'x', 'n': 30, 'trig_pct': 80.0, 'win_pct': 60.0,
                        'exp_usd': 5.0,
                        'ablation': {'delta_win_pp': 1.0, 'delta_exp_usd': 2.0},
                        'weight': 'none', 'verdict': 'WORKING'}],
        'shadow': {
            'base_weights': [0.0] * 22, 'shadow_weights': [0.0] * 22,
            'overrides': {'acceleration': 0.225},
            'base_grade': {'n': 5, 'win_pct': 50.0, 'exp_usd': -2.0,
                           'exp_se_usd': 1.0},
            'shadow_grade': {'n': 5, 'win_pct': 60.0, 'exp_usd': 3.0,
                             'exp_se_usd': 1.0},
            'full_history_base_grade': {'n': 5, 'win_pct': 50.0,
                                        'exp_usd': -2.0},
            'shadow_on_full_history_grade': {'n': 5, 'win_pct': 60.0,
                                             'exp_usd': 3.0},
            'joint_dead_indices': [1], 'joint_dead_names': ['reversion'],
            'joint_dead_grade': {'n': 5, 'win_pct': 50.0, 'exp_usd': -1.0,
                                 'exp_se_usd': 1.0},
            'diagnostics': {
                'raises_only_grade': {'win_pct': 55.0, 'exp_usd': 0.0},
                'cuts_only_grade': {'win_pct': 52.0, 'exp_usd': 1.0}},
            'note': 'test note'},
        'cycle_deduped_scoreboard': {
            'rows': [{'horizon': '24h', 'direction': 'LONG', 'graded': 2,
                      'wins': 1, 'losses': 1, 'win_rate': 50.0,
                      'wilson_lower_95': 20.8, 'expectancy_per_1000': -3.5,
                      'max_drawdown_pct': 3.5}],
            'overall': {'graded': 2, 'wins': 1, 'losses': 1,
                        'win_rate': 50.0, 'wilson_lower_95': 20.8,
                        'expectancy_per_1000': -3.5,
                        'max_drawdown_pct': 3.5},
            'deduped_n_outcomes': 2, 'deduped_n_graded': 2,
            'inflated_n_graded': 10, 'inflation_factor': 5.0,
            'label_agreement_pct': 100.0, 'label_disagreements': 0,
            'cumulative_excluded': None},
        'fifty_rule': {
            'rule': 'signed weight = (win_rate - 0.50) x SCALE',
            'scale': 0.5, 'max_distance_from_50': 0.45,
            'cap_weight': 0.225,
            'feature_slice': 'train slice',
            'track_slice': 'full ledgers',
            'weights': [0.0] * 22,
            'grade': {'n': 5, 'win_pct': 55.0, 'exp_usd': -1.0,
                      'exp_se_usd': 1.0},
            'geo_only_grade': {'win_pct': 56.0, 'exp_usd': 2.0},
            'feature_rows': [{'index': 6, 'name': 'acceleration',
                              'train_top_win_pct': 51.6,
                              'test_top_win_pct': 34.8,
                              'signed_weight': 0.009,
                              'weight_train_fit': -0.81,
                              'weight_ablation_set': 0.225,
                              'rule_set_delta_exp_usd': -0.36}],
            'tracks': [{'name': 'cycle — DEDUPED (distinct outcomes)',
                        'win_pct': 21.2, 'n': 297, 'signed_weight': -0.167,
                        'exp_usd': -19.12,
                        'inverted': {'win_rate': 78.8,
                                     'expectancy_usd': 12.0},
                        'rule_verdict': 'HELPS — test'}]},
        'untestable': [('balance / coil score', 'ranges', 'no data', '0.0')],
    }
    md = vt.render_report(results)
    assert '## Table A' in md and '## Table B' in md
    assert 'momentum' in md and 'EARNS WEIGHT' in md
    assert 'balance / coil score' in md
    assert 'No 95% significance gate' in md
    assert 'Corrected shadow weight set' in md
    assert 'Cycle — deduped grading' in md
    assert 'The 50% rule' in md
    assert 'acceleration' in md


def test_module_does_not_import_intraday():
    src = open(os.path.join(REPO, 'Scripts', 'variable_tests.py')).read()
    assert 'import intraday' not in src
    assert 'from intraday' not in src


# ------------------------------------------------------------------ integration

def _sha(path):
    return hashlib.sha256(open(path, 'rb').read()).hexdigest()


def test_run_on_real_repo(tmp_path):
    before = {p: _sha(os.path.join(REPO, 'data', p))
              for p in ('history.json', 'intraday.json', 'proposed_weights.json')}
    results, md = vt.run(REPO)
    assert len(results['feature_rows']) == 22
    assert {r['name'] for r in results['feature_rows']} == set(vt.FEATURE_NAMES)
    assert results['validation']['test_picks'] > 100
    assert results['validation']['direction_agreement_pct'] is not None
    assert 0 <= results['validation']['direction_agreement_pct'] <= 100
    assert results['validation']['direction_agreement_full_pct'] is not None
    assert results['validation']['published_test_win_pct'] is not None
    assert len(results['track_rows']) > 10
    assert len(results['untestable']) == 10
    # every track row carries a trigger rate (except equity, which has
    # no graded/expired split by construction)
    for t in results['track_rows']:
        if t['name'].startswith('stocks — equity'):
            assert t['trig_pct'] is None
        else:
            assert t['trig_pct'] is not None
            assert 0 <= t['trig_pct'] <= 100
    # the setup row must show the trigger rate, not hide it in n
    setup = next(t for t in results['track_rows'] if t['name'].startswith('setup ('))
    assert setup['n'] > 100  # graded incl. expired, not just triggered
    assert setup['trig_pct'] is not None and setup['trig_pct'] < 50
    # --- corrected shadow set (same walk-forward split, same n)
    sh = results['shadow']
    assert sh['shadow_grade']['n'] == results['validation']['test_picks']
    assert sh['base_grade']['n'] == results['validation']['test_picks']
    assert sh['joint_dead_grade']['n'] == results['validation']['test_picks']
    assert len(sh['joint_dead_indices']) == 21  # all but acceleration
    assert 6 not in sh['joint_dead_indices']  # acceleration earns weight
    assert sh['shadow_weights'][6] == 0.225
    assert sh['shadow_weights'][2] == 0.173
    for j in (20, 1, 18, 9, 11):
        assert sh['shadow_weights'][j] == 0.0
    # --- deduped cycle scoreboard (distinct trades, not 5x labels)
    cyc = results['cycle_deduped_scoreboard']
    assert cyc['deduped_n_outcomes'] > 100
    assert cyc['inflation_factor'] == 5.0
    assert cyc['label_agreement_pct'] == 100.0
    assert cyc['overall']['graded'] == results['cycle']['deduped']['graded']
    assert cyc['overall']['win_rate'] == results['cycle']['deduped']['win_rate']
    assert cyc['overall']['expectancy_per_1000'] == \
        results['cycle']['deduped']['expectancy_usd']
    # --- Nic's 50% rule (signed weights from graded proof)
    fr = results['fifty_rule']
    assert len(fr['weights']) == 22
    assert fr['grade']['n'] == results['validation']['test_picks']
    assert fr['scale'] > 0
    assert max(abs(w) for w in fr['weights']) <= 0.225 + 1e-9
    assert len(fr['feature_rows']) == 22
    acc = next(x for x in fr['feature_rows'] if x['name'] == 'acceleration')
    # Train slice reads acceleration slightly ABOVE 50% (51.6%), so the
    # rule gives it a tiny POSITIVE weight — the negative read (34.8%)
    # is the test slice, which must never set a weight (look-ahead).
    assert acc['train_top_win_pct'] > 50
    assert acc['test_top_win_pct'] < 50
    assert acc['signed_weight'] > 0 and acc['signed_weight'] < 0.05
    # The largest distance from 50% is CMI CLEAN (11.3%): it maps to
    # the -0.225 cap, and its mirror pays.
    clean = next(t for t in fr['tracks'] if t['name'] == 'CMI band CLEAN')
    assert clean['signed_weight'] == -0.225
    assert clean['inverted']['expectancy_usd'] > 0
    assert clean['rule_verdict'].startswith('HELPS')
    cyc_tr = next(t for t in fr['tracks']
                  if t['name'] == 'cycle — DEDUPED (distinct outcomes)')
    assert cyc_tr['signed_weight'] < 0
    assert cyc_tr['inverted']['expectancy_usd'] > 0
    setup_tr = next(t for t in fr['tracks']
                    if t['name'].startswith('setup LONG'))
    assert setup_tr['signed_weight'] > 0
    # No mirror verdict for setups today: the visible 100-entry window
    # holds no graded setup entries, so the inversion is too thin
    # (n < 20) and the verdict is honestly UNPROVEN, not HURTS.
    assert setup_tr['rule_verdict'].startswith('UNPROVEN')
    pat_tr = next(t for t in fr['tracks']
                  if t['name'].startswith('pattern LONG'))
    assert pat_tr['rule_verdict'].startswith('UNPROVEN')
    out = tmp_path / 'variable-tests-2026-11-11.md'
    out.write_text(md)
    text = out.read_text()
    assert '## Table A' in text and '## Table B' in text
    assert 'Trig%' in text
    assert 'trend_align' in text
    after = {p: _sha(os.path.join(REPO, 'data', p))
             for p in ('history.json', 'intraday.json', 'proposed_weights.json')}
    assert before == after  # the harness is read-only on the ledgers


# ------------------------------------------------------------------ residual hunt + F-form

def _residual_picks():
    w = [0.0] * 22
    w[0] = 1.0
    return w


def test_residual_cases_calls_lost_ranked_by_abs_score():
    w = _residual_picks()
    picks = [
        _pick(-2.0, False, _feats(3.0), asset='BIG'),   # score +3, lost
        _pick(-4.0, False, _feats(1.0), asset='SMALL'),  # score +1, lost
        _pick(5.0, True, _feats(2.0), asset='WINNER'),   # won — not a miss
    ]
    res = vt.residual_cases(picks, w, None, None)
    assert [c['asset'] for c in res['calls_lost']] == ['BIG', 'SMALL']
    assert res['calls_lost'][0]['f_direction'] == 'LONG'
    assert res['calls_lost'][0]['dollar_miss_per_1000'] == 20.0
    assert res['calls_lost'][1]['dollar_miss_per_1000'] == 40.0
    assert res['fades_won'] == []


def test_residual_cases_fades_won():
    w = _residual_picks()
    picks = [
        # published LONG won, but F scored it SHORT (negative score) — a fade that won
        _pick(5.0, True, _feats(-2.0), asset='FADED'),
    ]
    res = vt.residual_cases(picks, w, None, None)
    assert len(res['fades_won']) == 1
    c = res['fades_won'][0]
    assert c['asset'] == 'FADED' and c['f_direction'] == 'SHORT'
    assert c['published_direction'] == 'LONG' and c['outcome'] == 'won'
    assert c['dollar_miss_per_1000'] == 50.0  # the win F talked us out of
    assert res['calls_lost'] == []


def test_residual_cases_empty_when_nothing_wrong():
    w = _residual_picks()
    picks = [_pick(5.0, True, _feats(2.0), asset='OK')]
    res = vt.residual_cases(picks, w, None, None)
    assert res == {'calls_lost': [], 'fades_won': []}


def test_f_form_mapping_is_proposed_and_covers_all_terms():
    results = {
        'per_kind': {'setup': {'win_rate': 85.4, 'wins': 35, 'losses': 6,
                               'expectancy_usd': 11.3}},
        'fifty_rule': {'grade': {'exp_usd': -3.58},
                       'feature_rows': [
                           {'name': 'psych_bias', 'signed_weight': 0.01,
                            'rule_set_delta_exp_usd': 5.89},
                           {'name': 'acceleration', 'signed_weight': 0.009,
                            'rule_set_delta_exp_usd': -0.36}]},
        'shadow': {'base_grade': {'exp_usd': 12.27}},
    }
    m = vt.f_form_mapping(results)
    assert m['mapping'].startswith('PROPOSED')
    assert [t['term'] for t in m['terms']] == ['a', 'b', 'c', 'd', 'f-e', 'unknown']
    unknown = next(t for t in m['terms'] if t['term'] == 'unknown')
    assert 'no value invented' in unknown['graded'].lower()
    assert 'OPEN DOOR' in unknown['graded']
    assert 'Evidence that would count' in unknown['graded']
    d = next(t for t in m['terms'] if t['term'] == 'd')
    assert '85.4' in d['proxy'] and 'order book depth' in d['graded']


def test_distance_to_95_setup_is_closest():
    results = {
        'per_kind': {'setup': {'win_rate': 85.4, 'graded': 338}},
        'fifty_rule': {'tracks': [
            {'name': 'cycle — DEDUPED', 'win_pct': 21.2, 'n': 297},
            {'name': 'mover LONG neutral', 'win_pct': 34.1, 'n': 41}]},
        'feature_rows': [
            {'name': 'momentum',
             'standalone': {'top_win_pct': 52.5, 'top_n': 155}}],
    }
    dist = vt.distance_to_95(results)
    assert dist[0]['name'].startswith('accumulation setups')
    assert dist[0]['distance_to_95'] == 9.6
    assert all(d['distance_to_95'] > 0 for d in dist)
    assert all(d['n'] >= 20 for d in dist)


def test_run_residual_and_f_form_on_real_repo(tmp_path):
    results, md = vt.run(REPO)
    res = results['residual']
    assert len(res['calls_lost']) == 5 and len(res['fades_won']) == 5
    for c in res['calls_lost'] + res['fades_won']:
        assert c['asset'] and c['run_date'] and c['f_score'] != 0
        # a zero-return outcome misses $0 — legitimate, not an error
        assert c['dollar_miss_per_1000'] >= 0
    assert 'PROPOSED' in results['f_form']['mapping']
    assert len(results['f_form']['terms']) == 6
    dist = results['distance_to_95']
    # Live-record ordering drifts as picks are graded — the list is sorted
    # by win% descending, so assert that shape, not which cell is first
    # (accumulation setups led at 85.4% / 9.6 on the day pinned).
    assert dist
    assert all(dist[i]['win_pct'] >= dist[i + 1]['win_pct']
               for i in range(len(dist) - 1))
    assert any(d['name'].startswith('accumulation setups') for d in dist)
    assert all(0 <= d['win_pct'] <= 100
               and d['distance_to_95'] == round(95.0 - d['win_pct'], 1)
               for d in dist)
    assert '## Hunting the residual (unknown)' in md
    assert '## F — the formula (PROPOSED mapping)' in md
    assert '## Distance to 95% (the target Nic set)' in md
