#!/usr/bin/env python3
"""
NexIntel Intraday Engine — hourly buy/sell signals for crypto.
Runs separately from the swing engine (nexintel.py).

Strategy: hourly momentum + mean-reversion on top 50 coins by market cap.
Horizon: 1-4 hours. Tighter stops than swing (0.8x hourly vol).
"""
import json, os, math, urllib.request, subprocess
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '..', 'data')
# Resolve relative to repo root
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, 'data')

def fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': 'NexIntel-Intraday/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f'  [warn] {url[:70]}... failed: {e}')
        return None

def hourly_features(prices):
    """Extract intraday features from hourly price series (24 points)."""
    if len(prices) < 5:
        return None
    closes = [p[1] for p in prices]
    now = closes[-1]
    chg_1h = (now - closes[-2]) / closes[-2] * 100 if closes[-2] else 0
    chg_4h = (now - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 and closes[-5] else 0
    chg_24h = (now - closes[0]) / closes[0] * 100 if closes[0] else 0

    # Hourly volatility (std of hourly returns)
    rets = [(closes[i] - closes[i-1]) / closes[i-1] * 100 for i in range(1, len(closes)) if closes[i-1]]
    vol = (sum(r*r for r in rets) / len(rets)) ** 0.5 if rets else 1.0

    # RSI proxy (14h)
    gains = [max(r, 0) for r in rets[-14:]]
    losses = [abs(min(r, 0)) for r in rets[-14:]]
    avg_g = sum(gains) / 14 if gains else 0
    avg_l = sum(losses) / 14 if losses else 0.001
    rsi = 100 - 100 / (1 + avg_g / avg_l) if avg_l else 50

    # Distance from 24h high/low (breakout signal)
    hi = max(closes)
    lo = min(closes)
    rng = hi - lo if hi != lo else 1
    pos_in_range = (now - lo) / rng  # 0 = at low, 1 = at high

    # Acceleration: is momentum speeding up?
    accel = chg_1h - (sum(rets[-4:-1]) / 3 if len(rets) >= 4 else 0)

    return {
        'chg_1h': chg_1h, 'chg_4h': chg_4h, 'chg_24h': chg_24h,
        'vol': max(vol, 0.3), 'rsi': rsi,
        'pos_in_range': pos_in_range, 'accel': accel, 'price': now,
    }

def score_intraday(f):
    """Score: positive = long, negative = short. Weighted intraday signals."""
    score = 0
    # Hourly momentum (strongest signal intraday)
    score += f['chg_1h'] * 3.0
    score += f['chg_4h'] * 1.5
    # RSI mean-reversion at extremes
    if f['rsi'] < 30: score += (30 - f['rsi']) * 0.5   # oversold bounce
    if f['rsi'] > 70: score -= (f['rsi'] - 70) * 0.5    # overbought fade
    # Breakout: near highs with momentum = continuation
    if f['pos_in_range'] > 0.9 and f['chg_1h'] > 0: score += 2.0
    if f['pos_in_range'] < 0.1 and f['chg_1h'] < 0: score -= 2.0
    # Acceleration confirms
    score += f['accel'] * 2.0
    # Don't fight the 24h trend too hard
    if (score > 0) != (f['chg_24h'] > 0):
        score *= 0.6
    return score

def build_intraday_thesis(sym, f, score):
    direction = 'LONG' if score > 0 else 'SHORT'
    vol = f['vol']
    price = f['price']
    if direction == 'LONG':
        stop = price * (1 - vol / 100 * 0.8)
        target = price * (1 + vol / 100 * 1.2)
    else:
        stop = price * (1 + vol / 100 * 0.8)
        target = price * (1 - vol / 100 * 1.2)

    now = datetime.now(timezone.utc)
    # Intraday horizon: 2 hours typical
    from datetime import timedelta
    exit_time = now + timedelta(hours=2)

    exp_ret = abs(target - price) / price * 100
    max_loss = abs(price - stop) / price * 100

    bull, bear = [], []
    if f['chg_1h'] > 1: bull.append(f"+{f['chg_1h']:.1f}% last hour momentum")
    if f['chg_1h'] < -1: bear.append(f"{f['chg_1h']:.1f}% last hour drop")
    if f['rsi'] < 30: bull.append(f"RSI {f['rsi']:.0f} — oversold bounce setup")
    if f['rsi'] > 70: bear.append(f"RSI {f['rsi']:.0f} — overbought, fade setup")
    if f['pos_in_range'] > 0.9: bull.append("breaking 24h highs")
    if f['pos_in_range'] < 0.1: bear.append("breaking 24h lows")

    return {
        'direction': direction,
        'entry': round(price, 4) if price < 10 else round(price, 2),
        'stop_loss': round(stop, 4) if price < 10 else round(stop, 2),
        'take_profit': round(target, 4) if price < 10 else round(target, 2),
        'entry_time': now.strftime('%H:%M UTC'),
        'exit_by': exit_time.strftime('%H:%M UTC'),
        'horizon': '1-4 hours',
        'expected_return_pct': round(exp_ret, 2),
        'max_loss_pct': round(max_loss, 2),
        'rsi': round(f['rsi'], 0),
        'chg_1h': round(f['chg_1h'], 2),
        'bull_case': bull or ['intraday momentum alignment'],
        'bear_case': bear or ['intraday momentum divergence'],
    }

def main():
    print('INTRADAY: hourly signals...')
    # Coins not tradable on major US exchanges (delisted / regulatory)
    BLOCKED = {'XMR',  # Monero — delisted from Coinbase
               }
    # Exchange listings — which coins Nic can actually trade
    listings_cb, listings_cdc = set(), set()
    try:
        cb_prods = fetch_json('https://api.exchange.coinbase.com/products') or []
        listings_cb = {p['base_currency'] for p in cb_prods
            if p.get('quote_currency') == 'USD' and p.get('status') == 'online' and p.get('base_currency')}
    except Exception:
        pass
    try:
        cdc = fetch_json('https://api.crypto.com/exchange/v1/public/get-instruments') or {}
        listings_cdc = {i['base_ccy'] for i in (cdc.get('result', {}).get('data', []) or [])
            if i.get('inst_type') == 'CCY_PAIR' and i.get('quote_ccy') in ('USD', 'USDT') and i.get('base_ccy')}
    except Exception:
        pass
    def tradable_on(sym):
        out = []
        if sym in listings_cb: out.append('coinbase')
        if sym in listings_cdc: out.append('cryptocom')
        return out
    # Broad universe for the movers scan: top 1,000 by market cap (4 pages).
    # Page 1 doubles as the top-50 universe for the signal scan below.
    universe = []
    for page in range(1, 5):
        mkts_page = fetch_json('https://api.coingecko.com/api/v3/coins/markets'
            f'?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}'
            '&price_change_percentage=1h,24h')
        if not mkts_page:
            break
        universe.extend(mkts_page)
    if not universe:
        print('  FAILED: no market data')
        return
    mkts = universe[:50]

    signals = []
    for c in mkts:
        cid = c['id']
        sym = (c['symbol'] or '').upper()
        if sym in BLOCKED:
            continue
        mcap = c.get('market_cap') or 0
        if mcap < 100_000_000:
            continue
        # Hourly chart (24h)
        chart = fetch_json(f'https://api.coingecko.com/api/v3/coins/{cid}/market_chart'
            '?vs_currency=usd&days=1&interval=hourly')
        if not chart or not chart.get('prices'):
            continue
        f = hourly_features(chart['prices'])
        if not f:
            continue
        score = score_intraday(f)
        conf = min(95, max(40, 50 + abs(score) * 3))
        # Only signals with meaningful score
        if abs(score) < 3:
            continue
        thesis = build_intraday_thesis(sym, f, score)
        signals.append({
            'asset': sym, 'score': round(score, 2), 'confidence': round(conf),
            'direction': thesis['direction'], 'thesis': thesis,
            'name': c.get('name') or sym,
            'info_url': f'https://www.coingecko.com/en/coins/{cid}',
            'tradable': tradable_on(sym),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })

    signals.sort(key=lambda s: -abs(s['score']))
    longs = [s for s in signals if s['direction'] == 'LONG'][:5]
    shorts = [s for s in signals if s['direction'] == 'SHORT'][:5]

    # --- Movers scan (two tiers) over the full top-1,000 universe ---
    # 🚀 MOVER: already moving — |1h| >= 5% or |24h| >= 10%, mcap >= $100M.
    # 👀 WATCH: early signs — |1h| >= 3% AND (volume surge OR fresh inflection).
    #   Volume-surge approximation: CoinGecko gives only current 24h volume
    #   (no per-coin baseline), so "surge" = the coin's volume/market-cap
    #   ratio sits in the TOP QUARTILE of the scanned universe — unusually
    #   heavy trading relative to the coin's own size.
    #   Fresh inflection: 1h move >= 3% while the 24h trend is still against
    #   it (24h <= 0 for an up-turn, >= 0 for a down-turn) — the turn is
    #   starting before the daily numbers confirm it.
    def mover_fields(c, direction):
        return {
            'asset': (c['symbol'] or '').upper(),
            'name': c.get('name') or (c['symbol'] or '').upper(),
            'price': c.get('current_price') or 0,
            'chg_1h': round(c.get('price_change_percentage_1h_in_currency') or 0, 2),
            'chg_24h': round(c.get('price_change_percentage_24h_in_currency')
                             if c.get('price_change_percentage_24h_in_currency') is not None
                             else (c.get('price_change_percentage_24h') or 0), 2),
            'market_cap': c.get('market_cap') or 0,
            'direction': direction,
            'tradable': tradable_on((c['symbol'] or '').upper()),
            'info_url': f"https://www.coingecko.com/en/coins/{c['id']}",
        }
    scanned = [c for c in universe
               if (c.get('market_cap') or 0) >= 100_000_000
               and (c['symbol'] or '').upper() not in BLOCKED]
    ratios = sorted((c.get('total_volume') or 0) / c['market_cap']
                    for c in scanned if c.get('market_cap'))
    vol_q3 = ratios[int(len(ratios) * 0.75)] if ratios else float('inf')
    movers, watch = [], []
    for c in scanned:
        chg1 = c.get('price_change_percentage_1h_in_currency') or 0
        chg24 = c.get('price_change_percentage_24h_in_currency')
        if chg24 is None:
            chg24 = c.get('price_change_percentage_24h') or 0
        if abs(chg1) >= 5 or abs(chg24) >= 10:
            direction = ('UP' if chg24 > 0 else 'DOWN') if abs(chg24) >= abs(chg1) \
                        else ('UP' if chg1 > 0 else 'DOWN')
            movers.append(mover_fields(c, direction))
            continue  # a mover is never also a watch item
        if abs(chg1) >= 3:
            vol_ratio = (c.get('total_volume') or 0) / c['market_cap'] if c.get('market_cap') else 0
            surge = vol_ratio >= vol_q3
            inflection = (chg1 >= 3 and chg24 <= 0) or (chg1 <= -3 and chg24 >= 0)
            if surge or inflection:
                w = mover_fields(c, 'UP' if chg1 > 0 else 'DOWN')
                w['watch_reason'] = ('volume surge' if surge else '') + \
                    (' + ' if surge and inflection else '') + \
                    ('fresh inflection' if inflection else '')
                watch.append(w)
    movers.sort(key=lambda m: -abs(m['chg_24h']))
    watch.sort(key=lambda m: -abs(m['chg_1h']))
    movers, watch = movers[:10], watch[:10]
    print(f'  Movers scan: {len(movers)} movers, {len(watch)} watching (of {len(scanned)} scanned)')

    out = {
        'generated': datetime.now(timezone.utc).isoformat(),
        'mode': 'intraday',
        'horizon': '1-4 hours',
        'longs': longs,
        'shorts': shorts,
        'movers': movers,
        'watch': watch,
        'coins_scanned': len(mkts),
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, 'intraday.json'), 'w') as f:
        json.dump(out, f, indent=2)
    print(f'  Intraday complete: {len(longs)} longs, {len(shorts)} shorts from {len(mkts)} coins')

if __name__ == '__main__':
    main()
