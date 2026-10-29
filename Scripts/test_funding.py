"""Feed #2 — Kraken Futures funding rates. Pure-function tests plus a
no-network fetch test (fetch_json is rigged to explode on any unexpected
call)."""
import sys

import pytest

import funding


def test_annualized_math():
    # 0.0001 per 4h -> ~21.9% annualized
    assert funding.annualized_pct(0.0001) == pytest.approx(21.9, abs=0.01)
    assert funding.annualized_pct(0) == 0
    # Real Kraken shape: 1.94e-05 -> ~+4.26%/yr (BTC, 2026-10-29)
    assert funding.annualized_pct(1.9437e-05) == pytest.approx(4.26, abs=0.05)


def test_score_baseline_is_zero():
    assert funding.funding_score(15.0) == pytest.approx(0.0, abs=1e-9)


def test_score_crowded_longs_negative():
    s = funding.funding_score(100.0)
    assert s < -0.9  # deeply crowded -> near -1
    assert funding.funding_score(55.0) < -0.5  # past the suppression line


def test_score_crowded_shorts_positive():
    s = funding.funding_score(-20.0)
    assert s > 0.5  # squeeze-up potential


def test_score_none_is_none():
    assert funding.funding_score(None) is None


def test_kraken_symbol_mapping():
    assert funding.kraken_symbol('BTC') == 'PF_XBTUSD'  # Kraken's XBT quirk
    assert funding.kraken_symbol('eth') == 'PF_ETHUSD'
    assert funding.kraken_symbol('HYPE') == 'PF_HYPEUSD'


def _fake_fetch(payload):
    def fetch(url, timeout=30):
        assert 'futures.kraken.com' in url
        return payload
    return fetch


def test_fetch_funding_maps_bulk_tickers():
    payload = {'result': 'success', 'tickers': [
        {'symbol': 'PF_XBTUSD', 'relativeFundingRate': 1.9437e-05,
         'openInterest': 2749.0},
        {'symbol': 'PF_ETHUSD', 'relativeFundingRate': -2.0e-05,
         'openInterest': 31000.0},
        {'symbol': 'XXBTZUSD', 'relativeFundingRate': 0.5},  # spot, skipped
        {'symbol': 'PF_DOGEUSD', 'relativeFundingRate': 'nonsense'},  # skip
    ]}
    out = funding.fetch_funding(_fake_fetch(payload))
    assert set(out) == {'PF_XBTUSD', 'PF_ETHUSD'}
    assert out['PF_XBTUSD']['ann_pct'] == pytest.approx(4.26, abs=0.05)
    assert out['PF_XBTUSD']['oi'] == 2749.0
    assert out['PF_ETHUSD']['ann_pct'] == pytest.approx(-4.38, abs=0.05)


def test_fetch_funding_failure_fails_open():
    # fetch_json-style failure: fetch_funding must never raise
    assert funding.fetch_funding(lambda u, timeout=30: None) == {}
    assert funding.fetch_funding(_fake_fetch({'result': 'error'})) == {}
    assert funding.fetch_funding(_fake_fetch('not-a-dict')) == {}


def test_funding_by_asset_maps_engine_symbols():
    payload = {'result': 'success', 'tickers': [
        {'symbol': 'PF_XBTUSD', 'relativeFundingRate': 2.5e-04,
         'openInterest': 2700.0},   # ~+54.75%/yr -> crowded
        {'symbol': 'PF_HYPEUSD', 'relativeFundingRate': 7.05e-05,
         'openInterest': 260000.0},  # ~+15.43%/yr -> near baseline
    ]}
    out = funding.funding_by_asset({'BTC', 'HYPE', 'NOSUCHCOIN'},
                                   _fake_fetch(payload))
    assert set(out) == {'BTC', 'HYPE'}
    assert out['BTC']['score'] < -0.5
    assert out['BTC']['ann_pct'] == pytest.approx(54.75, abs=0.1)
    assert -0.05 < out['HYPE']['score'] < 0.05


def test_runner_allowed_gate():
    assert funding.runner_allowed(None) is True      # missing data fails open
    assert funding.runner_allowed(0.0) is True
    assert funding.runner_allowed(-0.49) is True
    assert funding.runner_allowed(-0.51) is False
    assert funding.runner_allowed(-0.95) is False
