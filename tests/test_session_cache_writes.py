"""User-defined writes are read deltas; provider accounting remains evidence."""
import copy
import json
from decimal import Decimal

import pytest
from _hermes_ai_usage_ledger_v2.session_cache_writes import calculate
from _hermes_ai_usage_ledger_v2.storage import Store
from _hermes_ai_usage_ledger_v2.accounting import normalize


def row(key, started, reads, **kwargs):
    raw={'input_tokens':30000, 'output_tokens':20,
         'input_tokens_details': {'cached_tokens':reads,'cache_write_tokens':0}}
    d=dict(id=key, started=started, ended=started+1, session_id='s',
           source='main_hook',provider='openai-codex',model='test-only',
           response_model='test-only', api_mode='codex_responses',
           status='completed',task='main',process='p',agent_kind='primary',
           usage=normalize(raw))
    d.update(kwargs)
    return d


def values(rows):
    return {k:v['tokens'] for k,v in calculate(rows).items()}


def test_positive_differences_are_the_displayed_writes():
    rows=[row('cold',1,0),row('warm',3,12000),row('longer',5,15000)]
    before=copy.deepcopy(rows)
    assert values(rows)=={'cold':None,'warm':12000,'longer':3000}
    assert rows==before


def test_falls_do_not_remove_previously_counted_writes():
    rows=[row('a',1,10000),row('b',3,0),row('c',5,10000)]
    assert values(rows)=={'a':None,'b':0,'c':10000}


def test_equal_reads_are_zero_growth():
    assert values([row('a',1,500),row('b',3,500)])['b']==0


@pytest.mark.parametrize('changes',[{'process':'after-restart'},
    {'service_tier':'fast','returned_service_tier':'priority'},
    {'cache_request':{'tools_fingerprint':'changed','mode_sent':'explicit'}},
    {'ended':9}, {'status':'error'}])
def test_no_process_tier_or_settings_exceptions(changes):
    a,b=row('a',1,1000),row('b',3,5000,**changes)
    assert values([a,b])['b']==4000


@pytest.mark.parametrize('field,value',[('session_id','child'),('provider','another'),
  ('response_model','another'),('account_id','other-account'),('subagent_id','child-id'),
  ('api_mode','another-api')])
def test_independent_streams_never_subtract_each_other(field,value):
    a,b,c=row('a',1,1000),row('b',2,25000,**{field:value}),row('c',3,5000)
    got=calculate([a,b,c])
    assert got['b']['tokens'] is None
    assert got['c']['tokens']==4000 and got['c']['previous_request_id']=='a'


def test_helper_and_main_streams_are_separate_but_helpers_can_grow():
    rows=[row('a',1,1000),row('h1',2,5000,source='aux_hook',task='compression'),
          row('h2',3,8000,source='aux_hook',task='compression'),row('b',4,2000)]
    assert values(rows)=={'a':None,'h1':None,'h2':3000,'b':1000}


def test_main_task_session_ids_do_not_create_a_different_sequence():
    a,b=row('a',1,500,task='s'),row('b',3,2500,task='main')
    assert values([a,b])['b']==2000


def test_pending_does_not_reset_an_existing_observed_sequence():
    a,p,b=row('a',1,1000),row('p',2,0,status='pending',usage={}),row('b',3,5000)
    got=calculate([a,p,b])
    assert got['p']['status']=='pending' and got['b']['tokens']==4000


def test_completed_missing_read_is_not_filled_with_zero():
    a,p,b=row('a',1,1000),row('p',2,0,usage={}),row('b',3,5000)
    got=calculate([a,p,b])
    assert got['b']['tokens'] is None and got['b']['previous_request_id']=='p'


def test_actual_session_required_and_aggregated_calls_not_retitled():
    r=row('r',1,0,session_id=None)
    assert calculate([r])['r']['status']=='missing_session'
    a,b=row('a',1,1000),row('b',2,5000)
    b['usage']['request_count']=3
    assert calculate([a,b])['b']['status']=='aggregate_reading'


def test_out_of_order_arrival_and_duplicate_database_events(tmp_path):
    s=Store(tmp_path)
    a,b,c=row('a',10,0),row('b',20,12000),row('c',30,15000)
    for r in [c,a,b,b]:s.request(r,'request_completed')
    d=s.read(0,40)
    assert d['request_count']==3
    assert d['summary']['session_cache_writes']['tokens']==15000
    assert d['summary']['session_cache_writes']['compared_requests']==2


def test_all_aggregations_and_export_pages_use_same_previous_read(tmp_path,monkeypatch):
    s=Store(tmp_path)
    rate={'provider':'openai-codex','model':'test-only','service_tier':'standard',
        'input_tokens':'1','output_tokens':'2','cache_read_tokens':'.1','cache_write_tokens':'1.25'}
    monkeypatch.setattr(s,'latest_rate',lambda *_:rate)
    for i,r in enumerate((0,12000,15000,15000)):
        s.request(row(str(i),10+i*10,r,project_id='project'), 'request_completed')
    with s.db() as c:before=[tuple(r) for r in c.execute('SELECT id,data FROM main.requests ORDER BY id')]
    d=s.read(start=20,end=50,project='project',offset=1,limit=1)
    assert len(d['requests'])==1 and d['request_count']==3
    assert d['summary']['session_cache_writes']['tokens']==15000
    assert d['requests'][0]['calculated_cache_writes']['tokens']==3000
    assert d['requests'][0]['calculated_cache_writes']['previous_request_id']=='1'
    for name in ('provider_groups','model_groups','groups','applied_rate_groups','session_groups','project_groups'):
        assert sum(r['session_cache_writes']['tokens'] for r in d[name])==15000
    assert sum(r['session_cache_writes']['tokens'] for r in d['trend']['buckets'])==15000
    # Both raw tokens and saved prices remain unmodified, even if reported writes=0.
    assert d['summary']['known']['cache_write_tokens']==0
    assert Decimal(d['summary']['cost_components']['cache_write_tokens'])==0
    assert d['requests'][0]['usage']['cache_write_tokens']==0
    assert d['summary']['known']['total_tokens']==90060
    with s.db() as c:assert before==[tuple(r) for r in c.execute('SELECT id,data FROM main.requests ORDER BY id')]


def test_known_compaction_does_not_suppress_positive_difference(tmp_path):
    s=Store(tmp_path)
    for r in [row('a',10,1000),row('b',30,4000)]:s.request(r,'request_completed')
    s.compression(dict(id='comp',started=20,ended=21,provider='openai-codex',session_id='s',session_after='s'))
    d=s.read(25,40)
    assert d['summary']['session_cache_writes']['tokens']==3000
    # Old strict diagnostic can coexist in raw JSON without controlling the UI.
    assert d['cache_read_progression']['positive_read_growth_tokens'] is None


def test_subagent_totals_are_only_its_own_sequence(tmp_path):
    s=Store(tmp_path)
    for r in [row('p1',10,0),row('c1',11,0,session_id='child',agent_kind='subagent',root_session_id='s',session_lineage=['s']),
              row('p2',20,5000),row('c2',21,12000,session_id='child',agent_kind='subagent',root_session_id='s',session_lineage=['s'])]:
        s.request(r,'request_completed')
    d=s.read(0,30)
    assert d['summary']['session_cache_writes']['tokens']==17000
    assert d['subagent_summary']['session_cache_writes']['tokens']==12000
    filtered=s.read(0,30,session='s',session_scope='family',agent='subagent')
    assert filtered['summary']['session_cache_writes']['tokens']==12000


def test_empty_window_is_zero_requests_not_unknown_activity(tmp_path):
    d=Store(tmp_path).read(1,2)
    assert d['summary']['session_cache_writes']['tokens']==0
    assert d['summary']['session_cache_writes']['compared_requests']==0


def test_single_request_uses_first_read_as_baseline(tmp_path):
    s=Store(tmp_path);s.request(row('a',10,15000),'request_completed')
    d=s.read(0,20)
    assert d['requests'][0]['calculated_cache_writes']['status']=='baseline'
    assert d['requests'][0]['calculated_cache_writes']['tokens'] is None


def test_two_profile_databases_are_independent(tmp_path):
    a,b=Store(tmp_path/'a'),Store(tmp_path/'b')
    a.request(row('1',1,0),'request_completed');b.request(row('2',2,9000),'request_completed')
    assert b.read(0,3)['requests'][0]['calculated_cache_writes']['tokens'] is None
