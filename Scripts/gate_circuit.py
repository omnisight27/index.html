"""Circuit breaker for the intraday alert tier (Trade Signal Gate v3.0,
Nic-approved 2026-11-04).

Tracks gate rejections as timestamped events. Trips only on SYSTEMIC
failures — the gate itself being unable to do its job (missing cost
data, stale market data, unsupported order types, exchange unavailable).
Per-plan net-return / reward-risk / gross-floor rejections are the gate
working as designed — the 2.8% net floor filters most quiet coins by
construction — so they are recorded for diagnostics but do NOT trip the
breaker. Counting them would trip the breaker on every normal run and
permanently disable alerts.

Trip condition: >= BREAKER_THRESHOLD systemic failures within a trailing
BREAKER_WINDOW_MINUTES window -> publishing_disabled = True.

Auto-restore: the window slides. Once the newest systemic failure ages
past 60 minutes the trailing count drops below threshold and publishing
re-enables on its own — no manual reset, no separate timer.

Persistence: events ride inside data/intraday.json under 'gate_events'
(the file the workflow commits every run — the same trick the Proof Book
ledger and the accumulation history use), and the computed breaker state
is stamped under 'gate_circuit' for the alert selector. No separate
state file: on ephemeral runners an uncommitted file would not survive
between runs.

Never silently drops: a disabled breaker must be visible. The alert
selector (workspace scripts/intraday-sms.py) refuses to publish buy
cards while disabled and logs PUBLISHING_DISABLED with the reason and
counts.

Fail-open: no events (or corrupt ones) means "no known failures" —
publishing enabled. All functions are pure and never raise.
"""

from datetime import datetime, timezone, timedelta

BREAKER_THRESHOLD = 3          # systemic failures ...
BREAKER_WINDOW_MINUTES = 60    # ... within this trailing window -> trip
EVENT_RETENTION_HOURS = 24     # prune events older than this
MAX_EVENTS = 500               # cap the carried event list

# Systemic = the gate cannot do its job. Everything else is normal
# per-plan filtering and is diagnostic-only.
SYSTEMIC_REASONS = frozenset({
    'MISSING_GATE_INPUTS',
    'STALE_QUOTE',
    'STALE_SIGNAL',
    'ORDER_TYPE_UNSUPPORTED',
    'EXCHANGE_UNAVAILABLE',
})


def _now():
    return datetime.now(timezone.utc)


def _parse_t(t, now):
    try:
        d = datetime.fromisoformat(t)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return now


def record_event(events, reason, systemic=False, now=None):
    """Return a new event list with one rejection appended. Pure — the
    caller owns persistence (the engine carries these in intraday.json).
    Never raises."""
    try:
        now = now or _now()
        ev = list(events or [])
        ev.append({'t': now.isoformat(),
                   'reason': str(reason),
                   'systemic': bool(systemic)})
        return prune_events(ev, now=now)
    except Exception:
        try:
            return list(events or [])
        except Exception:
            return []


def prune_events(events, now=None, retention_hours=EVENT_RETENTION_HOURS):
    """Drop malformed events and anything older than the retention
    window; cap the list length. Never raises."""
    try:
        now = now or _now()
        cutoff = now - timedelta(hours=retention_hours)
        kept = [e for e in (events or [])
                if isinstance(e, dict) and isinstance(e.get('t'), str)
                and _parse_t(e['t'], now) >= cutoff]
        return kept[-MAX_EVENTS:]
    except Exception:
        return []


def status_from_events(events, now=None):
    """Current breaker state from an event list. Fail-open: no (valid)
    events -> publishing enabled."""
    try:
        now = now or _now()
        window_start = now - timedelta(minutes=BREAKER_WINDOW_MINUTES)
        recent = [e for e in prune_events(events, now=now)
                  if _parse_t(e.get('t'), now) >= window_start]
        systemic = [e for e in recent
                    if e.get('systemic') or e.get('reason') in SYSTEMIC_REASONS]
        reasons = sorted({e.get('reason') for e in systemic
                          if e.get('reason')})
        newest = None
        for e in systemic:
            t = _parse_t(e.get('t'), now)
            if newest is None or t > newest:
                newest = t
        return {
            'publishing_disabled': len(systemic) >= BREAKER_THRESHOLD,
            'systemic_60min': len(systemic),
            'total_60min': len(recent),
            'reasons_60min': reasons,
            'newest_systemic_failure': (newest.isoformat()
                                        if newest else None),
            'threshold': BREAKER_THRESHOLD,
            'window_minutes': BREAKER_WINDOW_MINUTES,
        }
    except Exception:
        return {'publishing_disabled': False, 'systemic_60min': 0,
                'total_60min': 0, 'reasons_60min': [],
                'newest_systemic_failure': None,
                'threshold': BREAKER_THRESHOLD,
                'window_minutes': BREAKER_WINDOW_MINUTES}
