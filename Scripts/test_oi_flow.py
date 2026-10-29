"""Feed #3 — open-interest flow. Pure-function tests; history is passed in
as plain dicts, no disk, no network."""
import math

import pytest

import oi_flow

NOW = 1_800_000_000.0


def _hist(oi_now, oi_1h_ago, oi_24h_ago=None, step=3600.0):
    """Build a plausible hourly history for one asset."""
    pts = []
    if oi_24h_ago is not None:
        # 24 hourly points ending 1h ago, then the current one
        for i in range(24, 0, -1):
            oi = oi_24h_ago + (oi_1h_ago - oi_24h_ago) * (24 - i) / 23
            pts.append([NOW - i * step, oi])
    else:
        pts.append([NOW - step, oi_1h_ago])
    pts.append([NOW, oi_now])
    return {'BTC': pts}


def test_quadrants():
    assert oi_flow.quadrant_score(10, 2) == 1.0    # new longs, healthy
    assert oi_flow.quadrant_score(10, -2) == -0.8  # positioning into weakness
    assert oi_flow.quadrant_score(-10, 2) == -0.4  # short covering, may exhaust
    assert oi_flow.quadrant_score(-10, -2) == 0.5  # capitulation, exhausting


def test_flow_new_longs_fueling():
    h = _hist(oi_now=115, oi_1h_ago=100, oi_24h_ago=90)
    f = oi_flow.flow_for('BTC', h, chg_1h=2.0, now_ts=NOW)
    assert f['oi_chg_1h_pct'] == pytest.approx(15.0)
    assert f['oi_chg_24h_pct'] == pytest.approx(115 / 90 * 100 - 100, abs=0.5)
    assert f['score'] == pytest.approx(1.0 * math.tanh(15 / 15), abs=1e-3)
    assert f['score'] > 0.6  # strong enough for a card line


def test_flow_positioning_against_the_move():
    h = _hist(oi_now=120, oi_1h_ago=100)
    f = oi_flow.flow_for('BTC', h, chg_1h=-1.5, now_ts=NOW)
    assert f['score'] < -0.6  # fragile, veto territory


def test_flow_tiny_wiggle_scores_near_zero():
    h = _hist(oi_now=100.5, oi_1h_ago=100)
    f = oi_flow.flow_for('BTC', h, chg_1h=2.0, now_ts=NOW)
    assert abs(f['score']) < 0.05


def test_flow_missing_history_is_none():
    assert oi_flow.flow_for('BTC', {}, chg_1h=2.0, now_ts=NOW) is None
    assert oi_flow.flow_for('ETH', _hist(115, 100), chg_1h=2.0,
                            now_ts=NOW) is None
    # Single point: no window to compare against
    assert oi_flow.flow_for('BTC', {'BTC': [[NOW, 100]]}, chg_1h=2.0,
                            now_ts=NOW) is None
    assert oi_flow.flow_for('BTC', _hist(115, 100), chg_1h=None,
                            now_ts=NOW) is None


def test_flow_stale_gap_fails_open():
    # Last base reading is 5h old — a gap, not a trend.
    h = {'BTC': [[NOW - 5 * 3600, 100], [NOW, 115]]}
    assert oi_flow.flow_for('BTC', h, chg_1h=2.0, now_ts=NOW) is None


def test_update_history_appends_and_caps():
    fund = {'BTC': {'oi': 1000.0}, 'ETH': {'oi': None}, 'XRP': {}}
    h1 = oi_flow.update_oi_history({}, fund, NOW)
    assert h1['BTC'] == [[NOW, 1000.0]]
    assert 'ETH' not in h1 and 'XRP' not in h1
    h2 = oi_flow.update_oi_history(h1, {'BTC': {'oi': 1010.0}}, NOW + 3600)
    assert len(h2['BTC']) == 2
    # Cap: 40 appends keep only the last 30
    h = {}
    for i in range(40):
        h = oi_flow.update_oi_history(h, {'BTC': {'oi': float(i)}},
                                      NOW + i * 3600)
    assert len(h['BTC']) == oi_flow.MAX_POINTS
    assert h['BTC'][0][1] == 10.0
    # Input dict not mutated
    assert h1['BTC'] == [[NOW, 1000.0]]


def test_runner_ok_gate():
    assert oi_flow.runner_ok(None) is True
    assert oi_flow.runner_ok(0.5) is True
    assert oi_flow.runner_ok(-0.69) is True
    assert oi_flow.runner_ok(-0.7) is False
    assert oi_flow.runner_ok(-0.9) is False


def test_card_line():
    assert oi_flow.card_line(None) is None
    assert oi_flow.card_line({'score': 0.2, 'oi_chg_1h_pct': 5}) is None
    assert oi_flow.card_line(
        {'score': 0.76, 'oi_chg_1h_pct': 15.0}) == \
        "OI +15%/1h with the move — new money fueling it"
    assert oi_flow.card_line(
        {'score': -0.75, 'oi_chg_1h_pct': 20.0}) == \
        "OI +20%/1h against the move — fragile"
