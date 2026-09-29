// Offline-only DTO fixtures. Never loaded by the packaged Desktop plugin.
const baseDemoRest=demoRest;
// Match projection.py's version-1 wire shape. Keep the legacy fixture full:
// projected reads alone omit fields, rather than silently supplying full DTOs.
const fixtureCommon=['generated_at','seq','window','summary','unpriced_total','request_count','next_offset',
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
// Unpriced signal (wire shape of storage.summary): synthetic rows without an
// injected `unpriced` count every incomplete row as usage_missing, 0 tokens.
const fixtureUnpricedReasons=['no_catalogue','model_missing','ambiguous','usage_missing','endpoint_unpriced'];
function fixtureUnpriced(row){
 if(row?.unpriced)return row.unpriced;
 const n=row?.unpriced_requests||0;
 return {requests:n,tokens:0,reasons:Object.fromEntries(fixtureUnpricedReasons.map(k=>[k,k==='usage_missing'?n:0]))};
}
// Row-derived unpriced signal on every synthetic summary (tests set
// `unpriced_reason` on incomplete rows; unset means usage_missing).
if(typeof summarize==='function'&&!summarize.fixtureUnpriced){
 const base=summarize;
 summarize=function(rows){
  const out=base(rows),gap=rows.filter(r=>!r.cost?.complete);
  out.unpriced={requests:gap.length,tokens:gap.reduce((n,r)=>n+(r.usage?.total_tokens||0),0),
   reasons:Object.fromEntries(fixtureUnpricedReasons.map(k=>[k,gap.filter(r=>(r.unpriced_reason||'usage_missing')===k).length]))};
  return out;
 };
 summarize.fixtureUnpriced=true;
}
// Opt-in mirror of the backend trend ladder (trend_resolution nested in storage.sql_trend): set
// window.demoTrendLadder=true before a read. Off by default, so scripts that
// assert the older minute/2-minute/hour/day preview grid are unchanged.
const fixtureTrendLadder=[[1800,60,'minute'],[7200,120,'2 minutes'],[21600,300,'5 minutes'],
 [43200,600,'10 minutes'],[86460,900,'15 minutes'],[172800,1800,'30 minutes'],[604800,3600,'hour'],
 [2592000,21600,'6 hours'],[7776000,86400,'day']];
if(typeof trendFor==='function'&&!trendFor.fixtureLadder){
 const legacy=trendFor;
 trendFor=function(rows,start,end){
  if(!window.demoTrendLadder)return legacy(rows,start,end);
  if(!start)start=rows.length?Math.min(...rows.map(r=>r.started)):end-86400;
  const duration=end-start,hit=fixtureTrendLadder.find(([limit])=>duration<=limit);
  let step=hit?hit[1]:86400;
  if(!hit&&duration/step>400)step=86400*(Math.floor(duration/86400/400)+1);
  const unit=hit?hit[2]:step===86400?'day':step/86400+' days',buckets=[];
  for(let t=Math.floor(start/step)*step;t<end;t+=step){
   const members=rows.filter(r=>r.started>=t&&r.started<t+step);
   buckets.push({start:Math.max(start,t),end:Math.min(end,t+step),...summarize(members),
    provider_buckets:grouped(members,['provider']).sort((a,b)=>a.provider.localeCompare(b.provider))});
  }
  return {unit,timezone:'UTC',seconds:step,buckets};
 };
 trendFor.fixtureLadder=true;
}
function fixtureOverviewSummary(row){
 const reasons=['awaiting_usage','unresolved_execution','abandoned_execution','unverified_accounting','ended_without_usage','unreported_field'];
 return {...row,unpriced:fixtureUnpriced(row),peak_observed:row.peak_observed||{value:window.demoPeakValue??null,unmatched:window.demoPeakUnmatched||0,basis:'matched_child_lifecycle'},unresolved:row.unresolved||0,abandoned:row.abandoned||0,
  supplemental_requests:row.supplemental_requests||0,
  known_cost_usd:String(row.known_cost_usd),
  missing_reasons:Object.fromEntries(Object.entries(row.missing_fields).map(([key,n])=>
   [key,Object.fromEntries(reasons.map(reason=>[reason,reason==='unreported_field'?n:0]))])),
  cost_components:Object.fromEntries(Object.entries(row.cost_components).map(([key,n])=>[key,String(n)])),
  savings:Object.fromEntries(Object.entries(row.savings).map(([key,n])=>[key,String(n)]))};
}
function fixtureProject(full,p){
 const view=p.get('view'),group=p.get('group');
 if(full.summary!==undefined&&full.unpriced_total===undefined){
  const gap=full.summary?fixtureUnpriced(full.summary):null;
  full={...full,unpriced_total:gap?{requests:gap.requests,tokens:gap.tokens}:null};
 }
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
    ...fixtureOverviewSummary(row),start:row.start,end:row.end,
    provider_buckets:row.provider_buckets.map(fixtureOverviewSummary)}))}};
 }
 if(view===null){if(group!==null)throw new Error('Group requires a view');return full}
 if(p.has('list_mode')){
  const field=({requests:'requests',compressions:'compressions',cache:'applied_rate_groups',models:'groups',overview:fixtureGroups[group]||'list_rows'})[view];
  if(field){
   const all=p.get('list_mode')==='all',offset=all?0:Number(p.get('offset')||0),limit=all?20000:Number(p.get('limit')||10);
   const rows=(field==='list_rows'?[...full.trend.buckets].reverse():full[field]).filter(row=>field!=='compressions'||!p.get('compression_kind')||row.kind===p.get('compression_kind'));
   if(all&&rows.length>20000)throw new Error('Full record report exceeds the 20,000-row safety limit; narrow the window.');
   full={...full,[field]:rows.slice(offset,offset+limit),list_count:rows.length,
    list_next_offset:!all&&offset+limit<rows.length?offset+limit:null,
    ...(p.get('profile_scope')==='all'?{list_provenance:'Per-profile copied snapshots; no simultaneous cross-profile instant.'}:{})};
  }
 }
 if(!Object.hasOwn(fixtureViews,view)||view==='overview'&&!Object.hasOwn(fixtureGroups,group)||
    view!=='overview'&&group!==null)throw new Error('Invalid projection');
 const included=[...new Set([...fixtureCommon,...fixtureViews[view],...(view==='overview'&&fixtureGroups[group]?[fixtureGroups[group]]:[])])].sort();
 const omitted=fixtureDetail.filter(field=>!included.includes(field)).sort();
 for(const field of included)if(!Object.hasOwn(full,field))throw new Error('Fixture lacks projected field '+field);
 return {...Object.fromEntries(included.map(field=>[field,full[field]])),
  ...Object.fromEntries(['profile_scope','read_only','refresh_mode','analytics_revision','coverage','quota','profile_sequences']
   .filter(field=>Object.hasOwn(full,field)).map(field=>[field,full[field]])),
  projection:{version:1,included,omitted},
  ...(p.has('list_mode')?{list_count:full.list_count,list_next_offset:full.list_next_offset,
    ...(full.list_rows?{list_rows:full.list_rows}:{}),...(full.list_provenance?{list_provenance:full.list_provenance}:{})}:{})};
}
// Opt-in synthetic Anthropic subscription shaped like the plugin's limits[]
// windows (Current session, Current week, then the model-scoped Fable week) plus
// one recorded request so it has an All-providers row. Not in the default
// catalogue: call before the pane mounts (scripts inject it before register).
function fixtureAnthropicFable({fable=true}={}){
 const event=JSON.parse(JSON.stringify(events[0]));
 Object.assign(event,{provider:'anthropic',id:'fixture-anthropic-fable',started:now-900,ended:now-899});
 events.push(event);
 const windows=[{label:'Current session',used_percent:38,remaining_percent:62,reset_at:new Date((now+3600)*1000).toISOString(),detail:null},
  {label:'Current week',used_percent:45,remaining_percent:55,reset_at:new Date((now+4*86400)*1000).toISOString(),detail:null}];
 if(fable)windows.push({label:'Fable week',used_percent:15,remaining_percent:85,reset_at:new Date((now+4*86400)*1000).toISOString(),detail:null});
 demoProviders.push({id:'anthropic',label:'Anthropic',active:true,configured:true,quota:{available:true,source:'oauth_usage_api',
  title:'Account limits',plan:'Max',details:[],windows}});
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
  out.requests=p.has('list_mode')?rows:rows.slice(offset,offset+limit);out.request_count=active.length?rows.length:null;out.next_offset=offset+limit<rows.length?offset+limit:null;
  for(const [field,kind,key] of [['project_groups','project','key'],['session_groups','session','key'],['subagent_groups','subagent','key'],['project_options','project','id'],['compressions','compression','id'],['tests','test','id'],['groups',null,'id']])out[field]=active.flatMap(p=>(base[field]||[]).map(r=>fixtureQualify(r,p,kind,key)));
  out.compression_count=active.length?out.compressions.length:null;
  out.compression_truncated=false;
  out.provider_groups=grouped(rows,['provider']);out.model_groups=grouped(rows.map(r=>({...r,model:r.response_model||r.model||'unknown'})),['provider','model']);out.applied_rate_groups=appliedRateGroups(rows);
  out.trend=trendFor(rows,base.window.start,base.window.end);
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
  const source=new URL(path,'https://offline.test');
  if(source.pathname==='/ledger'&&source.searchParams.has('list_mode')){source.searchParams.set('limit','20000');source.searchParams.set('offset','0')}
  const result=await baseDemoRest(source.pathname==='/ledger'?source.pathname+source.search:path,options);
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

// Opt-in synthetic Cache & costs rows, one per pricing basis (rate DTO shape
// of pricing.py). Returns {model: [Context text, ...tooltip phrases]} so the
// browser test asserts exactly what this fixture seeded. Never called by
// default: other preview tests see the unchanged synthetic set.
window.demoPricingBasisRows=function(){
 const LIST_BASIS='OpenRouter list price for this model id; serving provider has no catalogue here, actual charge may differ';
 const anth={input_tokens:'5',output_tokens:'25',cache_read_tokens:'.5',cache_write_tokens:'6.25',source:'SYNTHETIC Anthropic public pricing',
  service_tier:'standard',inference_geo:'global',long_context_flat:true,endpoint_assumption:false,geo_assumption:false,
  tier_assumption:false,speed_inferred:false,speed_basis:'usage.speed',pricing_basis:'provider_catalog_estimate'};
 const list={input_tokens:'5',output_tokens:'25',cache_read_tokens:'.5',cache_write_tokens:'6.25',service_tier:'standard',
  source:'OpenRouter list price (fallback)',basis:LIST_BASIS,pricing_basis:'list_price_fallback',tier_assumption:false,
  fallback_model:'anthropic/claude-opus-9.5',fallback_match:'vendor_prefixed'};
 const rows=[
  ['basis-tier',{...anth,tier_assumption:true,tier_basis:'unknown_value_as_default',unknown_values:{'usage.service_tier':'priority_plus'}},
   'Global · assumed tier',['Standard tier assumed: usage.service_tier reported "priority_plus", not in the catalogue.','Standard speed reported by usage.speed.']],
  ['basis-geo',{...anth,geo_assumption:true,geo_basis:'unknown_value_as_default',unknown_values:{'usage.inference_geo':'eu'}},
   'Global · assumed geo',['Global routing assumed: usage.inference_geo reported "eu", not in the catalogue.']],
  ['basis-speed',{...anth,tier_assumption:true,speed_inferred:true,speed_basis:'unknown usage.speed value; documented standard speed assumed',unknown_values:{'usage.speed':'turbo'}},
   'Global · assumed standard speed',['Standard speed assumed: usage.speed reported "turbo", not in the catalogue.']],
  ['basis-na',{...anth,geo_assumption:true,geo_basis:'not_available_as_global'},
   'Global · assumed geo',['Global routing assumed: usage.inference_geo reported not_available on a model with a US rate.']],
  ['basis-unrec',{...anth,geo_assumption:true,geo_basis:'unrecognised_as_global',retrospective:true},
   'Global · assumed geo',['Global routing assumed retrospectively: the pre-fix recorder kept only an unrecognised sentinel.']],
  ['basis-absent',{...anth,geo_assumption:true,tier_assumption:true,speed_inferred:true,speed_basis:'assumed standard speed (no fast request)'},
   'Global · assumed geo/standard speed',['Global routing assumed: no usage.inference_geo was reported.','Standard speed assumed: no fast request and no usage.speed reported.']],
  ['basis-list',{...list},'List price',[LIST_BASIS+'.','Matched OpenRouter id anthropic/claude-opus-9.5 (vendor-prefixed).']],
  ['basis-list-speed',{...list,fallback_match:'exact',fallback_model:'basis-list-speed',speed_inferred:true,
   speed_basis:'unknown usage.speed value; documented standard speed assumed',unknown_values:{'usage.speed':'turbo'}},
   'List price · assumed speed',['Matched OpenRouter id basis-list-speed (exact).','Standard speed assumed: usage.speed reported "turbo", not in the catalogue.']],
  ['basis-routed-tier',{input_tokens:'1',output_tokens:'2',service_tier:'standard',source:'SYNTHETIC routed',pricing_basis:'provider_catalog_estimate',
   tier_assumption:true,tier_basis:'unknown_value_as_default',unknown_values:{'requested service_tier':'turbo'}},
   'Standard · assumed tier',['Standard tier assumed: the request asked for service_tier "turbo", not in the catalogue.']],
  // Banded / minimum-prompt rates keep their band text and still show the assumption.
  ['basis-band-tier',{input_tokens:'2.5',output_tokens:'15',service_tier:'standard',source:'SYNTHETIC OpenAI public pricing',pricing_basis:'provider_catalog_estimate',
   context_band:'short',threshold_tokens:272000,tier_assumption:true,tier_basis:'unknown_value_as_default',unknown_values:{'usage.service_tier':'priority_plus'}},
   '≤272,000 · assumed tier',['Standard tier assumed: usage.service_tier reported "priority_plus", not in the catalogue.']],
  ['basis-long-tier',{input_tokens:'5',output_tokens:'22.5',service_tier:'standard',source:'SYNTHETIC OpenAI public pricing',pricing_basis:'provider_catalog_estimate',
   context_band:'long',threshold_tokens:272000,tier_assumption:true,tier_basis:'unknown_value_as_default',unknown_values:{'usage.service_tier':'priority_plus'}},
   '>272,000 · assumed tier',['Standard tier assumed: usage.service_tier reported "priority_plus", not in the catalogue.']],
  ['basis-list-min-speed',{...list,fallback_match:'exact',fallback_model:'basis-list-min-speed',min_prompt_tokens:200000,speed_inferred:true,
   speed_basis:'unknown usage.speed value; documented standard speed assumed',unknown_values:{'usage.speed':'turbo'}},
   '≥200,000 · assumed speed',['Matched OpenRouter id basis-list-min-speed (exact).','Standard speed assumed: usage.speed reported "turbo", not in the catalogue.']],
  ['basis-band-plain',{input_tokens:'2.5',output_tokens:'15',service_tier:'standard',source:'SYNTHETIC OpenAI public pricing',pricing_basis:'provider_catalog_estimate',
   context_band:'short',threshold_tokens:128000,tier_assumption:false},'≤128,000',[]],
 ];
 const expected={};
 rows.forEach(([model,rate,text,phrases],i)=>{
  expected[model]=[text,...phrases];
  const usage={input_tokens:100,output_tokens:10,cache_read_tokens:0,cache_write_tokens:0,reasoning_tokens:0,prompt_tokens:100,total_tokens:110,usage_source:'SYNTHETIC TEST',request_count:1};
  events.push({id:'basis-'+i,provider:'openrouter',model,response_model:model,service_tier:'unspecified',started:now-120-i,ended:now-119-i,
   session_id:'basis-session',task:'main',agent_kind:'primary',status:'completed',usage,
   cost:{components:{input_tokens:0,output_tokens:0,cache_read_tokens:0,cache_write_tokens:0},total_usd:0,known_components_usd:0,complete:true,rate:{provider:'openrouter',model,...rate},
    cache_read_savings_usd:0,cache_write_premium_usd:0,cache_savings_usd:0}});
 });
 window.demoChange++;
 for(const s of window.demoSubscribers)s({type:'changed',mode:'native-events'});
 return expected;
};
