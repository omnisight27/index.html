"""Deribit options feed — free data-edge track #3 (2026-11-07,
~/workspace/goals/build-out-omnisight/hidden_files/
free-data-edge-dive-2026-11-07.md).

DISPLAY-ONLY + SHADOW. This module never touches a buy gate: it does
not import, call, or consult gate_math.py, gate_circuit.py, or the
60% live bar. Deribit readings are annotations, never inputs. No
alert-tier output, no confidence influence, no trades.

Why Deribit: the engine has NO volatility data anywhere — IV is a
genuinely new dimension (a vol spike precedes most large moves;
put-call skew is a contrarian tell). One keyless public call per
currency returns the whole option book.

Endpoint (GET, keyless, no sign-up):
  * Book summary by currency:
    /api/v2/public/get_book_summary_by_currency?currency={BTC|ETH}
    &kind=option
    Per-option mark IV, OI, greeks-free mark price, bid/ask, volume,
    underlying price, and the row's interest rate. ~930 BTC rows,
    ~784 ETH rows per call.

Honest limitations (read before trusting a number):
  * DVOL is NOT available: Deribit's get_index and
    get_volatility_index_data endpoints both return HTTP 400 from
    this runtime, so the 30-day ATM IV below is computed from the
    book (average of the call/put mark IV at the strike nearest the
    underlying, expiry nearest 30 days) — not the published DVOL.
  * 25-delta is APPROXIMATE: the book-summary endpoint carries no
    greeks/delta, so delta is computed by Black-Scholes from each
    option's own mark IV (standard practice) with the row's
    interest rate. The deltas actually found are reported next to
    the skew, so a skew built on 0.31/-0.19 deltas is visible as
    such and never presented as an exact 25-delta read.
  * Max pain uses the expiry nearest 30 days (the monthly-ish
    expiry), not the nearest weekly — the pin that matters on the
    horizon the ATM IV is measured on.
  * Underlying price is the MEDIAN of the per-row underlying_price
    values (rows vary slightly — they are mark-based snapshots).

Every fetch is fail-open: a dead endpoint, a 400/429, or bad JSON
degrades that currency (or that field) to "no data", never an
exception, never a blocked run. Two calls per run (BTC + ETH) —
no pacing needed at that volume.
"""

import math
import re
import time
from datetime import date, datetime, timedelta, timezone

BASE_URL = 'https://www.deribit.com/api/v2'
BOOK_URL = (BASE_URL + '/public/get_book_summary_by_currency'
            '?currency={ccy}&kind=option')

CURRENCIES = ('BTC', 'ETH')

TARGET_DTE = 30        # ATM IV / skew / max-pain expiry target (days)
TARGET_DELTA = 0.25    # skew legs: put delta ~-0.25, call delta ~+0.25

# Instrument names look like BTC-25DEC26-104000-C (currency, expiry
# DDMMMYY, strike, C/P).
_INSTR_RE = re.compile(r'^([A-Z]+)-(\d{2}[A-Z]{3}\d{2})-([\d.]+)-([CP])$')


def _parse_expiry(code):
    """'25DEC26' -> date, or None."""
    try:
        return datetime.strptime(code, '%d%b%y').date()
    except (TypeError, ValueError):
        return None


def parse_instrument(name):
    """(currency, expiry date, strike, 'C'/'P') from a Deribit
    instrument name, or None on any malformed name."""
    try:
        m = _INSTR_RE.match((name or '').strip())
        if not m:
            return None
        expiry = _parse_expiry(m.group(2))
        if expiry is None:
            return None
        return (m.group(1), expiry, float(m.group(3)), m.group(4))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Parsers — pure, never raise, None / [] on bad data
# ---------------------------------------------------------------------------

def parse_book(data):
    """[option, ...] from a get_book_summary_by_currency response.
    Each option: {currency, expiry (date), strike, kind, iv (pct
    points), oi (contracts), mark_price, underlying, rate}. Rows
    with a bad name, non-positive IV, or missing OI are dropped.
    [] on any failure."""
    out = []
    try:
        rows = (data or {}).get('result')
        if not isinstance(rows, list):
            return out
        for row in rows:
            try:
                if not isinstance(row, dict):
                    continue
                parsed = parse_instrument(row.get('instrument_name'))
                if parsed is None:
                    continue
                ccy, expiry, strike, kind = parsed
                iv = float(row.get('mark_iv'))
                if iv <= 0:
                    continue
                oi = float(row.get('open_interest'))
                under = row.get('underlying_price')
                under = float(under) if under not in (None, '') else None
                rate = row.get('interest_rate')
                try:
                    rate = float(rate) if rate not in (None, '') else 0.0
                except (TypeError, ValueError):
                    rate = 0.0
                out.append({'currency': ccy, 'expiry': expiry,
                            'strike': strike, 'kind': kind, 'iv': iv,
                            'oi': oi, 'mark_price': row.get('mark_price'),
                            'underlying': under, 'rate': rate})
            except (TypeError, ValueError):
                continue
    except Exception:
        return out
    return out


def _median(values):
    """Median of a numeric list, or None when empty."""
    try:
        vals = sorted(float(v) for v in (values or []))
        if not vals:
            return None
        n = len(vals)
        if n % 2:
            return vals[n // 2]
        return (vals[n // 2 - 1] + vals[n // 2]) / 2
    except (TypeError, ValueError):
        return None


def underlying_price(options):
    """Median underlying across the book's rows (they vary slightly
    — mark-based snapshots), or None when no row carries one."""
    try:
        return _median([o.get('underlying') for o in (options or [])
                        if o.get('underlying') is not None])
    except Exception:
        return None


def nearest_expiry(options, target_dte=TARGET_DTE, today=None):
    """The expiry date nearest `target_dte` days out (future
    expiries only — dte >= 1), ties broken toward the later expiry.
    None when the book has no future expiry."""
    try:
        d = today or datetime.now(timezone.utc).date()
        if isinstance(d, datetime):
            d = d.date()
        cands = set()
        for o in (options or []):
            exp = (o or {}).get('expiry')
            if not isinstance(exp, date):
                continue
            dte = (exp - d).days
            if dte >= 1:
                cands.add(exp)
        if not cands:
            return None
        return min(cands,
                   key=lambda e: (abs((e - d).days - target_dte),
                                  -(e - d).days))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Black-Scholes delta (for the approximate 25-delta skew)
# ---------------------------------------------------------------------------

def _norm_cdf(x):
    """Standard normal CDF via erf. Pure, never raises."""
    try:
        return 0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))
    except (TypeError, ValueError):
        return None


def bs_delta(kind, spot, strike, dte_days, iv_pct, rate=0.0):
    """Black-Scholes delta for one option: call in (0, 1), put in
    (-1, 0). sigma is the option's own mark IV (standard practice
    for a delta-from-IV read). None when the inputs can't price
    (zero/negative spot/strike/IV/time)."""
    try:
        s = float(spot)
        k = float(strike)
        sigma = float(iv_pct) / 100.0
        t = float(dte_days) / 365.0
        r = float(rate or 0.0)
        if s <= 0 or k <= 0 or sigma <= 0 or t <= 0:
            return None
        d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) \
            / (sigma * math.sqrt(t))
        n = _norm_cdf(d1)
        if n is None:
            return None
        return n if (kind or '').upper() == 'C' else n - 1.0
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Metrics — pure, never raise, None on missing data
# ---------------------------------------------------------------------------

def atm_iv(options, expiry, underlying):
    """30-day ATM IV: mean of the call and put mark IV at the strike
    nearest the underlying, on `expiry`. One side alone is used
    when the other has no quote. None when the expiry has no
    options or no underlying."""
    try:
        if not expiry or underlying is None:
            return None
        s = float(underlying)
        legs = [o for o in (options or [])
                if (o or {}).get('expiry') == expiry]
        if not legs:
            return None
        strike = min((o['strike'] for o in legs),
                     key=lambda k: abs(k - s))
        ivs = [o['iv'] for o in legs if o['strike'] == strike]
        if not ivs:
            return None
        return round(sum(ivs) / len(ivs), 2)
    except (TypeError, ValueError):
        return None


def skew_25d(options, expiry, underlying, today=None):
    """{'skew', 'put_iv', 'call_iv', 'put_delta', 'call_delta'}:
    put IV minus call IV at the ~25-delta legs of `expiry` (the put
    whose BS delta is nearest -0.25, the call nearest +0.25).
    Positive skew = puts pricier than calls (fear). None when
    either leg is missing. The deltas actually found ride along
    so an approximate read is visible as one."""
    try:
        if not expiry or underlying is None:
            return None
        d = today or datetime.now(timezone.utc).date()
        if isinstance(d, datetime):
            d = d.date()
        dte = (expiry - d).days
        if dte < 1:
            return None
        legs = [o for o in (options or [])
                if (o or {}).get('expiry') == expiry]
        if not legs:
            return None
        s = float(underlying)
        best_put = None
        best_call = None
        for o in legs:
            delta = bs_delta(o['kind'], s, o['strike'], dte, o['iv'],
                             o.get('rate') or 0.0)
            if delta is None:
                continue
            if o['kind'] == 'P':
                gap = abs(delta - (-TARGET_DELTA))
                if best_put is None or gap < best_put[0]:
                    best_put = (gap, o['iv'], delta)
            else:
                gap = abs(delta - TARGET_DELTA)
                if best_call is None or gap < best_call[0]:
                    best_call = (gap, o['iv'], delta)
        if best_put is None or best_call is None:
            return None
        return {'skew': round(best_put[1] - best_call[1], 2),
                'put_iv': round(best_put[1], 2),
                'call_iv': round(best_call[1], 2),
                'put_delta': round(best_put[2], 3),
                'call_delta': round(best_call[2], 3)}
    except (TypeError, ValueError):
        return None


def put_call_oi_ratio(options):
    """Total put OI / total call OI across ALL expiries (the whole
    book's positioning). > 1 = more put OI than call OI. None
    when call OI is zero or the book is empty."""
    try:
        put_oi = 0.0
        call_oi = 0.0
        for o in (options or []):
            if (o or {}).get('kind') == 'P':
                put_oi += float(o.get('oi') or 0)
            elif (o or {}).get('kind') == 'C':
                call_oi += float(o.get('oi') or 0)
        if call_oi <= 0:
            return None
        return round(put_oi / call_oi, 3)
    except (TypeError, ValueError):
        return None


def max_pain(options, expiry, underlying):
    """{'strike', 'distance_pct'}: the strike that minimizes the
    total intrinsic value of the expiry's OI (calls pay max(S-K,0),
    puts pay max(K-S,0), weighted by OI) — where the market is
    pinned at expiry. distance_pct is (underlying - strike) /
    underlying in % (negative = max pain above spot). None when
    the expiry has no options or no underlying."""
    try:
        if not expiry or underlying is None:
            return None
        s = float(underlying)
        legs = [o for o in (options or [])
                if (o or {}).get('expiry') == expiry]
        if not legs:
            return None
        strikes = sorted({o['strike'] for o in legs})
        best = None
        for cand in strikes:
            total = 0.0
            for o in legs:
                oi = float(o.get('oi') or 0)
                if o['kind'] == 'C':
                    total += max(cand - o['strike'], 0.0) * oi
                else:
                    total += max(o['strike'] - cand, 0.0) * oi
            if best is None or total < best[0]:
                best = (total, cand)
        if best is None:
            return None
        return {'strike': best[1],
                'distance_pct': round((s - best[1]) / s * 100, 2)}
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Fetches — fail-open, never raise
# ---------------------------------------------------------------------------

def fetch_currency(fetch_json, currency, today=None):
    """One currency's Deribit readings: 30d ATM IV, 25-delta skew,
    put/call OI, max pain, and the book size. Fields are None for
    anything missing; 'error' is set only when the book itself
    failed to load. Never raises."""
    ccy = (currency or '').upper()
    row = {'currency': ccy, 'underlying': None, 'expiry': None,
           'dte': None, 'atm_iv': None, 'skew_25d': None,
           'put_iv_25d': None, 'call_iv_25d': None,
           'put_delta_25d': None, 'call_delta_25d': None,
           'put_call_oi': None, 'max_pain_strike': None,
           'max_pain_distance_pct': None, 'options': 0, 'error': None}
    if not ccy:
        row['error'] = 'no currency'
        return row
    try:
        data = fetch_json(BOOK_URL.format(ccy=ccy))
    except Exception as e:
        row['error'] = f'book: {str(e)[:80]}'
        return row
    options = parse_book(data)
    row['options'] = len(options)
    if not options:
        row['error'] = 'empty book'
        return row
    try:
        d = today or datetime.now(timezone.utc).date()
        if isinstance(d, datetime):
            d = d.date()
        under = underlying_price(options)
        row['underlying'] = round(under, 2) if under is not None else None
        expiry = nearest_expiry(options, TARGET_DTE, d)
        if expiry is not None:
            row['expiry'] = expiry.isoformat()
            row['dte'] = (expiry - d).days
            row['atm_iv'] = atm_iv(options, expiry, under)
            sk = skew_25d(options, expiry, under, d)
            if sk:
                row['skew_25d'] = sk['skew']
                row['put_iv_25d'] = sk['put_iv']
                row['call_iv_25d'] = sk['call_iv']
                row['put_delta_25d'] = sk['put_delta']
                row['call_delta_25d'] = sk['call_delta']
            mp = max_pain(options, expiry, under)
            if mp:
                row['max_pain_strike'] = mp['strike']
                row['max_pain_distance_pct'] = mp['distance_pct']
        row['put_call_oi'] = put_call_oi_ratio(options)
    except Exception as e:
        row['error'] = f'compute: {str(e)[:80]}'
    return row


def fetch_all(fetch_json, currencies=CURRENCIES, today=None):
    """{CCY: row} for BTC + ETH (order kept). A failed currency
    still gets a row (all None + error) so the diag counts honestly.
    Never raises."""
    try:
        out = {}
        for c in (currencies or []):
            ccy = (c or '').upper().strip()
            if not ccy or ccy in out:
                continue
            try:
                out[ccy] = fetch_currency(fetch_json, ccy, today=today)
            except Exception as e:
                out[ccy] = {'currency': ccy, 'underlying': None,
                            'expiry': None, 'dte': None, 'atm_iv': None,
                            'skew_25d': None, 'put_iv_25d': None,
                            'call_iv_25d': None, 'put_delta_25d': None,
                            'call_delta_25d': None, 'put_call_oi': None,
                            'max_pain_strike': None,
                            'max_pain_distance_pct': None, 'options': 0,
                            'error': str(e)[:120]}
        return out
    except Exception:
        return {}


def pulse_line(deribit_map):
    """One plain-words line for the run log, e.g. "Deribit: BTC
    30d ATM IV 43.1%, skew +4.2 (put 47.3 / call 43.1), P/C OI 0.87,
    max pain 76,000 (-0.5%) · 2 currencies". Missing fields read
    "no data". Pure — never raises."""
    try:
        m = deribit_map or {}
        if not m:
            return 'Deribit no data'
        btc = m.get('BTC') or {}
        parts = []
        if btc.get('atm_iv') is not None:
            parts.append(f"BTC 30d ATM IV {btc['atm_iv']:.1f}%")
        else:
            parts.append('BTC ATM IV no data')
        if btc.get('skew_25d') is not None:
            parts.append(f"skew {btc['skew_25d']:+.1f}")
        if btc.get('put_call_oi') is not None:
            parts.append(f"P/C OI {btc['put_call_oi']:.2f}")
        if btc.get('max_pain_strike') is not None:
            parts.append(
                f"max pain {btc['max_pain_strike']:,.0f} "
                f"({btc['max_pain_distance_pct']:+.1f}%)")
        n_ok = sum(1 for r in m.values()
                   if (r or {}).get('atm_iv') is not None)
        parts.append(f"{n_ok}/{len(m)} currencies")
        return 'Deribit: ' + ' '.join(parts)
    except Exception:
        return 'Deribit no data'


# ---------------------------------------------------------------------------
# Live smoke test: python3 Scripts/deribit_feed.py
# ---------------------------------------------------------------------------

def _http_json(url, timeout=30):
    import json
    import urllib.request
    req = urllib.request.Request(
        url, headers={'User-Agent': 'NexIntel-DeribitFeed/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def smoke_main():
    """Fetch BTC/ETH live and print the values."""
    import json as _json
    rows = fetch_all(_http_json, CURRENCIES)
    for ccy, row in rows.items():
        print(f"  {ccy}: underlying={row['underlying']} "
              f"expiry={row['expiry']} dte={row['dte']} "
              f"atm_iv={row['atm_iv']} skew={row['skew_25d']} "
              f"(put {row['put_iv_25d']} @ {row['put_delta_25d']}, "
              f"call {row['call_iv_25d']} @ {row['call_delta_25d']}) "
              f"pc_oi={row['put_call_oi']} "
              f"max_pain={row['max_pain_strike']} "
              f"({row['max_pain_distance_pct']}%) "
              f"options={row['options']} error={row['error']}")
    print('  ' + pulse_line(rows))
    print('  raw BTC row: ' + _json.dumps(rows.get('BTC') or {}))


if __name__ == '__main__':
    smoke_main()
