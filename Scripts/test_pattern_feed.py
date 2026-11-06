"""Price-action pattern track, Phase 1 — pure-function tests.

Synthetic candle fixtures per pattern: must-fire, must-not-fire, and
edge cases (doji, gaps, missing data). No disk, no network (the Kraken
fetch is tested with a fake fetch_json).

Run: python3 -m pytest Scripts/test_pattern_feed.py
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pattern_feed as pf

T0 = 1_800_000_000


# ---------------------------------------------------------------- fixtures

def lin(n, a, b):
    """n closes linearly from a to b inclusive."""
    if n <= 1:
        return [a]
    return [a + (b - a) * k / (n - 1) for k in range(n)]


def seg(closes, rng=1.0):
    """Candles from a close series: open = prior close, high/low =
    +/- rng/2 around the open/close extremes. rng=0.0 gives wickless
    candles (lows never dip below the open/close range)."""
    rows = []
    prev = closes[0]
    for cl in closes:
        o = prev
        rows.append([o, max(o, cl) + rng / 2, min(o, cl) - rng / 2, cl])
        prev = cl
    return rows


def flat(n, price=100.0):
    return seg([price] * n, rng=1.0)


def with_ts(rows, t0=T0):
    return [[t0 + 3600 * k, o, h, l, c] for k, (o, h, l, c) in enumerate(rows)]


def names(tags):
    return [t['pattern'] for t in tags]


def invert(rows):
    """Mirror a fixture around 100 (LONG fixture -> SHORT fixture)."""
    return [[200 - o, 200 - l, 200 - h, 200 - c] for o, h, l, c in rows]


def atr_last(rows):
    return pf.atr_series(pf._parse_candles(with_ts(rows)))[-1]


# ---------------------------------------------------------------- guards

def test_detect_needs_40_candles():
    assert pf.detect(with_ts(flat(39))) == []
    assert pf.detect(with_ts(flat(40))) == []  # flat: nothing fires


def test_detect_garbage_never_raises():
    assert pf.detect(None) == []
    assert pf.detect([]) == []
    assert pf.detect('nope') == []
    assert pf.detect([[None, None, None, None, None]] * 50) == []
    assert pf.detect([[1, 2, 3]] * 50) == []  # short rows dropped
    # Mixed good/bad rows: bad ones dropped, good ones still detect.
    rows = with_ts(flat(39) + [[100.0, 100.5, 99.5, 100.0]])
    assert pf.detect(rows) == []


def test_detect_returns_tags_shape():
    rows = with_ts(flat(39) + [[100.2, 101.6, 100.0, 101.4]])  # BOS
    tags = pf.detect(rows)
    assert any(t['pattern'] == 'BOS' for t in tags)
    t = next(t for t in tags if t['pattern'] == 'BOS')
    assert t['direction'] == 'LONG' and t['timeframe'] == '1h'
    assert t['fired_ts'] == T0 + 39 * 3600
    assert isinstance(t['strength'], float)


# ---------------------------------------------------------------- §1a

def test_hammer_fires():
    rows = flat(33) + seg(lin(6, 99.8, 95.0))
    rows.append([95.2, 95.5, 94.4, 95.4])  # long lower wick at the low
    assert 0.5 < atr_last(rows) < 2.0
    assert 'Hammer' in names(pf.detect(with_ts(rows)))


def test_hammer_not_when_prior_green():
    rows = flat(33) + seg(lin(6, 99.8, 95.0))
    # Prior candle green: flip the last descent candle's close above open.
    rows[-1] = [95.0, 95.6, 94.5, 95.5]
    rows.append([95.2, 95.5, 94.4, 95.4])
    assert 'Hammer' not in names(pf.detect(with_ts(rows)))


def test_hammer_not_when_upper_wick_too_big():
    rows = flat(33) + seg(lin(6, 99.8, 95.0))
    rows.append([95.2, 96.5, 94.4, 95.4])  # uw 1.1 > 0.15 * range
    assert 'Hammer' not in names(pf.detect(with_ts(rows)))


def test_hammer_not_on_doji():
    rows = flat(33) + seg(lin(6, 99.8, 95.0))
    rows.append([95.4, 95.5, 94.4, 95.4])  # body 0 — a doji, not a hammer
    assert 'Hammer' not in names(pf.detect(with_ts(rows)))


def test_shooting_star_fires():
    rows = invert(flat(33) + seg(lin(6, 99.8, 95.0)))
    rows.append([104.8, 105.6, 104.6, 104.6])  # long upper wick at the high
    assert 'Shooting Star' in names(pf.detect(with_ts(rows)))


def test_shooting_star_not_when_prior_red():
    rows = invert(flat(33) + seg(lin(6, 99.8, 95.0)))
    rows[-1] = [105.0, 105.6, 104.5, 104.5]  # prior red
    rows.append([104.8, 105.6, 104.6, 104.6])
    assert 'Shooting Star' not in names(pf.detect(with_ts(rows)))


def test_bull_engulfing_fires():
    rows = flat(38)
    rows.append([101.0, 101.2, 100.0, 100.2])   # red, body 0.8
    rows.append([100.0, 101.5, 99.8, 101.2])    # green, engulfs
    assert 'Bullish Engulfing' in names(pf.detect(with_ts(rows)))


def test_bull_engulfing_not_when_prev_body_too_small():
    rows = flat(38)
    rows.append([100.4, 100.6, 100.0, 100.2])   # red, body 0.2 < 0.4 ATR
    rows.append([100.0, 101.5, 99.8, 101.2])
    assert 'Bullish Engulfing' not in names(pf.detect(with_ts(rows)))


def test_bull_engulfing_not_on_open_gap():
    rows = flat(38)
    rows.append([101.0, 101.2, 100.0, 100.2])
    rows.append([100.5, 101.5, 100.4, 101.2])   # opens above prev close
    assert 'Bullish Engulfing' not in names(pf.detect(with_ts(rows)))


def test_bear_engulfing_fires():
    rows = flat(38)
    rows.append([99.0, 100.0, 98.8, 99.8])      # green, body 0.8
    rows.append([100.0, 100.2, 98.5, 98.8])     # red, engulfs
    assert 'Bearish Engulfing' in names(pf.detect(with_ts(rows)))


def test_morning_star_fires():
    rows = flat(37)
    rows.append([101.0, 101.2, 99.6, 99.8])     # long red
    rows.append([99.9, 100.1, 99.7, 99.9])      # small middle
    rows.append([100.0, 101.5, 99.9, 101.2])    # long green, past midpoint
    assert 'Morning Star' in names(pf.detect(with_ts(rows)))


def test_morning_star_not_when_middle_too_big():
    rows = flat(37)
    rows.append([101.0, 101.2, 99.6, 99.8])
    rows.append([99.9, 100.6, 99.4, 100.5])     # middle body 0.6 > 0.3*1.2
    rows.append([100.0, 101.5, 99.9, 101.2])
    assert 'Morning Star' not in names(pf.detect(with_ts(rows)))


def test_morning_star_not_when_third_closes_below_midpoint():
    rows = flat(37)
    rows.append([101.0, 101.2, 99.6, 99.8])
    rows.append([99.9, 100.1, 99.7, 99.9])
    rows.append([100.0, 101.5, 99.9, 100.2])    # closes below 100.4 midpoint
    assert 'Morning Star' not in names(pf.detect(with_ts(rows)))


def test_evening_star_fires():
    rows = flat(37)
    rows.append([99.0, 100.4, 98.8, 100.2])     # long green
    rows.append([100.1, 100.3, 99.9, 100.1])    # small middle
    rows.append([100.0, 100.1, 98.5, 98.8])     # long red, past midpoint
    assert 'Evening Star' in names(pf.detect(with_ts(rows)))


# ---------------------------------------------------------------- §1b

def _double_bottom_rows(confirm_close=99.6, second_low=96.0):
    """Two equal swing lows (95.5) with a 99.0 intervening high."""
    rows = flat(20)
    rows += seg(lin(5, 99.5, 96.0))             # low 1: low 95.5
    rows += seg(lin(17, 96.0, 99.0), rng=0.0)   # rise, lows stay >= 96.0
    rows += seg(lin(5, 99.0, second_low))       # low 2: low 95.5
    rows += seg(lin(4, 96.0, 98.0), rng=0.0)    # hover below the high
    rows.append([98.5, 99.8, 98.4, confirm_close])
    return rows


def test_double_bottom_fires():
    rows = _double_bottom_rows()
    assert len(rows) == 20 + 5 + 17 + 5 + 4 + 1
    assert 'Double Bottom' in names(pf.detect(with_ts(rows)))


def test_double_bottom_not_without_confirmation():
    rows = _double_bottom_rows(confirm_close=98.5)  # below intervening 99.0
    assert 'Double Bottom' not in names(pf.detect(with_ts(rows)))


def test_double_bottom_not_when_lows_too_far_apart():
    # Second low gaps straight down to 93.5 in ONE candle (no
    # intermediate swing low to pair with the first low at 95.5).
    rows = flat(20)
    rows += seg(lin(5, 99.5, 96.0))
    rows += seg(lin(17, 96.0, 99.0), rng=0.0)
    rows.append([99.0, 99.2, 93.5, 94.0])       # gap-down second low
    rows += seg(lin(4, 94.0, 98.0), rng=0.0)
    rows.append([98.5, 99.8, 98.4, 99.6])
    assert 'Double Bottom' not in names(pf.detect(with_ts(rows)))


def test_double_top_fires():
    rows = invert(_double_bottom_rows())
    assert 'Double Top' in names(pf.detect(with_ts(rows)))


def _bull_flag_rows(confirm_close=104.5, cons_rng=1.0):
    rows = flat(26)
    rows += seg(lin(6, 100.5, 103.5))           # pole: +3.5% in 6 candles
    rows += seg([103.3, 103.1, 103.0, 102.9, 102.9, 102.8, 102.8],
                rng=0.4)                         # consolidation, range ~1.1
    rows.append([103.0, 104.6, 102.9, confirm_close])
    return rows


def test_bull_flag_fires():
    assert 'Bullish Flag' in names(pf.detect(with_ts(_bull_flag_rows())))


def test_bull_flag_not_when_consolidation_too_wide():
    # Consolidation range ~3.0 > 40% of the 3.5 pole.
    rows = flat(26)
    rows += seg(lin(6, 100.5, 103.5))
    rows += seg([103.5, 102.0, 103.5, 102.0, 103.5, 102.0, 103.5], rng=1.0)
    rows.append([103.0, 104.6, 102.9, 104.5])
    assert 'Bullish Flag' not in names(pf.detect(with_ts(rows)))


def test_bull_flag_not_without_confirmation():
    rows = _bull_flag_rows(confirm_close=103.2)  # below consolidation high
    assert 'Bullish Flag' not in names(pf.detect(with_ts(rows)))


def test_bear_flag_fires():
    rows = invert(_bull_flag_rows())
    assert 'Bearish Flag' in names(pf.detect(with_ts(rows)))


def _inv_hs_rows(head_close=94.0):
    """Inverse H&S: shoulders ~96.0, head ~94.0, neckline ~98.6."""
    rows = flat(20)
    rows += seg(lin(5, 99.5, 96.0))             # shoulder 1: low 95.5
    rows += seg(lin(10, 96.2, 98.5), rng=0.0)   # rise to H1 ~98.5
    rows += seg(lin(10, 98.3, head_close))      # head: low 93.5
    rows += seg(lin(10, head_close + 0.2, 98.6), rng=0.0)  # recovery
    rows += seg(lin(15, 98.4, 97.6), rng=0.0)   # hover, lows stay high
    rows += seg(lin(5, 97.4, 96.1))             # shoulder 2: low 95.6
    rows += seg(lin(6, 96.3, 98.4), rng=0.0)    # hover below neckline
    rows.append([98.5, 99.5, 98.4, 99.2])       # close above neckline
    return rows


def test_inverse_head_shoulders_fires():
    rows = _inv_hs_rows()
    assert len(rows) >= 40
    assert 'Inverse Head & Shoulders' in names(pf.detect(with_ts(rows)))


def test_inverse_head_shoulders_not_when_head_too_shallow():
    # Gap-candle pivots only (no mid-descent pivots to substitute):
    # head low 95.3 is just 0.2 below the 95.5 shoulder — not >= 0.5x
    # ATR deeper, so no trio qualifies even though the neckline breaks.
    rows = flat(20)
    rows.append([100.0, 100.3, 95.5, 96.0])     # shoulder 1 (gap down)
    rows += seg(lin(10, 96.2, 98.5), rng=0.0)
    rows.append([98.5, 98.7, 95.3, 95.8])       # head (gap down, shallow)
    rows += seg(lin(10, 96.0, 98.6), rng=0.0)
    rows += seg([98.0] * 10, rng=0.0)           # hover
    rows.append([98.0, 98.2, 95.6, 96.1])       # shoulder 2 (gap down)
    rows += seg(lin(6, 96.3, 98.4), rng=0.0)
    rows.append([98.5, 99.5, 98.4, 99.2])       # close above the neckline
    assert 'Inverse Head & Shoulders' not in names(pf.detect(with_ts(rows)))


def test_head_shoulders_fires():
    rows = invert(_inv_hs_rows())
    assert 'Head & Shoulders' in names(pf.detect(with_ts(rows)))


def _triple_bottom_rows():
    """Three equal swing lows (95.5), each pair >= 8 apart."""
    rows = flat(20)
    rows += seg(lin(5, 99.5, 96.0))             # low 1
    rows += seg(lin(17, 96.0, 99.0), rng=0.0)
    rows += seg(lin(5, 99.0, 96.0))             # low 2
    rows += seg(lin(17, 96.0, 99.1), rng=0.0)
    rows += seg(lin(5, 99.0, 96.0))             # low 3
    rows += seg(lin(4, 96.0, 98.0), rng=0.0)
    rows.append([98.5, 99.8, 98.4, 99.6])
    return rows


def test_triple_bottom_fires():
    rows = _triple_bottom_rows()
    assert 'Triple Bottom' in names(pf.detect(with_ts(rows)))


def test_triple_top_fires():
    rows = invert(_triple_bottom_rows())
    assert 'Triple Top' in names(pf.detect(with_ts(rows)))


def test_triple_bottom_not_when_third_touch_off():
    rows = flat(20)
    rows += seg(lin(5, 99.5, 96.0))
    rows += seg(lin(17, 96.0, 99.0), rng=0.0)
    rows += seg(lin(5, 99.0, 96.0))
    rows += seg(lin(17, 96.0, 99.1), rng=0.0)
    rows += seg(lin(5, 99.0, 94.0))             # third low 2.0 off
    rows += seg(lin(4, 94.0, 98.0), rng=0.0)
    rows.append([98.5, 99.8, 98.4, 99.6])
    assert 'Triple Bottom' not in names(pf.detect(with_ts(rows)))


# ---------------------------------------------------------------- §1c

def test_bos_long_fires():
    rows = flat(39)
    rows.append([100.2, 101.6, 100.0, 101.4])   # close past 100.5 + 0.25 ATR
    assert 'BOS' in names(pf.detect(with_ts(rows)))


def test_bos_short_fires():
    rows = flat(39)
    rows.append([99.8, 100.0, 98.4, 98.6])
    tags = pf.detect(with_ts(rows))
    assert any(t['pattern'] == 'BOS' and t['direction'] == 'SHORT'
               for t in tags)


def test_bos_not_when_displacement_body_too_small():
    rows = flat(39)
    rows.append([100.9, 101.6, 100.8, 101.4])   # body 0.5... still >= 0.5 ATR?
    # Tighter: body 0.3 with the same close — displacement too small.
    rows = flat(39)
    rows.append([101.1, 101.6, 101.0, 101.4])
    assert 'BOS' not in names(pf.detect(with_ts(rows)))


def test_bos_not_when_close_barely_past():
    rows = flat(39)
    rows.append([100.2, 101.0, 100.0, 100.6])   # 100.6 < 100.5 + 0.25 ATR
    assert 'BOS' not in names(pf.detect(with_ts(rows)))


def test_fvg_bullish_fires():
    rows = flat(38)
    rows.append([100.0, 100.5, 99.5, 100.0])    # i-2
    rows.append([101.0, 101.5, 100.9, 101.3])   # i-1, stays above the gap
    rows.append([101.4, 102.0, 101.2, 101.8])   # i: low 101.2, gap 0.7
    tags = pf.detect(with_ts(rows))
    assert any(t['pattern'] == 'FVG' and t['direction'] == 'LONG'
               for t in tags)


def test_fvg_bearish_fires():
    rows = flat(38)
    rows.append([100.0, 100.5, 99.5, 100.0])
    rows.append([99.0, 99.1, 98.5, 98.7])
    rows.append([98.6, 98.8, 98.0, 98.2])       # high 98.8, gap 0.7
    tags = pf.detect(with_ts(rows))
    assert any(t['pattern'] == 'FVG' and t['direction'] == 'SHORT'
               for t in tags)


def test_fvg_not_when_gap_too_small():
    rows = flat(38)
    rows.append([100.0, 100.5, 99.5, 100.0])
    rows.append([100.4, 100.9, 100.3, 100.7])
    rows.append([100.8, 101.2, 100.6, 101.0])   # gap 0.1 < 0.25 ATR
    assert 'FVG' not in names(pf.detect(with_ts(rows)))


def test_fvg_not_when_filled():
    rows = flat(38)
    rows.append([100.0, 100.5, 99.5, 100.0])
    rows.append([101.0, 101.5, 100.2, 101.3])   # i-1 dips through the gap
    rows.append([101.4, 102.0, 101.2, 101.8])
    assert 'FVG' not in names(pf.detect(with_ts(rows)))


def test_liquidity_sweep_short_fires():
    rows = flat(39)
    rows.append([100.4, 101.2, 99.8, 100.2])    # pierces 100.5, closes back
    tags = pf.detect(with_ts(rows))
    assert any(t['pattern'] == 'Liquidity Sweep'
               and t['direction'] == 'SHORT' for t in tags)


def test_liquidity_sweep_long_fires():
    rows = flat(39)
    rows.append([99.6, 100.2, 98.8, 99.8])      # pierces 99.5, closes back
    tags = pf.detect(with_ts(rows))
    assert any(t['pattern'] == 'Liquidity Sweep'
               and t['direction'] == 'LONG' for t in tags)


def test_liquidity_sweep_not_when_close_stays_beyond():
    rows = flat(39)
    rows.append([100.4, 101.2, 99.8, 100.8])    # closes above the swing high
    assert 'Liquidity Sweep' not in names(pf.detect(with_ts(rows)))


def _ote_long_rows(fire_close=101.5):
    rows = flat(25)
    rows += seg(lin(8, 100.0, 103.0))           # impulse +3% in 8 candles
    rows += seg(lin(6, 103.0, 101.0))           # retrace toward the zone
    rows.append([101.3, 101.6, 100.5, fire_close])
    return rows


def test_ote_long_fires():
    assert 'OTE' in names(pf.detect(with_ts(_ote_long_rows())))


def test_ote_long_not_without_rejection():
    # Close in the bottom of the firing candle — no rejection candle.
    assert 'OTE' not in names(pf.detect(with_ts(_ote_long_rows(100.6))))


def test_ote_short_fires():
    rows = invert(_ote_long_rows())
    assert 'OTE' in names(pf.detect(with_ts(rows)))


def _order_block_long_rows(retest_early=False):
    rows = flat(34)
    rows.append([101.0, 101.2, 99.6, 99.8])     # order-block candle (red)
    rows.append([100.0, 101.8, 99.9, 101.6])    # BOS up through 100.5
    rows.append([101.3, 101.5, 101.1, 101.4])
    rows.append([101.2, 101.4, 101.1, 101.3])
    if retest_early:
        rows.append([101.0, 101.3, 100.5, 101.1])  # touches the zone early
    rows.append([101.3, 101.5, 101.1, 101.4])
    rows.append([101.2, 101.4, 100.6, 101.3])   # retest: wick into the zone
    return rows


def test_order_block_long_fires():
    rows = _order_block_long_rows()
    assert len(rows) == 40
    assert 'Order Block' in names(pf.detect(with_ts(rows)))


def test_order_block_not_when_already_retested():
    rows = _order_block_long_rows(retest_early=True)
    assert 'Order Block' not in names(pf.detect(with_ts(rows)))


def test_order_block_not_without_retest():
    rows = flat(34)
    rows.append([101.0, 101.2, 99.6, 99.8])
    rows.append([100.0, 101.8, 99.9, 101.6])
    rows.append([101.3, 101.5, 101.1, 101.4])
    rows.append([101.2, 101.4, 101.1, 101.3])
    rows.append([101.3, 101.5, 101.1, 101.4])
    rows.append([101.2, 101.4, 101.1, 101.3])   # never touches the zone
    assert 'Order Block' not in names(pf.detect(with_ts(rows)))


# ---------------------------------------------------------------- confluence

def test_confluence_shared_id():
    # Bullish Engulfing + BOS on the same candle: both LONG, same id,
    # and nothing else (the prior candle's high stays below the swing
    # high so the BOS is genuine; the flat base can't form a triple —
    # its intervening highs sit < 1.0x ATR above the lows).
    rows = flat(38)
    rows.append([100.6, 100.8, 99.8, 100.0])    # red, body 0.6
    rows.append([99.9, 101.6, 99.7, 101.3])     # green, engulfs + breaks out
    tags = pf.detect(with_ts(rows))
    assert {t['pattern'] for t in tags} == {'Bullish Engulfing', 'BOS'}
    by_name = {t['pattern']: t for t in tags}
    cid = by_name['Bullish Engulfing']['confluence_id']
    assert cid is not None and cid == by_name['BOS']['confluence_id']
    assert cid.startswith('conf-1h-LONG-')


def test_single_pattern_has_no_confluence_id():
    rows = flat(39)
    rows.append([100.2, 101.6, 100.0, 101.4])   # BOS alone
    tags = pf.detect(with_ts(rows))
    bos = [t for t in tags if t['pattern'] == 'BOS']
    assert bos and bos[0]['confluence_id'] is None


def test_detect_for_universe_skips_bad_symbol():
    good = with_ts(flat(39) + [[100.2, 101.6, 100.0, 101.4]])
    out = pf.detect_for_universe({'BTC': good, 'BAD': None, 'ETH': []})
    assert list(out) == ['BTC']


# ---------------------------------------------------------------- Kraken fetch

def _kraken_payload(pairs):
    """Fake Kraken OHLC payload: 210 hourly rows per pair + a forming row."""
    result = {}
    for pair in pairs:
        rows = []
        for k in range(210):
            rows.append([T0 + 3600 * k, '100.0', '100.5', '99.5', '100.0',
                         '100.0', '1.0', 10])
        rows.append([T0 + 3600 * 210, '100.0', '100.5', '99.5', '100.0',
                     '100.0', '1.0', 10])  # forming hour — must be dropped
        result[pair] = rows
    return {'error': [], 'result': result}


def test_kraken_pair_map():
    assert pf.kraken_pair('BTC') == 'XBTUSD'
    assert pf.kraken_pair('btc') == 'XBTUSD'
    assert pf.kraken_pair('ETH') == 'ETHUSD'
    assert pf.kraken_pair('SOL') == 'SOLUSD'
    assert pf.kraken_pair('RNDR') == 'RENDERUSD'
    assert pf.kraken_pair('') == ''
    assert pf.kraken_pair(None) == ''


def test_fetch_ohlc_drops_forming_candle_and_caps_200():
    out = pf.fetch_ohlc(lambda url: _kraken_payload(['XBTUSD', 'ETHUSD']),
                        ['BTC', 'ETH'])
    assert set(out) == {'BTC', 'ETH'}
    assert len(out['BTC']) == 200          # last 200 of 210 closed
    assert out['BTC'][-1][0] == T0 + 3600 * 209  # forming row dropped
    assert out['BTC'][0] == [T0 + 3600 * 10, 100.0, 100.5, 99.5, 100.0]


def test_fetch_ohlc_skips_bad_pairs_and_errors():
    def fake(url):
        if 'XBTUSD' in url:
            raise RuntimeError('boom')
        if 'ETHUSD' in url:
            return {'error': ['EGeneral:Invalid arguments'], 'result': {}}
        return _kraken_payload(['SOLUSD'])
    out = pf.fetch_ohlc(fake, ['BTC', 'ETH', 'SOL'])
    assert list(out) == ['SOL']


def test_fetch_ohlc_caps_symbols():
    seen = []
    pf.fetch_ohlc(lambda url: (seen.append(url), _kraken_payload(['XBTUSD']))[1],
                  [f'C{i}' for i in range(40)])
    assert len(seen) == pf.MAX_SYMBOLS_PER_RUN


def test_update_pattern_candles_merges_and_caps():
    prior = {'BTC': [[T0 + 3600 * k, 100.0, 100.5, 99.5, 100.0]
                     for k in range(150)]}
    fetched = {'BTC': [[T0 + 3600 * k, 101.0, 101.5, 100.5, 101.0]
                       for k in range(140, 210)],
               'ETH': [[T0, 50.0, 50.5, 49.5, 50.0]]}
    new = pf.update_pattern_candles(prior, fetched)
    assert len(new['BTC']) == 200
    assert new['BTC'][-1][0] == T0 + 3600 * 209
    # Overlap rows take the fetched values.
    by_ts = {p[0]: p for p in new['BTC']}
    assert by_ts[T0 + 3600 * 145] == [T0 + 3600 * 145, 101.0, 101.5, 100.5, 101.0]
    assert new['ETH'] == fetched['ETH']
    # Inputs not mutated.
    assert len(prior['BTC']) == 150
    # No fetch: prior survives untouched.
    assert pf.update_pattern_candles(prior, {}) == prior
    assert pf.update_pattern_candles(None, None) == {}
