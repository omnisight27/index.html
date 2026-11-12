"""Tests for the Google News RSS headline fallback (2026-11-12 daily review).

freenewsapi.ai went key-gated (401 api_key_required) and the headlines panel
was running on the newsletter digest alone. When the wire call returns
nothing, ingest() must fall back to keyless Google News RSS so the wire
panel and the news-risk cross-reference keep running on real market news.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nexintel

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>test</title>
<item><title>Bitcoin plunges as ETF outflows mount</title>
<link>https://example.com/1</link>
<source url="https://example.com">CoinDesk</source></item>
<item><title>Fed holds rates steady</title>
<link>https://example.com/2</link>
<source url="https://example.com">Reuters</source></item>
<item><title></title><link>https://example.com/3</link></item>
</channel></rss>"""


def test_rss_parses_title_and_source(monkeypatch):
    monkeypatch.setattr(nexintel, 'fetch_text', lambda url, timeout=20: SAMPLE_RSS)
    out = nexintel.fetch_google_news_rss(size=10)
    assert out == [
        {'title': 'Bitcoin plunges as ETF outflows mount', 'source': 'CoinDesk'},
        {'title': 'Fed holds rates steady', 'source': 'Reuters'},
    ], out


def test_rss_respects_size(monkeypatch):
    monkeypatch.setattr(nexintel, 'fetch_text', lambda url, timeout=20: SAMPLE_RSS)
    assert len(nexintel.fetch_google_news_rss(size=1)) == 1


def test_rss_empty_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(nexintel, 'fetch_text', lambda url, timeout=20: None)
    assert nexintel.fetch_google_news_rss() == []


def test_rss_empty_on_bad_xml(monkeypatch):
    monkeypatch.setattr(nexintel, 'fetch_text', lambda url, timeout=20: '<not-xml')
    assert nexintel.fetch_google_news_rss() == []


if __name__ == '__main__':
    print('run with pytest')
