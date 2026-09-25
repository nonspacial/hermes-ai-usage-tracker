"""Offline record-page and one-response frozen-list contracts."""
import json
import sqlite3
from contextlib import contextmanager

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from _hermes_ai_usage_ledger_v2 import storage
from _hermes_ai_usage_ledger_v2.api import add_routes


def seed(root, size):
    folder = root / 'usage-ledger'
    folder.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(folder / 'events.sqlite3') as conn:
        conn.executescript(storage.SCHEMA)
        for i in range(size):
            key = f'row-{i:03}'
            row = dict(id=key, started=i + 1, ended=i + 1.5, provider='synthetic',
                       model=f'model-{i}', response_model=f'model-{i}', session_id=key,
                       root_session_id=key, project_id=f'project-{i}', task=f'task-{i}',
                       agent_kind='primary', status='completed', usage={'total_tokens': 1},
                       cost={'rate': {'source': 'synthetic', 'revision': str(i)}})
            conn.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (key, i + 1, i + 1.5, 'synthetic', f'model-{i}', key,
                          f'task-{i}', None, 'completed', json.dumps(row)))
            comp = dict(id=key, started=i + 1, ended=i + 1.5, provider='synthetic',
                        model=f'model-{i}', session_id=key, kind='compression')
            conn.execute('INSERT INTO compressions VALUES(?,?,?,?,?,?,?,?)',
                         (key, i + 1, i + 1.5, 'synthetic', key, key, None, json.dumps(comp)))


def client_for(root):
    router = APIRouter()
    add_routes(router, lambda _: (root, 'synthetic', None), lambda: root)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize('size', [0, 10, 11, 21])
@pytest.mark.parametrize('view,group,field', [
    ('requests', None, 'requests'), ('compressions', None, 'compressions'),
    ('models', None, 'groups'), ('overview', 'model', 'model_groups'),
    ('overview', 'project', 'project_groups'), ('overview', 'session', 'session_groups'),
    ('overview', 'time', 'list_rows'), ('cache', None, 'applied_rate_groups'),
])
def test_selected_pages_and_frozen_full(tmp_path, size, view, group, field):
    seed(tmp_path, size)
    with client_for(tmp_path) as client:
        query = dict(start=0, end=40, view=view, limit=10, list_mode='page')
        if group is not None:query['group'] = group
        first = client.get('/ledger', params=query)
        assert first.status_code == 200, first.text
        first = first.json()
        count = first['list_count']
        assert len(first[field]) == min(10, count)
        assert first['summary']['attempts'] == size
        assert first['list_next_offset'] == (10 if count > 10 else None)
        assert first['trend']['buckets']  # chart remains full-window
        pages = list(first[field])
        cursor = first['list_next_offset']
        while cursor is not None:
            result = client.get('/ledger', params={**query, 'offset': cursor}).json()
            assert result['list_count'] == count
            pages += result[field]
            cursor = result['list_next_offset']
        frozen = client.get('/ledger', params={**query, 'list_mode': 'all'})
        assert frozen.status_code == 200, frozen.text
        frozen = frozen.json()
        assert frozen['list_count'] == count == len(pages) == len(frozen[field])
        assert frozen['list_next_offset'] is None
        assert frozen[field] == pages
        assert frozen['summary'] == first['summary']
        assert frozen['trend'] == first['trend']


@pytest.mark.parametrize('size', [0, 10, 11, 21])
def test_all_profiles_independent_snapshots(tmp_path, size):
    seed(tmp_path, size)
    seed(tmp_path / 'profiles' / 'other', size)
    with client_for(tmp_path) as client:
        params = dict(profile_scope='all', start=0, end=40, view='requests', limit=10, list_mode='page')
        first = client.get('/ledger', params=params).json()
        full = client.get('/ledger', params={**params, 'list_mode': 'all'}).json()
        assert len(first['requests']) == min(10, size * 2)
        assert full['list_count'] == len(full['requests']) == size * 2
        assert first['summary']['attempts'] == size * 2
        assert full['coverage']['status'] == 'complete'
        assert 'no simultaneous cross-profile instant' in full['list_provenance']
        assert len({(row['profile_id'], row['original_ids']['id']) for row in full['requests']}) == size * 2


def test_all_profiles_unavailable_full_report_fails_explicitly(tmp_path):
    (tmp_path / 'profiles' / 'missing').mkdir(parents=True)
    with client_for(tmp_path) as client:
        response = client.get('/ledger', params=dict(profile_scope='all', end=40,
            view='requests', list_mode='all', limit=10))
        assert response.status_code == 400
        assert 'no readable profiles' in response.json()['detail']


@pytest.mark.parametrize('scope', ['selected', 'all'])
def test_compression_type_filter_pages_before_render(tmp_path, scope):
    for root in ([tmp_path, tmp_path / 'profiles' / 'other'] if scope == 'all' else [tmp_path]):
        seed(root, 21)
        with sqlite3.connect(root / 'usage-ledger' / 'events.sqlite3') as conn:
            conn.execute("UPDATE compressions SET data=json_set(data,'$.kind','micro_compaction') WHERE CAST(SUBSTR(id,5) AS INT) % 2 = 1")
    with client_for(tmp_path) as client:
        query = dict(profile_scope=scope, end=40, view='compressions', list_mode='page',
                     compression_kind='micro_compaction', limit=10)
        first = client.get('/ledger', params=query).json()
        assert first['compression_count'] == (42 if scope == 'all' else 21)
        assert first['list_count'] == (20 if scope == 'all' else 10)
        assert len(first['compressions']) == 10
        assert all(row['kind'] == 'micro_compaction' for row in first['compressions'])
        full = client.get('/ledger', params={**query, 'list_mode': 'all'}).json()
        assert len(full['compressions']) == full['list_count']
        assert full['compression_count'] == first['compression_count']
        if scope == 'all':
            second = client.get('/ledger', params={**query, 'offset': 10}).json()
            assert first['compressions'] + second['compressions'] == full['compressions']


def test_full_limit_fails_explicitly_without_truncation(tmp_path):
    seed(tmp_path, 11)
    # Build >20k rows with minimal request data and no side effects outside this fixture.
    with sqlite3.connect(tmp_path / 'usage-ledger' / 'events.sqlite3') as conn:
        conn.executemany('INSERT INTO compressions VALUES(?,?,?,?,?,?,?,?)',
                         ((f'extra-{i}', 1, 2, 'synthetic', '', '', None,
                           json.dumps({'id': f'extra-{i}', 'started': 1, 'kind': 'compression'}))
                          for i in range(20000)))
    with client_for(tmp_path) as client:
        response = client.get('/ledger', params=dict(end=40, view='compressions', list_mode='all', limit=10))
        assert response.status_code == 400
        assert '20,000-row safety limit' in response.json()['detail']
        page = client.get('/ledger', params=dict(end=40, view='compressions', list_mode='page', limit=10)).json()
        assert len(page['compressions']) == 10
        assert page['list_count'] == 20011

@pytest.mark.parametrize('view,group,field,label', [
    ('models', None, 'groups', 'model'), ('overview','model','model_groups','model'),
    ('overview','project','project_groups','key'),
    ('overview','session','session_groups','key'),
    ('overview','subagent','subagent_groups','key'),
    ('cache',None,'applied_rate_groups','model'),
])
def test_all_profile_groups_select_newest_keys_before_hydration(tmp_path, monkeypatch, view, group, field, label):
    from _hermes_ai_usage_ledger_v2 import aggregate
    from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
    roots=[tmp_path,tmp_path/'profiles'/'other']
    for root in roots:
        seed(root,21)
        with sqlite3.connect(root/'usage-ledger'/'events.sqlite3') as conn:
            for i in range(21):
                conn.execute("UPDATE requests SET data=json_set(data,'$.agent_kind','subagent', '$.subagent_id', ?, '$.cost.rate.revision', ?) WHERE id=?",
                             (f'row-{i:03}',str(i),f'row-{i:03}'))
    runtime=AnalyticsRuntime()
    original=runtime._reader
    hydrated=[]
    def tracked(generation, root):
        reader=original(generation,root)
        read=reader.read
        def observed(*args,**kwargs):
            result=read(*args,**kwargs)
            hydrated.append((len(kwargs.get('_group_keys') or []),len(result[field])))
            return result
        reader.read=observed
        return reader
    monkeypatch.setattr(runtime,'_reader',tracked)
    try:
        inventory=aggregate.discover(tmp_path)
        query=dict(start=0,end=40,view=view,group=group,list_mode='page',limit=10)
        first=aggregate.ledger(runtime,inventory,**query)
        second=aggregate.ledger(runtime,inventory,**{**query,'offset':10})
        assert first['list_count']==(42 if field in ('groups','project_groups','session_groups','subagent_groups') else 21)
        assert len(first[field])==len(second[field])==10
        assert all(keys<=10 and rows<=10 for keys,rows in hydrated),hydrated
        assert first['summary']['attempts']==42==second['summary']['attempts']
        if field in ('groups','project_groups','session_groups','subagent_groups'):
            expected=[f'{"task" if field=="groups" else "project" if field=="project_groups" else "row"}-{i if field in ("groups","project_groups") else f"{i:03}"}' for i in range(20,15,-1) for _ in roots]
            actual=[r.get('original_ids',{}).get('task' if field=='groups' else label,r[label]) for r in first[field]]
            assert actual==expected
        else:
            assert [r[label] for r in first[field]]==[f'model-{i}' for i in range(20,10,-1)]
            assert all(r['attempts']==2 for r in first[field])
    finally:
        runtime._discard(runtime.current)


def test_group_recency_is_latest_matching_member_and_qualified_scope(tmp_path):
    seed(tmp_path,21)
    seed(tmp_path/'profiles'/'other',21)
    with sqlite3.connect(tmp_path/'usage-ledger'/'events.sqlite3') as conn:
        row=json.loads(conn.execute("SELECT data FROM requests WHERE id='row-000'").fetchone()[0])
        row.update(id='new-old-group',started=39,ended=39.5)
        conn.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                     (row['id'],39,39.5,row['provider'],row['model'],row['session_id'],row['task'],None,'completed',json.dumps(row)))
    with client_for(tmp_path) as client:
        selected=client.get('/ledger',params=dict(profile='default',end=40,view='overview',group='model',
                                                   list_mode='page',limit=10)).json()
        both=client.get('/ledger',params=dict(profile_scope='all',end=40,view='overview',group='model',
                                               list_mode='page',limit=10)).json()
        assert [r['model'] for r in selected['model_groups']]==['model-0']+[f'model-{i}' for i in range(20,11,-1)]
        assert [r['model'] for r in both['model_groups']]==[r['model'] for r in selected['model_groups']]
        assert both['model_groups'][0]['attempts']==3
        assert both['summary']['attempts']==43 and both['list_count']==21
        earlier=client.get('/ledger',params=dict(profile_scope='all',end=30,view='overview',group='model',
                                                  list_mode='page',limit=10)).json()
        assert [r['model'] for r in earlier['model_groups']]==[f'model-{i}' for i in range(20,10,-1)]


def test_rate_revisions_with_same_model_merge_only_matching_saved_rates(tmp_path):
    seed(tmp_path,2)
    seed(tmp_path/'profiles'/'other',2)
    for root in (tmp_path,tmp_path/'profiles'/'other'):
        with sqlite3.connect(root/'usage-ledger'/'events.sqlite3') as conn:
            conn.execute("UPDATE requests SET data=json_set(data,'$.response_model','shared', '$.cost.rate.revision', id)")
    with client_for(tmp_path) as client:
        result=client.get('/ledger',params=dict(profile_scope='all',end=40,view='cache',
                                                list_mode='page',limit=1)).json()
        assert result['list_count']==2
        assert len(result['applied_rate_groups'])==1
        assert result['applied_rate_groups'][0]['rate']['revision']=='row-001'
        assert result['applied_rate_groups'][0]['attempts']==2
        next_page=client.get('/ledger',params=dict(profile_scope='all',end=40,view='cache',
                                                   list_mode='page',limit=1,offset=1)).json()
        assert next_page['applied_rate_groups'][0]['rate']['revision']=='row-000'
        assert next_page['applied_rate_groups'][0]['attempts']==2
        assert next_page['summary']==result['summary']


def test_equivalent_rate_serialisations_have_one_bounded_group(tmp_path, monkeypatch):
    from _hermes_ai_usage_ledger_v2 import aggregate
    from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
    seed(tmp_path,21)
    seed(tmp_path/'profiles'/'other',21)
    for root in (tmp_path,tmp_path/'profiles'/'other'):
        with sqlite3.connect(root/'usage-ledger'/'events.sqlite3') as conn:
            for i in range(21):
                key=f'row-{i:03}'
                row=json.loads(conn.execute('SELECT data FROM requests WHERE id=?',(key,)).fetchone()[0])
                row['response_model']='shared'
                row['cost']['rate']=({'revision':'same','source':'synthetic'} if i%2
                                     else {'source':'synthetic','revision':'same'})
                conn.execute('UPDATE requests SET data=? WHERE id=?',(json.dumps(row),key))
    runtime=AnalyticsRuntime()
    original=runtime._reader
    hydrated=[]
    def tracked(generation, root):
        reader=original(generation,root)
        read=reader.read
        def observed(*args,**kwargs):
            result=read(*args,**kwargs)
            hydrated.append(len(result['applied_rate_groups']))
            return result
        reader.read=observed
        return reader
    monkeypatch.setattr(runtime,'_reader',tracked)
    try:
        result=aggregate.ledger(runtime,aggregate.discover(tmp_path),start=0,end=40,
                                view='cache',list_mode='page',limit=10)
        assert result['list_count']==len(result['applied_rate_groups'])==1
        assert result['applied_rate_groups'][0]['attempts']==42
        assert hydrated==[1,1]
    finally:
        runtime._discard(runtime.current)


@pytest.mark.parametrize('view,group,fragment', [
    ('requests', None, 'SELECT data FROM requests WHERE'),
    ('compressions', None, 'SELECT data FROM compressions WHERE'),
    ('models', None, 'GROUP BY provider,'),
    ('overview', 'model', 'GROUP BY provider,'),
    ('overview', 'project', 'GROUP BY COALESCE('),
    ('cache', None, 'applied_tier,saved_rate'),
])
def test_selected_page_sql_fetches_bounded_rows(tmp_path, monkeypatch, view, group, fragment):
    from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
    seed(tmp_path, 21)
    runtime = AnalyticsRuntime()
    original = runtime._reader
    statements = []

    def traced(generation, root):
        reader = original(generation, root)
        db = reader.db

        @contextmanager
        def bounded_db():
            with db() as conn:
                conn.set_trace_callback(statements.append)
                yield conn

        reader.db = bounded_db
        return reader

    monkeypatch.setattr(runtime, '_reader', traced)
    try:
        report = runtime.read(tmp_path, 0, 40, '', 0, 10, view=view, group=group, list_mode='page')
        assert report['list_count'] == 21
        assert any(fragment in statement and 'LIMIT 10 OFFSET 0' in statement
                   for statement in statements), (view, group)
    finally:
        runtime._discard(runtime.current)
