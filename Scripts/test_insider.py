"""Tests for Scripts/insider.py — Form 4 buy/sell parsing, scoring, cache."""

import json
from datetime import datetime, timezone

import insider


SAMPLE_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Cook Timothy D</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>true</isOfficer>
      <officerTitle>Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>200.00</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>5000</value></transactionShares>
        <transactionPricePerShare><value>0.00</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""

SAMPLE_XML_2 = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Maestri Luca</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>true</isOfficer>
      <officerTitle>Senior Vice President</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>100</value></transactionShares>
        <transactionPricePerShare><value>200.00</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""

SAMPLE_XML_SELL = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Newstead Jennifer</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>true</isOfficer>
      <officerTitle>SVP, GC and Government Affairs</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>2399</value></transactionShares>
        <transactionPricePerShare><value>343.36</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>100</value></transactionShares>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""


def test_parse_form4_buy_and_m_skipped():
    reporters, txs = insider._parse_form4(SAMPLE_XML.encode())
    assert reporters == [('Cook Timothy D', 'Chief Executive Officer')]
    # M (exercise) is compensation noise — only the P survives
    assert txs == [('P', 10000.0, 200.0)]


def test_parse_form4_sell_missing_price_skipped():
    reporters, txs = insider._parse_form4(SAMPLE_XML_SELL.encode())
    assert reporters == [('Newstead Jennifer', 'SVP, GC and Government Affairs')]
    # second S has no price -> value unknown, skipped (never invented)
    assert txs == [('S', 2399.0, 343.36)]


def test_parse_form4_malformed_never_raises():
    assert insider._parse_form4(b'not xml at all') == ([], [])
    assert insider._parse_form4(b'') == ([], [])


def test_aggregate_cluster_and_csuite():
    forms = [insider._parse_form4(SAMPLE_XML.encode()),
             insider._parse_form4(SAMPLE_XML_2.encode())]
    agg = insider.aggregate_forms(forms)
    assert agg['n_filings'] == 2
    assert agg['buyers'] == 2
    assert agg['buy_value'] == 10000 * 200 + 100 * 200
    assert agg['sell_value'] == 0
    assert agg['net_value'] == agg['buy_value']
    assert agg['cluster_buy'] is True
    assert agg['csuite_buy'] is True  # CEO title


def test_aggregate_lone_non_csuite_buyer():
    forms = [insider._parse_form4(SAMPLE_XML_2.encode())]
    agg = insider.aggregate_forms(forms)
    assert agg['buyers'] == 1
    assert agg['cluster_buy'] is False
    assert agg['csuite_buy'] is False  # SVP without CHIEF keyword


def test_score_boundaries():
    assert insider.score_insider(None) == 0.0
    assert insider.score_insider({'cluster_buy': True, 'csuite_buy': False,
                                  'buy_value': 1, 'net_value': -99_000_000}) == 1.0
    assert insider.score_insider({'cluster_buy': False, 'csuite_buy': True,
                                  'buy_value': 1, 'net_value': 0}) == 1.0
    assert insider.score_insider({'cluster_buy': False, 'csuite_buy': False,
                                  'buy_value': 50_000, 'net_value': 50_000}) == 0.5
    # selling is noisy: only heavy net selling scores, and only -0.25
    assert insider.score_insider({'cluster_buy': False, 'csuite_buy': False,
                                  'buy_value': 0, 'net_value': -5_000_000}) == 0.0
    assert insider.score_insider({'cluster_buy': False, 'csuite_buy': False,
                                  'buy_value': 0, 'net_value': -5_000_001}) == -0.25
    assert insider.score_insider({'cluster_buy': False, 'csuite_buy': False,
                                  'buy_value': 0, 'net_value': 0}) == 0.0


def test_tags_direction_aware():
    agg = {'n_filings': 2, 'buyers': 3, 'buy_value': 2_100_000,
           'sell_value': 0, 'net_value': 2_100_000,
           'csuite_buy': False, 'cluster_buy': True}
    tags = insider.insider_tags(agg)
    assert len(tags) == 1 and 'cluster buying' in tags[0] and '3 insiders' in tags[0]
    agg2 = dict(agg, cluster_buy=False, csuite_buy=False, buyers=1)
    assert 'buying' in insider.insider_tags(agg2)[0]
    agg3 = {'n_filings': 4, 'buyers': 0, 'buy_value': 0, 'sell_value': 8_200_000,
            'net_value': -8_200_000, 'csuite_buy': False, 'cluster_buy': False}
    assert 'noisy' in insider.insider_tags(agg3)[0]
    assert insider.insider_tags(None) == []
    assert insider.insider_tags({'n_filings': 0}) == []


def test_day_cache_hit_no_network(monkeypatch, tmp_path):
    monkeypatch.setattr(insider, 'CACHE_PATH', tmp_path / 'insider_cache.json')
    cached = {'score': 1.0, 'tags': ['x']}
    insider._save_cache({'AAPL': {'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
                                  'ts': 9999999999, 'data': cached}})

    def boom(url):
        raise AssertionError('network on cache hit: ' + url)
    monkeypatch.setattr(insider, '_http_get', boom)
    assert insider.get_insider('aapl', 320193) == cached


def test_fail_open_bad_cik_and_network(monkeypatch, tmp_path):
    monkeypatch.setattr(insider, 'CACHE_PATH', tmp_path / 'insider_cache.json')
    monkeypatch.setattr(insider, '_http_get', lambda url: None)
    assert insider.get_insider('ZZZ', 0) is None
    assert insider.get_insider('ZZZ', 99999999) is None
    # None result is cached (negative cache) — still None, still no raise
    assert insider.get_insider('ZZZ', 99999999) is None
    assert insider.get_insider(None, None) is None
    assert insider.get_insider('', 0) is None


def test_feature_names_fixed_order():
    assert insider.INSIDER_FEATURE_NAMES == ['insider_score', 'insider_cluster',
                                             'insider_net']
    assert insider.N_INSIDER == 3


def test_fmt_money():
    assert insider._fmt_money(2_100_000) == '$2.1M'
    assert insider._fmt_money(850_000) == '$850K'
    assert insider._fmt_money(500) == '$500'


def test_is_csuite_vp_is_not_csuite():
    assert insider._is_csuite('Chief Executive Officer') is True
    assert insider._is_csuite('CEO') is True
    assert insider._is_csuite('President and CEO') is True
    assert insider._is_csuite('President') is True
    assert insider._is_csuite('Senior Vice President') is False
    assert insider._is_csuite('EVP, General Counsel and Sec') is False
    assert insider._is_csuite('SVP, GC and Government Affairs') is False
    assert insider._is_csuite('Principal Accounting Officer') is False
    assert insider._is_csuite('') is False
    assert insider._is_csuite(None) is False
