"""Tests for the SAM.gov contracting pulse (2026-10-25): the
deciding-factor view of government contracting. Individual
solicitations never change a buy or sell call, so the pages show the
pulse — 7-day volume, most-active agencies, direction vs ~24h ago —
not the raw list."""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nexintel


def _rec(agency):
    return {'agency': agency, 'title': 'x', 'notice_id': 'n'}


def _hist(tmp, hours_ago, total):
    t = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    with open(tmp, 'w') as f:
        json.dump([{'t': t, 'total': total}], f)
    return tmp


def test_pulse_uses_total_records():
    p = nexintel.build_sam_pulse(
        {'totalRecords': 12345, 'opportunitiesData': []},
        [_rec('Dept Of Defense'), _rec('Dept Of Defense'), _rec('Energy, Department Of')],
        history_path=os.path.join(tempfile.mkdtemp(), 'pulse.json'))
    assert p['total'] == 12345, p
    assert p['total_is_sample'] is False, p
    assert p['top_agencies'][0] == {'agency': 'Dept Of Defense', 'count': 2}, p
    assert p['direction'] == 'first', p  # no history yet


def test_pulse_falls_back_to_sampled_count():
    p = nexintel.build_sam_pulse(
        {}, [_rec('A'), _rec('B')],
        history_path=os.path.join(tempfile.mkdtemp(), 'pulse.json'))
    assert p['total'] == 2, p
    assert p['total_is_sample'] is True, p


def test_pulse_direction_rising_cooling_flat():
    tmp = os.path.join(tempfile.mkdtemp(), 'pulse.json')
    _hist(tmp, 24, 1000)
    p = nexintel.build_sam_pulse({'totalRecords': 1200}, [], history_path=tmp)
    assert p['direction'] == 'rising' and p['direction_pct'] == 20.0, p
    _hist(tmp, 24, 1000)
    p = nexintel.build_sam_pulse({'totalRecords': 800}, [], history_path=tmp)
    assert p['direction'] == 'cooling' and p['direction_pct'] == -20.0, p
    _hist(tmp, 24, 1000)
    p = nexintel.build_sam_pulse({'totalRecords': 1030}, [], history_path=tmp)
    assert p['direction'] == 'flat' and p['direction_pct'] == 3.0, p


def test_pulse_needs_12h_old_reference():
    tmp = os.path.join(tempfile.mkdtemp(), 'pulse.json')
    _hist(tmp, 6, 1000)  # only 6h ago — too fresh to call a direction
    p = nexintel.build_sam_pulse({'totalRecords': 2000}, [], history_path=tmp)
    assert p['direction'] == 'first', p


def test_pulse_never_raises():
    p = nexintel.build_sam_pulse(None, None, history_path='/nonexistent/dir/pulse.json')
    assert p['total'] == 0 and p['direction'] in ('unknown', 'first'), p
    p = nexintel.build_sam_pulse({'totalRecords': 'not-a-number'}, [_rec(None)],
                                 history_path=os.path.join(tempfile.mkdtemp(), 'pulse.json'))
    assert p['total'] == 1, p  # fell back to the sampled rows
    json.dumps(p)  # serializable for latest.json


def test_pulse_history_caps_at_28():
    d = tempfile.mkdtemp()
    tmp = os.path.join(d, 'pulse.json')
    for _ in range(35):
        nexintel.build_sam_pulse({'totalRecords': 100}, [], history_path=tmp)
    with open(tmp) as f:
        hist = json.load(f)
    assert len(hist) == 28, len(hist)
