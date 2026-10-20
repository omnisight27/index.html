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

def build_mover_plan(cid, price):
    """LONG trade plan for an UP mover, using the same hourly-vol math as
    build_intraday_thesis (stop = entry x (1 - vol x 0.8), target = entry x
    (1 + vol x 1.2)). Returns None when the chart feed fails so one bad
    coin can never break the run."""
    from datetime import timedelta
    chart = fetch_json(f'https://api.coingecko.com/api/v3/coins/{cid}/market_chart'
        '?vs_currency=usd&days=1&interval=hourly')
    if not chart or not chart.get('prices'):
        return None
    f = hourly_features(chart['prices'])
    if not f:
        return None
    vol = f['vol']
    entry = price or f['price']
    stop = entry * (1 - vol / 100 * 0.8)
    target = entry * (1 + vol / 100 * 1.2)
    exit_time = datetime.now(timezone.utc) + timedelta(hours=3)
    # Same rounding as the thesis for normal prices; extra decimals for
    # sub-cent coins (e.g. PEPE) so their plans don't round to $0.00.
    if entry >= 10:
        rnd = lambda v: round(v, 2)
    elif entry >= 0.01:
        rnd = lambda v: round(v, 4)
    else:
        rnd = lambda v: round(v, 8)
    return {
        'entry': rnd(entry),
        'take_profit': rnd(target),
        'stop_loss': rnd(stop),
        'exit_by': exit_time.strftime('%H:%M UTC'),
        'horizon': '1-4 hours',
        'expected_return_pct': round(abs(target - entry) / entry * 100, 2),
        'max_loss_pct': round(abs(entry - stop) / entry * 100, 2),
    }

def _sig(v):
    """Round to 6 significant digits — keeps accum_history payloads lean."""
    return float(f'{v:.6g}') if v else 0

def accum_reason(hist, vol_ratio, vol_q90, chg_1h, now):
    """Pre-move accumulation detector (pure function — unit-tested).
    hist = stored observations for one coin INCLUDING the current run's
    observation as the last element. Needs >= 3 observations spanning
    >= 2 hours; returns (reason, vol_growth_pct, price_drift_pct) or None.
    Patterns:
      A) Quiet volume build — 24h volume up >= 20% vs the observation ~3h
         ago while price over the same span stayed within +/-2.5%.
      B) Ratio climb — vol_ratio in the top 10% of the universe AND above
         the previous run's value, with |chg_1h| < 2% (price still calm).
    """
    if len(hist) < 3:
        return None
    def ts(o):
        return datetime.fromisoformat(o['t'])
    if (ts(hist[-1]) - ts(hist[0])).total_seconds() / 3600 < 2:
        return None
    cur = hist[-1]
    # Reference observation ~3h back: oldest candidates are >= 2h old,
    # pick the one whose age is closest to 3h.
    cands = [o for o in hist[:-1] if (ts(cur) - ts(o)).total_seconds() / 3600 >= 2]
    if not cands:
        return None
    ref = min(cands, key=lambda o: abs((ts(cur) - ts(o)).total_seconds() / 3600 - 3))
    ref_age_h = (ts(cur) - ts(ref)).total_seconds() / 3600
    growth = drift = None
    if ref.get('vol24h'):
        growth = (cur['vol24h'] - ref['vol24h']) / ref['vol24h'] * 100
        drift = (cur['price'] - ref['price']) / ref['price'] * 100 if ref.get('price') else 0
        if growth >= 20 and abs(drift) <= 2.5:
            return (f"Volume building {growth:+.0f}% in {ref_age_h:.0f}h, price still quiet"
                    " — accumulation pattern", round(growth, 1), round(drift, 2))
    prev = hist[-2]
    if vol_ratio >= vol_q90 and vol_ratio > (prev.get('vol_ratio') or 0) and abs(chg_1h) < 2:
        return ("Volume in top 10% for its size and still climbing, price quiet"
                " — accumulation pattern",
                round(growth, 1) if growth is not None else 0.0,
                round(drift, 2) if drift is not None else 0.0)
    return None

def strong_day(chg24, chg1, vol_ratio, vol_med):
    """Strong-day watch predicate (pure function — unit-tested): a coin
    grinding >= 8% over 24h in steps — no single explosive hour — counts
    as setting up IF the current hour is still moving the same direction
    (>= +0.5% / <= -0.5%) AND its volume ratio is at least the universe
    median. Case study: CVX +9.24% 24h on 2026-10-20 tripped no tier."""
    if chg24 >= 8:
        return chg1 >= 0.5 and vol_ratio >= vol_med
    if chg24 <= -8:
        return chg1 <= -0.5 and vol_ratio >= vol_med
    return False

# ---------------------------------------------------------------------------
# 📒 Proof Book — a public paper-trading ledger of every intraday call.
#
# Persistence route: the intraday workflow commits ONLY data/intraday.json
# (see .github/workflows/intraday-loop.yml, `git add data/intraday.json`),
# so a separate data/proof.json would never survive a production run. The
# ledger therefore rides inside data/intraday.json under the `proof` key —
# the same precedent as accum_history above. The Reports page reads it
# from there.
#
# Conventions: every call is graded as if $1,000 rode it (paper money).
# A call that touches its target first is 'won' (+expected_return_pct);
# one that touches its stop first is 'lost' (-max_loss_pct); one that
# reaches its exit time + 2h grace untouched is 'expired' and graded at
# the actual price drift. Anything unresolved after 7 days expires too.
# Grading walks the hourly price path SINCE ENTRY (main() tops up chart
# fetches so every open call's asset has one, not just the top-50 signal
# universe); first touch in time order decides. If no chart is
# available, the fallback is extremes actually observed run over run
# (current-price snapshots accumulated on the entry) — the markets
# payload's trailing-24h high/low is trusted ONLY once a call is >= 24h
# old, because before that the window mostly predates the call (a local
# end-to-end test graded 17/17 fresh calls instantly against pre-entry
# movement — exactly what a proof book must never do). Where both levels
# are crossed inside one observation gap, order is unknowable and the
# call is graded a loss: the book never credits an ambiguous win.
# Simplification: the hourly path is hourly closes, so a spike that
# fully reverted inside a single hour can be missed.
# ---------------------------------------------------------------------------
PAPER_USD = 1000

def _iso_parse(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None

def _proof_candidate(kind, asset, name, direction, plan, now, horizon_h):
    """Normalize a signal thesis / mover plan into a ledger entry.
    horizon_h matches the builders: signals exit at +2h
    (build_intraday_thesis), mover plans at +3h (build_mover_plan)."""
    from datetime import timedelta
    if not plan:
        return None
    try:
        entry = float(plan.get('entry'))
        tp = float(plan.get('take_profit'))
        sl = float(plan.get('stop_loss'))
    except (TypeError, ValueError):
        return None
    if not entry or entry <= 0:
        return None
    exp_ret = plan.get('expected_return_pct')
    if exp_ret is None:
        exp_ret = round(abs(tp - entry) / entry * 100, 2)
    max_loss = plan.get('max_loss_pct')
    if max_loss is None:
        max_loss = round(abs(entry - sl) / entry * 100, 2)
    return {
        'id': f'{kind}:{asset}:{int(now.timestamp())}',
        'ts_open': now.isoformat(),
        'kind': kind,
        'asset': asset,
        'name': name or asset,
        'direction': direction or 'LONG',
        'entry': entry,
        'take_profit': tp,
        'stop_loss': sl,
        'exit_by': (now + timedelta(hours=horizon_h)).isoformat(),
        'expected_return_pct': exp_ret,
        'max_loss_pct': max_loss,
        'status': 'open',
        'result_pct': None,
        'paper_pnl': None,
        'ts_closed': None,
        'last_price': entry,
    }

def proof_record_new(record, candidates, now):
    """Prepend fresh calls to the ledger (kept newest-first). Dedup: the
    same asset+kind is never re-recorded while an entry is still open,
    nor within 24h of a previous entry — movers re-fire every hour and
    must not flood the book."""
    fresh = []
    for cand in candidates:
        if not cand:
            continue
        dup = False
        for e in record:
            if e.get('asset') != cand['asset'] or e.get('kind') != cand['kind']:
                continue
            if e.get('status') == 'open':
                dup = True
                break
            opened = _iso_parse(e.get('ts_open') or '')
            if opened and (now - opened).total_seconds() < 24 * 3600:
                dup = True
                break
        if not dup:
            fresh.append(cand)
    record[:] = fresh + record
    return len(fresh)

def proof_grade(record, mkt_by_sym, charts_by_sym, now):
    """Grade every open ledger entry against fresh market data.
    mkt_by_sym: {sym: {'price', 'high_24h', 'low_24h'}} from the markets
    payload; charts_by_sym: {sym: [[ts_ms, price], ...]} hourly points —
    main() tops this up so every open call's asset is covered."""
    from datetime import timedelta
    for e in record:
        if e.get('status') != 'open':
            continue
        sym = e['asset']
        is_long = e.get('direction') != 'SHORT'
        tp, sl, entry = e['take_profit'], e['stop_loss'], e['entry']
        opened = _iso_parse(e.get('ts_open') or '')
        mkt = mkt_by_sym.get(sym) or {}
        cur = mkt.get('price')
        if cur:
            e['last_price'] = cur
        status = result = None
        if cur:
            pts = []
            chart = charts_by_sym.get(sym)
            if chart and opened:
                opened_ms = opened.timestamp() * 1000
                pts = [pr for ts, pr in chart if ts >= opened_ms]
            if pts:
                # Walk in time order — first touch decides. Current price
                # is the final observation.
                for p in pts + [cur]:
                    if is_long:
                        if p >= tp:
                            status = 'won'
                            break
                        if p <= sl:
                            status = 'lost'
                            break
                    else:
                        if p <= tp:
                            status = 'won'
                            break
                        if p >= sl:
                            status = 'lost'
                            break
            else:
                # No hourly path: extremes actually observed since entry
                # (accumulated snapshots), plus the trailing-24h window
                # once it lies entirely after entry (age >= 24h).
                hi = max(e.get('obs_hi') or cur, cur)
                lo = min(e.get('obs_lo') or cur, cur)
                if opened and (now - opened).total_seconds() >= 24 * 3600:
                    if mkt.get('high_24h') is not None:
                        hi = max(hi, mkt['high_24h'])
                    if mkt.get('low_24h') is not None:
                        lo = min(lo, mkt['low_24h'])
                e['obs_hi'], e['obs_lo'] = hi, lo
                if is_long:
                    hit_tp, hit_sl = hi >= tp, lo <= sl
                else:
                    hit_tp, hit_sl = lo <= tp, hi >= sl
                if hit_tp and hit_sl:
                    status = 'lost'  # order unknowable — conservative
                elif hit_tp:
                    status = 'won'
                elif hit_sl:
                    status = 'lost'
            if status == 'won':
                result = e['expected_return_pct']
            elif status == 'lost':
                result = -e['max_loss_pct']
        if status is None:
            exit_by = _iso_parse(e.get('exit_by') or '')
            age_days = (now - opened).total_seconds() / 86400 if opened else 99
            drift_from = None
            if exit_by and now > exit_by + timedelta(hours=2):
                drift_from = cur or e.get('last_price')
            elif age_days > 7:
                drift_from = e.get('last_price') or entry
            if drift_from:
                status = 'expired'
                result = ((drift_from - entry) / entry * 100) if is_long \
                    else ((entry - drift_from) / entry * 100)
        if status:
            e['status'] = status
            e['result_pct'] = round(result, 2)
            e['paper_pnl'] = round(result * PAPER_USD / 100, 2)
            e['ts_closed'] = now.isoformat()

def proof_stats(record):
    def agg(entries):
        closed = [e for e in entries if e.get('status') in ('won', 'lost', 'expired')]
        wins = sum(1 for e in closed if e['status'] == 'won')
        losses = sum(1 for e in closed if e['status'] == 'lost')
        expired = sum(1 for e in closed if e['status'] == 'expired')
        return {
            'graded': len(closed),
            'wins': wins,
            'losses': losses,
            'expired': expired,
            'open': sum(1 for e in entries if e.get('status') == 'open'),
            'win_rate': round(wins / (wins + losses) * 100, 1) if (wins + losses) else None,
            'paper_pnl_total': round(sum(e.get('paper_pnl') or 0 for e in closed), 2),
        }
    stats = agg(record)
    stats['by_kind'] = {k: agg([e for e in record if e.get('kind') == k])
                        for k in ('signal', 'mover')}
    return stats

def _proof_trim(record, cap=100):
    """Cap the published ledger at the newest 100 entries, dropping the
    oldest CLOSED entries first so open calls are never silently lost."""
    if len(record) <= cap:
        return record
    drop = set()
    for i in range(len(record) - 1, -1, -1):  # newest-first: walk oldest back
        if len(record) - len(drop) <= cap:
            break
        if record[i].get('status') != 'open':
            drop.add(i)
    out = [e for i, e in enumerate(record) if i not in drop]
    return out[:cap]

def main():
    print('INTRADAY: hourly signals...')
    # Cross-run memory for the accumulation detector: the engine itself is
    # stateless, but data/intraday.json is committed every run, so the
    # observation history rides inside it (see accum_history below).
    prior_history = {}
    try:
        with open(os.path.join(DATA_DIR, 'intraday.json')) as f:
            prior_history = (json.load(f) or {}).get('accum_history') or {}
    except Exception:
        pass
    # Same persistence trick for the 📒 Proof Book ledger (see proof block).
    prior_proof = []
    try:
        with open(os.path.join(DATA_DIR, 'intraday.json')) as f:
            prior_proof = ((json.load(f) or {}).get('proof') or {}).get('record') or []
    except Exception:
        pass
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
    # Core market data for this run is in hand — stamp the pull time so the
    # frontend can show "Data pulled … ET" for cross-referencing.
    data_pulled_at = datetime.now(timezone.utc).isoformat()
    mkts = universe[:50]
    # Market lookup for Proof Book grading — universe is market-cap
    # ordered, so the first occurrence of a symbol is its biggest coin.
    mkt_by_sym = {}
    for c in universe:
        s = (c['symbol'] or '').upper()
        if s and s not in mkt_by_sym:
            mkt_by_sym[s] = {'price': c.get('current_price'),
                             'high_24h': c.get('high_24h'),
                             'low_24h': c.get('low_24h')}

    signals = []
    charts_by_sym = {}  # retained for Proof Book grading (hourly points)
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
        charts_by_sym[sym] = chart['prices']
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
            'id': c['id'],
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
    vol_q90 = ratios[int(len(ratios) * 0.90)] if ratios else float('inf')
    vol_med = ratios[len(ratios) // 2] if ratios else float('inf')
    # Accumulation history upkeep: append this run's observation for every
    # scanned coin, keep the last 6, prune coins not in the latest scan.
    now_iso = datetime.now(timezone.utc).isoformat()
    new_history = {}
    for c in scanned:
        sym = (c['symbol'] or '').upper()
        obs = {'t': now_iso,
               'price': _sig(c.get('current_price') or 0),
               'vol24h': _sig(c.get('total_volume') or 0),
               'vol_ratio': _sig((c.get('total_volume') or 0) / c['market_cap'])}
        new_history[sym] = ((prior_history.get(sym) or []) + [obs])[-6:]
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
        vol_ratio = (c.get('total_volume') or 0) / c['market_cap'] if c.get('market_cap') else 0
        listed = False
        if abs(chg1) >= 3:
            surge = vol_ratio >= vol_q3
            inflection = (chg1 >= 3 and chg24 <= 0) or (chg1 <= -3 and chg24 >= 0)
            if surge or inflection:
                w = mover_fields(c, 'UP' if chg1 > 0 else 'DOWN')
                w['watch_reason'] = ('volume surge' if surge else '') + \
                    (' + ' if surge and inflection else '') + \
                    ('fresh inflection' if inflection else '')
                watch.append(w)
                listed = True
        # Strong day (additive): +8%+ over 24h in steps, still moving the
        # same way this hour, at least median volume — CVX case 2026-10-20:
        # +9.24% 24h with no +3% hour tripped neither movers nor watch.
        if not listed and strong_day(chg24, chg1, vol_ratio, vol_med):
            w = mover_fields(c, 'UP' if chg24 > 0 else 'DOWN')
            w['watch_reason'] = (f"Strong day building — {chg24:+.0f}% in 24h and still "
                                 f"{'rising' if chg24 > 0 else 'falling'} this hour")
            watch.append(w)
    movers.sort(key=lambda m: -abs(m['chg_24h']))
    watch.sort(key=lambda m: -abs(m['chg_1h']))
    movers, watch = movers[:10], watch[:10]
    print(f'  Movers scan: {len(movers)} movers, {len(watch)} watching (of {len(scanned)} scanned)')

    # --- Trade plans (Nic 2026-10-20: "Where is buy target, sell target and
    # stop loss target?") ---
    # UP movers get a LONG plan from build_mover_plan. DOWN movers get no buy
    # plan (Nic trades long-only) plus a note. Watch items: only the top 3 by
    # |chg_1h| (the list is already sorted that way) get plans the same way;
    # the rest carry plan: null with no note — the plan appears when they
    # graduate to movers.
    def attach_plan(m, with_note=True):
        if m['direction'] != 'UP':
            m['plan'] = None
            if with_note:
                m['plan_note'] = 'Falling mover — no buy plan (long-only). Watch for reversal.'
            return
        m['plan'] = build_mover_plan(m.get('id'), m.get('price') or 0)
        if m['plan'] is None and with_note:
            m['plan_note'] = 'Plan unavailable — price feed hiccup.'
    for m in movers:
        attach_plan(m)
    for w in watch[:3]:
        attach_plan(w)
    for w in watch[3:]:
        w['plan'] = None

    # --- Pre-move accumulation (🔭 Setting Up) ---
    # Fires BEFORE the jump: volume building while price is still quiet.
    # Coins already flagged as movers are excluded (they already moved).
    # History warms up over the first ~2-3 hourly runs after deploy —
    # until a coin has >= 3 observations spanning >= 2h it cannot flag.
    mover_syms = {m['asset'] for m in movers}
    now_dt = datetime.now(timezone.utc)
    accum = []
    for c in scanned:
        sym = (c['symbol'] or '').upper()
        if sym in mover_syms:
            continue
        chg1 = c.get('price_change_percentage_1h_in_currency') or 0
        vol_ratio = (c.get('total_volume') or 0) / c['market_cap'] if c.get('market_cap') else 0
        res = accum_reason(new_history.get(sym) or [], vol_ratio, vol_q90, chg1, now_dt)
        if not res:
            continue
        reason, growth, drift = res
        a = mover_fields(c, 'UP' if chg1 >= 0 else 'DOWN')
        a.pop('direction', None)  # accumulation has no direction call yet
        a.update({
            'chg_1h': round(chg1, 2),
            'vol_growth_pct': growth,
            'price_drift_pct': drift,
            'vol_ratio': _sig(vol_ratio),
            'watch_reason': reason,
        })
        accum.append(a)
    accum.sort(key=lambda a: (not a['tradable'], -a['vol_growth_pct']))
    accum = accum[:8]
    print(f'  Accumulation scan: {len(accum)} setting up')

    # --- 📒 Proof Book: grade yesterday's calls before recording today's
    # (a call is never graded in the same run that creates it, so the
    # trailing-24h windows can't credit a pre-entry move) ---
    proof = list(prior_proof)
    proof_now = datetime.now(timezone.utc)
    # Grading wants an hourly path for every open call, not just the
    # top-50 signal universe: fetch charts for open entries' assets that
    # the signal scan didn't cover (one fetch per symbol, bounded by the
    # number of open calls).
    id_by_sym = {}
    for c in universe:
        s = (c['symbol'] or '').upper()
        if s and s not in id_by_sym:
            id_by_sym[s] = c['id']
    for e in proof:
        if e.get('status') != 'open':
            continue
        sym = e['asset']
        if sym in charts_by_sym or sym not in id_by_sym:
            continue
        ch = fetch_json(f'https://api.coingecko.com/api/v3/coins/{id_by_sym[sym]}/market_chart'
                        '?vs_currency=usd&days=1&interval=hourly')
        if ch and ch.get('prices'):
            charts_by_sym[sym] = ch['prices']
    proof_grade(proof, mkt_by_sym, charts_by_sym, proof_now)
    proof_cands = []
    for s in longs + shorts:
        proof_cands.append(_proof_candidate(
            'signal', s['asset'], s.get('name'), s.get('direction'),
            s.get('thesis'), proof_now, 2))
    for m in movers:
        proof_cands.append(_proof_candidate(
            'mover', m['asset'], m.get('name'), 'LONG',
            m.get('plan'), proof_now, 3))
    n_added = proof_record_new(proof, proof_cands, proof_now)
    proof = _proof_trim(proof)
    pstats = proof_stats(proof)
    print(f"  Proof book: +{n_added} new calls, {pstats['open']} open, "
          f"{pstats['graded']} graded, paper P&L ${pstats['paper_pnl_total']:+.2f}")

    pub_now = datetime.now(timezone.utc).isoformat()
    out = {
        'generated': pub_now,
        'data_pulled_at': data_pulled_at,
        'published_at': pub_now,
        'mode': 'intraday',
        'horizon': '1-4 hours',
        'longs': longs,
        'shorts': shorts,
        'movers': movers,
        'watch': watch,
        'accum': accum,
        'accum_history': new_history,
        'proof': {'record': proof, 'stats': pstats},
        'coins_scanned': len(mkts),
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, 'intraday.json'), 'w') as f:
        json.dump(out, f, indent=2)
    print(f'  Intraday complete: {len(longs)} longs, {len(shorts)} shorts from {len(mkts)} coins')

if __name__ == '__main__':
    main()
