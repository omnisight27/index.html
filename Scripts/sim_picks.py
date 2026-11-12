"""Venue-blind sim gate (paper sims ONLY — Nov 12, Nic: 'Do both').

Reads the engine's published data files (data/latest.json for swing
picks, data/intraday.json for intraday signals and movers) and lists the
picks that qualify under a sim's buy gate with the venue requirement
REMOVED: any pick clearing its bar with a complete 4:1 plan paper-fills
at its listed price, venue ignored. The pick's `venues` field (widened
list: coinbase/cryptocom/kraken/okx) is reported for each trade so the
final report shows where it would have executed; an empty venues list
means NO verified venue — the fill is still a paper fill, and the report
says so.

Gate rules (from the Week 2 ledgers, venue-blind):
- Swing longs (crypto only): confidence >= bar; >= 4/5 timeframe
  agreement; complete plan (entry/target/stop/exit); no news flag.
- Intraday longs: confidence >= 70% (both sims); complete plan; no
  news flag.
- Movers (long-only): complete plan; chg_1h > 0; +15% <= chg_24h < +20%
  (never chase); no news flag.
- ALL kinds: the is_feed_artifact guard MUST still apply — a pick whose
  24h move exceeds 500% or 7d move exceeds 1,000% is skipped, always.
  The guard reads the move legs the published data carries (swing:
  tf_vector; intraday long: chg_24h/chg_7d; mover: chg_24h/chg_7d).
  When NO move leg is published for a pick, the pick is skipped and the
  reason says the artifact check could not run (fail closed).

This module never touches the live gates (decide.py, intraday-sms.py),
never opens a trade, and never writes a ledger — it only reports what
the new gate would open, so the noon workers and the dry-run can use it.

Usage:
    python3 Scripts/sim_picks.py --latest data/latest.json \
        --intraday data/intraday.json --bar 60
Prints JSON: {"bar", "generated", "qualifying": [...], "skipped": [...]}.
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from nexintel import is_feed_artifact
except Exception:  # pragma: no cover - mirror of nexintel.is_feed_artifact
    def is_feed_artifact(chg24, chg7d):
        return abs(chg24 or 0) > 500 or abs(chg7d or 0) > 1000

INTRADAY_MIN_CONF = 70
MOVER_MIN_24H = 15
MOVER_MAX_24H = 20


def _tf_agree(tf_align):
    """Parse '4/5 timeframes agree' / '4/5' / 4 -> 4. None if unparseable."""
    if tf_align is None:
        return None
    if isinstance(tf_align, (int, float)):
        return int(tf_align)
    m = re.search(r'(\d+)\s*/\s*5', str(tf_align))
    return int(m.group(1)) if m else None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _plan_complete(plan):
    """A plan is complete when it has numeric entry, take_profit,
    stop_loss, and an exit (exit_by or projected_exit)."""
    if not isinstance(plan, dict):
        return False
    if _num(plan.get('take_profit')) is None:
        return False
    if _num(plan.get('stop_loss')) is None:
        return False
    # Entry: numeric entry (intraday/mover plans) or entry_date (swing
    # theses, where the numeric entry is the pick's price).
    if _num(plan.get('entry')) is None and not plan.get('entry_date'):
        return False
    return bool(plan.get('exit_by') or plan.get('projected_exit'))


def _venue_note(venues):
    if venues:
        return 'verified on ' + ', '.join(venues)
    return 'NO verified venue — venue-blind paper fill at listed price'


def _qualify(asset, kind, confidence, bar, entry, plan, tf_align, venues,
             news_flag):
    return {
        'asset': asset,
        'kind': kind,
        'confidence': confidence,
        'bar': bar,
        'price': entry,
        'entry': (_num(plan.get('entry')) if plan else None) or entry,
        'take_profit': _num(plan.get('take_profit')) if plan else None,
        'stop_loss': _num(plan.get('stop_loss')) if plan else None,
        'exit_by': (plan or {}).get('exit_by') or (plan or {}).get('projected_exit'),
        'tf_align': tf_align,
        'venues': list(venues or []),
        'venue_note': _venue_note(venues),
        'news_flag': bool(news_flag),
    }


def _skip(asset, kind, reason):
    return {'asset': asset, 'kind': kind, 'reason': reason}


def swing_picks(latest, bar):
    """Qualifying swing longs under the venue-blind gate."""
    qualifying, skipped = [], []
    for p in (latest or {}).get('longs') or []:
        asset = p.get('asset') or '?'
        if (p.get('market') or 'crypto') != 'crypto':
            skipped.append(_skip(asset, 'swing',
                                 f"market '{p.get('market')}' — sim is crypto-only"))
            continue
        conf = p.get('confidence')
        if conf is None or conf < bar:
            skipped.append(_skip(asset, 'swing',
                                 f'confidence {conf} below the {bar}% bar'))
            continue
        tf = _tf_agree(p.get('tf_align'))
        if tf is None or tf < 4:
            skipped.append(_skip(asset, 'swing',
                                 f"timeframe agreement '{p.get('tf_align')}' below 4/5"))
            continue
        if p.get('news_flag'):
            skipped.append(_skip(asset, 'swing', 'news flag'))
            continue
        thesis = p.get('thesis') or {}
        if (thesis.get('direction') or 'LONG') != 'LONG':
            skipped.append(_skip(asset, 'swing', 'not a LONG'))
            continue
        if not _plan_complete(thesis):
            skipped.append(_skip(asset, 'swing', 'incomplete plan'))
            continue
        tv = p.get('tf_vector') or []
        chg24 = tv[1] if len(tv) > 1 else p.get('chg24')
        chg7d = tv[2] if len(tv) > 2 else p.get('chg7d')
        if chg24 is None and chg7d is None:
            skipped.append(_skip(asset, 'swing',
                                 'no 24h/7d move published — artifact check '
                                 'could not run (fail closed)'))
            continue
        if is_feed_artifact(chg24, chg7d):
            skipped.append(_skip(asset, 'swing',
                                 f'feed artifact: 24h {chg24}%, 7d {chg7d}%'))
            continue
        entry = _num(p.get('price')) or _num(thesis.get('entry'))
        qualifying.append(_qualify(asset, 'swing', conf, bar, entry, thesis,
                                   p.get('tf_align'), p.get('venues'),
                                   p.get('news_flag')))
    return qualifying, skipped


def intraday_picks(intraday, bar):
    """Qualifying intraday longs under the venue-blind gate. The bar does
    not apply here: intraday longs keep the 70% floor in both sims."""
    qualifying, skipped = [], []
    for s in (intraday or {}).get('longs') or []:
        asset = s.get('asset') or '?'
        conf = s.get('confidence')
        if conf is None or conf < INTRADAY_MIN_CONF:
            skipped.append(_skip(asset, 'intraday',
                                 f'confidence {conf} below the 70% intraday floor'))
            continue
        if (s.get('direction') or 'LONG') != 'LONG':
            skipped.append(_skip(asset, 'intraday', 'not a LONG'))
            continue
        if s.get('news_flag'):
            skipped.append(_skip(asset, 'intraday', 'news flag'))
            continue
        thesis = s.get('thesis') or {}
        if not _plan_complete(thesis):
            skipped.append(_skip(asset, 'intraday', 'incomplete plan'))
            continue
        chg24 = s.get('chg_24h')
        chg7d = s.get('chg_7d')
        if chg24 is None and chg7d is None:
            skipped.append(_skip(asset, 'intraday',
                                 'no 24h/7d move published — artifact check '
                                 'could not run (fail closed)'))
            continue
        if is_feed_artifact(chg24, chg7d):
            skipped.append(_skip(asset, 'intraday',
                                 f'feed artifact: 24h {chg24}%, 7d {chg7d}%'))
            continue
        entry = _num(thesis.get('entry')) or _num(s.get('price'))
        qualifying.append(_qualify(asset, 'intraday', conf, bar, entry,
                                   thesis, None, s.get('venues'),
                                   s.get('news_flag')))
    return qualifying, skipped


def mover_picks(intraday, bar):
    """Qualifying UP movers under the venue-blind gate (long-only,
    never chase: +15% <= 24h < +20%)."""
    qualifying, skipped = [], []
    for m in (intraday or {}).get('movers') or []:
        asset = m.get('asset') or '?'
        if str(m.get('direction') or '').upper() not in ('UP', 'LONG'):
            skipped.append(_skip(asset, 'mover',
                                 f"direction {m.get('direction')} — long-only"))
            continue
        if m.get('news_flag'):
            skipped.append(_skip(asset, 'mover', 'news flag'))
            continue
        plan = m.get('plan') or {}
        if not _plan_complete(plan):
            skipped.append(_skip(asset, 'mover', 'no complete plan'))
            continue
        chg1 = _num(m.get('chg_1h'))
        if chg1 is None or chg1 <= 0:
            skipped.append(_skip(asset, 'mover',
                                 f'1h change {m.get("chg_1h")}% — fails 1h > 0'))
            continue
        chg24 = _num(m.get('chg_24h'))
        if chg24 is None or not (MOVER_MIN_24H <= chg24 < MOVER_MAX_24H):
            skipped.append(_skip(asset, 'mover',
                                 f'24h change {m.get("chg_24h")}% — outside '
                                 f'+{MOVER_MIN_24H}% to <+{MOVER_MAX_24H}% '
                                 '(never chase)'))
            continue
        chg7d = _num(m.get('chg_7d'))
        if is_feed_artifact(chg24, chg7d):
            skipped.append(_skip(asset, 'mover',
                                 f'feed artifact: 24h {chg24}%, 7d {chg7d}%'))
            continue
        entry = _num(plan.get('entry')) or _num(m.get('price'))
        qualifying.append(_qualify(asset, 'mover', None, bar, entry, plan,
                                   None, m.get('venues'), m.get('news_flag')))
    return qualifying, skipped


def qualifying_picks(latest, intraday, bar):
    """All qualifying picks across the three kinds, plus the skips."""
    qualifying, skipped = [], []
    for fn, data in ((swing_picks, latest), (intraday_picks, intraday),
                     (mover_picks, intraday)):
        q, s = fn(data, bar)
        qualifying.extend(q)
        skipped.extend(s)
    return qualifying, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description='Venue-blind sim gate (paper only).')
    ap.add_argument('--latest', required=True, help='data/latest.json')
    ap.add_argument('--intraday', required=True, help='data/intraday.json')
    ap.add_argument('--bar', required=True, type=int,
                    help='swing confidence bar (60 main, 40 OPEN)')
    args = ap.parse_args(argv)
    with open(args.latest) as f:
        latest = json.load(f)
    with open(args.intraday) as f:
        intraday = json.load(f)
    qualifying, skipped = qualifying_picks(latest, intraday, args.bar)
    print(json.dumps({
        'bar': args.bar,
        'generated': {
            'latest': latest.get('generated'),
            'intraday': intraday.get('generated'),
        },
        'qualifying': qualifying,
        'skipped': skipped,
    }, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
