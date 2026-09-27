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
 function setup({windowSeconds=86400,view='requests'}={}){
  let token='t1',revision='rev-1',reads=[],calls=[],publications=[],tasks=[],time=0;
  const shape={first:0,pending:0,attempts:5};
  const clock={now:()=>time,setTimeout(fn,ms){const task={fn,ms};tasks.push(task);return task},
   clearTimeout(task){tasks=tasks.filter(t=>t!==task)}};
  // Each fixture owns the global transport while active; install() re-binds.
  const transport=(path)=>{
   calls.push(path.split('?')[0]);
   if(path.startsWith('/ledger/change-token'))return Promise.resolve({version:1,profile_scope:'selected',
    read_only:true,token_kind:'opaque-filesystem-hint',change_token:token,coverage,capabilities:{change_check:true}});
   if(path.startsWith('/ledger/analytics'))return Promise.resolve({revision,restart_required:false});
   if(path.startsWith('/ledger?'))return new Promise((resolve,reject)=>reads.push({path,reject,
    resolve:(extra={})=>{
     const q=new URLSearchParams(path.split('?')[1]),start=Number(q.get('start'));
     const end=windowSeconds?start+windowSeconds:wall/1000,step=3600,edge=(Math.floor(start/step)+1)*step;
     const peak={value:null,unmatched:0,basis:'matched_child_lifecycle'};
     resolve({generated_at:end,seq:7,analytics_revision:revision,window:{start,end,basis:'fixture'},
      summary:{attempts:shape.attempts,pending:shape.pending},
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
   path:'/ledger?'+new URLSearchParams({profile:'infra',start:'0',view}),
   windowSeconds,clock,connection:ledgerMemoryConnection,publish:p=>publications.push(p)});
  const run=async()=>{const pending=tasks;tasks=[];for(const task of pending)task.fn();await flush()};
  const count=route=>calls.filter(c=>c===route).length;
  return {owner,reads,publications,run,count,shape,install,get tasks(){return tasks},
   // The coordinator clock and the wall clock can move separately.
   advance:(ms,wallMs=ms)=>{time+=ms;wall+=wallMs},setToken:v=>token=v,setRevision:v=>revision=v};
 }
 const read=async(x,extra)=>{x.reads.at(-1).resolve(extra);await flush()};
 try{
  // (1) A recorder notify hint reads after the short debounce and publishes
  // the new response. Reads after the first reuse the verified revision.
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
  if(x.count('/ledger/analytics')!==1)throw Error('analytics info refetched on background read');
  if(x.count('/ledger/change-token')!==4)throw Error('notify read must take its own pre/post token checks');
  reports.push('notify hint → 150 ms read → publish; analytics info cached');

  // (2) Rolling expiry with an unchanged, certified token: one cheap check,
  // no full read, only the window edges advance; totals and rows untouched.
  x.advance(60000);const tokens=x.count('/ledger/change-token');
  const before=x.publications.filter(p=>p.data).at(-1).data;
  await x.owner.expire();await flush();
  if(x.reads.length!==2||x.tasks.length)throw Error('quiet expiry issued a full read');
  if(x.count('/ledger/change-token')!==tokens+1)throw Error('expiry must check the change token once');
  const shifted=x.publications.at(-1);
  if(!shifted.data||shifted.fetching!==undefined||shifted.received!==undefined)throw Error('shift must not claim a fresh read');
  if(shifted.data.window.start!==before.window.start+60||shifted.data.window.end!==before.window.end+60||
     shifted.data.trend.buckets[0].start!==before.window.start+60||shifted.data.trend.buckets[2].end!==before.window.end+60||
     shifted.data.trend.buckets[1]!==before.trend.buckets[1]||shifted.data.summary!==before.summary||
     shifted.data.requests!==before.requests||shifted.data.seq!==before.seq)throw Error('shift changed more than edges');
  // Four more quiet minutes stay read-free; the fifth forces reconciliation.
  for(let i=0;i<3;i++){x.advance(60000);await x.owner.expire();await flush()}
  if(x.reads.length!==2)throw Error('quiet minutes read');
  x.advance(60000);await x.owner.expire();await flush();
  if(x.tasks.length!==1||x.tasks[0].ms!==0)throw Error('5-minute reconciliation missing');
  await x.run();if(x.reads.length!==3)throw Error('reconciliation read missing');
  await read(x);
  reports.push('quiet expiry: one token check, zero full reads, edges only; 5-minute reconciliation');

  // Expiry reads whenever the shift could change a number.
  const needsRead=async(label,prepare)=>{
   const y=setup();y.owner.start();await flush();await read(y);
   const n=y.count('/ledger/change-token');
   y.advance(60000);prepare(y);
   await y.owner.expire();await flush();
   if(y.tasks.length!==1||y.tasks[0].ms!==0)throw Error(label+': expiry did not schedule a read');
   await y.run();
   if(y.reads.length!==2)throw Error(label+': expiry read missing');
   // The expiry probe serves as the read's pre-read check (no second probe).
   if(y.count('/ledger/change-token')!==n+1)throw Error(label+': pre-read token not reused '+(y.count('/ledger/change-token')-n));
   await read(y);
   if(y.count('/ledger/change-token')!==n+2)throw Error(label+': post-read check missing');
   y.owner.dispose();
  };
  await needsRead('changed token',y=>y.setToken('t-new'));
  const leading=setup();leading.shape.first=2;leading.owner.start();await flush();await read(leading);
  leading.advance(60000);await leading.owner.expire();await flush();
  if(leading.tasks.length!==1)throw Error('non-empty leading bucket skipped');leading.owner.dispose();
  const live=setup();live.shape.pending=1;live.owner.start();await flush();await read(live);
  live.advance(60000);await live.owner.expire();await flush();
  if(live.tasks.length!==1)throw Error('owner-live request skipped');live.owner.dispose();
  const crossing=setup();crossing.owner.start();await flush();await read(crossing);
  // One minute since the read, but the wall edge reaches the next hour.
  crossing.advance(60000,40*60000);await crossing.owner.expire();await flush();
  if(crossing.tasks.length!==1||crossing.count('/ledger/change-token')!==3)throw Error('bucket boundary crossing skipped');crossing.owner.dispose();
  const comp=setup({view:'compressions'});comp.owner.start();await flush();await read(comp);
  comp.advance(60000);await comp.owner.expire();await flush();
  if(comp.tasks.length!==1)throw Error('Compressions view skipped');comp.owner.dispose();
  const recent=setup();recent.owner.start();await flush();await read(recent);
  recent.advance(10000);await recent.owner.expire();await flush();
  if(recent.tasks.length||recent.count('/ledger/change-token')!==2)throw Error('just-sampled window re-evaluated');
  recent.owner.dispose();
  reports.push('expiry reads on changed token, leading attempts, owner-live, bucket crossing, Compressions view; skips a just-sampled window');

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

  // (4) Revision changes: a response from a reloaded backend is re-verified
  // once; a confirmed revision is adopted without a redundant full read.
  const infos=x.count('/ledger/analytics');x.setRevision('rev-2');
  x.owner.hint();await x.run();await read(x);await x.run();
  if(x.count('/ledger/analytics')!==infos+1||x.reads.length!==6||x.tasks.length)throw Error('revision re-verify');
  x.owner.hint();await x.run();await read(x);
  if(x.count('/ledger/analytics')!==infos+1)throw Error('confirmed revision not cached');
  // A failed read drops the cached revision; the next read verifies it.
  x.owner.hint();await x.run();x.reads.at(-1).reject(new Error('synthetic failure'));await flush();
  await x.run();await read(x);
  if(x.count('/ledger/analytics')!==infos+2)throw Error('failure did not force revision check');
  x.owner.dispose();
  reports.push('revision reused per connection; mismatch re-verified once; failure re-verifies');
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
    # Only closed requests: an owner-live request can change without a write.
    page.evaluate("events=events.filter(e=>e.status!=='pending')")
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
    for _ in range(3):
        page.clock.run_for(60000)
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
        print(f'PASS mounted pane: notify read started {delay:.0f} ms (fake clock) after the event and repainted; '
              'three quiet rolling minutes made change checks only; reconciliation and later notify still read')
        browser.close()


if __name__ == '__main__':
    run()
