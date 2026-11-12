"""Tests for the venue-blind sim gate (Nov 12, Nic: 'Do both').

The sims paper-trade any pick clearing its bar with a complete 4:1
plan, venue ignored; the pick's `venues` field is reported, not gated.
These tests pin the gate on synthetic picks shaped exactly like the
published data: HASH/OZO (Nov 12 noon) qualify at 40 but not 60, QFI
(a +76,833% artifact at 78%) is skipped at BOTH bars, and the mover
chase cap, intraday 70% floor, news flag, and fail-closed artifact
check all still bind.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import nexintel
import sim_picks


def _swing(asset, conf, tf, chg24, chg7d, market='crypto', news=None,
           complete=True, venues=None):
    thesis = {'direction': 'LONG', 'entry_date': '2026-11-12',
              'projected_exit': '2026-11-15', 'stop_loss': 0.19,
              'take_profit': 0.33}
    if not complete:
        thesis.pop('take_profit')
    return {'asset': asset, 'confidence': conf, 'market': market,
            'tf_align': tf, 'tf_vector': [0.1, chg24, chg7d, chg7d, chg7d],
            'thesis': thesis, 'price': 0.22, 'news_flag': news,
            'tradable': [], 'venues': venues if venues is not None else []}


def _intraday_long(asset, conf, chg24=12.0, chg7d=20.0, complete=True):
    thesis = {'direction': 'LONG', 'entry': 10.0, 'take_profit': 11.0,
              'stop_loss': 9.5, 'exit_by': '14:00 UTC'}
    if not complete:
        thesis.pop('stop_loss')
    d = {'asset': asset, 'confidence': conf, 'direction': 'LONG',
         'thesis': thesis, 'tradable': [], 'venues': []}
    if chg24 is not None:
        d['chg_24h'] = chg24
    if chg7d is not None:
        d['chg_7d'] = chg7d
    return d


def _mover(asset, direction='UP', chg1=2.0, chg24=17.5, chg7d=20.0,
           plan=True):
    return {'asset': asset, 'direction': direction, 'chg_1h': chg1,
            'chg_24h': chg24, 'chg_7d': chg7d,
            'plan': ({'entry': 1.0, 'take_profit': 1.2, 'stop_loss': 0.95,
                      'exit_by': '14:00 UTC'} if plan else None),
            'tradable': [], 'venues': []}


def _run(latest_longs, intraday_longs=(), movers=(), bar=40):
    latest = {'generated': '2026-11-12T14:17:50Z', 'longs': list(latest_longs)}
    intraday = {'generated': '2026-11-12T11:01:08Z',
                'longs': list(intraday_longs), 'movers': list(movers)}
    return sim_picks.qualifying_picks(latest, intraday, bar)


def test_noon_hash_and_ozo_qualify_at_40_not_60():
    longs = [_swing('HASH', 47, '4/5 timeframes agree', 8.1, 26.2),
             _swing('OZO', 42, '5/5 timeframes agree', 71.4, 71.3)]
    q, s = _run(longs, bar=40)
    assert [p['asset'] for p in q] == ['HASH', 'OZO']
    assert q[0]['entry'] == 0.22 and q[0]['take_profit'] == 0.33
    # No verified venue for either — venue-blind fill, reported as such.
    assert q[0]['venues'] == []
    assert 'NO verified venue' in q[0]['venue_note']
    q60, s60 = _run(longs, bar=60)
    assert q60 == []
    assert {x['asset'] for x in s60} == {'HASH', 'OZO'}


def test_qfi_artifact_skipped_at_both_bars():
    qfi = _swing('QFI', 78, '4/5 timeframes agree', 76833.7, 86837.2)
    for bar in (40, 60):
        q, s = _run([qfi], bar=bar)
        assert q == []
        assert len(s) == 1 and 'artifact' in s[0]['reason']


def test_swing_gate_reasons():
    q, s = _run([
        _swing('ORCL', 41, None, 1.0, 2.0, market='stock'),
        _swing('YELLOW', 47, '2/5 timeframes agree', 5.0, 9.0),
        _swing('NEWSY', 50, '5/5 timeframes agree', 5.0, 9.0, news='bad news'),
        _swing('NOPLAN', 50, '5/5 timeframes agree', 5.0, 9.0, complete=False),
    ], bar=40)
    assert q == []
    reasons = {x['asset']: x['reason'] for x in s}
    assert 'crypto-only' in reasons['ORCL']
    assert 'below 4/5' in reasons['YELLOW']
    assert 'news flag' in reasons['NEWSY']
    assert 'incomplete plan' in reasons['NOPLAN']


def test_swing_without_move_data_fails_closed():
    p = _swing('GHOST', 50, '5/5 timeframes agree', 5.0, 9.0)
    p.pop('tf_vector')
    q, s = _run([p], bar=40)
    assert q == []
    assert 'fail closed' in s[0]['reason']


def test_venues_reported_when_present():
    p = _swing('BTC', 65, '5/5 timeframes agree', 3.0, 9.0,
               venues=['coinbase', 'kraken'])
    q, _ = _run([p], bar=60)
    assert q[0]['venues'] == ['coinbase', 'kraken']
    assert q[0]['venue_note'] == 'verified on coinbase, kraken'


def test_intraday_long_keeps_70_floor_at_both_bars():
    good = _intraday_long('AAA', 72)
    weak = _intraday_long('BBB', 65)
    for bar in (40, 60):
        q, s = _run([], [good, weak], bar=bar)
        assert [p['asset'] for p in q] == ['AAA']
        assert any(x['asset'] == 'BBB' and '70%' in x['reason'] for x in s)


def test_intraday_long_artifact_and_missing_move():
    q, s = _run([], [_intraday_long('ART', 80, chg24=600.0),
                     _intraday_long('NOMOVE', 80, chg24=None, chg7d=None)],
                bar=40)
    assert q == []
    reasons = {x['asset']: x['reason'] for x in s}
    assert 'artifact' in reasons['ART']
    assert 'fail closed' in reasons['NOMOVE']


def test_mover_qualifies_in_band_only():
    good = _mover('M1', chg24=17.5)
    chase = _mover('OZO', chg24=71.57)
    low = _mover('XCN', chg24=12.96)
    down = _mover('PEPE', direction='DOWN')
    noplan = _mover('NOP', plan=False)
    flat1h = _mover('FLAT', chg1=0.0)
    q, s = _run([], [], [good, chase, low, down, noplan, flat1h], bar=40)
    assert [p['asset'] for p in q] == ['M1']
    reasons = {x['asset']: x['reason'] for x in s}
    assert 'never chase' in reasons['OZO']
    assert 'never chase' in reasons['XCN']
    assert 'long-only' in reasons['PEPE']
    assert 'no complete plan' in reasons['NOP']
    assert 'fails 1h > 0' in reasons['FLAT']


def test_mover_artifact_in_band_is_skipped():
    # +17.5% today but +1,500% in 7 days: inside the chase band, but the
    # artifact guard still catches it via the 7d leg.
    q, s = _run([], [], [_mover('ART7', chg24=17.5, chg7d=1500.0)], bar=40)
    assert q == []
    assert 'artifact' in s[0]['reason']


def test_guard_agrees_with_nexintel_on_boundaries():
    for chg24, chg7d in ((500, 0), (501, 0), (0, 1000), (0, 1001),
                         (None, None), (-600, 0), (17.5, 1500)):
        assert sim_picks.is_feed_artifact(chg24, chg7d) == \
            nexintel.is_feed_artifact(chg24, chg7d)


def test_tf_agree_parsing():
    assert sim_picks._tf_agree('4/5 timeframes agree') == 4
    assert sim_picks._tf_agree('5/5') == 5
    assert sim_picks._tf_agree(4) == 4
    assert sim_picks._tf_agree(None) is None
    assert sim_picks._tf_agree('no data') is None
