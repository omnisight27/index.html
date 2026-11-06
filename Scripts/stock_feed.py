#!/usr/bin/env python3
"""Stocks research track, Phase 1 (display-only + shadow grading).

Yahoo Finance v8 chart API (keyless, verified live 2026-11-05), a fixed
30-ticker liquid US large-cap universe, US market-hours handling, and a
stock-specific shadow fee tier. Stocks NEVER join longs/shorts, never
touch a buy gate, never alert — the top 5 each way are graded in a
separate Proof Book ledger (kind='stock') and shown on a stock
scoreboard. Promotion to any live universe is Nic's call.

Design: hidden_files/stocks-research-track-spec-2026-11-05.md

Key facts baked in here (all verified, not assumed):
- Yahoo v8 chart works keyless from this environment (AAPL 1h/5d -> 36
  real candles; 6 symbols rapid-fire all HTTP 200, no 429). Stooq is
  bot-blocked (JS challenge). Alpha Vantage / Tiingo / IEX / FMP need
  keys — excluded. Free Yahoo data is delayed ~15 min: one more reason
  stocks stay shadow/paper on this feed.
- The crypto fee floor (1.4% round trip / 2.8% net) would kill ~all
  stock signals (large-cap hourly vol ~0.3-0.8% -> targets ~0.4-1.0%).
  Stocks are commission-free; round-trip cost ~= spread ~0.05-0.10%.
  The stock SHADOW tier uses entry/exit fee 0, slips 0.05% each, net
  floor 0.5% (~5x cost cover, conservative). This is implemented HERE
  as a standalone check mirroring gate_math.evaluate_net_gate's math —
  gate_math.py itself is NOT modified, the live crypto gates are
  untouched, and stock rejections NEVER feed the crypto circuit
  breaker (no _record_gate_rejection call anywhere in this module).
- score_intraday's scale is crypto-calibrated: a 0.5% hourly stock move
  (~1 sigma) scores ~1.5-2, so the crypto |score| >= 3 filter would
  admit almost nothing. Stocks use STOCK_SCORE_MIN = 1.5 (Phase 1
  default, flagged for Nic) — the shadow ledger measures whether it
  works; tuning it is Nic's call.
- Feature windows are bar-based, not wall-clock: the 24-bar window
  spans ~3-4 trading sessions for stocks. chg_24h is really "change
  over the last 24 bars" — same math, different clock.
"""
import json
import urllib.request
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- universe
# Phase 1: fixed 30 liquid US large-caps (S&P 500 mega/large, high dollar
# volume). Fixed list = deterministic and gradeable. Expansion is Nic's call.
STOCK_UNIVERSE = [
    'AAPL', 'MSFT', 'NVDA', 'AMZN', 'GOOGL', 'META', 'AVGO', 'TSLA',
    'JPM', 'LLY', 'V', 'MA', 'UNH', 'HD', 'PG', 'COST', 'NFLX', 'XOM',
    'WMT', 'BAC', 'CRM', 'AMD', 'ADBE', 'ABBV', 'KO', 'PEP', 'DIS',
    'NKE', 'ORCL', 'MRK',
]

STOCK_RECORD_CAP = 2000            # separate stock ledger cap
STOCK_SCORE_MIN = 1.5              # |score| filter (crypto uses 3.0 — see header)
STOCK_TOP_N = 5                    # top 5 each way join the book per run

# Stock shadow fee tier (SHADOW ONLY — see header). Round-trip cost
# ~= 0.10%; net floor 0.5% is ~5x cost cover.
STOCK_ENTRY_FEE = 0.0
STOCK_EXIT_FEE = 0.0
STOCK_ENTRY_SLIP = 0.0005
STOCK_EXIT_SLIP = 0.0005
STOCK_STOP_SLIP = 0.001
STOCK_NET_FLOOR = 0.005           # 0.5% net
STOCK_TARGET_FLOOR_PCT = 0.2      # target >= 2x round-trip cost (0.2%)

YAHOO_HOSTS = ('query1.finance.yahoo.com', 'query2.finance.yahoo.com')
ET = ZoneInfo('America/New_York')

# US market holidays (NYSE calendar) — the Yahoo data check is the
# backstop: if the latest candle is >2h old during supposed market
# hours, the scan skips anyway, so a stale list fails safe.
US_MARKET_HOLIDAYS = {
    # 2026
    '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25',
    '2026-06-19', '2026-07-03', '2026-09-07', '2026-11-26', '2026-12-25',
    # 2027
    '2027-01-01', '2027-01-18', '2027-02-15', '2027-03-26', '2027-05-31',
    '2027-06-18', '2027-07-05', '2027-09-06', '2027-11-25', '2027-12-24',
}


def us_market_open_now(now=None):
    """True when US equities are in regular session (9:30-16:00 ET,
    Mon-Fri, not a listed holiday). `now` may be an aware datetime
    (defaults to now, UTC). Pure function — no network."""
    if now is None:
        now = datetime.now(timezone.utc)
    et = now.astimezone(ET)
    if et.weekday() >= 5:  # Sat/Sun
        return False
    if et.strftime('%Y-%m-%d') in US_MARKET_HOLIDAYS:
        return False
    return time(9, 30) <= et.time() < time(16, 0)


def fetch_stock_candles(sym, range_='7d', interval='1h', timeout=30):
    """Yahoo v8 chart for one ticker. Returns {'closes': [[ts_ms, close]],
    'volumes': [[ts_ms, vol]], 'last': price} or None (fail-open).
    The forming (current, incomplete) hour is dropped — same rule as
    pattern_feed. Tries query1, falls back to query2."""
    for host in YAHOO_HOSTS:
        url = (f'https://{host}/v8/finance/chart/{sym}'
               f'?range={range_}&interval={interval}')
        try:
            req = urllib.request.Request(
                url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
        except Exception:
            continue
        try:
            res = (d.get('chart') or {}).get('result') or []
            if not res:
                continue
            res = res[0]
            ts = res.get('timestamp') or []
            q = (res.get('indicators') or {}).get('quote') or [{}]
            closes = q[0].get('close') or []
            vols = q[0].get('volume') or []
            pts, vpts = [], []
            for i, t in enumerate(ts):
                c = closes[i] if i < len(closes) else None
                if c is None or t is None:
                    continue
                pts.append([int(t) * 1000, float(c)])
                v = vols[i] if i < len(vols) else None
                vpts.append([int(t) * 1000, float(v) if v is not None else 0.0])
            if len(pts) < 5:
                continue
            # Drop the forming hour: its volume is 0 / partial and its
            # close is still moving. (Same rule as pattern_feed.)
            if vpts and vpts[-1][1] == 0:
                pts, vpts = pts[:-1], vpts[:-1]
            if len(pts) < 5:
                continue
            return {'closes': pts, 'volumes': vpts, 'last': pts[-1][1]}
        except Exception:
            continue
    return None


def clears_stock_fee_floor(vol):
    """Cheap pre-check mirroring clears_fee_floor(): the vol-based
    target (vol x 1.2) must clear 2x the ~0.1% round-trip cost."""
    return vol is not None and vol * 1.2 >= STOCK_TARGET_FLOOR_PCT


def stock_net_gate(entry, target, stop, direction):
    """Stock shadow net check, mirroring gate_math.evaluate_net_gate's
    math with the stock fee tier and the 0.5% net floor. Standalone on
    purpose: gate_math.py is NOT modified and its 2.8% floor stays for
    crypto. Returns (passed, net_return_pct, reason)."""
    try:
        e, t, s = float(entry), float(target), float(stop)
    except (TypeError, ValueError):
        return False, 0.0, 'INVALID_INPUT'
    if not all(x > 0 for x in (e, t, s)):
        return False, 0.0, 'INVALID_INPUT'
    if direction == 'LONG':
        if s >= e:
            return False, 0.0, 'INVALID_STOP'
        if t <= e:
            return False, 0.0, 'INVALID_TARGET'
        all_in = e * (1 + STOCK_ENTRY_SLIP) * (1 + STOCK_ENTRY_FEE)
        net_exit = t * (1 - STOCK_EXIT_SLIP) * (1 - STOCK_EXIT_FEE)
        net_return = net_exit / all_in - 1
        required = (all_in * (1 + STOCK_NET_FLOOR)
                    / ((1 - STOCK_EXIT_FEE) * (1 - STOCK_EXIT_SLIP)))
        if net_return < STOCK_NET_FLOOR:
            return False, round(net_return * 100, 2), 'NET_RETURN_BELOW_FLOOR'
        if t < required:
            return False, round(net_return * 100, 2), 'REQUIRED_SELL_NOT_MET'
    else:  # SHORT
        if s <= e:
            return False, 0.0, 'INVALID_STOP'
        if t >= e:
            return False, 0.0, 'INVALID_TARGET'
        proceeds = e * (1 - STOCK_ENTRY_SLIP) * (1 - STOCK_ENTRY_FEE)
        buyback = t * (1 + STOCK_EXIT_SLIP) * (1 + STOCK_EXIT_FEE)
        net_return = (proceeds - buyback) / proceeds
        required = (proceeds * (1 - STOCK_NET_FLOOR)
                    / ((1 + STOCK_EXIT_FEE) * (1 + STOCK_EXIT_SLIP)))
        if net_return < STOCK_NET_FLOOR:
            return False, round(net_return * 100, 2), 'NET_RETURN_BELOW_FLOOR'
        if buyback > required:
            return False, round(net_return * 100, 2), 'REQUIRED_SELL_NOT_MET'
    return True, round(net_return * 100, 2), None


def _round_price(p):
    """Adaptive precision mirroring intraday.round_price() (kept local
    so this module never imports intraday — no circular import)."""
    if not p:
        return 0
    if p >= 100:
        return round(p, 2)
    if p >= 1:
        return round(p, 4)
    if p >= 0.01:
        return round(p, 6)
    return round(p, 8)


def build_stock_thesis(sym, f, score):
    """Vol-based thesis for a stock signal, same construction as
    build_intraday_thesis (target = vol x 1.2, stop = vol x 0.8, 2h
    exit) but with the stock shadow fee tier. NEVER touches a gate
    or the circuit breaker — a rejection just returns None.
    Exit note: a 2h window crossing the 16:00 close grades at the
    close (the path simply ends there) — see the spec."""
    direction = 'LONG' if score > 0 else 'SHORT'
    vol = f['vol']
    price = f['price']
    if not clears_stock_fee_floor(vol):
        return None
    if direction == 'LONG':
        stop = price * (1 - vol / 100 * 0.8)
        target = price * (1 + vol / 100 * 1.2)
    else:
        stop = price * (1 + vol / 100 * 0.8)
        target = price * (1 - vol / 100 * 1.2)
    passed, net_pct, _reason = stock_net_gate(price, target, stop, direction)
    if not passed:
        return None
    entry_r, target_r, stop_r = _round_price(price), _round_price(target), \
        _round_price(stop)
    if len({entry_r, target_r, stop_r}) < 3:
        entry_r, target_r, stop_r = (round(price, 10), round(target, 10),
                                     round(stop, 10))
    now = datetime.now(timezone.utc)
    exit_time = now + timedelta(hours=2)
    exp_ret = abs(target - price) / price * 100
    max_loss = abs(price - stop) / price * 100
    bull, bear = [], []
    if f['chg_1h'] > 1:
        bull.append(f"+{f['chg_1h']:.1f}% last hour momentum")
    if f['chg_1h'] < -1:
        bear.append(f"{f['chg_1h']:.1f}% last hour drop")
    if f['rsi'] < 30:
        bull.append(f"RSI {f['rsi']:.0f} — oversold bounce setup")
    if f['rsi'] > 70:
        bear.append(f"RSI {f['rsi']:.0f} — overbought, fade setup")
    if f['pos_in_range'] > 0.9:
        bull.append("breaking 24-bar highs")
    if f['pos_in_range'] < 0.1:
        bear.append("breaking 24-bar lows")
    return {
        'direction': direction,
        'entry': entry_r,
        'stop_loss': stop_r,
        'take_profit': target_r,
        'entry_time': now.strftime('%H:%M UTC'),
        'exit_by': exit_time.strftime('%H:%M UTC'),
        'horizon': '1-4 hours',
        'expected_return_pct': round(exp_ret, 2),
        'max_loss_pct': round(max_loss, 2),
        'order_type': 'LIMIT',
        'order_note': 'Stock shadow — paper only, never a live order',
        'net_return_pct': net_pct,
        'rsi': round(f['rsi'], 0),
        'chg_1h': round(f['chg_1h'], 2),
        'bull_case': bull or ['intraday momentum alignment'],
        'bear_case': bear or ['intraday momentum divergence'],
    }


def scan_stock_with(sym, closes, hourly_features, score_intraday,
                    balance_score, volumes=None, closes_7d=None):
    """One ticker of the stock scan: features -> score -> thesis.
    `closes` is the [[ts_ms, close]] list (forming hour already
    dropped); the last 24 bars feed the features. Returns the signal
    dict or None (too few bars, weak score, or fee floor)."""
    if not closes or len(closes) < 5:
        return None
    prices = [[t, c] for t, c in closes[-24:]]
    f = hourly_features(prices)
    if not f:
        return None
    score = score_intraday(f)
    if abs(score) < STOCK_SCORE_MIN:
        return None
    thesis = build_stock_thesis(sym, f, score)
    if thesis is None:
        return None
    conf = min(95, max(40, 50 + abs(score) * 3))
    bs, _ = (None, {})
    try:
        _closes = [c for _, c in closes]
        _c7 = [c for _, c in (closes_7d or closes)]
        bs, _ = balance_score(_closes, volumes=volumes, closes_7d=_c7)
    except Exception:
        bs = None
    return {
        'asset': sym, 'score': round(score, 2),
        'confidence': round(conf),
        'direction': thesis['direction'], 'thesis': thesis,
        'name': sym,
        'info_url': f'https://finance.yahoo.com/quote/{sym}/',
        'tradable': True,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'balance_score': bs,
    }
