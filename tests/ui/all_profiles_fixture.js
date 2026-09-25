// Offline-only DTO fixtures. Never loaded by the packaged Desktop plugin.
const baseDemoRest=demoRest;
// Match projection.py's version-1 wire shape. Keep the legacy fixture full:
// projected reads alone omit fields, rather than silently supplying full DTOs.
const fixtureCommon=['generated_at','seq','window','summary','request_count','next_offset',
 'provider_groups','trend','subagent_summary','project_options','providers','compression_count','tests'];
const fixtureDetail=['requests','cache_read_progression','price_catalogs','applied_rate_groups',
 'compressions','compression_truncated','groups','model_groups','project_groups',
 'session_groups','subagent_groups','agent_groups','health','rates','quota_observations',
 'crossing_start','crossing_end'];
const fixtureGroups={model:'model_groups',time:null,project:'project_groups',
 session:'session_groups',subagent:'subagent_groups'};
const fixtureViews={overview:[],requests:['requests','cache_read_progression'],
 cache:['price_catalogs','applied_rate_groups'],compressions:['compressions','compression_truncated'],
 models:['groups'],skills:[]};
function fixtureOverviewSummary(row){
 const reasons=['awaiting_usage','unresolved_execution','abandoned_execution','unverified_accounting','ended_without_usage','unreported_field'];
 return {...row,unresolved:row.unresolved||0,abandoned:row.abandoned||0,
  supplemental_requests:row.supplemental_requests||0,
  known_cost_usd:String(row.known_cost_usd),
  missing_reasons:Object.fromEntries(Object.entries(row.missing_fields).map(([key,n])=>
   [key,Object.fromEntries(reasons.map(reason=>[reason,reason==='unreported_field'?n:0]))])),
  cost_components:Object.fromEntries(Object.entries(row.cost_components).map(([key,n])=>[key,String(n)])),
  savings:Object.fromEntries(Object.entries(row.savings).map(([key,n])=>[key,String(n)]))};
}
function fixtureProject(full,p){
 const view=p.get('view'),group=p.get('group');
 if(view==='overview'&&Object.hasOwn(fixtureGroups,group)&&full.summary){
  // The synthetic base predates the backend wire summary; keep only the
  // projected fixture's consumed shape aligned with the real API.
  full={...full,window:{...full.window,basis:'synthetic request starts'},
   summary:fixtureOverviewSummary(full.summary),
   subagent_summary:fixtureOverviewSummary(full.subagent_summary),
   provider_groups:full.provider_groups.map(fixtureOverviewSummary),
   model_groups:full.model_groups.map(fixtureOverviewSummary),
   ...(fixtureGroups[group]?{[fixtureGroups[group]]:full[fixtureGroups[group]].map(fixtureOverviewSummary)}:{}),
   trend:{...full.trend,buckets:full.trend.buckets.map(row=>({
    ...fixtureOverviewSummary(row),start:row.start,end:row.end}))}};
 }
 if(view===null){if(group!==null)throw new Error('Group requires a view');return full}
 if(!Object.hasOwn(fixtureViews,view)||view==='overview'&&!Object.hasOwn(fixtureGroups,group)||
    view!=='overview'&&group!==null)throw new Error('Invalid projection');
 const included=[...new Set([...fixtureCommon,...fixtureViews[view],...(view==='overview'&&fixtureGroups[group]?[fixtureGroups[group]]:[])])].sort();
 const omitted=fixtureDetail.filter(field=>!included.includes(field)).sort();
 for(const field of included)if(!Object.hasOwn(full,field))throw new Error('Fixture lacks projected field '+field);
 return {...Object.fromEntries(included.map(field=>[field,full[field]])),
  ...Object.fromEntries(['profile_scope','read_only','refresh_mode','analytics_revision','coverage','quota','profile_sequences']
   .filter(field=>Object.hasOwn(full,field)).map(field=>[field,full[field]])),
  projection:{version:1,included,omitted}};
}
// Synthetic reset responses; never connect to a Codex account. Tests can
// change window.demoResets before invalidating the reset query.
window.demoResets={infra:{profile:'infra',count:0,auto:false,binding:'fixture-infra',episode:null,exhausted:false,redeemable:false,blocked:false},
                   default:{profile:'default',count:0,auto:false,binding:'fixture-default',episode:null,exhausted:false,redeemable:false,blocked:false}};
window.demoResetCalls=[];
const fixtureProfiles=[{name:'default',profile_id:'p-default',is_default:true,aliases:[]},{name:'infra',profile_id:'p-infra',is_server:true,aliases:[]},{name:'all',profile_id:'p-all',aliases:[]}];
window.scopeCalls=[];
window.demoAggregateCoverage='complete';
window.demoStored={};
function fixtureId(pid,kind,value){return 'ap1.'+btoa(JSON.stringify([pid,kind,value])).replaceAll('+','-').replaceAll('/','_').replace(/=+$/,'')}
function fixtureDecode(value){return JSON.parse(atob(value.slice(4).replaceAll('-','+').replaceAll('_','/')))}
function fixtureQualify(value,p,kind,key='id'){
 if(Array.isArray(value))return value.map(v=>fixtureQualify(v,p));
 if(!value||typeof value!=='object')return value;
 const out={},ids={},types={session_id:'session',parent_session_id:'session',root_session_id:'session',session_after:'session',project_id:'project',subagent_id:'subagent',task:'task',skill:'skill',compression_id:'compression',next_request_id:'request'};
 for(const [k,v] of Object.entries(value)){
  const type=k===key&&kind?kind:types[k];
  if(type&&v!=null&&v!==''){ids[k]=v;out[k]=fixtureId(p.profile_id,type,v)}
  else out[k]=fixtureQualify(v,p);
 }
 return {...out,...(Object.keys(ids).length?{original_ids:ids}:{}),profile:p.name,profile_id:p.profile_id};
}
function fixtureCoverage(profiles){
 const unavailable=window.demoAggregateCoverage==='unavailable',partial=window.demoAggregateCoverage==='partial';
 const rows=profiles.map((p,i)=>({name:p.name,profile_id:p.profile_id,aliases:[],status:unavailable?'unreadable':partial&&i===0?'missing':'read'}));
 return {status:unavailable?'unavailable':partial?'partial':'complete',profiles:rows,read_profiles:rows.filter(p=>p.status==='read').length,selected_profiles:rows.length,discovery:{status:'complete',errors:[]},note:'Synthetic inventory coverage only; not historic capture completeness. Independent profile snapshots, not one atomic snapshot.'};
}
async function fixtureAggregate(u){
 const p=u.searchParams,local=new URLSearchParams(p);local.delete('profile_scope');local.set('offset','0');local.set('limit',String(Math.max(2000,events.length)));
 let profiles=fixtureProfiles.slice(0,2),owner=null;
 for(const [field,kind] of [['session','session'],['project','project'],['subagent','subagent'],['test_id','test'],['skill','skill']]){
  if(!p.get(field))continue;
  let id;try{id=fixtureDecode(p.get(field))}catch{throw new Error('Qualified '+field+' identity required')}
  if(id[1]!==kind||owner&&owner!==id[0])throw new Error('Conflicting qualified identities');
  owner=id[0];local.set(field,id[2]);
 }
 if(owner)profiles=profiles.filter(p=>p.profile_id===owner);
 const coverage=fixtureCoverage(profiles),active=profiles.filter(p=>coverage.profiles.find(c=>c.profile_id===p.profile_id).status==='read');
 // Call only the in-memory fixture function, never a route or network transport.
 const base=await baseDemoRest(u.pathname+'?'+local);
 const out={...base,profile_scope:'all',read_only:true,refresh_mode:'polling',analytics_revision:window.demoAnalyticsRevision||'synthetic-analytics-initial',seq:null,profile_sequences:Object.fromEntries(active.map(p=>[p.profile_id,window.fixtureSequence||events.length])),coverage};
 const offset=Number(p.get('offset')||0),limit=Number(p.get('limit')||200);
 const ordered=(rows,stamp)=>rows.sort((a,b)=>b[stamp]-a[stamp]||String(b.original_ids?.id).localeCompare(String(a.original_ids?.id))||b.profile_id.localeCompare(a.profile_id));
 if(u.pathname==='/ledger/skills'){
  out.skills=active.flatMap(p=>base.skills.map(s=>fixtureQualify(s,p,'skill','name')));
  out.catalogue=active.flatMap(p=>base.catalogue.map(s=>fixtureQualify(s,p,'skill','name')));
  out.catalogue_coverage=active.length?base.catalogue_coverage:{status:'unavailable',since:null};
  const rows=ordered(active.flatMap(p=>base.events.map(e=>fixtureQualify(e,p,'skill_event'))),'ts');
  out.events=rows.slice(offset,offset+limit);out.event_count=active.length&&!p.has('aggregate_only')?rows.length:null;out.next_offset=offset+limit<rows.length?offset+limit:null;
  out.snapshots=ordered(active.flatMap(p=>base.snapshots.map(e=>fixtureQualify(e,p,'skill_event'))),'ts');out.snapshot_count=active.length&&!p.has('aggregate_only')?out.snapshots.length:null;
  out.summary=active.length?Object.fromEntries(Object.entries(base.summary).map(([k,v])=>[k,v*active.length])):null;
 }else{
  const rows=ordered(active.flatMap(p=>base.requests.map(r=>fixtureQualify(r,p,'request'))),'started');
  out.summary=active.length?summarize(rows):null;
  out.subagent_summary=active.length?{...summarize(rows.filter(r=>r.agent_kind==='subagent')),agents:base.subagent_summary.agents*active.length}:null;
  out.requests=rows.slice(offset,offset+limit);out.request_count=active.length?rows.length:null;out.next_offset=offset+limit<rows.length?offset+limit:null;
  for(const [field,kind,key] of [['project_groups','project','key'],['session_groups','session','key'],['subagent_groups','subagent','key'],['project_options','project','id'],['compressions','compression','id'],['tests','test','id'],['groups',null,'id']])out[field]=active.flatMap(p=>(base[field]||[]).map(r=>fixtureQualify(r,p,kind,key)));
  out.compression_count=active.length?out.compressions.length:null;
  out.compression_truncated=false;
  out.provider_groups=grouped(rows,['provider']);out.model_groups=grouped(rows.map(r=>({...r,model:r.response_model||r.model||'unknown'})),['provider','model']);out.applied_rate_groups=appliedRateGroups(rows);
 }
 return u.pathname==='/ledger'?fixtureProject(out,p):out;
}
demoRest=async function(path,options={}){
 const u=new URL(path,'https://offline.test');
 if(u.pathname.startsWith('/codex/resets')){
  const profile=u.searchParams.get('profile')||options.body?.profile||'infra';
  window.demoResetCalls.push({path,method:options.method||'GET',profile});
  const record=window.demoResets[profile];if(!record)throw new Error('Unknown synthetic reset profile');
  if(u.pathname==='/codex/resets/auto'){
   if(options.body.binding!==record.binding)throw new Error('Synthetic account changed');
   record.auto=options.body.enabled;return {...record};
  }
  if(u.pathname==='/codex/resets/redeem'){
   if(options.body.binding!==record.binding||options.body.episode!==record.episode||options.body.count!==record.count||!record.redeemable)throw new Error('Synthetic stale redemption');
   record.count--;record.redeemable=false;record.exhausted=false;record.episode=null;
   return {profile,outcome:'reset',view:{...record}};
  }
  return {...record};
 }
 window.scopeCalls.push({path,method:options.method||'GET',at:Date.now()});
 if(u.pathname==='/ledger/change-token'){
  const original=await baseDemoRest(path,options);
  const selected=u.searchParams.get('profile_scope')!=='all';
  const members=selected?fixtureProfiles.filter(p=>p.name===(u.searchParams.get('profile')||'infra')):fixtureProfiles.slice(0,2);
  const rows=members.map(p=>({profile_id:p.profile_id,name:p.name,aliases:p.aliases,status:'available'}));
  return {...original,coverage:{status:'complete',discovery:{status:'complete',errors:[]},profiles:rows}};
 }
 if(u.pathname==='/ledger/profiles')return {profiles:fixtureProfiles,scope_options:[{label:'All profiles',profile_scope:'all'},...fixtureProfiles.map(p=>({label:p.name,profile:p.name,profile_scope:'selected'}))],default_profile_scope:'selected'};
 if(u.searchParams.get('profile_scope')!=='all'){
  // Optional isolated in-process FastAPI bridge for the incremental browser
  // check. Ordinary preview fixtures never call a backend or provider.
  if(window.backendLedger&&['/ledger','/ledger/refresh'].includes(u.pathname))
   return window.backendLedger({path,method:options.method||'GET',body:options.body});
  const result=await baseDemoRest(path,options);
  if(u.pathname!=='/ledger')return result;
  return fixtureProject({...result,compression_truncated:false,analytics_revision:window.demoAnalyticsRevision||'synthetic-analytics-initial'},u.searchParams);
 }
 if(options.method&&options.method!=='GET')throw new Error('All profiles is read-only');
 if(u.pathname==='/usage')return {profile_scope:'all',read_only:true,providers:[],quota:{available:false,reason:'aggregate_quota_unavailable'}};
 if(u.pathname==='/ledger/status')return {profile_scope:'all',read_only:true,refresh_mode:'polling',status:'unavailable',reason:'Recorder health is profile-specific; select a profile.'};
 if(!['/ledger','/ledger/skills'].includes(u.pathname))throw new Error('Unexpected aggregate route '+path);
 const result=await fixtureAggregate(u);
 if(window.holdAggregate)return new Promise(resolve=>window.holdAggregate.push(()=>resolve(result)));
 return result;
};
