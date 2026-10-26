#!/usr/bin/env python3
"""
NexIntel Signal Engine — the autonomous loop.
Runs on GitHub Actions (no server). Every run:
  1. INGEST  — pulls all keyless live feeds
  2. THINK   — scores assets, applies the psychology layer, generates picks
  3. REMEMBER — scores yesterday's picks against today's prices, updates track record
  4. PUBLISH — writes data/latest.json + data/history.json (site reads these)

API keys (FRED, Finnhub, EIA) are read from environment variables = GitHub Secrets.
If a key is absent, that feed is skipped gracefully — the engine degrades, never dies.
"""

import json, os, math, time, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')
SNAP_DIR = os.path.join(DATA_DIR, 'snapshots')
os.makedirs(SNAP_DIR, exist_ok=True)

# Stock + ETF universe scored every run (Finnhub quotes). NRR + SCHD added
# 2026-10-25 at Nic's request: NRR is the Bitwise NEAR ETF (trading since
# 2026-09-29), SCHD the dividend ETF from the Oct 23 dividend lane.
STOCK_SYMBOLS = ['SPY', 'QQQ', 'DIA', 'IWM',                      # indices
                 'XLK', 'XLF', 'XLE', 'XLV', 'XLI', 'XLU', 'XLP', 'XLY', 'XLRE', 'XLB', 'XLC',  # sectors
                 'AAPL', 'MSFT', 'NVDA', 'AMZN', 'META', 'GOOGL', 'TSLA', 'AVGO', 'JPM',
                 'V', 'MA', 'XOM', 'CVX', 'UNH', 'LLY', 'COST', 'HD', 'PG', 'JNJ', 'BAC',
                 'CRM', 'ORCL', 'AMD', 'NFLX', 'DIS', 'INTC', 'COIN', 'MSTR', 'PLTR',  # mega-caps + crypto-adjacent
                 'NRR', 'SCHD']  # crypto + dividend ETFs

def _retryable_fetch_error(e):
    """Retry timeouts, DNS/connection blips, 429s and 5xx — not other 4xx."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code == 429 or 500 <= e.code < 600
    return True

def fetch_json(url, timeout=20, method='GET', body=None, headers=None, retries=2):
    req = urllib.request.Request(url, method=method,
        data=json.dumps(body).encode() if body else None,
        headers=headers or {'User-Agent': 'NexIntel/1.0', 'Content-Type': 'application/json'})
    last_err = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last_err = e
            if attempt < retries and _retryable_fetch_error(e):
                wait = 2 ** attempt
                print(f'  [retry {attempt + 1}/{retries}] {url[:60]}... ({e}) — waiting {wait}s')
                time.sleep(wait)
            elif attempt < retries:
                break  # non-retryable client error — fail fast
    print(f'  [warn] {url[:60]}... failed: {last_err}')
    return None

def fetch_text(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': 'NexIntel/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode('utf-8', errors='ignore')
    except Exception as e:
        print(f'  [warn] {url[:60]}... failed: {e}')
        return None

def sam_agency(o):
    """Agency name for a SAM.gov opportunity record.

    The v2 API carries no officeAddress.agency — the hierarchy lives in
    fullParentPathName ("DEPT OF DEFENSE.DEPT OF THE NAVY..."), whose
    first segment is the department. Fall back to department, then
    office, then '?' (never crash the ingest on a missing field)."""
    path = (o.get('fullParentPathName') or '').split('.')
    if path and path[0].strip():
        return path[0].strip().title()
    return o.get('department') or o.get('office') or '?'

# ------------------------------------------------------- NONPROFIT WATCH
# Contract watch, focused on what a 501(c)(3) thrift + community
# nonprofit can actually pursue. Federal procurement has no general
# nonprofit set-aside, so relevance is matched two ways: NAICS codes
# for the work NicNacs does (used-merchandise retail, materials
# recovery, community and family services, vocational rehab) and
# keywords in the solicitation title.
NONPROFIT_NAICS = {
    '453310': 'Used merchandise stores',
    '562920': 'Materials recovery',
    '562111': 'Solid waste collection',
    '562119': 'Other waste collection',
    '562998': 'Other waste management',
    '624190': 'Individual & family services',
    '624210': 'Community food services',
    '624220': 'Community housing services',
    '624230': 'Emergency & relief services',
    '624310': 'Vocational rehabilitation',
    '624120': 'Services for elderly & disabled',
    '624110': 'Child & youth services',
    '813410': 'Civic & social organizations',
    '453998': 'Misc store retailers',
    '812990': 'Other personal services',
}
NONPROFIT_KEYWORDS = [
    'nonprofit', 'non-profit', '501(c)(3)', '501c3', 'community',
    'thrift', 'used merchandise', 'donation', 'donated',
    'surplus property', 'recycling', 'materials recovery',
    'vocational rehabilitation', 'food bank', 'shelter', 'homeless',
    'family services', 'social services', 'workforce development',
    'job training', 'charitable', 'community development',
]

def nonprofit_match(title, naics):
    """Why a SAM.gov opportunity is relevant to a nonprofit like NicNacs:
    returns a short reason string, or None. NAICS match first, then a
    title keyword — both are reported when both hit."""
    reasons = []
    label = NONPROFIT_NAICS.get(str(naics or '').strip())
    if label:
        reasons.append(f'NAICS {naics} — {label}')
    t = (title or '').lower()
    hit = next((k for k in NONPROFIT_KEYWORDS if k in t), None)
    if hit:
        reasons.append(f'keyword: {hit}')
    return '; '.join(reasons) if reasons else None

def _sam_record(o):
    """One SAM.gov opportunity, normalized for the dashboard. Place of
    performance state is kept when the API carries it (OR vs national
    matters for a Grants Pass nonprofit)."""
    pop = o.get('placeOfPerformance') or {}
    state = pop.get('state') or {}
    title = o.get('title', '?')
    naics = o.get('naicsCode', '?')
    return {
        'title': title,
        'agency': sam_agency(o),
        'type': o.get('type', {}).get('value', '?') if isinstance(o.get('type'), dict) else o.get('type', '?'),
        'posted': o.get('postedDate', '?'),
        'deadline': o.get('responseDeadLine', '?'),
        'naics': naics,
        'state': state.get('code') or '?',
        'notice_id': o.get('noticeId', '?'),
        'nonprofit': bool(nonprofit_match(title, naics)),
        'nonprofit_match': nonprofit_match(title, naics),
    }

def _past_deadline(deadline):
    """True when a response deadline has already passed. Unparseable
    values keep the row — never drop an opportunity on bad data."""
    try:
        d = str(deadline).replace('Z', '+00:00')
        dt = datetime.fromisoformat(d)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc)
    except Exception:
        return False

def build_sam_pulse(sam_response, records, history_path=None):
    """Market-direction read from the SAM.gov pull — the deciding-factor
    view of government contracting (Nic, 2026-10-25).

    Individual solicitations (a DOE gasket set, a DoD cooler) never change
    a buy or sell call, so the pages show the pulse, not the raw list:
    how many solicitations posted in the 7-day window, which agencies are
    most active among the sampled rows, and whether volume is rising or
    cooling vs ~24h ago. The API's totalRecords is the true window count
    when it carries it; otherwise the count is the sampled rows, labeled
    so. Direction needs a stored history (data/sam_pulse.json, kept by
    the loop like history.json); the first reading has no direction.
    Never raises — a failed pulse is an honest empty one."""
    pulse = {'total': 0, 'total_is_sample': True, 'window_days': 7,
             'top_agencies': [], 'direction': 'unknown',
             'direction_pct': None, 'sampled': len(records or [])}
    try:
        from datetime import timedelta as _td
        total = None
        if isinstance(sam_response, dict):
            tr = sam_response.get('totalRecords')
            if isinstance(tr, int):
                total = tr
                pulse['total_is_sample'] = False
        if total is None:
            total = len(records or [])
        pulse['total'] = total
        counts = {}
        for r in (records or []):
            a = (r or {}).get('agency') or '?'
            counts[a] = counts.get(a, 0) + 1
        pulse['top_agencies'] = [
            {'agency': a, 'count': c}
            for a, c in sorted(counts.items(), key=lambda kv: -kv[1])[:5]]
        path = history_path or os.path.join(DATA_DIR, 'sam_pulse.json')
        hist = []
        try:
            with open(path) as f:
                saved = json.load(f)
            if isinstance(saved, list):
                hist = saved
        except Exception:
            pass
        now = datetime.now(timezone.utc)
        entry = {'t': now.isoformat(), 'total': total}
        if hist:
            target = now - _td(hours=24)
            best, best_gap = None, None
            for h in hist:
                try:
                    ht = datetime.fromisoformat(
                        str(h.get('t', '')).replace('Z', '+00:00'))
                except Exception:
                    continue
                if ht.tzinfo is None:
                    ht = ht.replace(tzinfo=timezone.utc)
                gap = abs((ht - target).total_seconds())
                if best_gap is None or gap < best_gap:
                    best, best_gap = h, gap
            age_h = 0
            if best is not None:
                try:
                    bt = datetime.fromisoformat(
                        str(best.get('t', '')).replace('Z', '+00:00'))
                    if bt.tzinfo is None:
                        bt = bt.replace(tzinfo=timezone.utc)
                    age_h = (now - bt).total_seconds() / 3600
                except Exception:
                    age_h = 0
            if (best is not None and isinstance(best.get('total'), (int, float))
                    and best['total'] and age_h >= 12):
                pct = round((total - best['total']) / best['total'] * 100, 1)
                pulse['direction_pct'] = pct
                pulse['direction'] = ('rising' if pct > 5 else
                                      'cooling' if pct < -5 else 'flat')
            else:
                pulse['direction'] = 'first'
        else:
            pulse['direction'] = 'first'
        hist.append(entry)
        hist = hist[-28:]
        try:
            with open(path, 'w') as f:
                json.dump(hist, f, indent=2)
        except Exception:
            pass
    except Exception:
        pass
    return pulse

def collect_nonprofit_opportunities(ledger_path=None):
    """Nonprofit-focused SAM.gov watch (the NicNacs lane).

    The API takes ONE NAICS code per request (ncode) and a non-associated
    key gets ~10 requests/day, while the loop runs 4x/day and the general
    feed already uses 4. So each run queries ONE code, rotating through
    NONPROFIT_NAICS, and merges the results into a persistent ledger
    (data/sam_nonprofit.json, committed by the loop like history.json).
    A 30-day window per code means a ~4-day rotation never misses a
    posting. Past-deadline rows are pruned on every merge; a failed fetch
    keeps the old ledger and just logs it. Never raises."""
    path = ledger_path or os.path.join(DATA_DIR, 'sam_nonprofit.json')
    ledger = {'rows': [], 'next_index': 0}
    try:
        with open(path) as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            ledger = saved
    except Exception:
        pass
    sam_key = os.environ.get('SAM_GOV_API_KEY')
    if sam_key:
        try:
            codes = list(NONPROFIT_NAICS)
            idx = ledger.get('next_index', 0) % len(codes)
            code = codes[idx]
            from datetime import timedelta as _td
            today = datetime.now(timezone.utc).date()
            posted_from = (today - _td(days=30)).strftime('%m/%d/%Y')
            posted_to = today.strftime('%m/%d/%Y')
            sam = fetch_json(
                f'https://api.sam.gov/opportunities/v2/search?api_key={sam_key}'
                f'&postedFrom={posted_from}&postedTo={posted_to}&limit=25&ptype=o'
                f'&ncode={code}')
            opps = (sam or {}).get('opportunitiesData', [])
            fresh = [_sam_record(o) for o in opps[:25]]
            rows = [r for r in ledger.get('rows', [])
                    if r.get('naics') != code]
            rows.extend(fresh)
            rows = [r for r in rows if not _past_deadline(r.get('deadline'))]
            rows.sort(key=lambda r: str(r.get('posted', '')), reverse=True)
            ledger = {'rows': rows[:100], 'next_index': (idx + 1) % len(codes)}
            with open(path, 'w') as f:
                json.dump(ledger, f, indent=2)
            print(f"  SAM.gov nonprofit: {len(fresh)} fresh for NAICS {code} "
                  f"({NONPROFIT_NAICS[code]}), {len(ledger['rows'])} tracked")
        except Exception as e:
            print(f'  SAM.gov nonprofit query failed: {e}')
    else:
        print('  [info] SAM_GOV_API_KEY absent — nonprofit watch skipped')
    return ledger.get('rows', [])

# ---------------------------------------------------------------- FORESIGHT
# Where 50-year government plans leak in public, before they happen:
# rulemaking (Federal Register), budget analysis (CBO), procurement
# (SAM.gov — already live), Fed commentary (already live), spending
# (USASpending — already live). Lenses are the parts of Nic's life the
# plans touch; a document only becomes a signal when it matches a lens.
FORESIGHT_LENSES = {
    'Health': ['medicare', 'medicaid', 'drug pric', 'clinical', 'transplant',
               'health insurance', '340b', 'prior authori', 'lab test',
               'diagnostic'],
    'Money & Tax': ['tax', 'retirement', 'social security', '401(k)',
                    'capital gains', 'estate tax'],
    'Small Business & Nonprofits': ['small business', 'nonprofit', '501(c)',
                                    'charitable', 'grant', 'sba'],
    'Energy & Infrastructure': ['energy', 'electric grid', 'infrastructure',
                                'broadband', 'water system', 'highway'],
    'Tech & AI': ['artificial intelligence', 'algorithm', 'data privacy',
                  'cybersecurity', 'cryptocurrenc', 'digital asset',
                  'stablecoin', 'quantum'],
    'Markets & Budgets': ['federal reserve', 'interest rate', 'treasury',
                          'federal debt', 'deficit', 'budget outlook',
                          'long-term', 'deflation'],
}
FORESIGHT_WHY = {
    'Health': 'Health rules change what care costs and what labs can charge — this touches transplant care directly.',
    'Money & Tax': 'Tax and retirement rules change what you keep and what a business pays.',
    'Small Business & Nonprofits': 'The lane NicNacs lives in — grants, nonprofit rules, small-business programs.',
    'Energy & Infrastructure': 'Energy and infrastructure spending moves markets and construction years ahead.',
    'Tech & AI': 'Rules for AI, crypto, and data decide what the platforms — and this engine — are allowed to do.',
    'Markets & Budgets': 'Budgets and debt are the 50-year plan in numbers — this is where the future gets funded or cut.',
}

def foresight_lenses(text):
    """Lens names whose keywords appear in the text (title/abstract)."""
    t = (text or '').lower()
    return [name for name, kws in FORESIGHT_LENSES.items()
            if any(k in t for k in kws)]

def collect_foresight(ledger_path=None):
    """Pull this week's long-horizon signals from public sources.

    Federal Register (no key): newest rulemaking documents, lens-matched.
    CBO publications RSS (no key): budget analysis, lens-matched, with
    Long-Term/Outlook items always kept. Returns the fresh signals and
    appends first-seen entries to the graded ledger
    (data/foresight.json — committed by the loop like history.json).
    A signal is a hint, not a prediction: the ledger is the record the
    system earns trust against, graded as rules finalize or budgets pass.
    Never raises — a failed feed degrades to an empty list."""
    from datetime import timedelta as _td
    import re as _re, subprocess as _sp
    signals = []
    today = datetime.now(timezone.utc).date()
    week_ago = (today - _td(days=7)).isoformat()
    # --- Federal Register: rulemaking is where policy first appears ---
    # Routine paperwork notices (information collections, Privacy Act
    # system-of-records notices, Sunshine Act meetings) match keywords
    # in their boilerplate but carry no policy signal — skip them.
    _ROUTINE = ('information collection', 'privacy act of 1974',
                'sunshine act', 'agency information collection',
                'data collection available')
    try:
        fed = fetch_json('https://www.federalregister.gov/api/v1/documents.json'
                         '?per_page=100&order=newest')
        for r in (fed or {}).get('results', []) or []:
            pub = (r.get('publication_date') or '')[:10]
            if pub and pub < week_ago:
                continue
            title = (r.get('title') or '')
            if title.lower().startswith(_ROUTINE):
                continue
            text = title + ' ' + (r.get('abstract') or '')
            lenses = foresight_lenses(text)
            if not lenses:
                continue
            signals.append({
                'source': 'Federal Register',
                'title': (r.get('title') or '?')[:160],
                'agency': (r.get('agency_names') or [{}])[0].get('name', '?')
                          if isinstance(r.get('agency_names'), list) and r.get('agency_names')
                          else '?',
                'date': pub,
                'url': r.get('html_url', ''),
                'lens': lenses[0],
                'why': FORESIGHT_WHY[lenses[0]],
            })
    except Exception as e:
        print(f'  Foresight Federal Register failed: {e}')
    # --- CBO: the nonpartisan budget scorekeeper ---
    try:
        rss = _sp.run(['curl', '-sL', '--max-time', '25',
                       'https://www.cbo.gov/publications/all/rss.xml'],
                      capture_output=True, text=True, timeout=30).stdout
        if rss:
            for it in _re.findall(r'<item>(.*?)</item>', rss, _re.DOTALL)[:20]:
                tm = _re.search(r'<title>(.*?)</title>', it, _re.DOTALL)
                lm = _re.search(r'<link>(.*?)</link>', it, _re.DOTALL)
                dm = _re.search(r'<pubDate>(.*?)</pubDate>', it)
                if not tm:
                    continue
                title = _re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', tm.group(1)).strip()
                lenses = foresight_lenses(title)
                if not lenses and 'long-term' not in title.lower() \
                        and 'outlook' not in title.lower():
                    continue
                signals.append({
                    'source': 'CBO',
                    'title': title[:160],
                    'agency': 'Congressional Budget Office',
                    'date': (dm.group(1)[:16] if dm else ''),
                    'url': lm.group(1).strip() if lm else '',
                    'lens': lenses[0] if lenses else 'Markets & Budgets',
                    'why': FORESIGHT_WHY[lenses[0]] if lenses
                           else FORESIGHT_WHY['Markets & Budgets'],
                })
    except Exception as e:
        print(f'  Foresight CBO failed: {e}')
    signals = signals[:12]
    print(f'  Foresight: {len(signals)} signals this week')
    # --- Graded ledger: first-seen record, deduped by URL ---
    try:
        path = ledger_path or os.path.join(DATA_DIR, 'foresight.json')
        ledger = []
        if os.path.exists(path):
            with open(path) as f:
                ledger = json.load(f)
        seen = {e.get('url') for e in ledger}
        new = 0
        for s in signals:
            if s.get('url') and s['url'] not in seen:
                ledger.append({**s, 'first_seen': today.isoformat(),
                               'status': 'watching'})
                seen.add(s['url'])
                new += 1
        ledger = ledger[-200:]
        with open(path, 'w') as f:
            json.dump(ledger, f, indent=2)
        if new:
            print(f'  Foresight ledger: +{new} new (total {len(ledger)})')
    except Exception as e:
        print(f'  Foresight ledger failed: {e}')
    return signals

def load_newsletter_digest():
    """Load data/newsletters.json — the daily digest distilled from Nic's
    nine morning newsletters (built by scripts/build-newsletter-digest.py
    in the goal workspace, committed by the newsletter cron). Returns the
    parsed digest, or None when absent/unreadable — the engine degrades,
    never dies."""
    try:
        with open(os.path.join(DATA_DIR, 'newsletters.json')) as f:
            return json.load(f)
    except Exception:
        return None

# ---------------------------------------------------------------- INGEST
def ingest():
    print('INGEST: pulling live feeds...')
    d = {}

    # Crypto — top 1000 by market cap (CoinGecko, 4 paginated calls, no key)
    d['crypto'] = {}
    try:
        for page in range(1, 5):
            mkts = fetch_json('https://api.coingecko.com/api/v3/coins/markets'
                f'?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}'
                '&price_change_percentage=1h,24h,7d,14d,30d')
            if not mkts:
                break
            for c in mkts:
                d['crypto'][c['id']] = {
                    'usd': c['current_price'],
                    'usd_1h_change': c.get('price_change_percentage_1h_in_currency') or 0,
                    'usd_24h_change': c.get('price_change_percentage_24h') or 0,
                    'usd_7d_change': c.get('price_change_percentage_7d_in_currency') or 0,
                    'usd_14d_change': c.get('price_change_percentage_14d_in_currency') or 0,
                    'usd_30d_change': c.get('price_change_percentage_30d_in_currency') or 0,
                    'symbol': (c['symbol'] or '').upper(),
                    'name': c.get('name') or '',
                    'market_cap': c.get('market_cap') or 0,
                }
        print(f"  crypto: {len(d['crypto'])} coins ingested")
    except Exception as e:
        print(f'  crypto markets failed: {e}')

    # Coinbase real-time tickers
    d['tickers'] = {}
    for pair in ['BTC-USD', 'ETH-USD', 'SOL-USD']:
        t = fetch_json(f'https://api.exchange.coinbase.com/products/{pair}/ticker')
        if t: d['tickers'][pair] = t

    # Exchange listings — which coins Nic can actually trade (Coinbase + Crypto.com)
    d['listings'] = {'coinbase': set(), 'cryptocom': set()}
    try:
        cb_prods = fetch_json('https://api.exchange.coinbase.com/products') or []
        d['listings']['coinbase'] = {p['base_currency'] for p in cb_prods
            if p.get('quote_currency') == 'USD' and p.get('status') == 'online' and p.get('base_currency')}
        print(f"  listings: Coinbase {len(d['listings']['coinbase'])} coins")
    except Exception as e:
        print(f'  coinbase listings failed: {e}')
    try:
        cdc = fetch_json('https://api.crypto.com/exchange/v1/public/get-instruments') or {}
        d['listings']['cryptocom'] = {i['base_ccy'] for i in (cdc.get('result', {}).get('data', []) or [])
            if i.get('inst_type') == 'CCY_PAIR' and i.get('quote_ccy') in ('USD', 'USDT') and i.get('base_ccy')}
        print(f"  listings: Crypto.com {len(d['listings']['cryptocom'])} coins")
    except Exception as e:
        print(f'  cryptocom listings failed: {e}')

    # Sentiment (Fear & Greed)
    fg = fetch_json('https://api.alternative.me/fng/?limit=7')
    d['fear_greed'] = fg['data'] if fg and 'data' in fg else []

    # Forex (open.er-api.com)
    fx = fetch_json('https://open.er-api.com/v6/latest/USD')
    d['forex'] = fx.get('rates', {}) if fx else {}

    # Metals (gold-api.com)
    d['metals'] = {}
    for sym in ['XAU', 'XAG', 'XPT', 'XPD']:
        m = fetch_json(f'https://api.gold-api.com/price/{sym}')
        if m: d['metals'][sym] = m

    # Federal contracts — USASpending (awards) + SAM.gov (opportunities)
    awards = fetch_json('https://api.usaspending.gov/api/v2/search/spending_by_award/',
        method='POST',
        body={'filters': {'award_type_codes': ['A', 'B', 'C', 'D']},
              'fields': ['Award ID', 'Recipient Name', 'Award Amount', 'Awarding Agency'],
              'limit': 10, 'sort': 'Award Amount', 'order': 'desc'})
    d['contracts'] = (awards or {}).get('results', [])

    # SAM.gov contract opportunities (solicitations — what's coming, not just awarded)
    sam_key = os.environ.get('SAM_GOV_API_KEY')
    d['opportunities'] = []
    sam = None
    if sam_key:
        try:
            from datetime import timedelta as _td
            today = datetime.now(timezone.utc).date()
            posted_from = (today - _td(days=7)).strftime('%m/%d/%Y')
            posted_to = today.strftime('%m/%d/%Y')
            sam = fetch_json(
                f'https://api.sam.gov/opportunities/v2/search?api_key={sam_key}'
                f'&postedFrom={posted_from}&postedTo={posted_to}&limit=25&ptype=o')
            opps = (sam or {}).get('opportunitiesData', [])
            d['opportunities'] = [_sam_record(o) for o in opps[:25]]
            print(f"  SAM.gov: {len(d['opportunities'])} opportunities ingested")
        except Exception as e:
            print(f'  SAM.gov failed: {e}')
    else:
        print('  [info] SAM_GOV_API_KEY absent — opportunities feed skipped')

    # Contracting pulse — the deciding-factor view: volume + direction,
    # not the raw list of individual solicitations (see build_sam_pulse).
    d['sam_pulse'] = build_sam_pulse(sam, d['opportunities'])

    # Nonprofit watch — the NicNacs lane (rotating NAICS, persistent
    # ledger; self-contained key check, never breaks the run).
    d['opportunities_nonprofit'] = collect_nonprofit_opportunities()
    # Contracting officers don't always code NAICS consistently, so a
    # nonprofit-relevant solicitation can surface in the general feed
    # under another code. Merge any general-feed row the matcher tags
    # into the nonprofit list (dedup by notice id) — the ledger stays
    # the rotation store; this union is what publishes.
    seen = {r.get('notice_id') for r in d['opportunities_nonprofit']}
    for r in d.get('opportunities', []):
        if r.get('nonprofit') and r.get('notice_id') not in seen:
            d['opportunities_nonprofit'].append(r)
            seen.add(r.get('notice_id'))
    d['opportunities_nonprofit'].sort(
        key=lambda r: str(r.get('posted', '')), reverse=True)

    # Headlines
    news = fetch_json('https://freenewsapi.ai/v1/search?q=markets%20stocks%20crypto&date=2d&lang=en&size=10&sort=date')
    d['headlines'] = (news or {}).get('results', [])[:10]

    # Morning newsletters — merge the daily digest's headlines into the
    # news feed, tagged with the newsletter name as source, so the
    # news-risk cross-reference in think() covers them too. The digest
    # itself rides along for its pre-scanned market flags.
    d['newsletter_digest'] = load_newsletter_digest()
    if d['newsletter_digest']:
        merged = 0
        for src in d['newsletter_digest'].get('sources', []):
            for h in src.get('headlines', []):
                d['headlines'].append({'title': h, 'source': src.get('name'),
                                        'via': 'newsletter'})
                merged += 1
        print(f"  Newsletters: {merged} headlines merged "
              f"({d['newsletter_digest'].get('date')})")
    else:
        print('  [info] no newsletter digest — newsletter feed skipped')

    # St. Louis Fed "On the Economy" blog — economist commentary feed
    # (via curl: stlouisfed.org blocks Python urllib)
    d['fed_commentary'] = []
    try:
        import re as _re, subprocess as _sp
        rss = _sp.run(['curl', '-sL', '--max-time', '25',
                       'https://www.stlouisfed.org/on-the-economy/rss'],
                      capture_output=True, text=True, timeout=30).stdout
        if rss:
            items = _re.findall(r'<item>(.*?)</item>', rss, _re.DOTALL)[:8]
            for it in items:
                tm = _re.search(r'<title>(.*?)</title>', it, _re.DOTALL)
                lm = _re.search(r'<link>(.*?)</link>', it, _re.DOTALL)
                dm = _re.search(r'<pubDate>(.*?)</pubDate>', it)
                if tm:
                    title = _re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', tm.group(1)).strip()
                    d['fed_commentary'].append({
                        'title': title[:120],
                        'link': lm.group(1).strip() if lm else '',
                        'date': dm.group(1)[:16] if dm else '',
                    })
            print(f"  Fed commentary: {len(d['fed_commentary'])} articles")
    except Exception as e:
        print(f'  Fed RSS failed: {e}')

    # Foresight — where 50-year plans leak in public: Federal Register
    # rulemaking + CBO budget analysis. Lens-matched to Nic's life, with
    # a graded first-seen ledger (data/foresight.json). Keyless feeds;
    # a failure degrades to an empty list, never a dead run.
    d['foresight'] = collect_foresight()

    # --- Keyed feeds (graceful if absent) ---
    fred_key = os.environ.get('FRED_API_KEY')
    d['rates'] = {}
    d['macro'] = {}
    if fred_key:
        # Rates & curve
        for sid, name in [('DGS10', 'us10y'), ('DGS2', 'us2y'), ('DFEDTARU', 'fedfunds'),
                          ('T10Y2Y', 'curve'), ('DCOILWTICO', 'wti')]:
            r = fetch_json(f'https://api.stlouisfed.org/fred/series/observations'
                           f'?series_id={sid}&api_key={fred_key}&file_type=json&limit=1&sort_order=desc')
            try: d['rates'][name] = r['observations'][0]['value']
            except: pass
        # Macro dashboard: CPI, GDP, unemployment, consumer sentiment, money supply
        # + GNP, payrolls (PAYEMS), jobless claims (ICSA), consumer spending (PCE)
        for sid, name in [('CPIAUCSL', 'cpi'), ('GDP', 'gdp'), ('UNRATE', 'unemployment'),
                          ('UMCSENT', 'consumer_sentiment'), ('M2SL', 'm2'),
                          ('DEXUSEU', 'eur_usd'), ('DGS30', 'us30y'),
                          ('GNP', 'gnp'), ('PAYEMS', 'payrolls'),
                          ('ICSA', 'jobless_claims'), ('PCE', 'consumer_spending')]:
            r = fetch_json(f'https://api.stlouisfed.org/fred/series/observations'
                           f'?series_id={sid}&api_key={fred_key}&file_type=json&limit=3&sort_order=desc')
            try:
                obs = [o for o in r['observations'] if o['value'] != '.']
                if len(obs) >= 2:
                    cur, prev = float(obs[0]['value']), float(obs[1]['value'])
                    d['macro'][name] = {
                        'value': cur, 'prev': prev,
                        'change': round(cur - prev, 2),
                        'change_pct': round((cur - prev) / abs(prev) * 100, 2) if prev else 0,
                        'date': obs[0]['date'],
                    }
                elif obs:
                    d['macro'][name] = {'value': float(obs[0]['value']), 'date': obs[0]['date']}
            except: pass
        # YoY inflation from CPI (12 obs back)
        try:
            r = fetch_json(f'https://api.stlouisfed.org/fred/series/observations'
                           f'?series_id=CPIAUCSL&api_key={fred_key}&file_type=json&limit=13&sort_order=desc')
            obs = [o for o in r['observations'] if o['value'] != '.']
            if len(obs) >= 13:
                yoy = (float(obs[0]['value']) / float(obs[12]['value']) - 1) * 100
                d['macro']['cpi_yoy'] = {'value': round(yoy, 2), 'date': obs[0]['date']}
        except: pass
    else:
        print('  [info] FRED_API_KEY absent — rates/macro feed skipped (add key in GitHub Secrets)')

    finnhub_key = os.environ.get('FINNHUB_API_KEY')
    d['stocks'] = {}
    if finnhub_key:
        # Indices, sector ETFs, mega-caps — the stocks that move markets
        symbols = STOCK_SYMBOLS
        for sym in symbols:
            q = fetch_json(f'https://finnhub.io/api/v1/quote?symbol={sym}&token={finnhub_key}')
            if q and q.get('c'): d['stocks'][sym] = q
        print(f"  stocks: {len(d['stocks'])} symbols ingested")
    else:
        print('  [info] FINNHUB_API_KEY absent — stocks feed skipped (add key in GitHub Secrets)')

    # --- Kalshi probability feed (keyless public API, read-only) ---
    # Watched Fed/CPI/crypto-threshold markets + paper tracking ledger.
    # Self-contained in Scripts/kalshi.py; a failure here never blocks
    # the rest of the run.
    try:
        import kalshi as _kalshi
        d['kalshi'] = _kalshi.collect(DATA_DIR)
        print(f"  kalshi: watching {d['kalshi'].get('watch_total', 0)} markets, "
              f"{len(d['kalshi'].get('paper_open', []))} paper positions open")
    except Exception as e:
        print(f'  Kalshi feed failed: {e}')
        d['kalshi'] = {}

    return d

# ---------------------------------------------------------------- THINK
def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))

def round_price(p):
    """Adaptive price precision: sub-cent coins keep enough decimals to be
    non-zero and scoreable (a $0.0012 coin rounded to 2dp is $0.00 and can
    never be scored, since remember() treats 0 as missing)."""
    if not p: return 0
    if p >= 100: return round(p, 2)
    if p >= 1: return round(p, 4)
    if p >= 0.01: return round(p, 6)
    return round(p, 8)

def fmt_price(p):
    """Display-string twin of round_price(): same adaptive precision bands,
    rendered for humans. Sub-$1 coins keep real decimals (a $0.1194 coin
    must never read '@ $0'); $1+ gets commas + 2dp. Mirrors the convention
    in scripts/intraday-sms.py."""
    if not p:
        return '$0'
    if p >= 1:
        return f'${p:,.2f}'
    s = f'{round_price(p):.8f}'.rstrip('0').rstrip('.')
    return '$' + s

# Nic's actual crypto holdings (Coinbase, 2026-10-21). SELL alerts fire
# ONLY for assets in this set — a sell signal on something he doesn't
# hold is noise to a long-only trader. Keep in sync with the same
# constant in scripts/intraday-sms.py (the WhatsApp selector).
NIC_HOLDINGS = {'BTC', 'XRP', 'ETH', 'SOL', 'AVAX'}  # ARB sold + XLM fully converted to BTC, XRP trimmed ~40% — all 2026-10-23

# Confidence bands shared by the calibration table (remember) and the
# empirical calibration layer (think). A band's stated label (e.g. "80+")
# is only as honest as its realized win rate — calibration pulls stated
# confidence toward the band's empirical rate once enough samples exist.
CONF_BANDS = [('50-59', 50, 59), ('60-69', 60, 69), ('70-79', 70, 79), ('80+', 80, 999)]
CALIB_MIN_N = 10    # bands with fewer scored picks keep the raw confidence
CALIB_MAX_PULL = 0.5  # cap the blend so a noisy band can't snap the value

def calibrate_confidence(raw, calibration_rows):
    """Shrink a raw confidence toward its band's empirical win rate.

    Evidence-gated: returns the raw value unchanged when the band has
    fewer than CALIB_MIN_N scored picks or no realized rate yet. The pull
    weight grows with sample size (n/60) up to CALIB_MAX_PULL, so the
    mapping can't whipsaw on small-sample noise.
    Returns (calibrated, band_label, n, empirical_rate)."""
    label = next((b[0] for b in CONF_BANDS if b[1] <= raw <= b[2]), None)
    rows = {c.get('band'): c for c in (calibration_rows or [])}
    row = rows.get(label) or {}
    n = row.get('n') or 0
    emp = row.get('win_rate')
    if label is None or n < CALIB_MIN_N or emp is None:
        return int(round(raw)), label, n, emp
    w = min(CALIB_MAX_PULL, n / 60.0)
    cal = raw * (1 - w) + emp * w
    return int(round(clamp(cal, 35, 97))), label, n, emp

# Plain-English regime labels for user-facing display (internal values stay
# RISK_ON / RISK_OFF / NEUTRAL for all logic)
REGIME_LABELS = {
    'RISK_ON': '🟢 Conditions favor buying',
    'RISK_OFF': '🔴 Conditions favor caution',
    'NEUTRAL': '🟡 Mixed conditions',
}

# ---------------------------------------------------------------- OPTIMIZATION
# Row echelon form via Gaussian elimination with partial pivoting.
# Solves Ax = b for the least-squares optimal weight vector.
def to_row_echelon(A, b):
    """Forward elimination: reduce [A|b] to row echelon form. Returns (U, y)."""
    n = len(A)
    U = [row[:] + [b[i]] for i, row in enumerate(A)]  # augmented matrix
    for col in range(n):
        # partial pivot: largest absolute value in this column at/below diagonal
        piv = max(range(col, n), key=lambda r: abs(U[r][col]))
        U[col], U[piv] = U[piv], U[col]
        if abs(U[col][col]) < 1e-12:
            continue  # singular column — leave as is (ridge handles it)
        for r in range(col + 1, n):
            factor = U[r][col] / U[col][col]
            for c in range(col, n + 1):
                U[r][c] -= factor * U[col][c]
    return [row[:n] for row in U], [row[n] for row in U]

def back_substitute(U, y):
    """Solve Ux = y where U is upper-triangular (row echelon form)."""
    n = len(U)
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        s = y[i] - sum(U[i][j] * x[j] for j in range(i + 1, n))
        x[i] = s / U[i][i] if abs(U[i][i]) > 1e-12 else 0.0
    return x

def solve_least_squares(X, y, ridge=1e-3):
    """
    Solve min_w ||Xw - y||^2 via normal equations: (X^T X + λI) w = X^T y.
    The system is reduced to row echelon form, then back-substituted.
    Returns the optimal weight vector w.
    """
    n_feat = len(X[0])
    # X^T X + λI
    XtX = [[sum(X[r][i] * X[r][j] for r in range(len(X))) + (ridge if i == j else 0)
            for j in range(n_feat)] for i in range(n_feat)]
    Xty = [sum(X[r][i] * y[r] for r in range(len(X))) for i in range(n_feat)]
    U, yy = to_row_echelon(XtX, Xty)
    return back_substitute(U, yy)

# Feature order (must stay consistent between training and scoring):
# [momentum, reversion, psych_bias, fg_level, fg_delta, vol,
#  acceleration, zscore, rsi, trend_align,
#  macd, ma_align, donchian, rsi2, roc, stoch,
#  boll_pctb, atr, vol_regime, zscore2, extreme_fade, var_proxy]
# 1-10: base signals | 11-22: model library (trend/momentum/volatility/risk)
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from models import build_model_features, MODEL_NAMES, kelly_fraction, drawdown_guard
N_FEATURES = 22
HEURISTIC_W = ([1.0, 1.0, 0.6, 8.0, 4.0, -0.5, 0.8, -1.2, 0.3, 2.0] +
               [0.5] * 12)  # model features start at half weight
FEATURE_NAMES = (['momentum', 'reversion', 'psych_bias', 'fg_level', 'fg_delta', 'vol',
                  'acceleration', 'zscore', 'rsi', 'trend_align'] + MODEL_NAMES)

def extract_features(chg24, chg7d, psych_bias, fg_now, fg_delta):
    # --- Calculus: velocity (1st derivative) and acceleration (2nd derivative) ---
    velocity = chg24
    daily_trend = chg7d / 7.0
    acceleration = chg24 - daily_trend  # + = move speeding up, - = dying
    momentum = chg24 * (1 + (0.5 if (chg24 > 0) == (chg7d > 0) else -0.3))
    reversion = -chg24 * 0.4 if abs(chg24) > 8 else 0.0
    # --- Statistics: volatility, z-score, RSI proxy, trend alignment ---
    vol = abs(chg24) / 10.0
    zscore = max(-3.0, min(3.0, chg24 / 5.0))  # ~5% = 1 std for crypto daily
    rs = max(chg24, 0.01) / max(-chg24, 0.01)
    rsi = 100 - (100 / (1 + rs))  # 0-100 bounded oscillator
    rsi_centered = (rsi - 50) / 50.0
    trend_align = 1.0 if (chg24 > 0) == (chg7d > 0) else -1.0
    base = [momentum, reversion, float(psych_bias),
            (fg_now - 50) / 50.0, fg_delta / 20.0, vol,
            acceleration / 5.0, zscore / 3.0, rsi_centered, trend_align]
    # Append the full model library — optimizer decides what earns its weight
    return base + build_model_features(chg24, chg7d)

# ---------------------------------------------------------------- GEOMETRY
def dot(a, b):
    return sum(x * y for x, y in zip(a, b))

def norm(a):
    return math.sqrt(sum(x * x for x in a)) or 1e-12

def cosine_sim(a, b):
    """Cosine similarity: 1 = identical direction, -1 = opposite, 0 = orthogonal."""
    return dot(a, b) / (norm(a) * norm(b))

DIVERSITY_FRACTION = 0.10  # max haircut on a pick's strength for being a near-twin of a chosen pick

def select_diverse_top(picks, k=3, fraction=DIVERSITY_FRACTION):
    """Greedy top-k with a co-movement haircut — the Quantum Lab lesson
    (2026-10-25), now in the live engine.

    The lab's portfolio QUBO measured greedy-by-score over-buying assets
    that move together (0.325 cost on a toy universe: the top scorers were
    one correlated cluster). Here each candidate's strength (|score|) is
    docked by fraction * its highest cosine similarity to an already-chosen
    pick, so the top 3 don't hand Nic three versions of the same trade.
    Similarity is measured on the DE-MEANED 5-window return vector
    (1h/24h/7d/14d/30d): de-meaning removes the shared market move, so
    only idiosyncratic co-movement counts — on a green day everything
    points the same way, and that must not trigger the dock. Picks without
    a vector (stocks: Finnhub gives day change only) are never docked.
    Returns the full list reordered: diversified top k first, the rest in
    original (score) order. Deterministic. The raw score is untouched —
    the dock lives on the pick as div_dock/div_twin for audit."""
    vecs = [p.get('tf_vector') for p in picks if p.get('tf_vector')]
    mean = None
    if vecs:
        mean = [sum(v[i] for v in vecs) / len(vecs)
                for i in range(len(vecs[0]))]

    def resid(p):
        v = p.get('tf_vector')
        if not v or mean is None:
            return None
        return [v[i] - mean[i] for i in range(len(v))]

    chosen, rest = [], list(picks)
    while rest and len(chosen) < k:
        best, best_strength, best_sim, best_twin = None, None, 0.0, None
        for p in rest:
            strength = abs(p['score'])
            r = resid(p)
            sim, twin = 0.0, None
            if r is not None:
                for c in chosen:
                    rc = resid(c)
                    if rc is None:
                        continue
                    s = cosine_sim(r, rc)
                    if s > sim:
                        sim, twin = s, c['asset']
            docked = strength * (1 - fraction * max(sim, 0.0))
            if best is None or docked > best_strength:
                best, best_strength, best_sim, best_twin = p, docked, sim, twin
        dock = abs(best['score']) - best_strength
        if dock > 0.5 and best_twin:
            best['div_dock'] = round(dock, 1)
            best['div_twin'] = best_twin
            best['reason'] += (f' [Diversity: -{dock:.1f} strength — '
                               f'moves with {best_twin}]')
        chosen.append(best)
        rest.remove(best)
    return chosen + rest

def geometric_edge(features, history):
    """
    Fast geometric pattern match: compare today's feature vector against the
    centroid of historical WINNING setups vs LOSING setups.
    Returns (edge, info) where edge > 0 favors the trade.
    """
    wins, losses = [], []
    for run in history.get('picks', []):
        if not run.get('scored'): continue
        for p in run.get('longs', []) + run.get('shorts', []):
            if _excluded_pick(p):
                continue
            f, r = p.get('features'), p.get('return_pct')
            if f and r is not None and len(f) <= N_FEATURES:
                pf = _pad(f)
                if r > 0: wins.append(pf)
                else: losses.append(pf)
    if len(wins) < 3 or len(losses) < 3:
        return 0.0, 'warming up'
    win_c = [sum(f[i] for f in wins) / len(wins) for i in range(N_FEATURES)]
    loss_c = [sum(f[i] for f in losses) / len(losses) for i in range(N_FEATURES)]
    edge = cosine_sim(features, win_c) - cosine_sim(features, loss_c)
    return edge, f'{len(wins)}W/{len(losses)}L centroids'

def _pad(f):
    """Pad older shorter feature vectors with zeros to current N_FEATURES."""
    f = list(f)
    while len(f) < N_FEATURES: f.append(0.0)
    return f[:N_FEATURES]

def learn_weights(history):
    """
    Train on scored historical picks: X = feature rows, y = realized returns.
    Solves for optimal weights via row echelon form. Needs >= N_FEATURES samples;
    falls back to heuristics before that.
    """
    X, y = [], []
    for run in history.get('picks', []):
        if not run.get('scored'): continue
        for p in run.get('longs', []) + run.get('shorts', []):
            if _excluded_pick(p):
                continue
            f = p.get('features')
            r = p.get('return_pct')
            if f and r is not None and len(f) <= N_FEATURES:
                X.append(_pad(f)); y.append(r)
    if len(X) < N_FEATURES:
        return HEURISTIC_W, {'trained': False, 'samples': len(X),
                             'note': f'warming up ({len(X)}/{N_FEATURES} samples) — heuristic weights'}
    # Ridge must scale with the data: the solver adds λ to the diagonal of
    # XᵀX (unnormalized sums over all rows), so the old fixed λ=1e-3 was
    # ~7 orders of magnitude below the diagonal (~3e4) and no regularization
    # at all. Worse, two feature columns are perfectly collinear in training
    # data (trend_align f9 == ma_align f11, corr 1.0), so XᵀX is singular
    # and the unregularized solve produced wild weights (max |w| ≈ 7192),
    # collapsing the score distribution to one side: on 2026-10-24 only 1
    # of 270 liquid coins scored negative, so the site published 1 short
    # for two straight runs despite a broadly down market. A data-scaled
    # ridge (1% of the mean XᵀX diagonal) restores a two-sided distribution
    # (71/270 negative on the same snapshot) at a ~2% in-sample RMSE cost.
    ridge = 0.01 * (sum(v * v for row in X for v in row) / len(X[0]))
    w = solve_least_squares(X, y, ridge=ridge)
    return w, {'trained': True, 'samples': len(X),
               'note': f'optimized on {len(X)} scored picks via row echelon form (ridge {ridge:.1f})'}

# ---------------------------------------------------------------- REGIME
def detect_regime(fg_now, xau_price, usd_jpy, macro=None):
    """Classify market regime + buy-conditions percentage (0-100)."""
    macro = macro or {}
    score = 0
    reasons = []
    if fg_now >= 55: score += 1; reasons.append('greed')
    elif fg_now <= 35: score -= 1; reasons.append('fear')
    if xau_price and xau_price > 4100: score -= 1; reasons.append('gold bid (safe-haven demand)')
    if usd_jpy and usd_jpy > 155: score -= 1; reasons.append('strong USD (dollar strength)')
    if usd_jpy and usd_jpy < 145: score += 1; reasons.append('weak USD (dollar weakness)')
    # Macro overlays (FRED)
    cpi_yoy = (macro.get('cpi_yoy') or {}).get('value')
    if cpi_yoy and cpi_yoy > 4: score -= 1; reasons.append(f'inflation hot ({cpi_yoy:.1f}% YoY)')
    elif cpi_yoy and cpi_yoy < 2: score += 1; reasons.append(f'inflation cool ({cpi_yoy:.1f}% YoY)')
    # Labor + spending overlays (FRED ICSA / PCE)
    icsa = macro.get('jobless_claims') or {}
    if icsa.get('value') and icsa.get('prev') and icsa['value'] > icsa['prev']:
        score -= 1; reasons.append('jobless claims rising')
    pce = macro.get('consumer_spending') or {}
    if pce.get('change_pct') is not None and pce['change_pct'] >= 0.5:
        score += 1; reasons.append('consumer spending strong')
    if score >= 1: regime = 'RISK_ON'
    elif score <= -1: regime = 'RISK_OFF'
    else: regime = 'NEUTRAL'
    # Buy-conditions percentage: 50 = neutral, higher = favors buying
    buy_pct = max(5, min(95, 50 + score * 12))
    if buy_pct >= 65:
        buy_text = f'Conditions favor buying ({buy_pct}%)'
    elif buy_pct <= 35:
        buy_text = f'Conditions favor caution ({100 - buy_pct}%)'
    else:
        buy_text = f'Mixed conditions ({buy_pct}% buy / {100 - buy_pct}% caution)'
    return regime, reasons, buy_pct, buy_text

def regime_adjustment(regime):
    return {
        'RISK_ON':  {'long_boost': 1.2, 'short_damp': 0.7,
                     'note': 'Conditions favor buying: lean into longs, demand stronger short signals'},
        'RISK_OFF': {'long_boost': 0.7, 'short_damp': 1.2,
                     'note': 'Conditions favor caution: lean into shorts/hedges, demand stronger long signals'},
        'NEUTRAL':  {'long_boost': 1.0, 'short_damp': 1.0,
                     'note': 'Neutral: no directional tilt'},
    }[regime]

# ---------------------------------------------------------------- THESIS
def build_thesis(asset, chg24, chg7d, price, score, conf, regime, fg_now, geo_edge, cls):
    """Readable investment thesis: bull/bear case, stop/target, risk-reward."""
    direction = 'LONG' if score > 0 else 'SHORT'
    vol = max(abs(chg24), 2.0)
    if direction == 'LONG':
        stop = price * (1 - vol / 100 * 1.5)
        target = price * (1 + vol / 100 * 2.0)
    else:
        stop = price * (1 + vol / 100 * 1.5)
        target = price * (1 - vol / 100 * 2.0)
    rr = abs(target - price) / max(abs(price - stop), 0.01)
    # Expected return % if target hits
    exp_return_pct = abs(target - price) / price * 100 if price else 0
    # Max loss % if stopped out
    max_loss_pct = abs(price - stop) / price * 100 if price else 0
    # Dates: entry today, projected exit in 3 days (midpoint of 1-5d swing)
    today = datetime.now(timezone.utc).date()
    exit_date = today + timedelta(days=3)
    bull, bear = [], []
    if chg24 > 3: bull.append(f'strong 24h momentum (+{chg24:.1f}%)')
    if chg24 < -3: bear.append(f'sharp 24h selloff ({chg24:.1f}%)')
    if (chg24 > 0) == (chg7d > 0) and abs(chg24) > 1:
        (bull if chg24 > 0 else bear).append('trend alignment across timeframes')
    if fg_now <= 25: bull.append('extreme fear — contrarian tailwind')
    if fg_now >= 75: bear.append('extreme greed — contrarian headwind')
    if geo_edge > 0.15: bull.append('pattern matches historical winners')
    if geo_edge < -0.15: bear.append('pattern resembles historical losers')
    if regime == 'RISK_ON' and direction == 'LONG': bull.append('conditions favor buying, which helps longs')
    if regime == 'RISK_OFF' and direction == 'SHORT': bear.append('conditions favor caution, which helps shorts')
    return {
        'direction': direction,
        'bull_case': bull or ['no strong bullish factors'],
        'bear_case': bear or ['no strong bearish factors'],
        'stop_loss': round_price(stop),
        'take_profit': round_price(target),
        'risk_reward': round(rr, 2),
        'time_horizon': '1-5 days (swing)',
        'entry_date': today.isoformat(),
        'projected_exit': exit_date.isoformat(),
        'expected_return_pct': round(exp_return_pct, 2),
        'max_loss_pct': round(max_loss_pct, 2),
    }

def think(data, history):
    """Generate long/short picks using learned optimal weights + psychology layer."""
    print('THINK: scoring assets (optimization)...')
    longs, shorts = [], []
    explanations = []

    crypto = data.get('crypto', {})
    fg_now = int(data['fear_greed'][0]['value']) if data.get('fear_greed') else 50
    fg_prev = int(data['fear_greed'][1]['value']) if len(data.get('fear_greed', [])) > 1 else fg_now
    fg_delta = fg_now - fg_prev

    # --- Psychology layer ("god particle"): crowd sentiment drives contrarian edge ---
    psych_bias = 0
    if fg_now <= 20:   psych_bias = +25; explanations.append(f'Extreme fear ({fg_now}) — capitulation zone, long bias')
    elif fg_now <= 35: psych_bias = +10; explanations.append(f'Fear ({fg_now}) — cautious long bias')
    elif fg_now >= 80: psych_bias = -25; explanations.append(f'Extreme greed ({fg_now}) — euphoria, short bias')
    elif fg_now >= 65: psych_bias = -10; explanations.append(f'Greed ({fg_now}) — trim-into-strength bias')
    if fg_delta <= -8: explanations.append(f'Sentiment collapsing ({fg_delta:+d}) — fear accelerating')
    if fg_delta >= +8: explanations.append(f'Sentiment surging ({fg_delta:+d}) — greed accelerating')

    # --- Learn optimal weights from history (row echelon form) ---
    weights, winfo = learn_weights(history)
    explanations.append('Weights: ' + winfo['note'])
    print(f"  weights = {[round(w, 3) for w in weights]} ({winfo['note']})")

    # --- Regime detection: different markets need different behavior ---
    metals = data.get('metals', {})
    fx = data.get('forex', {})
    xau = (metals.get('XAU') or {}).get('price')
    usd_jpy = fx.get('JPY')
    regime, regime_reasons, buy_pct, buy_text = detect_regime(fg_now, xau, usd_jpy, data.get('macro'))
    adj = regime_adjustment(regime)
    explanations.append(f'{buy_text} ({", ".join(regime_reasons) or "mixed signals"}) — {adj["note"]}')
    print(f"  regime = {regime} | {buy_text}")

    # --- Kelly position sizing from track record ---
    stats = history.get('stats', {})
    wr = (stats.get('win_rate') or 50) / 100.0
    # Estimate avg win/loss from scored history
    avg_w, avg_l = _avg_win_loss(history)
    kelly = kelly_fraction(wr, avg_w, avg_l)
    print(f"  kelly = {kelly:.3f} (win_rate={wr:.2f})")

    # --- Score crypto with w·x (ensemble: regression + geometry + regime) ---
    BLOCKED_SYMBOLS = {'XMR',  # not tradable on Coinbase (delisted)
                       # Stablecoins are cash, not trades — never pick or alert them (USDT incident 2026-10-23)
                       'USDT', 'USDC', 'DAI', 'TUSD', 'FDUSD', 'PYUSD', 'USDP', 'GUSD', 'FRAX', 'USDD', 'LUSD', 'SUSD', 'USDE', 'CRVUSD', 'EURC',
                       # Pegged / money-market tokens that leaked into the 2026-10-25 longs (all ~$1, no upside)
                       'RLUSD', 'U', 'EURSAFO', 'EUTBL', 'THBILL',
                       # Second wave, same morning: SG-FORGE euro/dollar stables
                       # (EURCV/USDCV), Origin Dollar (OUSD) and Web 3 Dollar (USD3)
                       'EURCV', 'USDCV', 'OUSD', 'USD3'}
    listings = data.get('listings', {})
    def tradable_on(sym):
        """Where Nic can trade this coin: subset of ['coinbase', 'cryptocom']."""
        out = []
        if sym in listings.get('coinbase', set()): out.append('coinbase')
        if sym in listings.get('cryptocom', set()): out.append('cryptocom')
        return out
    for cid, info in crypto.items():
        chg24 = info.get('usd_24h_change') or 0
        chg7d = (info.get('usd_7d_change') or 0)
        price = info.get('usd') or 0
        sym = info.get('symbol') or cid.upper()
        if sym in BLOCKED_SYMBOLS:
            continue
        # Behavioral peg filter: a coin that has not moved more than 1% on
        # ANY window (1h/24h/7d/14d/30d) is cash, not a trade. The name
        # blocklist keeps missing new stablecoins — EURCV, OUSD and USD3
        # leaked into the 2026-10-25 longs hours after the 03:10 blocklist
        # fix. A real swing candidate moves >1% in a month; a flat coin
        # only fills the top-10 when the market is weak.
        windows = [info.get('usd_1h_change') or 0, chg24, chg7d,
                   info.get('usd_14d_change') or 0, info.get('usd_30d_change') or 0]
        if max(abs(w) for w in windows) < 1.0:
            continue
        # Skip dust / illiquid: no price, or market cap under $100M (only liquid, tradable assets)
        if not price or price <= 0:
            continue
        if (info.get('market_cap') or 0) < 100_000_000:
            continue
        feats = extract_features(chg24, chg7d, psych_bias, fg_now, fg_delta)
        score = sum(w * f for w, f in zip(weights, feats))
        # Geometric layer: fastest pattern match via cosine similarity
        g_edge, g_info = geometric_edge(feats, history)
        score += g_edge * 10.0
        # Regime tilt
        if score > 0: score *= adj['long_boost']
        else: score *= adj['short_damp']
        conf = clamp(50 + abs(score) * 1.5)
        # Multi-timeframe alignment: count windows (1h/24h/7d/14d/30d) that
        # agree with the pick direction. Full agreement +3 conf, near-none -3.
        tf_windows = [info.get('usd_1h_change') or 0, chg24, chg7d,
                      info.get('usd_14d_change') or 0, info.get('usd_30d_change') or 0]
        tf_agree = sum(1 for w in tf_windows if w != 0 and (w > 0) == (score > 0))
        tf_align = f'{tf_agree}/5 timeframes agree'
        if tf_agree == 5: conf = min(97, conf + 3)
        elif tf_agree <= 1: conf = max(35, conf - 3)
        thesis = build_thesis(sym, chg24, chg7d, price, score, conf, regime, fg_now, g_edge, 'crypto')
        reason = (f'{sym} {chg24:+.1f}% (24h), {chg7d:+.1f}% (7d) @ {fmt_price(price)}. '
                  f'Ensemble score {score:+.1f} (w·x + geo {g_edge:+.2f} + {regime}).')
        pick = {'asset': sym, 'price': round_price(price), 'score': round(score, 1),
                'confidence': round(conf), 'reason': reason, 'class': 'crypto',
                'market': 'crypto',
                'name': info.get('name') or sym,
                'info_url': f'https://www.coingecko.com/en/coins/{cid}',
                'tradable': tradable_on(sym),
                'tf_align': tf_align,
                'tf_vector': [round(w, 3) for w in tf_windows],
                'features': [round(f, 3) for f in feats], 'geo_edge': round(g_edge, 3),
                'thesis': thesis, 'position_pct': round(kelly * 100, 1),
                'regime': regime}
        (longs if score > 0 else shorts).append(pick)

    # --- Metals as risk barometer ---
    metals = data.get('metals', {})
    xau = (metals.get('XAU') or {}).get('price')
    if xau:
        # Gold strength = risk-off → temper crypto longs
        risk_off = xau > 4000
        if risk_off:
            explanations.append(f'Gold ${xau:,.0f} — safe-haven bid, trimming long confidence')
            for p in longs: p['confidence'] = max(40, p['confidence'] - 5)

    # --- Forex: USD strength = risk-off for crypto ---
    fx = data.get('forex', {})
    if fx.get('JPY'):
        # JPY per USD high = strong dollar = risk-off
        if fx['JPY'] > 155:
            explanations.append('Strong USD (JPY>155) — headwind for risk assets')
            for p in longs: p['confidence'] = max(40, p['confidence'] - 3)

    # --- Stocks (if Finnhub key present) — same feature model ---
    # SEC EDGAR CIKs for deep-dive filing links (public domain data)
    TICKER_CIK = {
        'SPY': 884394, 'DIA': 1041130,
        'AAPL': 320193, 'MSFT': 789019, 'NVDA': 1045810, 'AMZN': 1018724,
        'META': 1326801, 'GOOGL': 1652044, 'TSLA': 1318605, 'AVGO': 1730168,
        'JPM': 19617, 'V': 1403161, 'MA': 1141391, 'XOM': 2115436,
        'CVX': 93410, 'UNH': 731766, 'LLY': 59478, 'COST': 909832,
        'HD': 354950, 'PG': 80424, 'JNJ': 200406, 'BAC': 70858,
        'WMT': 104169, 'DIS': 1744489, 'NFLX': 1065280, 'CRM': 1108524,
        'ORCL': 1341439, 'PLTR': 1321655, 'MSTR': 1050446, 'COIN': 1679788,
    }
    def edgar_url(sym):
        cik = TICKER_CIK.get(sym)
        if cik:
            return f'https://www.sec.gov/edgar/browse/?CIK={cik}&owner=exclude'
        # Fallback: EDGAR company search for ETFs without direct CIK
        return f'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company={sym}&type=&dateb=&owner=include&count=10'
    STOCK_NAMES = {
        'SPY': 'SPDR S&P 500 ETF', 'QQQ': 'Invesco QQQ (Nasdaq 100)', 'DIA': 'SPDR Dow Jones ETF',
        'IWM': 'iShares Russell 2000 ETF', 'XLK': 'Technology Select Sector SPDR', 'XLF': 'Financial Select Sector SPDR',
        'XLE': 'Energy Select Sector SPDR', 'XLV': 'Health Care Select Sector SPDR', 'XLI': 'Industrial Select Sector SPDR',
        'XLU': 'Utilities Select Sector SPDR', 'XLP': 'Consumer Staples Select Sector SPDR', 'XLY': 'Consumer Discretionary Select Sector SPDR',
        'XLRE': 'Real Estate Select Sector SPDR', 'XLB': 'Materials Select Sector SPDR', 'XLC': 'Communication Services Select Sector SPDR',
        'AAPL': 'Apple Inc.', 'MSFT': 'Microsoft Corp.', 'NVDA': 'NVIDIA Corp.', 'AMZN': 'Amazon.com Inc.',
        'META': 'Meta Platforms Inc.', 'GOOGL': 'Alphabet Inc.', 'TSLA': 'Tesla Inc.', 'AVGO': 'Broadcom Inc.',
        'JPM': 'JPMorgan Chase & Co.', 'V': 'Visa Inc.', 'MA': 'Mastercard Inc.', 'XOM': 'Exxon Mobil Corp.',
        'CVX': 'Chevron Corp.', 'UNH': 'UnitedHealth Group Inc.', 'LLY': 'Eli Lilly and Co.', 'COST': 'Costco Wholesale Corp.',
        'HD': 'Home Depot Inc.', 'PG': 'Procter & Gamble Co.', 'JNJ': 'Johnson & Johnson', 'BAC': 'Bank of America Corp.',
        'NRR': 'Bitwise NEAR ETF', 'SCHD': 'Schwab US Dividend Equity ETF',
        'CRM': 'Salesforce Inc.', 'ORCL': 'Oracle Corp.', 'AMD': 'Advanced Micro Devices Inc.', 'NFLX': 'Netflix Inc.',
        'DIS': 'Walt Disney Co.', 'INTC': 'Intel Corp.', 'COIN': 'Coinbase Global Inc.', 'MSTR': 'MicroStrategy Inc.',
        'PLTR': 'Palantir Technologies Inc.',
    }
    # --- SEC insider activity: count Form 4 filings in the last 90 days ---
    # v1 counts filings only — buy vs sell needs per-form parsing, so the note
    # never claims a direction. Per-run cache; SEC downtime must never break us.
    # NOTE: SEC 403s user-agents without a name + email-style contact.
    _sec_cache = {}
    def insider_activity(cik):
        if cik in _sec_cache:
            return _sec_cache[cik]
        result = None
        try:
            sub = fetch_json(f'https://data.sec.gov/submissions/CIK{cik:010d}.json',
                             headers={'User-Agent': 'OmniSight Research contact@omnisight.local',
                                      'Accept': 'application/json'})
            recent = (sub or {}).get('filings', {}).get('recent', {})
            forms = recent.get('form', [])
            dates = recent.get('filingDate', [])
            cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).strftime('%Y-%m-%d')
            count = 0
            for form, fdate in zip(forms, dates):
                if fdate < cutoff:
                    break  # filings are newest-first — nothing older counts
                if form == '4':
                    count += 1
            result = count
        except Exception as e:
            print(f'  SEC insider lookup failed for CIK {cik}: {e}')
        _sec_cache[cik] = result
        return result

    for sym, q in data.get('stocks', {}).items():
        dp = q.get('dp') or 0  # day percent change
        c = q.get('c') or 0
        feats = extract_features(dp, dp * 2, psych_bias, fg_now, fg_delta)
        score = sum(w * f for w, f in zip(weights, feats))
        g_edge, _ = geometric_edge(feats, history)
        score += g_edge * 10.0
        if score > 0: score *= adj['long_boost']
        else: score *= adj['short_damp']
        conf = clamp(50 + abs(score) * 2)
        thesis = build_thesis(sym, dp, dp * 2, c, score, conf, regime, fg_now, g_edge, 'equity')
        reason = f'{sym} {dp:+.2f}% today @ ${c:,.2f}. Ensemble score {score:+.1f} (w·x + geo + {regime}).'
        pick = {'asset': sym, 'price': round_price(c), 'score': round(score, 1),
                'confidence': round(conf), 'reason': reason, 'class': 'equity',
                'market': 'stock',
                'name': STOCK_NAMES.get(sym, sym),
                'info_url': f'https://finance.yahoo.com/quote/{sym}',
                'edgar_url': edgar_url(sym),
                'cik': TICKER_CIK.get(sym),
                'tf_align': None,  # Finnhub gives day change only — no multi-window data
                'features': [round(f, 3) for f in feats], 'geo_edge': round(g_edge, 3),
                'thesis': thesis, 'position_pct': round(kelly * 100, 1),
                'regime': regime}
        # Insider activity (Form 4 count, 90d) — activity signal, not direction
        cik = TICKER_CIK.get(sym)
        if cik:
            n_insider = insider_activity(cik)
            if n_insider is not None:
                pick['insider_recent'] = n_insider
                if n_insider >= 3:
                    pick['reason'] += (f' Insider activity: {n_insider} Form 4 filings '
                                       f'in 90d — see SEC deep dive.')
        (longs if score > 0 else shorts).append(pick)

    # --- Contracts watch (Contract Intel): flag mega-awards with tickers ---
    # Major government contractors → ticker mapping
    CONTRACTOR_TICKERS = {
        'lockheed martin': 'LMT', 'boeing': 'BA', 'raytheon': 'RTX', 'rtx corp': 'RTX',
        'northrop grumman': 'NOC', 'general dynamics': 'GD', 'l3harris': 'LHX',
        'huntington ingalls': 'HII', 'leidos': 'LDOS', 'booz allen': 'BAH',
        'caci': 'CACI', 'saic': 'SAIC', 'parsons': 'PSN', 'kbr': 'KBR',
        'fluor': 'FLR', 'jacobs': 'J', 'aecom': 'ACM', 'tetra tech': 'TTEK',
        'palantir': 'PLTR', 'accenture federal': 'ACN', 'deloitte': None,
        'general electric': 'GE', 'honeywell': 'HON', 'textron': 'TXT',
        'osha': None, 'amazon web services': 'AMZN', 'aws': 'AMZN',
        'microsoft': 'MSFT', 'google': 'GOOGL', 'oracle': 'ORCL',
        'spacex': None, 'blue origin': None,  # private
    }
    def recipient_ticker(name):
        if not name: return None
        nl = name.lower()
        for key, ticker in CONTRACTOR_TICKERS.items():
            if key in nl:
                return ticker
        return None

    contracts = data.get('contracts', [])
    mega = [c for c in contracts if (c.get('Award Amount') or 0) > 500_000_000]
    contract_notes = []
    for c in mega[:5]:
        name = c.get('Recipient Name', '?')
        ticker = recipient_ticker(name)
        tick_str = f' [{ticker}]' if ticker else ''
        contract_notes.append(
            f"${c['Award Amount']/1e9:.2f}B → {name}{tick_str} ({c.get('Awarding Agency','?')})"
        )

    longs.sort(key=lambda p: -p['score'])
    shorts.sort(key=lambda p: p['score'])

    # --- Correlation-aware top picks (Quantum Lab lesson, 2026-10-25) ---
    # Greedy-by-score over-buys assets that move together — the lab's
    # portfolio QUBO measured it. The top 3 of each side are now chosen
    # with a co-movement haircut on the 5-window return vector, so the
    # Picks page doesn't hand Nic three versions of the same trade.
    longs = select_diverse_top(longs)
    shorts = select_diverse_top(shorts)

    # --- News-risk cross-reference: flag picks named in risk headlines ---
    # Motivated by the ZEC/NU7 miss (2026-10-20): the engine went long ZEC
    # while a "22,309 ZEC stuck for good" headline sat unused in its own feed.
    # If a pick's symbol or full name appears in a headline title that also
    # carries a risk keyword, dock confidence 8 pts (floor 35) and mark it.
    import re as _re
    RISK_KEYWORDS = ['freeze', 'frozen', 'hack', 'hacked', 'exploit', 'breach',
                     'delist', 'delisted', 'lawsuit', 'sued', 'sec', 'fraud',
                     'scam', 'rug', 'bankrupt', 'insolvency', 'halt', 'suspended',
                     'investigation', 'probe', 'crash', 'plunge', 'warning',
                     'stuck', 'vulnerability']
    def news_risk_title(pick):
        sym = pick.get('asset', '')
        name = (pick.get('name') or '').lower()
        for h in data.get('headlines', []):
            title = h.get('title') or ''
            if not title:
                continue
            tl = title.lower()
            if not any(_re.search(r'\b' + _re.escape(kw) + r'\b', tl) for kw in RISK_KEYWORDS):
                continue
            sym_hit = bool(sym) and _re.search(r'\b' + _re.escape(sym) + r'\b', title)
            name_hit = len(name) >= 3 and name in tl
            if sym_hit or name_hit:
                return title
        return None
    for p in longs + shorts:
        ntitle = news_risk_title(p)
        if ntitle:
            p['news_flag'] = ntitle
            p['confidence'] = max(35, p['confidence'] - 8)
            p['reason'] += f' ⚠️ News risk: {ntitle}'
            print(f"  news risk flagged: {p['asset']} — {ntitle[:70]}")

    # Newsletter market flags: the digest pre-scans its headlines for risk
    # terms (hack, SEC, lawsuit, ...). A pick named in one of those flagged
    # headlines gets the exact same news_flag treatment as above — same
    # dock, same reason suffix, same BUY exclusion downstream.
    RISK_FLAG_TERMS = {'hack', 'exploit', 'sec', 'lawsuit', 'ban', 'delist',
                       'fraud', 'breach', 'stolen'}
    digest_flags = [f for f in (data.get('newsletter_digest') or {})
                    .get('market_flags', [])
                    if str(f.get('term', '')).lower() in RISK_FLAG_TERMS]
    for p in longs + shorts:
        if p.get('news_flag'):
            continue
        sym = p.get('asset', '')
        name = (p.get('name') or '').lower()
        for flag in digest_flags:
            title = flag.get('headline') or ''
            if not title:
                continue
            tl = title.lower()
            if (sym and _re.search(r'\b' + _re.escape(sym) + r'\b', title)) or \
               (len(name) >= 3 and name in tl):
                p['news_flag'] = title
                p['confidence'] = max(35, p['confidence'] - 8)
                p['reason'] += f' ⚠️ News risk: {title}'
                print(f"  news risk flagged (newsletter): {p['asset']} — {title[:70]}")
                break

    # --- Empirical calibration ("no fake calibration") ---
    # Pull stated confidence toward the band's REALIZED win rate so a pick
    # stamped 86% isn't really a 59% proposition. Evidence-gated: bands
    # with < 10 scored picks keep the raw value. Runs before alert
    # generation so alerts and pick cards use the same honest number.
    calib_applied = []
    for p in longs + shorts:
        raw = p['confidence']
        cal, label, n, emp = calibrate_confidence(raw, history.get('calibration'))
        p['conf_raw'] = raw  # pre-calibration value, kept for audit
        if cal != raw:
            p['confidence'] = cal
            p['calib_band'] = label
            calib_applied.append(f"{p['asset']} {raw}%→{cal}% ({label}: n={n}, {emp}%)")
    if calib_applied:
        explanations.append('Calibration: ' + '; '.join(calib_applied[:6]))
        print(f"  calibration: {len(calib_applied)} picks adjusted")
    else:
        print('  calibration: no band has enough samples yet — raw confidence kept')

    # --- Generate buy/sell alerts from high-conviction picks ---
    # Nic's rule (2026-10-21): the alerts feed is a push surface, not a
    # research surface. A BUY alert fires only for something he can
    # actually buy on his own venues (the pick carries a Coinbase /
    # Crypto.com listing, or it is a US-listed equity for Schwab); a
    # SELL alert fires only for an asset he actually holds
    # (NIC_HOLDINGS). Everything else stays on the Picks page, badged,
    # and out of his alert stream. The reason string carries the entry
    # price — a signal without a buy price is not actionable.
    alerts = []
    for p in longs[:3] + shorts[:3]:
        if p['confidence'] >= 65:
            action = 'BUY' if p in longs else 'SELL'
            if action == 'BUY':
                if p.get('class') != 'equity' and not (p.get('tradable') or []):
                    continue
            elif p['asset'] not in NIC_HOLDINGS:
                continue
            t = p.get('thesis', {})
            alerts.append({
                'type': action,
                'asset': p['asset'],
                'price': p['price'],
                'confidence': p['confidence'],
                'asset_class': p.get('class', 'crypto'),
                'reason': f"{action} signal: {p['asset']} @ ${round_price(p['price'])} (confidence {p['confidence']}%). " +
                          f"Stop ${t.get('stop_loss', '?')}, target ${t.get('take_profit', '?')}.",
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })
    # Regime shift alert
    if regime in ('RISK_ON', 'RISK_OFF'):
        alerts.append({
            'type': 'REGIME',
            'asset': 'MARKET',
            'price': 0,
            'confidence': 70,
            'reason': f"Regime: {REGIME_LABELS.get(regime, regime)} — {', '.join(regime_reasons[:3])}.",
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })

    # Contract Intel alerts — mega awards (>$500M) with tickers
    for c in mega[:3]:
        name = c.get('Recipient Name', '?')
        ticker = recipient_ticker(name)
        amt = c.get('Award Amount', 0)
        alerts.append({
            'type': 'CONTRACT',
            'asset': ticker or name,
            'price': round(amt / 1e9, 2),
            'confidence': 85,
            'asset_class': 'contract',
            'reason': f"${amt/1e9:.2f}B award → {name}" + (f" [{ticker}]" if ticker else "") + f" ({c.get('Awarding Agency','?')}).",
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })

    # Liquidity Engine alerts — significant macro moves
    macro = data.get('macro', {})
    cpi = macro.get('cpi_yoy', {})
    if cpi.get('value'):
        cpi_v = cpi['value']
        if cpi_v > 4:
            alerts.append({
                'type': 'MACRO', 'asset': 'CPI', 'price': cpi_v, 'confidence': 75,
                'asset_class': 'macro',
                'reason': f"CPI YoY at {cpi_v}% — above 4% (hot inflation — tilts conditions toward caution).",
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })
        elif cpi_v < 2:
            alerts.append({
                'type': 'MACRO', 'asset': 'CPI', 'price': cpi_v, 'confidence': 75,
                'asset_class': 'macro',
                'reason': f"CPI YoY at {cpi_v}% — below 2% (cool inflation — tilts conditions toward buying).",
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })
    # Liquidity Engine alert — jobless claims spike (>10% vs prior week)
    icsa = macro.get('jobless_claims', {})
    if icsa.get('change_pct') and icsa['change_pct'] > 10 and icsa.get('value'):
        alerts.append({
            'type': 'MACRO', 'asset': 'ICSA', 'price': icsa['value'], 'confidence': 80,
            'asset_class': 'macro',
            'reason': f"Jobless claims jumped {icsa['change_pct']:+.1f}% to {icsa['value']:,.0f} — labor market cooling fast, tilts conditions toward caution.",
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })

    # --- Regime FLIP alert: compare to previous run ---
    prev_regime = None
    try:
        picks_hist = history.get('picks', [])
        if picks_hist:
            prev_regime = picks_hist[-1].get('regime')
    except Exception:
        pass
    regime_flipped = prev_regime and prev_regime != regime
    if regime_flipped:
        alerts.append({
            'type': 'REGIME_FLIP',
            'asset': 'MARKET',
            'price': 0,
            'confidence': 85,
            'reason': f"🔄 REGIME FLIP: {REGIME_LABELS.get(prev_regime, prev_regime)} → "
                      f"{REGIME_LABELS.get(regime, regime)}. {buy_text}.",
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })
        print(f"  REGIME FLIP: {prev_regime} → {regime}")

    # --- Long-term outlook (30-90d, macro-driven) ---
    macro = data.get('macro', {})
    cpi_yoy = (macro.get('cpi_yoy') or {}).get('value')
    lt_outlook = {
        'horizon': '30-90 days',
        'regime': regime,
        'thesis': [],
    }
    if regime == 'RISK_ON':
        lt_outlook['thesis'].append('Macro favors risk assets — equities and crypto have tailwinds.')
    elif regime == 'RISK_OFF':
        lt_outlook['thesis'].append('Defensive posture — gold, Treasuries, and cash preserve capital.')
    else:
        lt_outlook['thesis'].append('Mixed signals — selective positioning, wait for regime clarity.')
    if cpi_yoy and cpi_yoy > 3:
        lt_outlook['thesis'].append(f'Inflation at {cpi_yoy:.1f}% YoY erodes cash — real assets preferred.')
    if cpi_yoy and cpi_yoy < 2.5:
        lt_outlook['thesis'].append(f'Inflation cool at {cpi_yoy:.1f}% — Fed has room to ease, bullish for duration.')

    # News output: first 5 wire headlines + up to 5 newsletter headlines
    # (tagged with the newsletter name as their source).
    _hl = data.get('headlines', [])
    headlines_out = [{'title': h.get('title'), 'source': h.get('source')}
                     for h in _hl[:5]]
    headlines_out += [{'title': h.get('title'), 'source': h.get('source')}
                      for h in _hl if h.get('via') == 'newsletter'][:5]

    return {
        'longs': longs[:10], 'shorts': shorts[:10],
        'psychology': {'fear_greed': fg_now, 'delta': fg_delta, 'bias': psych_bias,
                       'notes': explanations},
        'weights': [round(w, 4) for w in weights],
        'weight_info': winfo,
        'regime': regime,
        'buy_pct': buy_pct,
        'buy_text': buy_text,
        'kelly_pct': round(kelly * 100, 1),
        'contracts_watch': contract_notes,
        'opportunities': data.get('opportunities', []),
        'opportunities_nonprofit': data.get('opportunities_nonprofit', []),
        'foresight': data.get('foresight', []),
        'fed_commentary': data.get('fed_commentary', []),
        'headlines': headlines_out,
        'alerts': alerts,
        'long_term': lt_outlook,
    }

def _excluded_pick(p):
    """A pick removed from the graded record (e.g. the 9 fake CVX wins
    scored against Chevron's price by the pre-fix symbol collision).
    Excluded picks stay in the file for audit but never count again —
    not in stats, calibration, error, weight training, geometry, or
    Kelly sizing. Nic approved the removal 2026-10-24; the record
    itself carries the correction note."""
    return bool(p.get('excluded'))


def calibration_from_picks(picks):
    """Bucket scored picks by stated confidence band and compare against
    realized win rate — "says 70-79% → actually wins X%". Excluded
    picks never count."""
    bands = CONF_BANDS
    calib = {b[0]: {'n': 0, 'wins': 0} for b in bands}
    for run in picks:
        for p in run.get('longs', []) + run.get('shorts', []):
            if _excluded_pick(p):
                continue
            if p.get('win') is None or p.get('confidence') is None:
                continue
            for label, lo, hi in bands:
                if lo <= p['confidence'] <= hi:
                    calib[label]['n'] += 1
                    if p['win']: calib[label]['wins'] += 1
                    break
    return [
        {'band': label, 'n': c['n'],
         'win_rate': round(100 * c['wins'] / c['n'], 1) if c['n'] else None}
        for label, c in calib.items()]


def _avg_win_loss(history):
    """Mean |win| / |loss| over scored, non-excluded picks. Feeds Kelly
    sizing; a single absurd return (like the +9,000% fake CVX wins)
    used to dominate the average and inflate every position size."""
    wins_r, loss_r = [], []
    for run in history.get('picks', []):
        for p in run.get('longs', []) + run.get('shorts', []):
            if _excluded_pick(p):
                continue
            r = p.get('return_pct')
            if r is not None:
                (wins_r if r > 0 else loss_r).append(abs(r))
    avg_w = sum(wins_r) / len(wins_r) if wins_r else 5.0
    avg_l = sum(loss_r) / len(loss_r) if loss_r else 5.0
    return avg_w, avg_l


def load_history():
    hist_path = os.path.join(DATA_DIR, 'history.json')
    try:
        with open(hist_path) as f: return json.load(f)
    except: return {'picks': [], 'stats': {'wins': 0, 'losses': 0, 'total': 0}}

# ---------------------------------------------------------------- REMEMBER
def remember(data, signals):
    """Score previous picks against current prices; build track record + error metrics."""
    print('REMEMBER: scoring prior picks...')
    history = load_history()
    stats = history.setdefault('stats', {'wins': 0, 'losses': 0, 'total': 0})
    # Error tracking: 5% margin-of-error target — the thesis's stated
    # expected return (%) vs the realized return (%). Both are percents,
    # so MAE reads in %-points and "within 5%" means the call landed
    # within 5 points of its own expectation. (2026-10-26: the old metric
    # compared the unitless ensemble score (~15) against the realized
    # return (%), so MAE printed as 223% and within-5% drifted down to
    # 13.4% — a units mismatch, not model decay. The counters below are
    # reset once by the migration so the series stays honest.)
    err = history.setdefault('error', {'sum_abs_err': 0.0, 'n': 0, 'within_5pct': 0})
    if err.get('metric') != 'return_pct_v1':
        before_err = {k: v for k, v in err.items()}
        history.setdefault('corrections', []).append({
            'date': datetime.now(timezone.utc).isoformat(),
            'action': 'redefined model-error metric (score-vs-return -> expected-return-vs-realized-return)',
            'reason': 'Old metric compared the unitless ensemble score (~15) with the realized return (%), so MAE printed 223% and within-5% drifted to 13.4% — a units mismatch, not model decay. New metric: |thesis expected_return_pct - realized return_pct| (return is direction-adjusted, so a winning short counts positive). Counters reset; old values preserved here.',
            'before': {'error': before_err},
            'after': {'error': {'sum_abs_err': 0.0, 'n': 0, 'within_5pct': 0, 'metric': 'return_pct_v1'}},
        })
        err.clear()
        err.update({'sum_abs_err': 0.0, 'n': 0, 'within_5pct': 0, 'metric': 'return_pct_v1'})

    # Current prices for scoring — keyed by MARKET then symbol. Crypto and
    # stock tickers collide (CVX = Convex Finance AND Chevron; META =
    # MetaDAO AND Meta Platforms). One symbol-keyed dict let the stock
    # quote overwrite the crypto price and scored crypto CVX longs at
    # +9,000% — nine fake wins in the graded record. Keep the books
    # separate and match each pick to its own market's price.
    px_crypto = {}
    for cid, info in data.get('crypto', {}).items():
        if info.get('usd'): px_crypto[(info.get('symbol') or cid).upper()] = info['usd']
    px_stock = {}
    for sym, q in data.get('stocks', {}).items():
        if q.get('c'): px_stock[sym] = q['c']

    def price_for(p):
        # Picks written before the market field existed fall back to class;
        # crypto is the default (the engine was crypto-only for months).
        market = p.get('market') or ('stock' if p.get('class') == 'equity' else 'crypto')
        return (px_stock if market == 'stock' else px_crypto).get(p['asset'])

    # Score any unscored picks from prior runs
    for run in history['picks']:
        if run.get('scored'): continue
        scored_any = False
        for p in run.get('longs', []) + run.get('shorts', []):
            if _excluded_pick(p):
                continue  # removed from the graded record — never re-score
            cur = price_for(p)
            if not cur or not p.get('price'): continue
            ret = (cur - p['price']) / p['price'] * 100
            if p in run.get('shorts', []): ret = -ret
            p['exit'] = round_price(cur); p['return_pct'] = round(ret, 2)
            p['win'] = ret > 0
            # Error metric: how far was the thesis's expected return (%) from
            # the realized return (%)? `ret` is direction-adjusted (a winning
            # short is positive), and expected_return_pct is a positive
            # magnitude, so this penalizes wrong-direction calls too. Picks
            # without a thesis expectation (pre-thesis era) skip error
            # tracking — they still count in win/loss.
            exp_ret = (p.get('thesis') or {}).get('expected_return_pct')
            if exp_ret is not None:
                abs_err = abs(abs(exp_ret) - ret)
                err['sum_abs_err'] += abs_err; err['n'] += 1
                if abs_err <= 5.0: err['within_5pct'] += 1
                p['abs_error'] = round(abs_err, 2)
            scored_any = True
            stats['total'] += 1
            stats['wins' if ret > 0 else 'losses'] += 1
        if scored_any: run['scored'] = True

    # Append today's picks as a new run
    now = datetime.now(timezone.utc).isoformat()
    history['picks'].append({
        'date': now, 'scored': False,
        'longs': signals['longs'], 'shorts': signals['shorts'],
        'fear_greed': signals['psychology']['fear_greed'],
        'regime': signals.get('regime'),
    })
    # Keep last 90 runs
    history['picks'] = history['picks'][-90:]

    s = stats
    s['win_rate'] = round(100 * s['wins'] / s['total'], 1) if s['total'] else None
    # Calibration: bucket scored picks by stated confidence band and compare
    # against realized win rate — "says 70-79% → actually wins X%"
    history['calibration'] = calibration_from_picks(history['picks'])
    # Error summary: mean absolute error + % within the 5% target
    if err['n']:
        err['mae'] = round(err['sum_abs_err'] / err['n'], 2)
        err['within_5pct_rate'] = round(100 * err['within_5pct'] / err['n'], 1)
    else:
        err['mae'] = None; err['within_5pct_rate'] = None
    return history

# ---------------------------------------------------------------- PUBLISH
def publish(data, signals, history, data_pulled_at=None):
    now = datetime.now(timezone.utc)
    stamp = now.strftime('%Y-%m-%dT%H-%M')

    # Snapshot raw data (listings sets → lists so JSON can serialize)
    snap_data = dict(data)
    if 'listings' in snap_data:
        snap_data['listings'] = {k: sorted(v) for k, v in snap_data['listings'].items()}
    with open(os.path.join(SNAP_DIR, f'{stamp}.json'), 'w') as f:
        json.dump({'timestamp': now.isoformat(), 'data': snap_data}, f)

    # Key-gated feeds (FRED macro/rates) are skipped on keyless local runs.
    # Don't publish an empty dashboard section because of that: carry the
    # previous run's values forward so the site never regresses to blank
    # between keyed runs. Flags let the frontend label them as carried.
    macro = data.get('macro') or {}
    rates = data.get('rates') or {}
    macro_carried = rates_carried = False
    if not macro or not rates:
        try:
            with open(os.path.join(DATA_DIR, 'latest.json')) as f:
                prev = json.load(f)
            if not macro and prev.get('macro'):
                macro = prev['macro']; macro_carried = True
            if not rates and prev.get('rates'):
                rates = prev['rates']; rates_carried = True
            if macro_carried or rates_carried:
                print(f'  publish: carried forward macro={macro_carried} rates={rates_carried} from previous run')
        except Exception as e:
            print(f'  publish: carry-forward unavailable ({e})')

    # Same carry-forward protection for the Kalshi summary: a run where
    # the Kalshi API hiccups must not blank the panel.
    kalshi = data.get('kalshi') or {}
    kalshi_carried = False
    if not kalshi:
        try:
            with open(os.path.join(DATA_DIR, 'latest.json')) as f:
                prev_k = json.load(f)
            if prev_k.get('kalshi'):
                kalshi = prev_k['kalshi']; kalshi_carried = True
        except Exception:
            pass

    latest = {
        'generated': now.isoformat(),
        'data_pulled_at': data_pulled_at,
        'published_at': now.isoformat(),
        'fear_greed': signals['psychology']['fear_greed'],
        'longs': signals['longs'], 'shorts': signals['shorts'],
        'psychology': signals['psychology'],
        'weights': signals.get('weights'),
        'weight_info': signals.get('weight_info'),
        'contracts_watch': signals['contracts_watch'],
        'opportunities': signals.get('opportunities', []),
        'opportunities_nonprofit': signals.get('opportunities_nonprofit', []),
        'foresight': signals.get('foresight', []),
        'fed_commentary': data.get('fed_commentary', []),
        'headlines': signals['headlines'],
        'newsletters': ({'date': data['newsletter_digest'].get('date'),
                         'sources': [s.get('name') for s in
                                     data['newsletter_digest'].get('sources', [])]}
                        if data.get('newsletter_digest') else None),
        'newsletter_flags': (data.get('newsletter_digest') or {}).get('market_flags', []),
        'track_record': history['stats'],
        'model_error': history.get('error', {}),
        'calibration': history.get('calibration', []),
        'regime': signals.get('regime'),
        'regime_label': REGIME_LABELS.get(signals.get('regime'), signals.get('regime')),
        'buy_pct': signals.get('buy_pct'),
        'buy_text': signals.get('buy_text'),
        'kelly_pct': signals.get('kelly_pct'),
        'macro': macro,
        'macro_carried': macro_carried,
        'rates': rates,
        'rates_carried': rates_carried,
        'kalshi': kalshi,
        'kalshi_carried': kalshi_carried,
        'alerts': signals.get('alerts', []),
        'long_term': signals.get('long_term', {}),
    }
    with open(os.path.join(DATA_DIR, 'latest.json'), 'w') as f:
        json.dump(latest, f, indent=2)
    with open(os.path.join(DATA_DIR, 'history.json'), 'w') as f:
        json.dump(history, f, indent=2)
    print(f'PUBLISH: data/latest.json written ({len(signals["longs"])}L/{len(signals["shorts"])}S)')

def main():
    print('=== NexIntel loop starting ===')
    data = ingest()
    # Market-data pull complete — stamp it so the frontend can show
    # "Data pulled … ET" next to "Published … ET" for cross-referencing.
    pulled_at = datetime.now(timezone.utc).isoformat()
    history = load_history()          # load BEFORE think — weights learn from it
    signals = think(data, history)
    history = remember(data, signals) # remember() reloads + scores + appends
    publish(data, signals, history, pulled_at)
    print('=== NexIntel loop complete ===')

if __name__ == '__main__':
    main()
