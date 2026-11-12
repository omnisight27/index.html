"""Tests for the widened venue listings (Nov 12, Nic: 'Do both').

The sims now report four verified venues — Coinbase, Crypto.com,
Kraken, OKX — instead of two. The live gate reads `tradable` only
(Coinbase/Crypto.com); `venues` is sim reporting. These tests pin:
parsers map each venue's public payload to plain symbols (Kraken's
XBT/XDG wsname codes normalize to BTC/DOGE), `tradable` never gains a
venue, and a dead endpoint fails open per venue.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import venues


def test_parse_coinbase_products_online_usd_only():
    payload = [
        {'base_currency': 'BTC', 'quote_currency': 'USD', 'status': 'online'},
        {'base_currency': 'ETH', 'quote_currency': 'USD', 'status': 'offline'},
        {'base_currency': 'SOL', 'quote_currency': 'EUR', 'status': 'online'},
        {'base_currency': 'XRP', 'quote_currency': 'USD', 'status': 'online'},
    ]
    assert venues.parse_coinbase_products(payload) == {'BTC', 'XRP'}


def test_parse_cryptocom_instruments_ccy_pair_usd_usdt():
    payload = {'result': {'data': [
        {'base_ccy': 'BTC', 'quote_ccy': 'USD', 'inst_type': 'CCY_PAIR'},
        {'base_ccy': 'ETH', 'quote_ccy': 'USDT', 'inst_type': 'CCY_PAIR'},
        {'base_ccy': 'SOL', 'quote_ccy': 'USD', 'inst_type': 'PERPETUAL'},
        {'base_ccy': 'DOGE', 'quote_ccy': 'EUR', 'inst_type': 'CCY_PAIR'},
    ]}}
    assert venues.parse_cryptocom_instruments(payload) == {'BTC', 'ETH'}


def test_parse_kraken_pairs_wsname_and_x_codes():
    payload = {'result': {
        'XXBTZUSD': {'base': 'XXBT', 'wsname': 'XBT/USD'},
        'XBTUSDT': {'base': 'XXBT', 'wsname': 'XBT/USDT'},
        'XDGUSD': {'base': 'XXDG', 'wsname': 'XDG/USD'},
        'ETHUSD': {'base': 'XETH', 'wsname': 'ETH/USD'},
        'SOLEUR': {'base': 'SOL', 'wsname': 'SOL/EUR'},
        'XRPGBP': {'base': 'XXRP', 'wsname': 'XRP/GBP'},
        'BAD': {'base': 'BAD'},
    }}
    # XBT -> BTC, XDG -> DOGE; EUR/GBP pairs and wsname-less rows drop out.
    assert venues.parse_kraken_pairs(payload) == {'BTC', 'DOGE', 'ETH'}


def test_parse_okx_instruments_live_usd_usdt_only():
    payload = {'data': [
        {'baseCcy': 'BTC', 'quoteCcy': 'USD', 'state': 'live'},
        {'baseCcy': 'ETH', 'quoteCcy': 'USDT', 'state': 'live'},
        {'baseCcy': 'SOL', 'quoteCcy': 'USD', 'state': 'suspend'},
        {'baseCcy': 'XRP', 'quoteCcy': 'EUR', 'state': 'live'},
    ]}
    assert venues.parse_okx_instruments(payload) == {'BTC', 'ETH'}


def test_tradable_on_is_live_venues_only():
    listings = {'coinbase': {'BTC'}, 'cryptocom': {'BTC', 'ETH'},
                'kraken': {'BTC', 'ETH', 'SOL'}, 'okx': {'BTC', 'SOL'}}
    # SOL is on Kraken and OKX but NOT on a live venue: tradable stays [].
    assert venues.tradable_on('SOL', listings) == []
    assert venues.tradable_on('ETH', listings) == ['cryptocom']
    assert venues.tradable_on('BTC', listings) == ['coinbase', 'cryptocom']


def test_venues_on_reports_all_four_in_order():
    listings = {'coinbase': {'BTC'}, 'cryptocom': {'BTC', 'ETH'},
                'kraken': {'BTC', 'ETH', 'SOL'}, 'okx': {'BTC', 'SOL'}}
    assert venues.venues_on('BTC', listings) == ['coinbase', 'cryptocom',
                                                 'kraken', 'okx']
    assert venues.venues_on('SOL', listings) == ['kraken', 'okx']
    assert venues.venues_on('DOGE', listings) == []
    # Missing keys fail open (empty), never raise.
    assert venues.venues_on('BTC', {}) == []
    assert venues.tradable_on('BTC', None) == []


def test_live_venues_are_exactly_coinbase_and_cryptocom():
    assert venues.LIVE_VENUES == ('coinbase', 'cryptocom')
    assert venues.VENUE_ORDER == ['coinbase', 'cryptocom', 'kraken', 'okx']


def test_fetch_venue_listings_fail_open_per_venue():
    def fetch(url):
        if 'kraken' in url:
            raise RuntimeError('kraken down')
        if 'coinbase' in url:
            return [{'base_currency': 'BTC', 'quote_currency': 'USD',
                     'status': 'online'}]
        if 'crypto.com' in url:
            return {'result': {'data': [
                {'base_ccy': 'ETH', 'quote_ccy': 'USD',
                 'inst_type': 'CCY_PAIR'}]}}
        if 'okx' in url:
            return {'data': [{'baseCcy': 'SOL', 'quoteCcy': 'USDT',
                              'state': 'live'}]}
        raise AssertionError(url)

    listings = venues.fetch_venue_listings(fetch)
    assert listings['coinbase'] == {'BTC'}
    assert listings['cryptocom'] == {'ETH'}
    assert listings['kraken'] == set()
    assert listings['okx'] == {'SOL'}
