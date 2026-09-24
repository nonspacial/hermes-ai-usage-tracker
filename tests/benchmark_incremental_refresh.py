"""Offline fixed synthetic benchmark; no live profile, account or provider access.

Run: .venv/bin/python tests/benchmark_incremental_refresh.py
"""
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ledger_runtime.storage import Store
from ledger_runtime.analytics_reload import AnalyticsRuntime

ORIGIN=1_000_000.


def record(key,started,session,reads):
    data=dict(id=key,started=started,ended=started+1,provider='fixture',model='m',
              session_id=session,source='main_hook',task='main',status='completed',
              usage={'cache_read_tokens':reads,'prompt_tokens':reads,'total_tokens':reads})
    return (key,started,started+1,'fixture','m',session,'main',None,'completed',
            json.dumps(data,separators=(',',':')))


def measure(store,runtime,args,key,started,*,group='time',advance=0):
    t=time.perf_counter()
    base=runtime.read(store.root,*args,view='overview',group=group)
    cold=time.perf_counter()-t
    with store.db() as connection:
        connection.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                           record(key,started,key,123))
    current=(args[0]+advance,args[1]+advance,*args[2:])
    t=time.perf_counter()
    delta=runtime.refresh(store.root,*current,resume_token=base['incremental']['resume_token'],group=group)
    refresh=time.perf_counter()-t
    t=time.perf_counter()
    full=runtime.read(store.root,*current,view='overview',group=group)
    full_time=time.perf_counter()-t
    excluded={'generated_at','incremental'}
    assert {k:v for k,v in delta.items() if k not in excluded}=={
        k:v for k,v in full.items() if k not in excluded}
    return dict(rows_before=base['request_count'],cold_full_s=round(cold,4),
                few_changes_s=round(refresh,4),full_after_s=round(full_time,4),
                mode=delta['incremental']['mode'],
                changed_buckets=delta['incremental'].get('changed_buckets'),
                signed_token_bytes=len(base['incremental']['resume_token']))


def main():
    with tempfile.TemporaryDirectory(prefix='ai-usage-delta-benchmark-') as root:
        store=Store(root)
        with store.db() as connection:
            connection.executemany('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',(
                record(f'r{i}',ORIGIN+i*100,f's{i%40}',i*3) for i in range(6000)))
        runtime=AnalyticsRuntime()
        suffix=('',0,50,'','','','exact','','','')
        seven=measure(store,runtime,(ORIGIN,ORIGIN+604800.,*suffix),
                      'seven-new',ORIGIN+600000.)
        hours=measure(store,runtime,(ORIGIN+490000.,ORIGIN+510000.,*suffix),
                      'hours-new',ORIGIN+509000.)
        rolling=measure(store,runtime,(ORIGIN+400000.,ORIGIN+486400.,*suffix),
                        'rolling-new',ORIGIN+486450.,group='model',advance=100)
        print(json.dumps({'fixture_requests':6000,'seven_days':seven,'hours_gap':hours,
                          'rolling_24h_model':rolling}))


if __name__=='__main__':
    main()
