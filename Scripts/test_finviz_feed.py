#!/usr/bin/env python3
"""Regression tests for the Finviz FREE screener prototype (2026-11-07).

Public screener page, keyless, stdlib HTMLParser only — no login, no
key, no Elite, no unofficial `finviz` library. Display-only / shadow
research: parsed rows never touch a buy gate.

Run: python3 -m pytest Scripts/test_finviz_feed.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import finviz_feed
import stock_feed

# A saved sample of the real screener markup (div grid, not <table>),
# trimmed to two rows + a header fragment. Mirrors the live page
# structure verified 2026-11-07: <div data-row-key="TICKER"> rows,
# <div data-column-id="..."> cells, nested spans inside cells, and a
# logo tile whose fallback letter must NOT corrupt the ticker.
SAMPLE_HTML = """
<div class="fv-table_header">
  <div class="fv-table_cell" data-column-id="ticker">Ticker</div>
  <div class="fv-table_cell" data-column-id="price">Price</div>
</div>
<div data-row-key="AAPL" role="row">
  <div class="fv-table_lane" data-lane="left">
    <div class="fv-table_cell" data-column-id="ticker">
      <span class="logo">A</span><span>AAPL</span>
    </div>
    <div class="fv-table_cell" data-column-id="company">Apple Inc</div>
    <div class="fv-table_cell" data-column-id="sector">Technology</div>
    <div class="fv-table_cell" data-column-id="marketCap">4975.38B</div>
    <div class="fv-table_cell" data-column-id="PE">38.15</div>
    <div class="fv-table_cell" data-column-id="price">342.37</div>
    <div class="fv-table_cell" data-column-id="change">-0.52%</div>
    <div class="fv-table_cell" data-column-id="volume">44.70M</div>
    <div class="fv-table_cell" data-column-id="rsi">51.46</div>
  </div>
</div>
<div data-row-key="MSFT" role="row">
  <div class="fv-table_lane" data-lane="left">
    <div class="fv-table_cell" data-column-id="ticker">
      <span class="logo">M</span><span>MSFT</span>
    </div>
    <div class="fv-table_cell" data-column-id="company">Microsoft Corp</div>
    <div class="fv-table_cell" data-column-id="sector">Technology</div>
    <div class="fv-table_cell" data-column-id="marketCap">3812.10B</div>
    <div class="fv-table_cell" data-column-id="PE">33.20</div>
    <div class="fv-table_cell" data-column-id="price">512.05</div>
    <div class="fv-table_cell" data-column-id="change">0.84%</div>
    <div class="fv-table_cell" data-column-id="volume">18.22M</div>
    <div class="fv-table_cell" data-column-id="rsi">61.03</div>
  </div>
</div>
"""


# ---------------------------------------------------------------- parsing

def test_parse_sample_html_two_rows():
    rows = finviz_feed.parse_finviz_html(SAMPLE_HTML)
    assert len(rows) == 2
    aapl, msft = rows
    # Ticker comes from data-row-key, NOT the cell (logo fallback
    # letter 'A' + 'AAPL' must not become 'AAAPL').
    assert aapl['ticker'] == 'AAPL'
    assert msft['ticker'] == 'MSFT'
    assert aapl['company'] == 'Apple Inc'
    assert aapl['price'] == '342.37'
    assert aapl['change'] == '-0.52%'
    assert aapl['marketCap'] == '4975.38B'
    assert msft['rsi'] == '61.03'


def test_parse_ignores_header_row():
    # Header cells carry data-column-id but no data-row-key — they
    # must not become a row.
    rows = finviz_feed.parse_finviz_html(SAMPLE_HTML)
    assert all(r['ticker'] in ('AAPL', 'MSFT') for r in rows)
    assert 'Ticker' not in [r.get('company') for r in rows]


def test_parse_number_suffixes_and_percents():
    p = finviz_feed.parse_number
    assert p('342.37') == pytest.approx(342.37)
    assert p('-0.52%') == pytest.approx(-0.52)
    assert p('4975.38B') == pytest.approx(4_975_380_000_000.0)
    assert p('44.70M') == pytest.approx(44_700_000.0)
    assert p('1,234') == pytest.approx(1234.0)
    assert p('-') is None
    assert p('') is None
    assert p('—') is None
    assert p(None) is None
    assert p('abc') is None


def test_normalize_row_types_fields():
    raw = finviz_feed.parse_finviz_html(SAMPLE_HTML)[0]
    row = finviz_feed.normalize_row(raw)
    assert row['ticker'] == 'AAPL'
    assert row['company'] == 'Apple Inc'
    assert row['price'] == pytest.approx(342.37)
    assert row['change_pct'] == pytest.approx(-0.52)
    assert row['market_cap'] == pytest.approx(4_975_380_000_000.0)
    assert row['volume'] == pytest.approx(44_700_000.0)
    assert row['pe'] == pytest.approx(38.15)
    assert row['rsi'] == pytest.approx(51.46)


# ---------------------------------------------------------------- fail-open

@pytest.mark.parametrize('bad', [
    '', None, 123, '<html><body>not a screener</body></html>',
    '<div data-row-key="X"><div data-column-id="price">',
    '<div><span>unclosed soup',
])
def test_parse_fail_open_on_bad_html(bad):
    # Never raises; empty list on anything unparseable.
    rows = finviz_feed.parse_finviz_html(bad)
    assert rows == []


def test_fetch_fail_open_on_network_error(monkeypatch):
    monkeypatch.setattr(finviz_feed, '_fetch_url', lambda *a, **k: None)
    rows, diag = finviz_feed.fetch_finviz_view('111', ['AAPL'])
    assert rows == []
    assert 'FETCH_FAILED' in diag
    res = finviz_feed.fetch_finviz_universe()
    assert res['rows'] == []
    assert 'FETCH_FAILED' in res['diag']


def test_fetch_paginates_two_pages(monkeypatch):
    # Page 1 returns a full 20 rows -> fetch page 2; page 2 short.
    def fake_fetch(url, timeout=30):
        if 'r=21' in url:
            return SAMPLE_HTML  # 2 rows -> short page, stop
        rows = ''.join(
            f'<div data-row-key="T{i:02d}"><div data-column-id="price">'
            f'{100 + i}</div></div>' for i in range(20))
        return f'<div>{rows}</div>'
    monkeypatch.setattr(finviz_feed, '_fetch_url', fake_fetch)
    rows, diag = finviz_feed.fetch_finviz_view(
        '111', [f'T{i:02d}' for i in range(20)] + ['AAPL', 'MSFT'])
    assert len(rows) == 22
    assert 'pages=2' in diag
    assert rows[0]['ticker'] == 'T00'
    assert rows[-1]['ticker'] == 'MSFT'


# ---------------------------------------------------------------- universe

def test_universe_is_the_30_ticker_stock_universe():
    assert finviz_feed.STOCK_UNIVERSE == stock_feed.STOCK_UNIVERSE
    assert len(finviz_feed.STOCK_UNIVERSE) == 30
    assert len(set(finviz_feed.STOCK_UNIVERSE)) == 30
    for must in ('AAPL', 'MSFT', 'NVDA', 'JPM', 'XOM', 'MRK'):
        assert must in finviz_feed.STOCK_UNIVERSE


def test_merge_combines_views_on_ticker(monkeypatch):
    def fake_view(view, tickers=None, timeout=30):
        if view == '111':
            return ([{'ticker': 'AAPL', 'price': 100.0}], 'OK view=111')
        return ([{'ticker': 'AAPL', 'rsi': 55.0}], 'OK view=171')
    monkeypatch.setattr(finviz_feed, 'fetch_finviz_view', fake_view)
    res = finviz_feed.fetch_finviz_universe()
    assert len(res['rows']) == 1
    assert res['rows'][0] == {'ticker': 'AAPL', 'price': 100.0, 'rsi': 55.0}
    assert res['diag'].startswith('OK 1/30')
