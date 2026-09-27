"""Fixture-only catalog refresh and historical read projection; no external endpoints."""
import json
import threading
import time
from decimal import Decimal

from _hermes_ai_usage_ledger_v2 import accounting, pricing, aggregate
from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
from _hermes_ai_usage_ledger_v2.storage import Store, summary
from test_provider_pricing import openai_fixture, openai_metadata

USAGE = dict(input_tokens=100, output_tokens=200, cache_read_tokens=50,
             cache_write_tokens=0, prompt_tokens=150, total_tokens=350,
             reasoning_tokens=0)


def record(key, provider='openai-codex', model='gpt-6-sol', **extra):
    return dict(id=key, provider=provider, model=model, started=time.time()-86400*3,
                ended=time.time()-86400*3+1, status='completed', session_id='fixture',
                usage=dict(USAGE), **extra)


def catalog(store, source, rates, stamp=None):
    stamp = stamp or time.time()
    snapshot = pricing._snapshot(source, rates, stamp, 'fixture_provider_catalog')
    with store.db() as c:
        c.execute('INSERT INTO provider_catalog(source_id,observed,data) VALUES(?,?,?)',
                  (source, stamp, json.dumps(snapshot)))
    return snapshot


def test_old_sol_joins_weekly_cost_without_mutation_or_double_count(tmp_path, monkeypatch):
    s = Store(tmp_path)
    # Simulate an installed catalogue predating the published Sol model.
    with s.db() as c:
        c.execute("DELETE FROM provider_catalog WHERE source_id='openai'")
    catalog(s, 'openai', [pricing.rate('older','standard',['1','2','0','0'])], time.time()-86400*5)
    s.request(record('past'), 'request_completed')
    with s.db() as c:
        original = c.execute("SELECT data FROM requests WHERE id='past'").fetchone()[0]
    assert json.loads(original)['cost']['rate'] is None
    assert s.read()['summary']['known_cost_usd'] == '0'
    monkeypatch.setattr(pricing, 'fetch', lambda *_: (_ for _ in ()).throw(AssertionError('read fetched prices')))
    # A new observed catalogue is deliberately later than the historical call.
    snap = catalog(s, 'openai', [r for r in pricing.parse_openai(openai_fixture(),openai_metadata())
                                  if r['model']=='gpt-6-sol'])
    window = s.read(start=time.time()-86400*7, end=time.time(), limit=1)
    [row] = window['requests']
    assert row['cost']['rate']['retrospective'] is True
    assert row['supplemental_valuation']['observed_at'] == snap['observed_at']
    assert row['supplemental_valuation']['not_historical_charge'] is True
    assert row['stored_accounting']['cost'] == json.loads(original)['cost']
    assert row['cost']['rate']['context_band'] == 'short'
    assert row['cost']['rate']['pricing_basis'] == 'subscription_api_equivalent'
    expected = accounting.costs(USAGE, row['cost']['rate'])['total_usd']
    assert Decimal(expected) > 0
    for report in (window['summary'],window['groups'][0],window['provider_groups'][0],
                   window['model_groups'][0],next(b for b in window['trend']['buckets'] if b['attempts']),
                   window['applied_rate_groups'][0]):
        assert Decimal(report['known_cost_usd']) == Decimal(expected)
        assert report['supplemental_requests'] == 1
    assert window['summary'] == summary(window['requests'])
    with s.db() as c:
        assert c.execute("SELECT data FROM requests WHERE id='past'").fetchone()[0] == original
    assert s.read()['summary']['known_cost_usd'] == expected


def test_saved_rate_untouched_and_serving_provider_isolation(tmp_path):
    s=Store(tmp_path)
    recent=record('priced', model='gpt-6-sol');recent.update(started=time.time()-2,ended=time.time()-1)
    s.request(recent, 'request_completed')
    saved=s.read()['requests'][0]['cost']
    # Only router's own catalog qualifies; vendor rate cannot leak across routes.
    s.request(record('router',provider='openrouter'), 'request_completed')
    s.request(record('unknown',model='gpt-6-sol-900k'), 'request_completed')
    s.request(record('tier',service_tier='unknown-tier'), 'request_completed')
    catalog(s,'openai',[pricing.rate('gpt-6-sol','standard',['900','900','900','900'])])
    rows={r['id']:r for r in s.read()['requests']}
    assert rows['priced']['cost']==saved and 'supplemental_valuation' not in rows['priced']
    assert all(rows[k]['cost']['rate'] is None for k in ('router','unknown','tier'))
    catalog(s,'openrouter',[pricing.rate('gpt-6-sol','standard',['3','4','0','0'])])
    routed={r['id']:r for r in s.read()['requests']}['router']
    assert routed['supplemental_valuation']['source_id']=='openrouter'
    assert routed['cost']['rate']['input_tokens']=='3'
    assert s.read()['summary']['supplemental_requests']==1


def test_long_context_tier_and_missing_fields_parity(tmp_path):
    s=Store(tmp_path)
    with s.db() as c:c.execute("DELETE FROM provider_catalog WHERE source_id='openai'")
    for key,prompt,tier in [('short',272000,'standard'),('long',272001,'priority')]:
        r=record(key,service_tier=tier)
        r['usage']['prompt_tokens']=prompt
        s.request(r,'request_completed')
    catalog(s,'openai',[r for r in pricing.parse_openai(openai_fixture(),openai_metadata())
                        if r['model']=='gpt-6-sol'])
    d=s.read()
    rows={r['id']:r for r in d['requests']}
    assert rows['short']['cost']['rate']['context_band']=='short'
    assert rows['long']['cost']['rate']['context_band']=='long'
    assert rows['long']['cost']['rate']['service_tier']=='fast'
    assert d['summary']==summary(d['requests'])
    assert sum(Decimal(g['known_cost_usd']) for g in d['applied_rate_groups'])==Decimal(d['summary']['known_cost_usd'])


def test_missing_usage_stays_unknown_and_original_nonzero_cost_is_not_replaced(tmp_path):
    s=Store(tmp_path)
    with s.db() as c:c.execute("DELETE FROM provider_catalog WHERE source_id='openai'")
    partial=record('partial');partial['usage']['cache_read_tokens']=None
    s.request(partial,'request_completed')
    s.request(record('existing'),'request_completed')
    with s.db() as c:
        before=c.execute("SELECT data FROM requests WHERE id='existing'").fetchone()[0]
        saved=json.loads(before)
        saved['cost']['known_components_usd']='0.001'
        c.execute('UPDATE requests SET data=? WHERE id=?',(json.dumps(saved),'existing'))
    catalog(s,'openai',[r for r in pricing.parse_openai(openai_fixture(),openai_metadata())
                        if r['model']=='gpt-6-sol'])
    rows={r['id']:r for r in s.read()['requests']}
    assert rows['partial']['supplemental_valuation']['not_historical_charge']
    assert rows['partial']['cost']['components']['cache_read_tokens'] is None
    assert rows['partial']['cost']['complete'] is False
    assert rows['existing']['cost']==saved['cost']
    assert 'supplemental_valuation' not in rows['existing']
    assert s.read()['summary']==summary(list(rows.values()))


def test_daily_success_retry_failure_and_manual_interval(tmp_path, monkeypatch):
    s=Store(tmp_path)
    base=1724198390  # ten seconds before a UTC day boundary
    clock=[base]
    monkeypatch.setattr(pricing.time,'time',lambda:clock[0])
    calls=[]
    def fetch(url):
        calls.append(url)
        if url==pricing.SOURCES['openai']['url']:return openai_metadata()
        if url==pricing.OPENAI_TABLE_URL:return openai_fixture()
        raise ConnectionError('fixture failure')
    pricing.refresh(s,fetcher=fetch)
    assert len(calls)==6
    clock[0]+=8
    pricing.refresh(s,fetcher=fetch)
    assert len(calls)==6  # successful same day; failures still within retry bound
    clock[0]+=3
    pricing.refresh(s,fetcher=fetch)
    assert len(calls)==8  # UTC rollover refreshes OpenAI, failures remain bounded
    pricing.refresh(s,force=True,fetcher=fetch)
    assert len(calls)==8  # explicit refresh retains a minute floor
    clock[0]+=61
    pricing.refresh(s,force=True,fetcher=fetch)
    assert len(calls)==14


def test_concurrent_refresh_coalesces_per_profile(tmp_path):
    s=Store(tmp_path)
    entered=threading.Event();release=threading.Event();responses=[]
    def fetch(url):
        entered.set();assert release.wait(3)
        raise ConnectionError('fixture offline')
    t=threading.Thread(target=lambda:responses.append(pricing.refresh(s,fetcher=fetch)))
    t.start();assert entered.wait(3)
    assert pricing.refresh(s,fetcher=fetch) is False
    release.set();t.join(3)
    assert not t.is_alive() and responses==[True]
    with s.db() as c:
        assert c.execute('SELECT COUNT(*) FROM pricing_status').fetchone()[0]==len(pricing.SOURCES)


def test_manual_requests_share_existing_background_worker(tmp_path, monkeypatch):
    s=Store(tmp_path)
    monkeypatch.delenv('HERMES_USAGE_PRICING_OFFLINE',raising=False)
    entered=threading.Event();release=threading.Event();manual=threading.Event();calls=[]
    def fake_refresh(store,force=False):
        calls.append(force)
        if not force:
            entered.set();assert release.wait(3)
        else:manual.set()
    monkeypatch.setattr(pricing,'refresh',fake_refresh)
    pricing.start_worker(s)
    assert entered.wait(3)
    worker=pricing._WORKERS[str(s.path.resolve())][0]
    for _ in range(5):pricing.start_worker(s,force=True)
    assert pricing._WORKERS[str(s.path.resolve())][0] is worker
    release.set()
    assert manual.wait(3)
    assert calls==[False,True]


def test_read_only_aggregate_merges_two_profile_supplements(tmp_path):
    stores=[Store(tmp_path),Store(tmp_path/'profiles'/'other')]
    for n,s in enumerate(stores):
        with s.db() as c:c.execute("DELETE FROM provider_catalog WHERE source_id='openai'")
        s.request(record('past-'+str(n)), 'request_completed')
        catalog(s,'openai',[r for r in pricing.parse_openai(openai_fixture(),openai_metadata())
                            if r['model']=='gpt-6-sol'])
    before=[]
    for s in stores:
        with s.db() as c:before.append(c.execute('SELECT data FROM requests').fetchone()[0])
    runtime=AnalyticsRuntime()
    try:
        end=time.time()+1
        first=aggregate.ledger(runtime,aggregate.discover(tmp_path),start=end-86400*7,end=end,limit=1)
        second=aggregate.ledger(runtime,aggregate.discover(tmp_path),start=end-86400*7,end=end,offset=1,limit=1)
        assert first['coverage']['status']=='complete'
        assert first['summary']['supplemental_requests']==2
        assert first['request_count']==2 and len(first['requests'])==len(second['requests'])==1
        assert first['summary']==second['summary']
        assert sum(Decimal(r['cost']['total_usd']) for r in first['requests']+second['requests'])==Decimal(first['summary']['known_cost_usd'])
        assert sum(g['supplemental_requests'] for g in first['provider_groups'])==2
        assert sum(b['supplemental_requests'] for b in first['trend']['buckets'])==2
        assert all(r['stored_accounting']['cost']['rate'] is None for r in first['requests']+second['requests'])
    finally:runtime._discard(runtime.current)
    for s,raw in zip(stores,before):
        with s.db() as c:assert c.execute('SELECT data FROM requests').fetchone()[0]==raw
