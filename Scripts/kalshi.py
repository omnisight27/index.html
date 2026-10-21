"""Kalshi probability feed + paper tracking for NexIntel.

Kalshi is a CFTC-regulated exchange where every contract is a yes/no
question priced 1-99 cents; the price is the crowd's probability. This
module reads Kalshi's PUBLIC market-data API (no key, no account, no
trading — read-only by construction) and does three things:

1. WATCH — snapshots the prices of a curated set of series in the lanes
   NexIntel already models: Fed decisions, inflation prints, and crypto
   price thresholds (see WATCH_SERIES).
2. MODEL — for crypto threshold markets only, computes NexIntel's own
   probability from spot price + realized volatility (lognormal). Fed
   and CPI markets get no model number until an honest estimator exists;
   the crowd baseline is what any future estimate must beat.
3. PAPER BOOK — when the model disagrees with the Kalshi price by
   >= DIVERGENCE on a crypto market, it books a paper position ($300
   notional, like the alert test size) on the underpriced side and
   grades it at settlement, fees estimated at Kalshi's general taker
   rate. Separately, every watched market is graded at settlement as a
   CROWD baseline (Brier score + did the crowd's favored side win),
   using the price ~24h before close when a snapshot exists that old,
   else the first-seen price.

Ledger: data/kalshi.json (committed by the engine loop like all data/).
Nothing here places orders or touches an account.
"""

import json
import math
import os
import urllib.request
from datetime import datetime, timezone

API = 'https://api.elections.kalshi.com/trade-api/v2'
COINGECKO = 'https://api.coingecko.com/api/v3'

# (series_ticker, lane, max markets per run)
WATCH_SERIES = [
    ('KXFEDDECISION', 'fed', 12),
    ('KXRATECUTCOUNT', 'fed', 8),
    ('KXFED', 'fed', 8),
    ('KXCPI', 'inflation', 10),
    ('KXCPIYOY', 'inflation', 8),
    ('KXCPICOREYOY', 'inflation', 8),
    ('KXBTCD', 'crypto', 8),
    ('KXETHD', 'crypto', 8),
    ('KXBTCMAXY', 'crypto', 8),
    ('KXBTCY', 'crypto', 8),
]
CRYPTO_SERIES = {'KXBTCD', 'KXETHD', 'KXBTCMAXY', 'KXBTCY'}
TOUCH_SERIES = {'KXBTCMAXY'}          # "how high will it get" = one-touch
COIN_IDS = {'KXBTCD': 'bitcoin', 'KXBTCMAXY': 'bitcoin', 'KXBTCY': 'bitcoin',
            'KXETHD': 'ethereum'}

PAPER_SIZE_USD = 300.0
DIVERGENCE = 0.10                      # model must disagree by >= 10 points
FEE_RATE = 0.07                        # Kalshi general taker fee coefficient
MIN_ENTRY = 0.03                       # skip degenerate prices (fees dominate)
MAX_ENTRY = 0.97
SNAPSHOT_CAP = 120
GRADED_CAP = 400


# ---------------------------------------------------------------- helpers
def _get(url, timeout=25):
    req = urllib.request.Request(url, headers={'Accept': 'application/json',
                                               'User-Agent': 'NexIntel/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _now():
    return datetime.now(timezone.utc)


def _parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except (TypeError, ValueError, AttributeError):
        return None


def mid_price(bid, ask, last=None):
    """Best single probability estimate from a Kalshi quote, in 0..1."""
    if bid is not None and ask is not None and ask >= bid:
        return round((bid + ask) / 2, 4)
    if last is not None:
        return last
    if ask is not None:
        return ask
    return bid


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_above(spot, strike, sigma, t_years):
    """P(S_T > strike) for lognormal S with zero risk-free rate."""
    if spot <= 0 or strike <= 0 or sigma <= 0 or t_years <= 0:
        return None
    d2 = (math.log(spot / strike) - 0.5 * sigma * sigma * t_years) / \
        (sigma * math.sqrt(t_years))
    return norm_cdf(d2)


def prob_touch_above(spot, strike, sigma, t_years):
    """P(max S_t >= strike) via the reflection principle (log-drift -s^2/2)."""
    if spot <= 0 or strike <= 0 or sigma <= 0 or t_years <= 0:
        return None
    if spot >= strike:
        return 1.0
    b = math.log(strike / spot)
    sq = sigma * math.sqrt(t_years)
    p = (1.0 - norm_cdf((b + 0.5 * sigma * sigma * t_years) / sq)) + \
        (spot / strike) * norm_cdf((-b + 0.5 * sigma * sigma * t_years) / sq)
    return min(0.999, max(0.001, p))


def model_prob(market, spot, sigma, now):
    """NexIntel probability (0..1) for a Kalshi crypto market, or None."""
    close = _parse_ts(market.get('expected_expiration_time') or
                      market.get('close_time'))
    if close is None or spot is None or sigma is None:
        return None
    t = (close - now).total_seconds() / (365.25 * 24 * 3600)
    if t <= 0:
        return None
    floor_s = _f(market.get('floor_strike'))
    cap_s = _f(market.get('cap_strike'))
    st = market.get('strike_type')
    series = market.get('_series')
    if series in TOUCH_SERIES and floor_s:
        return prob_touch_above(spot, floor_s, sigma, t)
    if st == 'greater' and floor_s:
        return prob_above(spot, floor_s, sigma, t)
    if st == 'less' and (cap_s or floor_s):
        k = cap_s or floor_s
        p = prob_above(spot, k, sigma, t)
        return None if p is None else 1.0 - p
    if st == 'between' and floor_s and cap_s:
        hi = prob_above(spot, floor_s, sigma, t)
        lo = prob_above(spot, cap_s, sigma, t)
        if hi is None or lo is None:
            return None
        return max(0.0, hi - lo)
    # Fallback for range/threshold markets without a clean strike type:
    # a lone floor strike reads as "above the floor".
    if floor_s and not cap_s:
        return prob_above(spot, floor_s, sigma, t)
    return None


def fee_estimate(contracts, price):
    """Kalshi general taker fee: ceil(rate * C * P * (1-P)) dollars."""
    if contracts <= 0 or price <= 0 or price >= 1:
        return 0.0
    raw = FEE_RATE * contracts * price * (1 - price) * 100
    return math.ceil(raw - 1e-9) / 100.0


def grade_paper(record, outcome_yes):
    """Grade one paper position. outcome_yes: True/False. Pure function."""
    win = (record['side'] == 'yes') == bool(outcome_yes)
    payout = record['contracts'] if win else 0.0
    pnl = payout - PAPER_SIZE_USD - record.get('fee_est', 0.0)
    return {'win': win, 'pnl': round(pnl, 2)}


def brier(p, outcome):
    return round((p - outcome) ** 2, 4)


def realized_sigma(coin_id, days=90):
    """Annualized vol from CoinGecko daily closes (keyless)."""
    d = _get(f'{COINGECKO}/coins/{coin_id}/market_chart'
             f'?vs_currency=usd&days={days}&interval=daily')
    closes = [p[1] for p in d.get('prices', []) if p[1] > 0]
    if len(closes) < 20:
        return None, None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    return math.sqrt(var) * math.sqrt(365.25), closes[-1]


# ---------------------------------------------------------------- collect
def _empty_ledger():
    return {'watch': {}, 'paper_open': [], 'graded': [], 'misses': {}}


def _load_ledger(path):
    try:
        with open(path) as f:
            led = json.load(f)
        for k, v in _empty_ledger().items():
            led.setdefault(k, v)
        return led
    except Exception:
        return _empty_ledger()


def _crowd_baseline(rec, close_time):
    """(mid, label): price ~24h before close if we have one, else first-seen."""
    snaps = rec.get('snapshots') or []
    if close_time:
        from datetime import timedelta
        target = close_time - timedelta(hours=24)
        old = [s for s in snaps if (_parse_ts(s['ts']) or _now()) <= target]
        if old:
            return old[-1]['mid'], 'T-24h'
    return rec.get('first_mid'), 'first-seen'


def _compute_stats(graded):
    crowd = [g for g in graded if g.get('kind') == 'crowd']
    model = [g for g in graded if g.get('kind') == 'model']
    st = {}
    if crowd:
        st['crowd_n'] = len(crowd)
        st['crowd_brier'] = round(sum(g['brier'] for g in crowd) / len(crowd), 4)
        fav = [g for g in crowd if g.get('crowd_mid') is not None]
        if fav:
            right = sum(1 for g in fav
                        if (g['crowd_mid'] >= 0.5) == bool(g['outcome']))
            st['crowd_fav_accuracy'] = round(right / len(fav) * 100, 1)
    if model:
        w = sum(1 for g in model if g['win'])
        st['model_n'] = len(model)
        st['model_w'] = w
        st['model_l'] = len(model) - w
        st['model_pnl'] = round(sum(g['pnl'] for g in model), 2)
    return st


def collect(data_dir):
    """Run one Kalshi pass. Returns a JSON-safe summary for latest.json."""
    now = _now()
    path = os.path.join(data_dir, 'kalshi.json')
    led = _load_ledger(path)
    watch, paper_open, graded = led['watch'], led['paper_open'], led['graded']

    # Volatility + spot per coin (only if a crypto series yields markets).
    vol = {}
    for coin in ('bitcoin', 'ethereum'):
        try:
            sigma, spot = realized_sigma(coin)
            if sigma and spot:
                vol[coin] = {'sigma': sigma, 'spot': spot}
        except Exception as e:
            print(f'  kalshi: vol fetch failed for {coin}: {e}')

    seen_open = set()
    for series, lane, cap in WATCH_SERIES:
        try:
            d = _get(f'{API}/markets?series_ticker={series}&status=open'
                     f'&limit=100')
        except Exception as e:
            print(f'  kalshi: {series} fetch failed: {e}')
            continue
        markets = [m for m in d.get('markets', []) if m.get('status') == 'active']
        markets.sort(key=lambda m: m.get('close_time') or '')
        if series in CRYPTO_SERIES:
            # The strike ladder runs deep into the tails (0% / 100% buckets).
            # Keep the markets priced nearest 50% — the informative ones —
            # from the nearest events, not one end of the ladder.
            events = []
            for m in markets:
                if m.get('event_ticker') not in events:
                    events.append(m.get('event_ticker'))
            keep_events = set(events[:2])
            markets = [m for m in markets
                       if m.get('event_ticker') in keep_events]

            def _dist(m):
                md = mid_price(_f(m.get('yes_bid_dollars')),
                               _f(m.get('yes_ask_dollars')),
                               _f(m.get('last_price_dollars')))
                return abs((md if md is not None else 0) - 0.5)
            markets.sort(key=_dist)
        for m in markets[:cap]:
            m['_series'] = series
            ticker = m['ticker']
            seen_open.add(ticker)
            mid = mid_price(_f(m.get('yes_bid_dollars')),
                            _f(m.get('yes_ask_dollars')),
                            _f(m.get('last_price_dollars')))
            if mid is None:
                continue
            mp = None
            if series in CRYPTO_SERIES:
                coin = COIN_IDS.get(series)
                v = vol.get(coin) if coin else None
                if v:
                    mp = model_prob(m, v['spot'], v['sigma'], now)
                    if mp is not None:
                        mp = round(mp, 4)
            rec = watch.get(ticker)
            if rec is None:
                rec = {'ticker': ticker, 'series': series, 'lane': lane,
                       'title': m.get('title') or ticker,
                       'sub': m.get('yes_sub_title') or '',
                       'close_time': m.get('close_time'),
                       'first_seen': now.isoformat(), 'first_mid': mid,
                       'snapshots': []}
                watch[ticker] = rec
            rec.update({'mid': mid, 'model_p': mp,
                        'yes_bid': _f(m.get('yes_bid_dollars')),
                        'yes_ask': _f(m.get('yes_ask_dollars')),
                        'volume': _f(m.get('volume_fp')),
                        'close_time': m.get('close_time'),
                        'updated': now.isoformat()})
            snaps = rec['snapshots']
            if not snaps or snaps[-1]['mid'] != mid:
                snaps.append({'ts': now.isoformat(), 'mid': mid})
                del snaps[:-SNAPSHOT_CAP]

            # Paper position on a fresh >=10pt model/crowd disagreement.
            if mp is not None and abs(mp - mid) >= DIVERGENCE:
                already = any(p['ticker'] == ticker for p in paper_open) or \
                    any(g.get('ticker') == ticker and g.get('kind') == 'model'
                        for g in graded)
                close = _parse_ts(m.get('close_time'))
                hours = (close - now).total_seconds() / 3600 if close else 0
                if not already and hours >= 1:
                    side = 'yes' if mp > mid else 'no'
                    entry = mid if side == 'yes' else round(1 - mid, 4)
                    if MIN_ENTRY <= entry <= MAX_ENTRY:
                        contracts = round(PAPER_SIZE_USD / entry, 2)
                        paper_open.append({
                            'ticker': ticker, 'title': rec['title'],
                            'series': series, 'side': side,
                            'entry': entry, 'kalshi_mid': mid, 'model_p': mp,
                            'contracts': contracts,
                            'fee_est': fee_estimate(contracts, entry),
                            'opened': now.isoformat(),
                            'close_time': m.get('close_time')})

    # Settle anything that left the open list.
    open_tickers = {p['ticker'] for p in paper_open}
    for ticker in list(watch.keys()):
        if ticker in seen_open:
            led['misses'].pop(ticker, None)
            continue
        # Not open anymore: check for a final result (once per run per ticker,
        # with a miss counter so a delisted-without-result market can't loop).
        try:
            m = _get(f'{API}/markets/{ticker}').get('market', {})
        except Exception:
            m = {}
        result = (m.get('result') or '').lower()
        rec = watch[ticker]
        if result in ('yes', 'no'):
            outcome = 1 if result == 'yes' else 0
            cmid, clabel = _crowd_baseline(
                rec, _parse_ts(rec.get('close_time') or m.get('close_time')))
            if cmid is not None:
                graded.append({'kind': 'crowd', 'ticker': ticker,
                               'title': rec['title'], 'series': rec['series'],
                               'crowd_mid': cmid, 'crowd_src': clabel,
                               'outcome': outcome,
                               'brier': brier(cmid, outcome),
                               'settled': now.isoformat()})
            for p in list(paper_open):
                if p['ticker'] == ticker:
                    g = grade_paper(p, bool(outcome))
                    graded.append({'kind': 'model', 'ticker': ticker,
                                   'title': p['title'], 'series': p['series'],
                                   'side': p['side'], 'entry': p['entry'],
                                   'model_p': p['model_p'],
                                   'outcome': outcome, 'win': g['win'],
                                   'pnl': g['pnl'],
                                   'settled': now.isoformat()})
                    paper_open.remove(p)
            del watch[ticker]
            led['misses'].pop(ticker, None)
        else:
            led['misses'][ticker] = led['misses'].get(ticker, 0) + 1
            if led['misses'][ticker] >= 6:
                # Gone a full day+ with no result: void it, never graded.
                for p in list(paper_open):
                    if p['ticker'] == ticker:
                        paper_open.remove(p)
                del watch[ticker]
                led['misses'].pop(ticker, None)

    graded = graded[-GRADED_CAP:]
    led['watch'], led['paper_open'], led['graded'] = watch, paper_open, graded
    led['updated'] = now.isoformat()
    led['stats'] = _compute_stats(graded)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(led, f, indent=1)
    os.replace(tmp, path)

    rows = []
    for rec in sorted(watch.values(),
                      key=lambda r: r.get('close_time') or ''):
        rows.append({k: rec.get(k) for k in
                     ('ticker', 'series', 'lane', 'title', 'sub', 'close_time',
                      'mid', 'model_p', 'first_mid')})
    return {'updated': now.isoformat(), 'watch': rows[:24],
            'watch_total': len(rows),
            'paper_open': paper_open, 'paper_stats': led['stats'],
            'graded_recent': graded[-8:][::-1]}


if __name__ == '__main__':
    import sys
    dd = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'data')
    s = collect(dd)
    print(f"Kalshi: watching {s['watch_total']} markets, "
          f"{len(s['paper_open'])} paper positions open, "
          f"stats: {s['paper_stats']}")
    for r in s['watch'][:10]:
        mp = f" model {r['model_p']:.0%}" if r.get('model_p') is not None else ''
        dv = ''
        if r.get('model_p') is not None:
            dv = f" gap {r['model_p'] - r['mid']:+.0%}"
        print(f"  {r['ticker']:<28} kalshi {r['mid']:.0%}{mp}{dv}  "
              f"closes {r['close_time']}")
