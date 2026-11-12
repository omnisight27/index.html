"""Verified spot-venue listings — SIM REPORTING ONLY. Live gates untouched.

The engine has always checked two venues Nic can trade on (Coinbase,
Crypto.com) for the live tradability gate. The paper sims now also report
two more US-accessible venues — Kraken and OKX — verified from their
public, unauthenticated listing endpoints, the same fail-open pattern as
the Coinbase / Crypto.com fetches. A dead endpoint leaves that venue's
set empty; it never blocks a run.

`tradable` (Coinbase/Crypto.com only) is UNCHANGED — it is what the live
gate (decide.py), the live alerts (intraday-sms.py) and the engine's own
gate_src checks read. `venues` is the widened, sim-reporting list, in
VENUE_ORDER. A pick's `venues` says where it would have executed; an
empty list means NO verified venue — the venue-blind sims still
paper-fill it at the listed price, and say so in the report.

Kraken note: its websocket names use XBT for BTC and XDG for DOGE; every
other wsname base already matches the engine's CoinGecko symbols. The
map below is the only normalization. OKX note: OKX's US spot platform
(launched April 2025) is US-accessible; only `live` USD/USDT spot
instruments count.

Nothing in this module touches scoring, gates, or alerts.
"""

# All venues the engine can verify, in reporting order.
VENUE_ORDER = ['coinbase', 'cryptocom', 'kraken', 'okx']

# Nic's live-trading venues — the ONLY ones the live gate reads.
LIVE_VENUES = ('coinbase', 'cryptocom')

# Kraken wsname bases that differ from the engine's symbols.
_KRAKEN_SYMBOL_MAP = {'XBT': 'BTC', 'XDG': 'DOGE'}


def parse_coinbase_products(payload):
    """Spot base currencies with an online USD pair (Coinbase Exchange)."""
    return {p['base_currency'] for p in (payload or [])
            if p.get('quote_currency') == 'USD'
            and p.get('status') == 'online' and p.get('base_currency')}


def parse_cryptocom_instruments(payload):
    """Spot base currencies with a USD/USDT pair (Crypto.com)."""
    return {i['base_ccy'] for i in ((payload or {}).get('result', {}).get('data', []) or [])
            if i.get('inst_type') == 'CCY_PAIR'
            and i.get('quote_ccy') in ('USD', 'USDT') and i.get('base_ccy')}


def parse_kraken_pairs(payload):
    """Spot base symbols with a USD/USDT pair, from a Kraken AssetPairs
    payload. Uses `wsname` (e.g. 'BTC/USD') because `base` carries Kraken
    codes (XXBT, XETH) instead of the plain symbol."""
    out = set()
    for v in ((payload or {}).get('result') or {}).values():
        ws = (v or {}).get('wsname') or ''
        if '/' not in ws:
            continue
        base, quote = ws.split('/', 1)
        if quote in ('USD', 'USDT'):
            out.add(_KRAKEN_SYMBOL_MAP.get(base, base))
    return out


def parse_okx_instruments(payload):
    """Spot base currencies with a live USD/USDT pair, from an OKX
    instruments payload."""
    out = set()
    for row in (payload or {}).get('data') or []:
        if row.get('state') == 'live' and row.get('quoteCcy') in ('USD', 'USDT'):
            base = row.get('baseCcy')
            if base:
                out.add(base)
    return out


def fetch_venue_listings(fetch_json):
    """Fetch all four venue listing sets. Fail-open per venue: a dead
    endpoint leaves that venue's set empty and prints why."""
    listings = {v: set() for v in VENUE_ORDER}
    try:
        listings['coinbase'] = parse_coinbase_products(
            fetch_json('https://api.exchange.coinbase.com/products') or [])
        print(f"  listings: Coinbase {len(listings['coinbase'])} coins")
    except Exception as e:
        print(f'  coinbase listings failed: {e}')
    try:
        listings['cryptocom'] = parse_cryptocom_instruments(
            fetch_json('https://api.crypto.com/exchange/v1/public/get-instruments') or {})
        print(f"  listings: Crypto.com {len(listings['cryptocom'])} coins")
    except Exception as e:
        print(f'  cryptocom listings failed: {e}')
    try:
        listings['kraken'] = parse_kraken_pairs(
            fetch_json('https://api.kraken.com/0/public/AssetPairs') or {})
        print(f"  listings: Kraken {len(listings['kraken'])} coins")
    except Exception as e:
        print(f'  kraken listings failed: {e}')
    try:
        listings['okx'] = parse_okx_instruments(
            fetch_json('https://www.okx.com/api/v5/public/instruments?instType=SPOT') or {})
        print(f"  listings: OKX {len(listings['okx'])} coins")
    except Exception as e:
        print(f'  okx listings failed: {e}')
    return listings


def tradable_on(sym, listings):
    """Where Nic can trade this coin: subset of ['coinbase', 'cryptocom'].
    UNCHANGED — the live gate reads exactly this."""
    listings = listings or {}
    out = []
    if sym in listings.get('coinbase', set()):
        out.append('coinbase')
    if sym in listings.get('cryptocom', set()):
        out.append('cryptocom')
    return out


def venues_on(sym, listings):
    """Widened verified venues for sim reporting: subset of VENUE_ORDER."""
    listings = listings or {}
    return [v for v in VENUE_ORDER if sym in listings.get(v, set())]
