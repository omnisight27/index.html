// Quantum Lab panel — the quantum algorithms, simulated classically
// and graded against the classical methods. Reads data/quantum_lab.json,
// written by Scripts/quantum_lab.py (deterministic benchmark — re-run
// only when the lab code changes, not every engine loop). Rows are
// never invented; the empty state is shown plainly if the file is
// missing. A simulated result is never presented as a speedup.
(function () {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  fetch('data/quantum_lab.json?_=' + Date.now())
    .then(function (r) { return r.json(); })
    .then(function (d) {
      var list = document.getElementById('quantum-lab-list');
      var stamp = document.getElementById('quantum-lab-stamp');
      if (stamp && d.generated) {
        stamp.textContent = 'Benchmark run — ' + new Date(d.generated).toLocaleString();
      }
      var g = d.grover || {}, q = d.qaoa || {}, v = d.vqc || {}, p = d.portfolio || {};
      var html = '';
      if (g.verdict) {
        html += '<p><strong>🔍 Grover\u2019s search</strong> — ' + esc(g.verdict) + '</p>';
      }
      if (q.verdict) {
        html += '<p><strong>✂️ QAOA optimization</strong> — ' + esc(q.verdict) + '</p>';
      }
      if (v.verdict) {
        html += '<p><strong>🧠 VQC classifier</strong> — ' + esc(v.verdict) + '</p>';
      }
      if (p.verdict) {
        html += '<p><strong>📈 Portfolio QUBO</strong> — ' + esc(p.verdict) + '</p>';
      }
      if (!html) {
        list.innerHTML = '<p class="live-stamp">No quantum lab results yet — the lab runs when its code changes.</p>';
        return;
      }
      if (d.note) {
        html += '<p class="live-stamp">' + esc(d.note) + '</p>';
      }
      list.innerHTML = html;
    })
    .catch(function () {
      document.getElementById('quantum-lab-list').innerHTML =
        '<p class="live-stamp">Quantum lab results unavailable right now — try reloading in a minute.</p>';
    });
})();
