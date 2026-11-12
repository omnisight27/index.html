"""Tests for shadow_actuator.py — the fast-correction actuator.

Unit tests use synthetic results (fast, hermetic). One integration
test runs the actuator against the real ledgers (read-only) and
verifies the dated proposal files are produced and the ledgers are
untouched.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shadow_actuator as sa

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _feature_row(name, index, delta, top_win, verdict, shadow_w=0.0):
    return {'name': name, 'index': index,
            'ablation': {'delta_exp_usd': delta},
            'standalone': {'top_win_pct': top_win},
            'verdict': verdict}


def _fake_results(tmp_path, cells=None, feature_rows=None,
                  cycle_overall=None, cmi_bands=None):
    repo = tmp_path / 'repo'
    (repo / 'data').mkdir(parents=True, exist_ok=True)
    (repo / 'data' / 'intraday.json').write_text(json.dumps(
        {'proof': {'shadow_record': [], 'stock_record': []}}))
    (repo / 'data' / 'history.json').write_text(json.dumps({'picks': []}))
    return {
        'repo': str(repo),
        'shadow': {'shadow_weights': [0.0] * 22,
                   'base_grade': {'n': 466, 'exp_usd': 12.27},
                   'shadow_grade': {'exp_usd': -7.24}},
        'cells': cells or {},
        'feature_rows': feature_rows or [],
        'cycle_deduped_scoreboard': {
            'overall': cycle_overall or {'graded': 0, 'wins': 0,
                                         'losses': 0, 'win_rate': None,
                                         'expectancy_per_1000': None},
            'rows': []},
        'cmi': {'by_band': cmi_bands or {}},
        'track_rows': [],
        'fifty_rule': {
            'rule': 'signed weight = (win_rate - 0.50) x SCALE',
            'scale': 0.5, 'max_distance_from_50': 0.45,
            'grade': {'exp_usd': -1.0, 'n': 466},
            'feature_rows': [],
            'tracks': []},
    }


# ------------------------------------------------------------------ factors

def test_track_factor_math():
    # Win leg only (positive expectancy): 34.1% keeps 34.1/50.
    assert sa.track_factor(34.1, 0.36) == (0.682, ['win 34.1% <= 35% line'])
    # Expectancy leg floors at zero: -$22.40/1k -> 0.
    f, reasons = sa.track_factor(22.7, -22.4)
    assert f == 0.0 and len(reasons) == 2
    # Win above the line, expectancy negative: expectancy leg sets it.
    assert sa.track_factor(43.5, -5.3)[0] == 0.735
    # Winning on both legs: no cut.
    assert sa.track_factor(60.0, 5.0) == (1.0, [])


def test_is_losing_track_boundaries():
    assert sa.is_losing_track(35.0, 5.0, 20) is True   # win line, at n=20
    assert sa.is_losing_track(35.1, 5.0, 20) is False
    assert sa.is_losing_track(60.0, -0.01, 20) is True  # expectancy leg
    assert sa.is_losing_track(60.0, 0.0, 20) is False
    assert sa.is_losing_track(10.0, -20.0, 19) is False  # too thin
    assert sa.is_losing_track(None, -5.0, 25) is True
    assert sa.is_losing_track(None, None, 25) is False


def test_feature_factor_earns_exempt():
    # Acceleration: standalone 34.8% (below the win line) but earns
    # +$12.19/1k — the earning verdict exempts it from down-weighting.
    row = _feature_row('acceleration', 6, 12.19, 34.8,
                       'EARNS WEIGHT · INVERTED')
    losing, factor, _reasons = sa.feature_factor(row)
    assert losing is False and factor == 1.0


def test_feature_factor_negative_delta():
    row = _feature_row('macd', 10, -0.88, 34.8, 'DEAD WEIGHT · INVERTED')
    losing, factor, reasons = sa.feature_factor(row)
    assert losing is True
    assert factor == 0.912  # min(1 - 0.88/10, 34.8/35)
    assert len(reasons) == 2
    # Positive delta, strong standalone: not losing at all.
    row2 = _feature_row('psych_bias', 2, 9.36, 52.3, 'DEAD WEIGHT')
    assert sa.feature_factor(row2)[0] is False


def test_wilson_is_display_only(tmp_path):
    # A WINNING cell with a LOW Wilson lower bound is never cut: the
    # bound is displayed, it never gates. 60% at n=20 -> Wilson ~38.7%.
    cells = {('setup', 'LONG', 'na', 'neutral'):
             {'wins': 12, 'losses': 8, 'win_rate': 60.0,
              'expectancy_usd': 5.0, 'graded': 20,
              'paper_pnl_total': 100.0}}
    p = sa.propose(_fake_results(tmp_path, cells=cells))
    assert p['track_changes'] == []
    # ...while a LOSING cell at the same n is cut, and its change
    # carries the Wilson value as a display field only.
    cells = {('pattern', 'LONG', 'na', 'risk_off'):
             {'wins': 4, 'losses': 16, 'win_rate': 20.0,
              'expectancy_usd': -18.0, 'graded': 20,
              'paper_pnl_total': -360.0}}
    p = sa.propose(_fake_results(tmp_path, cells=cells))
    assert len(p['track_changes']) == 1
    ch = p['track_changes'][0]
    assert ch['wilson_lower_95_pct'] is not None
    assert ch['new_shadow_multiplier'] == sa.track_factor(20.0, -18.0)[0]


def test_propose_fake_cells_and_features(tmp_path):
    cells = {
        ('pattern', 'LONG', 'na', 'risk_off'):
            {'wins': 16, 'losses': 75, 'win_rate': 17.6,
             'expectancy_usd': -18.1, 'graded': 91,
             'paper_pnl_total': -1646.7},
        ('setup', 'LONG', 'na', 'neutral'):
            {'wins': 22, 'losses': 4, 'win_rate': 84.6,
             'expectancy_usd': 11.23, 'graded': 205,
             'paper_pnl_total': 291.86},
        ('mover', 'LONG', 'na', 'neutral'):
            {'wins': 1, 'losses': 2, 'win_rate': 33.3,
             'expectancy_usd': -5.0, 'graded': 3,
             'paper_pnl_total': -15.0},  # too thin: skipped
    }
    rows = [
        _feature_row('macd', 10, -0.88, 34.8, 'DEAD WEIGHT · INVERTED'),
        # A big-weight losing feature: the step IS material.
        _feature_row('big', 5, -5.0, 40.0, 'DEAD WEIGHT'),
    ]
    results = _fake_results(tmp_path, cells=cells, feature_rows=rows)
    results['shadow']['shadow_weights'] = [0.0] * 22
    results['shadow']['shadow_weights'][5] = 1.0
    results['shadow']['shadow_weights'][10] = -0.015
    p = sa.propose(results)
    # checked counts every add_track call: 3 cells + cycle overall +
    # shadow crypto + stock intraday + equity swings = 7.
    assert p['checked'] == {'features': 2, 'track_cells': 7}
    assert len(p['track_changes']) == 1
    ch = p['track_changes'][0]
    assert ch['name'].startswith('pattern LONG')
    assert ch['new_shadow_multiplier'] == 0.095  # min(17.6/50, 1-18.1/20)
    assert ch['wilson_lower_95_pct'] is not None
    assert len(p['feature_changes']) == 1
    assert p['feature_changes'][0]['feature'] == 'big'
    assert p['feature_changes'][0]['new_shadow_weight'] == 0.5
    # macd crossed the line but its step is < 0.005: immaterial, listed.
    assert len(p['feature_immaterial']) == 1
    assert p['feature_immaterial'][0]['feature'] == 'macd'
    # Too thin: the mover cell (n=3) + cycle overall + both ledgers +
    # equity swings (all n=0 in the fake repo).
    assert len(p['skipped_thin']) == 5
    assert 'mover LONG' in p['skipped_thin'][0]['name']
    md = sa.render_report(p)
    assert 'pattern LONG' in md and 'shadow multiplier 1.0 →' in md
    assert 'displayed only, never a gate' in md.lower() or \
        'DISPLAYED ONLY' in md


def test_propose_cycle_deduped_not_per_variable(tmp_path):
    # The actuator acts on the DEDUPED cycle book once — never on the
    # five per-variable copies.
    overall = {'graded': 297, 'wins': 63, 'losses': 234, 'win_rate': 21.2,
               'expectancy_per_1000': -19.12}
    p = sa.propose(_fake_results(tmp_path, cycle_overall=overall))
    cyc = [t for t in p['track_changes'] if t['kind'] == 'cycle']
    assert len(cyc) == 1
    assert cyc[0]['name'] == 'cycle — DEDUPED (distinct outcomes)'
    assert cyc[0]['new_shadow_multiplier'] == 0.044  # min(21.2/50, 1-19.12/20)


def test_module_does_not_touch_live_engine():
    src = open(os.path.join(REPO, 'Scripts', 'shadow_actuator.py')).read()
    for mod in ('import intraday', 'from intraday', 'import decide',
                'from decide', 'import quantum_lab', 'from quantum_lab',
                'import self_tune', 'from self_tune'):
        assert mod not in src
    assert 'variable_tests' in src  # harness only (+ gate_log for the adjust)
    assert 'gate_log' in src
    # The ONE live write is the scoring-weight file; the boundary is
    # stated at every live change point. No order placement anywhere.
    assert 'corrected_weights' in src
    assert 'NEVER places an order' in src
    for banned in ('place_order', 'submit_order', 'buy(', 'sell(',
                   'stripe', 'plaid'):
        assert banned not in src


# ------------------------------------------------------------------ integration

def _sha(path):
    return hashlib.sha256(open(path, 'rb').read()).hexdigest()


def test_run_on_real_repo(tmp_path):
    before = {p: _sha(os.path.join(REPO, 'data', p))
              for p in ('history.json', 'intraday.json')}
    out_dir = tmp_path / 'hidden'
    # --no-live: the integration test must not write the real repo's
    # live weight file; the daily run omits the flag and writes it.
    sys.argv = ['shadow_actuator.py', '--repo', REPO, '--out',
                str(out_dir), '--no-live']
    assert sa.main() == 0
    today = sa.datetime.now(sa.timezone.utc).strftime('%Y-%m-%d')
    md_path = out_dir / f'shadow-actuator-{today}.md'
    json_path = out_dir / f'shadow-weights-{today}.json'
    assert md_path.exists() and json_path.exists()
    proposal = json.loads(json_path.read_text())
    assert proposal['losing_line']['wilson_95'] == 'displayed only, never a gate'
    assert proposal['f_t'] == 1
    assert proposal['live_weights_path'] is None  # --no-live
    # F(t) state, versioned by t; correction log with a live entry.
    state_lines = (out_dir / 'f-state.jsonl').read_text().strip().splitlines()
    assert len(state_lines) == 1
    state = json.loads(state_lines[0])
    assert state['t'] == 1 and len(state['weights']) == 22
    assert (out_dir / 'shadow-state.json').exists()
    log = [json.loads(l) for l in
           (out_dir / 'correction-log.jsonl').read_text().splitlines()]
    live_entries = [e for e in log if e['scope'] == 'live']
    assert len(live_entries) == 1
    assert live_entries[0]['kind'] == 'live-weights'
    assert live_entries[0]['outcome'] is None  # graded on later runs
    assert all(e['t'] == 1 for e in log)
    # Every changed cell carries its distance to 95%.
    assert proposal['next_t_preview']['features_near_50']
    text = md_path.read_text()
    assert 'APPLIED at t=1' in text
    assert 'pts short of 95%' in text
    assert 'PROPOSED ONLY' not in text
    # Today's known losers must be proposed: pattern LONG Caution and
    # the deduped cycle book, with the multipliers the factor math gives.
    by_name = {t['name']: t for t in proposal['track_changes']}
    pat = by_name.get('pattern LONG band=na regime=risk_off')
    assert pat is not None
    # Live-record values drift as picks are graded — assert shape and
    # range, not the snapshot (17.6% / 0.095 on the day pinned; the
    # factor has since fallen to 0 as the cell's win rate dropped —
    # track_factor's range is 0..1, and 0 zeroes a heavy loser).
    assert 0 <= pat['win_pct'] <= 100
    assert 0 <= pat['new_shadow_multiplier'] <= 1
    cyc = by_name.get('cycle — DEDUPED (distinct outcomes)')
    assert cyc is not None
    assert 0 <= cyc['new_shadow_multiplier'] <= 1
    # Feature corrections move the large train-fit weights — pin presence
    # and range, not the snapshot values (macd 0.204→0.186, roc
    # 0.201→0.195 on the day pinned). Presence itself is live-record
    # dependent: when no feature is currently losing, feature_changes is
    # legitimately empty (2026-11-12: 0 feature corrections, 18 too thin)
    # — then assert the check ran instead of failing on the empty list.
    by_feat = {f['feature']: f for f in proposal['feature_changes']}
    if by_feat:
        for feat in ('macd', 'roc'):
            assert feat in by_feat
            assert 0 <= by_feat[feat]['old_shadow_weight'] <= 1
            assert 0 <= by_feat[feat]['new_shadow_weight'] <= 1
    else:
        assert proposal['checked']['features'] > 0
    assert len(proposal['feature_immaterial']) > 0
    # 50%-rule shadow set rides in the proposal too.
    fr = proposal['fifty_rule_shadow_set']
    assert fr is not None
    # Graded-pick count grows as picks are scored (466 on the day pinned,
    # 471 on 2026-11-12) — assert shape, not the snapshot.
    assert isinstance(fr['grade_vs_current']['n'], int)
    assert fr['grade_vs_current']['n'] > 0
    fr_tracks = {t['name']: t for t in fr['tracks']}
    assert fr_tracks['cycle — DEDUPED (distinct outcomes)']['signed_weight'] < 0
    setup50 = next(t for t in fr['tracks'] if t['name'].startswith('setup LONG'))
    assert setup50['signed_weight'] > 0
    assert 'pattern LONG' in text and 'cycle — DEDUPED' in text
    assert '50%-rule shadow set' in text
    after = {p: _sha(os.path.join(REPO, 'data', p))
             for p in ('history.json', 'intraday.json')}
    assert before == after  # the actuator is read-only on the ledgers


# ------------------------------------------------------------------ autonomous F(t) state

def test_distance_to_95():
    assert sa.distance_to_95(85.4) == 9.6
    assert sa.distance_to_95(95.0) == 0.0
    assert sa.distance_to_95(None) is None


def _fifty_results(tmp_path):
    results = _fake_results(tmp_path)
    results['fifty_rule']['weights'] = [0.01 * i for i in range(22)]
    import variable_tests as vt
    results['fifty_rule']['feature_rows'] = [
        {'name': n, 'train_top_win_pct': 50.0 + i,
         'signed_weight': 0.01 * i,
         'rule_set_delta_exp_usd': 1.0 if i % 2 else -1.0}
        for i, n in enumerate(vt.FEATURE_NAMES)]
    results['fifty_rule']['grade'] = {'exp_usd': -3.58, 'n': 466}
    results['per_kind'] = {'setup': {'expectancy_usd': 11.3}}
    results['residual'] = {'calls_lost': [{'dollar_miss_per_1000': 40.0}],
                           'fades_won': []}
    return results


def test_build_state_folds_term_multipliers(tmp_path):
    results = _fifty_results(tmp_path)
    proposal = sa.propose(results)
    state = sa.build_state(1, results, proposal, {'c': 2.0, 'd': 0.5},
                           50.0, None)
    assert state['t'] == 1 and len(state['weights']) == 22
    # c scales the psychology features (2, 3, 4); d scales acceleration (6)
    assert state['weights'][2] == round(0.02 * 2.0, 4)
    assert state['weights'][3] == round(0.03 * 2.0, 4)
    assert state['weights'][6] == round(0.06 * 0.5, 4)
    assert state['weights'][0] == 0.0  # untouched by the fold
    assert state['term_multipliers'] == {'c': 2.0, 'd': 0.5}
    assert state['proof']['fifty_rule_exp_usd'] == -3.58
    assert state['unknown']['biggest_miss_usd_per_1k'] == 40.0


def test_diff_state_logs_every_change_with_evidence(tmp_path):
    results = _fifty_results(tmp_path)
    proposal = sa.propose(results)
    state = sa.build_state(1, results, proposal, {'c': 1.0, 'd': 1.0},
                           50.0, None)
    entries = sa.diff_state(None, state, 1, results, proposal, [])
    feats = [e for e in entries if e['kind'] == 'feature']
    assert len(feats) == 22  # first run establishes every weight
    assert all(e['t'] == 1 and e['outcome'] is None for e in entries)
    assert all(e['evidence']['distance_to_95'] is not None for e in feats)
    live = [e for e in entries if e['kind'] == 'live-weights']
    assert len(live) == 1 and live[0]['scope'] == 'live'
    assert live[0]['evidence']['fifty_rule_exp_usd'] == -3.58


def test_diff_state_second_run_only_logs_moves(tmp_path):
    results = _fifty_results(tmp_path)
    proposal = sa.propose(results)
    state1 = sa.build_state(1, results, proposal, {'c': 1.0, 'd': 1.0},
                            50.0, None)
    # Identical grades at t=2: no feature/track moves, only the live entry.
    state2 = sa.build_state(2, results, proposal, {'c': 1.0, 'd': 1.0},
                            50.0, state1)
    entries = sa.diff_state(state1, state2, 2, results, proposal, [])
    assert [e for e in entries if e['kind'] == 'feature'] == []
    assert [e for e in entries if e['kind'] == 'track'] == []
    assert len([e for e in entries if e['kind'] == 'live-weights']) == 1


def test_grade_prior_corrections_helped_and_hurt(tmp_path):
    results = _fifty_results(tmp_path)
    log = [
        {'t': 1, 'scope': 'shadow', 'kind': 'feature', 'target': 'momentum',
         'old_value': 0.0, 'new_value': 0.01,
         'evidence': {}, 'outcome': None, 'outcome_t': None,
         'outcome_evidence': None},
        {'t': 1, 'scope': 'shadow', 'kind': 'feature', 'target': 'reversion',
         'old_value': 0.0, 'new_value': 0.02,
         'evidence': {}, 'outcome': None, 'outcome_t': None,
         'outcome_evidence': None},
    ]
    # momentum is index 0 -> delta -1.0 (hurt); reversion index 1 -> +1.0 (helped)
    out = sa.grade_prior_corrections(log, results, 2)
    by_target = {e['target']: e for e in out}
    assert by_target['momentum']['outcome'] == 'hurt'
    assert by_target['momentum']['outcome_t'] == 2
    assert by_target['reversion']['outcome'] == 'helped'


def test_grade_prior_corrections_track_exp_comparison(tmp_path):
    results = _fifty_results(tmp_path)
    results['cells'] = {('pattern', 'LONG', 'na', 'risk_off'):
                        {'wins': 16, 'losses': 75, 'win_rate': 17.6,
                         'expectancy_usd': -18.1, 'graded': 91,
                         'paper_pnl_total': -1646.7}}
    log = [{'t': 1, 'scope': 'shadow', 'kind': 'track',
            'target': 'pattern LONG band=na regime=risk_off',
            'old_value': 1.0, 'new_value': 0.095,
            'evidence': {'expectancy_usd': -25.0},
            'outcome': None, 'outcome_t': None, 'outcome_evidence': None}]
    out = sa.grade_prior_corrections(log, results, 2)
    # -18.1 now vs -25.0 then: the cell improved -> helped
    assert out[0]['outcome'] == 'helped'
    assert out[0]['outcome_evidence']['expectancy_usd_now'] == -18.1


def test_write_live_weights_shape(tmp_path):
    repo = tmp_path / 'repo'
    state = {'t': 3, 'timestamp': '2026-11-11T00:00:00+00:00',
             'weights': [0.01] * 22, 'weights_source': 'test',
             'proof': {'fifty_rule_exp_usd': -3.58}}
    path = sa.write_live_weights(str(repo), state)
    payload = json.loads(open(path).read())
    assert payload['t'] == 3 and len(payload['weights']) == 22
    assert payload['proof']['fifty_rule_exp_usd'] == -3.58


def test_two_runs_grade_the_first_run(tmp_path):
    out_dir = tmp_path / 'hidden'
    for _ in range(2):
        sys.argv = ['shadow_actuator.py', '--repo', REPO, '--out',
                    str(out_dir), '--no-live']
        assert sa.main() == 0
    states = [json.loads(l) for l in
              (out_dir / 'f-state.jsonl').read_text().splitlines()]
    assert [s['t'] for s in states] == [1, 2]
    log = [json.loads(l) for l in
           (out_dir / 'correction-log.jsonl').read_text().splitlines()]
    t1 = [e for e in log if e['t'] == 1]
    assert t1, 'first-run corrections must be logged'
    # Same grades on run 2: the t=1 feature corrections are graded
    # from the fresh deltas — helped where the signed weight earns.
    graded = [e for e in t1 if e['kind'] == 'feature'
              and e['outcome'] is not None]
    assert graded, 'prior feature corrections must carry an outcome by t=2'
    assert {e['outcome'] for e in graded} <= {'helped', 'hurt', 'neutral'}
    psych = next(e for e in t1 if e['target'] == 'psych_bias')
    assert psych['outcome'] == 'helped'  # rule-set delta +$5.89/1k
