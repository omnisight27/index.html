#!/usr/bin/env python3
"""Plain-language guard for published text (Nic's site rule, 2026-10-21).

The regime is ALWAYS displayed as "🟢 Conditions favor buying" /
"🔴 Conditions favor caution" / "🟡 Mixed conditions". The RISK_ON /
RISK_OFF tokens are internal keys only — the hyphenated phrases
'risk-on' / 'risk-off' must never appear in a published string
(pick theses, alert reasons, explanations). These tests scan the
string constants of both engines and exercise the string builders
directly, so a future edit can't quietly reintroduce the jargon.

Run: python3 -m pytest Scripts/test_nexintel_language.py
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nexintel

HERE = os.path.dirname(os.path.abspath(__file__))
BANNED = ('risk-on', 'risk-off')


def _string_constants(path):
    tree = ast.parse(open(path).read())
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def test_no_banned_phrase_in_any_engine_string():
    for fn in ('nexintel.py', 'intraday.py'):
        for s in _string_constants(os.path.join(HERE, fn)):
            low = s.lower()
            for b in BANNED:
                assert b not in low, f'{fn}: banned phrase in string: {s[:90]!r}'


def test_thesis_regime_lines_are_plain():
    bull = nexintel.build_thesis('BTC', 1.0, 1.0, 100.0, 5.0, 70,
                                 'RISK_ON', 50, 0.0, 'crypto')
    assert 'conditions favor buying, which helps longs' in bull['bull_case']
    bear = nexintel.build_thesis('BTC', -1.0, -1.0, 100.0, -5.0, 70,
                                 'RISK_OFF', 50, 0.0, 'crypto')
    assert 'conditions favor caution, which helps shorts' in bear['bear_case']


def test_detect_regime_reasons_are_plain():
    # Gold bid + strong USD both fire -> both glosses must be plain.
    regime, reasons, _, _ = nexintel.detect_regime(50, 4200.0, 160.0)
    assert regime == 'RISK_OFF'  # internal token unchanged
    joined = ' '.join(reasons)
    assert 'safe-haven demand' in joined
    assert 'dollar strength' in joined
    for b in BANNED:
        assert b not in joined.lower()


def test_regime_adjustment_notes_are_plain():
    for token in ('RISK_ON', 'RISK_OFF', 'NEUTRAL'):
        note = nexintel.regime_adjustment(token)['note'].lower()
        for b in BANNED:
            assert b not in note


def test_regime_labels_are_the_display_strings():
    assert nexintel.REGIME_LABELS['RISK_ON'] == '🟢 Conditions favor buying'
    assert nexintel.REGIME_LABELS['RISK_OFF'] == '🔴 Conditions favor caution'
    assert nexintel.REGIME_LABELS['NEUTRAL'] == '🟡 Mixed conditions'
