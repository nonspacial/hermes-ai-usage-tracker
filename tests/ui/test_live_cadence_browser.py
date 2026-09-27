"""Offline read cadence: real-time notify reads, quiet rolling expiry, coalescing.

Exercises the packaged coordinator (fake clock/rest) and the mounted preview
pane (Playwright clock, synthetic fixture). No real ledger, host or provider.
"""
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]

PROBE = r'''async () => {
 const original=rest, realNow=Date.now, reports=[];
 const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve()};
 const coverage={status:'complete',discovery:{status:'complete',errors:[]},
  profiles:[{profile_id:'p-infra',name:'infra',aliases:[],status:'available'}]};
 // Wall clock pinned 20 minutes into an hour so a 60 s shift stays inside
 // one hourly trend bucket unless a case moves it deliberately.
 let wall=Math.floor(realNow()/3600000)*3600000+20*60000;
 Date.now=()=>wall;
 function setup({windowSeconds=86400,view='requests',extra={}}={}){
  let token='t1',revision='rev-1',reads=[],calls=[],publications=[],tasks=[],time=0,holdToken=null;
  // body: extra top-level DTO fields merged into every response (per case).
  const shape={first:0,pending:0,unresolved:0,attempts:5,compressions:0,body:{}};
  const clock={now:()=>time,setTimeout(fn,ms){const task={fn,ms};tasks.push(task);return task},
   clearTimeout(task){tasks=tasks.filter(t=>t!==task)}};
  // Each fixture owns the global transport while active; install() re-binds.
  const transport=(path)=>{
   calls.push(path.split('?')[0]);
   if(path.startsWith('/ledger/change-token')){
    const answer=()=>({version:1,profile_scope:'selected',
     read_only:true,token_kind:'opaque-filesystem-hint',change_token:token,coverage,capabilities:{change_check:true}});
    // A held probe answers with the token current at release time.
    if(holdToken)return new Promise(resolve=>holdToken.push(()=>resolve(answer())));
    return Promise.resolve(answer());
   }
   if(path.startsWith('/ledger/analytics'))return Promise.resolve({revision,restart_required:false});
   if(path.startsWith('/ledger?'))return new Promise((resolve,reject)=>reads.push({path,reject,
    resolve:(extra={})=>{
     const q=new URLSearchParams(path.split('?')[1]),start=Number(q.get('start'));
     const end=windowSeconds?start+windowSeconds:wall/1000,step=3600,edge=(Math.floor(start/step)+1)*step;
     const peak={value:null,unmatched:0,basis:'matched_child_lifecycle'};
     resolve({generated_at:end,seq:7,analytics_revision:revision,window:{start,end,basis:'fixture'},
      summary:{attempts:shape.attempts,pending:shape.pending,unresolved:shape.unresolved},
      compression_count:shape.compressions,...shape.body,
      trend:{unit:'hour',timezone:'UTC',seconds:step,buckets:[
       {start,end:edge,attempts:shape.first,peak_observed:peak,provider_buckets:[]},
       {start:edge,end:Math.floor(end/step)*step,attempts:shape.attempts-shape.first,peak_observed:peak,provider_buckets:[]},
       {start:Math.floor(end/step)*step,end,attempts:0,peak_observed:peak,provider_buckets:[]}]},
      requests:[{id:'r'+reads.length}],...extra});
    }}));
   return original(path);
  };
  const install=()=>{rest=transport};install();
  const owner=makeLedgerCoordinator({profile:'infra',
   path:'/ledger?'+new URLSearchParams({profile:'infra',start:'0',view,...extra}),
   windowSeconds,clock,connection:ledgerMemoryConnection,publish:p=>publications.push(p)});
  const run=async()=>{const pending=tasks;tasks=[];for(const task of pending)task.fn();await flush()};
  const count=route=>calls.filter(c=>c===route).length;
  return {owner,reads,publications,run,count,shape,install,get tasks(){return tasks},
   hold:()=>{holdToken=[]},release:async()=>{const held=holdToken||[];holdToken=null;for(const fn of held)fn();await flush()},
   get time(){return time},
   // The coordinator clock and the wall clock can move separately.
   advance:(ms,wallMs=ms)=>{time+=ms;wall+=wallMs},setToken:v=>token=v,setRevision:v=>revision=v};
 }
 const read=async(x,extra)=>{x.reads.at(-1).resolve(extra);await flush()};
 try{
  // (1) A recorder notify hint reads after the short debounce and publishes
  // the new response. Every read re-validates the analytics revision first.
  let x=setup();x.owner.start();await flush();
  if(x.reads.length!==1||x.count('/ledger/analytics')!==1||x.count('/ledger/change-token')!==1)
   throw Error('initial read shape '+JSON.stringify([x.reads.length,x.count('/ledger/analytics')]));
  await read(x);
  if(x.count('/ledger/change-token')!==2)throw Error('post-read check missing');
  x.setToken('t2');x.owner.hint();
  if(x.tasks.length!==1||x.tasks[0].ms!==150)throw Error('notify debounce '+JSON.stringify(x.tasks.map(t=>t.ms)));
  await x.run();
  if(x.reads.length!==2)throw Error('notify did not read');
  await read(x,{requests:[{id:'notified'}]});
  const painted=x.publications.filter(p=>p.data).at(-1);
  if(painted.data.requests[0].id!=='notified'||painted.fetching!==false)throw Error('notify response not published');
  if(x.count('/ledger/analytics')!==2)throw Error('analytics revision must be checked before every read');
  if(x.count('/ledger/change-token')!==4)throw Error('notify read must take its own pre/post token checks');
  reports.push('notify hint → 150 ms read → publish; revision checked per read');

  // (2) Rolling expiry with an unchanged, certified token: one cheap check,
  // no full read, only the window edges advance; totals and rows untouched.
  // The token confirmation restarts the freshness age (as a read did).
  x.advance(60000);const tokens=x.count('/ledger/change-token');
  const before=x.publications.filter(p=>p.data).at(-1).data;
  await x.owner.expire();await flush();
  if(x.reads.length!==2||x.tasks.length)throw Error('quiet expiry issued a full read');
  if(x.count('/ledger/change-token')!==tokens+1)throw Error('expiry must check the change token once');
  const shifted=x.publications.at(-1);
  if(!shifted.data||shifted.fetching!==undefined||shifted.received!==x.time)throw Error('skip must publish its confirmation time');
  if(shifted.data.window.start!==before.window.start+60||shifted.data.window.end!==before.window.end+60||
     shifted.data.trend.buckets[0].start!==before.window.start+60||shifted.data.trend.buckets[2].end!==before.window.end+60||
     shifted.data.trend.buckets[1]!==before.trend.buckets[1]||shifted.data.summary!==before.summary||
     shifted.data.requests!==before.requests||shifted.data.seq!==before.seq)throw Error('shift changed more than edges');
  // Four more quiet minutes stay read-free; the fifth forces reconciliation.
  for(let i=0;i<3;i++){x.advance(60000);await x.owner.expire();await flush()}
  if(x.reads.length!==2)throw Error('quiet minutes read');
  x.advance(60000);await x.owner.expire();await flush();
  if(x.tasks.length!==1||x.tasks[0].ms!==150)throw Error('5-minute reconciliation missing');
  await x.run();if(x.reads.length!==3)throw Error('reconciliation read missing');
  await read(x);
  reports.push('quiet expiry: one token check, zero full reads, edges only, age restarts; 5-minute reconciliation');

  // Expiry reads whenever the shift could change a displayed field. A changed
  // token is observed by the expiry probe, which serves as the pre-read check.
  const needsRead=async(label,prepare)=>{
   const y=setup();y.owner.start();await flush();await read(y);
   const n=y.count('/ledger/change-token');
   y.advance(60000);prepare(y);
   await y.owner.expire();await flush();
   if(y.tasks.length!==1||y.tasks[0].ms!==150)throw Error(label+': expiry did not schedule a read');
   await y.run();
   if(y.reads.length!==2)throw Error(label+': expiry read missing');
   if(y.count('/ledger/change-token')!==n+1)throw Error(label+': pre-read token not reused '+(y.count('/ledger/change-token')-n));
   await read(y);
   if(y.count('/ledger/change-token')!==n+2)throw Error(label+': post-read check missing');
   y.owner.dispose();
  };
  await needsRead('changed token',y=>y.setToken('t-new'));
  // A response that cannot prove every displayed field unchanged reads
  // without even probing the token.
  const unprovable=async(label,prepare,wallMs=60000)=>{
   const y=setup();prepare(y);y.owner.start();await flush();await read(y);
   const n=y.count('/ledger/change-token');
   y.advance(60000,wallMs);await y.owner.expire();await flush();
   if(y.tasks.length!==1||y.tasks[0].ms!==150)throw Error(label+': skipped');
   if(y.count('/ledger/change-token')!==n)throw Error(label+': probed an unprovable skip');
   await y.run();if(y.reads.length!==2)throw Error(label+': read missing');
   y.owner.dispose();
  };
  await unprovable('leading attempts',y=>{y.shape.first=2});
  await unprovable('owner-live request',y=>{y.shape.pending=1});
  await unprovable('unresolved request',y=>{y.shape.unresolved=1});
  await unprovable('unknown execution state',y=>{y.shape.unresolved=undefined});
  await unprovable('compression in window (C1)',y=>{y.shape.compressions=1});
  await unprovable('compression count unknown',y=>{y.shape.compressions=null});
  await unprovable('quota observation in window',y=>{y.shape.body={quota_observations:[{ts:1}]}});
  await unprovable('crossing count',y=>{y.shape.body={crossing_start:0}});
  await unprovable('fresh price catalogue (UTC-day stale flag)',y=>{y.shape.body={price_catalogs:[{source_id:'a',stale:false}]}});
  await unprovable('unrecognised field',y=>{y.shape.body={future_rate_per_hour:3}});
  await unprovable('bucket boundary crossing',()=>{},40*60000);
  const progression=async(label,path,expectSkip)=>{
   const y=setup({extra:path});y.shape.body={cache_read_progression:{eligible_pairs:0}};
   y.owner.start();await flush();await read(y);y.advance(60000);await y.owner.expire();await flush();
   if(!!y.tasks.length===expectSkip)throw Error(label);y.owner.dispose();
  };
  await progression('filtered cache-read progression skipped',{provider:'p1'},false);
  await progression('unfiltered cache-read progression read',{},true);
  const stale=setup();stale.shape.body={price_catalogs:[{source_id:'a',stale:true}],quota_observations:[]};
  stale.owner.start();await flush();await read(stale);stale.advance(60000);
  await stale.owner.expire();await flush();
  if(stale.tasks.length||stale.reads.length!==1)throw Error('provably unchanged optional fields forced a read');
  stale.owner.dispose();
  // Compressions view (the helper above builds a requests view).
  const comp=setup({view:'compressions'});comp.owner.start();await flush();await read(comp);
  comp.advance(60000);await comp.owner.expire();await flush();
  if(comp.tasks.length!==1)throw Error('Compressions view skipped');comp.owner.dispose();
  reports.push('expiry reads on changed token, leading attempts, owner-live/unresolved, any compression, quota, crossing, fresh catalogue, unknown field, bucket crossing, filtered cache-read progression, Compressions view');

  // C1 end to end: a compression in the leading bucket ages out; the tick
  // reads and the new count is published.
  const aged=setup();aged.shape.compressions=1;aged.owner.start();await flush();await read(aged);
  aged.advance(60000);aged.shape.compressions=0;await aged.owner.expire();await flush();await aged.run();
  await read(aged);
  if(aged.publications.filter(p=>p.data).at(-1).data.compression_count!==0||aged.reads.length!==2)
   throw Error('aged-out compression count not refreshed');
  // With no compression left, the next quiet minute may skip.
  aged.advance(60000);await aged.owner.expire();await flush();
  if(aged.tasks.length||aged.reads.length!==2)throw Error('proved-empty window did not skip');
  aged.owner.dispose();
  reports.push('C1: compression ageing out of the leading bucket → full read, count 1 → 0');

  // An expiry tick during a flight is re-evaluated when that flight settles
  // (its rolling start was sampled before the tick).
  const flight=setup();flight.shape.first=1;flight.owner.start();await flush();await read(flight);
  flight.owner.hint();await flight.run();
  flight.advance(60000);await flight.owner.expire();await flush();
  if(flight.tasks.length)throw Error('expiry during flight started a read');
  await read(flight);
  if(flight.tasks.length!==1||flight.tasks[0].ms!==150)throw Error('expiry during flight was dropped');
  flight.owner.dispose();
  // A tick just after a read is still evaluated (no 30 s suppression).
  const recent=setup();recent.owner.start();await flush();await read(recent);
  recent.advance(10000,10000);await recent.owner.expire();await flush();
  if(recent.tasks.length||recent.count('/ledger/change-token')!==3||recent.publications.at(-1).received!==recent.time)
   throw Error('tick after a recent read not confirmed');
  recent.owner.dispose();
  reports.push('expiry during a flight re-evaluated on settle; recent read does not suppress a tick');

  // M1: a notify arriving while an expiry probe is held does not reuse that
  // probe; its read takes a fresh pre-read token and publishes no stale shift.
  const held=setup();held.owner.start();await flush();await read(held);
  held.advance(60000);held.hold();const pending=held.owner.expire();await flush();
  held.setToken('t-notify');held.owner.hint();await held.run();
  const probes=held.count('/ledger/change-token');
  await held.release();await pending;await flush();
  if(held.reads.length!==2)throw Error('notify during held probe did not read');
  if(held.count('/ledger/change-token')<probes)throw Error('probe count');
  if(held.publications.some(p=>p.data&&!Number.isFinite(p.received))||held.publications.filter(p=>p.data).length!==1)throw Error('stale shift published during notify');
  await read(held);
  if(held.publications.filter(p=>p.data).at(-1).received===undefined)throw Error('notify read not published');
  held.owner.dispose();
  reports.push('notify during a held expiry probe reads with its own token; no stale shift');

  // (3) Overlapping hints, polls and expiry during one flight: exactly one
  // trailing read, then quiet.
  x.install();x.setToken('t3');x.owner.hint();await x.run();
  if(x.reads.length!==4)throw Error('burst setup');
  for(let i=0;i<25;i++)x.owner.hint();
  x.setToken('t4');await x.owner.checkForChanges();await x.owner.expire();x.owner.hint();await flush();
  if(x.reads.length!==4||x.tasks.length)throw Error('read started during flight');
  await read(x);
  if(x.tasks.length!==1||x.tasks[0].ms!==150)throw Error('trailing read not scheduled once '+x.tasks.length);
  x.owner.hint();x.owner.hint();
  if(x.tasks.length!==1)throw Error('queued hints not joined');
  await x.run();
  if(x.reads.length!==5)throw Error('trailing read missing');
  await read(x);await x.run();
  if(x.reads.length!==5||x.tasks.length)throw Error('extra reads after trailing read');
  reports.push('overlapping hints/poll/expiry in flight → exactly one trailing read');

  // (4) A changed analytics revision clears the displayed body (pending) as
  // soon as it is seen, before that read's response is admitted.
  x.setRevision('rev-2');x.owner.hint();await x.run();
  const cleared=x.publications.findLastIndex(p=>p.invalidated===true&&p.data===null);
  if(cleared<0||x.publications.slice(cleared+1).some(p=>p.data))throw Error('revision change did not clear the body before admission');
  await read(x);await x.run();
  if(x.reads.length!==6||x.tasks.length||x.publications.filter(p=>p.data).at(-1).data.analytics_revision!=='rev-2')
   throw Error('revision change read');
  x.owner.dispose();
  reports.push('revision change clears the body before the read settles');
  return reports;
 }finally{rest=original;Date.now=realNow}
}'''


def mounted(browser):
    """Mounted pane: notify → read → repaint; quiet rolling expiry → no read."""
    page = browser.new_page(viewport={'width': 1500, 'height': 1000})
    errors, outbound = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('request', lambda r: outbound.append(r.url))
    page.route('**/*', lambda r: r.abort())
    # Fixed synthetic instant, 20 minutes into an hour, so the rolling 24h
    # window's leading hourly bucket is stable for the first minutes.
    page.clock.install(time=datetime(2026, 9, 27, 10, 20, tzinfo=timezone.utc))
    page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
    # Only closed requests (an owner-live request can change without a write)
    # and one compression, 30 s after the rolling 24h start, in a leading
    # bucket with no requests: it ages out of the window without any write.
    page.evaluate("""()=>{events=events.filter(e=>e.status!=='pending');
      const lead=compressions[0];compressions.length=0;
      const at=Date.now()/1000-86400+30;compressions.push({...lead,id:'lead-comp',started:at,ended:at+1})}""")
    page.evaluate('''()=>{window.ledgerReads=[];const o=rest;
      rest=(path,options)=>{if(path.startsWith('/ledger?'))ledgerReads.push({path,at:performance.now()});return o(path,options)}}''')
    page.get_by_role('tab', name='All providers', exact=True).click()
    quality = page.locator('.au-quality-line').first
    expect(quality).to_contain_text(re.compile(r'\d+ requests'))
    page.clock.run_for(2000)
    def attempts():
        match = re.search(r'([\d,]+) requests', quality.inner_text())
        assert match, quality.inner_text()
        return int(match.group(1).replace(',', ''))
    shown = attempts()
    def compressions_shown():
        match = re.search(r'([\d,]+) compressions', quality.inner_text())
        assert match, quality.inner_text()
        return int(match.group(1).replace(',', ''))
    # (C1) The leading-bucket compression is counted; the next minute's
    # expiry cannot prove it unchanged, so it reads and the count drops.
    expect(quality).to_contain_text('1 compressions')
    reads = page.evaluate('ledgerReads.length')
    page.clock.run_for(60000)
    page.wait_for_function('n=>ledgerReads.length>n', arg=reads)
    expect(quality).to_contain_text('0 compressions')
    assert compressions_shown() == 0
    page.clock.run_for(2000)
    reads = page.evaluate('ledgerReads.length')
    # (1) Recorder notify: one read within the short debounce, then repaint.
    page.evaluate('window.notifyAt=performance.now();addDemoEvent()')
    page.clock.run_for(200)
    page.wait_for_function('n=>ledgerReads.length>n', arg=reads)
    delay = page.evaluate('ledgerReads.at(-1).at-notifyAt')
    assert delay < 1000, delay
    expect(quality).to_contain_text(f'{shown + 1:,} requests')
    page.clock.run_for(2000)
    reads = page.evaluate('ledgerReads.length')
    tokens = page.evaluate("demoCalls.filter(p=>p.startsWith('/ledger/change-token')).length")
    # (2) Quiet rolling expiry over three visible minutes: change checks only.
    # Each token-confirmed skip restarts the freshness age, as a read did on
    # main: sampled once a minute at the same phase, the age never grows.
    ages = []
    for _ in range(3):
        page.clock.run_for(60000)
        age = page.get_by_test_id('connection-status').get_attribute('data-au-freshness-age')
        assert age is not None, age
        ages.append(int(age.rstrip('s')))
    assert len(set(ages)) == 1 and ages[0] < 60, ages
    assert page.evaluate('ledgerReads.length') == reads, 'quiet rolling expiry read the ledger'
    assert page.evaluate("demoCalls.filter(p=>p.startsWith('/ledger/change-token')).length") > tokens
    expect(quality).to_contain_text(f'{shown + 1:,} requests')
    # Forced reconciliation still bounds how long a missed hint can hide.
    page.clock.run_for(3 * 60000)
    page.wait_for_function('n=>ledgerReads.length>n', arg=reads)
    # A notify after quiet minutes is still immediate.
    reads = page.evaluate('ledgerReads.length')
    page.evaluate('addDemoEvent()')
    page.clock.run_for(200)
    page.wait_for_function('n=>ledgerReads.length>n', arg=reads)
    expect(quality).to_contain_text(f'{shown + 2:,} requests')
    assert page.get_by_role('button', name='Refresh', exact=True).count() == 1
    assert not errors, errors
    assert not outbound, outbound
    page.close()
    return delay


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
                                    headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
        print('PASS', '; '.join(page.evaluate(PROBE)))
        assert not errors, errors
        page.close()
        delay = mounted(browser)
        print(f'PASS mounted pane: aged-out leading compression read and repainted 1 → 0 compressions; notify read '
              f'started {delay:.0f} ms (fake clock) after the event and repainted; three quiet rolling minutes made '
              'change checks only, the freshness age resetting each minute as on main; reconciliation and later notify still read')
        browser.close()


if __name__ == '__main__':
    run()
