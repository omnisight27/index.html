#!/usr/bin/env python3
"""Check-mode guard: `python3 Scripts/intraday.py --check` must run the full
pipeline without writing to the repo's data dir.

Regression test for the 2026-11-03 daily review: the swing engine got a
--check dry-run flag on 2026-11-02 because a plain local run silently
overwrote the published data files — but the intraday engine had the same
footgun: `python3 Scripts/intraday.py` rewrites data/intraday.json, which the
scheduled loop publishes every 30 minutes. --check redirects every file write
to a throwaway temp dir.

Run: python3 -m pytest Scripts/test_intraday_check_mode.py
"""
import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATA = os.path.join(REPO, 'data')

WATCHED = ('intraday.json', 'latest.json', 'history.json')


def _digest(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def test_intraday_check_mode_writes_nothing_to_repo_data():
    before = {fn: _digest(os.path.join(DATA, fn))
              for fn in WATCHED if os.path.exists(os.path.join(DATA, fn))}
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, 'intraday.py'), '--check'],
        capture_output=True, text=True, timeout=900, cwd=REPO)
    assert proc.returncode == 0, f'intraday engine failed in --check mode:\n{proc.stderr[-2000:]}'
    assert 'CHECK MODE' in proc.stdout
    for fn, digest in before.items():
        assert _digest(os.path.join(DATA, fn)) == digest, \
            f'--check mode modified data/{fn}'
