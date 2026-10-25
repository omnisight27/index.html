"""Tests for the nonprofit contract watch (2026-10-25): SAM.gov
opportunities filtered to what a 501(c)(3) like NicNacs can pursue.
The API takes one NAICS code per request and a non-associated key gets
~10 requests/day, so the watch rotates one code per run and merges
into a persistent ledger."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nexintel


def _opp(title, naics, nid, deadline='2999-06-01T00:00:00Z', state='OR'):
    return {
        'title': title,
        'naicsCode': naics,
        'noticeId': nid,
        'fullParentPathName': 'DEPT OF AGRICULTURE.FOOD AND NUTRITION SERVICE',
        'type': {'value': 'Solicitation'},
        'postedDate': '2026-10-20',
        'responseDeadLine': deadline,
        'placeOfPerformance': {'state': {'code': state}},
    }


def test_nonprofit_match_naics():
    m = nexintel.nonprofit_match('Janitorial services', '453310')
    assert m and 'NAICS 453310' in m and 'Used merchandise' in m, m


def test_nonprofit_match_keyword():
    m = nexintel.nonprofit_match('Community food bank operations', '541512')
    assert m and 'keyword: community' in m, m


def test_nonprofit_match_none():
    assert nexintel.nonprofit_match('F-35 fighter jet maintenance', '336411') is None


def test_nonprofit_match_case_insensitive():
    m = nexintel.nonprofit_match('NONPROFIT capacity building', '000000')
    assert m and 'keyword: nonprofit' in m, m


def test_sam_record():
    r = nexintel._sam_record(_opp('Used goods resale services', '453310', 'N1'))
    assert r['agency'] == 'Dept Of Agriculture'
    assert r['state'] == 'OR'
    assert r['notice_id'] == 'N1'
    assert r['nonprofit'] is True
    assert 'NAICS 453310' in (r['nonprofit_match'] or '')


def test_sam_record_defense():
    r = nexintel._sam_record(_opp('F-35 fighter jet maintenance', '336411', 'N9'))
    assert r['nonprofit'] is False
    assert r['nonprofit_match'] is None


def test_past_deadline():
    assert nexintel._past_deadline('2020-01-01') is True
    assert nexintel._past_deadline('2999-01-01') is False
    assert nexintel._past_deadline('garbage') is False
    assert nexintel._past_deadline('?') is False


def test_collect_rotates_and_merges():
    saved_fetch = nexintel.fetch_json
    saved_key = os.environ.get('SAM_GOV_API_KEY')
    os.environ['SAM_GOV_API_KEY'] = 'test-key'
    tmp = tempfile.mkdtemp()
    ledger = os.path.join(tmp, 'sam_nonprofit.json')

    def fake_fetch(url, **kw):
        if 'ncode=453310' in url:
            return {'opportunitiesData': [
                _opp('Used goods resale services', '453310', 'N1'),
                _opp('Old thrift contract', '453310', 'N0',
                     deadline='2020-01-01T00:00:00Z'),
            ]}
        if 'ncode=562920' in url:
            return {'opportunitiesData': [
                _opp('Materials recovery facility operations', '562920', 'N2'),
            ]}
        return {'opportunitiesData': []}

    try:
        nexintel.fetch_json = fake_fetch
        # Run 1: first code (453310). The past-deadline row is dropped.
        rows = nexintel.collect_nonprofit_opportunities(ledger)
        assert [r['notice_id'] for r in rows] == ['N1'], rows
        saved = json.load(open(ledger))
        assert saved['next_index'] == 1, saved
        # Run 2: next code (562920) merges in alongside.
        rows = nexintel.collect_nonprofit_opportunities(ledger)
        assert sorted(r['notice_id'] for r in rows) == ['N1', 'N2'], rows
        # Run 3: force a re-query of 453310 — old rows for that code are
        # replaced, not duplicated. (Reload the ledger from disk first:
        # run 2 rewrote it after the earlier read.)
        saved = json.load(open(ledger))
        saved['next_index'] = 0
        json.dump(saved, open(ledger, 'w'))

        def fake_fetch2(url, **kw):
            if 'ncode=453310' in url:
                return {'opportunitiesData': [
                    _opp('New used goods contract', '453310', 'N3'),
                ]}
            return fake_fetch(url, **kw)
        nexintel.fetch_json = fake_fetch2
        rows = nexintel.collect_nonprofit_opportunities(ledger)
        assert sorted(r['notice_id'] for r in rows) == ['N2', 'N3'], rows
        # A failed fetch keeps the old ledger and never raises.
        nexintel.fetch_json = lambda url, **kw: (_ for _ in ()).throw(
            RuntimeError('boom'))
        rows = nexintel.collect_nonprofit_opportunities(ledger)
        assert sorted(r['notice_id'] for r in rows) == ['N2', 'N3'], rows
    finally:
        nexintel.fetch_json = saved_fetch
        if saved_key is None:
            os.environ.pop('SAM_GOV_API_KEY', None)
        else:
            os.environ['SAM_GOV_API_KEY'] = saved_key


def test_collect_no_key():
    saved_key = os.environ.pop('SAM_GOV_API_KEY', None)
    try:
        tmp = tempfile.mkdtemp()
        rows = nexintel.collect_nonprofit_opportunities(
            os.path.join(tmp, 'sam_nonprofit.json'))
        assert rows == [], rows
    finally:
        if saved_key is not None:
            os.environ['SAM_GOV_API_KEY'] = saved_key


if __name__ == '__main__':
    for name, fn in sorted(list(globals().items())):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'{name}: PASS')
    print('all nonprofit watch tests pass')
