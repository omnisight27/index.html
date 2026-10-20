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
                '&price_change_percentage=24h,7d')
            if not mkts:
                break
            for c in mkts:
                d['crypto'][c['id']] = {
                    'usd': c['current_price'],
                    'usd_24h_change': c.get('price_change_percentage_24h') or 0,
                    'usd_7d_change': c.get('price_change_percentage_7d_in_currency') or 0,
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
            d['opportunities'] = [{
                'title': o.get('title', '?'),
                'agency': (o.get('officeAddress') or {}).get('agency', '?'),
                'type': o.get('type', {}).get('value', '?') if isinstance(o.get('type'), dict) else o.get('type', '?'),
                'posted': o.get('postedDate', '?'),
                'deadline': o.get('responseDeadLine', '?'),
                'naics': o.get('naicsCode', '?'),
            } for o in opps[:25]]
            print(f"  SAM.gov: {len(d['opportunities'])} opportunities ingested")
        except Exception as e:
            print(f'  SAM.gov failed: {e}')
    else:
        print('  [info] SAM_GOV_API_KEY absent — opportunities feed skipped')

    # Headlines
    news = fetch_json('https://freenewsapi.ai/v1/search?q=markets%20stocks%20crypto&date=2d&lang=en&size=10&sort=date')
    d['headlines'] = (news or {}).get('results', [])[:10]

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
        symbols = ['SPY', 'QQQ', 'DIA', 'IWM',                      # indices
                   'XLK', 'XLF', 'XLE', 'XLV', 'XLI', 'XLU', 'XLP', 'XLY', 'XLRE', 'XLB', 'XLC',  # sectors
                   'AAPL', 'MSFT', 'NVDA', 'AMZN', 'META', 'GOOGL', 'TSLA', 'AVGO', 'JPM',
                   'V', 'MA', 'XOM', 'CVX', 'UNH', 'LLY', 'COST', 'HD', 'PG', 'JNJ', 'BAC',
                   'CRM', 'ORCL', 'AMD', 'NFLX', 'DIS', 'INTC', 'COIN', 'MSTR', 'PLTR']  # mega-caps + crypto-adjacent
        for sym in symbols:
            q = fetch_json(f'https://finnhub.io/api/v1/quote?symbol={sym}&token={finnhub_key}')
            if q and q.get('c'): d['stocks'][sym] = q
        print(f"  stocks: {len(d['stocks'])} symbols ingested")
    else:
        print('  [info] FINNHUB_API_KEY absent — stocks feed skipped (add key in GitHub Secrets)')

    return d

# ---------------------------------------------------------------- THINK
def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))

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
            f = p.get('features')
            r = p.get('return_pct')
            if f and r is not None and len(f) <= N_FEATURES:
                X.append(_pad(f)); y.append(r)
    if len(X) < N_FEATURES:
        return HEURISTIC_W, {'trained': False, 'samples': len(X),
                             'note': f'warming up ({len(X)}/{N_FEATURES} samples) — heuristic weights'}
    w = solve_least_squares(X, y)
    return w, {'trained': True, 'samples': len(X),
               'note': f'optimized on {len(X)} scored picks via row echelon form'}

# ---------------------------------------------------------------- REGIME
def detect_regime(fg_now, xau_price, usd_jpy, macro=None):
    """Classify market regime + buy-conditions percentage (0-100)."""
    macro = macro or {}
    score = 0
    reasons = []
    if fg_now >= 55: score += 1; reasons.append('greed')
    elif fg_now <= 35: score -= 1; reasons.append('fear')
    if xau_price and xau_price > 4100: score -= 1; reasons.append('gold bid (risk-off)')
    if usd_jpy and usd_jpy > 155: score -= 1; reasons.append('strong USD (risk-off)')
    if usd_jpy and usd_jpy < 145: score += 1; reasons.append('weak USD (risk-on)')
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
                     'note': 'Risk-on: favor longs, demand stronger short signals'},
        'RISK_OFF': {'long_boost': 0.7, 'short_damp': 1.2,
                     'note': 'Risk-off: favor shorts/hedges, demand stronger long signals'},
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
    if regime == 'RISK_ON' and direction == 'LONG': bull.append('risk-on regime favors longs')
    if regime == 'RISK_OFF' and direction == 'SHORT': bear.append('risk-off regime favors shorts')
    return {
        'direction': direction,
        'bull_case': bull or ['no strong bullish factors'],
        'bear_case': bear or ['no strong bearish factors'],
        'stop_loss': round(stop, 2),
        'take_profit': round(target, 2),
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
    wins_r, loss_r = [], []
    for run in history.get('picks', []):
        for p in run.get('longs', []) + run.get('shorts', []):
            r = p.get('return_pct')
            if r is not None:
                (wins_r if r > 0 else loss_r).append(abs(r))
    avg_w = sum(wins_r) / len(wins_r) if wins_r else 5.0
    avg_l = sum(loss_r) / len(loss_r) if loss_r else 5.0
    kelly = kelly_fraction(wr, avg_w, avg_l)
    print(f"  kelly = {kelly:.3f} (win_rate={wr:.2f})")

    # --- Score crypto with w·x (ensemble: regression + geometry + regime) ---
    BLOCKED_SYMBOLS = {'XMR'}  # not tradable on Coinbase (delisted)
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
        thesis = build_thesis(sym, chg24, chg7d, price, score, conf, regime, fg_now, g_edge, 'crypto')
        reason = (f'{sym} {chg24:+.1f}% (24h), {chg7d:+.1f}% (7d) @ ${price:,.0f}. '
                  f'Ensemble score {score:+.1f} (w·x + geo {g_edge:+.2f} + {regime}).')
        pick = {'asset': sym, 'price': round(price, 2), 'score': round(score, 1),
                'confidence': round(conf), 'reason': reason, 'class': 'crypto',
                'name': info.get('name') or sym,
                'info_url': f'https://www.coingecko.com/en/coins/{cid}',
                'tradable': tradable_on(sym),
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
            explanations.append(f'Gold ${xau:,.0f} — risk-off bid, trimming long confidence')
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
        'CRM': 'Salesforce Inc.', 'ORCL': 'Oracle Corp.', 'AMD': 'Advanced Micro Devices Inc.', 'NFLX': 'Netflix Inc.',
        'DIS': 'Walt Disney Co.', 'INTC': 'Intel Corp.', 'COIN': 'Coinbase Global Inc.', 'MSTR': 'MicroStrategy Inc.',
        'PLTR': 'Palantir Technologies Inc.',
    }
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
        pick = {'asset': sym, 'price': round(c, 2), 'score': round(score, 1),
                'confidence': round(conf), 'reason': reason, 'class': 'equity',
                'name': STOCK_NAMES.get(sym, sym),
                'info_url': f'https://finance.yahoo.com/quote/{sym}',
                'edgar_url': edgar_url(sym),
                'cik': TICKER_CIK.get(sym),
                'features': [round(f, 3) for f in feats], 'geo_edge': round(g_edge, 3),
                'thesis': thesis, 'position_pct': round(kelly * 100, 1),
                'regime': regime}
        (longs if score > 0 else shorts).append(pick)

    # --- Contracts watch (Govgistics): flag mega-awards with tickers ---
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

    # --- Generate buy/sell alerts from high-conviction picks ---
    alerts = []
    for p in longs[:3] + shorts[:3]:
        if p['confidence'] >= 65:
            action = 'BUY' if p in longs else 'SELL'
            t = p.get('thesis', {})
            alerts.append({
                'type': action,
                'asset': p['asset'],
                'price': p['price'],
                'confidence': p['confidence'],
                'asset_class': p.get('class', 'crypto'),
                'reason': f"{action} signal: {p['asset']} (confidence {p['confidence']}%). " +
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
            'reason': f"Regime: {regime} — {', '.join(regime_reasons[:3])}.",
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
                'reason': f"CPI YoY at {cpi_v}% — above 4% (hot inflation, risk-off tilt).",
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })
        elif cpi_v < 2:
            alerts.append({
                'type': 'MACRO', 'asset': 'CPI', 'price': cpi_v, 'confidence': 75,
                'asset_class': 'macro',
                'reason': f"CPI YoY at {cpi_v}% — below 2% (cool inflation, risk-on tilt).",
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })
    # Liquidity Engine alert — jobless claims spike (>10% vs prior week)
    icsa = macro.get('jobless_claims', {})
    if icsa.get('change_pct') and icsa['change_pct'] > 10 and icsa.get('value'):
        alerts.append({
            'type': 'MACRO', 'asset': 'ICSA', 'price': icsa['value'], 'confidence': 80,
            'asset_class': 'macro',
            'reason': f"Jobless claims jumped {icsa['change_pct']:+.1f}% to {icsa['value']:,.0f} — labor market cooling fast (risk-off tilt).",
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
        flip_dir = 'BULLISH' if regime == 'RISK_ON' else 'BEARISH' if regime == 'RISK_OFF' else 'NEUTRAL'
        alerts.append({
            'type': 'REGIME_FLIP',
            'asset': 'MARKET',
            'price': 0,
            'confidence': 85,
            'reason': f"🔄 REGIME FLIP: {prev_regime} → {regime} ({flip_dir}). {buy_text}.",
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
        'fed_commentary': data.get('fed_commentary', []),
        'headlines': [{'title': h.get('title'), 'source': h.get('source')} 
                      for h in data.get('headlines', [])[:5]],
        'alerts': alerts,
        'long_term': lt_outlook,
    }

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
    # Error tracking: 5% margin-of-error target — |predicted| vs |realized|
    err = history.setdefault('error', {'sum_abs_err': 0.0, 'n': 0, 'within_5pct': 0})

    # Current prices for scoring — keyed by SYMBOL to match pick assets
    # (CoinGecko ids like 'arbitrum' never match pick symbols like 'ARB')
    px = {}
    for cid, info in data.get('crypto', {}).items():
        if info.get('usd'): px[(info.get('symbol') or cid).upper()] = info['usd']
    for sym, q in data.get('stocks', {}).items():
        if q.get('c'): px[sym] = q['c']

    # Score any unscored picks from prior runs
    for run in history['picks']:
        if run.get('scored'): continue
        scored_any = False
        for p in run.get('longs', []) + run.get('shorts', []):
            cur = px.get(p['asset'])
            if not cur or not p.get('price'): continue
            ret = (cur - p['price']) / p['price'] * 100
            if p in run.get('shorts', []): ret = -ret
            p['exit'] = round(cur, 2); p['return_pct'] = round(ret, 2)
            p['win'] = ret > 0
            # Error metric: how far was |predicted score| from |actual return|?
            abs_err = abs(abs(p.get('score') or 0) - abs(ret))
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
    # Error summary: mean absolute error + % within the 5% target
    if err['n']:
        err['mae'] = round(err['sum_abs_err'] / err['n'], 2)
        err['within_5pct_rate'] = round(100 * err['within_5pct'] / err['n'], 1)
    else:
        err['mae'] = None; err['within_5pct_rate'] = None
    return history

# ---------------------------------------------------------------- PUBLISH
def publish(data, signals, history):
    now = datetime.now(timezone.utc)
    stamp = now.strftime('%Y-%m-%dT%H-%M')

    # Snapshot raw data (listings sets → lists so JSON can serialize)
    snap_data = dict(data)
    if 'listings' in snap_data:
        snap_data['listings'] = {k: sorted(v) for k, v in snap_data['listings'].items()}
    with open(os.path.join(SNAP_DIR, f'{stamp}.json'), 'w') as f:
        json.dump({'timestamp': now.isoformat(), 'data': snap_data}, f)

    latest = {
        'generated': now.isoformat(),
        'fear_greed': signals['psychology']['fear_greed'],
        'longs': signals['longs'], 'shorts': signals['shorts'],
        'psychology': signals['psychology'],
        'weights': signals.get('weights'),
        'weight_info': signals.get('weight_info'),
        'contracts_watch': signals['contracts_watch'],
        'opportunities': signals.get('opportunities', []),
        'fed_commentary': data.get('fed_commentary', []),
        'headlines': signals['headlines'],
        'track_record': history['stats'],
        'model_error': history.get('error', {}),
        'regime': signals.get('regime'),
        'buy_pct': signals.get('buy_pct'),
        'buy_text': signals.get('buy_text'),
        'kelly_pct': signals.get('kelly_pct'),
        'macro': data.get('macro', {}),
        'rates': data.get('rates', {}),
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
    history = load_history()          # load BEFORE think — weights learn from it
    signals = think(data, history)
    history = remember(data, signals) # remember() reloads + scores + appends
    publish(data, signals, history)
    print('=== NexIntel loop complete ===')

if __name__ == '__main__':
    main()
