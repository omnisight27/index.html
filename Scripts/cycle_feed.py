"""Cycle research track — Phase 1 computation (spec DRAFT 2026-11-05,
~/workspace/goals/build-out-omnisight/hidden_files/
cycle-research-track-spec-2026-11-05.md).

DISPLAY-ONLY + SHADOW-GRADED. This module never touches a buy gate: it
does not import, call, or consult gate_math.py, gate_circuit.py, or the
60% live bar. Cycle states are annotations, never inputs. No alert-tier
output, no confidence influence, no trades.

What it does:
  * Strict cycle-state computation per spec §1 (7 variables — the spec
    counts six sections §1a–§1f, the seventh row is the asset-day
    itself; the six state variables are halving position, month,
    dominance trend, Fear & Greed, day-of-week, drawdown depth):
    one rule function per variable, each independently unit-testable
    with synthetic fixtures (halving boundaries, month edges,
    dominance thresholds, FNG band edges, drawdown bands).
  * Keyless data (§2): BTC daily closes via Kraken public OHLC
    (interval=1440); dominance via CoinGecko /global (cached daily);
    Fear & Greed via Alternative.me fng/ (cached daily). Every fetch
    is fail-open — a missing feed degrades that variable's cell to
    "no data", never an exception, never a blocked run.
  * The daily state rides in intraday.json['cycle_state'] and is
    displayed on the Picks page as a "Cycle pulse" (display-only).

Conventions (conservative display-only defaults, flagged for Nic):
  * The state date is today (UTC). Drawdown uses the last CLOSED daily
    close (today's forming daily candle is dropped, like the pattern
    track drops the forming hour) — so the state is "as of the last
    close", never a partial day.
  * Dominance change is measured against the cached value from 30 days
    ago (nearest cached day within +/-3 days); no cached value that
    old yet -> DOM band is "no data" until the cache warms up.
  * FNG / dominance history is NOT backfilled: CoinGecko /global has
    no keyless history endpoint and Alternative.me history starts
    2018-02-01, so backfilled states carry FNG from 2018 on and
    dominance "no data" throughout. The live run fills both daily
    from today forward.
  * Backfill source deviation (flagged): the spec's Kraken daily OHLC
    returns at most the last 720 candles and CoinGecko's free
    market_chart is capped at the past 365 days, so neither can reach
    2016. The one-off backfill job (this file's __main__) reads BTC
    daily closes from Coinbase public candles instead — keyless, and
    already a provider the engine calls — paged in 250-day windows.
    The LIVE run still uses Kraken per spec §2; Coinbase is
    backfill-only, never a live dependency.
  * Halving bands are BTC-anchored and graded cross-asset (spec §1a):
    BTC itself contributes only 3-4 observations per band, so halving
    cells are descriptive cross-asset evidence, never "the halving
    cycle works". That disclosure rides in the scoreboard.
  * Every rule function is pure and never raises: bad data returns
    None ("no data"), never an exception.
"""

import json
import os
import urllib.request
from datetime import date, datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Spec §1 constants (frozen at spec time — no tuning, §5.5)
# ---------------------------------------------------------------------------

HALVING_DATES = [
    date(2012, 11, 28),
    date(2016, 7, 9),
    date(2020, 5, 11),
    date(2024, 4, 19),
]

# Halving bands: (upper day bound inclusive, band). H5 is open-ended.
HALVING_BANDS = [(90, 'H1'), (180, 'H2'), (365, 'H3'), (730, 'H4')]
HALVING_OPEN_BAND = 'H5'

DOM_THRESHOLD_PTS = 1.0   # >= 1.0pt move over 30 days (spec §1c)
DOM_LOOKBACK_DAYS = 30
DOM_CACHE_DAYS = 45       # daily dominance values kept (30d lookback + slack)
DOM_STALE_DAYS = 3        # 30d-ago value must be within +/-3 days

FNG_BANDS = [(25, 'FNG_XF'), (45, 'FNG_F'), (56, 'FNG_N'),
             (75, 'FNG_G')]
FNG_OPEN_BAND = 'FNG_XG'  # 75+
FNG_NEUTRAL = 'FNG_N'     # control band (spec §1d)

DD_WINDOW_DAYS = 365
# Drawdown bands: (upper % below the 365d high, inclusive, band).
DD_BANDS = [(10.0, 'DD_SHALLOW'), (25.0, 'DD_MID'), (40.0, 'DD_DEEP')]
DD_OPEN_BAND = 'DD_EXTREME'  # > 40% below the high

KRAKEN_DAILY_URL = ('https://api.kraken.com/0/public/OHLC'
                    '?pair=XBTUSD&interval=1440')
COINGECKO_GLOBAL_URL = 'https://api.coingecko.com/api/v3/global'
FNG_URL = 'https://api.alternative.me/fng/?limit=1'
FNG_HISTORY_URL = 'https://api.alternative.me/fng/?limit=0'
COINBASE_CANDLES_URL = ('https://api.exchange.coinbase.com/products/'
                        'BTC-USD/candles?granularity=86400&start={start}'
                        '&end={end}')

MAX_DAILY_STORED = 720    # Kraken's own per-call cap; store the same
BACKFILL_START = date(2016, 1, 1)
BACKFILL_PAGE_DAYS = 250  # Coinbase caps at 300 candles per call

VARIABLES = ['halving', 'month', 'dom', 'fng', 'dow', 'dd']
# Spec §4 control bands (for the scoreboard's second baseline). Only
# FNG and dominance have one; the rest grade against the all-days
# base rate alone.
CONTROL_BANDS = {'fng': FNG_NEUTRAL, 'dom': 'DOM_FLAT'}

BAND_FIELD = {'halving': 'halving_band', 'month': 'month',
              'dom': 'dom_band', 'fng': 'fng_band', 'dow': 'dow',
              'dd': 'dd_band'}


# ---------------------------------------------------------------------------
# Date / close parsing (pure, never raise)
# ---------------------------------------------------------------------------

def _parse_day(day):
    """date/datetime/'YYYY-MM-DD'/unix ts -> date or None."""
    try:
        if day is None:
            return None
        if isinstance(day, datetime):
            return day.date()
        if isinstance(day, date):
            return day
        if isinstance(day, (int, float)):
            return datetime.fromtimestamp(day, tz=timezone.utc).date()
        if isinstance(day, str):
            return date.fromisoformat(day.strip()[:10])
        return None
    except Exception:
        return None


def _parse_closes(btc_closes):
    """Normalize daily closes to [(date, close)] sorted by date, bad
    rows dropped. Accepts: [[ts, close], ...] (ts in seconds or ms),
    [['YYYY-MM-DD', close], ...], OHLC rows [[ts, o, h, l, close, ...]]
    (close at index 4), Coinbase candle rows [[ts, low, high, open,
    close, volume]] (close at index 4), or {date_str: close}."""
    out = []
    try:
        items = btc_closes.items() if isinstance(btc_closes, dict) \
            else (btc_closes or [])
        for row in items:
            try:
                if isinstance(row, tuple) and len(row) == 2 \
                        and isinstance(row[0], str):
                    d, c = _parse_day(row[0]), float(row[1])
                elif isinstance(row, (list, tuple)):
                    if len(row) >= 5:
                        d, c = _parse_day(row[0]), float(row[4])
                    elif len(row) == 2:
                        d, c = _parse_day(row[0]), float(row[1])
                    else:
                        continue
                else:
                    continue
                if d is None or c <= 0:
                    continue
                out.append((d, c))
            except (TypeError, ValueError, IndexError):
                continue
        out.sort(key=lambda x: x[0])
        # Dedup by date, keeping the last row for the day.
        dedup = {}
        for d, c in out:
            dedup[d] = c
        return sorted(dedup.items())
    except Exception:
        return []


def closes_from_ohlc_rows(rows):
    """[[ts, close], ...] from Kraken/Coinbase OHLC-style rows."""
    return _ohlc(rows)


def _ohlc(rows):
    out = []
    try:
        for r in (rows or []):
            try:
                if not isinstance(r, (list, tuple)) or len(r) < 5:
                    continue
                out.append([int(r[0]), float(r[4])])
            except (TypeError, ValueError, IndexError):
                continue
    except Exception:
        return []
    return out


# ---------------------------------------------------------------------------
# §1 rule functions — one per variable, pure, never raise
# ---------------------------------------------------------------------------

def halving_band(day):
    """(band, days_since_halving) for `day`, or (None, None) before
    the first halving (2012-11-28). Bands: H1 0-90, H2 91-180,
    H3 181-365, H4 366-730, H5 731+ days after a halving."""
    try:
        d = _parse_day(day)
        if d is None:
            return None, None
        last = None
        for h in HALVING_DATES:
            if h <= d:
                last = h
            else:
                break
        if last is None:
            return None, None
        days = (d - last).days
        for upper, band in HALVING_BANDS:
            if days <= upper:
                return band, days
        return HALVING_OPEN_BAND, days
    except Exception:
        return None, None


def month_of(day):
    """Calendar month name ('January'..'December') or None."""
    try:
        d = _parse_day(day)
        return d.strftime('%B') if d else None
    except Exception:
        return None


def dow_of(day):
    """Day-of-week name ('Monday'..'Sunday') or None."""
    try:
        d = _parse_day(day)
        return d.strftime('%A') if d else None
    except Exception:
        return None


def dom_band(dom_now, dom_30d_ago):
    """DOM_UP / DOM_FLAT / DOM_DOWN on the 30-day dominance change in
    percentage points (>= +1.0pt up, <= -1.0pt down, else flat).
    None when either reading is missing — the cell is "no data"."""
    try:
        if dom_now is None or dom_30d_ago is None:
            return None
        chg = float(dom_now) - float(dom_30d_ago)
        if chg >= DOM_THRESHOLD_PTS:
            return 'DOM_UP'
        if chg <= -DOM_THRESHOLD_PTS:
            return 'DOM_DOWN'
        return 'DOM_FLAT'
    except (TypeError, ValueError):
        return None


def dom_change_pts(dom_now, dom_30d_ago):
    """The 30-day dominance change in points (rounded), or None."""
    try:
        if dom_now is None or dom_30d_ago is None:
            return None
        return round(float(dom_now) - float(dom_30d_ago), 2)
    except (TypeError, ValueError):
        return None


def fng_band(value):
    """Fear & Greed bands (spec §1d): <25 extreme fear, 25-44 fear,
    45-55 neutral (control), 56-74 greed, 75+ extreme greed.
    None when missing or outside 0-100 (bad data -> no data)."""
    try:
        if value is None:
            return None
        v = float(value)
        if v < 0 or v > 100:
            return None
        for upper, band in FNG_BANDS:
            if v < upper:
                return band
        return FNG_OPEN_BAND
    except (TypeError, ValueError):
        return None


def dd_band(btc_closes, day):
    """(band, drawdown_pct) for BTC vs its trailing 365-day high,
    from daily closes at or before `day`. Bands: within 10% shallow,
    10-25% mid, 25-40% deep, >40% extreme. (None, None) when no
    closes exist in the window — the cell is "no data"."""
    try:
        d = _parse_day(day)
        if d is None:
            return None, None
        closes = _parse_closes(btc_closes)
        window = [(cd, c) for cd, c in closes
                  if d - timedelta(days=DD_WINDOW_DAYS) < cd <= d]
        if not window:
            return None, None
        current = window[-1][1]
        high = max(c for _, c in window)
        if not high or high <= 0:
            return None, None
        pct = round((high - current) / high * 100, 2)
        for upper, band in DD_BANDS:
            if pct <= upper:
                return band, pct
        return DD_OPEN_BAND, pct
    except Exception:
        return None, None


def state_band(state, variable):
    """The band value for one variable in a CycleState dict, or None."""
    try:
        return (state or {}).get(BAND_FIELD.get(variable) or '')
    except Exception:
        return None


def direction_for(variable, band, asset):
    """Spec §1 direction for a (variable, band, asset) cell: 'LONG',
    'SHORT', or None when the cell is not graded for this asset.
    Halving / month / DOW / drawdown: LONG everywhere. Dominance:
    DOM_UP grades BTC-long only, DOM_DOWN altcoin-long only, DOM_FLAT
    (control) grades LONG on every asset. Fear & Greed: LONG except
    extreme greed (FNG_XG), which tests the contrarian SHORT."""
    try:
        if not band:
            return None
        if variable in ('halving', 'month', 'dow', 'dd'):
            return 'LONG'
        if variable == 'dom':
            is_btc = (asset or '').upper().strip() in ('BTC', 'XBT')
            if band == 'DOM_UP':
                return 'LONG' if is_btc else None
            if band == 'DOM_DOWN':
                return 'LONG' if not is_btc else None
            if band == 'DOM_FLAT':
                return 'LONG'
            return None
        if variable == 'fng':
            return 'SHORT' if band == 'FNG_XG' else 'LONG'
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API — compute_state + plain-words pulse line
# ---------------------------------------------------------------------------

def compute_state(day, btc_closes, dom_now=None, dom_30d_ago=None,
                  fng=None):
    """CycleState for `day`: {date, halving_band, halving_day, month,
    dom_band, dom_change_pts, fng_band, fng_value, dow, dd_band,
    dd_pct}. Missing feeds read None ("no data") per variable. Pure —
    never raises."""
    try:
        d = _parse_day(day) or datetime.now(timezone.utc).date()
    except Exception:
        d = datetime.now(timezone.utc).date()
    try:
        hband, hday = halving_band(d)
    except Exception:
        hband, hday = None, None
    try:
        ddb, ddp = dd_band(btc_closes, d)
    except Exception:
        ddb, ddp = None, None
    try:
        fng_v = int(float(fng)) if fng is not None else None
    except (TypeError, ValueError):
        fng_v = None
    return {
        'date': d.isoformat(),
        'halving_band': hband,
        'halving_day': hday,
        'month': month_of(d),
        'dom_band': dom_band(dom_now, dom_30d_ago),
        'dom_change_pts': dom_change_pts(dom_now, dom_30d_ago),
        'fng_band': fng_band(fng_v),
        'fng_value': fng_v,
        'dow': dow_of(d),
        'dd_band': ddb,
        'dd_pct': ddp,
    }


_FNG_WORDS = {'FNG_XF': 'Extreme Fear', 'FNG_F': 'Fear',
              'FNG_N': 'Neutral', 'FNG_G': 'Greed',
              'FNG_XG': 'Extreme Greed'}
_DOM_WORDS = {'DOM_UP': 'Dominance rising', 'DOM_DOWN': 'Dominance falling',
              'DOM_FLAT': 'Dominance flat'}
_DD_WORDS = {'DD_SHALLOW': 'near its 365-day high',
             'DD_MID': '10–25% below its 365-day high',
             'DD_DEEP': '25–40% below its 365-day high',
             'DD_EXTREME': 'over 40% below its 365-day high'}


def pulse_line(state):
    """One plain-words line for the Picks page, e.g. "Halving day 565
    (H4) · November · Dominance falling · Fear & Greed 32 (Fear) ·
    BTC 10–25% below its 365-day high". Missing bands read "no data".
    The day-of-week is in the state (and graded) but not in the line
    — the page already shows the date. Pure — never raises."""
    try:
        st = state or {}
        parts = []
        if st.get('halving_band') and st.get('halving_day') is not None:
            parts.append(f"Halving day {st['halving_day']} "
                         f"({st['halving_band']})")
        else:
            parts.append('Halving no data')
        parts.append(st.get('month') or 'Month no data')
        parts.append(_DOM_WORDS.get(st.get('dom_band'),
                                    'Dominance no data'))
        if st.get('fng_band') and st.get('fng_value') is not None:
            parts.append(f"Fear & Greed {st['fng_value']} "
                         f"({_FNG_WORDS[st['fng_band']]})")
        else:
            parts.append('Fear & Greed no data')
        if st.get('dd_band'):
            parts.append(f"BTC {_DD_WORDS[st['dd_band']]}")
        else:
            parts.append('Drawdown no data')
        return ' · '.join(parts)
    except Exception:
        return 'Cycle pulse no data'


# ---------------------------------------------------------------------------
# Fetches (keyless, fail-open, never raise, never retry)
# ---------------------------------------------------------------------------

def fetch_btc_daily(fetch_json):
    """[[ts, close], ...] daily BTC closes from Kraken public OHLC
    (interval=1440). The last row is today's forming day — dropped,
    so only closed daily candles are ever stored. Kraken caps at 720
    candles/call, which is why the backfill uses Coinbase (below)."""
    try:
        data = fetch_json(KRAKEN_DAILY_URL)
        result = (data or {}).get('result') or {}
        rows = []
        for v in result.values():
            if isinstance(v, list):
                rows = v
                break
        closes = _ohlc(rows)
        closes.sort(key=lambda x: x[0])
        today = datetime.now(timezone.utc).date()
        while closes and datetime.fromtimestamp(
                closes[-1][0], tz=timezone.utc).date() >= today:
            closes.pop()
        return closes[-MAX_DAILY_STORED:]
    except Exception:
        return []


def fetch_dominance(fetch_json):
    """BTC dominance % from CoinGecko /global, or None."""
    try:
        data = fetch_json(COINGECKO_GLOBAL_URL)
        pct = ((data or {}).get('data') or {}).get(
            'market_cap_percentage') or {}
        v = pct.get('btc')
        return float(v) if v is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def fetch_fng(fetch_json):
    """Latest Fear & Greed index value (int 0-100), or None."""
    try:
        data = fetch_json(FNG_URL)
        rows = (data or {}).get('data') or []
        if not rows:
            return None
        return int(float(rows[0].get('value')))
    except (TypeError, ValueError, IndexError, AttributeError):
        return None


def fetch_fng_history(fetch_json):
    """{date_str: int} Fear & Greed history (Alternative.me limit=0 —
    full history, from 2018-02-01). {} on any failure."""
    out = {}
    try:
        data = fetch_json(FNG_HISTORY_URL)
        for row in (data or {}).get('data') or []:
            try:
                ts = int(row.get('timestamp'))
                d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
                out[d.isoformat()] = int(float(row.get('value')))
            except (TypeError, ValueError):
                continue
    except Exception:
        return out
    return out


def fetch_btc_daily_range_coinbase(fetch_json, start_day, end_day):
    """[[ts, close], ...] BTC daily closes from Coinbase public
    candles for one date window (<= 300 days — the endpoint's cap).
    Backfill-only (see module docstring)."""
    try:
        s = _parse_day(start_day)
        e = _parse_day(end_day)
        if not s or not e or s > e:
            return []
        url = COINBASE_CANDLES_URL.format(
            start=s.isoformat() + 'T00:00:00Z',
            end=(e + timedelta(days=1)).isoformat() + 'T00:00:00Z')
        data = fetch_json(url)
        if not isinstance(data, list):
            return []
        closes = _ohlc(data)
        closes.sort(key=lambda x: x[0])
        return closes
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Stores / cache helpers (pure, never raise)
# ---------------------------------------------------------------------------

def update_btc_daily_store(prior, fetched):
    """Merge fetched daily closes into the stored cycle_btc_daily
    ({[[ts, close]]}): dedup by ts, sorted, last MAX_DAILY_STORED.
    Returns a NEW list; never raises."""
    try:
        merged = {}
        for row in (prior or []):
            try:
                merged[int(row[0])] = [int(row[0]), float(row[1])]
            except (TypeError, ValueError, IndexError):
                continue
        for row in (fetched or []):
            try:
                merged[int(row[0])] = [int(row[0]), float(row[1])]
            except (TypeError, ValueError, IndexError):
                continue
        return sorted(merged.values(),
                      key=lambda x: x[0])[-MAX_DAILY_STORED:]
    except Exception:
        return list(prior or [])


def update_dom_history(prior, date_str, value):
    """{date_str: dominance} daily cache, last DOM_CACHE_DAYS days.
    A None value leaves the history untouched. Returns a NEW dict."""
    try:
        out = {}
        for k, v in (prior or {}).items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        if value is not None:
            out[str(date_str)] = round(float(value), 2)
        if not out:
            return out
        keep_from = (max(_parse_day(k) for k in out)
                     - timedelta(days=DOM_CACHE_DAYS)).isoformat()
        return {k: v for k, v in sorted(out.items())
                if k >= keep_from}
    except Exception:
        return dict(prior or {})


def dom_30d_ago(dom_history, date_str):
    """The cached dominance value nearest 30 days before `date_str`
    (within +/-DOM_STALE_DAYS), preferring the earlier day on ties.
    None when the cache has no value that old yet."""
    try:
        d = _parse_day(date_str)
        if d is None:
            return None
        target = d - timedelta(days=DOM_LOOKBACK_DAYS)
        best = None
        for k, v in (dom_history or {}).items():
            kd = _parse_day(k)
            if kd is None:
                continue
            gap = abs((kd - target).days)
            if gap > DOM_STALE_DAYS:
                continue
            if best is None or gap < best[0] or (
                    gap == best[0] and kd < best[1]):
                best = (gap, kd, float(v))
        return best[2] if best else None
    except Exception:
        return None


def build_history(btc_closes, fng_by_date=None):
    """{date_str: CycleState} for every day from the first close to
    the last close. Halving / month / DOW / drawdown come from the BTC
    closes; FNG from `fng_by_date` when present; dominance is always
    "no data" in history (no keyless history source). Pure."""
    out = {}
    try:
        closes = _parse_closes(btc_closes)
        if not closes:
            return out
        first, last = closes[0][0], closes[-1][0]
        d = first
        while d <= last:
            fng = (fng_by_date or {}).get(d.isoformat())
            out[d.isoformat()] = compute_state(d, closes, None, None,
                                               fng)
            d += timedelta(days=1)
        return out
    except Exception:
        return out


# ---------------------------------------------------------------------------
# One-off backfill job: python3 Scripts/cycle_feed.py
# Writes data/cycle_history.json (BTC daily closes 2016 -> today).
# ---------------------------------------------------------------------------

def _http_json(url, timeout=30):
    req = urllib.request.Request(
        url, headers={'User-Agent': 'NexIntel-CycleBackfill/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def backfill_main():
    """Fetch BTC daily closes (Coinbase, paged) + FNG history, build
    the daily-state history, write data/cycle_history.json."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_path = os.path.join(root, 'data', 'cycle_history.json')
    today = datetime.now(timezone.utc).date()
    closes = []
    start = BACKFILL_START
    while start <= today:
        end = min(start + timedelta(days=BACKFILL_PAGE_DAYS - 1), today)
        page = fetch_btc_daily_range_coinbase(_http_json, start, end)
        print(f'  backfill: {start}..{end}: {len(page)} closes')
        closes.extend(page)
        start = end + timedelta(days=1)
    closes.sort(key=lambda x: x[0])
    dedup = {}
    for ts, c in closes:
        dedup[int(ts)] = [int(ts), float(c)]
    closes = sorted(dedup.values(), key=lambda x: x[0])
    # Drop today's forming daily candle — states use closed closes only.
    closes = [row for row in closes
              if datetime.fromtimestamp(row[0], tz=timezone.utc).date()
              < today]
    print(f'  backfill: {len(closes)} daily closes total')
    fng_hist = fetch_fng_history(_http_json)
    print(f'  backfill: {len(fng_hist)} FNG history days')
    history = build_history(closes, fng_hist)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(history, f, indent=2)
    print(f'  backfill: wrote {len(history)} daily states to {out_path}')


if __name__ == '__main__':
    backfill_main()
