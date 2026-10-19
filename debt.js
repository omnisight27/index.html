// OmniSight Debt Payoff Optimizer — avalanche method (highest APR first = least interest).
(function () {
  'use strict';
  var debts = [];

  function debtRow(d, i) {
    var div = document.createElement('div');
    div.className = 'debt-row';
    div.innerHTML =
      '<input type="text" placeholder="Name (e.g. Visa)" value="' + (d.name || '') + '" data-f="name">' +
      '<input type="number" placeholder="Balance $" value="' + (d.balance || '') + '" min="0" step="1" data-f="balance">' +
      '<input type="number" placeholder="APR %" value="' + (d.apr || '') + '" min="0" step="0.1" data-f="apr">' +
      '<input type="number" placeholder="Min payment $" value="' + (d.min || '') + '" min="0" step="1" data-f="min">' +
      '<button class="btn-small" data-del="' + i + '">✕</button>';
    return div;
  }

  function render() {
    var list = document.getElementById('debt-list');
    list.innerHTML = '';
    debts.forEach(function (d, i) { list.appendChild(debtRow(d, i)); });
    if (!debts.length) {
      list.innerHTML = '<p class="hint">No debts yet — tap "+ Add a debt" above.</p>';
    }
  }

  function readRows() {
    var rows = document.querySelectorAll('.debt-row');
    debts = [];
    rows.forEach(function (r) {
      var d = {};
      r.querySelectorAll('input').forEach(function (inp) { d[inp.dataset.f] = inp.value; });
      d.balance = parseFloat(d.balance) || 0;
      d.apr = parseFloat(d.apr) || 0;
      d.min = parseFloat(d.min) || 0;
      if (d.balance > 0) debts.push(d);
    });
  }

  // Avalanche: sort by APR desc, simulate month by month
  function avalanche(debts, budget) {
    var ds = debts.map(function (d) {
      return { name: d.name || 'Debt', balance: d.balance, apr: d.apr, min: d.min };
    }).sort(function (a, b) { return b.apr - a.apr; });
    var schedule = [], totalInterest = 0, month = 0;
    while (ds.some(function (d) { return d.balance > 0.01; }) && month < 600) {
      month++;
      var m = { month: month, payments: [] };
      // Minimums first
      ds.forEach(function (d) {
        if (d.balance <= 0.01) return;
        var interest = d.balance * (d.apr / 100 / 12);
        totalInterest += interest;
        d.balance += interest;
        var pay = Math.min(d.min, d.balance);
        d.balance -= pay;
        m.payments.push({ name: d.name, paid: Math.round(pay) });
      });
      // Extra toward highest-APR remaining
      var paid_min = m.payments.reduce(function (s, p) { return s + p.paid; }, 0);
      var extra = budget - paid_min;
      for (var i = 0; i < ds.length && extra > 0.01; i++) {
        var d = ds[i];
        if (d.balance <= 0.01) continue;
        var x = Math.min(extra, d.balance);
        d.balance -= x; extra -= x;
        var mp = m.payments.filter(function (p) { return p.name === d.name; })[0];
        if (mp) mp.paid += Math.round(x);
      }
      m.remaining = Math.round(ds.reduce(function (s, d) { return s + Math.max(d.balance, 0); }, 0));
      schedule.push(m);
    }
    return { months: month, totalInterest: Math.round(totalInterest), schedule: schedule, order: ds.map(function (d) { return d.name; }) };
  }

  // Snowball: sort by balance asc (for comparison)
  function snowball(debts, budget) {
    var ds = debts.map(function (d) {
      return { name: d.name || 'Debt', balance: d.balance, apr: d.apr, min: d.min };
    }).sort(function (a, b) { return a.balance - b.balance; });
    var totalInterest = 0, month = 0;
    while (ds.some(function (d) { return d.balance > 0.01; }) && month < 600) {
      month++;
      ds.forEach(function (d) {
        if (d.balance <= 0.01) return;
        var interest = d.balance * (d.apr / 100 / 12);
        totalInterest += interest;
        d.balance += interest;
        d.balance -= Math.min(d.min, d.balance);
      });
      var paid_min = ds.reduce(function (s, d) { return s + Math.min(d.min, d.balance + d.min); }, 0);
      var extra = budget - ds.reduce(function (s, d) { return s + d.min; }, 0);
      for (var i = 0; i < ds.length && extra > 0.01; i++) {
        var d = ds[i];
        if (d.balance <= 0.01) continue;
        var x = Math.min(extra, d.balance);
        d.balance -= x; extra -= x;
      }
    }
    return { months: month, totalInterest: Math.round(totalInterest) };
  }

  function fmtDate(months) {
    var d = new Date();
    d.setMonth(d.getMonth() + months);
    return d.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
  }

  document.getElementById('add-debt').addEventListener('click', function () {
    readRows();
    debts.push({ name: '', balance: '', apr: '', min: '' });
    render();
  });

  document.getElementById('debt-list').addEventListener('click', function (e) {
    if (e.target.dataset.del !== undefined) {
      readRows();
      debts.splice(parseInt(e.target.dataset.del, 10), 1);
      render();
    }
  });

  document.getElementById('calc').addEventListener('click', function () {
    readRows();
    var budget = parseFloat(document.getElementById('budget').value) || 0;
    if (!debts.length) { alert('Add at least one debt first.'); return; }
    var minTotal = debts.reduce(function (s, d) { return s + d.min; }, 0);
    if (budget < minTotal) { alert('Your budget ($' + budget + ') is less than your minimum payments ($' + minTotal + ').'); return; }

    var av = avalanche(debts, budget);
    var sn = snowball(debts, budget);
    var saved = sn.totalInterest - av.totalInterest;

    var html = '<div class="result-hero">' +
      '<div><span class="big">' + av.months + '</span> months to debt-free</div>' +
      '<div>Debt-free by <strong>' + fmtDate(av.months) + '</strong></div>' +
      '<div>Total interest: <strong>$' + av.totalInterest.toLocaleString() + '</strong></div>' +
      '<div class="save">Avalanche saves you <strong>$' + saved.toLocaleString() + '</strong> vs snowball (' + sn.months + ' mo, $' + sn.totalInterest.toLocaleString() + ' interest)</div>' +
      '<div class="hint">Payoff order: ' + av.order.join(' → ') + '</div></div>';

    document.getElementById('summary').innerHTML = html;

    // First 12 months + last month of schedule
    var sched = av.schedule;
    var show = sched.slice(0, 12);
    if (sched.length > 13) show.push({ month: '…', payments: [], remaining: '…' });
    if (sched.length > 12) show.push(sched[sched.length - 1]);
    var t = '<table class="sched-table"><tr><th>Month</th><th>Payments</th><th>Remaining</th></tr>';
    show.forEach(function (m) {
      var pays = m.payments.map(function (p) { return p.name + ': $' + p.paid; }).join('<br>');
      t += '<tr><td>' + m.month + '</td><td>' + pays + '</td><td>$' + Number(m.remaining).toLocaleString() + '</td></tr>';
    });
    document.getElementById('schedule').innerHTML = t + '</table>';
    document.getElementById('results').style.display = 'block';
    document.getElementById('results').scrollIntoView({ behavior: 'smooth' });
  });

  render();
})();
