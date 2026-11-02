"""Insider flow feed — SEC EDGAR Form 4 buy/sell parsing (Nic 2026-11-01,
"wire the insider feed for stocks").

Replaces the v1 filing-count in nexintel.py with real per-form parsing:
open-market purchases (code P) vs sales (code S) over the trailing 90 days,
aggregated per ticker into a [-1, +1] score plus display tags.

DISPLAY + GRADING ONLY. Nothing here feeds a buy gate, alert threshold,
verdict, or live scoring weight. Candidate variables (INSIDER_FEATURE_NAMES)
are recorded per stock pick by the daily engine and graded by the
significance ranking ("Significance is predictability based on variables
used for an outcome") — never asserted.

Fail-open everywhere: any network/parse failure returns None (the caller
skips the insider block); the day cache keeps a run to ~124 requests worst
case and usually zero. Keyless — SEC EDGAR public data, same User-Agent
as the rest of the engine. Request rate <= 4/second.

Notes from live verification (2026-11-01, AAPL/NVDA real filings):
- the raw ownershipDocument XML is found via the filing's index.json
  (filename varies: form4.xml, wk-form4_*.xml); the submissions JSON
  primaryDocument is the XSL-rendered HTML, NOT the raw XML.
- S transactions often carry no price -> value skipped, never invented.
- G/M/F/A codes are compensation/gifts/tax — excluded by design.
"""

import json
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

INSIDER_FEATURE_NAMES = ['insider_score', 'insider_cluster', 'insider_net']
N_INSIDER = len(INSIDER_FEATURE_NAMES)

UA = 'OmniSight Research contact@omnisight.local'
SUBMISSIONS_URL = 'https://data.sec.gov/submissions/CIK{cik:010d}.json'
ARCHIVE_URL = 'https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{name}'

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = REPO_ROOT / 'data' / 'insider_cache.json'

WINDOW_DAYS = 90
MAX_FORMS = 3          # newest-first cap per ticker per run
RATE_SLEEP = 0.25      # <= 4 requests/second
NEG_CACHE_TTL = 3600   # retry a failed ticker after 1h, not next day
OPEN_MARKET = ('P', 'S')  # purchase / sale — everything else is comp/gifts/tax


def _http_get(url):
    """Raw bytes or None. Never raises."""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA,
                                                   'Accept': 'application/json'})
        time.sleep(RATE_SLEEP)
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    except Exception:
        return None


def _load_cache():
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_cache(cache):
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache))
    except Exception:
        pass  # cache is a nicety, never a failure


def _cache_fresh(entry):
    """Success entries last the UTC day; failures retry after NEG_CACHE_TTL."""
    if not isinstance(entry, dict):
        return False
    ts = entry.get('ts') or 0
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if entry.get('date') == today and entry.get('data') is not None:
        return True
    return (time.time() - ts) < NEG_CACHE_TTL


def _parse_form4(xml_bytes):
    """Parse one ownershipDocument. Returns
    (reporters, transactions) where reporters = [(name, officer_title)] and
    transactions = [(code, shares, price)] for P/S rows with usable numbers.
    Never raises — malformed XML yields ([], [])."""
    try:
        root = ET.fromstring(xml_bytes.decode('utf-8', 'replace'))
    except Exception:
        return [], []
    reporters = []
    for ro in root.findall('reportingOwner'):
        name = (ro.findtext('reportingOwnerId/rptOwnerName') or '').strip()
        title = (ro.findtext('reportingOwnerRelationship/officerTitle') or '').strip()
        if name:
            reporters.append((name, title))
    txs = []
    for tx in root.findall('nonDerivativeTable/nonDerivativeTransaction'):
        code = (tx.findtext('transactionCoding/transactionCode') or '').strip().upper()
        if code not in OPEN_MARKET:
            continue
        try:
            shares = float(tx.findtext('transactionAmounts/transactionShares/value'))
        except (TypeError, ValueError):
            continue
        try:
            price = float(tx.findtext('transactionAmounts/transactionPricePerShare/value'))
        except (TypeError, ValueError):
            price = None
        if price is None:
            continue  # no price -> value unknown; skip, never invent
        txs.append((code, shares, price))
    return reporters, txs


def _is_csuite(title):
    """C-suite heuristic on the officer title: standalone CEO/CFO/COO,
    'Chief X Officer', or 'President' not preceded by Vice. A plain
    'Vice President' / SVP / EVP is NOT C-suite (substring matching would
    wrongly catch 'PRESIDENT' inside 'Vice President')."""
    import re
    tokens = re.findall(r'[A-Z]+', (title or '').upper())
    if not tokens:
        return False
    if any(k in tokens for k in ('CEO', 'CFO', 'COO')):
        return True
    if 'CHIEF' in tokens and 'OFFICER' in tokens:
        return True
    return 'PRESIDENT' in tokens and 'VICE' not in tokens


def aggregate_forms(forms):
    """forms: list of (reporters, transactions). Returns the per-ticker
    90d aggregation dict (dollar values rounded to whole dollars)."""
    buyers, buy_value, sell_value = set(), 0.0, 0.0
    csuite_buy = False
    n_filings = len(forms)
    for reporters, txs in forms:
        for name, title in reporters:
            has_buy = any(code == 'P' for code, _, _ in txs)
            if has_buy:
                buyers.add(name)
                if _is_csuite(title):
                    csuite_buy = True
        for code, shares, price in txs:
            if code == 'P':
                buy_value += shares * price
            elif code == 'S':
                sell_value += shares * price
    net_value = buy_value - sell_value
    cluster_buy = len(buyers) >= 2
    return {
        'n_filings': n_filings,
        'buyers': len(buyers),
        'buy_value': round(buy_value),
        'sell_value': round(sell_value),
        'net_value': round(net_value),
        'csuite_buy': csuite_buy,
        'cluster_buy': cluster_buy,
    }


def score_insider(agg):
    """[-1, +1]-ish score. Clustered or C-suite buying is the documented
    signal (+1.0); lone buying is weaker (+0.5); heavy net selling is a
    small negative (-0.25) because selling is mostly compensation/
    diversification noise, not information."""
    if agg is None:
        return 0.0
    if agg.get('cluster_buy') or agg.get('csuite_buy'):
        return 1.0
    if (agg.get('buy_value') or 0) > 0:
        return 0.5
    if (agg.get('net_value') or 0) < -5_000_000:
        return -0.25
    return 0.0


def _fmt_money(v):
    v = float(v or 0)
    a = abs(v)
    if a >= 1_000_000:
        return f'${a / 1_000_000:.1f}M'
    if a >= 1_000:
        return f'${a / 1_000:.0f}K'
    return f'${a:.0f}'


def insider_tags(agg):
    """Compact display tags (mirrors the econ-pack tag convention)."""
    if not agg or not agg.get('n_filings'):
        return []
    bv, sv, net = agg['buy_value'], agg['sell_value'], agg['net_value']
    if agg['cluster_buy']:
        return [f"\U0001F575\uFE0F Insider cluster buying: {agg['buyers']} insiders "
                f"bought {_fmt_money(bv)} (90d)"]
    if agg['csuite_buy']:
        return [f"\U0001F575\uFE0F C-suite insider buying {_fmt_money(bv)} (90d)"]
    if bv > 0:
        return [f"\U0001F575\uFE0F Insider buying: {_fmt_money(bv)} (90d)"]
    if net < -5_000_000:
        return [f"\U0001F575\uFE0F Insider selling: {_fmt_money(-net)} net sold "
                f"(90d, noisy)"]
    if sv > 0:
        return [f"\U0001F575\uFE0F Insider selling: {_fmt_money(sv)} (90d, noisy)"]
    return [f"\U0001F575\uFE0F {agg['n_filings']} insider filings, "
            f"no open-market trades (90d)"]


def _xml_url_for_form(cik, accession):
    """index.json -> the raw ownershipDocument .xml (filename varies)."""
    acc = accession.replace('-', '')
    raw = _http_get(ARCHIVE_URL.format(cik=cik, acc=acc, name='index.json'))
    if not raw:
        return None
    try:
        items = json.loads(raw)['directory']['item']
    except Exception:
        return None
    for it in items:
        name = it.get('name') or ''
        if name.endswith('.xml') and 'index' not in name.lower():
            return ARCHIVE_URL.format(cik=cik, acc=acc, name=name)
    return None


def fetch_forms(cik):
    """Newest-first Form 4 (reporters, transactions) within WINDOW_DAYS,
    capped at MAX_FORMS. [] on any failure (fail open)."""
    raw = _http_get(SUBMISSIONS_URL.format(cik=cik))
    if not raw:
        return []
    try:
        recent = json.loads(raw)['filings']['recent']
    except Exception:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).strftime('%Y-%m-%d')
    forms = []
    for form, fdate, acc in zip(recent.get('form', []),
                                recent.get('filingDate', []),
                                recent.get('accessionNumber', [])):
        if fdate < cutoff:
            break  # newest-first — nothing older counts
        if form != '4' or len(forms) >= MAX_FORMS:
            continue
        xml_url = _xml_url_for_form(cik, acc)
        if not xml_url:
            continue
        xml = _http_get(xml_url)
        if not xml:
            continue
        forms.append(_parse_form4(xml))
    return forms


def build_insider(ticker, cik):
    """Full per-ticker result dict (display object) or None. Never raises."""
    try:
        forms = fetch_forms(cik)
        if not forms:
            return None
        agg = aggregate_forms(forms)
        score = score_insider(agg)
        net_scaled = max(-1.0, min(1.0, agg['net_value'] / 10_000_000))
        result = dict(agg)
        result['score'] = score
        result['tags'] = insider_tags(agg)
        result['features'] = [round(score, 3),
                              1.0 if agg['cluster_buy'] else 0.0,
                              round(net_scaled, 3)]
        return result
    except Exception:
        return None


def get_insider(ticker, cik):
    """Day-cached build_insider. Never raises — None on any failure."""
    if not ticker or not cik:
        return None
    key = str(ticker).upper()
    cache = _load_cache()
    entry = cache.get(key)
    if entry and _cache_fresh(entry):
        return entry.get('data')
    try:
        data = build_insider(key, cik)
    except Exception:
        data = None
    cache[key] = {'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
                  'ts': time.time(), 'data': data}
    _save_cache(cache)
    return data
