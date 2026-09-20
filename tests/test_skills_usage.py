"""Synthetic-only skill lifecycle/API verification: no upstream runtime or live DB."""
import json
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from _hermes_ai_usage_ledger_v2 import adapters, api, recorder as r, skills
from _hermes_ai_usage_ledger_v2.attribution import capture
from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime, RestartRequired
from _hermes_ai_usage_ledger_v2.storage import Store


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(adapters, 'install', lambda: None)
    monkeypatch.setattr(r, 'health', lambda *a, **kw: None)
    monkeypatch.setattr(r, '_STORES', {})
    monkeypatch.setattr(r, '_LOOKUP', {})
    monkeypatch.setattr(r, '_TURN_ROOTS', {})
    token = r.CURRENT.set(None)
    yield tmp_path
    r.CURRENT.reset(token)


def start(sid='session', turn='turn', aid='request', model='fixture', **kw):
    args = dict(session_id=sid, turn_id=turn, api_request_id=aid, provider='fixture', model=model,
                platform='cli', started_at=100, request_messages=[], system_prompt='SYSTEM_SECRET')
    args.update(kw)
    r.pre(**args)
    return args


def load(call='call', name='alpha', reference='', success=True, **kw):
    args = dict(tool_name='skill_view', session_id='session', turn_id='turn', api_request_id='request',
                tool_call_id=call, args={'name': name, 'file_path': reference}, status='ok',
                result={'name': name, 'success': success, 'content': 'CONTENT_SECRET' if success else None,
                        'error': 'DIAGNOSTIC_SECRET' if not success else None, 'skill_dir': '/PRIVATE_PATH'})
    args.update(kw)
    skills.tool(**args)
    return args


def report(env, **kw):
    return skills.read(env, **kw)


def test_success_reference_failure_reload_dedup_and_privacy(env):
    start()
    event = load()
    skills.tool(**event)
    load('repeat')
    load('reference', reference='references/api.md')
    load('failed', success=False)
    load('stub', result={'name': 'alpha', 'success': True, 'dedup': True, 'content_returned': False})
    out = report(env)
    assert out['summary'] == {'loads': 3, 'references': 1, 'failures': 1, 'sessions': 1}
    assert out['skills'][0]['repeat_loads'] == 2
    assert out['skills'][0]['estimated_tokens'] is None
    assert out['event_count'] == 6  # 5 distinct loads plus the pre-request snapshot
    assert out['coverage']['status'] == 'partial'
    with r.store(env).db() as c:
        data = '\n'.join(row[0] for row in c.execute('SELECT data FROM skill_events'))
    for forbidden in ('SYSTEM_SECRET', 'CONTENT_SECRET', 'DIAGNOSTIC_SECRET', 'PRIVATE_PATH', 'skill_dir', 'args'):
        assert forbidden not in data
    stub = next(e for e in out['events'] if e.get('deduplicated'))
    assert stub['content_returned'] is False and stub['repeat'] is True


def test_request_snapshot_uses_raw_not_truncated_body_and_does_not_double_system(env):
    messages = [{'role': 'system', 'content': 'abcd<available_skills>alpha</available_skills>'},
                {'role': 'assistant', 'tool_calls': [{'id': 'tc', 'function': {'name': 'skill_view', 'arguments': '{"name":"alpha"}'}}]},
                {'role': 'tool', 'tool_call_id': 'tc', 'content': json.dumps({'name': 'alpha', 'success': True, 'content': 'private' * 40})},
                {'role': 'user', 'content': 'user secret'}]
    kw = start(request_messages=messages, system_prompt=messages[0]['content'],
               request={'body': {'messages': [{'role': 'user', 'content': 'TRUNCATED'}]}})
    r.pre(**kw)
    out = report(env)
    assert out['snapshot_count'] == 1
    snap = out['snapshots'][0]
    assert snap['context_used'] == skills.context(messages, None)['context_used']
    assert snap['context_used'] == sum(c['tokens'] for c in snap['categories'])
    assert snap['context_max'] is None
    assert snap['retained_skills'] == ['alpha']
    assert snap['source'] == 'rough_chars_v1'
    assert snap['attribution'] == 'direct_tool_pairs_only'
    assert next(c['tokens'] for c in snap['categories'] if c['id'] == 'system_prompt') == 1
    assert report(env, skill='alpha')['snapshot_count'] == 1
    assert report(env, skill='unseen')['snapshot_count'] == 0
    assert 'private' not in json.dumps(snap)


def test_native_preflight_estimate_and_missing_raw_context(env):
    start(approx_input_tokens=987, request_messages=None)
    snap = report(env)['snapshots'][0]
    assert snap['context_used'] == 987 and snap['categories'] == []
    assert snap['source'] == 'native_preflight_estimate_categories_chars_v1'
    assert snap['attribution'] == 'unknown'


def test_responses_pairs_and_structured_system_are_estimated_without_retaining_text():
    messages = [{'type': 'function_call', 'call_id': 'tc', 'name': 'skill_view', 'arguments': '{"name":"alpha"}'},
                {'type': 'function_call_output', 'call_id': 'tc', 'output': json.dumps({'success': True, 'content': 'PRIVATE'})}]
    snap = skills.context(messages, [{'type': 'text', 'text': 'PRIVATE_SYSTEM'}])
    assert snap['retained_skills'] == ['alpha']
    assert {c['id'] for c in snap['categories']} == {'conversation', 'skills', 'system_prompt'}
    assert 'PRIVATE' not in json.dumps(snap)
    messages[1]['output'] = '[SKILL_PRUNED]'
    assert skills.context(messages, None)['attribution'] == 'unknown'
    assert skills.context(messages, None)['retained_skills'] == []


def test_profile_recovery_requires_unique_exact_request(env, monkeypatch):
    start()
    other = env / 'other'
    monkeypatch.setenv('HERMES_HOME', str(other))
    r.CURRENT.set(None)
    load()  # unique session/request pair recovers the registered profile
    assert report(env)['summary']['loads'] == 1
    assert not (other / 'usage-ledger').exists()
    start(model='other-model')
    third = env / 'third'
    monkeypatch.setenv('HERMES_HOME', str(third))
    r.CURRENT.set(None)
    load('ambiguous')
    assert not (third / 'usage-ledger').exists()
    assert report(env)['summary']['loads'] == 1 and report(other)['summary']['loads'] == 0
    monkeypatch.setenv('HERMES_HOME', str(env))
    load('mismatch', turn_id='wrong-turn')
    assert report(env)['summary']['loads'] == 1


def test_observation_is_immutable_and_does_not_change_request_accounting(env):
    kw = start()
    s = r.store(env)
    with s.db() as c:
        before = c.execute('SELECT data FROM requests').fetchall()[0][0]
        original_snapshot = c.execute('SELECT data FROM skill_events').fetchone()[0]
    load()
    r.pre(**{**kw, 'request_messages': [{'role': 'user', 'content': 'changed'}]})
    with s.db() as c:
        assert c.execute('SELECT data FROM requests').fetchone()[0] == before
        assert c.execute("SELECT data FROM skill_events WHERE kind='context_snapshot'").fetchone()[0] == original_snapshot


def test_turn_completion_has_no_fabricated_occupancy(env):
    start()
    r.session_end(session_id='session', turn_id='turn', completed=True)
    r.session_end(session_id='session', turn_id='turn', completed=True)
    event = report(env)['snapshots'][0]
    assert event['kind'] == 'turn_end'
    assert event['context_used'] is None and event['categories'] == []
    assert event['source'] == 'unavailable_at_boundary'
    assert report(env)['snapshot_count'] == 2


@pytest.mark.parametrize('outcome', ['committed', 'aborted', 'raised'])
def test_compression_links_real_commit_only(env, outcome):
    start()
    agent = SimpleNamespace(session_id='session', provider='fixture', model='fixture', platform='cli',
                            _current_turn_id='turn', _cached_system_prompt='SECRET_SYSTEM',
                            context_compressor=SimpleNamespace(_resolved_context_length=32000))
    before = [{'role': 'user', 'content': 'content' * 100}]
    after = [{'role': 'user', 'content': 'summary'}]

    def original(agent, messages, system_message, **kw):
        if outcome == 'raised':
            raise RuntimeError('PRIVATE_DIAGNOSTIC')
        if outcome == 'committed':
            agent.session_id = 'rotated-session'
        adapters.native_comp_event(agent, {'commit_status': outcome, 'split_status': 'rotated_committed'})
        return after, 'new system'

    wrapped = adapters.compression_wrapper(original)
    if outcome == 'raised':
        with pytest.raises(RuntimeError, match='PRIVATE_DIAGNOSTIC'):
            wrapped(agent, before, None)
    else:
        assert wrapped(agent, before, None) == (after, 'new system')
    events = [s for s in report(env)['snapshots'] if s['kind'].startswith('compression_')]
    assert len(events) == (2 if outcome == 'committed' else 1)
    assert len({e['compression_id'] for e in events}) == 1
    assert next(e for e in events if e['kind'] == 'compression_before')['session_id'] == 'session'
    if outcome == 'committed':
        post = events[0]
        assert post['session_id'] == 'rotated-session' and post['kind'] == 'compression_after'
        assert post['context_max'] == 32000 and post['attribution'] == 'unknown'
        assert post['context_used'] < events[1]['context_used']
    assert r.COMPRESSION.get() is None


def test_exact_identity_parallel_turns_and_profiles(env, monkeypatch):
    start(turn='one', aid='a', model='model-one')
    start(turn='two', aid='b', model='model-two')
    # Clear current request as post_api_request does before tool execution.
    r.CURRENT.set(None)
    load('one-call', turn_id='one', api_request_id='a')
    load('two-call', turn_id='two', api_request_id='b')
    other = env / 'other-profile'
    monkeypatch.setenv('HERMES_HOME', str(other))
    start(turn='one', aid='a', model='model-other')
    load('one-call', turn_id='one', api_request_id='a')
    assert {s['model'] for s in report(env)['events'] if s['kind'] == 'skill_load'} == {'model-one', 'model-two'}
    assert report(other)['summary']['loads'] == 1
    assert next(s for s in report(other)['events'] if s['kind'] == 'skill_load')['model'] == 'model-other'
    assert report(env)['summary']['loads'] == 2


def test_missing_ids_and_unrelated_tools_do_not_guess(env):
    load(tool_name='terminal')
    load(tool_call_id=None)
    load(session_id=None)
    assert not (env / 'usage-ledger').exists()


def test_concurrent_duplicate_callbacks_insert_once(env):
    start()
    args = load()
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: skills.tool(**args), range(18)))
    assert report(env)['summary']['loads'] == 1
    assert report(env)['skills'][0]['repeat_loads'] == 0


def test_full_period_aggregates_scope_filters_and_pagination(env):
    start()
    load()
    load('beta-call', name='beta')
    s = r.store(env)
    capture(s, 'child', 'subagent', {'parent_session_id': 'session', 'parent_known': True, 'subagent_id': 'delegate', 'cwd': '/fixture/project'})
    start(sid='child', turn='child-turn', aid='child-req', model='child-model', platform='subagent')
    load('child-call', session_id='child', turn_id='child-turn', api_request_id='child-req')
    out = report(env, session='session', session_scope='family', limit=1)
    assert out['summary']['loads'] == 3 and out['summary']['sessions'] == 2
    assert out['event_count'] == 5 and out['next_offset'] == 1
    assert len(out['events']) == 1
    assert report(env, session='session')['summary']['loads'] == 2
    assert report(env, agent='subagent')['summary']['loads'] == 1
    assert report(env, subagent='delegate')['summary']['loads'] == 1
    project = report(env, agent='subagent')['events'][0]['project_id']
    assert report(env, project=project)['summary']['loads'] == 1
    assert report(env, project='unattributed')['summary']['loads'] == 2
    detail = report(env, skill='alpha')
    assert detail['summary'] == out['summary']
    assert detail['skills'] == out['skills']
    assert detail['model_options'] == ['child-model', 'fixture']
    assert detail['event_count'] == 2
    assert report(env, model='child-model')['summary']['loads'] == 1
    assert report(env, provider='absent')['summary']['loads'] == 0
    pages = [report(env, offset=i, limit=1)['events'][0]['id'] for i in range(out['event_count'])]
    assert len(set(pages)) == out['event_count']
    assert report(env, offset=100)['next_offset'] is None


def test_equal_timestamps_deterministic_pagination_snapshot_bound_and_window(env, monkeypatch):
    s = Store(env)
    for i in range(6):
        skills.append(s, {'id': f'event-{i}', 'ts': 10, 'kind': 'context_snapshot', 'session_id': 's',
                          'provider': 'fixture', 'model': 'fixture', 'context_used': None, 'categories': []})
    monkeypatch.setattr(skills, 'SNAPSHOT_LIMIT', 3)
    out = report(env, start=10, end=11, limit=2)
    assert out['event_count'] == out['snapshot_count'] == 6
    assert out['snapshots_truncated'] and len(out['snapshots']) == 3
    assert [e['id'] for e in out['events']] == ['event-5', 'event-4']
    assert [e['id'] for e in report(env, start=10, end=11, offset=2, limit=2)['events']] == ['event-3', 'event-2']
    assert report(env, start=0, end=10)['event_count'] == 0
    assert report(env, start=11, end=12)['event_count'] == 0


def test_test_marker_applies_to_aggregates_and_details(env):
    start()
    before = time.time()
    load()
    after = time.time()
    with r.store(env).db() as c:
        c.execute('INSERT INTO tests VALUES(?,?,?,?,?)', ('test', before, after, 'fixture', '{}'))
    out = report(env, test_id='test')
    assert out['window'] == {'start': before, 'end': after}
    assert out['summary']['loads'] == 1 and out['snapshot_count'] == 0
    with pytest.raises(ValueError, match='Unknown test'):
        report(env, test_id='absent')


def client_for(root):
    router = APIRouter()
    api.add_routes(router, lambda profile: (root, profile, 'No profile' if profile == 'absent' else None), lambda: root)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_endpoint_read_only_missing_legacy_and_current_databases(env, monkeypatch):
    def prohibited(*a, **kw):
        raise AssertionError('Read endpoint attempted to initialise a writer')
    monkeypatch.setattr(api, 'Store', prohibited)
    monkeypatch.setattr(api, 'start_worker', prohibited)
    client = client_for(env)
    assert client.get('/ledger/skills').json()['coverage']['status'] == 'not_recorded'
    assert not (env / 'usage-ledger').exists()
    folder = env / 'usage-ledger'
    folder.mkdir()
    db = folder / 'events.sqlite3'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE legacy(x)')
    before = db.read_bytes()
    assert client.get('/ledger/skills').json()['coverage']['status'] == 'not_recorded'
    assert db.read_bytes() == before
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [('legacy',)]
    assert client.get('/ledger/skills?profile=absent').status_code == 404
    assert client.get('/ledger/skills?test_id=missing').status_code == 400
    for query in ('limit=201', 'limit=0', 'offset=-1', 'start=-1', 'start=nan', 'end=inf', 'session_scope=bad', 'agent=bad'):
        assert client.get('/ledger/skills?' + query).status_code == 400
    # Producer migration is allowed; subsequent reads still do not mutate it.
    start()
    load()
    with sqlite3.connect(db) as c:
        c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    before = db.read_bytes()
    response = client.get('/ledger/skills?limit=1')
    assert response.status_code == 200 and response.json()['summary']['loads'] == 1
    assert db.read_bytes() == before


def test_new_skills_module_is_protected_from_analytics_reload(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'ledger_runtime'
    folder = tmp_path / 'plugin' / 'ledger_runtime'
    shutil.copytree(source, folder, ignore=shutil.ignore_patterns('__pycache__'))
    runtime = AnalyticsRuntime(folder)
    try:
        path = folder / 'skills.py'
        path.write_text(path.read_text() + '\n# changed producer boundary\n')
        assert runtime.info()['restart_required']
        with pytest.raises(RestartRequired):
            runtime.reload()
    finally:
        runtime._discard(runtime.current)


def test_reused_native_tool_id_across_requests_is_not_a_duplicate(env):
    start()
    first = load('native-fallback')
    start(aid='second')
    second = load('native-fallback', api_request_id='second')
    skills.tool(**first)
    skills.tool(**second)
    out = report(env)
    assert out['summary']['loads'] == 2
    assert out['skills'][0]['repeat_loads'] == 1


def test_categorised_lookup_unifies_main_reference_failure_and_retention(env):
    start()
    load('main', name='category/alpha', result={'name': 'alpha', 'success': True, 'content': 'text'})
    load('reference', name='category/alpha', reference='references/api.md')
    load('failure', name='category/alpha', success=False)
    out = report(env, skill='category/alpha')
    assert len(out['skills']) == 1
    assert out['skills'][0]['name'] == 'category/alpha'
    assert out['summary']['loads'] == out['summary']['references'] == out['summary']['failures'] == 1
    assert out['event_count'] == 3
    snap = skills.context([
        {'type': 'function_call', 'call_id': 'tc', 'name': 'skill_view', 'arguments': '{"name":"category/alpha"}'},
        {'type': 'function_call_output', 'call_id': 'tc', 'output': '{"success":true,"name":"alpha","content":"text"}'},
    ], '')
    assert snap['retained_skills'] == ['category/alpha']


@pytest.mark.parametrize('name', ['/home/private/customer/skill', '../private', 'category/../private', 'C:/private/skill'])
def test_invalid_skill_lookup_names_never_persist(env, name):
    start()
    load(name=name, success=False)
    load('fallback', args={}, result={'success': False, 'name': name})
    assert report(env)['summary']['failures'] == 0
    with r.store(env).db() as c:
        assert name not in '\n'.join(row[0] for row in c.execute('SELECT data FROM skill_events'))


def test_compression_observer_never_resolves_lazy_context_limit(env):
    class Compressor:
        _resolved_context_length: int | None = None
        accesses = 0

        @property
        def context_length(self):
            self.accesses += 1
            raise AssertionError('Observer must not resolve model metadata')

    cc = Compressor()
    agent = SimpleNamespace(session_id='session', provider='fixture', model='fixture', context_compressor=cc)
    skills.compression((str(env), {'id': 'comp-unresolved'}), agent, [], '', 'compression_before')
    assert cc.accesses == 0
    assert report(env)['snapshots'][0]['context_max'] is None
    cc._resolved_context_length = 12345
    skills.compression((str(env), {'id': 'comp-resolved'}), agent, [], '', 'compression_before')
    assert cc.accesses == 0
    assert next(s for s in report(env)['snapshots'] if s['compression_id'] == 'comp-resolved')['context_max'] == 12345
