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
    # Top 50 coins by market cap (1 call)
    mkts = fetch_json('https://api.coingecko.com/api/v3/coins/markets'
        '?vs_currency=usd&order=market_cap_desc&per_page=50&page=1')
    if not mkts:
        print('  FAILED: no market data')
        return

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
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })

    signals.sort(key=lambda s: -abs(s['score']))
    longs = [s for s in signals if s['direction'] == 'LONG'][:5]
    shorts = [s for s in signals if s['direction'] == 'SHORT'][:5]

    out = {
        'generated': datetime.now(timezone.utc).isoformat(),
        'mode': 'intraday',
        'horizon': '1-4 hours',
        'longs': longs,
        'shorts': shorts,
        'coins_scanned': len(mkts),
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, 'intraday.json'), 'w') as f:
        json.dump(out, f, indent=2)
    print(f'  Intraday complete: {len(longs)} longs, {len(shorts)} shorts from {len(mkts)} coins')

if __name__ == '__main__':
    main()
