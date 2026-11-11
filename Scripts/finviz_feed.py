#!/usr/bin/env python3
"""Finviz FREE screener feed — display-only / shadow research prototype.

Fetches the public Finviz stock screener (no login, no key, no Elite)
for the fixed 30-ticker universe from stock_feed.py and parses the
server-rendered grid into structured rows. Intended as a screening /
discovery complement to the Yahoo-based stocks research track
(stock_feed.py): same universe, same display-only + shadow grading
rules — Finviz rows NEVER join longs/shorts, NEVER touch a buy gate,
NEVER alert, NEVER feed the crypto circuit breaker.

How it works (verified live 2026-11-07 from this environment):
- Public URL: https://finviz.com/screener/stocks?v=<view>&t=<CSV tickers>
  (the legacy /screener.ashx form 301-redirects here; plain GET with a
  browser User-Agent returns HTTP 200, ~280 KB, no login wall).
- The grid is server-rendered as divs, NOT a <table>: each data row is
  a <div data-row-key="TICKER"> and each cell is a
  <div data-column-id="<name>">. Parsed with stdlib html.parser only.
- Pagination: 20 rows per page; page 2 starts at r=21. Two requests
  cover the 30-ticker universe per view.
- Views (verified 2026-11-07 — Finviz renumbered them since the old
  docs): 111 Overview (company/sector/marketCap/PE/price/change/
  volume), 171 Technical (beta/ATR/SMA20/50/200/52w high-low/RSI/
  price/change/gap/volume), 141 Performance (perf 1w/4w/YTD/52w,
  volatility, relative volume). Default fetch merges 111 + 171 so
  each row carries price, change, volume, PE, RSI and SMAs.

Honest limitations (read before using):
- DELAYED DATA. The page footer states stock quotes are delayed
  1 minute; independent reviews report the free tier delayed up to
  ~15 min (NASDAQ) / ~20 min (NYSE/AMEX). Either way this is NOT
  real-time: screening / discovery and shadow research ONLY, never
  a live signal source. (The stocks spec flags the same issue for
  Yahoo: a real-time source is a precondition for any live use.)
- Screen-scraping a public page is inherently brittle: Finviz can
  change the markup, throttle, or block at any time. Every fetch is
  fail-open (empty rows + diag, never an exception) and the module
  makes only a handful of requests per run (~4 for the default
  two views x two pages). Do NOT hammer it.
- No unofficial `finviz` PyPI library is used (scraping lib, ToS
  risk) — this parses the public page directly with the stdlib.
- RSI here is the screener's 14-day RSI as displayed; it is a
  discovery field, not a validated engine input.

Design: parent task 2026-11-07 (Finviz free workaround probe).
"""
import json
import re
import urllib.request
from html.parser import HTMLParser

try:
    from stock_feed import STOCK_UNIVERSE
except Exception:  # pragma: no cover - standalone fallback
    STOCK_UNIVERSE = [
        'AAPL', 'MSFT', 'NVDA', 'AMZN', 'GOOGL', 'META', 'AVGO', 'TSLA',
        'JPM', 'LLY', 'V', 'MA', 'UNH', 'HD', 'PG', 'COST', 'NFLX', 'XOM',
        'WMT', 'BAC', 'CRM', 'AMD', 'ADBE', 'ABBV', 'KO', 'PEP', 'DIS',
        'NKE', 'ORCL', 'MRK',
    ]

FINVIZ_BASE = 'https://finviz.com/screener/stocks'
VIEW_OVERVIEW = '111'      # company/sector/marketCap/PE/price/change/volume
VIEW_TECHNICAL = '171'     # beta/ATR/SMA20/50/200/52w/RSI/price/change/gap
VIEW_PERFORMANCE = '141'   # perf 1w/4w/YTD/52w, volatility, relative volume
ROWS_PER_PAGE = 20
MAX_PAGES = 10             # safety cap (universe is 30 -> 2 pages)
# Top-1,000 US-listed by market cap (stocks Phase 2, Nic 2026-11-11):
# the same screener, filtered to the three US exchanges and sorted by
# market cap descending. 50 pages at 20 rows/page; paced, day-cached
# by the caller (stock_feed) — the composition moves slowly.
TOP1000_FILTERS = 'exch_amex,exch_nasdaq,exch_nyse'
TOP1000_SORT = '-marketcap'
TOP1000_TARGET = 1000
TOP1000_MAX_PAGES = 50
TOP1000_PACE_S = 0.3
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
              'AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/120.0 Safari/537.36')

# Raw screener column id -> normalized field name.
COLUMN_MAP = {
    'ticker': 'ticker', 'company': 'company', 'sector': 'sector',
    'industry': 'industry', 'country': 'country',
    'marketCap': 'market_cap', 'PE': 'pe', 'forwardPE': 'forward_pe',
    'PEG': 'peg', 'price': 'price', 'change': 'change_pct',
    'changeopen': 'change_open_pct', 'gap': 'gap_pct',
    'volume': 'volume', 'averageVolume': 'avg_volume',
    'relativeVolume': 'relative_volume',
    'rsi': 'rsi', 'beta': 'beta', 'averageTrueRange': 'atr',
    'sma20': 'sma20', 'sma50': 'sma50', 'sma200': 'sma200',
    'high52w': 'high52w', 'low52w': 'low52w',
    'perf1w': 'perf1w_pct', 'perf4w': 'perf4w_pct',
    'perfMtd': 'perf_mtd_pct', 'perf13w': 'perf13w_pct',
    'perf26w': 'perf26w_pct', 'perfYtd': 'perf_ytd_pct',
    'perf52w': 'perf52w_pct', 'perf3y': 'perf3y_pct',
    'perf5y': 'perf5y_pct', 'perf10y': 'perf10y_pct',
    'volatility1w': 'volatility1w_pct', 'volatility4w': 'volatility4w_pct',
    'shortInterestShare': 'short_interest_pct',
    'shortInterestRatio': 'short_interest_ratio',
    'dividendYield': 'dividend_yield_pct',
}

# Fields that are plain numbers (no % / suffix handling beyond commas).
_PLAIN_NUMERIC = {
    'price', 'pe', 'forward_pe', 'peg', 'rsi', 'beta', 'atr',
    'sma20', 'sma50', 'sma200', 'high52w', 'low52w',
    'short_interest_ratio', 'relative_volume',
}
# Fields that arrive with a trailing '%' (kept as percent points).
_PCT_FIELDS = {
    'change_pct', 'change_open_pct', 'gap_pct', 'perf1w_pct',
    'perf4w_pct', 'perf_mtd_pct', 'perf13w_pct', 'perf26w_pct',
    'perf_ytd_pct', 'perf52w_pct', 'perf3y_pct', 'perf5y_pct',
    'perf10y_pct', 'volatility1w_pct', 'volatility4w_pct',
    'short_interest_pct', 'dividend_yield_pct',
}
# Fields with magnitude suffixes (B/M/K/T) — market cap, volume.
_SUFFIXED_FIELDS = {'market_cap', 'volume', 'avg_volume'}

_SUFFIX_MULT = {'K': 1e3, 'M': 1e6, 'B': 1e9, 'T': 1e12}
_VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
              'link', 'meta', 'param', 'source', 'track', 'wbr'}


def parse_number(text):
    """Parse a Finviz cell value to float, or None if missing/unparseable.

    Handles '342.37', '-0.52%', '4975.38B', '45.23M', '1,234', '-',
    '' and '—'. Percent signs are stripped (value kept as percent
    points, e.g. '-0.52%' -> -0.52). Pure function — no network."""
    if text is None:
        return None
    s = str(text).strip().replace(',', '')
    if s in ('', '-', '—', 'N/A', 'n/a'):
        return None
    if s.endswith('%'):
        s = s[:-1]
    mult = 1.0
    if s and s[-1].upper() in _SUFFIX_MULT:
        mult = _SUFFIX_MULT[s[-1].upper()]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


class _ScreenerParser(HTMLParser):
    """Parse Finviz's div-grid screener into raw row dicts.

    Rows are <div data-row-key="TICKER">; cells are
    <div data-column-id="<name>"> containing (possibly nested) text.
    The header row has cells but no data-row-key, so it is ignored.
    Depth-tracked so nested spans/imgs inside a cell don't break it.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._depth = 0
        self._row = None        # current row dict, or None outside a row
        self._row_depth = None
        self._cell_col = None   # column id being captured, or None
        self._cell_depth = None
        self._cell_buf = []

    def handle_starttag(self, tag, attrs):
        if tag in _VOID_TAGS:
            return
        self._depth += 1
        if tag != 'div':
            return
        d = dict(attrs)
        if 'data-row-key' in d:
            self._row = {'ticker': d['data-row-key']}
            self._row_depth = self._depth
            return
        if self._row is not None and 'data-column-id' in d:
            self._cell_col = d['data-column-id']
            self._cell_depth = self._depth
            self._cell_buf = []

    def handle_startendtag(self, tag, attrs):
        # Self-closing tags (e.g. <img ... />) never affect depth.
        return

    def handle_data(self, data):
        if self._cell_col is not None:
            self._cell_buf.append(data)

    def handle_endtag(self, tag):
        if tag in _VOID_TAGS:
            return
        if self._cell_col is not None and self._depth == self._cell_depth:
            text = ''.join(self._cell_buf).strip()
            # The ticker cell also contains the logo tile's fallback
            # letter (e.g. 'A' + 'AAPL' -> 'AAAPL'); the row-key is
            # authoritative, so never let the cell overwrite it.
            if text and self._cell_col != 'ticker':
                self._row[self._cell_col] = text
            self._cell_col = None
            self._cell_depth = None
            self._cell_buf = []
        if self._row is not None and self._depth == self._row_depth:
            self.rows.append(self._row)
            self._row = None
            self._row_depth = None
        self._depth -= 1


def parse_finviz_html(html):
    """Parse screener HTML into a list of raw row dicts keyed by the
    screener's column ids (e.g. {'ticker': 'AAPL', 'price': '342.37',
    'change': '-0.52%', ...}). Returns [] on any parse failure —
    fail-open, never raises."""
    try:
        if not html or not isinstance(html, str):
            return []
        p = _ScreenerParser()
        p.feed(html)
        p.close()
        return [r for r in p.rows if r.get('ticker')]
    except Exception:
        return []


def normalize_row(raw):
    """Map a raw parsed row to typed fields. Unknown columns are kept
    under their raw id; unparseable numbers are dropped (field absent
    rather than wrong). Pure function — no network."""
    out = {}
    for col, val in raw.items():
        if col == 'row':
            continue  # screener row index, not data
        field = COLUMN_MAP.get(col, col)
        if field in ('ticker', 'company', 'sector', 'industry', 'country'):
            out[field] = str(val).strip()
        elif field in _PLAIN_NUMERIC or field in _PCT_FIELDS \
                or field in _SUFFIXED_FIELDS:
            num = parse_number(val)
            if num is not None:
                out[field] = num
        else:
            out[field] = val
    return out


def _fetch_url(url, timeout=30):
    """One GET with a browser User-Agent. Returns decoded text, or
    None on any failure (fail-open)."""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return r.read().decode('utf-8', errors='replace')
    except Exception:
        return None


def fetch_finviz_view(view, tickers=None, timeout=30):
    """Fetch one screener view for the tickers, paginating (20/page)
    until a short page. Returns (rows, diag) with rows as normalized
    dicts. Never raises; on failure rows is [] and diag explains."""
    tickers = list(tickers) if tickers else list(STOCK_UNIVERSE)
    if not tickers:
        return [], 'EMPTY_UNIVERSE'
    csv = ','.join(tickers)
    rows, start, pages = [], 1, 0
    while pages < MAX_PAGES:
        url = (f'{FINVIZ_BASE}?v={view}&t={csv}&r={start}')
        html = _fetch_url(url, timeout=timeout)
        if html is None:
            return [], f'FETCH_FAILED view={view} r={start}'
        page_rows = [normalize_row(r) for r in parse_finviz_html(html)]
        pages += 1
        rows.extend(page_rows)
        if len(page_rows) < ROWS_PER_PAGE:
            break
        start += ROWS_PER_PAGE
    return rows, f'OK view={view} rows={len(rows)} pages={pages}'


def fetch_top1000_by_mcap(timeout=30, pace_s=TOP1000_PACE_S):
    """Top 1,000 US-listed tickers by market cap from the Finviz free
    screener (no login, no key). One view (overview), 20 rows/page,
    50 pages, paced so Finviz is not hammered. Returns
    (tickers_in_mcap_order, diag). Fail-open: a failed page stops the
    walk and returns what was collected, with a diag that says so.
    The list is Finviz's US-listed universe (exchanges AMEX/NASDAQ/
    NYSE, market-cap order) — it includes a few ETFs/ADRs alongside
    common stock; Yahoo v8 serves them all the same. Display-only /
    shadow research — never a live signal source."""
    import time
    tickers, pages, start = [], 0, 1
    while len(tickers) < TOP1000_TARGET and pages < TOP1000_MAX_PAGES:
        url = (f'{FINVIZ_BASE}?v={VIEW_OVERVIEW}'
               f'&f={TOP1000_FILTERS}&o={TOP1000_SORT}&r={start}')
        html = _fetch_url(url, timeout=timeout)
        if html is None:
            return tickers, (f'FETCH_FAILED r={start} pages={pages} '
                             f'tickers={len(tickers)}')
        rows = parse_finviz_html(html)
        pages += 1
        if not rows:
            break
        for row in rows:
            t = row.get('ticker')
            if t and t not in tickers:
                tickers.append(t)
        if len(rows) < ROWS_PER_PAGE:
            break
        start += ROWS_PER_PAGE
        if len(tickers) < TOP1000_TARGET:
            time.sleep(pace_s)
    return tickers[:TOP1000_TARGET], \
        f'OK tickers={len(tickers)} pages={pages}'


def fetch_finviz_universe(views=(VIEW_OVERVIEW, VIEW_TECHNICAL),
                           tickers=None, timeout=30):
    """Fetch the universe across views and merge on ticker (later
    views fill in fields the earlier ones lack). Returns
    {'rows': [merged dicts, sorted by ticker], 'diag': str}.
    Fail-open: any view failing yields rows=[] with a diag string.
    Display-only / shadow research — never a live signal source."""
    tickers = list(tickers) if tickers else list(STOCK_UNIVERSE)
    merged, diags = {}, []
    for view in views:
        rows, diag = fetch_finviz_view(view, tickers, timeout=timeout)
        diags.append(diag)
        if not rows:
            return {'rows': [], 'diag': ' | '.join(diags)}
        for row in rows:
            t = row.get('ticker')
            if not t:
                continue
            merged.setdefault(t, {}).update(row)
    out = [merged[t] for t in sorted(merged)]
    return {'rows': out,
            'diag': f"OK {len(out)}/{len(tickers)} tickers | "
                    + ' | '.join(diags)}


if __name__ == '__main__':
    # Smoke test: live fetch, print row count + 3 sample rows + diag.
    res = fetch_finviz_universe()
    print(f"rows: {len(res['rows'])}")
    print(f"diag: {res['diag']}")
    for row in res['rows'][:3]:
        print(json.dumps(row, indent=2))
    print('NOTE: delayed data (free tier) — screening/shadow only, '
          'never a live signal.')
