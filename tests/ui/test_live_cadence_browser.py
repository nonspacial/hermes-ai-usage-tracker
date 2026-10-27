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
 function setup({windowSeconds=86400,view='requests',extra={},growing=false}={}){
  let token='t1',revision='rev-1',reads=[],calls=[],publications=[],tasks=[],time=0,holdToken=null;
  // pin: hold only the next change-token probe (later probes answer at once).
  let pinNext=false,pinned=[];
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
    if(pinNext){pinNext=false;return new Promise(resolve=>pinned.push(()=>resolve(answer())))}
    return Promise.resolve(answer());
   }
   if(path.startsWith('/ledger/analytics'))return Promise.resolve({revision,restart_required:false});
   if(path.startsWith('/ledger?'))return new Promise((resolve,reject)=>reads.push({path,reject,at:time,
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
   windowSeconds,growing,clock,connection:ledgerMemoryConnection,publish:p=>publications.push(p)});
  const run=async()=>{const pending=tasks;tasks=[];for(const task of pending)task.fn();await flush()};
  const count=route=>calls.filter(c=>c===route).length;
  return {owner,reads,publications,run,count,shape,install,get tasks(){return tasks},
   hold:()=>{holdToken=[]},release:async()=>{const held=holdToken||[];holdToken=null;for(const fn of held)fn();await flush()},
   pin:()=>{pinNext=true},get pinned(){return pinned.length},
   unpin:async()=>{const held=pinned;pinned=[];for(const fn of held)fn();await flush()},
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
  // A running subagent is bounded by the read time and its owner's liveness,
  // which change without a ledger write: never skip while one is counted.
  const lifecycle=extra=>({subagent_summary:{peak_observed:{value:1,unmatched:0,basis:'matched_child_lifecycle',...extra}}});
  await unprovable('running subagent',y=>{y.shape.body=lifecycle({running:1,open_ended:0})});
  await unprovable('malformed running count',y=>{y.shape.body=lifecycle({running:'1'})});
  const quietPeak=async(label,body)=>{
   const y=setup();y.shape.body=body;y.owner.start();await flush();await read(y);
   y.advance(60000);await y.owner.expire();await flush();
   if(y.tasks.length||y.reads.length!==1)throw Error(label+': did not skip');y.owner.dispose();
  };
  // Factual ends (matched or last-request bounded) and older responses
  // without the split stay skippable.
  await quietPeak('stopped-without-record children',lifecycle({running:0,open_ended:2}));
  await quietPeak('older peak shape',lifecycle({}));
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
  reports.push('expiry reads on changed token, leading attempts, owner-live/unresolved, any compression, quota, crossing, fresh catalogue, unknown field, bucket crossing, running subagent, filtered cache-read progression, Compressions view; stop-less/older peaks still skip');

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

  // Open-ended Custom (start only, end omitted = server now): windowSeconds is
  // 0, yet every visible-minute tick reads after the 150 ms debounce without a
  // rolling-edge probe (the old 60 s behaviour). A closed fixed window's tick
  // stays a no-op (its 5-minute reconcile is a hint).
  const open=setup({windowSeconds:0,growing:true});open.owner.start();await flush();await read(open);
  if(new URLSearchParams(open.reads[0].path.split('?')[1]).has('end'))throw Error('open-ended read sent an end');
  for(let i=0;i<3;i++){
   const n=open.count('/ledger/change-token'),before=open.reads.length;
   open.advance(60000);await open.owner.expire();await flush();
   if(open.tasks.length!==1||open.tasks[0].ms!==150)throw Error('open-ended tick did not schedule a read');
   if(open.count('/ledger/change-token')!==n)throw Error('open-ended tick probed for a skip');
   await open.run();if(open.reads.length!==before+1)throw Error('open-ended tick read missing');
   await read(open);
  }
  const ends=open.publications.filter(p=>p.data).map(p=>p.data.window.end);
  if(!(ends.at(-1)>ends[0]))throw Error('open-ended window did not grow '+JSON.stringify(ends));
  open.owner.dispose();
  const fixed=setup({windowSeconds:0});fixed.owner.start();await flush();await read(fixed);
  fixed.advance(60000);await fixed.owner.expire();await flush();
  if(fixed.tasks.length||fixed.reads.length!==1)throw Error('closed fixed window read on a minute tick');
  fixed.owner.dispose();
  reports.push('open-ended Custom: end omitted, each minute tick reads (no skip probe) and the window grows; closed fixed tick unchanged');

  // M1: a notify arriving while an expiry probe is held does not reuse that
  // probe's answer; its read (no certifying token while the probe is held)
  // publishes, and no stale shift follows.
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
  reports.push('notify during a held expiry probe does not reuse it; no stale shift');

  // I1 (probe joining): a notify never waits behind a probe that began before
  // it. With an expiry or poll probe held (not released until the end), the
  // notify's GET /ledger starts at the 150 ms debounce (a read's own check
  // returns null while a probe is in flight, as on main) and its response is
  // published without waiting for the held probe. Releasing the old probe
  // afterwards adds at most one trailing read and publishes no stale shift.
  // The reviewer's variant with every probe held is covered by `allHeld`.
  const behindHeld=async(label,begin)=>{
   const y=setup();y.owner.start();await flush();await read(y,{requests:[{id:'first'}]});
   y.advance(60000);y.pin();const old=begin(y);await flush();
   if(y.pinned!==1)throw Error(label+': probe not held');
   const probes=y.count('/ledger/change-token'),analytics=y.count('/ledger/analytics');
   // The notify announces a write; the held probe will answer with it too.
   y.setToken('t-'+label);const hintAt=y.time;y.owner.hint();
   if(y.tasks.length!==1||y.tasks[0].ms!==150)throw Error(label+': notify debounce '+JSON.stringify(y.tasks.map(t=>t.ms)));
   y.advance(150,0);await y.run();
   if(y.reads.length!==2)throw Error(label+': GET /ledger waited behind the held probe');
   if(y.reads[1].at-hintAt!==150)throw Error(label+': GET /ledger started at '+(y.reads[1].at-hintAt)+' ms');
   if(y.count('/ledger/change-token')>probes+1||y.count('/ledger/analytics')!==analytics+1)
    throw Error(label+': expected at most one pre-read probe and one revision check');
   if(y.pinned!==1)throw Error(label+': held probe released early');
   await read(y,{requests:[{id:'notified'}]});
   const painted=y.publications.filter(p=>p.data).at(-1);
   if(painted.data.requests[0].id!=='notified'||painted.fetching!==false)
    throw Error(label+': notified data not published while the old probe was held');
   if(y.pinned!==1)throw Error(label+': publish waited on the held probe');
   const settled=y.publications.length,readsBefore=y.reads.length;
   // The held probe answers with the unchanged token: a stale shift would
   // otherwise look certified.
   await y.unpin();await old;await flush();
   if(y.tasks.length>1)throw Error(label+': several trailing reads queued');
   await y.run();
   if(y.reads.length>readsBefore+1)throw Error(label+': more than one trailing read');
   if(y.reads.length>readsBefore){await read(y);await y.run()}
   if(y.reads.length>readsBefore+1||y.tasks.length)throw Error(label+': read chain after trailing read');
   const late=y.publications.slice(settled).filter(p=>p.data);
   // A read publishes fetching:false; only a local edge shift omits it.
   if(late.some(p=>p.fetching===undefined))
    throw Error(label+': stale shift published after release');
   y.owner.dispose();
   return readsBefore===y.reads.length?0:1;
  };
  const trailExpiry=await behindHeld('held expiry probe',y=>y.owner.expire());
  const trailPoll=await behindHeld('held poll probe',y=>y.owner.checkForChanges());
  // Every probe held (a saturated backend threadpool): GET /ledger still
  // starts 150 ms after the notify and publishes while all probes are held.
  const allHeld=setup();allHeld.owner.start();await flush();await read(allHeld);
  allHeld.advance(60000);allHeld.hold();const heldExpiry=allHeld.owner.expire();await flush();
  const heldPoll=allHeld.owner.checkForChanges();await flush();
  allHeld.setToken('t-all');const allAt=allHeld.time;allHeld.owner.hint();allHeld.advance(150,0);await allHeld.run();
  if(allHeld.reads.length!==2||allHeld.reads[1].at-allAt!==150)throw Error('all probes held: GET /ledger delayed');
  await read(allHeld,{requests:[{id:'all-held'}]});
  const allPainted=allHeld.publications.filter(p=>p.data).at(-1);
  if(allPainted.data.requests[0].id!=='all-held'||allPainted.fetching!==false)throw Error('all probes held: publish waited');
  const allSettled=allHeld.publications.length,allReads=allHeld.reads.length;
  await allHeld.release();await heldExpiry;await heldPoll;await flush();await allHeld.run();
  if(allHeld.reads.length>allReads+1)throw Error('all probes held: more than one trailing read');
  if(allHeld.reads.length>allReads){await read(allHeld);await allHeld.run()}
  if(allHeld.reads.length>allReads+1||allHeld.tasks.length)throw Error('all probes held: read chain');
  if(allHeld.publications.slice(allSettled).some(p=>p.data&&p.fetching===undefined))throw Error('all probes held: stale shift');
  allHeld.owner.dispose();
  reports.push(`notify with a held expiry/poll probe: GET /ledger at 150 ms, published before release (also with every probe held); trailing reads after release ${trailExpiry}/${trailPoll}, no stale shift`);

  // Polls compare with the pre-read token of an active read, or of a settled
  // read whose post-read check returned null (a probe was in flight). Under
  // the 2 s polling fallback a slow read sees several polls answering the
  // token it started from: no duplicate read. A different token still gives
  // exactly one trailing read.
  const slow=setup();slow.owner.start();await flush();await read(slow);
  slow.setToken('s2');await slow.owner.checkForChanges();
  if(slow.tasks.length!==1||slow.tasks[0].ms!==150)throw Error('slow read: changed poll did not queue');
  await slow.run();if(slow.reads.length!==2)throw Error('slow read: read missing');
  for(let i=0;i<3;i++){slow.advance(2000);await slow.owner.checkForChanges()}
  await read(slow);
  if(slow.tasks.length)throw Error('slow read: polls answering its pre-read token queued a duplicate read');
  // Post-read check null: a poll probe is held across the end of the read.
  slow.setToken('s3');await slow.owner.checkForChanges();await slow.run();
  if(slow.reads.length!==3)throw Error('unconfirmed read: read missing');
  slow.pin();let heldSlow=slow.owner.checkForChanges();await flush();
  await read(slow);
  if(slow.tasks.length)throw Error('unconfirmed read: follow-up without a change');
  await slow.unpin();await heldSlow;await flush();
  if(slow.tasks.length)throw Error('unconfirmed read: poll answering its pre-read token queued a duplicate read');
  slow.advance(2000);await slow.owner.checkForChanges();
  if(slow.tasks.length||slow.reads.length!==3)throw Error('unconfirmed read: later poll read again');
  // A write while a read is in flight: polls mark it dirty once → one trailing read.
  slow.setToken('s4');await slow.owner.checkForChanges();await slow.run();
  if(slow.reads.length!==4)throw Error('mid-read write: read missing');
  slow.advance(1000);slow.setToken('s5');
  for(let i=0;i<2;i++){slow.advance(2000);await slow.owner.checkForChanges()}
  slow.pin();heldSlow=slow.owner.checkForChanges();await flush();
  await read(slow);await slow.unpin();await heldSlow;await flush();
  if(slow.tasks.length!==1||slow.tasks[0].ms!==150)throw Error('mid-read write: trailing read not queued once '+slow.tasks.length);
  await slow.run();await read(slow);await slow.run();
  if(slow.reads.length!==5||slow.tasks.length)throw Error('mid-read write: not exactly one trailing read');
  // A write after an unconfirmed read: the poll that sees it queues one read.
  slow.setToken('s6');await slow.owner.checkForChanges();await slow.run();
  slow.pin();heldSlow=slow.owner.checkForChanges();await flush();
  slow.setToken('s7');await read(slow);
  if(slow.tasks.length)throw Error('unconfirmed read: follow-up before any answer');
  await slow.unpin();await heldSlow;await flush();
  if(slow.tasks.length!==1||slow.tasks[0].ms!==150)throw Error('unconfirmed read: changed poll did not queue one read');
  await slow.run();await read(slow);await slow.run();
  if(slow.reads.length!==7||slow.tasks.length)throw Error('unconfirmed read: not exactly one read for the change');
  slow.owner.dispose();
  reports.push('polls during a slow or unconfirmed read compare with its pre-read token: no duplicate read; a write mid-read or after it → exactly one read');

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
        ages.append(int(re.fullmatch(r'(\d+)s ago', age)[1]))
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


def mounted_open_custom(browser):
    """Mounted open-ended Custom: start only, GET without end, grows each visible minute."""
    page = browser.new_page(viewport={'width': 1500, 'height': 1000})
    errors, outbound = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('request', lambda r: outbound.append(r.url))
    page.route('**/*', lambda r: r.abort())
    page.clock.install(time=datetime(2026, 9, 27, 10, 20, tzinfo=timezone.utc))
    page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
    page.evaluate('''()=>{window.demoTrendLadder=true;window.ledgerReads=[];const o=rest;
      rest=async(path,options)=>{const r=await o(path,options);
        if(path.startsWith('/ledger')&&!path.startsWith('/ledger/change-token')&&!path.startsWith('/ledger/analytics')&&!path.startsWith('/ledger/status')&&!path.startsWith('/ledger/profiles'))
          ledgerReads.push({path,method:options?.method||'GET',end:r?.window?.end,unit:r?.trend?.unit});
        return r}}''')
    page.get_by_role('tab', name='All providers', exact=True).click()
    expect(page.locator('.au-quality-line').first).to_contain_text(re.compile(r'\d+ requests'))
    page.get_by_role('group', name='Time window').get_by_role('button', name='Custom', exact=True).click()
    page.locator('.au-custom-range summary').click()
    start = page.evaluate('''()=>{const t=Math.floor(Date.now()/1800000)*1800000-3*3600000,d=new Date(t);
      return new Date(t-d.getTimezoneOffset()*60000).toISOString().slice(0,16)}''')
    page.get_by_label('Window start', exact=True).fill(start)
    page.get_by_label('Window end', exact=True).fill('')
    page.clock.run_for(500)
    page.wait_for_function("()=>ledgerReads.some(r=>!new URLSearchParams(r.path.split('?')[1]).has('end')&&r.end)")
    assert page.get_by_role('alert').filter(has_text='until now').count() == 0
    first = page.evaluate("ledgerReads.filter(r=>!new URLSearchParams(r.path.split('?')[1]).has('end')).at(-1)")
    assert first['unit'] == '5 minutes', first
    expect(page.get_by_test_id('usage-chart').locator('.au-chart-title')).to_contain_text('5-minute buckets')
    reads = page.evaluate('ledgerReads.length')
    ends = []
    for _ in range(2):
        page.clock.run_for(60000)
        page.wait_for_function('n=>ledgerReads.length>n', arg=reads)
        reads = page.evaluate('ledgerReads.length')
        ends.append(page.evaluate('ledgerReads.at(-1).end'))
    assert ends[1] - ends[0] >= 59 and ends[0] > first['end'], (first, ends)
    assert all(r['method'] == 'GET' for r in page.evaluate('ledgerReads')), 'open-ended Custom must stay GET-only'
    assert page.evaluate("ledgerReads.slice(-2).every(r=>!new URLSearchParams(r.path.split('?')[1]).has('end'))")
    assert not errors, errors
    assert not outbound, outbound
    page.close()
    return ends


POLL_SENTENCE = 'Live updates by change checks every 2 seconds; the live subscription is unavailable'
# Records change-token probes (time and path) and /ledger reads (Playwright
# clock time) and lets a case fail change checks. `ledgerDelay` delivers each
# GET /ledger that many ms after its snapshot is taken (a slow plugin-host
# round trip). A quiet event adds a request and moves the change token without
# a socket frame, as on a secondary-profile plugin host.
FALLBACK_SETUP = '''()=>{window.tokenChecks=[];window.tokenPaths=[];window.ledgerReads=[];window.failToken=false;
  window.ledgerDelay=0;const o=rest;
  rest=(path,options)=>{
   if(path.startsWith('/ledger/change-token')){tokenChecks.push(performance.now());tokenPaths.push(path);
    if(failToken)return Promise.reject(new Error('Synthetic change-check failure'))}
   if(path.startsWith('/ledger?')){ledgerReads.push(performance.now());const delay=ledgerDelay;
    if(delay)return o(path,options).then(r=>new Promise(resolve=>setTimeout(()=>resolve(r),delay)))}
   return o(path,options)};
  window.quietEvent=()=>{const e=JSON.parse(JSON.stringify(events.find(r=>r.status==='completed')));
   e.id='quiet-'+performance.now();e.started=Date.now()/1000;e.ended=e.started+0.1;events.push(e);
   window.demoChange++;return performance.now()};
  window.setVisibility=state=>{Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>state});
   document.dispatchEvent(new Event('visibilitychange'))}}'''


def fallback_page(browser, prepare=''):
    page = browser.new_page(viewport={'width': 1500, 'height': 1000})
    errors, outbound = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('request', lambda r: outbound.append(r.url))
    page.route('**/*', lambda r: r.abort())
    page.clock.install(time=datetime(2026, 9, 27, 10, 20, tzinfo=timezone.utc))
    page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
    page.evaluate('''()=>{events=events.filter(e=>e.status!=='pending')}''')
    page.evaluate(FALLBACK_SETUP)
    if prepare:
        page.evaluate(prepare)
    return page, errors, outbound


def checks_since(page, start):
    return page.evaluate('n=>tokenChecks.slice(n)', start)


def assert_two_second_cadence(page, ms, label):
    """Run `ms` of quiet visible time; every change check is exactly 2 s apart.

    'No read while quiet' holds for this synthetic fixture only: on a live
    profile each recorder heartbeat health write (every 15 s per producer,
    no notify) moves the change token, so a poll reads after each one.
    """
    start = page.evaluate('tokenChecks.length')
    reads = page.evaluate('ledgerReads.length')
    page.clock.run_for(ms)
    times = checks_since(page, start)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(times) == ms // 2000, (label, times)
    assert all(abs(gap - 2000) < 1 for gap in gaps), (label, gaps)
    assert page.evaluate('ledgerReads.length') == reads, label + ': quiet poll read the ledger'
    return len(times)


def mounted_socket_fallback(browser):
    """Refused/unacknowledged live subscription: 2 s change checks, Online badge."""
    # The demo socket never acknowledges (the secondary-profile plugin host
    # refuses plugin websockets since Hermes bf0203a).
    page, errors, outbound = fallback_page(browser, 'window.failSocket=true')
    badge = page.get_by_test_id('connection-status')
    quality = page.locator('.au-quality-line').first
    page.get_by_role('tab', name='All providers', exact=True).click()
    # A closed (non-rolling) period keeps minute expiry probes out of the counts.
    page.get_by_role('group', name='Time window').get_by_role('button', name='All recorded', exact=True).click()
    expect(quality).to_contain_text(re.compile(r'\d+ requests'))
    page.clock.run_for(1000)
    expect(badge).to_have_attribute('data-state', 'checking')
    before_deadline = page.evaluate('tokenChecks.length')
    # 8 s acknowledgement deadline → transport `polling`: Online, sentence.
    page.clock.run_for(8000)
    expect(badge).to_have_text('Online')
    expect(badge).to_have_attribute('data-state', 'online')
    title = badge.get_attribute('title')
    assert POLL_SENTENCE in title and 'No recent subscription acknowledgement' not in title, title
    assert page.evaluate('tokenChecks.length') - before_deadline <= 2, 'fast checks before the deadline'
    # The shared tooltip shows the sentence as a plain (not warning) line.
    badge.hover()
    popup = page.locator('.au-tooltip[role="tooltip"]:visible').last
    expect(popup).to_contain_text(POLL_SENTENCE)
    line = popup.locator('.au-tip-line', has_text='Live updates by change checks')
    assert line.count() == 1 and line.locator('.au-tip-warn').count() == 0, line.inner_html()
    page.mouse.move(5, 995)
    fast = assert_two_second_cadence(page, 10000, 'polling')
    # A quiet write: the next 2 s poll sees the token, GET /ledger follows
    # 150 ms after that poll and the pane repaints.
    match = re.search(r'([\d,]+) requests', quality.inner_text())
    assert match, quality.inner_text()
    shown = int(match[1].replace(',', ''))
    start = page.evaluate('tokenChecks.length')
    event_at = page.evaluate('quietEvent()')
    page.clock.run_for(2500)
    poll_at = checks_since(page, start)[0]
    read_at = page.evaluate('at=>ledgerReads.find(t=>t>at)', event_at)
    assert read_at is not None, 'changed token did not read'
    lag = read_at - poll_at
    assert 150 <= lag < 200 and read_at - event_at <= 2200, (event_at, poll_at, read_at)
    expect(quality).to_contain_text(f'{shown + 1:,} requests')
    page.clock.run_for(1000)
    # Hidden pane: no fast polling; the ordinary 20 s check remains.
    start = page.evaluate('tokenChecks.length')
    page.evaluate("setVisibility('hidden')")
    page.clock.run_for(19500)
    hidden_checks = len(checks_since(page, start))
    page.clock.run_for(1000)
    assert hidden_checks == 0 and len(checks_since(page, start)) == 1, checks_since(page, start)
    # Visible again: the existing return check, then 2 s checks resume.
    page.evaluate("setVisibility('visible')")
    page.clock.run_for(1000)
    assert_two_second_cadence(page, 6000, 'after visibility return')
    # A read error keeps the existing Limited badge and texts.
    page.evaluate('failLedger=true;quietEvent()')
    page.clock.run_for(2500)
    expect(badge).to_have_text('Limited')
    title = badge.get_attribute('title')
    assert 'No recent subscription acknowledgement' in title and 'Usage refresh failed' in title, title
    assert POLL_SENTENCE not in title, title
    page.evaluate('failLedger=false')
    page.clock.run_for(6000)
    expect(badge).to_have_text('Online')
    assert POLL_SENTENCE in badge.get_attribute('title')
    expect(quality).to_contain_text(f'{shown + 2:,} requests')
    # A later acknowledgement: normal Online and the 20 s cadence.
    page.clock.run_for(1000)
    page.evaluate("failSocket=false;for(const s of demoSubscribers)s({type:'heartbeat',mode:'native-events'})")
    page.clock.run_for(100)
    expect(badge).to_have_text('Online')
    assert POLL_SENTENCE not in badge.get_attribute('title')
    start = page.evaluate('tokenChecks.length')
    page.clock.run_for(19500)
    acked_quiet = len(checks_since(page, start))
    page.clock.run_for(1000)
    assert acked_quiet == 0 and len(checks_since(page, start)) == 1, checks_since(page, start)
    # The acknowledgement lapses (45 s): polling again.
    page.evaluate('failSocket=true')
    page.clock.run_for(46000)
    expect(badge).to_have_text('Online')
    assert POLL_SENTENCE in badge.get_attribute('title')
    page.clock.run_for(500)
    assert_two_second_cadence(page, 4000, 'after acknowledgement lapse')
    # Failing change checks keep the existing Limited badge and text.
    page.evaluate('failToken=true')
    page.clock.run_for(7000)
    expect(badge).to_have_text('Limited')
    title = badge.get_attribute('title')
    assert 'Change checks unavailable' in title and POLL_SENTENCE not in title, title
    assert not errors, errors
    assert not outbound, outbound
    page.close()
    return fast, lag


def mounted_socket_unavailable(browser):
    """A socket that throws or is absent polls at once; All profiles is unchanged."""
    results = []
    for label, prepare in [('throwing socket', "()=>{socket=()=>{throw new Error('HTTP 501 plugin websocket refused')}}"),
                           ('no socket', '()=>{socket=null}')]:
        page, errors, outbound = fallback_page(browser, prepare)
        badge = page.get_by_test_id('connection-status')
        page.get_by_role('tab', name='All providers', exact=True).click()
        page.get_by_role('group', name='Time window').get_by_role('button', name='All recorded', exact=True).click()
        expect(page.locator('.au-quality-line').first).to_contain_text(re.compile(r'\d+ requests'))
        page.clock.run_for(500)
        expect(badge).to_have_text('Online')
        assert POLL_SENTENCE in badge.get_attribute('title'), label
        assert_two_second_cadence(page, 6000, label)
        if label == 'no socket':
            # All profiles keeps Watching and its 30 s cadence.
            page.get_by_role('combobox', name='Hermes profile').select_option('scope:all')
            page.get_by_role('tab', name='All providers', exact=True).click()
            expect(page.locator('.au-quality-line').first).to_contain_text(re.compile(r'\d+ requests'))
            expect(badge).to_have_text('Watching')
            page.clock.run_for(2000)
            start = page.evaluate('tokenChecks.length')
            page.clock.run_for(62000)
            times = checks_since(page, start)
            # Two 30 s polls (a token-adopting read may add its own post-read check).
            assert len(times) >= 2 and abs(times[1] - times[0] - 30000) < 1, times
            assert POLL_SENTENCE not in badge.get_attribute('title')
        assert not errors, (label, errors)
        assert not outbound, (label, outbound)
        results.append(label)
        page.close()
    return results


def polling_online(browser):
    """Refused subscription, All providers, closed All recorded period, Online by polling."""
    page, errors, outbound = fallback_page(browser, 'window.failSocket=true')
    badge = page.get_by_test_id('connection-status')
    quality = page.locator('.au-quality-line').first
    page.get_by_role('tab', name='All providers', exact=True).click()
    page.get_by_role('group', name='Time window').get_by_role('button', name='All recorded', exact=True).click()
    expect(quality).to_contain_text(re.compile(r'\d+ requests'))
    page.clock.run_for(9000)
    expect(badge).to_have_attribute('data-state', 'online')
    page.clock.run_for(3000)
    return page, errors, outbound, badge, quality


MARKER_READS = '''()=>{window.markerReads=[];window.markerPosts=[];const o=rest;
  rest=async(path,options)=>{
   if(path.startsWith('/ledger/tests'))markerPosts.push(options?.body?.action);
   const r=await o(path,options);
   if(path.startsWith('/ledger?'))markerReads.push({q:Object.fromEntries(new URLSearchParams(path.split('?')[1])),
    method:options?.method||'GET',ws:r?.window?.start,we:r?.window?.end});
   return r}}'''


def mounted_marker_custom_edit(browser, polling):
    """Start test marker, then edit Custom From: the edit deselects the marker.

    Owner flow: with the open marker selected (window = marker start..now),
    editing From to an earlier time must read the typed open range, not stay
    clamped to the marker. The saved marker and the running test are untouched.
    """
    page, errors, outbound = fallback_page(browser, 'window.failSocket=true' if polling else '')
    page.evaluate(MARKER_READS)
    badge = page.get_by_test_id('connection-status')
    page.get_by_role('tab', name='All providers', exact=True).click()
    expect(page.locator('.au-quality-line').first).to_contain_text(re.compile(r'\d+ requests'))
    page.clock.run_for(9000)
    expect(badge).to_have_attribute('data-state', 'online')
    assert (POLL_SENTENCE in (badge.get_attribute('title') or '')) == polling
    page.get_by_role('button', name='Start test marker', exact=True).click()
    page.wait_for_function('savedTests.length>0')
    page.clock.run_for(500)
    marker = page.evaluate('({...savedTests[0]})')
    assert page.evaluate('markerReads.at(-1).q.test_id') == marker['id'], 'Start test did not select its marker'
    remove = page.get_by_role('button', name='Remove saved test marker filter')
    expect(remove).to_be_visible()
    page.locator('.au-custom-range summary').click()
    start_input = page.get_by_label('Window start', exact=True)
    expect(page.get_by_label('Window end', exact=True)).to_have_value('')

    def last_open_read(label):
        value = start_input.input_value()
        expected = page.evaluate('v=>Date.parse(v)/1000', value)
        read = page.evaluate('markerReads.at(-1)')
        assert 'test_id' not in read['q'], (label, 'Custom edit kept the marker', read, marker)
        assert 'end' not in read['q'] and read['method'] == 'GET', (label, read)
        assert float(read['q']['start']) == expected and read['ws'] == expected, (label, value, read)
        return expected

    # (1) Keyboard edit as in the picker: focus From, step its first segment back.
    box = start_input.bounding_box()
    page.mouse.click(box['x'] + 12, box['y'] + box['height'] / 2)
    page.keyboard.press('ArrowDown')
    page.clock.run_for(500)
    keyed = last_open_read('keyboard')
    assert keyed < marker['started'] - 3600, (keyed, marker)
    expect(remove).to_have_count(0)
    # (2) A typed earlier From, still open-ended.
    typed = page.evaluate('''()=>{const t=Math.floor(Date.now()/1800000)*1800000-30*3600000,d=new Date(t);
      return new Date(t-d.getTimezoneOffset()*60000).toISOString().slice(0,16)}''')
    start_input.fill(typed)
    page.clock.run_for(500)
    chosen = last_open_read('fill')
    # The window keeps the chosen start and grows each visible minute.
    ends = []
    for _ in range(2):
        reads = page.evaluate('markerReads.length')
        page.clock.run_for(60000)
        assert page.evaluate('markerReads.length') > reads, 'open Custom did not read on the minute'
        assert last_open_read('minute') == chosen
        ends.append(page.evaluate('markerReads.at(-1).we'))
    assert ends[1] - ends[0] >= 59, ends
    # The saved marker keeps its timestamps and the test keeps running.
    assert page.evaluate('({...savedTests[0]})') == marker, 'saved marker was rewritten'
    assert page.evaluate('markerPosts') == ['start'], page.evaluate('markerPosts')
    expect(page.get_by_role('button', name='End test marker', exact=True).first).to_be_visible()
    assert not errors, errors
    assert not outbound, outbound
    page.close()
    return chosen, ends


def started_marker_page(browser):
    """Acknowledged-socket page with a freshly started (selected) test marker."""
    page, errors, outbound = fallback_page(browser)
    page.evaluate(MARKER_READS)
    page.get_by_role('tab', name='All providers', exact=True).click()
    expect(page.locator('.au-quality-line').first).to_contain_text(re.compile(r'\d+ requests'))
    page.clock.run_for(9000)
    page.get_by_role('button', name='Start test marker', exact=True).click()
    page.wait_for_function('savedTests.length>0')
    page.clock.run_for(500)
    marker = page.evaluate('({...savedTests[0]})')
    assert page.evaluate('markerReads.at(-1).q.test_id') == marker['id'], 'Start test did not select its marker'
    expect(page.get_by_role('button', name='Remove saved test marker filter')).to_be_visible()
    return page, errors, outbound, marker


def settle_read(page, reads):
    """Advance the fake clock until a `/ledger` read after `reads` has settled."""
    for _ in range(40):
        page.clock.run_for(100)
        if page.evaluate('n=>markerReads.length>n', reads):
            break
    else:
        raise AssertionError('no /ledger read after the action')
    page.clock.run_for(500)


def local_input(page, seconds):
    return page.evaluate('''s=>{const d=new Date(s*1000);
      return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16)}''', seconds)


def end_test(page, marker):
    """Press End test marker; return the ended marker and the read it caused."""
    reads = page.evaluate('markerReads.length')
    page.get_by_role('button', name='End test marker', exact=True).first.click()
    settle_read(page, reads)
    assert page.evaluate('markerPosts') == ['start', 'stop'], page.evaluate('markerPosts')
    ended = page.evaluate('({...savedTests[0]})')
    assert ended['id'] == marker['id'] and ended['started'] == marker['started'] and ended['ended'], (ended, marker)
    expect(page.get_by_role('button', name='Start test marker', exact=True).first).to_be_visible()
    expect(page.get_by_role('button', name='End test marker', exact=True)).to_have_count(0)
    return ended, page.evaluate('markerReads.at(-1)')


def assert_marker_selected(page, ended, label):
    """The read carries the marker and its window equals the factual bounds."""
    read = page.evaluate('markerReads.at(-1)')
    assert read['q'].get('test_id') == ended['id'], (label, read)
    assert read['ws'] == ended['started'] and read['we'] == ended['ended'], (label, read, ended)
    started = page.evaluate('s=>Math.floor(s/1800)*1800', ended['started'])
    stopped = page.evaluate('([s,e])=>Math.max(Math.ceil(e/1800)*1800,Math.floor(s/1800)*1800+1800)',
                            [ended['started'], ended['ended']])
    expect(page.get_by_label('Window start', exact=True)).to_have_value(local_input(page, started))
    expect(page.get_by_label('Window end', exact=True)).to_have_value(local_input(page, stopped))
    expect(page.get_by_role('button', name='Remove saved test marker filter')).to_be_visible()


def mounted_marker_end_after_edit(browser):
    """Start → earlier From (deselects) → End: End only ends the test.

    The user's typed open range stays the view (no test_id, no end); the button
    returns to Start and the ended marker remains reselectable with its
    factual bounds from Saved test windows.
    """
    page, errors, outbound, marker = started_marker_page(browser)
    page.locator('.au-custom-range summary').click()
    start_input = page.get_by_label('Window start', exact=True)
    typed = local_input(page, page.evaluate('Math.floor(Date.now()/1800000)*1800-30*3600'))
    start_input.fill(typed)
    page.clock.run_for(500)
    expected = page.evaluate('v=>Date.parse(v)/1000', typed)
    read = page.evaluate('markerReads.at(-1)')
    assert 'test_id' not in read['q'] and float(read['q']['start']) == expected, read
    page.clock.run_for(90000)
    ended, read = end_test(page, marker)
    assert 'test_id' not in read['q'], ('End rewrote the view to the marker without test_id', read)
    assert 'end' not in read['q'] and read['method'] == 'GET', ('End discarded the typed open range', read)
    assert float(read['q']['start']) == expected and read['ws'] == expected, ('End moved From', read, expected)
    expect(start_input).to_have_value(typed)
    expect(page.get_by_label('Window end', exact=True)).to_have_value('')
    expect(page.get_by_role('button', name='Remove saved test marker filter')).to_have_count(0)
    # The view keeps growing as an open Custom range.
    reads = page.evaluate('markerReads.length')
    page.clock.run_for(60000)
    assert page.evaluate('markerReads.length') > reads
    later = page.evaluate('markerReads.at(-1)')
    assert 'test_id' not in later['q'] and float(later['q']['start']) == expected and 'end' not in later['q'], later
    # The ended marker is listed and reselectable with its factual bounds.
    saved = page.get_by_role('combobox', name='Saved tests', exact=True)
    expect(saved.locator(f'option[value="{ended["id"]}"]')).to_contain_text('· ended')
    reads = page.evaluate('markerReads.length')
    saved.select_option(ended['id'])
    settle_read(page, reads)
    assert_marker_selected(page, ended, 'reselected')
    expect(page.get_by_role('button', name='Start test marker', exact=True).first).to_be_visible()
    assert page.evaluate('({...savedTests[0]})') == ended, 'saved marker was rewritten'
    assert page.evaluate('markerPosts') == ['start', 'stop']
    assert not errors, errors
    assert not outbound, outbound
    page.close()
    return expected, ended


def mounted_marker_end_selected(browser):
    """Start → End without editing: unchanged — the marker stays selected."""
    page, errors, outbound, marker = started_marker_page(browser)
    page.clock.run_for(90000)
    ended, read = end_test(page, marker)
    page.locator('.au-custom-range summary').click()
    assert_marker_selected(page, ended, 'End while selected')
    assert not errors, errors
    assert not outbound, outbound
    page.close()
    return ended


def mounted_marker_other_inputs(browser):
    """Overview End input and Cache & costs From/End inputs also deselect."""
    page, errors, outbound, marker = started_marker_page(browser)
    remove = page.get_by_role('button', name='Remove saved test marker filter')
    saved = page.get_by_role('combobox', name='Saved tests', exact=True)
    start_value = page.get_by_label('Window start', exact=True).input_value()
    begin = page.evaluate('v=>Date.parse(v)/1000', start_value)

    def reselect():
        reads = page.evaluate('markerReads.length')
        saved.select_option(marker['id'])
        settle_read(page, reads)
        assert page.evaluate('markerReads.at(-1).q.test_id') == marker['id']
        expect(remove).to_be_visible()

    def edited(label, locator, value, start, end, view=None):
        reads = page.evaluate('markerReads.length')
        locator.fill(value)
        settle_read(page, reads)
        read = page.evaluate('markerReads.at(-1)')
        assert 'test_id' not in read['q'], (label, 'edit kept the marker', read)
        assert float(read['q']['start']) == start and read['ws'] == start, (label, read, start)
        if end is None:
            assert 'end' not in read['q'], (label, read)
        else:
            assert float(read['q']['end']) == end, (label, read, end)
        if view:
            assert read['q'].get('view') == view, (label, read)
        expect(remove).to_have_count(0)
        expect(page.get_by_role('button', name='End test marker', exact=True).first).to_be_visible()

    page.locator('.au-custom-range summary').click()
    closed = local_input(page, begin + 1800)
    edited('Overview End', page.get_by_label('Window end', exact=True), closed, begin, begin + 1800)
    reselect()
    page.get_by_role('navigation', name='Provider subpages', exact=True).get_by_role(
        'tab', name='Cache & costs', exact=True).click()
    filters = page.get_by_test_id('cache-window-filters')
    page.clock.run_for(500)
    earlier = begin - 6 * 3600
    edited('Cache start', filters.get_by_label('Cache window start', exact=True), local_input(page, earlier),
           earlier, None, 'cache')
    reselect()
    edited('Cache end', filters.get_by_label('Cache window end', exact=True), local_input(page, begin + 1800),
           begin, begin + 1800, 'cache')
    assert page.evaluate('({...savedTests[0]})') == marker, 'saved marker was rewritten'
    assert page.evaluate('markerPosts') == ['start'], page.evaluate('markerPosts')
    assert not errors, errors
    assert not outbound, outbound
    page.close()


def requests_shown(quality):
    match = re.search(r'([\d,]+) requests', quality.inner_text())
    assert match, quality.inner_text()
    return int(match[1].replace(',', ''))


def mounted_slow_read_polling(browser):
    """2 s polling with GET /ledger taking 3 s: one read per quiet write; a write mid-read → one trailing read."""
    page, errors, outbound, badge, quality = polling_online(browser)
    page.evaluate('ledgerDelay=3000')
    shown = requests_shown(quality)
    # One quiet write: the poll that sees it starts one read; the polls that
    # land during its 3 s flight answer that read's pre-read token.
    reads = page.evaluate('ledgerReads.length')
    page.evaluate('quietEvent()')
    page.clock.run_for(12000)
    single = page.evaluate('n=>ledgerReads.slice(n)', reads)
    assert len(single) == 1, ('duplicate read for one quiet write', single)
    expect(quality).to_contain_text(f'{shown + 1:,} requests')
    # A second write lands while the next read is in flight (after its
    # snapshot): exactly one trailing read, started only after the first settles.
    reads = page.evaluate('ledgerReads.length')
    page.evaluate('quietEvent()')
    page.clock.run_for(2200)
    assert page.evaluate('ledgerReads.length') == reads + 1, 'quiet write did not start a read'
    page.evaluate('quietEvent()')
    page.clock.run_for(12000)
    trailing = page.evaluate('n=>ledgerReads.slice(n)', reads)
    assert len(trailing) == 2 and trailing[1] - trailing[0] >= 3150, ('mid-read write', trailing)
    expect(quality).to_contain_text(f'{shown + 3:,} requests')
    expect(badge).to_have_text('Online')
    assert not errors, errors
    assert not outbound, outbound
    page.close()
    return single, trailing


def mounted_polling_switches(browser):
    """Profile switch and Reconnect while polling: no old-profile probes; 8 s wait, then 2 s checks."""
    page, errors, outbound, badge, quality = polling_online(browser)
    combobox = page.get_by_role('combobox', name='Hermes profile')
    start = page.evaluate('tokenPaths.length')
    combobox.select_option('profile:default')
    page.get_by_role('tab', name='All providers', exact=True).click()
    expect(quality).to_contain_text(re.compile(r'\d+ requests'))
    page.clock.run_for(1000)
    expect(badge).to_have_attribute('data-state', 'checking')
    page.clock.run_for(8000)
    expect(badge).to_have_text('Online')
    assert POLL_SENTENCE in badge.get_attribute('title')
    page.clock.run_for(500)
    assert_two_second_cadence(page, 6000, 'after profile switch')
    paths = page.evaluate('n=>tokenPaths.slice(n)', start)
    assert paths and all('profile=default' in p for p in paths), paths
    # Reconnect: the subscription is retried (Reconnecting, normal cadence),
    # then the 8 s deadline returns to polling and 2 s checks.
    page.get_by_test_id('connection-status').click()
    # Move away so the shared tooltip restores the badge's native title.
    page.mouse.move(5, 995)
    page.clock.run_for(500)
    assert badge.get_attribute('data-state') != 'online', badge.get_attribute('data-state')
    start = page.evaluate('tokenChecks.length')
    page.clock.run_for(5000)
    early = checks_since(page, start)
    gaps = [b - a for a, b in zip(early, early[1:])]
    assert not any(abs(gap - 2000) < 1 for gap in gaps), ('2 s checks while reconnecting', early)
    page.clock.run_for(3500)
    expect(badge).to_have_text('Online')
    assert POLL_SENTENCE in badge.get_attribute('title')
    page.clock.run_for(500)
    assert_two_second_cadence(page, 6000, 'after reconnect')
    assert not errors, errors
    assert not outbound, outbound
    page.close()


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
        ends = mounted_open_custom(browser)
        print(f'PASS mounted open-ended Custom: GET without end, 5-minute buckets labelled, one read per visible minute, window end grew {ends[0]:.0f} → {ends[1]:.0f}')
        fast, lag = mounted_socket_fallback(browser)
        print(f'PASS unacknowledged subscription: Reconnecting until the 8 s deadline, then Online with the polling '
              f'sentence; {fast} change checks exactly 2 s apart in 10 quiet visible seconds, no read; quiet write → '
              f'GET /ledger {lag:.0f} ms after the 2 s poll answer and repaint; hidden 19.5 s → no check, 20 s check '
              'kept; visible → 2 s resumes; read error → Limited (old texts) then Online; heartbeat ack → plain Online, '
              'no check for 19.5 s then 20 s; 45 s ack lapse → polling again; failing change checks → Limited')
        variants = mounted_socket_unavailable(browser)
        print('PASS ' + ' and '.join(variants) + ': Online with the polling sentence and exactly 2 s checks at once; '
              'All profiles keeps Watching and 30 s checks')
        single, trailing = mounted_slow_read_polling(browser)
        print(f'PASS polling with a 3 s GET /ledger: one quiet write → {len(single)} read; a write during the next '
              f'read → {len(trailing)} reads (one trailing, {trailing[1] - trailing[0]:.0f} ms after the first started)')
        mounted_polling_switches(browser)
        print('PASS polling: profile switch probes only the new profile, Online by polling after its 8 s deadline, '
              '2 s checks; Reconnect retries the subscription without 2 s checks, then polls at 2 s after 8 s')
        for polling in (True, False):
            chosen, ends = mounted_marker_custom_edit(browser, polling)
            print(f'PASS Start test marker then Custom From edit ({"polling" if polling else "acknowledged socket"}): '
                  f'keyboard and typed From each deselect the marker and read start={chosen:.0f} without end or '
                  f'test_id, response window starts there and grows {ends[0]:.0f} → {ends[1]:.0f}; saved marker '
                  'unchanged, test still running')
        ended = mounted_marker_end_selected(browser)
        print(f'PASS Start then End without editing: marker stays selected, read test_id with factual window '
              f'{ended["started"]:.0f} → {ended["ended"]:.0f}, aligned inputs, button back to Start')
        typed, ended = mounted_marker_end_after_edit(browser)
        print(f'PASS Start → earlier From → End: End only ends the test; read keeps start={typed:.0f}, no end, no '
              f'test_id, inputs unchanged, button back to Start; ended marker listed and reselected with factual '
              f'window {ended["started"]:.0f} → {ended["ended"]:.0f}')
        mounted_marker_other_inputs(browser)
        print('PASS Overview End input and Cache & costs From/End inputs each deselect the marker (no test_id, '
              'typed bounds, view=cache) while the test keeps running')
        browser.close()


if __name__ == '__main__':
    run()
