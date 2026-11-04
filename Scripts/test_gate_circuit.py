"""Unit tests for Scripts/gate_circuit.py (Trade Signal Gate v3.0,
circuit breaker on the alert tier).

The breaker state is computed from the timestamped event list the
engine carries in intraday.json['gate_events'] — the committed feed,
so it survives ephemeral runners. All functions are pure and never
raise; tests assert the trip / auto-restore / fail-open behavior and
the diagnostic-only scope for per-plan rejections.
"""

from datetime import datetime, timezone, timedelta

import gate_circuit as gc


def _t(minutes_ago, base=None):
    base = base or datetime.now(timezone.utc)
    return (base - timedelta(minutes=minutes_ago)).isoformat()


def _sys(minutes_ago, reason='MISSING_GATE_INPUTS'):
    return {'t': _t(minutes_ago), 'reason': reason, 'systemic': True}


def _plan(minutes_ago, reason='NET_RETURN_BELOW_FLOOR'):
    return {'t': _t(minutes_ago), 'reason': reason, 'systemic': False}


def test_three_systemic_failures_in_60min_disables():
    events = [_sys(50), _sys(30), _sys(5)]
    st = gc.status_from_events(events)
    assert st['publishing_disabled'] is True
    assert st['systemic_60min'] == 3
    assert st['reasons_60min'] == ['MISSING_GATE_INPUTS']


def test_two_systemic_failures_stays_enabled():
    events = [_sys(50), _sys(5)]
    st = gc.status_from_events(events)
    assert st['publishing_disabled'] is False
    assert st['systemic_60min'] == 2


def test_per_plan_rejections_never_trip():
    # Ten net-return rejections = the gate working as designed
    # (the 2.8% net floor filters most quiet coins) — diagnostic only.
    events = [_plan(m) for m in range(10)]
    st = gc.status_from_events(events)
    assert st['publishing_disabled'] is False
    assert st['systemic_60min'] == 0
    assert st['total_60min'] == 10


def test_auto_restore_after_window_slides():
    # Three systemic failures, but the window has moved on — publishing
    # re-enables with no manual reset.
    events = [_sys(61), _sys(70), _sys(120)]
    st = gc.status_from_events(events)
    assert st['publishing_disabled'] is False
    assert st['systemic_60min'] == 0
    # And two fresh ones still don't trip it.
    events = [_sys(61), _sys(70), _sys(120), _sys(5), _sys(2)]
    assert gc.status_from_events(events)['publishing_disabled'] is False


def test_fail_open_without_events():
    st = gc.status_from_events([])
    assert st['publishing_disabled'] is False
    st = gc.status_from_events(None)
    assert st['publishing_disabled'] is False


def test_corrupt_events_fail_open():
    st = gc.status_from_events([{'bogus': 1}, None, 'nope', {'t': 'junk'}])
    assert st['publishing_disabled'] is False


def test_old_events_pruned_from_retention():
    events = [_sys(5), {'t': _t(60 * 25), 'reason': 'MISSING_GATE_INPUTS',
                       'systemic': True}]
    kept = gc.prune_events(events)
    assert len(kept) == 1
    assert gc.status_from_events(kept)['systemic_60min'] == 1


def test_record_event_appends_and_caps():
    events = gc.record_event([], 'MISSING_GATE_INPUTS', systemic=True)
    assert len(events) == 1
    assert events[0]['reason'] == 'MISSING_GATE_INPUTS'
    assert events[0]['systemic'] is True
    assert 't' in events[0]
    # Never raises on garbage input — a None reason is recorded as-is
    # rather than blowing up the caller.
    ev = gc.record_event(None, None)
    assert len(ev) == 1 and ev[0]['reason'] == 'None'
    # A non-list events arg is tolerated too — prune keeps the new event.
    ev = gc.record_event('junk', 'x')
    assert len(ev) == 1 and ev[0]['reason'] == 'x'


def test_systemic_reason_inferred_from_reason_name():
    # An event marked systemic=False but with a systemic reason name
    # still counts — belt and suspenders.
    events = [_sys(5, 'STALE_QUOTE'), _sys(6, 'STALE_QUOTE'),
              {'t': _t(7), 'reason': 'STALE_QUOTE', 'systemic': False}]
    st = gc.status_from_events(events)
    assert st['systemic_60min'] == 3
    assert st['publishing_disabled'] is True


def test_status_never_raises():
    st = gc.status_from_events(object())
    assert st['publishing_disabled'] is False
    st = gc.prune_events(object())
    assert st == []
