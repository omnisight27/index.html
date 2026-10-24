#!/usr/bin/env python3
"""SAM.gov agency-name extraction (Contract Intel feed).

The opportunities feed used to print agency '?' for every row because it
read officeAddress.agency, a field the SAM.gov v2 API does not return.
The hierarchy lives in fullParentPathName instead. These tests pin the
fallback chain so a future API shape change degrades to '?' instead of
crashing the ingest.

Run: python3 -m pytest Scripts/test_sam_agency.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nexintel


def test_full_parent_path_first_segment():
    o = {'fullParentPathName': 'DEPT OF DEFENSE.DEPT OF THE NAVY.NAVAL SEA SYSTEMS COMMAND',
         'title': 'Gasket Sets'}
    assert nexintel.sam_agency(o) == 'Dept Of Defense'


def test_department_fallback():
    o = {'department': 'DEPT OF HOMELAND SECURITY', 'office': 'USCG HQ'}
    assert nexintel.sam_agency(o) == 'DEPT OF HOMELAND SECURITY'


def test_office_fallback():
    assert nexintel.sam_agency({'office': 'VA MEDICAL CENTER'}) == 'VA MEDICAL CENTER'


def test_unknown_is_question_mark_not_crash():
    assert nexintel.sam_agency({}) == '?'
    assert nexintel.sam_agency({'fullParentPathName': ''}) == '?'
    assert nexintel.sam_agency({'officeAddress': {'city': 'Washington'}}) == '?'
