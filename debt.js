// OmniSight Debt Payoff Optimizer v2 — debt types, amortization, due dates.
// Types: credit (revolving), installment (auto/personal), mortgage (amortized from start date).
(function () {
  'use strict';
  var debts = [];

  function esc(s) { return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'); }

  function debtRow(d, i) {
    d = d || {};
    var div = document.createElement('div');
    div.className = 'debt-row debt-card';
    var type = d.type || 'credit';
    var html =
      '<div class="debt-grid">' +
      '<input type="text" placeholder="Name (e.g. Visa)" value="' + esc(d.name) + '" data-f="name">' +
      '<select data-f="type">' +
        '<option value="credit"' + (type==='credit'?' selected':'') + '>💳 Credit Card</option>' +
        '<option value="installment"' + (type==='installment'?' selected':'') + '>🚗 Installment Loan</option>' +
        '<option value="mortgage"' + (type==='mortgage'?' selected':'') + '>🏠 Mortgage</option>' +
      '</select>' +
      '<input type="number" placeholder="Balance $" value="' + esc(d.balance) + '" min="0" step="1" data-f="balance">' +
      '<input type="number" placeholder="APR %" value="' + esc(d.apr) + '" min="0" step="0.01" data-f="apr">' +
      '<input type="number" placeholder="Min payment $" value="' + esc(d.min) + '" min="0" step="1" data-f="min">' +
      '<input type="number" placeholder="Due day (1-31)" value="' + esc(d.due) + '" min="1" max="31" data-f="due">' +
      '</div>' +
      '<div class="debt-extra" data-extra="credit" style="display:' + (type==='credit'?'':'none') + '">' +
        '<label><input type="checkbox" data-f="pif"' + (d.pif?' checked':'') + '> Paid in full each month (no interest)</label>' +
      '</div>' +
      '<div class="debt-extra" data-extra="installment" style="display:' + (type==='installment'?'':'none') + '">' +
        '<input type="number" placeholder="Original amount $" value="' + esc(d.orig) + '" min="0" step="1" data-f="orig">' +
        '<input type="number" placeholder="Term (months)" value="' + esc(d.term) + '" min="1" step="1" data-f="term">' +
        '<input type="month" value="' + esc(d.start) + '" data-f="start" title="Start date">' +
      '</div>' +
      '<div class="debt-extra" data-extra="mortgage" style="display:' + (type==='mortgage'?'':'none') + '">' +
        '<input type="number" placeholder="Original amount $" value="' + esc(d.orig) + '" min="0" step="1" data-f="orig">' +
        '<input type="number" placeholder="Term (years)" value="' + esc(d.termy) + '" min="1" step="1" data-f="termy">' +
        '<input type="month" value="' + esc(d.start) + '" data-f="start" title="Start date">' +
      '</div>' +
      '<button class="btn-small" data-del="' + i + '">✕ Remove</button>';
    div.innerHTML = html;
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

  // Persist to localStorage
  function save() {
    readRows();
    try {
      localStorage.setItem('nx_debts', JSON.stringify(debts));
      var budget = document.getElementById('budget').value;
      localStorage.setItem('nx_debt_budget', budget);
    } catch (e) {}
  }

  function load() {
    try {
      var saved = localStorage.getItem('nx_debts');
      if (saved) debts = JSON.parse(saved);
      var b = localStorage.getItem('nx_debt_budget');
      if (b) document.getElementById('budget').value = b;
    } catch (e) {}
  }

  // Auto-save on any input change (debounced)
  var saveTimer = null;
  document.getElementById('debt-list').addEventListener('input', function () {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 800);
  });
  document.getElementById('debt-list').addEventListener('change', function (e) {
    if (e.target.dataset.f === 'type') {
      var row = e.target.closest('.debt-row');
      var t = e.target.value;
      row.querySelectorAll('[data-extra]').forEach(function (el) {
        el.style.display = el.dataset.extra === t ? '' : 'none';
      });
    }
    save();
  });

  function readRows() {
    var rows = document.querySelectorAll('.debt-row');
    debts = [];
    rows.forEach(function (r) {
      var d = { type: 'credit' };
      r.querySelectorAll('input, select').forEach(function (inp) {
        var f = inp.dataset.f;
        if (!f) return;
        d[f] = inp.type === 'checkbox' ? inp.checked : inp.value;
      });
      d.balance = parseFloat(d.balance) || 0;
      d.apr = parseFloat(d.apr) || 0;
      d.min = parseFloat(d.min) || 0;
      d.due = parseInt(d.due, 10) || 1;
      d.orig = parseFloat(d.orig) || 0;
      d.term = parseInt(d.term, 10) || 0;
      d.termy = parseInt(d.termy, 10) || 0;
      if (d.balance > 0) debts.push(d);
    });
  }

  // Amortization: monthly payment for a loan
  function amortPayment(principal, apr, nMonths) {
    if (!principal || !nMonths) return 0;
    var r = apr / 100 / 12;
    if (r <= 0) return principal / nMonths;
    return principal * r * Math.pow(1 + r, nMonths) / (Math.pow(1 + r, nMonths) - 1);
  }

  // Remaining balance on an amortized loan given months elapsed
  function amortBalance(principal, apr, nMonths, elapsed) {
    var pmt = amortPayment(principal, apr, nMonths);
    var r = apr / 100 / 12;
    var bal = principal;
    for (var i = 0; i < elapsed && bal > 0.01; i++) {
      bal += bal * r;
      bal -= Math.min(pmt, bal);
    }
    return Math.max(bal, 0);
  }

  // Normalize debts: compute effective min payment for amortized types
  function normalize(ds) {
    var now = new Date();
    return ds.map(function (d) {
      var n = { name: d.name || 'Debt', type: d.type, balance: d.balance, apr: d.apr,
                min: d.min, due: d.due, pif: !!d.pif };
      if ((d.type === 'mortgage' || d.type === 'installment')) {
        var months = d.type === 'mortgage' ? (d.termy * 12) : d.term;
        if (d.orig > 0 && months > 0) {
          n.scheduledPmt = amortPayment(d.orig, d.apr, months);
          // If start date given, compute elapsed and adjust balance
          if (d.start) {
            var sd = new Date(d.start + '-01');
            var elapsed = (now.getFullYear() - sd.getFullYear()) * 12 + (now.getMonth() - sd.getMonth());
            if (elapsed > 0) {
              n.balance = amortBalance(d.orig, d.apr, months, Math.min(elapsed, months));
              n.monthsElapsed = elapsed;
            }
            n.termMonths = months;
            n.startDate = d.start;
          }
          // Use scheduled payment as minimum if not specified
          if (!n.min) n.min = Math.round(n.scheduledPmt * 100) / 100;
        }
      }
      return n;
    });
  }

  // Avalanche simulation (type-aware)
  function avalanche(rawDebts, budget) {
    var ds = normalize(rawDebts).sort(function (a, b) { return b.apr - a.apr; });
    // Paid-in-full cards: no interest, paid fully each month (excluded from extra allocation)
    var schedule = [], totalInterest = 0, month = 0;
    while (ds.some(function (d) { return d.balance > 0.01; }) && month < 600) {
      month++;
      var m = { month: month, payments: [], dueDates: [] };
      ds.forEach(function (d) {
        if (d.balance <= 0.01) return;
        var interest = 0;
        if (!(d.type === 'credit' && d.pif)) {
          interest = d.balance * (d.apr / 100 / 12);
          totalInterest += interest;
          d.balance += interest;
        }
        var pay = Math.min(d.type === 'credit' && d.pif ? d.balance : d.min, d.balance);
        d.balance -= pay;
        m.payments.push({ name: d.name, paid: Math.round(pay * 100) / 100, due: d.due,
                          type: d.type, interest: Math.round(interest * 100) / 100 });
      });
      var paid_min = m.payments.reduce(function (s, p) { return s + p.paid; }, 0);
      var extra = budget - paid_min;
      // Extra goes to highest-APR non-PIF debt
      for (var i = 0; i < ds.length && extra > 0.01; i++) {
        var d = ds[i];
        if (d.balance <= 0.01 || (d.type === 'credit' && d.pif)) continue;
        var x = Math.min(extra, d.balance);
        d.balance -= x; extra -= x;
        var mp = m.payments.filter(function (p) { return p.name === d.name; })[0];
        if (mp) mp.paid = Math.round((mp.paid + x) * 100) / 100;
      }
      m.remaining = Math.round(ds.reduce(function (s, d) { return s + Math.max(d.balance, 0); }, 0));
      schedule.push(m);
    }
    return { months: month, totalInterest: Math.round(totalInterest), schedule: schedule,
             order: ds.map(function (d) { return d.name; }), debts: ds };
  }

  function snowball(rawDebts, budget) {
    var ds = normalize(rawDebts).sort(function (a, b) { return a.balance - b.balance; });
    var totalInterest = 0, month = 0;
    while (ds.some(function (d) { return d.balance > 0.01; }) && month < 600) {
      month++;
      ds.forEach(function (d) {
        if (d.balance <= 0.01) return;
        if (!(d.type === 'credit' && d.pif)) {
          var interest = d.balance * (d.apr / 100 / 12);
          totalInterest += interest;
          d.balance += interest;
        }
        d.balance -= Math.min(d.type === 'credit' && d.pif ? d.balance : d.min, d.balance);
      });
      var extra = budget - ds.reduce(function (s, d) { return s + (d.type === 'credit' && d.pif ? 0 : d.min); }, 0);
      for (var i = 0; i < ds.length && extra > 0.01; i++) {
        var d = ds[i];
        if (d.balance <= 0.01 || (d.type === 'credit' && d.pif)) continue;
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

  function typeIcon(t) {
    return t === 'mortgage' ? '🏠' : t === 'installment' ? '🚗' : '💳';
  }

  document.getElementById('add-debt').addEventListener('click', function () {
    readRows();
    debts.push({ name: '', type: 'credit', balance: '', apr: '', min: '', due: '' });
    render();
  });

  document.getElementById('debt-list').addEventListener('click', function (e) {
    if (e.target.dataset.del !== undefined) {
      readRows();
      debts.splice(parseInt(e.target.dataset.del, 10), 1);
      save();
      render();
    }
  });

  document.getElementById('budget').addEventListener('input', function () {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 800);
  });

  document.getElementById('calc').addEventListener('click', function () {
    readRows();
    var budget = parseFloat(document.getElementById('budget').value) || 0;
    if (!debts.length) { alert('Add at least one debt first.'); return; }
    var norm = normalize(debts);
    var minTotal = norm.reduce(function (s, d) { return s + (d.type === 'credit' && d.pif ? 0 : d.min); }, 0);
    if (budget < minTotal) { alert('Your budget ($' + budget + ') is less than your minimum payments ($' + Math.round(minTotal) + ').'); return; }

    var av = avalanche(debts, budget);
    var sn = snowball(debts, budget);
    var saved = sn.totalInterest - av.totalInterest;

    // Mortgage amortization info
    var amortInfo = '';
    norm.forEach(function (d) {
      if ((d.type === 'mortgage' || d.type === 'installment') && d.scheduledPmt) {
        amortInfo += '<div class="hint">' + typeIcon(d.type) + ' ' + esc(d.name) +
          ': scheduled payment $' + d.scheduledPmt.toFixed(2) + '/mo' +
          (d.monthsElapsed ? ' · ' + d.monthsElapsed + ' payments in' : '') +
          (d.startDate ? ' · started ' + d.startDate : '') + '</div>';
      }
      if (d.type === 'credit' && d.pif) {
        amortInfo += '<div class="hint">💳 ' + esc(d.name) + ': paid in full monthly — no interest charged.</div>';
      }
    });

    var html = '<div class="result-hero">' +
      '<div><span class="big">' + av.months + '</span> months to debt-free</div>' +
      '<div>Debt-free by <strong>' + fmtDate(av.months) + '</strong></div>' +
      '<div>Total interest: <strong>$' + av.totalInterest.toLocaleString() + '</strong></div>' +
      '<div class="save">Avalanche saves you <strong>$' + saved.toLocaleString() + '</strong> vs snowball (' + sn.months + ' mo, $' + sn.totalInterest.toLocaleString() + ' interest)</div>' +
      '<div class="hint">Payoff order: ' + av.order.join(' → ') + '</div>' + amortInfo + '</div>';

    document.getElementById('summary').innerHTML = html;

    var sched = av.schedule;
    var show = sched.slice(0, 12);
    if (sched.length > 13) show.push({ month: '…', payments: [], remaining: '…' });
    if (sched.length > 12) show.push(sched[sched.length - 1]);
    var t = '<table class="sched-table"><tr><th>Mo</th><th>Payments (due day)</th><th>Remaining</th></tr>';
    show.forEach(function (m) {
      var pays = m.payments.map(function (p) {
        return typeIcon(p.type) + ' ' + esc(p.name) + ': $' + p.paid.toLocaleString() +
               ' <span class="hint">(due ' + p.due + 'th)</span>';
      }).join('<br>');
      t += '<tr><td>' + m.month + '</td><td>' + pays + '</td><td>$' + Number(m.remaining).toLocaleString() + '</td></tr>';
    });
    document.getElementById('schedule').innerHTML = t + '</table>';
    document.getElementById('results').style.display = 'block';
    document.getElementById('results').scrollIntoView({ behavior: 'smooth' });
  });

  load();
  render();
})();