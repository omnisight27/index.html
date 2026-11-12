# Tests for the decision simplifier (Scripts/decide.py).
# Run:  python3 -m pytest Scripts/test_decide.py -q
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import decide as d  # noqa: E402


# ------------------------------------------------------------------ fixtures
def _mover(asset='XCN', chg=14.33, direction='UP', plan=True,
           tradable=('coinbase',)):
    m = {'asset': asset, 'direction': direction, 'chg_24h': chg,
         'confidence': None,
         'plan': ({'entry': 0.0035962, 'take_profit': 0.0040482,
                   'stop_loss': 0.0034832} if plan else None),
         'tradable': list(tradable), 'plan_note': '',
         'kind': 'mover', 'track': 'mover',
         'evidence': {'win_pct': 29.9, 'n': 112, 'exp': -8.41,
                      'verdict': 'mover track', 'cal_win': None,
                      'cal_n': None}}
    return m


def _swing(asset='BTC', conf=75, direction='LONG', plan=True,
           tradable=('coinbase',), exp=4.5, n=700, win=45.0):
    return {'asset': asset, 'direction': direction, 'chg_24h': None,
            'confidence': conf,
            'plan': ({'entry': 74000, 'take_profit': 78000,
                      'stop_loss': 72000} if plan else None),
            'tradable': list(tradable), 'plan_note': '',
            'kind': 'swing', 'track': 'swing',
            'evidence': {'win_pct': win, 'n': n, 'exp': exp,
                         'verdict': 'swing book', 'cal_win': 43.6,
                         'cal_n': 55}}


def _caution_ctx():
    return {'regime': 'RISK_OFF', 'buy_pct': 38}


def _ok_ctx():
    return {'regime': 'RISK_ON', 'buy_pct': 62}


def _feeds_ok():
    return {'vote': 'NEUTRAL', 'note': 'CPI YoY 3.5%', 'cpi_yoy': 3.5,
            'oi_assets': [], 'funding_assets': [],
            'missing': ['OKX', 'Deribit', 'DeFiLlama', 'Finviz'],
            'macro_asof': '2026-11-06'}


def _precedent_against():
    return {'date': '2026-11-10', 'lines': ['7d: bearish'],
            'vote': 'AGAINST', 'detail': {'7d': 0.417, '30d': 0.417}}


# ------------------------------------------------------------------ parsing
def test_parse_harness_table(tmp_path):
    text = (
        "## Table B — tracks, feeds, gates\n\n"
        "| Variable / track | n graded | Trig% | Win% | Expectancy $/1k | Ablation | Current weight / role | Verdict |\n"
        "|---|---:|---:|---:|---:|---|:---|:---|\n"
        "| mover (Proof Book kind) | 112 | 77.7 | 29.9 | -8.41 | -1.9 / -0.10 | shadow | NOT WORKING |\n"
        "| setup (Proof Book kind) | 338 | 12.1 | 85.4 | 11.30 | 4.6 / 1.88 | shadow | WORKING |\n"
        "| cycle — DEDUPED book (distinct outcomes, not 5x labels) | 290 | 100.0 | 20.0 | -19.82 | — | shadow | NOT WORKING |\n"
        "| pattern FVG|LONG | 31 | 58.1 | 38.9 | -1.41 | — | shadow | NOT WORKING |\n")
    p = tmp_path / 'rep.md'
    p.write_text(text)
    out = d.parse_harness_table(str(p))
    assert out['mover'] == {'win_pct': 29.9, 'n': 112, 'trig': 77.7,
                            'exp': -8.41, 'verdict': 'NOT WORKING'}
    assert out['setup']['win_pct'] == 85.4
    assert out['cycle']['exp'] == -19.82
    assert 'pattern' not in out  # per-pattern rows are not aggregates


def test_parse_harness_table_a(tmp_path):
    text = (
        "## Table A — the 22 core features (out-of-sample test slice)\n\n"
        "| # | Feature | Current w (learned) | Heuristic w | Proposal Nov 1 | Standalone top-3rd win% (n) | Spread (pp) | Ablation Δwin (pp) | Ablation Δexp $/1k (±SE) | Flips | Justified w | Verdict |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
        "| 6 | acceleration | 0.129 | 0.8 | -1.034 | 34.8 (155) | -13.5 | -1.1 | 12.19 (±5.49) | 55 | 0.225 | EARNS WEIGHT · INVERTED |\n"
        "| 2 | psych_bias | -0.06 | 0.6 | -0.125 | 52.3 (155) | 3.2 | 3.6 | 9.36 (±3.61) | 99 | 0.173 | DEAD WEIGHT |\n")
    p = tmp_path / 'rep.md'
    p.write_text(text)
    rows = d.parse_harness_table_a(str(p))
    assert len(rows) == 2
    acc = [r for r in rows if r['feature'] == 'acceleration'][0]
    assert (acc['justified'], acc['ablation'], acc['verdict']) == (
        0.225, 12.19, 'EARNS WEIGHT · INVERTED')
    assert [r for r in rows if r['feature'] == 'psych_bias'][0][
        'justified'] == 0.173


def test_parse_harness_table_a_keeps_duplicate_rows(tmp_path):
    text = (
        "## Table A — the 22 core features (out-of-sample test slice)\n\n"
        "| # | Feature | Current w (learned) | Heuristic w | Proposal Nov 1 | Standalone top-3rd win% (n) | Spread (pp) | Ablation Δwin (pp) | Ablation Δexp $/1k (±SE) | Flips | Justified w | Verdict |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
        "| 8 | rsi | 0.002 | 0.3 | 0.117 | 35.5 (155) | -14.8 | 0.2 | 0.01 (±0.17) | 9 | 0.0 | DEAD WEIGHT · INVERTED |\n"
        "| 13 | rsi | 0.003 | 0.5 | 0.219 | 35.5 (155) | -14.8 | 0.2 | 0.01 (±0.19) | 13 | 0.0 | DEAD WEIGHT · INVERTED |\n")
    p = tmp_path / 'rep.md'
    p.write_text(text)
    rows = d.parse_harness_table_a(str(p))
    assert len(rows) == 2  # both rsi rows kept, not collapsed by name


def test_parse_harness_table_a_live_report_counts():
    # The real report: 22 rows, 21 dead weight, 16 read backwards.
    # The old name-keyed dict collapsed the rsi/zscore duplicates and
    # undercounted by two.
    rows = d.parse_harness_table_a()
    assert len(rows) == 22
    assert sum(1 for r in rows if 'DEAD WEIGHT' in r['verdict']) == 21
    assert sum(1 for r in rows if 'INVERTED' in r['verdict']) == 16


def test_parse_direction_agreement(tmp_path):
    text = ('16:- Direction agreement on the test slice: **77.0%** '
            '(recomputed direction vs the direction the engine published, '
            'train-fit weights).\n'
            '17:- Direction agreement with FULL-history weights: **57.3%** '
            '— lower than the train-fit agreement.\n')
    p = tmp_path / 'rep.md'
    p.write_text(text)
    assert d.parse_direction_agreement(str(p)) == (77.0, 57.3)
    assert d.parse_direction_agreement('/tmp/does-not-exist-xyz.md') == (
        None, None)


def test_parse_harness_table_missing_file():
    assert d.parse_harness_table('/tmp/does-not-exist-xyz.md') == {}
    assert d.parse_harness_table_a('/tmp/does-not-exist-xyz.md') == []


# ------------------------------------------------------------------ gates
def test_gate_caution_holds_clean_mover():
    v, reason, rung, nxt = d.decide_action(_mover(), _caution_ctx())
    assert v == 'WAIT'
    assert 'caution' in reason and '38% buy' in reason
    assert rung == 1
    assert 'out of caution' in nxt


def test_gate_all_clear_buys_when_track_earns():
    m = _mover()
    m['evidence'] = {'win_pct': 55.0, 'n': 50, 'exp': 11.30,
                     'verdict': 'setup track', 'cal_win': None,
                     'cal_n': None}
    v, reason, rung, nxt = d.decide_action(m, _ok_ctx())
    assert v == 'BUY'
    assert rung == 2
    assert '$500' in reason


def test_gate_rung3_needs_a_qualifying_cell():
    m = _mover()
    m['evidence'] = {'win_pct': 65.0, 'n': 150, 'exp': 12.0,
                     'verdict': 'setup track', 'cal_win': None,
                     'cal_n': None}
    v, reason, rung, nxt = d.decide_action(m, _ok_ctx())
    assert v == 'BUY' and rung == 3
    assert 'Kelly' in reason


def test_gate_down_mover_is_watch():
    m = _mover(direction='DOWN', chg=-13.93, plan=False, tradable=())
    v, reason, rung, nxt = d.decide_action(m, _ok_ctx())
    assert v == 'WATCH' and rung == 0
    assert 'down-move' in reason


def test_gate_short_is_watch_live_long_only():
    s = _swing(asset='UNI', conf=44, direction='SHORT', exp=4.5, n=700,
               win=54.6)
    v, reason, rung, nxt = d.decide_action(s, _ok_ctx())
    assert v == 'WATCH' and rung == 0
    assert 'live is long-only' in reason


def test_gate_no_plan_is_watch():
    m = _mover(plan=False)
    m['plan_note'] = 'Plan unavailable — price feed hiccup.'
    v, reason, rung, nxt = d.decide_action(m, _ok_ctx())
    assert v == 'WATCH' and rung == 0
    assert 'no plan' in reason and 'hiccup' in reason


def test_gate_no_venue_is_wait():
    m = _mover(tradable=())
    v, reason, rung, nxt = d.decide_action(m, _ok_ctx())
    assert v == 'WAIT' and 'no venue' in reason


def test_gate_already_ran_is_watch():
    m = _mover(chg=67.62)
    v, reason, rung, nxt = d.decide_action(m, _ok_ctx())
    assert v == 'WATCH' and "don't chase" in reason


def test_gate_low_confidence_is_wait():
    s = _swing(conf=55)
    v, reason, rung, nxt = d.decide_action(s, _ok_ctx())
    assert v == 'WAIT' and '55%' in reason and '60% bar' in reason


def test_gate_paper_only_when_track_loses():
    m = _mover()  # mover track loses $8.41/1k
    v, reason, rung, nxt = d.decide_action(m, _ok_ctx())
    assert v == 'WAIT' and 'paper only' in reason
    assert rung == 1
    assert 'positive' in nxt


def test_gate_no_evidence_is_paper_only():
    m = _mover()
    m['evidence'] = {'win_pct': None, 'n': None, 'exp': None,
                     'verdict': 'no graded row', 'cal_win': None,
                     'cal_n': None}
    v, reason, rung, nxt = d.decide_action(m, _ok_ctx())
    assert v == 'WAIT' and 'no graded expectancy' in reason


# ------------------------------------------------------------------ confluence
def test_confluence_disagreement_is_named():
    cand = _mover()
    cand['evidence']['exp'] = 11.30  # variable FOR
    votes, verdict = d.confluence(cand, _caution_ctx(),
                                  _precedent_against(), _feeds_ok())
    assert votes['regime'] == 'AGAINST'
    assert votes['variable'] == 'FOR'
    assert votes['precedent'] == 'AGAINST'
    assert 'BLOCKED by regime' in verdict
    assert 'disagreement' in verdict or 'BLOCKED' in verdict


def test_confluence_strong_agreement():
    cand = _mover()
    cand['evidence']['exp'] = 11.30
    prec = {'date': '2026-11-10', 'lines': [], 'vote': 'FOR', 'detail': {}}
    feeds = dict(_feeds_ok(), vote='FOR')
    votes, verdict = d.confluence(cand, _ok_ctx(), prec, feeds)
    assert 'strong agreement' in verdict


def test_confluence_no_read_when_nothing_known():
    cand = _mover()
    cand['evidence'] = {'win_pct': None, 'n': None, 'exp': None,
                        'verdict': 'no graded row', 'cal_win': None,
                        'cal_n': None}
    prec = {'date': None, 'lines': [], 'vote': 'NO READ', 'detail': {}}
    feeds = dict(_feeds_ok(), vote='NO READ')
    votes, verdict = d.confluence(cand, _ok_ctx(), prec, feeds)
    assert votes['variable'] == 'NO READ'
    # Only the regime speaks for it (RISK_ON) — weak agreement, and the
    # gate still holds it at paper-only for lack of graded expectancy.
    assert 'weak agreement — only regime' in verdict


# ------------------------------------------------------------------ evidence
def test_evidence_on_the_line():
    cand = _mover()
    votes, verdict = d.confluence(cand, _caution_ctx(),
                                  _precedent_against(), _feeds_ok())
    v, reason, rung, nxt = d.decide_action(cand, _caution_ctx())
    line = d.render_action(cand, v, reason, rung, nxt, votes, verdict)
    assert 'n=112' in line and '$/1k' in line
    assert '29.9%' in line
    assert 'Rung 1' in line
    assert 'conditions in caution' in line


def test_evidence_says_thin_when_n_small():
    cand = _mover()
    cand['evidence'] = {'win_pct': 60.0, 'n': 13, 'exp': 0.97,
                        'verdict': 'pattern cell', 'cal_win': None,
                        'cal_n': None}
    assert 'THIN EVIDENCE' in d.evidence_str(cand)


def test_evidence_says_no_graded_evidence():
    cand = _mover()
    cand['evidence'] = {'win_pct': None, 'n': None, 'exp': None,
                        'verdict': 'no graded row', 'cal_win': None,
                        'cal_n': None}
    assert d.evidence_str(cand) == 'no graded evidence'


def test_calibration_shown_on_swing_line():
    cand = _swing(conf=75)
    line = d.evidence_str(cand)
    assert 'stated 75%' in line and 'graded 43.6%' in line


# ------------------------------------------------------------------ ladder
def test_ladder_has_four_rungs_and_rules():
    assert [r['rung'] for r in d.LADDER] == [0, 1, 2, 3]
    assert 'Kelly' in d.LADDER[3]['name']
    assert 'Kelly' in d.LADDER[3]['size']
    assert 'real money off' in d.DEESCALATION
    assert 'trail the stop' in d.WITHIN_TRADE


def test_deescalation_caution_drops_a_rung():
    m = _mover()
    m['evidence'] = {'win_pct': 85.4, 'n': 338, 'exp': 11.30,
                     'verdict': 'setup track', 'cal_win': None,
                     'cal_n': None}
    _, _, rung_ok, _ = d.decide_action(m, _ok_ctx())
    _, _, rung_caution, _ = d.decide_action(m, _caution_ctx())
    # Rung 3 (larger) when clear; caution takes real money off -> paper.
    assert rung_ok == 3 and rung_caution == 1


# ------------------------------------------------------------------ QUBO
def _small_qubo():
    values = [0.30, 0.25, 0.20, 0.10]
    corr = [[0.0 if i == j else 0.1 for j in range(4)] for i in range(4)]
    return values, corr


def test_qubo_brute_force_finds_optimum():
    values, corr = _small_qubo()
    cost = d._qubo_cost(values, corr, 0.5, 1.0, 2)
    c, bits = d._brute_force(cost, 4)
    assert bits == [1, 1, 0, 0]
    assert abs(c - (-0.50)) < 1e-9


def test_qubo_annealing_beats_or_ties_greedy():
    values, corr = _small_qubo()
    cost = d._qubo_cost(values, corr, 0.5, 1.0, 2)
    order = sorted(range(4), key=lambda i: -values[i])
    greedy_bits = [0] * 4
    for i in order[:2]:
        greedy_bits[i] = 1
    greedy_cost = cost(greedy_bits)
    c, _ = d._anneal(cost, 4, 2)
    assert c <= greedy_cost + 1e-9


def test_qaoa_runs_small_and_stays_above_optimum():
    values, corr = _small_qubo()
    cost = d._qubo_cost(values, corr, 0.5, 1.0, 2)
    exp_cost, gamma, beta = d._qaoa_best(cost, 4)
    assert exp_cost >= -0.50 - 1e-9  # never below the true optimum
    assert exp_cost < 0.5  # and in a sane range, not nonsense


def test_solve_qubo_empty_pool():
    out = d.solve_qubo([0.0, 0.0, 0.0], [[0.0] * 3 for _ in range(3)],
                       k=3, label='empty')
    assert out['pool_empty'] is True


def test_solve_qubo_cardinality_holds_at_dollar_scale():
    # Regression: raw $/1k values (11.30) with a penalty of 1.0 let the
    # solver buy the penalty and pick 7 of 8. After normalization the
    # pick must be exactly k=3, for every solver.
    values = [11.30] * 8
    corr = [[0.0 if i == j else 0.7 for j in range(8)] for i in range(8)]
    out = d.solve_qubo(values, corr, k=3, label='queue')
    assert len(out['pick']) == 3
    assert len(out['brute_force']['pick']) == 3
    assert len(out['annealing']['pick']) == 3
    # And the optimum is a 3-pick: any 3-set ties, cost must beat a 7-pick.
    assert out['brute_force']['cost'] < 0


def test_solve_qubo_cardinality_mixed_values():
    values = [11.30, 9.0, 4.5, 0.5, 0.2, 0.1, 0.05, 0.01]
    corr = [[0.0 if i == j else 0.1 for j in range(8)] for i in range(8)]
    out = d.solve_qubo(values, corr, k=3, label='mixed')
    assert sorted(out['pick']) == [0, 1, 2]  # the three best values


def test_weight_selection_qubo_picks_earners():
    justified = {'acceleration': 0.225, 'psych_bias': 0.173,
                 'vol': 0.005, 'zscore': 0.001, 'momentum': 0.0,
                 'rsi': 0.0, 'macd': 0.0, 'trend_align': 0.0}
    out = d.weight_selection_qubo(justified)
    red = out['reduced']
    picks = {out['reduced_features'][i] for i in (red.get('pick') or [])}
    assert 'acceleration' in picks and 'psych_bias' in picks
    assert red['winner'] in ('brute_force', 'annealing')


# ------------------------------------------------------------------ queue line
def test_queue_qubo_line_names_and_tie():
    queue = [_mover(asset=a) for a in ('AKT', 'POL', 'RUNE')]
    for c in queue:
        c['evidence'] = {'win_pct': 85.4, 'n': 338, 'exp': 11.30,
                         'verdict': 'setup track', 'cal_win': None,
                         'cal_n': None}
    qubo = d.solve_qubo([11.30] * 3, [[0.0] * 3 for _ in range(3)],
                        k=3, label='candidate queue')
    line = d.queue_qubo_line(queue, qubo)
    assert 'AKT' in line and 'POL' in line and 'RUNE' in line
    assert 'tied' in line  # identical evidence -> arbitrary tie-break
    assert '[' not in line  # no bare index list
    assert 'pool empty' in d.queue_qubo_line(
        queue, {'pool_empty': True})


def test_in_queue_needs_a_plan():
    # A setup-track candidate held by caution but with NO plan fails
    # the plan gate, not just caution — it is not in the queue.
    cand = _mover(asset='AKT')
    cand['evidence'] = {'win_pct': 85.4, 'n': 338, 'exp': 11.30,
                        'verdict': 'setup track', 'cal_win': None,
                        'cal_n': None}
    cand['plan'] = None
    assert d.in_queue(cand, 'WAIT', 'conditions in caution — 38% buy') \
        is False
    cand['plan'] = {'entry': 1.0, 'take_profit': 1.2, 'stop_loss': 0.9}
    assert d.in_queue(cand, 'WAIT', 'conditions in caution — 38% buy') \
        is True
    # Losing track: not in the queue even with a plan.
    cand['evidence'] = dict(cand['evidence'], exp=-8.41)
    assert d.in_queue(cand, 'WAIT', 'conditions in caution — 38% buy') \
        is False


# ------------------------------------------------------------------ end to end
def test_end_to_end_fixture_like_today():
    tracks = {'mover': {'win_pct': 29.9, 'n': 112, 'trig': 77.7,
                        'exp': -8.41, 'verdict': 'NOT WORKING'},
              'setup': {'win_pct': 85.4, 'n': 338, 'trig': 12.1,
                        'exp': 11.30, 'verdict': 'WORKING'}}
    ctx = _caution_ctx()
    prec = _precedent_against()
    feeds = _feeds_ok()
    cands = [_mover(),  # XCN-like: plan + venue, held by caution
             _mover(asset='QFI', chg=86101.5, plan=False, tradable=()),
             _mover(asset='BTW', direction='DOWN', chg=-13.93, plan=False,
                    tradable=()),
             _swing(asset='QFI', conf=75, plan=False, tradable=())]
    lines = []
    buys = 0
    for cand in cands:
        votes, verdict = d.confluence(cand, ctx, prec, feeds)
        v, reason, rung, nxt = d.decide_action(cand, ctx)
        if v == 'BUY':
            buys += 1
        lines.append(d.render_action(cand, v, reason, rung, nxt, votes,
                                     verdict))
    assert buys == 0  # caution blocks everything today
    assert any('XCN' in ln and 'WAIT' in ln and 'caution' in ln
               for ln in lines)
    # Both QFIs are held by caution first (checklist order: caution
    # comes before no-plan / no-venue).
    assert sum(1 for ln in lines if 'QFI' in ln and 'WAIT' in ln
               and 'caution' in ln) == 2
    assert any('BTW' in ln and 'down-move' in ln for ln in lines)
    for ln in lines:
        assert 'n=' in ln and '$/1k' in ln
