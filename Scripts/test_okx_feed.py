"""OKX derivatives feed. Pure-function tests + fake-fetch injection;
no disk, no network."""
import math

import pytest

import okx_feed

NOW = 1_800_000_000.0

# --- Saved sample responses (real OKX shapes, probed 2026-11-07) ---

SAMPLE_FUNDING = {
    'code': '0',
    'data': [{
        'fundingRate': '0.0000284095951133',
        'fundingTime': '1794067200000',
        'instId': 'BTC-USDT-SWAP',
        'instType': 'SWAP',
        'nextFundingTime': '1794096000000',
        'settFundingRate': '-0.0000037730998910',
    }],
    'msg': '',
}

SAMPLE_FUNDING_HIST = {
    'code': '0',
    'data': [
        {'fundingRate': '0.0001000000000000',
         'fundingTime': '1794038400000', 'instId': 'BTC-USDT-SWAP'},
        {'fundingRate': '-0.0000500000000000',
         'fundingTime': '1794009600000', 'instId': 'BTC-USDT-SWAP'},
        {'fundingRate': '0.0000200000000000',
         'fundingTime': '1793980800000', 'instId': 'BTC-USDT-SWAP'},
    ],
    'msg': '',
}

SAMPLE_OI = {
    'code': '0',
    'data': [{
        'instId': 'BTC-USDT-SWAP',
        'instType': 'SWAP',
        'oi': '3451245.87000001391',
        'oiCcy': '34512.4587000001391',
        'oiUsd': '2619726848.80330055862197',
        'ts': '1794050359274',
    }],
    'msg': '',
}

SAMPLE_LS = {
    'code': '0',
    'data': [['1794050100000', '2.93'], ['1794049800000', '2.93'],
             ['1794049500000', '2.92']],
    'msg': '',
}

SAMPLE_ERROR = {'code': '51001', 'data': [],
                'msg': "Instrument ID doesn't exist."}


def _fake_fetch(mapping):
    """fetch_json stub: URL substring -> canned response (or raise)."""
    calls = []

    def fetch(url):
        calls.append(url)
        for needle, resp in mapping.items():
            if needle in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return None

    fetch.calls = calls
    return fetch


# --- Parsers ---

def test_parse_funding_annualizes_8h():
    f = okx_feed.parse_funding(SAMPLE_FUNDING)
    assert f['rate'] == pytest.approx(0.0000284095951133)
    # OKX settles every 8h: ann = rate * 3 * 365 * 100 (percent units,
    # same as Kraken's annualized_pct)
    assert f['ann_pct'] == pytest.approx(
        0.0000284095951133 * 3 * 365 * 100, abs=0.01)
    assert f['ann_pct'] == pytest.approx(3.11, abs=0.01)
    assert f['score'] == pytest.approx(
        -math.tanh((3.11 - 15.0) / 40.0), abs=0.01)
    assert f['next_funding_ts'] == 1794096000000


def test_parse_funding_rejects_error_code():
    assert okx_feed.parse_funding(SAMPLE_ERROR) is None
    assert okx_feed.parse_funding(None) is None
    assert okx_feed.parse_funding({}) is None
    assert okx_feed.parse_funding({'code': '0', 'data': []}) is None


def test_parse_funding_history_sorted_oldest_first():
    rates = okx_feed.parse_funding_history(SAMPLE_FUNDING_HIST)
    # Newest first in the payload -> oldest first out (by fundingTime)
    assert rates == pytest.approx([0.00002, -0.00005, 0.0001])
    assert okx_feed.parse_funding_history(SAMPLE_ERROR) == []
    assert okx_feed.parse_funding_history(None) == []


def test_funding_z_needs_history():
    hist = [0.0001 * i for i in range(20)]  # 20 points, rising
    z = okx_feed.funding_z(0.0019, hist)
    assert z is not None
    assert z > 0  # current is the max of the series
    # Too few rows -> no data, never a guess
    assert okx_feed.funding_z(0.001, [0.0001] * 5) is None
    # Flat history (std 0) -> no data
    assert okx_feed.funding_z(0.0001, [0.0001] * 20) is None
    assert okx_feed.funding_z(None, hist) is None


def test_parse_oi():
    oi = okx_feed.parse_oi(SAMPLE_OI)
    assert oi['oi_usd'] == pytest.approx(2619726848.80, abs=0.01)
    assert oi['oi'] == pytest.approx(3451245.87, abs=0.01)
    assert okx_feed.parse_oi(SAMPLE_ERROR) is None
    assert okx_feed.parse_oi(None) is None


def test_parse_ls():
    ls = okx_feed.parse_ls(SAMPLE_LS)
    assert ls['ratio'] == pytest.approx(2.93)
    assert ls['ts_ms'] == 1794050100000
    assert okx_feed.parse_ls(SAMPLE_ERROR) is None
    assert okx_feed.parse_ls(None) is None
    assert okx_feed.parse_ls({'code': '0', 'data': []}) is None


def test_funding_score_scale_matches_kraken():
    # 15% annualized = baseline -> score 0; crowded longs -> negative
    assert okx_feed.funding_score(15.0) == pytest.approx(0.0)
    assert okx_feed.funding_score(55.0) < -0.7
    assert okx_feed.funding_score(-25.0) > 0.7
    assert okx_feed.funding_score(None) is None


# --- Fetch (fake network) ---

def test_fetch_coin_all_legs():
    fetch = _fake_fetch({
        'funding-rate-history': SAMPLE_FUNDING_HIST,
        'funding-rate': SAMPLE_FUNDING,
        'open-interest': SAMPLE_OI,
        'long-short-account-ratio': SAMPLE_LS,
    })
    row = okx_feed.fetch_coin(fetch, 'BTC', pace_s=0)
    assert row['funding_rate'] == pytest.approx(0.0000284095951133)
    assert row['oi_usd'] == pytest.approx(2619726848.80, abs=0.01)
    assert row['ls_ratio'] == pytest.approx(2.93)
    assert row['funding_z'] is None  # only 3 history rows < 10
    assert row['error'] is None


def test_ls_endpoint_uses_ccy_not_instid():
    """Parameter fix: the rubik L/S endpoint takes ccy=BTC, NOT
    instId=BTC-USDT-SWAP (the probe's instId call 400'd)."""
    fetch = _fake_fetch({
        'funding-rate-history': SAMPLE_FUNDING_HIST,
        'funding-rate': SAMPLE_FUNDING,
        'open-interest': SAMPLE_OI,
        'long-short-account-ratio': SAMPLE_LS,
    })
    okx_feed.fetch_coin(fetch, 'BTC', pace_s=0)
    ls_calls = [u for u in fetch.calls
                if 'long-short-account-ratio' in u]
    assert len(ls_calls) == 1
    assert 'ccy=BTC' in ls_calls[0]
    assert 'instId=' not in ls_calls[0]


def test_fetch_coin_fail_open_per_leg():
    # L/S endpoint down, everything else fine -> only ratio is None
    fetch = _fake_fetch({
        'funding-rate-history': SAMPLE_FUNDING_HIST,
        'funding-rate': SAMPLE_FUNDING,
        'open-interest': SAMPLE_OI,
        'long-short-account-ratio': ConnectionError('boom'),
    })
    row = okx_feed.fetch_coin(fetch, 'BTC', pace_s=0)
    assert row['funding_rate'] is not None
    assert row['oi_usd'] is not None
    assert row['ls_ratio'] is None
    assert row['error'] is None  # funding leg landed, no error flag


def test_fetch_coin_all_down_fails_open():
    fetch = _fake_fetch({})
    row = okx_feed.fetch_coin(fetch, 'BTC', pace_s=0)
    assert row['funding_rate'] is None
    assert row['oi_usd'] is None
    assert row['ls_ratio'] is None
    assert row['error'] == 'all OKX legs failed'


def test_fetch_all_caps_and_btc_first():
    fetch = _fake_fetch({
        'funding-rate-history': SAMPLE_FUNDING_HIST,
        'funding-rate': SAMPLE_FUNDING,
        'open-interest': SAMPLE_OI,
        'long-short-account-ratio': SAMPLE_LS,
    })
    coins = ['ETH'] * 3 + ['BTC'] + [f'C{i}' for i in range(25)]
    out = okx_feed.fetch_all(fetch, coins, pace_s=0)
    assert len(out) == okx_feed.MAX_COINS_PER_RUN
    assert list(out)[0] == 'BTC'  # dispersion anchor first
    assert len(set(out)) == len(out)  # deduped


def test_fetch_all_always_includes_core_coins():
    """Core coins (BTC/ETH/SOL/XRP) are fetched even when no card
    carries them — a quiet run of small-cap movers must not leave
    the run with no BTC anchor."""
    fetch = _fake_fetch({
        'funding-rate-history': SAMPLE_FUNDING_HIST,
        'funding-rate': SAMPLE_FUNDING,
        'open-interest': SAMPLE_OI,
        'long-short-account-ratio': SAMPLE_LS,
    })
    out = okx_feed.fetch_all(fetch, ['DOGE'], pace_s=0)
    for core in okx_feed.CORE_COINS:
        assert core in out
        assert out[core]['funding_rate'] is not None
    assert 'DOGE' in out
    assert list(out)[0] == 'BTC'


def test_fetch_all_never_raises():
    def boom(url):
        raise RuntimeError('network is gone')
    out = okx_feed.fetch_all(boom, ['BTC', 'ETH'], pace_s=0)
    # Core coins are always included, so the set is the core four
    assert set(out) == {'BTC', 'ETH', 'SOL', 'XRP'}
    assert all(r['error'] is not None for r in out.values())


# --- OI history + changes ---

def _oi_hist(oi_now, oi_1h_ago, oi_24h_ago=None):
    pts = []
    if oi_24h_ago is not None:
        for i in range(24, 0, -1):
            oi = oi_24h_ago + (oi_1h_ago - oi_24h_ago) * (24 - i) / 23
            pts.append([NOW - i * 3600, oi])
    else:
        pts.append([NOW - 3600, oi_1h_ago])
    pts.append([NOW, oi_now])
    return {'BTC': pts}


def test_update_oi_history_keeps_last_30():
    hist = okx_feed.update_oi_history(
        {}, {'BTC': {'oi_usd': 100.0}}, NOW)
    assert hist == {'BTC': [[NOW, 100.0]]}
    hist = okx_feed.update_oi_history(
        {'BTC': [[NOW - 3600 * i, 100.0] for i in range(30, 0, -1)]},
        {'BTC': {'oi_usd': 101.0}}, NOW)
    assert len(hist['BTC']) == 30
    assert hist['BTC'][-1] == [NOW, 101.0]
    # No OI reading -> untouched
    assert okx_feed.update_oi_history(
        {'BTC': [[NOW, 100.0]]}, {'BTC': {'oi_usd': None}}, NOW) == \
        {'BTC': [[NOW, 100.0]]}


def test_oi_changes_windows():
    h = _oi_hist(oi_now=115, oi_1h_ago=100, oi_24h_ago=90)
    chg = okx_feed.oi_changes('BTC', h, NOW)
    assert chg['oi_chg_1h_pct'] == pytest.approx(15.0)
    assert chg['oi_chg_24h_pct'] == pytest.approx(115 / 90 * 100 - 100,
                                                 abs=0.5)
    # Warming up: single point -> None
    assert okx_feed.oi_changes('BTC', {'BTC': [[NOW, 100]]}, NOW) is None
    assert okx_feed.oi_changes('ETH', h, NOW) is None


# --- Dispersion ---

def test_dispersion_map():
    okx = {'BTC': {'ann_pct': 3.11}, 'ETH': {'ann_pct': 10.0},
           'SOL': {'ann_pct': 1.0}}
    kraken = {'BTC': {'ann_pct': 12.5}, 'ETH': {'ann_pct': 10.0}}
    d = okx_feed.dispersion_map(okx, kraken)
    assert d == {'BTC': pytest.approx(9.39), 'ETH': pytest.approx(0.0)}
    assert okx_feed.dispersion_for(None, 5.0) is None
    assert okx_feed.dispersion_for(5.0, None) is None
    assert okx_feed.dispersion_map({}, kraken) == {}


# --- Pulse line ---

def test_pulse_line():
    assert okx_feed.pulse_line({}) == 'OKX no data'
    line = okx_feed.pulse_line({'BTC': {
        'ann_pct': 3.11, 'funding_z': 0.4, 'oi_usd': 2.62e9,
        'ls_ratio': 2.93, 'funding_rate': 0.0000284}})
    assert 'BTC funding +3.11%/yr' in line
    assert 'z +0.40' in line
    assert 'OI $2.62B' in line
    assert 'L/S 2.93' in line
    assert '1/1 coins' in line
