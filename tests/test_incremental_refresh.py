"""Disposable ledger contract for signed fixed-window Overview/time refresh."""
import json
import sqlite3

import pytest

from ledger_runtime.analytics_reload import AnalyticsRuntime
from ledger_runtime.incremental import CAPACITY, MAX_ROW_BYTES, MAX_JOURNAL_BYTES, changes, supported
from ledger_runtime.storage import SCHEMA, Store
from ledger_runtime.skills import SCHEMA as SKILLS_SCHEMA

ARGS=(100., 50_000., '', 0, 50, '', '', '', 'exact', '', '', '')


def put(store, key, started, reads, *, session='shared', status='completed', **extra):
    row=dict(id=key,started=started,ended=started+1,provider='fixture',model='model',
             session_id=session,source='main_hook',task='main',status=status,
             usage={'cache_read_tokens':reads,'prompt_tokens':reads,'total_tokens':reads},**extra)
    store.request(row,'request_completed')


def comparable(response):
    return {k:v for k,v in response.items() if k not in ('generated_at','incremental')}


def fixture(tmp_path):
    store=Store(tmp_path)
    for i in range(12):
        put(store,str(i),200+i*3600,i*100,session='shared' if i<8 else 'other')
    runtime=AnalyticsRuntime()
    baseline=runtime.read(tmp_path,*ARGS,view='overview',group='time')
    assert baseline['incremental']['resume_token']
    return store,runtime,baseline


def parity(store,runtime,baseline, mutate):
    mutate(store)
    refreshed=runtime.refresh(store.root,*ARGS,resume_token=baseline['incremental']['resume_token'])
    expected=runtime.read(store.root,*ARGS,view='overview',group='time')
    assert comparable(refreshed)==comparable(expected)
    return refreshed


def test_new_and_corrected_request_use_bounded_reads_not_full_scope(tmp_path):
    store,runtime,base=fixture(tmp_path)
    seen=[]
    original=runtime.current['store_type'].read
    def traced(self,*args,**kwargs):
        seen.append(args[:2])
        return original(self,*args,**kwargs)
    runtime.current['store_type'].read=traced
    try:
        changed=parity(store,runtime,base,lambda s:put(s,'new',205+7*3600,888,session='shared'))
        assert changed['incremental']['mode']=='delta'
        assert seen and seen.count(ARGS[:2])==1  # only parity oracle, not refresh
        seen.clear()
        updated=parity(store,runtime,changed,
                       lambda s:put(s,'3',200+3*3600,999,session='shared'))
        assert updated['incremental']['mode']=='delta'
        assert seen and seen.count(ARGS[:2])==1
    finally:
        runtime.current['store_type'].read=original


def test_outside_scope_predecessor_and_late_usage_cascade(tmp_path):
    store=Store(tmp_path)
    put(store,'before',50,10)
    put(store,'inside',200,100)
    runtime=AnalyticsRuntime()
    first=runtime.read(tmp_path,*ARGS,view='overview',group='time')
    updated=parity(store,runtime,first,lambda s:put(s,'before',50,80))
    assert updated['incremental']['mode']=='delta'
    assert updated['summary']['session_cache_writes']['tokens']==20
    next_one=parity(store,runtime,updated,lambda s:put(s,'inside',200,130))
    assert next_one['incremental']['mode']=='delta'


def test_unknowns_decimal_distinct_membership_and_rolling_expiry(tmp_path):
    store,runtime,base=fixture(tmp_path)
    def mutate(s):
        with s.db() as c:
            old=json.loads(c.execute("SELECT data FROM requests WHERE id='2'").fetchone()[0])
            old['cost']={'known_components_usd':'0.000000000000000001',
                         'components':{'input_tokens':'0.000000000000000001'}}
            old['usage']['total_tokens']=None
            c.execute('UPDATE requests SET data=? WHERE id=?',(json.dumps(old),'2'))
    changed=parity(store,runtime,base,mutate)
    assert changed['incremental']['mode']=='delta'
    assert changed['summary']['missing_usage']==1
    assert changed['summary']['known_cost_usd']=='1E-18'
    # Scope change cannot reuse a signed baseline; a different rolling bound
    # requires a new full snapshot instead of an approximate bucket subtraction.
    with pytest.raises(ValueError,match='scope'):
        runtime.refresh(tmp_path,200+3600.,ARGS[1],*ARGS[2:],
                        resume_token=changed['incremental']['resume_token'])


def test_high_precision_cost_disables_nonassociative_bucket_merge(tmp_path):
    store,runtime,base=fixture(tmp_path)
    with store.db() as c:
        row=json.loads(c.execute("SELECT data FROM requests WHERE id='2'").fetchone()[0])
        row['cost']={'known_components_usd':'0.0000000000000000000000001'}
        c.execute('UPDATE requests SET data=? WHERE id=?',(json.dumps(row),'2'))
    refreshed=runtime.refresh(tmp_path,*ARGS,resume_token=base['incremental']['resume_token'])
    assert refreshed['incremental']['mode']=='snapshot'
    assert 'resume_token' not in refreshed['incremental']
    assert comparable(refreshed)==comparable(runtime.read(tmp_path,*ARGS,view='overview',group='time'))


def test_non_request_writers_and_missing_history_fall_back(tmp_path):
    store,runtime,base=fixture(tmp_path)
    with store.db() as c:
        c.execute('INSERT INTO health VALUES(?,?,?)',('worker',1,'{}'))
    changed=runtime.refresh(tmp_path,*ARGS,resume_token=base['incremental']['resume_token'])
    assert changed['incremental']['mode']=='snapshot'
    with store.db() as c:
        # Force retained history to advance beyond the old signed watermark.
        c.executemany("INSERT INTO events(ts,kind,item_id,data) VALUES(1,'other','x','{}')",
                      [()] * (CAPACITY+2))
        assert changes(c,base['incremental']['revision']) is None
    stale=runtime.refresh(tmp_path,*ARGS,resume_token=base['incremental']['resume_token'])
    assert stale['incremental']['mode']=='snapshot'


@pytest.mark.parametrize('mutation',[
    lambda c: c.execute("DELETE FROM requests WHERE id='2'"),
    lambda c: c.execute("UPDATE requests SET data=json_set(data,'$.project_id','reassigned') WHERE id='2'"),
    lambda c: c.execute("UPDATE requests SET provider='new-provider' WHERE id='2'"),
    lambda c: c.execute("INSERT INTO pricing_status VALUES('source','{}')"),
])
def test_tombstone_source_rewrite_and_pricing_invalidate(tmp_path,mutation):
    store,runtime,base=fixture(tmp_path)
    with store.db() as c:
        mutation(c)
    refreshed=runtime.refresh(tmp_path,*ARGS,resume_token=base['incremental']['resume_token'])
    assert refreshed['incremental']['mode']=='snapshot'
    assert comparable(refreshed)==comparable(runtime.read(tmp_path,*ARGS,view='overview',group='time'))


def test_internal_gap_and_duplicate_writes_reject_unsupported_history(tmp_path):
    store,runtime,base=fixture(tmp_path)
    put(store,'new1',360,10)
    put(store,'new2',370,20)
    with store.db() as c:
        high=c.execute('SELECT MAX(revision) FROM analytics_changes').fetchone()[0]
        c.execute('DELETE FROM analytics_changes WHERE revision=?',(high-1,))
        assert changes(c,base['incremental']['revision']) is None
    assert runtime.refresh(tmp_path,*ARGS,resume_token=base['incremental']['resume_token'])['incremental']['mode']=='snapshot'


def test_subagent_distinct_and_bucket_crossing_correction(tmp_path):
    store=Store(tmp_path)
    put(store,'one',300,100,session='same',agent_kind='subagent',subagent_id='child')
    put(store,'two',3900,200,session='same',agent_kind='subagent',subagent_id='child')
    runtime=AnalyticsRuntime()
    base=runtime.read(tmp_path,*ARGS,view='overview',group='time')
    def move(s):
        with s.db() as c:
            row=json.loads(c.execute("SELECT data FROM requests WHERE id='one'").fetchone()[0])
            row['started']=7500
            row['ended']=7501
            row['usage']['cache_read_tokens']=150
            c.execute('UPDATE requests SET started=?,ended=?,data=? WHERE id=?',
                      (7500,7501,json.dumps(row),'one'))
    changed=parity(store,runtime,base,move)
    assert changed['incremental']['mode']=='delta'
    assert changed['subagent_summary']['agents']==1
    assert changed['summary']['sessions']==1


def test_writer_change_between_bucket_read_and_commit_forces_full_retry(tmp_path):
    store,runtime,base=fixture(tmp_path)
    put(store,'new',205+7*3600,888,session='shared')
    original=runtime.current['store_type'].read
    injected=False
    def raced(self,*args,**kwargs):
        nonlocal injected
        result=original(self,*args,**kwargs)
        if not injected and args[0]!=ARGS[0]:
            injected=True
            put(store,'later',200+8*3600,999,session='other')
        return result
    runtime.current['store_type'].read=raced
    try:
        refreshed=runtime.refresh(tmp_path,*ARGS,resume_token=base['incremental']['resume_token'])
    finally:
        runtime.current['store_type'].read=original
    assert injected
    assert refreshed['incremental']['mode']=='snapshot'
    assert comparable(refreshed)==comparable(runtime.read(tmp_path,*ARGS,view='overview',group='time'))


def test_route_signed_refresh_and_scope_rejection(tmp_path):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient
    from ledger_runtime.api import add_routes
    store=Store(tmp_path)
    put(store,'a',300,10)
    router=APIRouter()
    add_routes(router,lambda profile:(tmp_path,'default',None),lambda:tmp_path)
    app=FastAPI()
    app.include_router(router)
    client=TestClient(app)
    query={'start':100,'end':50000,'view':'overview','group':'time'}
    base=client.get('/ledger',params=query)
    assert base.status_code==200
    put(store,'b',400,20)
    delta=client.post('/ledger/refresh',params=query,
                      json={'resume_token':base.json()['incremental']['resume_token']})
    assert delta.status_code==200
    assert delta.json()['incremental']['mode']=='delta'
    expected=client.get('/ledger',params=query).json()
    assert comparable(delta.json())==comparable(expected)
    bad=client.post('/ledger/refresh',params={**query,'provider':'another'},
                    json={'resume_token':base.json()['incremental']['resume_token']})
    assert bad.status_code==400
    alias=client.post('/ledger/refresh',params={**query,'profile':'same-root-alias'},
                      json={'resume_token':base.json()['incremental']['resume_token']})
    assert alias.status_code==400


def test_two_first_openers_have_one_complete_changefeed(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    gate=Barrier(2)
    def open_store(_):
        gate.wait()
        return Store(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        stores=list(pool.map(open_store,range(2)))
    with stores[0].db() as c:
        assert supported(c)
        assert c.execute('SELECT COUNT(*) FROM analytics_change_meta').fetchone()[0]==1


def test_rollback_duplicate_tamper_missing_trigger_and_unmigrated_db(tmp_path):
    store,runtime,base=fixture(tmp_path)
    with pytest.raises(RuntimeError):
        with store.db() as c:
            c.execute('UPDATE requests SET status=? WHERE id=?',('abandoned','1'))
            raise RuntimeError('rollback')
    same=runtime.refresh(tmp_path,*ARGS,resume_token=base['incremental']['resume_token'])
    assert same['incremental']['mode']=='delta'
    assert comparable(same)==comparable(base)
    token=base['incremental']['resume_token']
    with pytest.raises(ValueError,match='Invalid analytics resume token'):
        runtime.refresh(tmp_path,*ARGS,resume_token=token[:-4]+'AAAA')
    with store.db() as c:
        c.execute('DROP TRIGGER analytics_requests_update')
        assert not supported(c)
    assert 'incremental' not in runtime.refresh(tmp_path,*ARGS,resume_token=token)
    legacy=tmp_path/'legacy'/'usage-ledger'
    legacy.mkdir(parents=True)
    with sqlite3.connect(legacy/'events.sqlite3') as c:
        c.executescript(SCHEMA+SKILLS_SCHEMA)
    Store(tmp_path/'legacy')
    with sqlite3.connect(legacy/'events.sqlite3') as c:
        assert not supported(c)
    assert 'incremental' not in runtime.read(tmp_path/'legacy',*ARGS,view='overview',group='time')


def test_replaced_file_with_copied_identity_and_revision_forces_full(tmp_path):
    import os
    import shutil
    store,runtime,base=fixture(tmp_path)
    source=store.path
    replacement=source.with_name('replacement.sqlite3')
    with store.db() as c:
        c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    shutil.copyfile(source,replacement)
    # The replacement has the same durable ID and revision but a different inode.
    os.replace(replacement,source)
    refreshed=runtime.refresh(tmp_path,*ARGS,resume_token=base['incremental']['resume_token'])
    assert refreshed['incremental']['mode']=='snapshot'
    assert comparable(refreshed)==comparable(runtime.read(tmp_path,*ARGS,view='overview',group='time'))


def test_offline_migration_opt_in_only_on_fixture(tmp_path):
    from ledger_runtime.incremental_migration import migrate_offline
    import hashlib
    folder=tmp_path/'usage-ledger'
    folder.mkdir()
    path=folder/'events.sqlite3'
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA+SKILLS_SCHEMA)
    with pytest.raises(ValueError,match='confirmation'):
        migrate_offline(path)
    with sqlite3.connect(path) as c:
        assert not supported(c)
    backup=tmp_path/'verified.sqlite3'
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    proof=hashlib.sha256(backup.read_bytes()).hexdigest()
    options=dict(confirmed=True,offline_target=path,backup=backup,backup_sha256=proof)
    with pytest.raises(ValueError,match='backup'):
        migrate_offline(path,confirmed=True)
    migrate_offline(path,**options)
    with sqlite3.connect(path) as c:
        assert supported(c)
    migrate_offline(path,**options)  # already-upgraded target is a no-op


@pytest.mark.parametrize('corrupt',[
    "ALTER TABLE requests RENAME TO requests_old",
    "DROP TABLE requests",
    "PRAGMA user_version=99",
    "DROP TABLE skill_events",
])
def test_migration_rejects_incompatible_schema_and_preserves_source(tmp_path,corrupt):
    import hashlib
    from ledger_runtime.incremental_migration import migrate_offline
    path=tmp_path/'events.sqlite3'
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA+SKILLS_SCHEMA)
        c.execute(corrupt)
    backup=tmp_path/'backup.sqlite3'
    with sqlite3.connect(path) as source,sqlite3.connect(backup) as target:
        source.backup(target)
    proof=hashlib.sha256(backup.read_bytes()).hexdigest()
    with pytest.raises(ValueError,match='schema|version'):
        migrate_offline(path,confirmed=True,offline_target=path,backup=backup,backup_sha256=proof)
    with sqlite3.connect(path) as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='analytics_changes'").fetchone()


def test_migration_refuses_alias_stale_backup_and_busy_writer(tmp_path):
    import hashlib
    from ledger_runtime.incremental_migration import migrate_offline
    path=tmp_path/'events.sqlite3'
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA+SKILLS_SCHEMA)
    backup=tmp_path/'backup.sqlite3'
    with sqlite3.connect(path) as source,sqlite3.connect(backup) as target:
        source.backup(target)
    proof=hashlib.sha256(backup.read_bytes()).hexdigest()
    options=dict(confirmed=True,offline_target=path,backup=backup,backup_sha256=proof)
    alias=tmp_path/'alias.sqlite3'
    alias.symlink_to(path)
    with pytest.raises(ValueError,match='symlink|target'):
        migrate_offline(alias,**options)
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO requests VALUES('x',1,NULL,'p','m','s','main',NULL,'completed','{}')")
    with pytest.raises(ValueError,match='backup'):
        migrate_offline(path,**options)
    with sqlite3.connect(path) as source,sqlite3.connect(backup) as target:
        source.backup(target)
    options['backup_sha256']=hashlib.sha256(backup.read_bytes()).hexdigest()
    writer=sqlite3.connect(path)
    writer.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(sqlite3.OperationalError):
            migrate_offline(path,**options)
    finally:
        writer.rollback()
        writer.close()
    with sqlite3.connect(path) as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='analytics_changes'").fetchone()


def test_selected_get_and_refresh_include_committed_wal_without_mutating_rows(tmp_path):
    store,runtime,base=fixture(tmp_path)
    put(store,'late',300,100)
    folder=store.folder
    # A writer may leave a live WAL: it must be read, never ignored by immutable=1.
    import sqlite3
    writer=sqlite3.connect(store.path)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                   ('wal',400,401,'fixture','model','shared','main',None,'completed',
                    json.dumps({'id':'wal','started':400,'ended':401,'provider':'fixture',
                                'model':'model','session_id':'shared','status':'completed',
                                'usage':{'total_tokens':5}})))
    writer.commit()
    from ledger_runtime.read_snapshot import signature
    files=[folder/('events.sqlite3'+suffix) for suffix in ('','-wal','-shm')]
    before_rows=writer.execute('SELECT COUNT(*),SUM(started) FROM requests').fetchone()
    refreshed=runtime.refresh(tmp_path,*ARGS,resume_token=base['incremental']['resume_token'])
    fresh=runtime.read(tmp_path,*ARGS,view='overview',group='time')
    assert refreshed['request_count']==fresh['request_count']==base['request_count']+2
    assert writer.execute('SELECT COUNT(*),SUM(started) FROM requests').fetchone()==before_rows
    assert all(signature(p) is not None for p in files)
    writer.close()


def test_oversize_invalidates_and_retention_bounds_both_payloads(tmp_path):
    store,runtime,base=fixture(tmp_path)
    with store.db() as c:
        row=json.loads(c.execute("SELECT data FROM requests WHERE id='2'").fetchone()[0])
        row['extra']='x'*(MAX_ROW_BYTES+1)
        c.execute('UPDATE requests SET data=? WHERE id=?',(json.dumps(row),'2'))
        assert c.execute('SELECT kind FROM analytics_changes ORDER BY revision DESC LIMIT 1').fetchone()[0]=='oversize'
        assert changes(c,base['incremental']['revision']) is None
        # Multiple large-but-admissible old/new images exercise the byte cap.
        row['extra']='y'*16000
        for i in range(300):
            row['extra']=str(i)+'y'*16000
            c.execute('UPDATE requests SET data=? WHERE id=?',(json.dumps(row),'2'))
        size,count=c.execute('SELECT SUM(length(CAST(COALESCE(old_row,\'\') AS BLOB)))+'
                             'SUM(length(CAST(COALESCE(new_row,\'\') AS BLOB)))+'
                             'SUM(length(CAST(COALESCE(item_id,\'\') AS BLOB)))+'
                             'SUM(length(CAST(kind AS BLOB)))+64*COUNT(*),COUNT(*) '
                             'FROM analytics_changes').fetchone()
        assert size<=MAX_JOURNAL_BYTES and count<=CAPACITY
    assert runtime.refresh(tmp_path,*ARGS,resume_token=base['incremental']['resume_token'])['incremental']['mode']=='snapshot'


@pytest.mark.parametrize('group',['time','model'])
def test_rolling_day_expiry_and_model_corrections_no_full_read(tmp_path,group):
    store=Store(tmp_path)
    start=1_000_000.
    args=(start,start+86400.,'',0,50,'','','','exact','','','')
    put(store,'expiring',start+30,10,session='gone',response_model='old')
    put(store,'still',start+3700,20,session='shared',response_model='m')
    put(store,'unknown',start+86300,30,session='shared',response_model='')
    with store.db() as c:
        data=json.loads(c.execute("SELECT data FROM requests WHERE id='unknown'").fetchone()[0])
        data['model']=''
        c.execute("UPDATE requests SET model='',data=? WHERE id='unknown'",(json.dumps(data),))
    runtime=AnalyticsRuntime()
    base=runtime.read(tmp_path,*args,view='overview',group=group)
    advanced=(start+100,start+86500,*args[2:])
    put(store,'entering',start+86420,40,session='new',response_model='m')
    put(store,'still',start+3700,25,session='shared',response_model='corrected')
    original=runtime.current['store_type'].read
    widths=[]
    def traced(self,*pos,**kw):
        widths.append(pos[1]-pos[0])
        return original(self,*pos,**kw)
    runtime.current['store_type'].read=traced
    try:
        changed=runtime.refresh(tmp_path,*advanced,resume_token=base['incremental']['resume_token'],group=group)
    finally:
        runtime.current['store_type'].read=original
    expected=runtime.read(tmp_path,*advanced,view='overview',group=group)
    assert changed['incremental']['mode']=='delta'
    assert widths and all(w<=3600 for w in widths)
    assert comparable(changed)==comparable(expected)
    assert changed['summary']['sessions']==2
    if group=='model':
        assert {r['model'] for r in changed['model_groups']}=={'m','corrected','unknown'}
    next_bounds=(start+200,start+86600,*args[2:])
    unchanged=runtime.refresh(tmp_path,*next_bounds,resume_token=changed['incremental']['resume_token'],group=group)
    assert unchanged['incremental']['mode']=='delta'
    assert comparable(unchanged)==comparable(runtime.read(tmp_path,*next_bounds,view='overview',group=group))


def test_atomic_replacement_during_narrow_read_is_rejected(tmp_path):
    import os
    store,runtime,base=fixture(tmp_path)
    put(store,'new',205+7*3600,888)
    original=runtime.current['store_type'].read
    replaced=False
    def raced(self,*args,**kwargs):
        nonlocal replaced
        result=original(self,*args,**kwargs)
        if not replaced and args[0]!=ARGS[0]:
            replaced=True
            replacement=store.path.with_name('replacement.sqlite3')
            # A backup preserves the same DB identity and revision while
            # swapping the path generation after the snapshot was taken.
            with sqlite3.connect(store.path) as source, sqlite3.connect(replacement) as target:
                source.backup(target)
            os.replace(replacement,store.path)
        return result
    runtime.current['store_type'].read=raced
    try:
        refreshed=runtime.refresh(tmp_path,*ARGS,resume_token=base['incremental']['resume_token'])
    finally:
        runtime.current['store_type'].read=original
    assert replaced
    assert refreshed['incremental']['mode']=='snapshot'
    assert comparable(refreshed)==comparable(runtime.read(tmp_path,*ARGS,view='overview',group='time'))


def test_route_rolling_model_scope_and_page_guards(tmp_path):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient
    from ledger_runtime.api import add_routes
    store=Store(tmp_path)
    put(store,'a',1000200,10)
    router=APIRouter()
    add_routes(router,lambda profile:(tmp_path,'selected',None),lambda:tmp_path)
    app=FastAPI()
    app.include_router(router)
    client=TestClient(app)
    query=dict(start=1000000,end=1086400,view='overview',group='model',limit=25)
    base=client.get('/ledger',params=query).json()
    token=base['incremental']['resume_token']
    put(store,'b',1086450,20)
    advanced={**query,'start':1000100,'end':1086500}
    result=client.post('/ledger/refresh',params=advanced,json={'resume_token':token})
    assert result.status_code==200
    assert result.json()['incremental']['mode']=='delta'
    assert comparable(result.json())==comparable(client.get('/ledger',params=advanced).json())
    for bad in ({**advanced,'limit':26}, {**advanced,'start':999900,'end':1086300},
                {**advanced,'start':1000100,'end':1086800},
                {**advanced,'group':'time'}, {**advanced,'provider':'other'}):
        assert client.post('/ledger/refresh',params=bad,json={'resume_token':token}).status_code==400
    assert client.post('/ledger/refresh',params={**advanced,'profile_scope':'all'},
                       json={'resume_token':token}).json().get('incremental') is None


def test_effective_response_models_in_unchanged_bucket_after_other_correction(tmp_path):
    store=Store(tmp_path)
    args=(100., 50000., '', 0, 50, '', '', '', 'exact', '', '', '')
    put(store,'alpha',200,1,response_model='alpha')
    put(store,'beta',250,2,response_model='beta')
    put(store,'other',4000,3,response_model='alpha')
    runtime=AnalyticsRuntime()
    base=runtime.read(tmp_path,*args,view='overview',group='model')
    put(store,'other',4000,4,response_model='alpha')
    changed=runtime.refresh(tmp_path,*args,resume_token=base['incremental']['resume_token'],group='model')
    full=runtime.read(tmp_path,*args,view='overview',group='model')
    assert changed['incremental']['mode']=='delta'
    assert comparable(changed)==comparable(full)
    assert {row['model']:row['attempts'] for row in changed['model_groups']}=={'alpha':2,'beta':1}


@pytest.mark.parametrize('tamper',[
    "DROP TRIGGER analytics_requests_update",
    "DROP TRIGGER analytics_requests_delete",
    "DROP TRIGGER analytics_session_context_update",
    "DROP TRIGGER analytics_compressions_delete",
    "UPDATE analytics_change_meta SET version=99",
    "UPDATE analytics_change_meta SET identity=''",
])
def test_changefeed_tamper_fails_closed(tmp_path,tamper):
    store,runtime,base=fixture(tmp_path)
    with store.db() as c:
        c.execute(tamper)
        assert not supported(c)
        c.execute("UPDATE requests SET data=json_set(data,'$.usage.total_tokens',99) WHERE id='2'")
    refreshed=runtime.refresh(tmp_path,*ARGS,resume_token=base['incremental']['resume_token'])
    assert comparable(refreshed)==comparable(runtime.read(tmp_path,*ARGS,view='overview',group='time'))
    assert 'incremental' not in refreshed


def test_same_name_noop_trigger_cannot_hide_update_or_delete(tmp_path):
    store,runtime,base=fixture(tmp_path)
    with store.db() as c:
        c.execute('DROP TRIGGER analytics_requests_update')
        c.execute('CREATE TRIGGER analytics_requests_update AFTER UPDATE ON requests BEGIN SELECT 1; END')
        assert not supported(c)
        c.execute("UPDATE requests SET data=json_set(data,'$.usage.total_tokens',99) WHERE id='2'")
    result=runtime.refresh(tmp_path,*ARGS,resume_token=base['incremental']['resume_token'])
    assert 'incremental' not in result
    assert comparable(result)==comparable(runtime.read(tmp_path,*ARGS,view='overview',group='time'))
    with store.db() as c:
        c.execute('DROP TRIGGER analytics_requests_delete')
        c.execute('CREATE TRIGGER analytics_requests_delete AFTER DELETE ON events BEGIN SELECT 1; END')
        c.execute("DELETE FROM requests WHERE id='2'")
        assert not supported(c)
    assert 'incremental' not in runtime.read(tmp_path,*ARGS,view='overview',group='time')


@pytest.mark.parametrize('table,operation,mutation',[
    ('requests','delete',"DELETE FROM requests WHERE id='2'"),
    ('session_context','update',"UPDATE session_context SET data='{}' WHERE session_id='shared'"),
    ('compressions','delete',"DELETE FROM compressions WHERE id='cascade'"),
])
def test_tampered_cascade_triggers_fall_back_on_actual_mutations(tmp_path,table,operation,mutation):
    store,runtime,base=fixture(tmp_path)
    with store.db() as c:
        c.execute("INSERT INTO session_context VALUES('shared',1,'{\"value\":1}')")
        c.execute("INSERT INTO compressions VALUES('cascade',200,NULL,'fixture','shared',NULL,NULL,'{}')")
    base=runtime.read(tmp_path,*ARGS,view='overview',group='time')
    with store.db() as c:
        trigger=f'analytics_{table}_{operation}'
        c.execute(f'DROP TRIGGER {trigger}')
        c.execute(f'CREATE TRIGGER {trigger} AFTER {operation.upper()} ON {table} BEGIN SELECT 1; END')
        c.execute(mutation)
        assert not supported(c)
    result=runtime.refresh(tmp_path,*ARGS,resume_token=base['incremental']['resume_token'])
    assert 'incremental' not in result
    assert comparable(result)==comparable(runtime.read(tmp_path,*ARGS,view='overview',group='time'))


def test_default_rolling_get_seeds_normalized_end_and_refresh(tmp_path):
    import time
    store=Store(tmp_path)
    start=time.time()-86401
    put(store,'rolling',start+1000,1)
    runtime=AnalyticsRuntime()
    base=runtime.read(tmp_path,start,None,*ARGS[2:],view='overview',group='model')
    assert base['incremental']['resume_token']
    end=base['window']['end']
    assert 86400<=end-start<86460
    put(store,'new',start+1100,2)
    refreshed=runtime.refresh(tmp_path,start+100,end+100,*ARGS[2:],
                              resume_token=base['incremental']['resume_token'],group='model')
    assert refreshed['incremental']['mode']=='delta'
    assert comparable(refreshed)==comparable(runtime.read(tmp_path,start+100,end+100,*ARGS[2:],view='overview',group='model'))


def test_sealed_token_is_always_accepted_and_decompression_is_bounded(tmp_path):
    import base64
    import hmac
    import os
    import zlib
    runtime=AnalyticsRuntime()
    for payload in (b'x'*4_194_305, os.urandom(4_194_305)):
        packed=zlib.compress(payload,3)
        digest=hmac.digest(runtime._resume_key,packed,'sha256')
        token=base64.urlsafe_b64encode(digest+packed).decode()
        if len(token)<=4_194_304:
            with pytest.raises(ValueError,match='Invalid analytics resume token'):
                runtime._unseal(token)
    # Raw JSON is under the uncompressed cap, but signing its packed/base64
    # form would exceed the POST token cap. Never issue this unusable token.
    noise=base64.urlsafe_b64encode(os.urandom(3_120_000)).decode()
    result={'analytics_revision':'fixture','incremental':{'revision':1},'noise':noise}
    raw=json.dumps(['root','fixture',ARGS,'overview','time','id',[],result,{}],
                   separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    assert len(raw)<4_194_304
    assert len(base64.urlsafe_b64encode(b'0'*32+zlib.compress(raw,3)))>4_194_304
    assert runtime._seal('root',ARGS,'overview','time',result,{},'id',[]) is None
    store=Store(tmp_path)
    put(store,'a',300,10)
    baseline=runtime.read(tmp_path,*ARGS,view='overview',group='time')
    token=baseline['incremental']['resume_token']
    assert runtime._unseal(token)
    assert len(token)<=4_194_304


def test_route_implicit_end_exposes_exact_bounds_for_forward_refresh(tmp_path):
    import time
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient
    from ledger_runtime.api import add_routes
    store=Store(tmp_path)
    start=time.time()-86401
    put(store,'a',start+1000,10)
    router=APIRouter()
    add_routes(router,lambda profile:(tmp_path,'selected',None),lambda:tmp_path)
    app=FastAPI()
    app.include_router(router)
    client=TestClient(app)
    query=dict(start=start,view='overview',group='model')
    base=client.get('/ledger',params=query).json()
    token=base['incremental']['resume_token']
    exact=base['window']
    advanced={**query,'start':exact['start']+100,'end':exact['end']+100}
    put(store,'b',start+1100,20)
    response=client.post('/ledger/refresh',params=advanced,json={'resume_token':token})
    assert response.status_code==200
    assert response.json()['incremental']['mode']=='delta'
    assert comparable(response.json())==comparable(client.get('/ledger',params=advanced).json())


def test_exact_bucket_end_at_epoch_precision_keeps_half_open_window(tmp_path):
    store=Store(tmp_path)
    start=1_800_000_000.
    end=start+86400.
    put(store,'inside',start+10,1)
    runtime=AnalyticsRuntime()
    args=(start,end,*ARGS[2:])
    base=runtime.read(tmp_path,*args,view='overview',group='model')
    put(store,'at-end',end,2)
    delta=runtime.refresh(tmp_path,*args,resume_token=base['incremental']['resume_token'],group='model')
    assert delta['incremental']['mode']=='delta'
    assert comparable(delta)==comparable(runtime.read(tmp_path,*args,view='overview',group='model'))
    assert delta['request_count']==1


@pytest.mark.parametrize('ddl', [
    "CREATE TRIGGER erase_evidence AFTER INSERT ON analytics_changes BEGIN DELETE FROM analytics_changes WHERE revision=NEW.revision; END",
    "CREATE TRIGGER skew_revision AFTER INSERT ON requests BEGIN UPDATE analytics_change_meta SET version=99; END",
    "CREATE TRIGGER rewrite_request AFTER INSERT ON analytics_changes BEGIN UPDATE requests SET data=json_set(data,'$.usage.total_tokens',99) WHERE id=NEW.item_id; END",
    "CREATE TRIGGER cascade_source AFTER INSERT ON events BEGIN UPDATE requests SET data=json_set(data,'$.usage.total_tokens',99) WHERE id='2'; DELETE FROM analytics_changes WHERE revision=(SELECT MAX(revision) FROM analytics_changes); END",
])
def test_unknown_trigger_inventory_cannot_suppress_or_rewrite_history(tmp_path, ddl):
    store, runtime, base = fixture(tmp_path)
    with store.db() as c:
        c.execute(ddl)
        assert not supported(c)
        if 'cascade_source' in ddl:
            c.execute("INSERT INTO events(ts,kind,item_id,data) VALUES(1,'change','2','{}')")
        else:
            row = json.loads(c.execute("SELECT data FROM requests WHERE id='2'").fetchone()[0])
            row['usage']['total_tokens'] = 10
            c.execute('UPDATE requests SET data=? WHERE id=?', (json.dumps(row), '2'))
            if 'rewrite_request' in ddl:
                c.execute("INSERT INTO requests VALUES('extra',220,221,'fixture','model','shared','main',NULL,'completed',?)",
                          (json.dumps({'id':'extra','usage':{'total_tokens':10}}),))
    result = runtime.refresh(tmp_path, *ARGS, resume_token=base['incremental']['resume_token'])
    full = runtime.read(tmp_path, *ARGS, view='overview', group='time')
    assert 'incremental' not in result
    assert comparable(result) == comparable(full)
    if 'rewrite_request' in ddl or 'cascade_source' in ddl:
        assert full['summary']['known']['total_tokens'] != base['summary']['known']['total_tokens']


@pytest.mark.parametrize('ddl', [
    'CREATE INDEX unexpected_index ON requests(status)',
    'CREATE TABLE analytics_extra(x TEXT)',
    'CREATE TABLE unexpected_source(x TEXT)',
])
def test_unknown_table_or_index_inventory_falls_back(tmp_path, ddl):
    store, runtime, base = fixture(tmp_path)
    with store.db() as c:
        c.execute(ddl)
        assert not supported(c)
    result = runtime.refresh(tmp_path, *ARGS, resume_token=base['incremental']['resume_token'])
    assert 'incremental' not in result
    assert comparable(result) == comparable(runtime.read(tmp_path, *ARGS, view='overview', group='time'))


@pytest.mark.parametrize('table,record', [
    ('requests', '{not-json'),
    ('requests', '[]'),
    ('requests', '{"usage":42}'),
    ('requests', '{"cost":{"known_components_usd":"invalid-price"}}'),
    ('requests', '{"cost":{"components":{"input_tokens":"invalid-price"}}}'),
    ('requests', '{"cost":{"rate":[]}}'),
    ('requests', '{"session_lineage":42}'),
    ('requests', '{"usage":{"raw_usage":[]}}'),
    ('compressions', 'null'),
    ('session_context', '{not-json'),
    ('provider_catalog', '{"rates":[42]}'),
    ('pricing_status', '["unexpected"]'),
    ('skill_events', '{"retained_skills":42}'),
    ('skill_events', '{not-json'),
])
def test_migration_preflight_rejects_unreadable_data_before_ddl(tmp_path, table, record):
    import hashlib
    from ledger_runtime.incremental_migration import migrate_offline
    path = tmp_path / 'events.sqlite3'
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA + SKILLS_SCHEMA)
        statements = {
            'requests': "INSERT INTO requests VALUES('x',200,201,'fixture','model','s','main',NULL,'completed',?)",
            'compressions': "INSERT INTO compressions VALUES('x',200,201,'fixture','s',NULL,NULL,?)",
            'session_context': "INSERT INTO session_context VALUES('s',200,?)",
            'provider_catalog': "INSERT INTO provider_catalog(source_id,observed,data) VALUES('fixture',200,?)",
            'pricing_status': "INSERT INTO pricing_status VALUES('fixture',?)",
            'skill_events': "INSERT INTO skill_events(id,ts,kind,session_id,provider,model,skill,data) VALUES('x',200,'skill_load','s','fixture','model','skill',?)",
        }
        c.execute(statements[table], (record,))
    backup = tmp_path / 'backup.sqlite3'
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    proof = hashlib.sha256(backup.read_bytes()).hexdigest()
    before = path.read_bytes()
    with pytest.raises(ValueError, match=f'Unreadable analytics data in {table} \\(row 1\\)') as error:
        migrate_offline(path, confirmed=True, offline_target=path, backup=backup, backup_sha256=proof)
    assert record not in str(error.value)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as c:
        assert not supported(c)
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='analytics_changes'").fetchone() is None
        assert c.execute(f'SELECT data FROM {table}').fetchone()[0] == record


def test_migration_accepts_sparse_legacy_json_objects(tmp_path):
    import hashlib
    from ledger_runtime.incremental_migration import migrate_offline
    path = tmp_path / 'usage-ledger' / 'events.sqlite3'
    path.parent.mkdir()
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA + SKILLS_SCHEMA)
        c.execute("INSERT INTO requests VALUES('old',200,201,'fixture','model','s','main',NULL,'completed',?)",
                  ('{"id":"old","status":"completed","usage":null,"unrecognized":{"future":true}}',))
        c.execute("INSERT INTO compressions VALUES('c',200,201,'fixture','s',NULL,NULL,'{}')")
        c.execute("INSERT INTO skill_events(id,ts,kind,session_id,provider,model,skill,data) VALUES('s',200,'skill_load','s','fixture','model','skill','{}')")
        c.execute("INSERT INTO provider_catalog(source_id,observed,data) VALUES('openai',200,'{}')")
    backup = tmp_path / 'backup.sqlite3'
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    migrate_offline(path, confirmed=True, offline_target=path, backup=backup,
                    backup_sha256=hashlib.sha256(backup.read_bytes()).hexdigest())
    with sqlite3.connect(path) as c:
        assert supported(c)
    full = AnalyticsRuntime().read(tmp_path, *ARGS, view='overview', group='time')
    assert full['request_count'] == 1
    assert full['summary']['missing_usage'] == 1


@pytest.mark.parametrize('table,payload', [
    ('requests', {'id': 'bad', 'status': 'completed', 'usage': {'usage_source': 'hermes_normalized',
        'cache_read_tokens': 0, 'raw_usage': None}, 'cost': {'rate': {'input_tokens': 'not-a-price'}}}),
    ('provider_catalog', {'rates': [{}]}),
])
def test_migration_refuses_nested_rate_and_catalogue_before_ddl(tmp_path, table, payload):
    import hashlib
    import json
    from ledger_runtime.incremental_migration import migrate_offline
    path = tmp_path / 'usage-ledger' / 'events.sqlite3'
    path.parent.mkdir()
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA + SKILLS_SCHEMA)
        if table == 'requests':
            c.execute("INSERT INTO requests VALUES('bad',200,201,'openai-codex','model','s','main',NULL,'completed',?)",
                      (json.dumps(payload),))
        else:
            c.execute("INSERT INTO provider_catalog(source_id,observed,data) VALUES('openai',200,?)",
                      (json.dumps(payload),))
    backup = tmp_path / 'verified.sqlite3'
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as saved:
        source.backup(saved)
    before, backup_before = path.read_bytes(), backup.read_bytes()
    with pytest.raises(ValueError, match=f'Unreadable analytics data in {table} \\(row 1\\)') as error:
        migrate_offline(path, confirmed=True, offline_target=path, backup=backup,
                        backup_sha256=hashlib.sha256(backup_before).hexdigest())
    assert 'not-a-price' not in str(error.value)
    assert path.read_bytes() == before and backup.read_bytes() == backup_before
    with sqlite3.connect(path) as c:
        assert not supported(c)
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='analytics_changes'").fetchone() is None


def test_migration_reads_old_row_beyond_first_detail_page(tmp_path):
    import hashlib
    import json
    from ledger_runtime.incremental_migration import migrate_offline, _reader_preflight
    path = tmp_path / 'usage-ledger' / 'events.sqlite3'
    path.parent.mkdir()
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA + SKILLS_SCHEMA)
        # Oldest item is outside the usual rolling 24h and page 1; its nested
        # value is not used by the first-page detail but breaks all-history SQL.
        c.execute("INSERT INTO requests VALUES('old',100,101,'fixture','model','s','main',NULL,'completed',?)",
                  (json.dumps({'id':'old','cost':{'components':{'input_tokens':'bad-old-price'}}}),))
        c.executemany('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                      ((str(i), 1_700_000_000+i, 1_700_000_001+i, 'fixture', 'model', 's', 'main',
                        None, 'completed', json.dumps({'id':str(i)})) for i in range(2001)))
    backup = tmp_path / 'verified.sqlite3'
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as saved:
        source.backup(saved)
    before, saved_before = path.read_bytes(), backup.read_bytes()
    with pytest.raises(ValueError, match='reader \\(full-history stage\\)'):
        _reader_preflight(backup, hashlib.sha256(saved_before).hexdigest())  # production SQL scans old history
    with pytest.raises(ValueError, match='Unreadable analytics data in requests \\(row') as error:
        migrate_offline(path, confirmed=True, offline_target=path, backup=backup,
                        backup_sha256=hashlib.sha256(saved_before).hexdigest())
    assert 'bad-old-price' not in str(error.value)
    assert path.read_bytes() == before and backup.read_bytes() == saved_before
    with sqlite3.connect(path) as c:
        assert not supported(c)


@pytest.mark.parametrize('column_status,column_end', [('pending', None), ('completed', 201)])
def test_migration_open_owner_refused_without_process_inspection(tmp_path, monkeypatch, column_status, column_end):
    import hashlib
    import json
    from ledger_runtime.incremental_migration import migrate_offline
    from ledger_runtime import ownership
    path = tmp_path / 'usage-ledger' / 'events.sqlite3'
    path.parent.mkdir()
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA + SKILLS_SCHEMA)
        c.execute("INSERT INTO requests VALUES('open',200,?,'fixture','model','s','main',NULL,?,?)",
                  (column_end, column_status, json.dumps({'id':'open','status':'pending','owner':{'version':1,'pid':42}})))
    backup = tmp_path / 'verified.sqlite3'
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as saved:
        source.backup(saved)
    original = path.read_bytes()
    monkeypatch.setattr(ownership, 'inspect_owner', lambda _: pytest.fail('preflight inspected process'))
    with pytest.raises(ValueError, match='requests \\(liveness stage\\)'):
        migrate_offline(path, confirmed=True, offline_target=path, backup=backup,
                        backup_sha256=hashlib.sha256(backup.read_bytes()).hexdigest())
    assert path.read_bytes() == original
    with sqlite3.connect(path) as c:
        assert not supported(c)


def test_migration_valid_catalogue_valuation_on_disposable_copy(tmp_path):
    import hashlib
    import json
    from ledger_runtime.incremental_migration import migrate_offline
    path = tmp_path / 'usage-ledger' / 'events.sqlite3'
    path.parent.mkdir()
    rate = dict(model='m', service_tier='standard', input_tokens='1', output_tokens='1',
                cache_read_tokens='1', cache_write_tokens='1')
    snapshot = dict(source_id='openai', source='fixture', source_url='https://example.invalid',
                    content_sha256='fixture', origin='fixture', observed_at=200, rates=[rate])
    request = dict(id='r', started=200, ended=201, provider='openai-codex', model='m',
                   status='completed', usage=dict(input_tokens=1, output_tokens=1,
                       cache_read_tokens=1, cache_write_tokens=1, prompt_tokens=2),
                   cost=dict(known_components_usd='0'))
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA + SKILLS_SCHEMA)
        c.execute("INSERT INTO requests VALUES('r',200,201,'openai-codex','m','s','main',NULL,'completed',?)",
                  (json.dumps(request),))
        c.execute("INSERT INTO provider_catalog(source_id,observed,data) VALUES('openai',200,?)",
                  (json.dumps(snapshot),))
    backup = tmp_path / 'verified.sqlite3'
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as saved:
        source.backup(saved)
    backup_bytes = backup.read_bytes()
    migrate_offline(path, confirmed=True, offline_target=path, backup=backup,
                    backup_sha256=hashlib.sha256(backup_bytes).hexdigest())
    assert backup.read_bytes() == backup_bytes
    with sqlite3.connect(path) as c:
        assert supported(c)
        assert json.loads(c.execute("SELECT data FROM requests WHERE id='r'").fetchone()[0]) == request


@pytest.mark.parametrize('location', ['saved', 'selected_catalogue', 'unselected_catalogue'])
@pytest.mark.parametrize('field,price',
                         [('input_tokens', value) for value in
                          (None, '', '0', 0, '1.25', '1e-7', '-1', -1,
                           '-0.01', 'NaN', 'Infinity', '-Infinity', False, '<missing>')]
                         + [(field, '-1') for field in
                            ('output_tokens', 'cache_read_tokens', 'cache_write_tokens',
                             'cache_write_1h_tokens')])
def test_migration_rate_prices_follow_accounting_domain(tmp_path, location, field, price):
    import hashlib
    import json
    from ledger_runtime.accounting import decimal_value
    from ledger_runtime.incremental_migration import migrate_offline

    # The runtime price validator is the oracle, including unknown and zero.
    try:
        decimal_value(None if price == '<missing>' else price)
        valid = True
    except (ValueError, ArithmeticError, TypeError, OverflowError):
        valid = False
    path = tmp_path / 'usage-ledger' / 'events.sqlite3'
    path.parent.mkdir()
    cost: dict[str, object] = dict(known_components_usd='0')
    request = dict(id='r', started=200, ended=201, provider='openai-codex', model='selected',
                   status='completed', usage=dict(input_tokens=1, output_tokens=1,
                       cache_read_tokens=1, cache_write_tokens=1, prompt_tokens=2),
                   cost=cost)
    rates: list[dict[str, object]] = [dict(model='selected', service_tier='standard', input_tokens='1',
                  output_tokens='1', cache_read_tokens='1', cache_write_tokens='1'),
             dict(model='unselected', service_tier='standard', input_tokens='1',
                  output_tokens='1', cache_read_tokens='1', cache_write_tokens='1')]
    snapshot = dict(source_id='openai', source='fixture', source_url='https://example.invalid',
                    content_sha256='fixture', origin='fixture', observed_at=200, rates=rates)
    if location == 'saved':
        cost['rate'] = {} if price == '<missing>' else {field: price}
    else:
        target = rates[0 if location == 'selected_catalogue' else 1]
        if price == '<missing>':
            target.pop(field, None)
        else:
            target[field] = price
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA + SKILLS_SCHEMA)
        c.execute("INSERT INTO requests VALUES('r',200,201,'openai-codex','selected','s','main',NULL,'completed',?)",
                  (json.dumps(request),))
        c.execute("INSERT INTO provider_catalog(source_id,observed,data) VALUES('openai',200,?)",
                  (json.dumps(snapshot),))
    backup = tmp_path / 'verified.sqlite3'
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as saved:
        source.backup(saved)
    before, backup_before = path.read_bytes(), backup.read_bytes()
    proof = hashlib.sha256(backup_before).hexdigest()
    if valid:
        migrate_offline(path, confirmed=True, offline_target=path, backup=backup,
                        backup_sha256=proof)
        with sqlite3.connect(path) as c:
            assert supported(c)
            assert json.loads(c.execute("SELECT data FROM requests WHERE id='r'").fetchone()[0]) == request
    else:
        table = 'requests' if location == 'saved' else 'provider_catalog'
        with pytest.raises(ValueError, match=f'Unreadable analytics data in {table} \\(row 1\\)') as error:
            migrate_offline(path, confirmed=True, offline_target=path, backup=backup,
                            backup_sha256=proof)
        assert str(price) not in str(error.value)
        assert path.read_bytes() == before
        with sqlite3.connect(path) as c:
            assert not supported(c)
            assert c.execute("SELECT 1 FROM sqlite_master WHERE name='analytics_changes'").fetchone() is None
    assert backup.read_bytes() == backup_before
