"""Native lifecycle attribution; synthetic events only, no accounts or inference."""
import hashlib, json, sqlite3, time, types
from pathlib import Path
import pytest
from fastapi import FastAPI, APIRouter
from fastapi.testclient import TestClient
from _hermes_ai_usage_ledger_v2.storage import Store
from _hermes_ai_usage_ledger_v2 import attribution as a, recorder as r, adapters as ad
from _hermes_ai_usage_ledger_v2.api import add_routes

@pytest.fixture
def root(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    r._STORES.clear();r._LOOKUP.clear();r.CURRENT.set(None);r.AUX.set(None);r.COMPRESSION.set(None)
    c=sqlite3.connect(tmp_path/'state.db')
    c.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,parent_session_id TEXT,cwd TEXT,git_repo_root TEXT,title TEXT,system_prompt TEXT)')
    c.executemany('INSERT INTO sessions VALUES(?,?,?,?,?,?,?)',[
      ('root','desktop',None,'/projects/alpha/src','/projects/alpha','PRIVATE_TITLE','SECRET_PROMPT'),
      ('child','subagent','root','/tmp/worktree/alpha',None,'PRIVATE_TITLE','SECRET_PROMPT'),
      ('grandchild','subagent','child','/tmp/worker',None,'PRIVATE_TITLE','SECRET_PROMPT'),
      ('other','desktop',None,'/projects/beta',None,'PRIVATE_TITLE','SECRET_PROMPT'),
      ('branch','desktop','root','/projects/alpha/src','/projects/alpha','PRIVATE_TITLE','SECRET_PROMPT'),
      ('orphan','subagent','missing',None,None,'PRIVATE_TITLE','SECRET_PROMPT')])
    c.commit();c.close();return tmp_path

def usage(n):
    return {'input_tokens':n,'output_tokens':10,'cache_read_tokens':100,'cache_write_tokens':0,'prompt_tokens':n+100,'total_tokens':n+110,'reasoning_tokens':0,'request_count':1}

def record(s,sid,key,n=100,provider='openai-codex',ts=None,platform=''):
    ctx=a.capture(s,sid,platform)
    s.request(dict(id=key,session_id=sid,provider=provider,model='m',task='main',started=ts or time.time()-1,ended=time.time(),status='completed',usage=usage(n),**ctx),'request_completed')

def test_primary_child_grandchild(root):
    s=Store(root)
    for sid in ['root','child','grandchild']:record(s,sid,sid)
    d=s.read();assert d['summary']['known']['total_tokens']==630
    assert d['subagent_summary']['known']['total_tokens']==420
    assert d['subagent_summary']['agents']==2
    child=next(x for x in d['requests'] if x['id']=='grandchild')
    assert child['parent_session_id']=='child' and child['root_session_id']=='root'
    assert child['session_lineage']==['child','root']
    assert child['project_path']=='/projects/alpha' and child['project_source']=='parent_repository'
    assert d['session_groups'][0]['subagents']==2

def test_parent_link_not_proof_of_subagent(root):
    s=Store(root);record(s,'branch','b')
    d=s.read();assert d['subagent_summary']['attempts']==0
    assert d['requests'][0]['agent_kind']=='primary'

def test_unknown_not_guessed_from_task_name(root):
    s=Store(root);s.request({'id':'u','session_id':'not-recorded','task':'subagent','usage':usage(20)},'request_completed')
    d=s.read(agent='unknown');assert d['request_count']==1
    assert d['subagent_summary']['attempts']==0

def test_lifecycle_handles_before_child_db_row(root):
    r.subagent_start(parent_session_id='root',child_session_id='new-child',child_subagent_id='worker-x',child_role='leaf',child_goal='SECRET_GOAL')
    r.pre(session_id='new-child',platform='subagent',api_request_id='x',provider='openai-codex')
    r.post(session_id='new-child',platform='subagent',api_request_id='x',provider='openai-codex',usage=usage(100))
    d=Store(root).read();rec=d['requests'][0]
    assert rec['agent_kind']=='subagent' and rec['parent_session_id']=='root' and rec['project_label']=='alpha'
    assert rec['subagent_id']=='worker-x'
    assert 'SECRET_GOAL' not in json.dumps(d)

def test_stop_does_not_add_tokens_or_private_content(root):
    r.subagent_start(parent_session_id='root',child_session_id='child',child_subagent_id='worker',child_role='leaf')
    s=Store(root);record(s,'child','c')
    r.subagent_stop(parent_session_id='root',child_session_id='child',child_status='completed',duration_ms=1000,child_summary='SECRET_SUMMARY',tool_call_history=[{'input':'SECRET_TOOL'}])
    r.subagent_stop(parent_session_id='root',child_session_id='child',child_status='completed',duration_ms=1000)
    d=s.read();assert d['request_count']==1 and d['summary']['known']['total_tokens']==210
    assert d['requests'][0]['agent_role']=='leaf'
    with s.db() as c:raw=' '.join(x[0] for x in c.execute('SELECT data FROM events'))
    assert 'SECRET_SUMMARY' not in raw and 'SECRET_TOOL' not in raw

def test_late_lifecycle_fills_identity_not_usage(root):
    s=Store(root);record(s,'unknown-child','first')
    assert s.read()['requests'][0]['agent_kind']=='unknown'
    r.subagent_start(parent_session_id='root',child_session_id='unknown-child',child_subagent_id='new-agent',child_role='orchestrator')
    d=s.read();assert d['requests'][0]['agent_kind']=='subagent'
    assert d['subagent_summary']['known']['total_tokens']==210 and d['request_count']==1

def test_family_exact_and_subagent_filters(root):
    s=Store(root)
    for sid in ['root','child','grandchild','other']:record(s,sid,sid)
    assert s.read(session='root',session_scope='exact')['request_count']==1
    assert s.read(session='root',session_scope='family')['request_count']==3
    assert s.read(session='child',session_scope='family')['request_count']==2
    assert s.read(agent='subagent')['request_count']==2
    assert s.read(agent='primary')['request_count']==2

def test_project_and_role_filters_compose(root):
    s=Store(root)
    for sid in ['root','child','grandchild','other']:record(s,sid,sid)
    project=next(p for p in s.read()['project_options'] if p['label']=='alpha')
    d=s.read(project=project['id'],agent='subagent')
    assert d['request_count']==2 and len(d['model_groups'])==1 and len(d['project_groups'])==1
    assert d['subagent_summary']['attempts']==2

def test_time_and_provider_filters(root):
    s=Store(root);t=time.time()
    record(s,'child','old',ts=t-500)
    record(s,'grandchild','new',ts=t-1,provider='openrouter')
    assert s.read(start=t-100,provider='openrouter')['subagent_summary']['attempts']==1
    assert s.read(start=t-100,provider='openai-codex')['subagent_summary']['attempts']==0

def test_directory_fallback_distinct_and_labelled(root):
    s=Store(root);record(s,'other','x');x=s.read()['requests'][0]
    assert x['project_label']=='beta' and x['project_source']=='working_directory'
    assert x['project_id'] and x['root_session_id']=='other'

def test_missing_ancestor_does_not_claim_root(root):
    s=Store(root);record(s,'orphan','x');x=s.read()['requests'][0]
    assert x['parent_session_id']=='missing' and x['root_session_id'] is None
    assert x['session_lineage']==['missing'] and not x['lineage_complete']
    assert x['project_id'] is None

def test_cycle_bounded(root):
    c=sqlite3.connect(root/'state.db');c.execute("UPDATE sessions SET parent_session_id='grandchild' WHERE id='root'");c.commit();c.close()
    s=Store(root);record(s,'child','x');x=s.read()['requests'][0]
    assert not x['lineage_complete'] and x['root_session_id'] is None
    assert len(x['session_lineage'])<=3

def test_no_state_modification_or_prompt_reads(root):
    path=root/'state.db';before=hashlib.sha256(path.read_bytes()).hexdigest()
    s=Store(root);record(s,'grandchild','x')
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    with s.db() as c:raw=' '.join(x[0] for x in c.execute('SELECT data FROM session_context'))
    assert 'SECRET_PROMPT' not in raw and 'PRIVATE_TITLE' not in raw
    assert 'title' not in raw and 'system_prompt' not in raw

def test_two_profiles_isolated(root,tmp_path):
    s=Store(root);record(s,'child','x')
    other=Store(tmp_path/'isolated');record(other,'child','y')
    assert other.read()['requests'][0]['agent_kind']=='unknown'
    assert other.read()['project_options'][0]['id']=='unattributed'

def test_auxiliary_stays_with_child(root):
    s=Store(root);a.capture(s,'child','subagent')
    # Helper routes use the session accounting context, not task-name guessing.
    import contextvars
    m=types.SimpleNamespace(_RUNTIME_MAIN_CONTEXT=contextvars.ContextVar('test-runtime',default={'session_id':'child'}))
    item=ad.aux_begin(m,{'model':'m'},{'provider':'openai-codex'})
    ad.aux_end(item,{'usage':{'input_tokens':100,'output_tokens':10,'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0}}})
    d=s.read();assert d['subagent_summary']['attempts']==1
    assert d['requests'][0]['task']=='auxiliary'

def test_stable_subagent_id_across_sessions(root):
    s=Store(root)
    for sid in ['child','child-after-compaction']:
        r.subagent_start(parent_session_id='root',child_session_id=sid,child_subagent_id='same-worker',child_role='leaf')
        record(s,sid,sid)
    d=s.read(subagent='same-worker');assert d['request_count']==2
    assert d['subagent_summary']['agents']==1 and d['subagent_summary']['sessions']==2
    assert len(d['subagent_groups'])==1

def test_request_identity_and_project_snapshot_immutable(root):
    s=Store(root);record(s,'root','x');before=s.read()['requests'][0]
    c=sqlite3.connect(root/'state.db');c.execute("UPDATE sessions SET git_repo_root='/projects/changed' WHERE id='root'");c.commit();c.close()
    a.capture(s,'root');s.request({'id':'x','status':'completed'},'request_completed')
    assert s.read()['requests'][0]['project_id']==before['project_id']

def test_compression_filters_follow_child(root):
    s=Store(root);a.capture(s,'child','subagent')
    s.compression({'id':'comp','session_id':'child','provider':'openai-codex','started':time.time()-1,'kind':'compression'})
    d=s.read(session='root',session_scope='family',agent='subagent')
    assert d['compression_count']==1
    assert s.read(agent='primary')['compression_count']==0

def test_api_parameters_validation_and_matching(root):
    s=Store(root);record(s,'child','x');record(s,'root','y')
    router=APIRouter();add_routes(router,lambda p:(root,None,None),lambda:root)
    app=FastAPI();app.include_router(router);client=TestClient(app)
    assert client.get('/ledger',params={'agent':'subagent'}).json()['request_count']==1
    assert client.get('/ledger',params={'session':'root','session_scope':'family'}).json()['request_count']==2
    assert client.get('/ledger',params={'agent':'bad'}).status_code==400
    assert client.get('/ledger',params={'session_scope':'bad'}).status_code==400

def test_full_rollups_not_paginated(root):
    s=Store(root)
    for i in range(8):record(s,'child','x'+str(i))
    d=s.read(limit=1)
    assert len(d['requests'])==1 and d['subagent_summary']['attempts']==8
    assert d['subagent_groups'][0]['attempts']==8
    assert d['project_groups'][0]['known']['total_tokens']==8*210
