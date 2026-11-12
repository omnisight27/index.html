"""Tests for the feed-artifact guard (2026-11-12 daily review).

Quantix Finance (QFI) printed +76,833% (24h) / +86,837% (7d) on CoinGecko
after the QAI -> QFI contract migration, while 24h volume was ~$19K on a
$411M market cap and the old contract still traded ~$69. The raw move fed
extract_features() unchecked and QFI became the #1 long (score 285.9,
confidence 78). A move that extreme on a $100M+ coin is a redenomination /
migration / bad feed row, not a tradeable move — it must never reach the
pick list.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nexintel


def test_qfi_migration_print_is_artifact():
    assert nexintel.is_feed_artifact(76833.7, 86837.2) is True
    assert nexintel.is_feed_artifact(-76833.7, 86837.2) is True


def test_weekly_only_extreme_is_artifact():
    assert nexintel.is_feed_artifact(10.0, 1500.0) is True


def test_genuine_parabolic_run_stays_eligible():
    # A real +300% day or +900% week is wild but tradeable — the guard
    # sits far above it and must not filter real momentum.
    assert nexintel.is_feed_artifact(300.0, 900.0) is False
    assert nexintel.is_feed_artifact(12.0, 20.0) is False
    assert nexintel.is_feed_artifact(0.0, 0.0) is False


def test_boundary():
    assert nexintel.is_feed_artifact(500.0, 1000.0) is False
    assert nexintel.is_feed_artifact(500.1, 0.0) is True
    assert nexintel.is_feed_artifact(0.0, 1000.1) is True


def _coin(cid, sym, chg24, chg7d, vec):
    return cid, {'symbol': sym, 'name': sym, 'usd': 10.0,
                 'usd_24h_change': chg24, 'usd_7d_change': chg7d,
                 'usd_1h_change': vec[0], 'usd_14d_change': vec[3],
                 'usd_30d_change': vec[4], 'market_cap': 200_000_000}


def test_think_excludes_feed_artifact_pick():
    # End-to-end through think(): a QFI-like migration print must not
    # appear in longs or shorts, while a normal strong mover still does.
    data = {
        'fear_greed': [{'value': 50}],
        'crypto': dict([
            _coin('quantixai', 'QFI', 76833.7, 86837.2, [0, 76833.7, 86837.2, 5783.9, 5455.8]),
            _coin('aaa', 'AAA', 12.0, 20.0, [2, 12, 20, 25, 30]),
            _coin('ddd', 'DDD', -8.0, -12.0, [-2, -8, -12, -15, -18]),
        ]),
        'listings': {'coinbase': {'AAA', 'DDD'}, 'cryptocom': set()},
        'metals': {}, 'forex': {}, 'stocks': {}, 'contracts': [],
        'headlines': [], 'newsletter_digest': {}, 'macro': {},
    }
    history = {'stats': {}, 'picks': [], 'calibration': []}
    signals = nexintel.think(data, history)
    assets = [p['asset'] for p in signals['longs'] + signals['shorts']]
    assert 'QFI' not in assets, assets
    assert 'AAA' in assets, assets


if __name__ == '__main__':
    test_qfi_migration_print_is_artifact()
    test_weekly_only_extreme_is_artifact()
    test_genuine_parabolic_run_stays_eligible()
    test_boundary()
    test_think_excludes_feed_artifact_pick()
    print('feed-artifact tests pass')
