"""Tests for the CONTRACT alert shape (2026-10-27 fix): a mega-award alert
used to carry the award size in $B in its `price` field — e.g. LMT "price"
48.06 vs a real ~$480 share price — so any consumer formatting `price` as
dollars would mislead. `price` is now 0 (no tradable price) and the award
size lives in `award_billions`; the reason text is unchanged.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nexintel


def _data(contracts):
    return {
        'fear_greed': [{'value': 50}, {'value': 50}],
        'crypto': {'bitcoin': {'symbol': 'BTC', 'usd': 100000,
                               'usd_1h_change': 0.5, 'usd_24h_change': 2.0,
                               'usd_7d_change': 3.0, 'usd_14d_change': 4.0,
                               'usd_30d_change': 5.0}},
        'listings': {'coinbase': {'BTC'}, 'cryptocom': {'BTC'}},
        'stocks': {},
        'contracts': contracts,
        'macro': {}, 'headlines': [], 'opportunities': [],
        'opportunities_nonprofit': [], 'foresight': [], 'fed_commentary': [],
    }


def _history():
    return {'picks': [],
            'stats': {'wins': 0, 'losses': 0, 'total': 0, 'win_rate': 50}}


def _contract_alerts(out):
    return [a for a in out['alerts'] if a['type'] == 'CONTRACT']


def test_contract_alert_price_is_zero_with_award_billions():
    contracts = [{'Recipient Name': 'Lockheed Martin Corporation',
                  'Award Amount': 48_060_000_000,
                  'Awarding Agency': 'Dept of Defense'}]
    out = nexintel.think(_data(contracts), _history())
    alerts = _contract_alerts(out)
    assert len(alerts) == 1
    a = alerts[0]
    assert a['asset'] == 'LMT'
    assert a['price'] == 0
    assert a['award_billions'] == 48.06
    assert a['confidence'] == 85
    assert a['asset_class'] == 'contract'
    assert '$48.06B award' in a['reason']
    assert 'Lockheed Martin Corporation' in a['reason']


def test_sub_threshold_award_produces_no_contract_alert():
    contracts = [{'Recipient Name': 'Small Co',
                  'Award Amount': 100_000_000,
                  'Awarding Agency': 'Dept of Energy'}]
    out = nexintel.think(_data(contracts), _history())
    assert _contract_alerts(out) == []


def test_contract_alert_without_known_ticker_keeps_recipient_name():
    contracts = [{'Recipient Name': 'Acme Widgets LLC',
                  'Award Amount': 2_500_000_000,
                  'Awarding Agency': 'Dept of Transportation'}]
    out = nexintel.think(_data(contracts), _history())
    alerts = _contract_alerts(out)
    assert len(alerts) == 1
    assert alerts[0]['asset'] == 'Acme Widgets LLC'
    assert alerts[0]['price'] == 0
    assert alerts[0]['award_billions'] == 2.5
