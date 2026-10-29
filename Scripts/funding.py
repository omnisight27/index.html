"""Feed #2 — perpetual funding rates (Nic 2026-10-29: "wire one feed at a
time"; the unknown variable is *who's buying and why* — funding reads
whether the crowd is already all-long, i.e. whether anyone is left to buy).

Source: Kraken Futures public tickers (no key, no geo-block — Binance's
futures API 451s from US egress, Hyperliquid never answers):
    GET https://futures.kraken.com/derivatives/api/v3/tickers
one bulk call; relativeFundingRate is the decimal funding rate per 4h
interval, so ann_pct = r * 6 * 365 * 100. Any failure fails OPEN (empty
map) so this feed can never block the engine run.

Scoring (the plugin contract: a number in [-1, +1], negative = bearish for
continuation):
    score = -tanh((ann_pct - BASELINE) / SCALE)
At the ~15% baseline the score is 0 (normal cost of being long). Deeply
positive funding (crowded longs) scores toward -1: the move is fragile, a
long squeeze down is the risk. Negative funding (crowded shorts) scores
toward +1: squeeze-up potential.

v1 integration: the score gates the mover *runner* (stretch target) — no
runner is printed into a crowded-long ceiling (score < -0.5 suppresses it),
and the reading is published on the mover's feed payload so cards and the
Proof Book can see it.
"""

import math

TICKERS_URL = 'https://futures.kraken.com/derivatives/api/v3/tickers'

# Annualized funding % that counts as "normal" (score 0) and the scale over
# which crowdedness saturates toward -1.
BASELINE_ANN_PCT = 15.0
SCALE_ANN_PCT = 40.0
# Runner suppression: below this score the longs are too crowded to ride.
RUNNER_SUPPRESS_SCORE = -0.5

# Kraken perp symbols that don't follow the plain PF_{ASSET}USD pattern.
SYMBOL_OVERRIDES = {
    'BTC': 'PF_XBTUSD',
}


def kraken_symbol(asset):
    """Map an engine asset symbol to its Kraken Futures perp symbol."""
    a = (asset or '').upper()
    if a in SYMBOL_OVERRIDES:
        return SYMBOL_OVERRIDES[a]
    return f'PF_{a}USD' if a else ''


def annualized_pct(rel_rate_4h):
    """Decimal funding rate per 4h interval -> annualized percent."""
    return rel_rate_4h * 6 * 365 * 100


def funding_score(ann_pct):
    """[-1, +1] crowdedness score. Positive funding (crowded longs) is
    negative for continuation; negative funding (crowded shorts) is
    positive (squeeze-up potential)."""
    if ann_pct is None:
        return None
    return -math.tanh((ann_pct - BASELINE_ANN_PCT) / SCALE_ANN_PCT)


def fetch_funding(fetch_json):
    """{kraken_symbol: {'rate_4h': float, 'ann_pct': float, 'oi': float}}
    from one bulk call. Returns {} on any failure (fail open — never
    blocks the engine)."""
    data = fetch_json(TICKERS_URL)
    tickers = (data or {}).get('tickers') if isinstance(data, dict) else None
    if not isinstance(tickers, list):
        return {}
    out = {}
    for t in tickers:
        try:
            sym = t.get('symbol')
            rate = float(t.get('relativeFundingRate') or 0)
            oi = t.get('openInterest')
        except (TypeError, ValueError):
            continue
        if sym and sym.startswith('PF_'):
            out[sym] = {'rate_4h': rate, 'ann_pct': annualized_pct(rate),
                        'oi': oi}
    return out


def funding_by_asset(assets, fetch_json):
    """{ASSET: {'ann_pct', 'score', 'oi'}} for the engine's asset symbols.
    Assets with no Kraken perp are simply absent."""
    bulk = fetch_funding(fetch_json)
    if not bulk:
        return {}
    out = {}
    for a in assets:
        ksym = kraken_symbol(a)
        row = bulk.get(ksym)
        if row is None:
            continue
        out[a] = {'ann_pct': round(row['ann_pct'], 2),
                  'score': round(funding_score(row['ann_pct']), 3),
                  'oi': row['oi']}
    return out


def runner_allowed(funding_score_or_none):
    """The mover runner may print unless longs are crowded past the
    suppression line. Missing data fails open (the momentum gates still
    bind)."""
    if funding_score_or_none is None:
        return True
    return funding_score_or_none >= RUNNER_SUPPRESS_SCORE
