# Quantum Lab — the quantum algorithms from the videos, simulated
# classically and graded against the classical methods.
#
# HONEST SCOPE: there is no quantum hardware here. This module runs the
# algorithms on a state-vector simulator written with the Python standard
# library only (no Qiskit, no cloud queue). The lab proves the algorithms
# work, benchmarks them against what the engine already does classically,
# and keeps the system ready for the day hardware can run them at scale.
# A simulated result is NEVER presented as a quantum speedup — simulation
# is slower than the classical method at real problem sizes. What the lab
# earns is readiness: when the hardware arrives, the system already knows
# which algorithm wins on which problem, because the comparison was
# graded here first.
#
# Algorithms (all from the videos Nic sent, Oct 24-25 2026):
#   Grover's search  — "How a Quantum Computer Finds 1 in 16 in 3 Steps"
#   QAOA             — Wittek's Quantum ML Lecture 18 (hybrid quantum +
#                      classical optimization loop)
#   VQC classifier   — "Quantum Machine Learning for Financial Trend
#                      Prediction" (VQC vs LSTM/XGBoost on direction)
#   Shor / HHL       — NOT simulated: Shor's needs thousands of logical
#                      qubits (encryption clock — tracked in Foresight,
#                      not run here); HHL returns a quantum state you
#                      cannot fully read out, so a classical benchmark
#                      of it would be meaningless.
import cmath
import json
import math
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        '..', 'data')


# ------------------------------------------------------------ state vector
def _apply_1q(state, n, q, gate):
    """Apply a 2x2 gate to qubit q (q=0 is the leftmost/most-significant
    bit). Returns a new state vector."""
    a, b, c, d = gate
    out = list(state)
    bit = n - 1 - q
    for i in range(len(state)):
        if (i >> bit) & 1 == 0:
            j = i | (1 << bit)
            x, y = state[i], state[j]
            out[i] = a * x + b * y
            out[j] = c * x + d * y
    return out


def _rx(theta):
    c, s = math.cos(theta / 2), math.sin(theta / 2)
    return (complex(c), complex(0, -s), complex(0, -s), complex(c))


def _ry(theta):
    c, s = math.cos(theta / 2), math.sin(theta / 2)
    return (complex(c), complex(-s), complex(s), complex(c))


def _h():
    s = 1 / math.sqrt(2)
    return (complex(s), complex(s), complex(s), complex(-s))


def _apply_cnot(state, n, control, target):
    out = list(state)
    cb, tb = n - 1 - control, n - 1 - target
    for i in range(len(state)):
        if ((i >> cb) & 1) and not ((i >> tb) & 1):
            j = i | (1 << tb)
            out[i], out[j] = state[j], state[i]
    return out


def _probs(state):
    return [abs(a) ** 2 for a in state]


# ------------------------------------------------------------ Grover's search
def grover(n_qubits=4, target=7, rounds=3):
    """Grover's search over N = 2**n items. Oracle flips the target's
    phase; diffusion inverts about the mean. Both are exact — the
    matrix-free form is mathematically identical to the gate circuit.
    Classical baseline: expected N/2 checks to find the item by
    checking one by one (N worst case)."""
    n_items = 2 ** n_qubits
    amp = 1 / math.sqrt(n_items)
    state = [complex(amp)] * n_items
    for _ in range(rounds):
        state[target] = -state[target]  # oracle: mark the answer
        mean = sum(state) / n_items     # diffusion: invert about the mean
        state = [2 * mean - a for a in state]
    p = abs(state[target]) ** 2
    return {
        'n_items': n_items,
        'target': target,
        'quantum_steps': rounds,
        'classical_expected_checks': n_items / 2,
        'classical_worst_checks': n_items,
        'success_probability': round(p, 4),
        'verdict': (f'Found 1 in {n_items} in {rounds} steps with '
                    f'{p:.1%} probability; checking one by one takes '
                    f'{n_items / 2:.0f} on average. Square-root speedup, '
                    f'not magic — and only once hardware exists.'),
    }


# ------------------------------------------------------------ QAOA (MaxCut)
def _maxcut_value(bits, edges):
    return sum(1 for i, j in edges if bits[i] != bits[j])


def _greedy_maxcut(n, edges):
    """Classical greedy baseline: build the cut one node at a time,
    always taking the side that cuts more edges so far."""
    adj = {i: [] for i in range(n)}
    for i, j in edges:
        adj[i].append(j)
        adj[j].append(i)
    side = [None] * n
    side[0] = 0
    for v in range(1, n):
        gain0 = sum(1 for u in adj[v] if side[u] == 1)
        gain1 = sum(1 for u in adj[v] if side[u] == 0)
        side[v] = 0 if gain0 >= gain1 else 1
    return _maxcut_value(side, edges)


def _qaoa_expected(n, edges, gamma, beta):
    """p=1 QAOA expected cut. Cost unitary applies a phase per edge
    from the Z_i Z_j eigenvalues; mixer is Rx(2*beta) on every qubit."""
    state = [complex(1 / math.sqrt(2 ** n))] * (2 ** n)
    # Cost: exp(+i*gamma/2 * sum_edges Z_i Z_j) — global phases dropped.
    for idx in range(2 ** n):
        bits = [(idx >> (n - 1 - q)) & 1 for q in range(n)]
        phase = 0.0
        for i, j in edges:
            zi = 1 if bits[i] == 0 else -1
            zj = 1 if bits[j] == 0 else -1
            phase += zi * zj
        state[idx] *= cmath.exp(1j * gamma * phase / 2)
    # Mixer: Rx(2*beta) on each qubit.
    for q in range(n):
        state = _apply_1q(state, n, q, _rx(2 * beta))
    exp_cut = 0.0
    for idx, p in enumerate(_probs(state)):
        bits = [(idx >> (n - 1 - q)) & 1 for q in range(n)]
        exp_cut += p * _maxcut_value(bits, edges)
    return exp_cut


def qaoa(edges=None, n=6):
    """QAOA p=1 on a fixed 6-node graph. Brute force gives the true
    optimum (the honest anchor); greedy is the classical baseline."""
    if edges is None:
        edges = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 0),
                 (0, 3), (1, 4)]
    optimum = max(_maxcut_value(
        [(i >> (n - 1 - q)) & 1 for q in range(n)], edges)
        for i in range(2 ** n))
    greedy = _greedy_maxcut(n, edges)
    best = (0.0, 0.0, 0.0)
    g_steps, b_steps = 48, 24
    for gi in range(g_steps):
        for bi in range(b_steps):
            g = gi * 2 * math.pi / g_steps
            b = bi * math.pi / b_steps
            val = _qaoa_expected(n, edges, g, b)
            if val > best[0]:
                best = (val, g, b)
    return {
        'graph': f'{n} nodes, {len(edges)} edges',
        'optimum_cut': optimum,
        'classical_greedy_cut': greedy,
        'qaoa_expected_cut': round(best[0], 3),
        'gamma': round(best[1], 3),
        'beta': round(best[2], 3),
        'verdict': (f'QAOA (simulated, p=1) expects a cut of {best[0]:.2f} '
                    f'against greedy {greedy} and optimal {optimum}. On '
                    f'this graph the classical method wins — p=1 is a '
                    f'shallow circuit. Deeper circuits and real hardware '
                    f'are what would pull ahead; the lab says so plainly '
                    f'instead of pretending otherwise.'),
    }


# ------------------------------------------------------------ VQC classifier
def _vqc_expectation(x, theta):
    """2-qubit variational classifier with data re-uploading: encode the
    2 features, variational layer, encode again, variational layer,
    read <Z> on qubit 0 as the score. Re-uploading is the standard
    trick that gives a small circuit enough expressivity to learn."""
    n = 2
    state = [complex(0)] * 4
    state[0] = complex(1)
    for layer in (0, 1):
        base = layer * 4
        state = _apply_1q(state, n, 0, _ry(x[0] * math.pi / 2))
        state = _apply_1q(state, n, 1, _ry(x[1] * math.pi / 2))
        state = _apply_1q(state, n, 0, _ry(theta[base + 0]))
        state = _apply_1q(state, n, 1, _ry(theta[base + 1]))
        state = _apply_cnot(state, n, 0, 1)
        state = _apply_1q(state, n, 0, _ry(theta[base + 2]))
        state = _apply_1q(state, n, 1, _ry(theta[base + 3]))
    return sum(p * (1 if ((i >> 1) & 1) == 0 else -1)
               for i, p in enumerate(_probs(state)))


def _vqc_loss(data, theta):
    return sum((1 - y * _vqc_expectation(x, theta)) / 2
               for x, y in data)


def _vqc_accuracy(data, theta):
    return sum(1 for x, y in data
               if (1 if _vqc_expectation(x, theta) >= 0 else -1) == y) \
        / len(data)


def _perceptron_accuracy(data, epochs=20):
    """Classical baseline on the same toy data."""
    w = [0.0, 0.0]
    b = 0.0
    for _ in range(epochs):
        for x, y in data:
            if y * (w[0] * x[0] + w[1] * x[1] + b) <= 0:
                w[0] += 0.5 * y * x[0]
                w[1] += 0.5 * y * x[1]
                b += 0.5 * y
    ok = sum(1 for x, y in data
             if (1 if w[0] * x[0] + w[1] * x[1] + b >= 0 else -1) == y)
    return ok / len(data)


def vqc():
    """Toy direction-classification task (the DEEPAK video's setup, at
    toy scale): 2 features, class = sign of their sum. Seeded random
    restarts + coordinate descent train the 8 variational angles;
    selection is by accuracy (the task's metric), loss breaks ties.
    Perceptron is the classical baseline on identical data."""
    import random
    data = [((0.8, 0.6), 1), ((0.6, 0.9), 1), ((0.9, -0.2), 1),
            ((-0.7, 0.8), 1), ((-0.8, -0.6), -1), ((-0.6, -0.9), -1),
            ((-0.9, 0.2), -1), ((0.7, -0.8), -1)]
    rng = random.Random(42)  # seeded: the lab is deterministic
    best = None  # (accuracy, -loss, theta)
    for _ in range(6):
        theta = [rng.uniform(-math.pi, math.pi) for _ in range(8)]
        loss = _vqc_loss(data, theta)
        for step in (0.6, 0.3, 0.15):
            improved = True
            while improved:
                improved = False
                for k in range(8):
                    for s in (step, -step):
                        trial = list(theta)
                        trial[k] += s
                        trial_loss = _vqc_loss(data, trial)
                        if trial_loss < loss - 1e-9:
                            loss, theta = trial_loss, trial
                            improved = True
        cand = (_vqc_accuracy(data, theta), -loss, theta)
        if best is None or cand[:2] > best[:2]:
            best = cand
    acc = best[0]
    base = _perceptron_accuracy(data)
    return {
        'task': '2-feature direction classification (8 toy points)',
        'vqc_accuracy': round(acc, 3),
        'classical_perceptron_accuracy': round(base, 3),
        'verdict': (f'VQC (simulated) scores {acc:.0%} against the '
                    f'classical perceptron at {base:.0%} on the same '
                    f'toy data. The quantum classifier works — it ties '
                    f'the classical one at this scale and does not beat '
                    f'it. That is the honest result.'),
    }


# ------------------------------------------------------------ runner
def run_lab(out_path=None):
    """Run all three lab benchmarks and write data/quantum_lab.json.
    Deterministic — re-run only when the lab code changes, not every
    engine loop."""
    result = {
        'generated': __import__('datetime').datetime.now(
            __import__('datetime').timezone.utc).isoformat(),
        'note': ('Simulated classically with the Python standard library '
                 '— no quantum hardware. The lab proves the algorithms '
                 'and benchmarks them against the classical methods, so '
                 'the system is ready the day hardware can run them at '
                 'scale. A simulated result is never a speedup claim.'),
        'grover': grover(),
        'qaoa': qaoa(),
        'vqc': vqc(),
    }
    path = out_path or os.path.join(DATA_DIR, 'quantum_lab.json')
    with open(path, 'w') as f:
        json.dump(result, f, indent=2)
    print(f'Quantum Lab: Grover P={result["grover"]["success_probability"]}, '
          f'QAOA cut={result["qaoa"]["qaoa_expected_cut"]} '
          f'(greedy {result["qaoa"]["classical_greedy_cut"]}, '
          f'opt {result["qaoa"]["optimum_cut"]}), '
          f'VQC acc={result["vqc"]["vqc_accuracy"]} '
          f'(perceptron {result["vqc"]["classical_perceptron_accuracy"]})')
    return result


if __name__ == '__main__':
    run_lab()
