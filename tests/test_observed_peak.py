"""Isolated lifecycle concurrency: no producer hooks or live ledgers."""
import json
from _hermes_ai_usage_ledger_v2.storage import Store
from _hermes_ai_usage_ledger_v2.observed_peak import intervals, peak
from _hermes_ai_usage_ledger_v2 import aggregate


def seed(root, children, requests=()):
    store=Store(root)
    with store.db() as c:
        for sid, identity, begin, end in children:
            c.execute('INSERT INTO events(ts,kind,item_id,data) VALUES(?,?,?,?)',
                      (begin,'subagent_start',sid,json.dumps({'subagent_id':identity})))
            if end is not None:
                c.execute('INSERT INTO events(ts,kind,item_id,data) VALUES(?,?,?,?)',
                          (end,'subagent_stop',sid,'{}'))
        for n,(sid,provider,model,stamp) in enumerate(requests):
            rec=dict(id=str(n),started=stamp,ended=stamp+.1,session_id=sid,provider=provider,
                     model='requested',response_model=model,agent_kind='subagent',subagent_id=sid,
                     task='main',status='completed')
            c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (str(n),stamp,stamp+.1,provider,'requested',sid,'main',None,'completed',json.dumps(rec)))
    return store


def test_overlap_sequential_touching_carry_and_repeated_identity(tmp_path):
    s=seed(tmp_path,[('a','same',1,8),('b','different',4,6),('c','same',5,9),
                     ('later','different',9,12),('open','unknown',2,None)])
    d=s.read(5,10)
    assert d['subagent_summary']['peak_observed']=={'value':2,'unmatched':1,'basis':'matched_child_lifecycle'}
    assert peak([('a','a',1,5),('b','b',5,9)],1,9)==1
    assert s.read(6,8)['subagent_summary']['peak_observed']['value']==1
    assert s.read(12,13)['subagent_summary']['peak_observed']['value']==0
    assert s.read(8,9)['subagent_summary']['peak_observed']['value']==1
    assert s.read(3,4)['subagent_summary']['peak_observed']['value']==1


def test_missing_and_ambiguous_do_not_invent_end(tmp_path):
    s=seed(tmp_path,[('open','o',1,None)])
    assert s.read(0,10)['subagent_summary']['peak_observed']['value'] is None
    with s.db() as c:
        c.execute("INSERT INTO events(ts,kind,item_id,data) VALUES(2,'subagent_start','open','{}')")
        c.execute("INSERT INTO events(ts,kind,item_id,data) VALUES(3,'subagent_stop','open','{}')")
        c.execute("INSERT INTO events(ts,kind,item_id,data) VALUES(4,'subagent_stop','orphan','{}')")
        matches,unmatched,seen=intervals(c)
    assert matches==[] and not seen and unmatched==4
    assert s.read(0,10)['subagent_summary']['peak_observed']['value'] is None


def test_duplicate_start_start_stop_counts_each_excluded_notification(tmp_path):
    s=seed(tmp_path,[])
    with s.db() as c:
        for ts,kind in [(1,'subagent_start'),(2,'subagent_start'),(3,'subagent_stop')]:
            c.execute('INSERT INTO events(ts,kind,item_id,data) VALUES(?,?,?,?)',(ts,kind,'same','{}'))
        matched,unmatched,seen=intervals(c)
    assert matched==[] and not seen and unmatched==3


def test_request_attribution_and_full_window_paging(tmp_path):
    s=seed(tmp_path,[('a','a',1,15),('b','b',5,13),('c','c',16,19)],
           [('a','alpha','returned',6),('b','beta','different',7),('c','alpha','returned',17)])
    d=s.read(0,20,view='overview',group='model',list_mode='page',limit=1)
    assert d['subagent_summary']['peak_observed']['value']==2
    assert d['model_groups'][0]['peak_observed']['value']==1
    assert s.read(0,20,provider='alpha',model='returned',model_provider='alpha',
                  view='overview',group='model',list_mode='page',limit=1)['subagent_summary']['peak_observed']['value']==1
    assert s.read(0,20,model='requested',model_provider='alpha')['subagent_summary']['peak_observed']['value']==0
    assert s.read(8,11,view='overview',group='time')['subagent_summary']['peak_observed']['value']==2
    assert '_peak_intervals' not in d


def test_scoped_subagents_require_matching_request_evidence(tmp_path):
    s=seed(tmp_path,[('a','a',1,8)])
    assert s.read(0,10)['subagent_summary']['peak_observed']['value']==1
    assert s.read(0,10,agent='subagent')['subagent_summary']['peak_observed']['value']==0


def test_pairing_uses_durable_sequence_despite_clock_shift(tmp_path):
    s=seed(tmp_path,[])
    with s.db() as c:
        for ts,kind,sid in [(5,'subagent_start','back'),(4,'subagent_stop','back'),
                            (3,'subagent_start','forward'),(7,'subagent_stop','forward')]:
            c.execute('INSERT INTO events(ts,kind,item_id,data) VALUES(?,?,?,?)',(ts,kind,sid,'{}'))
        matched,unmatched,seen=intervals(c)
    assert matched==[('forward','forward',3,7)]
    assert unmatched==2 and seen


def test_all_profiles_union_not_sum(tmp_path):
    seed(tmp_path,[('same','worker',1,4)], [('same','alpha','returned',2)])
    seed(tmp_path/'profiles'/'other',[('same','worker',3,6)], [('same','alpha','returned',4)])
    # Discovery returns stable qualified profile identities and copied sources.
    inventory=aggregate.discover(tmp_path)
    from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
    d=aggregate.ledger(AnalyticsRuntime(),inventory,start=0,end=10,view='overview',group='time',list_mode='page',limit=1)
    assert d['subagent_summary']['peak_observed']['value']==2
    assert d['provider_groups'][0]['peak_observed']['value']==2
    assert d['trend']['buckets'][0]['peak_observed']['value']==2
    model=aggregate.ledger(AnalyticsRuntime(),inventory,start=0,end=10,view='overview',group='model',list_mode='page',limit=1)
    assert model['model_groups'][0]['peak_observed']['value']==2
    assert '_peak_intervals' not in d


def test_selected_snapshot_refresh_recomputes_peak_after_event_only_change(tmp_path):
    s=seed(tmp_path,[('a','a',2,5)])
    from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
    runtime=AnalyticsRuntime()
    args=(1,10,'',0,10,'','','','exact','','','')
    first=runtime.read(tmp_path,*args,view='overview',group='time')
    assert first['subagent_summary']['peak_observed']['value']==1
    assert first['incremental']['resume_token']
    with s.db() as c:
        c.execute("INSERT INTO events(ts,kind,item_id,data) VALUES(3,'subagent_start','b','{}')")
        c.execute("INSERT INTO events(ts,kind,item_id,data) VALUES(4,'subagent_stop','b','{}')")
    latest=runtime.refresh(tmp_path,*args,resume_token=first['incremental']['resume_token'],view='overview',group='time')
    assert latest['subagent_summary']['peak_observed']['value']==2
    assert latest['trend']['buckets'][0]['peak_observed']['value']==2
    assert latest['incremental']['mode']=='delta'
    assert latest['incremental']['resume_token']


def test_delta_scans_lifecycle_once_for_multiple_changed_buckets(tmp_path, monkeypatch):
    import sys
    from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
    s=seed(tmp_path,[('a','a',2,500)], [('a','alpha','returned',100)])
    runtime=AnalyticsRuntime()
    observed_peak=sys.modules[runtime.current['store_type'].__module__.rsplit('.',1)[0]+'.observed_peak']
    args=(1,90000,'',0,10,'','','','exact','','','')
    base=runtime.read(tmp_path,*args,view='overview',group='time')
    with s.db() as c:
        for stamp in (4000,8000):
            c.execute('INSERT INTO events(ts,kind,item_id,data) VALUES(?,?,?,?)',
                      (stamp,'subagent_start',str(stamp),'{}'))
            c.execute('INSERT INTO events(ts,kind,item_id,data) VALUES(?,?,?,?)',
                      (stamp+100,'subagent_stop',str(stamp),'{}'))
            c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (str(stamp),stamp,stamp+1,'alpha','requested','a','main',None,'completed',
                       json.dumps(dict(id=str(stamp),started=stamp,ended=stamp+1,provider='alpha',
                                       model='requested',response_model='returned',agent_kind='subagent',session_id='a',status='completed'))))
    calls=[]
    original=observed_peak.intervals
    def counted(connection):
        calls.append(1)
        return original(connection)
    monkeypatch.setattr(observed_peak,'intervals',counted)
    changed=runtime.refresh(tmp_path,*args,resume_token=base['incremental']['resume_token'])
    assert changed['incremental']['mode']=='delta'
    assert len(calls)==1  # not one full event scan per changed request bucket
    full=runtime.read(tmp_path,*args,view='overview',group='time')
    assert changed['subagent_summary']['peak_observed']==full['subagent_summary']['peak_observed']
    assert changed['provider_groups'][0]['peak_observed']==full['provider_groups'][0]['peak_observed']


def test_old_signed_receipt_without_peak_falls_back_to_full(tmp_path):
    s=seed(tmp_path,[('a','a',2,5)])
    from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
    runtime=AnalyticsRuntime()
    args=(1,10,'',0,10,'','','','exact','','','')
    first=runtime.read(tmp_path,*args,view='overview',group='time')
    root,profile,revision,saved_args,view,group,identity,generation,base,seed_data=runtime._unseal(first['incremental']['resume_token'])
    del base['subagent_summary']['peak_observed']
    old=runtime._seal(tmp_path,args,view,group,base,seed_data,identity,generation,profile)
    with s.db() as c:
        c.execute("INSERT INTO events(ts,kind,item_id,data) VALUES(3,'subagent_start','b','{}')")
        c.execute("INSERT INTO events(ts,kind,item_id,data) VALUES(4,'subagent_stop','b','{}')")
    latest=runtime.refresh(tmp_path,*args,resume_token=old,view=view,group=group)
    assert latest['incremental']['mode']=='snapshot'
    assert latest['subagent_summary']['peak_observed']['value']==2


def test_all_profiles_partial_coverage_is_not_zero(tmp_path):
    seed(tmp_path,[('a','a',1,4)])
    (tmp_path/'profiles'/'missing').mkdir(parents=True)
    from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
    result=aggregate.ledger(AnalyticsRuntime(),aggregate.discover(tmp_path),start=0,end=10,
                            view='overview',group='time',list_mode='page',limit=1)
    assert result['coverage']['status']=='partial'
    assert result['subagent_summary']['peak_observed']['value']==1
