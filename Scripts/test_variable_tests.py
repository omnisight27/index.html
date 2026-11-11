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


def test_backfill_regimes_tags_missing_only():
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
        'untestable': [('balance / coil score', 'ranges', 'no data', '0.0')],
    }
    md = vt.render_report(results)
    assert '## Table A' in md and '## Table B' in md
    assert 'momentum' in md and 'EARNS WEIGHT' in md
    assert 'balance / coil score' in md
    assert 'No 95% significance gate' in md


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
    out = tmp_path / 'variable-tests-2026-11-11.md'
    out.write_text(md)
    text = out.read_text()
    assert '## Table A' in text and '## Table B' in text
    assert 'Trig%' in text
    assert 'trend_align' in text
    after = {p: _sha(os.path.join(REPO, 'data', p))
             for p in ('history.json', 'intraday.json', 'proposed_weights.json')}
    assert before == after  # the harness is read-only on the ledgers
