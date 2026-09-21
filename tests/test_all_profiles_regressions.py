"""Review regressions; all SQLite activity is on disposable synthetic fixtures."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from _hermes_ai_usage_ledger_v2 import aggregate, read_snapshot
from test_all_profiles import database, event, inventory, request, runtime, snapshot


@pytest.mark.parametrize('healthy', [False, True])
def test_active_wal_hardlink_paths_are_rejected(tmp_path, runtime, monkeypatch, healthy):
    path = database(tmp_path, [request()], [event()])
    alias = tmp_path / 'profiles' / 'writer' / 'usage-ledger' / path.name
    alias.parent.mkdir(parents=True)
    os.link(path, alias)
    (tmp_path / 'profiles' / 'same-root').symlink_to(tmp_path, target_is_directory=True)
    if healthy:
        database(tmp_path / 'profiles' / 'healthy', [request()], [event()])
    writer = sqlite3.connect(alias)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        row = request('wal-only', ts=2)
        writer.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (row['id'], 2, 2.1, 'fixture', 'fixture', 'same-session', 'same-task',
                        None, 'completed', json.dumps(row)))
        observed = event('wal-only', ts=2)
        writer.execute('INSERT INTO skill_events VALUES(?,?,?,?,?,?,?,?)',
                       (observed['id'], 2, 'skill_load', 'same-session', 'fixture', 'fixture',
                        'same-skill', json.dumps(observed)))
        writer.commit()
        assert writer.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == 2
        assert writer.execute('SELECT COUNT(*) FROM skill_events').fetchone()[0] == 2
        assert Path(str(alias) + '-wal').stat().st_size > 0
        assert not Path(str(path) + '-wal').exists()
        # The retained default pathname alone would silently lose committed rows.
        with read_snapshot.snapshot_root(tmp_path) as copied:
            with sqlite3.connect(copied / 'usage-ledger' / path.name) as c:
                assert c.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == 1
        before = snapshot(tmp_path)
        inv = inventory(tmp_path)
        public = aggregate.public_inventory(inv)
        assert public['discovery']['status'] == 'partial'
        assert str(tmp_path) not in json.dumps(public)
        rejected = [p for p in public['profiles'] if p['name'] != 'healthy']
        assert len(rejected) == 2
        assert all(p['availability'] == {'status': 'unavailable', 'reason': 'ambiguous_hardlink'}
                   for p in rejected)
        assert next(p for p in rejected if p['name'] == 'default')['aliases'] == ['same-root']
        original = aggregate.snapshot_root
        def only_healthy(root):
            assert Path(root).name == 'healthy', 'rejected source reached snapshot reader'
            return original(root)
        monkeypatch.setattr(aggregate, 'snapshot_root', only_healthy)
        for out in (aggregate.ledger(runtime, inv, end=5), aggregate.skills(inv, end=5)):
            assert out['coverage']['status'] == ('partial' if healthy else 'unavailable')
            assert out['coverage']['read_profiles'] == int(healthy)
            statuses = [p for p in out['coverage']['profiles'] if p['name'] != 'healthy']
            assert len(statuses) == 2
            assert all(p['status'] == 'unavailable' and p['reason'] == 'ambiguous_hardlink'
                       for p in statuses)
            count = out.get('request_count', out.get('event_count'))
            assert count == (1 if healthy else None)
            if not healthy:
                assert out['summary'] is None
        assert snapshot(tmp_path) == before
    finally:
        writer.close()


@pytest.mark.parametrize('target_name', ['events.sqlite3', 'renamed.sqlite3'])
def test_canonical_database_symlink_alias_keeps_active_wal(tmp_path, runtime, target_name):
    path = database(tmp_path, [request()], [event()])
    target = path.with_name(target_name)
    if target != path:
        path.rename(target)
        path.symlink_to(target)
    alias = tmp_path / 'profiles' / 'alias' / 'usage-ledger' / path.name
    alias.parent.mkdir(parents=True)
    alias.symlink_to(target)
    writer = sqlite3.connect(target)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        row = request('wal-only', ts=2)
        writer.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (row['id'], 2, 2.1, 'fixture', 'fixture', 'same-session', 'same-task',
                        None, 'completed', json.dumps(row)))
        writer.commit()
        before = snapshot(tmp_path)
        inv = inventory(tmp_path)
        assert inv['discovery']['status'] == 'complete'
        assert len(inv['profiles']) == 1
        assert inv['profiles'][0]['aliases'] == ['alias']
        ledger = aggregate.ledger(runtime, inv, end=5)
        assert ledger['coverage']['status'] == 'complete'
        assert ledger['request_count'] == 2
        skills = aggregate.skills(inv, end=5)
        assert skills['coverage']['status'] == 'complete'
        assert skills['event_count'] == 1
        assert snapshot(tmp_path) == before
    finally:
        writer.close()


@pytest.mark.parametrize('table_present', [False, True])
def test_skills_unrecorded_is_unavailable_not_measured_zero(tmp_path, table_present):
    path = database(tmp_path)
    if not table_present:
        with sqlite3.connect(path) as c:
            c.execute('DROP TABLE skill_events')
    before = snapshot(tmp_path)
    out = aggregate.skills(inventory(tmp_path), end=5)
    assert out['coverage']['status'] == 'unavailable'
    assert out['coverage']['read_profiles'] == 0
    status = out['coverage']['profiles'][0]
    assert status['status'] == status['observations']['status'] == 'not_recorded'
    assert out['summary'] is out['event_count'] is out['snapshot_count'] is None
    assert out['events'] == out['skills'] == out['snapshots'] == []
    assert snapshot(tmp_path) == before


def test_skills_mixed_instrumentation_keeps_available_measured_values(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute('DROP TABLE skill_events')
    database(tmp_path / 'profiles' / 'observed', events=[event(tokens=0)])
    out = aggregate.skills(inventory(tmp_path), end=5)
    assert out['coverage']['status'] == 'partial'
    assert out['coverage']['read_profiles'] == 1
    assert {p['status'] for p in out['coverage']['profiles']} == {'read', 'not_recorded'}
    assert out['summary']['loads'] == out['event_count'] == 1
    assert out['snapshot_count'] == 0
    assert out['skills'][0]['estimated_tokens'] == 0
    # A recorded source with no observations in this window has measured zeros.
    empty = aggregate.skills(inventory(tmp_path), start=2, end=5)
    assert empty['coverage']['status'] == 'partial'
    assert empty['summary']['loads'] == empty['event_count'] == 0


@pytest.mark.parametrize('profile_count', [1, 2])
def test_equal_timestamp_rates_global_500_match_numeric_source_order(tmp_path, runtime, profile_count):
    roots = [tmp_path, tmp_path / 'profiles' / 'other'][:profile_count]
    expected = []
    for root in roots:
        path = database(root)
        with sqlite3.connect(path) as c:
            c.executemany('INSERT INTO rates(id,created,data) VALUES(?,?,?)',
                          [(i, 1, '{}') for i in range(1, 601)])
        expected.extend((i, aggregate.profile_id(root)) for i in range(1, 601))
    out = aggregate.ledger(runtime, inventory(tmp_path), end=5)
    assert len(out['rates']) == 500
    actual = [(r['original_ids']['id'], r['profile_id']) for r in out['rates']]
    assert actual == sorted(expected, reverse=True)[:500]


@pytest.mark.parametrize('keep_changing', [False, True])
def test_real_wal_checkpoint_reset_during_copy(tmp_path, runtime, monkeypatch, keep_changing):
    path = database(tmp_path, [request()])
    writer = sqlite3.connect(path)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('PRAGMA wal_autocheckpoint=0')
    writer.execute("UPDATE requests SET status='completed'")
    writer.commit()
    original = read_snapshot.shutil.copyfile
    copies = 0

    def checkpoint_and_append(index):
        with sqlite3.connect(path) as c:
            assert c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
            row = request('concurrent-' + str(index), ts=2)
            c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (row['id'], 2, 2.1, 'fixture', 'fixture', 'same-session', 'same-task',
                       None, 'completed', json.dumps(row)))

    # Real writer thread changes source files between DB and WAL copying;
    # signatures and SQLite checkpoint/reset are not mocked.
    with ThreadPoolExecutor(max_workers=1) as pool:
        def copying(source, target):
            nonlocal copies
            result = original(source, target)
            if source == path:
                copies += 1
                if keep_changing or copies == 1:
                    pool.submit(checkpoint_and_append, copies).result(timeout=10)
            return result
        monkeypatch.setattr(read_snapshot.shutil, 'copyfile', copying)
        try:
            out = aggregate.ledger(runtime, inventory(tmp_path), end=5)
            if keep_changing:
                assert copies == 3
                assert out['request_count'] is out['summary'] is None
                assert out['coverage']['status'] == 'unavailable'
                assert out['coverage']['profiles'][0]['reason'] == 'snapshot_busy'
            else:
                assert copies == 2
                assert out['coverage']['status'] == 'complete'
                assert out['request_count'] == 2
                assert {r['original_ids']['id'] for r in out['requests']} == {'same', 'concurrent-1'}
        finally:
            writer.close()


def test_hot_rollback_journal_rejected_without_recovery_or_source_writes(tmp_path, runtime):
    path = database(tmp_path, [request()])
    # Spill dirty pages then crash without closing: a real hot rollback journal,
    # not a fabricated header or a mocked signature.
    child = '''import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('PRAGMA journal_mode=DELETE')
c.execute('CREATE TABLE spill (id INTEGER PRIMARY KEY, data BLOB)')
c.executemany('INSERT INTO spill(data) VALUES(?)', [(b'a' * 4096,)] * 128)
c.commit()
c.execute('PRAGMA cache_size=5')
c.execute('BEGIN IMMEDIATE')
c.execute("UPDATE spill SET data=zeroblob(4096)")
os._exit(0)
'''
    subprocess.run([sys.executable, '-c', child, str(path)], check=True, timeout=15)
    journal = Path(str(path) + '-journal')
    assert journal.read_bytes()[:8] == bytes.fromhex('d9d505f920a163d7')
    before = snapshot(tmp_path)
    for out in (aggregate.ledger(runtime, inventory(tmp_path), end=5),
                aggregate.skills(inventory(tmp_path), end=5)):
        assert out['summary'] is None
        assert out['coverage']['status'] == 'unavailable'
        assert out['coverage']['profiles'][0]['reason'] == 'unsupported_rollback_journal'
    assert snapshot(tmp_path) == before
