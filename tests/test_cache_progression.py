"""Read changes are separate observations, never replacements for write counts."""
import copy
import json
from decimal import Decimal

import pytest
from _hermes_ai_usage_ledger_v2.cache_progression import analyse
from _hermes_ai_usage_ledger_v2.storage import Store
from _hermes_ai_usage_ledger_v2.accounting import normalize


def row(key, started, reads, prompt=20000, **extra):
    raw={'input_tokens':prompt,'output_tokens':20,'input_tokens_details':{'cached_tokens':reads,'cache_write_tokens':0}}
    return dict(id=key,started=started,ended=started+1,session_id='s',provider='openai-codex',model='example',response_model='example',api_mode='codex_responses',service_tier='standard',process='p',source='main_hook',status='completed',usage=normalize(raw),**extra)


def test_cold_warm_growth_is_not_billed_writes():
    rows=[row('a',1,0),row('b',3,12000),row('c',5,15000)]
    before=copy.deepcopy(rows)
    s,d=analyse(rows,{'a','b','c'})
    assert s['positive_read_growth_tokens']==15000 and s['eligible_pairs']==2
    assert s['excluded_reasons']=={'no_previous_main_request':1}
    assert d['b']['read_delta_tokens']==12000 and d['c']['read_delta_tokens']==3000
    assert d['b']['current_provider_write_tokens']==0
    assert not s['included_in_usage_or_cost'] and not d['b']['prefix_identity_verified']
    assert rows==before


def test_read_drop_then_recovery_is_not_unique_cache_inventory():
    rows=[row('a',1,10000),row('b',3,0),row('c',5,10000)]
    s,d=analyse(rows,{'a','b','c'})
    assert s['read_drop_tokens']==10000 and s['positive_read_growth_tokens']==10000
    assert d['b']['read_delta_tokens']==-10000 and d['b']['positive_read_growth_tokens']==0
    assert s['not_a_write_count'] is True


@pytest.mark.parametrize('change,reason',[
    ({'process':'q'},'producer_changed'),
    ({'process':None},'process_unavailable'),
    ({'response_model':'other'},'provider_model_or_tier_changed'),
    ({'provider':'openai'},'provider_model_or_tier_changed'),
    ({'returned_service_tier':'fast'},'provider_model_or_tier_changed'),
    ({'status':'pending'},'not_both_completed'),
    ({'status':'error'},'not_both_completed'),
])
def test_boundaries(change,reason):
    a,b=row('a',1,0),row('b',3,12000);b.update(change)
    s,d=analyse([a,b],{'b'})
    assert s['positive_read_growth_tokens'] is None and s['eligible_pairs']==0
    assert d['b']['reason']==reason


def test_unknown_read_not_assumed_zero():
    a,b=row('a',1,0),row('b',3,12000)
    a['usage']['cache_read_tokens']=None
    s,d=analyse([a,b],{'b'})
    assert s['positive_read_growth_tokens'] is None
    assert d['b']['reason']=='read_or_input_count_missing'


def test_overlapping_calls_rejected():
    a,b=row('a',1,0),row('b',3,12000);a['ended']=4
    s,d=analyse([a,b],{'b'})
    assert d['b']['reason']=='overlapping_or_invalid_timing'


def test_compaction_and_settings_change_break_comparison():
    a,b=row('a',1,0),row('b',3,12000)
    s,d=analyse([a,b],{'b'},{'s':[2]})
    assert d['b']['reason']=='compression_or_compaction_between_requests'
    a['cache_request']={'tools_fingerprint':'a'};b['cache_request']={'tools_fingerprint':'b'}
    s,d=analyse([a,b],{'b'})
    assert d['b']['reason']=='observed_cache_settings_changed'


def test_helpers_ignored_subagents_separate_and_no_cross_session_subtraction():
    a,b=row('a',1,0),row('b',5,12000)
    helper=row('helper',3,18000);helper['source']='aux_hook'
    child=row('child',4,8000);child['session_id']='child';child['agent_kind']='subagent'
    s,d=analyse([a,helper,child,b],{'a','helper','child','b'})
    assert set(d)=={'a','child','b'}
    assert s['positive_read_growth_tokens']==12000
    assert d['child']['reason']=='no_previous_main_request'


def test_aggregates_invalid_and_no_baseline():
    a,b=row('a',1,0),row('b',3,12000);a['usage']['request_count']=3
    assert analyse([a,b],{'b'})[1]['b']['reason']=='aggregate_not_single_request'
    assert analyse([],set())[0]['positive_read_growth_tokens'] is None
    a['usage']['request_count']=1;b['usage']['cache_read_tokens']=30000
    assert analyse([a,b],{'b'})[1]['b']['reason']=='invalid_read_decomposition'


def test_unknown_intermediate_row_is_not_skipped():
    a,b,c=row('a',1,0),row('b',3,1000),row('c',5,12000)
    b['usage']['cache_read_tokens']=None
    s,d=analyse([a,b,c],{'c'})
    assert d['c']['previous_request_id']=='b'
    assert d['c']['reason']=='read_or_input_count_missing'


def test_sql_full_window_pagination_baseline_and_persistence(tmp_path,monkeypatch):
    s=Store(tmp_path)
    monkeypatch.setattr(s,'latest_rate',lambda *_:{'provider':'openai-codex','model':'example','service_tier':'standard','input_tokens':'1','output_tokens':'2','cache_read_tokens':'.1','cache_write_tokens':'1.25'})
    for i,read in enumerate((0,12000,15000,15000)):
        s.request(row(str(i),10+i*10,read),'request_completed')
    with s.db() as c:
        before=[tuple(r) for r in c.execute('SELECT id,data FROM main.requests ORDER BY id')]
    d=s.read(start=20,end=50,provider='openai-codex',limit=1,offset=1)
    p=d['cache_read_progression']
    assert p['positive_read_growth_tokens']==15000 and p['eligible_pairs']==3
    assert d['request_count']==3 and len(d['requests'])==1
    assert d['requests'][0]['cache_read_change']['previous_request_id']=='1'
    assert d['summary']['known']['cache_write_tokens']==0
    assert Decimal(d['summary']['cost_components']['cache_write_tokens'])==0
    # Full ledger rows and saved costs are byte-for-byte unchanged after reading.
    with s.db() as c:
        assert before==[tuple(r) for r in c.execute('SELECT id,data FROM main.requests ORDER BY id')]
    # A selected single request still uses the immediate preceding observation,
    # not a zero baseline or another page's first row.
    single=s.read(start=30,end=31)
    assert single['cache_read_progression']['positive_read_growth_tokens']==3000
    assert single['requests'][0]['cache_read_change']['previous_request_id']=='1'
    # Known compression stops that comparison.
    s.compression({'id':'compression','started':25,'ended':26,'session_id':'s','session_after':'s','provider':'openai-codex'})
    assert s.read(start=30,end=31)['cache_read_progression']['positive_read_growth_tokens'] is None


def test_sql_filters_do_not_invent_adjacent_reads(tmp_path):
    s=Store(tmp_path)
    a,b,c=row('a',10,0),row('b',20,5000),row('c',30,8000)
    # Different model intervenes; filtered-out rows still break the sequence.
    b['model']=b['response_model']='different';b['agent_kind']='subagent'
    a['agent_kind']=c['agent_kind']='primary'
    for r in (a,b,c):s.request(r,'request_completed')
    d=s.read(start=25,end=40,agent='primary')
    assert d['cache_read_progression']['positive_read_growth_tokens'] is None
