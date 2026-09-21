"""Synthetic profile fixtures only: named ownership and explicit history repair."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from _hermes_ai_usage_ledger_v2 import attribution as a
from _hermes_ai_usage_ledger_v2.projects import PROJECT_FIELDS, canonical_path
from _hermes_ai_usage_ledger_v2.reconcile_projects import reconcile_projects
from _hermes_ai_usage_ledger_v2.storage import Store


@pytest.fixture
def profile(tmp_path):
    with sqlite3.connect(tmp_path / 'state.db') as c:
        c.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,parent_session_id TEXT,git_repo_root TEXT,cwd TEXT)')
    with sqlite3.connect(tmp_path / 'projects.db') as c:
        c.execute('CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT,primary_path TEXT,archived INTEGER)')
        c.execute('CREATE TABLE project_folders(project_id TEXT,path TEXT)')
    return tmp_path


def session(root, sid, cwd=None, repo=None, parent=None, source='desktop'):
    with sqlite3.connect(root / 'state.db') as c:
        c.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?)', (sid, source, parent, repo, cwd))


def project(root, pid, name, *folders, archived=0):
    with sqlite3.connect(root / 'projects.db') as c:
        c.execute('INSERT INTO projects VALUES(?,?,?,?)', (pid, name, folders[0], archived))
        c.executemany('INSERT INTO project_folders VALUES(?,?)', [(pid, p) for p in folders])


def record(store, sid, key):
    ctx = a.capture(store, sid)
    store.request(dict(id=key, session_id=sid, started=1, ended=2, status='completed',
                       usage={'input_tokens':123, 'output_tokens':7, 'total_tokens':130,
                              'request_count':9, 'raw_usage':{'huge':2**80}}, **ctx), 'request_completed')


def snapshot(store):
    with store.db() as c:
        return {table: [tuple(r) for r in c.execute(f'SELECT * FROM {table} ORDER BY 1')]
                for table in ('requests', 'compressions', 'events', 'rates', 'session_context')}


def test_named_longest_alias_worktree_and_profile_isolation(profile, tmp_path):
    physical = profile / 'physical'
    physical.mkdir()
    alias = profile / 'alias'
    alias.symlink_to(physical, target_is_directory=True)
    project(profile, 'broad', 'Broad', str(profile))
    project(profile, 'p_alpha', 'Canonical Alpha', str(physical), '/elsewhere/worktrees/alpha')
    project(profile, 'archived', 'Ignored', str(physical / 'src'), archived=1)
    store = Store(profile)
    for sid, cwd, repo in [('repo', str(alias / 'src'), str(alias)),
                           ('cwd', str(physical), None),
                           ('tree', '/elsewhere/worktrees/alpha/feature', '/unrelated/repo')]:
        session(profile, sid, cwd, repo)
        record(store, sid, sid)
    records = store.read()['requests']
    assert {r['project_id'] for r in records} == {'p_alpha'}
    assert {r['project_label'] for r in records} == {'Canonical Alpha'}
    assert {r['project_path'] for r in records} == {str(physical)}
    other = Store(tmp_path / 'other-profile')
    assert a.capture(other, 'repo')['project_id'] is None


def test_fallback_same_path_not_kind_basename_or_process_cwd(profile):
    store = Store(profile)
    session(profile, 'repo', repo='/a/alpha/')
    session(profile, 'cwd', cwd='/a/alpha')
    session(profile, 'unrelated', cwd='/b/alpha')
    session(profile, 'relative', cwd='alpha')
    session(profile, 'home', cwd='/')
    values = {sid:a.capture(store, sid) for sid in ('repo','cwd','unrelated','relative','home','missing')}
    assert values['repo']['project_id'] == values['cwd']['project_id']
    assert values['unrelated']['project_id'] != values['cwd']['project_id']
    assert values['relative']['project_id'] is None
    assert values['missing']['project_id'] is None
    assert values['home']['project_label'] == 'Home'
    assert canonical_path('relative') is None


def test_explicit_primary_workspace_beats_ancestor_and_prefix_is_bounded(profile):
    project(profile, 'p', 'Project', '/p')
    store = Store(profile)
    session(profile, 'parent', cwd='/p')
    session(profile, 'home', cwd='/', parent='parent')
    session(profile, 'sibling', cwd='/p-other', parent='parent')
    session(profile, 'no-path', parent='parent')
    assert a.capture(store, 'home')['project_label'] == 'Home'
    assert a.capture(store, 'sibling')['project_id'] != 'p'
    assert a.capture(store, 'no-path')['project_id'] == 'p'


def test_ambiguous_folder_never_selects_arbitrary_project(profile):
    project(profile, 'one', 'One', '/a')
    project(profile, 'two', 'Two', '/a')
    session(profile, 's', cwd='/a/src')
    assert a.capture(Store(profile), 's')['project_id'] not in {'one','two'}


def test_reconcile_current_lineage_nulls_conservation_and_idempotence(profile):
    store = Store(profile)
    project(profile, 'old', 'Old', '/old')
    project(profile, 'new', 'New', '/new', '/worktrees/new')
    session(profile, 'root', cwd='/old', repo='/old')
    session(profile, 'child', cwd='/launcher', parent='root', source='subagent')
    session(profile, 'grandchild', parent='child', source='subagent')
    session(profile, 'general', cwd='/old', repo='/old')
    for sid in ('root','child','grandchild','general','unknown'):
        record(store, sid, sid)
    store.compression(dict(id='comp', session_id='grandchild', started=1, ended=2, extra={'cost':'1.234'}))
    before = snapshot(store)
    session(profile, 'root', cwd='/worktrees/new/feature', repo=None)
    session(profile, 'general', cwd='/', repo=None)
    files = {p:p.read_bytes() for p in (profile/'state.db',profile/'projects.db')}
    dry = reconcile_projects(profile)
    assert dry['tables']['requests'] == dict(scanned=5, changed=4, skipped=1)
    assert snapshot(store) == before
    result = reconcile_projects(profile, apply=True)
    assert result['tables'] == dry['tables']
    after = snapshot(store)
    for table in ('events','rates','session_context'):
        assert before[table] == after[table]
    for table in ('requests','compressions'):
        for old, new in zip(before[table], after[table]):
            assert old[:-1] == new[:-1]
            od, nd = json.loads(old[-1]), json.loads(new[-1])
            assert {k:v for k,v in od.items() if k not in PROJECT_FIELDS} == {k:v for k,v in nd.items() if k not in PROJECT_FIELDS}
            if nd['session_id'] == 'unknown':
                assert od == nd
            else:
                assert nd['project_label'] == ('Home' if nd['session_id']=='general' else 'New')
    assert all(p.read_bytes() == b for p,b in files.items())
    assert all(t['changed']==0 for t in reconcile_projects(profile, apply=True)['tables'].values())
    assert a.capture(store, 'general')['project_label'] == 'Home'
    assert store.read(project='new')['request_count'] == 3
    assert store.read(project='new')['compression_count'] == 1


def test_current_parent_clear_and_cached_link_for_missing_child(profile):
    project(profile, 'p', 'Project', '/p')
    store = Store(profile)
    session(profile, 'parent', cwd='/p')
    session(profile, 'child', cwd='/old', parent='parent', source='subagent')
    record(store, 'child', 'child')
    a.lifecycle(store, 'start', dict(child_session_id='not-in-state', parent_session_id='parent'))
    record(store, 'not-in-state', 'missing-child')
    session(profile, 'child', cwd='/', parent=None, source='subagent')
    reconcile_projects(profile, apply=True)
    with store.db() as c:
        rows = {r['id']:json.loads(r['data']) for r in c.execute('SELECT id,data FROM requests')}
    assert rows['child']['project_label'] == 'Home'
    assert rows['missing-child']['project_id'] == 'p'
    # Reconciliation does not rewrite non-project lineage fields.
    assert rows['child']['parent_session_id'] == 'parent'


def test_reconciliation_fail_closed_and_atomic(profile):
    store = Store(profile)
    session(profile, 's', cwd='/a')
    record(store, 's', 'a')
    session(profile, 's', cwd='/b')
    with store.db() as c:
        c.execute("INSERT INTO compressions VALUES('bad',1,2,NULL,'s',NULL,NULL,'not-json')")
    before = snapshot(store)
    with pytest.raises(json.JSONDecodeError):
        reconcile_projects(profile, apply=True)
    assert snapshot(store) == before
    (profile/'projects.db').unlink()
    with pytest.raises(ValueError):
        reconcile_projects(profile, apply=True)
    assert snapshot(store) == before
    with pytest.raises(ValueError):
        reconcile_projects('relative')


def test_skills_project_only_and_alias_target_refusal(profile):
    store = Store(profile)
    session(profile, 's', cwd='/p')
    project(profile, 'p', 'Named', '/p')
    data = dict(project_id='old', estimate=123, token_count=987, skill='test', private_extra={'keep':True})
    with store.db() as c:
        c.execute('INSERT INTO skill_events VALUES(?,?,?,?,?,?,?,?)',
                  ('skill', 1, 'skill_load', 's', 'provider', 'model', 'test', json.dumps(data)))
    assert reconcile_projects(profile, apply=True)['tables']['skill_events']['changed'] == 1
    with store.db() as c:
        result = json.loads(c.execute('SELECT data FROM skill_events').fetchone()[0])
    assert result == dict(data, project_id='p')
    actual = profile / 'external.sqlite3'
    store.path.rename(actual)
    store.path.symlink_to(actual)
    with pytest.raises(ValueError):
        reconcile_projects(profile, apply=True)


def test_state_refresh_retains_explicit_delegation(profile):
    project(profile, 'p', 'Parent', '/p')
    project(profile, 'child-p', 'Child cwd', '/child')
    session(profile, 'parent', cwd='/p')
    session(profile, 'child', cwd='/child', parent='parent', source='desktop')
    store = Store(profile)
    a.lifecycle(store, 'start', dict(child_session_id='child', parent_session_id='parent'))
    ctx = a.capture(store, 'child')
    assert ctx['agent_kind'] == 'subagent'
    assert ctx['attribution_source'] == 'subagent_lifecycle'
    assert ctx['project_id'] == 'p'


def test_current_metadata_can_rename_and_archive_without_implicit_history_change(profile):
    project(profile, 'p', 'Initial name', '/p')
    store = Store(profile)
    session(profile, 's', cwd='/p')
    record(store, 's', 'old')
    with sqlite3.connect(profile / 'projects.db') as c:
        c.execute("UPDATE projects SET name='Renamed'")
    record(store, 's', 'new')
    with store.db() as c:
        assert json.loads(c.execute("SELECT data FROM requests WHERE id='old'").fetchone()[0])['project_label']=='Initial name'
    reconcile_projects(profile, apply=True)
    assert {r['project_label'] for r in store.read()['requests']} == {'Renamed'}
    with sqlite3.connect(profile / 'projects.db') as c:
        c.execute('UPDATE projects SET archived=1')
    reconcile_projects(profile, apply=True)
    assert {r['project_id'] for r in store.read()['requests']} != {'p'}


@pytest.mark.parametrize('with_ancestor', [False, True])
def test_delegate_inherits_explicit_home_before_named_child_or_ancestor(profile, with_ancestor):
    project(profile, 'p', 'Named project', '/p')
    store = Store(profile)
    if with_ancestor:
        session(profile, 'ancestor', cwd='/p')
    session(profile, 'parent', cwd='/', parent='ancestor' if with_ancestor else None)
    session(profile, 'child', cwd='/p', parent='parent', source='subagent')
    record(store, 'child', 'child')
    assert a.capture(store, 'child')['project_label'] == 'Home'
    reconcile_projects(profile, apply=True)
    assert store.read()['requests'][0]['project_label'] == 'Home'


@pytest.mark.parametrize('path, equivalent', [
    (r'C:\work\repo', 'c:/work/repo'),
    (r'\\server\share\repo', '//SERVER/share/repo'),
    ('C:\\', 'c:/'),
])
def test_windows_absolute_paths_keep_fallback_identity(profile, path, equivalent):
    store = Store(profile)
    session(profile, 'repo', repo=path)
    session(profile, 'cwd', cwd=equivalent)
    record(store, 'repo', 'repo')
    record(store, 'cwd', 'cwd')
    values = [a.capture(store, sid) for sid in ('repo', 'cwd')]
    assert values[0]['project_id'] is not None
    assert all(v['project_label'] for v in values)
    assert values[0]['project_id'] == values[1]['project_id']
    reconcile_projects(profile, apply=True)
    assert {r['project_id'] for r in store.read()['requests']} == {values[0]['project_id']}


def test_cli_dry_run_on_isolated_profile(profile):
    Store(profile)
    result = subprocess.run([sys.executable, '-m', 'ledger_runtime.reconcile_projects',
                             '--profile-root', str(profile)], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)['applied'] is False
