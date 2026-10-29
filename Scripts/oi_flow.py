"""Feed #3 — open-interest flow (Nic 2026-10-29: "wire one feed at a time").

Funding (Feed #2) reads positioning *level* — how crowded the longs are.
OI flow reads positioning *flow* — what traders did in the last hour:

    OI up + price up    = new longs entering      -> healthy fuel      (+1.0)
    OI up + price down  = positioning into weakness -> fragile         (-0.8)
    OI down + price up  = short covering           -> may exhaust      (-0.4)
    OI down + price down= longs capitulating       -> selling exhausts (+0.5)

score = quadrant x tanh(|oi_chg_1h_pct| / 15), so a tiny OI wiggle scores
near 0 no matter the quadrant.

OI comes free inside the Feed #2 Kraken tickers call — no new API, no key.
History rides inside data/intraday.json under `oi_history` (the same
persistence trick as accum_history and the Proof Book: the workflow
commits the feed every run). Each symbol keeps the last 30 hourly
observations — enough for 1h and 24h changes.

v1 integration: the score is published on every mover (`oi_flow`), a
strongly adverse reading (<= -0.7) vetoes the runner alongside the funding
gate, and the card prints one plain line on strong readings.
"""

import math

MAX_POINTS = 30          # hourly observations kept per symbol (~30h)
OI_SCALE_PCT = 15.0      # tanh scale: 15%/1h OI move -> 0.76 x quadrant
OI_VETO_SCORE = -0.7     # at or below: runner withheld
CARD_LINE_THRESHOLD = 0.6  # |score| at or above: print the OI card line


def update_oi_history(hist, fund_map, now_ts):
    """Fold this run's OI readings into history. hist: {ASSET: [[ts, oi],
    ...]}. fund_map: {ASSET: {'oi': float, ...}}. Returns a NEW dict;
    symbols with no OI reading are left untouched."""
    new_hist = {a: list(pts) for a, pts in (hist or {}).items()}
    for asset, row in (fund_map or {}).items():
        oi = (row or {}).get('oi')
        if oi is None:
            continue
        try:
            oi = float(oi)
        except (TypeError, ValueError):
            continue
        pts = new_hist.get(asset, [])
        pts.append([now_ts, oi])
        new_hist[asset] = pts[-MAX_POINTS:]
    return new_hist


def _change_pct(hist_pts, now_ts, lookback_secs):
    """% change from the reading nearest `lookback_secs` ago to the latest.
    None when the window can't be built (too few points, gap, zero base)."""
    if not hist_pts or len(hist_pts) < 2:
        return None
    latest_ts, latest_oi = hist_pts[-1]
    target = now_ts - lookback_secs
    base = None
    for ts, oi in reversed(hist_pts[:-1]):
        if ts <= target:
            base = (ts, oi)
            break
    if base is None:
        return None
    base_ts, base_oi = base
    if now_ts - base_ts > lookback_secs + 3600:  # stale — a gap, not a trend
        return None
    if not base_oi:
        return None
    return (latest_oi - base_oi) / base_oi * 100


def quadrant_score(oi_chg_pct, price_chg_pct):
    """The four OI/price regimes, signed for a LONG lens."""
    oi_up = oi_chg_pct > 0
    px_up = price_chg_pct > 0
    if oi_up and px_up:
        return 1.0
    if oi_up and not px_up:
        return -0.8
    if not oi_up and px_up:
        return -0.4
    return 0.5


def flow_for(asset, hist, chg_1h, now_ts):
    """{'oi_chg_1h_pct', 'oi_chg_24h_pct', 'score'} or None when the 1h
    window can't be built. chg_1h is the coin's last-hour % move."""
    pts = (hist or {}).get(asset)
    oi_1h = _change_pct(pts, now_ts, 3600)
    if oi_1h is None or chg_1h is None:
        return None
    oi_24h = _change_pct(pts, now_ts, 24 * 3600)
    score = quadrant_score(oi_1h, chg_1h) * math.tanh(abs(oi_1h) / OI_SCALE_PCT)
    return {'oi_chg_1h_pct': round(oi_1h, 2),
            'oi_chg_24h_pct': round(oi_24h, 2) if oi_24h is not None else None,
            'score': round(score, 3)}


def runner_ok(oi_score):
    """Strongly adverse OI flow vetoes the runner. Missing data fails
    open."""
    if oi_score is None:
        return True
    return oi_score > OI_VETO_SCORE


def card_line(flow):
    """One plain-language line for a strong OI reading, or None."""
    if not flow:
        return None
    s = flow.get('score')
    if s is None or abs(s) < CARD_LINE_THRESHOLD:
        return None
    x = flow.get('oi_chg_1h_pct') or 0
    if s > 0:
        return f"OI {x:+.0f}%/1h with the move — new money fueling it"
    return f"OI {x:+.0f}%/1h against the move — fragile"
