"""Pure-function tests for Scripts/kalshi.py (no network)."""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kalshi as k


def test_mid_price():
    assert k.mid_price(0.60, 0.64) == 0.62
    assert k.mid_price(None, None, 0.41) == 0.41
    assert k.mid_price(None, 0.30) == 0.30
    assert k.mid_price(None, None, None) is None


def test_prob_above_at_the_money():
    # Spot == strike, sigma 0.5, 1y: d2 = -0.25 -> N(-0.25) ~= 0.4013
    p = k.prob_above(100.0, 100.0, 0.5, 1.0)
    assert abs(p - 0.4013) < 0.005, p


def test_prob_above_monotonic():
    lo = k.prob_above(100.0, 150.0, 0.6, 1.0)
    hi = k.prob_above(100.0, 50.0, 0.6, 1.0)
    assert 0 < lo < 0.5 < hi < 1


def test_prob_touch_ge_close():
    touch = k.prob_touch_above(100.0, 150.0, 0.6, 1.0)
    close = k.prob_above(100.0, 150.0, 0.6, 1.0)
    assert touch > close
    assert k.prob_touch_above(160.0, 150.0, 0.6, 1.0) == 1.0


def test_brier():
    assert k.brier(1.0, 1) == 0.0
    assert k.brier(0.0, 1) == 1.0
    assert abs(k.brier(0.7, 1) - 0.09) < 1e-9


def test_fee_estimate():
    # 100 contracts at 0.50: ceil(0.07*100*0.25 *100)/100 = $1.75
    assert k.fee_estimate(100, 0.50) == 1.75
    assert k.fee_estimate(0, 0.5) == 0.0
    assert k.fee_estimate(100, 1.0) == 0.0


def test_grade_paper():
    rec = {'side': 'yes', 'contracts': 600.0, 'fee_est': 10.5}
    g = k.grade_paper(rec, True)
    assert g['win'] is True and g['pnl'] == 600.0 - 300.0 - 10.5
    g = k.grade_paper(rec, False)
    assert g['win'] is False and g['pnl'] == -310.5
    rec_no = {'side': 'no', 'contracts': 500.0, 'fee_est': 8.75}
    assert k.grade_paper(rec_no, False)['win'] is True
    assert k.grade_paper(rec_no, True)['win'] is False


def test_model_prob_between():
    m = {'strike_type': 'between', 'floor_strike': 90.0, 'cap_strike': 110.0,
         'expected_expiration_time': '2027-10-21T00:00:00Z',
         '_series': 'KXBTCD'}
    from datetime import datetime, timezone
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    p = k.model_prob(m, 100.0, 0.5, now)
    # Hand-checked: N(d2(90)) - N(d2(110)) = 0.4843 - 0.3297 = 0.1546
    assert p is not None and abs(p - 0.1546) < 0.005, p


def test_missed_market_class_grade():
    yes = {'status': 'settled', 'result': 'yes'}
    no = {'status': 'settled', 'result': 'No'}
    assert k._missed_market_class(yes, True, False) == 'grade'
    assert k._missed_market_class(no, True, False) == 'grade'


def test_missed_market_class_alive():
    # Still an active, unsettled market that fell out of the listing cut:
    # keep watching, never count a miss.
    live = {'status': 'active', 'result': '', 'yes_bid_dollars': 0.60,
            'yes_ask_dollars': 0.64}
    assert k._missed_market_class(live, True, False) == 'alive'


def test_missed_market_class_dead():
    # 404 from the per-ticker GET: delisted -> count a miss.
    assert k._missed_market_class({}, False, True) == 'dead'
    # Closed/settled with no posted result: dead too.
    closed = {'status': 'closed', 'result': ''}
    assert k._missed_market_class(closed, True, False) == 'dead'
    # Clean fetch, empty market: dead.
    assert k._missed_market_class({}, True, False) == 'dead'


def test_missed_market_class_unknown():
    # Timeout / 500 / rate limit: do NOT touch the miss counter.
    assert k._missed_market_class({}, False, False) == 'unknown'


def test_refresh_watch_rec():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    m = {'yes_bid_dollars': 0.60, 'yes_ask_dollars': 0.64,
         'last_price_dollars': 0.62, 'volume_fp': 1000.0,
         'close_time': '2026-10-28T17:59:00Z'}
    rec = {'ticker': 'T', 'snapshots': [{'ts': '2026-10-21T23:00:00+00:00',
                                         'mid': 0.50}]}
    mid = k._refresh_watch_rec(rec, m, now)
    assert mid == 0.62, mid
    assert rec['mid'] == 0.62
    assert rec['yes_bid'] == 0.60 and rec['yes_ask'] == 0.64
    assert rec['volume'] == 1000.0
    assert rec['close_time'] == '2026-10-28T17:59:00Z'
    assert rec['updated'] == now.isoformat()
    assert len(rec['snapshots']) == 2
    assert rec['snapshots'][-1] == {'ts': now.isoformat(), 'mid': 0.62}
    # Unchanged mid: no new snapshot.
    k._refresh_watch_rec(rec, m, now)
    assert len(rec['snapshots']) == 2
    # Snapshot cap respected.
    rec['snapshots'] = [{'ts': 'x', 'mid': 0.0}] * 200
    k._refresh_watch_rec(rec, {**m, 'yes_bid_dollars': 0.70,
                               'yes_ask_dollars': 0.72}, now)
    assert len(rec['snapshots']) == 120  # SNAPSHOT_CAP
    assert rec['snapshots'][-1]['mid'] == 0.71


def test_refresh_watch_rec_no_mid():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    rec = {'ticker': 'T', 'mid': 0.50, 'snapshots': []}
    assert k._refresh_watch_rec(rec, {}, now) is None
    assert rec['mid'] == 0.50  # untouched
    assert rec['snapshots'] == []


if __name__ == '__main__':
    fns = [v for n, v in sorted(globals().items()) if n.startswith('test_')]
    for fn in fns:
        fn()
        print('PASS', fn.__name__)
    print(f'{len(fns)}/{len(fns)} kalshi tests passed')
