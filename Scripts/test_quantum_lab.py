# Tests for the Quantum Lab — run directly, no pytest needed:
#   python3 test_quantum_lab.py
import quantum_lab


def test_grover_finds_target():
    r = quantum_lab.grover()
    assert r['success_probability'] > 0.9, r
    assert r['quantum_steps'] == 3
    assert r['classical_expected_checks'] == 8


def test_qaoa_reports_honestly():
    r = quantum_lab.qaoa()
    assert 0 <= r['qaoa_expected_cut'] <= r['optimum_cut'], r
    assert r['classical_greedy_cut'] <= r['optimum_cut'], r


def test_vqc_runs_and_scores():
    r = quantum_lab.vqc()
    assert r['vqc_accuracy'] >= 0.75, r
    assert r['classical_perceptron_accuracy'] == 1.0, r


def test_portfolio_qubo():
    r = quantum_lab.portfolio()
    assert r['optimum_cost'] <= r['annealing_cost'] <= r['greedy_cost'], r
    assert r['qaoa_expected_cost'] >= r['optimum_cost'], r
    assert len(r['annealing_pick']) == 3
    assert len(r['greedy_pick']) == 3


def test_lab_writes_json(tmp_path='/tmp/quantum_lab_test.json'):
    result = quantum_lab.run_lab(out_path=tmp_path)
    assert set(result) == {'generated', 'note', 'grover', 'qaoa', 'vqc',
                           'portfolio'}


if __name__ == '__main__':
    for name in list(globals()):
        if name.startswith('test_'):
            globals()[name]()
            print(f'{name}: PASS')
    print('all quantum lab tests pass')
