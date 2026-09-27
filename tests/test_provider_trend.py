"""Per-provider trend partitions on disposable ledgers; no live profiles."""
import json
from copy import deepcopy
import pytest
from _hermes_ai_usage_ledger_v2.storage import Store
from _hermes_ai_usage_ledger_v2 import aggregate
from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime


def add(store, rows):
    with store.db() as c:
        for n, (provider, stamp, tokens, dollars) in enumerate(rows):
            key=f'{provider}-{stamp}-{n}'
            usage=None if tokens is None else {'total_tokens':tokens,'input_tokens':tokens,
                'output_tokens':0,'cache_read_tokens':0,'cache_write_tokens':0,
                'prompt_tokens':tokens,'reasoning_tokens':0}
            cost=None if dollars is None else {'total_usd':str(dollars),'known_components_usd':str(dollars),
                'components':{'input_tokens':str(dollars),'output_tokens':'0',
                    'cache_read_tokens':'0','cache_write_tokens':'0'},'complete':True}
            record={'id':key,'started':stamp,'ended':stamp+1,'provider':provider,
                'model':'fixture','session_id':key,'task':'main','status':'completed',
                'usage':usage,'cost':cost}
            c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                (key,stamp,stamp+1,provider,'fixture',key,'main',None,'completed',json.dumps(record)))


def shape(report):
    return [(r['start'],r['end'],r['attempts'],
             [(p['provider'],p['attempts'],p['known']['total_tokens'],p['missing_fields']['total_tokens'],
               p['known_cost_usd'],p['unpriced_requests']) for p in r['provider_buckets']])
            for r in report['trend']['buckets']]


def test_selected_shared_edges_real_shapes_and_missing(tmp_path):
    store=Store(tmp_path)
    add(store,[('alpha',120,10,'0.10'),('alpha',125,20,'0.20'),
               ('beta',180,90,'0.90'),('beta',240,None,None)])
    report=store.read(60,360,view='overview',group='time',list_mode='page',limit=1)
    rows=shape(report)
    assert report['trend']['seconds']==60
    assert rows[0]==(60,120,0,[])
    assert rows[1]==(120,180,2,[('alpha',2,30,0,'0.30',0)])
    assert rows[2]==(180,240,1,[('beta',1,90,0,'0.90',0)])
    assert rows[3][3]==[('beta',1,0,1,'0',1)]
    assert report['list_count']==5 and len(report['list_rows'])==1
    assert [r['attempts'] for r in report['trend']['buckets']]==[0,2,1,1,0]
    assert report['summary']['attempts']==4
    assert store.read(60,360,provider='alpha',view='overview',group='time')['trend']['buckets'][1]['known']['total_tokens']==30


def test_all_profiles_merge_and_partial_coverage(tmp_path):
    first=Store(tmp_path);second=Store(tmp_path/'profiles'/'second')
    add(first,[('alpha',120,10,'0.10'),('beta',180,30,'0.30')])
    add(second,[('alpha',120,7,'0.07'),('beta',240,4,'0.04')])
    runtime=AnalyticsRuntime()
    report=aggregate.ledger(runtime,aggregate.discover(tmp_path),start=60,end=360,view='overview',group='time',list_mode='page',limit=1)
    rows=shape(report)
    assert rows[1][3]==[('alpha',2,17,0,'0.17',0)]
    assert rows[2][3]==[('beta',1,30,0,'0.30',0)]
    assert rows[3][3]==[('beta',1,4,0,'0.04',0)]
    assert report['summary']['attempts']==4
    assert len(report['trend']['buckets'])==5 and len(report['list_rows'])==1
    inventory=aggregate.discover(tmp_path)
    inventory['profiles'][1]['availability']={'status':'unavailable','reason':'synthetic unreadable source'}
    partial=aggregate.ledger(runtime,inventory,start=60,end=360,view='overview',group='time',list_mode='page',limit=1)
    assert partial['coverage']['status']=='partial'
    assert partial['summary']['attempts']==2
    assert shape(partial)[1][3]==[('alpha',1,10,0,'0.10',0)]


def test_signed_full_and_changed_bucket(tmp_path):
    store=Store(tmp_path)
    add(store,[('alpha',120,10,'0.10'),('beta',180,30,'0.30')])
    runtime=AnalyticsRuntime()
    args=(60,360,'',0,10,'','','','exact','','','')
    before=runtime.read(tmp_path,*args,view='overview',group='time')
    assert before['trend']['buckets'][1]['provider_buckets'][0]['provider']=='alpha'
    add(store,[('beta',121,9,'0.09')])
    after=runtime.refresh(tmp_path,*args,resume_token=before['incremental']['resume_token'],view='overview',group='time')
    fresh=runtime.read(tmp_path,*args,view='overview',group='time')
    assert shape(after)==shape(fresh)
    assert after['summary']==fresh['summary']
    assert after['provider_groups']==fresh['provider_groups']
    assert after['trend']['buckets'][1]['provider_buckets'][1]['provider']=='beta'


@pytest.mark.parametrize('end,bucket,first,second,seconds',[
    (360,120,10,20,60),
    (3660,240,70,90,120),
    (50000,3600,300,420,3600),
    (259260,86400,3600,7200,86400),
])
@pytest.mark.parametrize('group',['time','model'])
def test_signed_changed_bucket_preserves_full_window_provider_partition(
        tmp_path,end,bucket,first,second,seconds,group):
    store=Store(tmp_path)
    add(store,[('alpha',bucket+first,None,None),('beta',bucket+first,100,'1.00')])
    runtime=AnalyticsRuntime()
    args=(60,end,'alpha',0,10,'','','','exact','','','')
    baseline=runtime.read(tmp_path,*args,view='overview',group=group)
    assert baseline['trend']['seconds']==seconds
    add(store,[('alpha',bucket+second,9,'0.09')])
    delta=runtime.refresh(tmp_path,*args,resume_token=baseline['incremental']['resume_token'],
                          view='overview',group=group)
    fresh=runtime.read(tmp_path,*args,view='overview',group=group)
    assert delta['incremental']['mode']=='delta'
    assert delta['window']==fresh['window']
    assert delta['trend']==fresh['trend']
    assert delta['summary']==fresh['summary']
    assert delta['provider_groups']==fresh['provider_groups']
    if group=='model':
        assert delta['model_groups']==fresh['model_groups']
    partition=next(row['provider_buckets'] for row in delta['trend']['buckets']
                   if row['start']<=bucket+first<row['end'])
    assert len(partition)==1 and partition[0]['provider']=='alpha'
    assert partition[0]['attempts']==2
    assert partition[0]['known']['total_tokens']==9
    assert partition[0]['missing_fields']['total_tokens']==1
    assert partition[0]['known_cost_usd']=='0.09'
    assert partition[0]['unpriced_requests']==1


def test_legacy_signed_peak_receipt_without_provider_partitions_reads_full(tmp_path):
    store=Store(tmp_path)
    add(store,[('alpha',120,10,'0.10'),('beta',180,30,'0.30'),
               ('alpha',240,None,None),('beta',300,7,'0.07')])
    runtime=AnalyticsRuntime()
    args=(60,360,'',0,10,'','','','exact','','','')
    first=runtime.read(tmp_path,*args,view='overview',group='time')
    root,profile,revision,saved_args,view,group,identity,generation,base,seed=runtime._unseal(
        first['incremental']['resume_token'])
    old=deepcopy(base)
    for bucket in old['trend']['buckets']:
        bucket.pop('provider_buckets')
    assert all('peak_observed' in bucket for bucket in old['trend']['buckets'])
    receipt=runtime._seal(tmp_path,args,view,group,old,seed,identity,generation,profile)
    # Only one narrow request bucket changes: a reducer that blindly carries
    # the legacy rows would leave four of five partitions absent.
    add(store,[('beta',121,9,'0.09')])
    refreshed=runtime.refresh(tmp_path,*args,resume_token=receipt,view=view,group=group)
    fresh=runtime.read(tmp_path,*args,view=view,group=group)
    assert refreshed['incremental']['mode']=='snapshot'
    assert refreshed['summary']==fresh['summary']
    assert refreshed['provider_groups']==fresh['provider_groups']
    assert refreshed['trend']==fresh['trend']
    assert len(refreshed['trend']['buckets'])==5
    assert all(isinstance(bucket['provider_buckets'],list) for bucket in refreshed['trend']['buckets'])
    assert shape(refreshed)==shape(fresh)
