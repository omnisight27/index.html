#!/usr/bin/env python3
"""Check-mode guard: `python3 Scripts/nexintel.py --check` must run the full
pipeline without writing to the repo's data dir.

Regression test for the 2026-11-02 daily review: a plain local run of the
engine silently overwrote data/latest.json, data/history.json, kalshi.json
and added a snapshot — all artifacts the scheduled GitHub Actions loop owns.
--check redirects every file write to a throwaway temp dir.

Run: python3 -m pytest Scripts/test_check_mode.py
"""
import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATA = os.path.join(REPO, 'data')

WATCHED = ('latest.json', 'history.json', 'intraday.json', 'kalshi.json',
           'sam_pulse.json', 'sam_nonprofit.json', 'foresight.json')


def _digest(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def test_check_mode_writes_nothing_to_repo_data():
    before = {fn: _digest(os.path.join(DATA, fn))
              for fn in WATCHED if os.path.exists(os.path.join(DATA, fn))}
    snaps_before = set(os.listdir(os.path.join(DATA, 'snapshots')))
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, 'nexintel.py'), '--check'],
        capture_output=True, text=True, timeout=600, cwd=REPO)
    assert proc.returncode == 0, f'engine failed in --check mode:\n{proc.stderr[-2000:]}'
    assert 'CHECK MODE' in proc.stdout
    for fn, digest in before.items():
        assert _digest(os.path.join(DATA, fn)) == digest, \
            f'--check mode modified data/{fn}'
    assert set(os.listdir(os.path.join(DATA, 'snapshots'))) == snaps_before, \
        '--check mode added a snapshot'
