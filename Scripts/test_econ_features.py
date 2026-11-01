"""Tests for Scripts/econ_features.py — the milestone-2 econ feature pack.

Covers: zone math on synthetic candles, cluster detection, predecessor
momentum sign, macro parsing, fail-open on missing data, and the fixed
variable order the significance ranking depends on.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import econ_features as ef


def test_round_number_levels():
    lv = ef.round_number_levels(77300)
    assert 80000 in lv and 77000 in lv, lv
    assert all(l > 0 for l in lv)
    assert ef.round_number_levels(0) == []
    assert ef.round_number_levels(None) == []


def test_stop_cluster_equal_highs():
    # Two equal swing highs at 100 -> cluster; price just under it.
    closes = [95, 96, 98, 100, 98, 96, 97, 99, 100, 98, 96, 95,
              96, 97, 98, 99, 100, 98, 97, 96, 95, 96, 97, 98]
    vol = ef._rms_vol_pct(closes)
    d, inside = ef.stop_distance(99.9, closes, vol)
    assert d is not None and d < 1.0, (d, inside)
    d2, inside2 = ef.stop_distance(100.0, closes, vol)
    assert inside2 == 1.0, (d2, inside2)  # sitting in the cluster: squeeze risk


def test_stop_distance_no_vol_fails_open():
    d, inside = ef.stop_distance(100.0, None, None)
    assert d is None and inside == 0.0


def test_volume_profile_zone():
    # Heavy volume traded at ~95 -> demand zone near 95.
    closes = [95 + (i % 3) * 0.2 for i in range(24)]
    volumes = [1000 if abs(c - 95) < 0.5 else 10 for c in closes]
    vol = ef._rms_vol_pct(closes)
    s_d, d_d, at_s, at_d = ef.volume_profile_zones(closes, volumes, 96.0, vol)
    assert d_d is not None and d_d < 3.0, (s_d, d_d)
    assert s_d is None  # no zone above 96 in this synthetic series
    # No data -> fail open
    assert ef.volume_profile_zones([], [], 96.0, vol) == (None, None, 0.0, 0.0)
    assert ef.volume_profile_zones(closes, volumes, 96.0, 0) == (None, None, 0.0, 0.0)


def test_crowdedness():
    assert ef.crowdedness(0.9) == 0.9
    assert ef.crowdedness(-0.4) == 0.4
    assert ef.crowdedness(None) is None


def test_history_delta_sign():
    now = 1_000_000.0
    hist = {'BTC': [[now - 7 * 3600, 0.1], [now - 6 * 3600, 0.2],
                    [now - 3600, 0.5], [now, 0.8]]}
    d = ef.history_delta(hist, 'BTC', now, 6 * 3600)
    assert d is not None and d > 0, d  # funding score rose -> positive
    # Too few points -> None
    assert ef.history_delta({'BTC': [[now, 0.5]]}, 'BTC', now, 3600) is None
    assert ef.history_delta({}, 'BTC', now, 3600) is None


def test_oi_acceleration_sign():
    now = 2_000_000.0
    # OI inflow accelerating: +1%/h three hours ago, +5%/h now.
    hist = {'ETH': [[now - 4 * 3600, 100.0], [now - 3 * 3600, 101.0],
                    [now - 3600, 104.0], [now, 109.2]]}
    a = ef.oi_acceleration(hist, 'ETH', now)
    assert a is not None and a > 0, a
    assert ef.oi_acceleration({}, 'ETH', now) is None


def test_parse_fred_csv():
    text = "observation_date,DFF\n2026-10-28,3.88\n2026-10-29,.\n2026-10-30,3.90\n"
    obs = ef.parse_fred_csv(text)
    assert obs == [('2026-10-28', 3.88), ('2026-10-30', 3.90)], obs


def test_monetary_from_snapshot():
    snap = {'DGS10': {'value': 5.05}, 'CPIAUCSL': {'yoy': 3.7},
            'DFF': {'value': 3.88, 'd90': 0.25}}
    ry, fs = ef.monetary_from_snapshot(snap)
    assert abs(ry - (5.05 - 3.7) / 3.0) < 1e-9, (ry, fs)
    assert fs == 0.25, (ry, fs)
    assert ef.monetary_from_snapshot(None) == (None, None)
    assert ef.monetary_from_snapshot({}) == (None, None)


def test_liquidity_regime_bounds():
    r = ef.liquidity_regime(btc_chg24=25.0, mean_abs_funding=1.0, fg_now=100)
    assert r is not None and -1 <= r <= 1, r
    r2 = ef.liquidity_regime(btc_chg24=-25.0, mean_abs_funding=1.0, fg_now=0)
    assert r2 is not None and r2 < 0, r2
    assert ef.liquidity_regime() is None


def test_compute_econ_fail_open():
    vals, disp = ef.compute_econ({})
    assert len(vals) == ef.N_ECON == 13
    assert disp['tags'] == []
    vals2, _ = ef.compute_econ(None)
    assert all(v is None or v == 0.0 for v in vals2)


def test_compute_econ_coil_passthrough_and_tags():
    vals, disp = ef.compute_econ({'mode': 'coarse', 'price': 80000.0,
                                  'windows': {'r1h': 0.5, 'r24h': 6.0, 'r7d': 12.0,
                                              'r14d': 5.0, 'r30d': -3.0},
                                  'coil_delta': 0.3})
    assert len(vals) == 13
    assert vals[9] == 0.3, vals
    assert 'Coiling up' in disp['tags'], disp['tags']
    assert vals[0] is not None  # stop_dist from round numbers works coarse


def test_feature_order_stable():
    assert ef.ECON_FEATURE_NAMES == [
        'stop_dist', 'squeeze_risk', 'crowded',
        'supply_dist', 'demand_dist', 'at_supply', 'at_demand',
        'fund_mom', 'oi_mom', 'coil_delta',
        'real_yield', 'fed_stance', 'liq_regime',
    ]
    assert ef.N_ECON == len(ef.ECON_FEATURE_NAMES) == 13
