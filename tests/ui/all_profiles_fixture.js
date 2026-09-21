// Offline-only DTO fixtures. Never loaded by the packaged Desktop plugin.
const baseDemoRest=demoRest;
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
 const out={...base,profile_scope:'all',read_only:true,refresh_mode:'polling',seq:null,profile_sequences:Object.fromEntries(active.map(p=>[p.profile_id,window.fixtureSequence||events.length])),coverage};
 const offset=Number(p.get('offset')||0),limit=Number(p.get('limit')||200);
 const ordered=(rows,stamp)=>rows.sort((a,b)=>b[stamp]-a[stamp]||String(b.original_ids?.id).localeCompare(String(a.original_ids?.id))||b.profile_id.localeCompare(a.profile_id));
 if(u.pathname==='/ledger/skills'){
  out.skills=active.flatMap(p=>base.skills.map(s=>fixtureQualify(s,p,'skill','name')));
  const rows=ordered(active.flatMap(p=>base.events.map(e=>fixtureQualify(e,p,'skill_event'))),'ts');
  out.events=rows.slice(offset,offset+limit);out.event_count=active.length?rows.length:null;out.next_offset=offset+limit<rows.length?offset+limit:null;
  out.snapshots=ordered(active.flatMap(p=>base.snapshots.map(e=>fixtureQualify(e,p,'skill_event'))),'ts');out.snapshot_count=active.length?out.snapshots.length:null;
  out.summary=active.length?Object.fromEntries(Object.entries(base.summary).map(([k,v])=>[k,v*active.length])):null;
 }else{
  const rows=ordered(active.flatMap(p=>base.requests.map(r=>fixtureQualify(r,p,'request'))),'started');
  out.summary=active.length?summarize(rows):null;
  out.subagent_summary=active.length?{...summarize(rows.filter(r=>r.agent_kind==='subagent')),agents:base.subagent_summary.agents*active.length}:null;
  out.requests=rows.slice(offset,offset+limit);out.request_count=active.length?rows.length:null;out.next_offset=offset+limit<rows.length?offset+limit:null;
  for(const [field,kind,key] of [['project_groups','project','key'],['session_groups','session','key'],['subagent_groups','subagent','key'],['project_options','project','id'],['compressions','compression','id'],['tests','test','id'],['groups',null,'id']])out[field]=active.flatMap(p=>(base[field]||[]).map(r=>fixtureQualify(r,p,kind,key)));
  out.compression_count=active.length?out.compressions.length:null;
  out.provider_groups=grouped(rows,['provider']);out.model_groups=grouped(rows,['provider','model']);out.applied_rate_groups=appliedRateGroups(rows);
 }
 return out;
}
demoRest=async function(path,options={}){
 window.scopeCalls.push({path,method:options.method||'GET',at:Date.now()});
 const u=new URL(path,'https://offline.test');
 if(u.pathname==='/ledger/profiles')return {profiles:fixtureProfiles,scope_options:[{label:'All profiles',profile_scope:'all'},...fixtureProfiles.map(p=>({label:p.name,profile:p.name,profile_scope:'selected'}))],default_profile_scope:'selected'};
 if(u.searchParams.get('profile_scope')!=='all')return baseDemoRest(path,options);
 if(options.method&&options.method!=='GET')throw new Error('All profiles is read-only');
 if(u.pathname==='/usage')return {profile_scope:'all',read_only:true,providers:[],quota:{available:false,reason:'aggregate_quota_unavailable'}};
 if(u.pathname==='/ledger/status')return {profile_scope:'all',read_only:true,refresh_mode:'polling',status:'unavailable',reason:'Recorder health is profile-specific; select a profile.'};
 if(!['/ledger','/ledger/skills'].includes(u.pathname))throw new Error('Unexpected aggregate route '+path);
 const result=await fixtureAggregate(u);
 if(window.holdAggregate)return new Promise(resolve=>window.holdAggregate.push(()=>resolve(result)));
 return result;
};
