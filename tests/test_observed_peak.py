"""Isolated lifecycle concurrency: no producer hooks or live ledgers."""
import json
import time

import pytest

from _hermes_ai_usage_ledger_v2.storage import Store
from _hermes_ai_usage_ledger_v2.observed_peak import OPEN_CHILD_GRACE_SECONDS, intervals, kind_of, peak
from _hermes_ai_usage_ledger_v2 import aggregate, ownership
from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime


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
    assert d['subagent_summary']['peak_observed']=={'value':2,'unmatched':1,'basis':'matched_child_lifecycle','running':0,'open_ended':0}
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
    def counted(*args,**kwargs):
        calls.append(1)
        return original(*args,**kwargs)
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


# --- Open children: still running or stopped without a stop record ---------
# Owner liveness is faked by patching ``ownership.inspect_owner``; no real
# process is inspected. Timestamps are relative to the read clock because the
# rule bounds running children by it.

LIVE = {'version': 1, 'pid': 1715, 'tag': 'live'}
DEAD = {'version': 1, 'pid': 99, 'tag': 'dead'}


@pytest.fixture
def owners(monkeypatch):
    """Fake liveness keyed by the synthetic owner's tag; counts inspections."""
    state = {'live': 'live', 'dead': 'dead', 'calls': []}

    def inspect(owner):
        state['calls'].append(owner)
        tag = owner.get('tag') if isinstance(owner, dict) else None
        return (state.get(tag, 'unknown'), 'synthetic')
    monkeypatch.setattr(ownership, 'inspect_owner', inspect)
    return state


def start(c, sid, ts, identity=None, parent=''):
    c.execute('INSERT INTO events(ts,kind,item_id,data) VALUES(?,?,?,?)',
              (ts, 'subagent_start', sid, json.dumps({'subagent_id': identity or sid, 'parent_session_id': parent})))


def stop(c, sid, ts):
    c.execute('INSERT INTO events(ts,kind,item_id,data) VALUES(?,?,?,?)', (ts, 'subagent_stop', sid, '{}'))


def request(c, key, sid, stamp, owner=None, *, provider='alpha', model='returned', kind='subagent',
            project='proj', root='root', ended=None):
    ended = stamp + .1 if ended is None else ended
    rec = dict(id=key, started=stamp, ended=ended, session_id=sid, provider=provider, model='requested',
               response_model=model, agent_kind=kind, subagent_id=sid, task='main', status='completed',
               project_id=project, root_session_id=root)
    if owner is not None:
        rec['owner'] = owner
    c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
              (key, stamp, ended, provider, 'requested', sid, 'main', None, 'completed', json.dumps(rec)))


def peak_of(report):
    return report['subagent_summary']['peak_observed']


def test_live_owner_counts_until_now_across_scopes(tmp_path, owners):
    now = time.time()
    t0 = now - 3600
    store = Store(tmp_path)
    with store.db() as c:
        start(c, 'run', t0, 'worker')
        request(c, 'r1', 'run', t0 + 10, LIVE)
        request(c, 'r2', 'run', now - 600, LIVE)
    d = store.read(t0 - 100, None, view='overview', group='time')
    assert peak_of(d) == {'value': 1, 'unmatched': 0, 'basis': 'matched_child_lifecycle',
                          'running': 1, 'open_ended': 0}
    # Every bucket after its start, including the leading one ending at now.
    after = [b for b in d['trend']['buckets'] if b['end'] > t0]
    assert after and all(b['peak_observed']['value'] == 1 and b['peak_observed']['running'] == 1 for b in after)
    assert d['trend']['buckets'][-1]['peak_observed']['value'] == 1
    before = [b for b in d['trend']['buckets'] if b['end'] <= t0]
    assert all(b['peak_observed'] == {'value': 0, 'unmatched': 0, 'basis': 'matched_child_lifecycle',
                                      'running': 0, 'open_ended': 0} for b in before)
    # A quiet stretch long after its last request still counts while live.
    assert peak_of(store.read(now - 300, None))['value'] == 1
    assert d['provider_groups'][0]['peak_observed']['value'] == 1
    model = store.read(t0 - 100, None, view='overview', group='model')
    assert model['model_groups'][0]['peak_observed']['running'] == 1
    for group, field in (('project', 'project_groups'), ('session', 'session_groups')):
        rows = store.read(t0 - 100, None, view='overview', group=group)[field]
        assert [r['peak_observed']['value'] for r in rows] == [1]
    scoped = store.read(t0 - 100, None, provider='alpha', model='returned', model_provider='alpha')
    assert peak_of(scoped)['value'] == 1 and peak_of(scoped)['running'] == 1
    # Request-attributed: another provider has no evidence for it.
    assert peak_of(store.read(t0 - 100, None, provider='beta'))['value'] == 0


def test_one_owner_inspection_per_read(tmp_path, owners):
    now = time.time()
    store = Store(tmp_path)
    with store.db() as c:
        for n in range(9):
            start(c, f'c{n}', now - 500, f'w{n}')
            request(c, f'r{n}', f'c{n}', now - 400 + n, LIVE)
    owners['calls'].clear()
    assert peak_of(store.read(now - 1000, None))['value'] == 9
    assert len(owners['calls']) == 1  # shared owner inspected once, not per child
    with store.db() as c:
        for n in range(9):  # distinct owners: at most one inspection per child
            request(c, f'own{n}', f'c{n}', now - 100 + n, {**LIVE, 'pid': 2000 + n})
    owners['calls'].clear()
    assert peak_of(store.read(now - 1000, None))['running'] == 9
    assert len(owners['calls']) == 9


@pytest.mark.parametrize('owner', [DEAD, {'version': 1, 'pid': 5, 'tag': 'other'}, None])
def test_dead_or_unknown_owner_ends_at_last_activity(tmp_path, owners, owner):
    now = time.time()
    t0 = now - 7200
    store = Store(tmp_path)
    with store.db() as c:
        start(c, 'gone', t0, 'worker')
        request(c, 'r1', 'gone', t0 + 10, owner)
        request(c, 'r2', 'gone', t0 + 50, owner, ended=t0 + 90)
    assert peak_of(store.read(t0 + 80, t0 + 100)) == {'value': 1, 'unmatched': 0, 'basis': 'matched_child_lifecycle',
                                                       'running': 0, 'open_ended': 1}
    assert peak_of(store.read(t0 + 90, now))['value'] == 0  # never presumed beyond evidence
    assert peak_of(store.read(t0 + 90, now))['open_ended'] == 0


def test_no_request_child_uses_grace_and_parent_owner(tmp_path, owners):
    now = time.time()
    store = Store(tmp_path)
    with store.db() as c:
        request(c, 'p-live', 'parent-live', now - 60, LIVE, kind='main')
        request(c, 'p-dead', 'parent-dead', now - 60, DEAD, kind='main')
        start(c, 'fresh', now - 30, parent='parent-live')
    assert OPEN_CHILD_GRACE_SECONDS == 120
    fresh = peak_of(store.read(now - 3600, None))
    assert fresh == {'value': 1, 'unmatched': 0, 'basis': 'matched_child_lifecycle', 'running': 1, 'open_ended': 0}
    with store.db() as c:
        start(c, 'orphaned', now - 30, parent='parent-dead')
        start(c, 'stale', now - OPEN_CHILD_GRACE_SECONDS - 60, parent='parent-live')
        start(c, 'parentless', now - 30)
    mixed = peak_of(store.read(now - 3600, None))
    assert mixed['value'] == 1 and mixed['running'] == 1 and mixed['unmatched'] == 3
    # Scoped reads need child-request evidence; a request-less child has none.
    assert peak_of(store.read(now - 3600, None, provider='alpha'))['value'] == 0


def test_only_excluded_open_children_stay_unknown(tmp_path, owners):
    now = time.time()
    store = Store(tmp_path)
    with store.db() as c:
        start(c, 'old', now - 9000)  # no request, beyond grace
    assert peak_of(store.read(now - 10000, None)) == {'value': None, 'unmatched': 1, 'basis': 'matched_child_lifecycle',
                                                       'running': 0, 'open_ended': 0}


def eleven_versus_four(c, now, owner=LIVE):
    """Earlier matched overlap of 4, then 2 matched still running when 9 open children start."""
    base = now - 9000
    for n in range(4):
        start(c, f'early{n}', base + n, f'e{n}')
        stop(c, f'early{n}', base + 400 + n)
    s = now - 3800  # the later burst
    start(c, 'm1', s - 100, 'm1')
    start(c, 'm2', s - 50, 'm2')
    for n in range(9):
        start(c, f'open{n}', s, f'o{n}')
        request(c, f'or{n}', f'open{n}', s + 5 + n, owner)
        request(c, f'late{n}', f'open{n}', now - 60 + n, owner)
    stop(c, 'm1', s + 130)
    stop(c, 'm2', s + 3000)
    return s


def test_mixed_matched_and_open_reproduces_eleven_not_four(tmp_path, owners):
    now = time.time()
    store = Store(tmp_path)
    with store.db() as c:
        s = eleven_versus_four(c, now)
    whole = store.read(now - 10000, None, view='overview', group='time')
    assert peak_of(whole) == {'value': 11, 'unmatched': 0, 'basis': 'matched_child_lifecycle',
                              'running': 9, 'open_ended': 0}
    assert max(b['peak_observed']['value'] for b in whole['trend']['buckets']) == 11
    # The old rule (matched pairs only) would report the earlier overlap of 4.
    assert peak_of(store.read(now - 10000, now - 4000))['value'] == 4
    assert peak_of(store.read(s + 3000, None))['value'] == 9  # after m2 stops


def test_dead_owner_burst_counts_until_last_request(tmp_path, owners):
    now = time.time()
    store = Store(tmp_path)
    with store.db() as c:
        eleven_versus_four(c, now, owner=DEAD)
    whole = peak_of(store.read(now - 10000, None))
    assert whole == {'value': 11, 'unmatched': 0, 'basis': 'matched_child_lifecycle', 'running': 0, 'open_ended': 9}
    assert peak_of(store.read(now - 1, None))['value'] == 0  # last request ended ~51 s ago


def test_unmatched_excludes_counted_children(tmp_path, owners):
    now = time.time()
    store = Store(tmp_path)
    with store.db() as c:
        start(c, 'counted', now - 500)
        request(c, 'r', 'counted', now - 400, LIVE)
        start(c, 'excluded', now - 9000)  # no request, beyond grace
        stop(c, 'orphan', now - 100)
        start(c, 'dup', now - 300)  # duplicate open start: ambiguous, never counted
        start(c, 'dup', now - 200)
        request(c, 'rd', 'dup', now - 150, LIVE)
    value = peak_of(store.read(now - 1000, None))
    assert value['value'] == 1 and value['running'] == 1 and value['unmatched'] == 4


def test_private_intervals_stay_four_tuples_with_kind(tmp_path, owners):
    """Readers that unpack four values (all-profile qualification) keep working."""
    now = time.time()
    store = Store(tmp_path)
    with store.db() as c:
        start(c, 'run', now - 500)
        request(c, 'r', 'run', now - 400, LIVE)
        start(c, 'done', now - 900)
        stop(c, 'done', now - 800)
    d = store.read(now - 1000, None, _include_peak_private=True)
    kinds = {}
    for sid, identity, lo, hi in d['_peak_intervals']:
        requalified = (sid, json.dumps(['p', identity]), lo, hi)
        kinds[str(sid)] = kind_of(requalified)
    assert kinds == {'run': 'running', 'done': 'matched'}


def test_all_profiles_union_counts_running_children(tmp_path, owners):
    now = time.time()
    for root, sid in ((tmp_path, 'a'), (tmp_path / 'profiles' / 'other', 'b')):
        store = Store(root)
        with store.db() as c:
            start(c, sid, now - 1800, 'worker')
            request(c, 'r' + sid, sid, now - 1700, LIVE)
            start(c, 'gone' + sid, now - 1800, 'gone')
            request(c, 'g' + sid, 'gone' + sid, now - 1750, DEAD, ended=now - 1600)
    inventory = aggregate.discover(tmp_path)
    d = aggregate.ledger(AnalyticsRuntime(), inventory, start=now - 3600, end=None, view='overview', group='time',
                         list_mode='page', limit=1)
    assert peak_of(d) == {'value': 4, 'unmatched': 0, 'basis': 'matched_child_lifecycle',
                          'running': 2, 'open_ended': 2}
    assert d['provider_groups'][0]['peak_observed']['value'] == 4
    assert d['trend']['buckets'][-1]['peak_observed']['value'] == 2
    assert d['trend']['buckets'][-1]['peak_observed']['running'] == 2
    model = aggregate.ledger(AnalyticsRuntime(), inventory, start=now - 3600, end=None, view='overview',
                             group='model', list_mode='page', limit=1)
    assert model['model_groups'][0]['peak_observed']['running'] == 2


def test_refresh_never_returns_a_stale_running_peak(tmp_path, owners):
    """No ledger write, but the owner dies: the signed delta recomputes peaks."""
    now = time.time()
    t0 = now - 7200
    store = Store(tmp_path)
    with store.db() as c:
        start(c, 'run', t0 + 100, 'worker')
        request(c, 'r1', 'run', t0 + 200, LIVE, ended=t0 + 300)
    runtime = AnalyticsRuntime()
    args = (t0, t0 + 3600, '', 0, 10, '', '', '', 'exact', '', '', '')
    first = runtime.read(tmp_path, *args, view='overview', group='time')
    assert peak_of(first)['running'] == 1
    assert first['trend']['buckets'][-1]['peak_observed']['value'] == 1
    owners['live'] = 'dead'
    latest = runtime.refresh(tmp_path, *args, resume_token=first['incremental']['resume_token'],
                             view='overview', group='time')
    assert latest['incremental']['mode'] == 'delta'
    assert peak_of(latest) == {'value': 1, 'unmatched': 0, 'basis': 'matched_child_lifecycle',
                               'running': 0, 'open_ended': 1}
    assert latest['trend']['buckets'][-1]['peak_observed']['value'] == 0
    full = runtime.read(tmp_path, *args, view='overview', group='time')
    assert [b['peak_observed'] for b in latest['trend']['buckets']] == [b['peak_observed'] for b in full['trend']['buckets']]


def test_older_receipt_without_split_fields_gains_them(tmp_path, owners):
    now = time.time()
    t0 = now - 7200
    store = Store(tmp_path)
    with store.db() as c:
        start(c, 'run', t0 + 100, 'worker')
        request(c, 'r1', 'run', t0 + 200, LIVE)
    runtime = AnalyticsRuntime()
    args = (t0, t0 + 3600, '', 0, 10, '', '', '', 'exact', '', '', '')
    first = runtime.read(tmp_path, *args, view='overview', group='time')
    root, profile, revision, saved_args, view, group, identity, generation, base, seed_data = \
        runtime._unseal(first['incremental']['resume_token'])
    for value in [base['subagent_summary']['peak_observed'], *(r['peak_observed'] for r in base['provider_groups']),
                  *(b['peak_observed'] for b in base['trend']['buckets'])]:
        value.pop('running')
        value.pop('open_ended')
    old = runtime._seal(tmp_path, args, view, group, base, seed_data, identity, generation, profile)
    latest = runtime.refresh(tmp_path, *args, resume_token=old, view=view, group=group)
    assert peak_of(latest)['running'] == 1
    assert all('running' in b['peak_observed'] for b in latest['trend']['buckets'])
