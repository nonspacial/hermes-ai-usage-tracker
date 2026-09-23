"""Offline deterministic coordinator ordering against the packaged browser source."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]

PROBE = r'''async () => {
 const original=rest, reports=[];
 const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve()};
 function setup(profile='infra'){
  let token='one', unsupported=false, reads=[], publications=[], tasks=[], time=0;
  const clock={now:()=>time,setTimeout(fn,ms){const task={fn,ms};tasks.push(task);return task},clearTimeout(task){tasks=tasks.filter(t=>t!==task)}};
  rest=(path)=>{
   if(path.startsWith('/ledger/change-token')){
    if(unsupported)return Promise.reject(Object.assign(new Error('No route'),{status:404}));
    return Promise.resolve({version:1,profile_scope:profile==='all'?'all':'selected',read_only:true,
      token_kind:'opaque-filesystem-hint',change_token:token,capabilities:{change_check:true}});
   }
   if(path.startsWith('/ledger?'))return new Promise((resolve,reject)=>reads.push({path,resolve,reject}));
   return original(path);
  };
  const owner=makeLedgerCoordinator({profile:profile==='all'?ALL_PROFILES:profile,
   path:'/ledger?'+new URLSearchParams(profile==='all'?{profile_scope:'all',start:'0'}:{profile,start:'0'}),
   windowSeconds:0,clock,publish:p=>publications.push(p)});
  const run=async()=>{const pending=tasks;tasks=[];for(const task of pending)task.fn();await flush()};
  return {owner,reads,publications,run,get tasks(){return tasks},advance:ms=>time+=ms,setToken:v=>token=v,setUnsupported:v=>unsupported=v};
 }
 try{
  // A socket hint during a held response must publish that coherent response,
  // then start a distinct flight; numerous hints cost one follow-up.
  let x=setup();x.owner.start();await flush();
  if(x.reads.length!==1)throw Error('initial read missing');
  for(let i=0;i<30;i++)x.owner.hint();
  x.setToken('two');x.reads[0].resolve({summary:{attempts:1},requests:[{id:'a'}],groups:[{key:'a'}]});await flush();
  if(x.publications.filter(p=>p.data).length!==1||x.tasks.length!==1)throw Error('dirty flight did not publish/schedule');
  await x.run();if(x.reads.length!==2||x.reads[1]===x.reads[0])throw Error('lost follow-up');
  x.reads[1].resolve({summary:{attempts:2},requests:[{id:'b'}],groups:[{key:'b'}]});await flush();
  if(x.publications.at(-1).data?.summary.attempts!==2||x.publications.at(-1).data.requests[0].id!=='b'||x.tasks.length)throw Error('non-coherent follow-up');
  await x.owner.checkForChanges();await flush();if(x.tasks.length)throw Error('unchanged token scheduled a read');
  // Catalogue-only change is still a hint; never invent counter deltas.
  x.setToken('catalogue-only');await x.owner.checkForChanges();await x.run();
  if(x.reads.length!==3)throw Error('catalogue token ignored');
  x.reads[2].resolve({summary:{attempts:2,cost_usd:3},requests:[{id:'b'}],groups:[{key:'priced'}]});await flush();
  if(x.publications.at(-1).data.groups[0].key!=='priced')throw Error('catalogue publication missing');
  reports.push('held hint, burst, quiet token, catalogue');x.owner.dispose();

  // A token changing inside the read is not acknowledged as an old response,
  // even when no socket event arrives.
  x=setup();x.owner.start();await flush();x.setToken('during-read');
  x.reads[0].resolve({summary:{attempts:1}});await flush();
  if(x.tasks.length!==1)throw Error('changed token falsely acknowledged');
  await x.run();x.reads[1].resolve({summary:{attempts:2}});await flush();
  if(x.publications.at(-1).data?.summary.attempts!==2)throw Error('changed token follow-up missing');
  reports.push('pre/post token boundary');x.owner.dispose();

  // Manual Refresh cannot join an earlier flight and silently finish busy.
  x=setup();x.owner.start();await flush();x.owner.refresh();x.owner.hint();
  x.reads[0].resolve({summary:{attempts:1}});await flush();
  if(!x.publications.at(-1).manual)throw Error('manual busy cleared before follow-up');
  await x.run();x.reads[1].resolve({summary:{attempts:2}});await flush();
  if(x.publications.at(-1).manual!==false||x.publications.filter(p=>p.data).at(-1).data.summary.attempts!==2)throw Error('manual crossing lost');
  reports.push('manual crossing');x.owner.dispose();

  // Failed follow-up retains prior response and retries after bounded backoff.
  x=setup();x.owner.start();await flush();x.owner.hint();
  x.reads[0].resolve({summary:{attempts:1}});await flush();await x.run();
  x.reads[1].reject(new Error('synthetic read failure'));await flush();
  if(!x.publications.at(-1).error||x.tasks.length!==1||x.tasks[0].ms!==5000)throw Error('retry not bounded');
  await x.run();x.reads[2].resolve({summary:{attempts:3}});await flush();
  if(x.publications.at(-1).data?.summary.attempts!==3||x.publications.at(-1).error!==null)throw Error('retry failed');
  reports.push('failed follow-up/retry');x.owner.dispose();

  // Old scope response and its scheduled callbacks cannot publish after disposal.
  x=setup();x.owner.start();await flush();const n=x.publications.length;x.owner.dispose();
  x.reads[0].resolve({summary:{attempts:99}});await flush();
  x.owner.hint();await x.run();if(x.publications.length!==n||x.reads.length!==1)throw Error('disposed generation wrote');
  reports.push('stale scope/unmount');

  // An old backend is not polled on every 20-second check.
  x=setup('all');x.setUnsupported(true);x.owner.start();await flush();x.reads[0].resolve({summary:{attempts:1},profile_scope:'all',read_only:true});await flush();
  x.advance(60000);await x.owner.checkForChanges();await x.owner.checkForChanges();
  if(x.tasks.length!==1)throw Error('unsupported endpoint storm '+x.tasks.length+' '+document.visibilityState);
  await x.run();x.reads[1].resolve({summary:{attempts:2},profile_scope:'all',read_only:true});await flush();
  if(x.publications.at(-1).data?.summary.attempts!==2)throw Error('aggregate fallback missing');
  reports.push('All-profile legacy fallback');x.owner.dispose();
  return reports;
 }finally{rest=original}
}'''

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
                                    headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
        print('PASS', ', '.join(page.evaluate(PROBE)))
        assert not errors, errors
        browser.close()

if __name__ == '__main__':
    run()
