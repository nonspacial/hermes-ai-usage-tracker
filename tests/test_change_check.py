"""Metadata-only change hints against disposable roots, never a live profile."""
import json
import os
from pathlib import Path
import sqlite3

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from _hermes_ai_usage_ledger_v2 import aggregate, api, change_check


def db(root):
    folder = root / 'usage-ledger'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'events.sqlite3'
    path.write_bytes(b'fixture')
    return path


def client(root, monkeypatch):
    def prohibited(*args, **kwargs):
        raise AssertionError('change check must not open SQLite, start workers, or write')
    monkeypatch.setattr(api, 'Store', prohibited)
    monkeypatch.setattr(api, 'start_worker', prohibited)
    # Router construction normally validates AnalyticsRuntime using its own
    # disposable SQLite; isolate this endpoint from that unrelated startup.
    monkeypatch.setattr(api, 'AnalyticsRuntime', lambda: object())
    monkeypatch.setattr(sqlite3, 'connect', prohibited)
    router = APIRouter()
    def resolve(name):
        if name in ('', 'default'):
            return root, 'default', None
        target = root / 'profiles' / name
        return (target, name, None) if target.is_dir() else (None, None, 'No profile')
    api.add_routes(router, resolve, lambda: root)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def token(root, selected=None):
    return change_check.check(aggregate.discover(root), selected_root=selected)


def test_endpoint_is_quiet_and_scoped(tmp_path, monkeypatch):
    source = db(tmp_path)
    other = db(tmp_path / 'profiles' / 'other')
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in (source, other)}
    with client(tmp_path, monkeypatch) as web:
        selected = web.get('/ledger/change-token').json()
        all_profiles = web.get('/ledger/change-token?profile_scope=all').json()
        assert selected == web.get('/ledger/change-token').json()
        assert all_profiles == web.get('/ledger/change-token?profile_scope=all').json()
        assert selected['profile_scope'] == 'selected'
        assert all_profiles['profile_scope'] == 'all'
        assert selected['read_only'] and all_profiles['read_only']
        assert selected['token_kind'] == 'opaque-filesystem-hint'
        assert all_profiles['capabilities'] == {
            'change_check': True, 'aggregate_events': False,
            'selected_events': 'negotiated-on-connect'}
        assert len(selected['coverage']['profiles']) == 1
        assert len(all_profiles['coverage']['profiles']) == 2
        assert all_profiles['coverage']['status'] == 'complete'
        assert str(tmp_path) not in json.dumps(all_profiles)
        assert web.get('/ledger/change-token?profile_scope=wrong').status_code == 400
        assert web.get('/ledger/change-token?profile=unknown').status_code == 404
        assert web.get('/ledger/change-token?profile=other').json()['profile_scope'] == 'selected'
    assert before == {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in (source, other)}
    assert not list(tmp_path.rglob('*-wal')) and not list(tmp_path.rglob('*-shm'))


def test_profile_isolation_inventory_aliases_and_hardlinks(tmp_path):
    default = db(tmp_path)
    other = db(tmp_path / 'profiles' / 'other')
    initial_all = token(tmp_path)
    initial_default = token(tmp_path, tmp_path)
    initial_other = token(tmp_path, other.parent.parent)
    # A size-preserving update still changes the metadata hint.
    other.write_bytes(b'Fixture')
    assert token(tmp_path)['change_token'] != initial_all['change_token']
    assert token(tmp_path, tmp_path)['change_token'] == initial_default['change_token']
    assert token(tmp_path, other.parent.parent)['change_token'] != initial_other['change_token']
    alias = tmp_path / 'profiles' / 'alias'
    alias.symlink_to(tmp_path / 'profiles' / 'other', target_is_directory=True)
    aliased = token(tmp_path)
    assert len(aliased['coverage']['profiles']) == 2
    assert aliased['change_token'] != token(tmp_path, tmp_path)['change_token']
    assert aliased['change_token'] != initial_all['change_token']
    linked = db(tmp_path / 'profiles' / 'linked')
    linked.unlink()
    os.link(default, linked)
    ambiguous = token(tmp_path)
    assert ambiguous['coverage']['status'] == 'partial'
    assert {row['reason'] for row in ambiguous['coverage']['profiles'] if row['status'] == 'unavailable'} == {'ambiguous_hardlink'}
    assert token(tmp_path, tmp_path)['coverage']['status'] == 'unavailable'
    assert str(tmp_path) not in json.dumps(ambiguous)


def test_selected_database_symlink_uses_discovered_identity(tmp_path):
    db(tmp_path)
    alias_root = tmp_path / 'profiles' / 'alias'
    alias_root.joinpath('usage-ledger').mkdir(parents=True)
    alias_root.joinpath('usage-ledger', 'events.sqlite3').symlink_to(
        tmp_path / 'usage-ledger' / 'events.sqlite3')
    inventory = aggregate.discover(tmp_path)
    assert len(inventory['profiles']) == 1
    canonical = inventory['profiles'][0]
    assert canonical['aliases'] == ['alias']
    for root in (tmp_path, alias_root):
        selected = token(tmp_path, root)
        assert selected['coverage']['status'] == 'complete'
        assert selected['coverage']['profiles'] == [
            {'profile_id': canonical['profile_id'], 'name': canonical['name'],
             'aliases': ['alias'], 'status': 'available'}]
        assert selected['change_token'] == token(tmp_path, tmp_path)['change_token']
    assert token(tmp_path)['coverage']['profiles'] == selected['coverage']['profiles']
    assert str(tmp_path) not in json.dumps(selected)


def test_endpoint_database_alias_matches_profiles_and_all(tmp_path, monkeypatch):
    source = db(tmp_path)
    alias_root = tmp_path / 'profiles' / 'alias'
    alias_root.joinpath('usage-ledger').mkdir(parents=True)
    alias_root.joinpath('usage-ledger', 'events.sqlite3').symlink_to(source)
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    with client(tmp_path, monkeypatch) as web:
        profiles = web.get('/ledger/profiles').json()['profiles']
        selected = web.get('/ledger/change-token?profile=alias').json()
        all_profiles = web.get('/ledger/change-token?profile_scope=all').json()
    assert len(profiles) == 1
    assert profiles[0]['aliases'] == ['alias']
    assert selected['coverage']['profiles'] == all_profiles['coverage']['profiles']
    assert selected['coverage']['profiles'][0]['profile_id'] == profiles[0]['profile_id']
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    assert str(tmp_path) not in json.dumps(selected)


def test_selected_root_symlink_uses_discovered_identity(tmp_path):
    db(tmp_path)
    profiles = tmp_path / 'profiles'
    profiles.mkdir()
    (profiles / 'alias').symlink_to(tmp_path, target_is_directory=True)
    inventory = aggregate.discover(tmp_path)
    assert len(inventory['profiles']) == 1
    assert inventory['profiles'][0]['aliases'] == ['alias']
    assert token(tmp_path, profiles / 'alias') == token(tmp_path, tmp_path)


def test_selected_database_alias_preserves_hardlink_ambiguity(tmp_path):
    default = db(tmp_path)
    linked = db(tmp_path / 'profiles' / 'linked')
    linked.unlink()
    os.link(default, linked)
    alias_root = tmp_path / 'profiles' / 'alias'
    alias_root.joinpath('usage-ledger').mkdir(parents=True)
    alias_root.joinpath('usage-ledger', 'events.sqlite3').symlink_to(linked)
    inventory = aggregate.discover(tmp_path)
    linked_row = next(row for row in inventory['profiles'] if
                      Path(row['path']).joinpath('usage-ledger', 'events.sqlite3').resolve() == linked)
    assert set((linked_row['name'], *linked_row['aliases'])) == {'alias', 'linked'}
    for selected_root in (alias_root, linked.parent.parent):
        selected = token(tmp_path, selected_root)
        assert selected['coverage']['status'] == 'unavailable'
        assert selected['coverage']['profiles'] == [
            {'profile_id': linked_row['profile_id'], 'name': linked_row['name'],
             'aliases': linked_row['aliases'], 'status': 'unavailable',
             'reason': 'ambiguous_hardlink'}]


def test_coverage_distinguishes_all_missing_mixed_and_all_statable(tmp_path):
    default = db(tmp_path)
    other = db(tmp_path / 'profiles' / 'other')
    assert token(tmp_path)['coverage']['status'] == 'complete'
    other.unlink()
    mixed = token(tmp_path)
    assert mixed['coverage']['status'] == 'partial'
    assert [row['status'] for row in mixed['coverage']['profiles']] == ['available', 'unavailable']
    default.unlink()
    missing = token(tmp_path)
    assert missing['coverage']['status'] == 'unavailable'
    assert all(row['reason'] == 'missing_database' for row in missing['coverage']['profiles'])


def test_all_metadata_unreadable_is_unavailable(tmp_path, monkeypatch):
    db(tmp_path)
    db(tmp_path / 'profiles' / 'other')
    original = change_check._signature
    def denied(path):
        if str(path).endswith('events.sqlite3'):
            raise PermissionError('/sensitive/path')
        return original(path)
    monkeypatch.setattr(change_check, '_signature', denied)
    result = token(tmp_path)
    assert result['coverage']['status'] == 'unavailable'
    assert all(row['reason'] == 'metadata_unreadable' for row in result['coverage']['profiles'])
    assert '/sensitive/path' not in json.dumps(result)


def test_wal_and_catalogue_metadata_and_unavailability(tmp_path):
    source = db(tmp_path)
    first = token(tmp_path)
    wal = Path(str(source) + '-wal')
    wal.write_bytes(b'WAL!')
    with_wal = token(tmp_path)
    assert with_wal['change_token'] != first['change_token']
    # Same-size WAL reuse with forced nanosecond timestamp: no size-only shortcut.
    stamp = wal.stat().st_mtime_ns
    wal.write_bytes(b'wal!')
    os.utime(wal, ns=(stamp + 1_000_000_000, stamp + 1_000_000_000))
    assert token(tmp_path)['change_token'] != with_wal['change_token']
    # Provider catalogue and pricing status live in the same SQLite database.
    before_database_change = token(tmp_path)
    source.write_bytes(b'Fixture')
    assert token(tmp_path)['change_token'] != before_database_change['change_token']
    journal = Path(str(source) + '-journal')
    journal.write_bytes(b'hot')
    assert token(tmp_path)['coverage']['profiles'][0]['reason'] == 'unsupported_rollback_journal'
    journal.unlink()
    source.unlink()
    missing = token(tmp_path)
    assert missing['coverage']['status'] == 'unavailable'
    assert missing['coverage']['profiles'][0]['reason'] == 'missing_database'
    assert missing['change_token'] != first['change_token']


def test_real_sqlite_catalogue_commits_in_uncheckpointed_wal(tmp_path):
    folder = tmp_path / 'usage-ledger'
    folder.mkdir()
    source = folder / 'events.sqlite3'
    with sqlite3.connect(source) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE provider_catalog(source_id TEXT, data TEXT)')
        connection.commit()
        first = token(tmp_path)['change_token']
        connection.execute('INSERT INTO provider_catalog VALUES(?,?)', ('fixture', '{"rate":1}'))
        connection.commit()
        wal = Path(str(source) + '-wal')
        assert wal.exists() and wal.stat().st_size > 0
        second = token(tmp_path)
        assert second['change_token'] != first
        assert second['coverage']['status'] == 'complete'
        connection.execute('UPDATE provider_catalog SET data=?', ('{"rate":2}',))
        connection.commit()
        assert token(tmp_path)['change_token'] != second['change_token']


def test_invalid_source_type_is_unavailable(tmp_path):
    folder = tmp_path / 'usage-ledger'
    folder.mkdir()
    (folder / 'events.sqlite3').mkdir()
    result = token(tmp_path)
    assert result['coverage']['status'] == 'unavailable'
    assert result['coverage']['profiles'][0]['reason'] == 'invalid_source_type'


def test_metadata_errors_are_publicly_redacted(tmp_path, monkeypatch):
    db(tmp_path)
    original = change_check._signature
    def denied(path):
        if str(path).endswith('events.sqlite3'):
            raise PermissionError('/sensitive/path')
        return original(path)
    monkeypatch.setattr(change_check, '_signature', denied)
    result = token(tmp_path)
    assert result['coverage']['status'] == 'unavailable'
    assert result['coverage']['profiles'][0]['reason'] == 'metadata_unreadable'
    assert '/sensitive/path' not in json.dumps(result)
