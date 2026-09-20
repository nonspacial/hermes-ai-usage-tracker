"""Per-profile durable event ledger. Request accounting is independent of Hermes state.db; metadata lookup is read-only."""
from __future__ import annotations
import json, os, socket, sqlite3, threading, time, uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from .accounting import METRICS, BUCKETS, costs, validate_rate, effective_record

LOCK=threading.RLock()
SCHEMA='''
CREATE TABLE IF NOT EXISTS session_context(session_id TEXT PRIMARY KEY, updated REAL NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, item_id TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, started REAL NOT NULL, ended REAL, provider TEXT, model TEXT, session_id TEXT, task TEXT, compression_id TEXT, status TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS request_time ON requests(started);
CREATE INDEX IF NOT EXISTS request_session_time ON requests(session_id,started);
CREATE INDEX IF NOT EXISTS request_provider ON requests(provider,started);
CREATE INDEX IF NOT EXISTS request_comp ON requests(compression_id);
CREATE TABLE IF NOT EXISTS compressions(id TEXT PRIMARY KEY, started REAL NOT NULL, ended REAL, provider TEXT, session_id TEXT, session_after TEXT, next_request_id TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS comp_time ON compressions(started);
CREATE TABLE IF NOT EXISTS provider_catalog(id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, observed REAL NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS catalog_time ON provider_catalog(source_id,observed);
CREATE TABLE IF NOT EXISTS pricing_status(source_id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rates(id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tests(id TEXT PRIMARY KEY, started REAL NOT NULL, ended REAL, label TEXT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS health(process TEXT PRIMARY KEY, updated REAL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quota(seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, data TEXT NOT NULL);
'''

def home():
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError: return Path(os.environ.get('HERMES_HOME',str(Path.home()/'.hermes')))

def iso(ts): return datetime.fromtimestamp(ts,timezone.utc).isoformat()
def jd(obj): return json.dumps(obj,ensure_ascii=False,separators=(',',':'),allow_nan=False)

def notify(folder):
    # Producers emit tiny hints after durable writes. Missed hints never lose records.
    if not hasattr(socket,'AF_UNIX'): return
    for path in folder.glob('notify-*.sock'):
        s=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM)
        try: s.settimeout(.005); s.sendto(b'changed',str(path))
        except OSError: pass
        finally: s.close()

class Store:
    def __init__(self,root=None):
        self.root=Path(root) if root else home()
        self.folder=self.root/'usage-ledger'
        self.folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=self.folder/'events.sqlite3'
        with self.db() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript(SCHEMA)
        try: os.chmod(self.path,0o600)
        except OSError: pass
        from .pricing import seed
        seed(self)
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.path,timeout=2)
        c.row_factory=sqlite3.Row
        c.create_aggregate('decimal_sum',1,DecimalSum)
        c.execute('PRAGMA synchronous=FULL')
        try:
            yield c; c.commit()
        except BaseException:
            c.rollback(); raise
        finally: c.close()
    def event(self,c,kind,key,data):
        c.execute('INSERT INTO events(ts,kind,item_id,data) VALUES(?,?,?,?)',(time.time(),kind,key,jd(data)))
    def latest_rate(self,c,rec):
        from .pricing import lookup
        return lookup(c,rec)
    def request(self,rec,kind):
        rec=dict(rec); key=rec['id']
        with LOCK,self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT data FROM requests WHERE id=?',(key,)).fetchone()
            data=json.loads(old[0]) if old else {}
            old_cost=data.get('cost')
            was_final=data.get('status') not in ('pending',None) and data.get('ended') is not None
            # Repeated pre/success notifications must not duplicate or downgrade a row.
            if old and kind=='request_started': return
            data.update({k:v for k,v in rec.items() if v is not None})
            data.setdefault('started',time.time());data.setdefault('status','pending')
            from .attribution import attach
            attach(c,data)
            if 'usage' in data:
                # Final records keep the price snapshot chosen at completion, even
                # if a duplicate notification arrives after a catalog refresh.
                rate=old_cost.get('rate') if was_final and old_cost else self.latest_rate(c,data)
                data['cost']=costs(data['usage'],rate)
            if kind=='request_started' and data.get('source')=='main_hook' and data.get('session_id'):
                candidates=c.execute("SELECT id,data FROM compressions WHERE session_after=? AND ended IS NOT NULL AND ended<=? AND next_request_id IS NULL ORDER BY ended DESC",(data['session_id'],data['started'])).fetchall()
                for i,row in enumerate(candidates):
                    comp=json.loads(row['data'])
                    if i==0:
                        comp['next_request_id']=key; comp['next_request_approx_tokens']=data.get('approx_input_tokens')
                    else: comp['next_request_id']='superseded';comp['next_context_status']='another compression intervened'
                    self._put_comp(c,comp)
            c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET ended=excluded.ended,provider=excluded.provider,model=excluded.model,session_id=excluded.session_id,task=excluded.task,compression_id=excluded.compression_id,status=excluded.status,data=excluded.data',
                (key,data['started'],data.get('ended'),data.get('provider','unknown'),data.get('model','unknown'),data.get('session_id',''),data.get('task','main'),data.get('compression_id'),data['status'],jd(data)))
            if data.get('usage'):
                for row in c.execute('SELECT data FROM compressions WHERE next_request_id=?',(key,)).fetchall():
                    comp=json.loads(row[0]);comp['next_request_prompt_tokens']=data['usage'].get('prompt_tokens');comp['next_usage_source']=data['usage'].get('usage_source');self._put_comp(c,comp)
            self.event(c,kind,key,rec)
        notify(self.folder)
    def _put_comp(self,c,data):
        c.execute('INSERT INTO compressions VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET ended=excluded.ended,provider=excluded.provider,session_after=excluded.session_after,next_request_id=excluded.next_request_id,data=excluded.data',
            (data['id'],data['started'],data.get('ended'),data.get('provider','unknown'),data.get('session_id',''),data.get('session_after'),data.get('next_request_id'),jd(data)))
    def compression(self,rec,kind='compression_updated'):
        with LOCK,self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT data FROM compressions WHERE id=?',(rec['id'],)).fetchone()
            data=json.loads(old[0]) if old else {}
            data.update({k:v for k,v in rec.items() if v is not None});data.setdefault('started',time.time())
            from .attribution import attach
            attach(c,data)
            self._put_comp(c,data);self.event(c,kind,rec['id'],rec)
        notify(self.folder)
    def health(self,process,data):
        with self.db() as c: c.execute('INSERT OR REPLACE INTO health VALUES(?,?,?)',(process,time.time(),jd(data)))
    def save_quota(self,payload):
        # Deliberately keep quota windows, not provider details / account identity.
        data={'generated_at':payload.get('generated_at'),'providers':[]}
        for p in payload.get('providers',[]):
            q=p.get('quota') or {}
            windows=[{k:w.get(k) for k in ('label','remaining_percent','reset_at','used_percent')} for w in q.get('windows',[]) if isinstance(w,dict)]
            data['providers'].append({'provider':p.get('id'),'available':q.get('available'),'windows':windows})
        with self.db() as c: c.execute('INSERT INTO quota(ts,data) VALUES(?,?)',(time.time(),jd(data)))
    def rate(self,data):
        r=validate_rate(data)
        with self.db() as c:
            cur=c.execute('INSERT INTO rates(created,data) VALUES(?,?)',(time.time(),jd(r)));self.event(c,'rate_created',str(cur.lastrowid),r)
        notify(self.folder);return r
    def test(self,action,label='',key=None):
        now=time.time()
        with self.db() as c:
            if action=='start':
                key=uuid.uuid4().hex
                data={'id':key,'started':now,'label':str(label or 'Reset test')[:160]}
                c.execute('INSERT INTO tests VALUES(?,?,?,?,?)',(key,now,None,data['label'],jd(data)))
            elif action=='stop':
                row=c.execute('SELECT data FROM tests WHERE id=?',(key,)).fetchone()
                if not row: raise ValueError('Unknown test.')
                data=json.loads(row[0]);data['ended']=now
                c.execute('UPDATE tests SET ended=?,data=? WHERE id=?',(now,jd(data),key))
            else: raise ValueError('Action must be start or stop.')
            self.event(c,'test_'+action,key,data)
        notify(self.folder);return data
    def test_window(self,key):
        with self.db() as c:row=c.execute('SELECT started,ended FROM tests WHERE id=?',(key,)).fetchone()
        if not row:raise ValueError('Unknown test marker.')
        return row['started'],row['ended']
    def read(self,start=0,end=None,provider='',offset=0,limit=200,session='',agent='',project='',session_scope='exact',subagent=''):
        if agent not in ('','primary','subagent','unknown'):raise ValueError('Invalid agent filter.')
        if session_scope not in ('exact','family'):raise ValueError('Invalid session scope.')
        end=end or time.time()
        if start<0 or end<start: raise ValueError('Invalid time window.')
        where='started>=? AND started<?';params=[start,end]
        if provider: where+=' AND provider=?';params.append(provider)
        if session:
            if session_scope=='family':
                where+=" AND (session_id=? OR EXISTS (SELECT 1 FROM json_each(requests.data,'$.session_lineage') WHERE value=?))";params.extend([session,session])
            else:where+=' AND session_id=?';params.append(session)
        if agent:where+=" AND COALESCE(json_extract(data,'$.agent_kind'),'unknown')=?";params.append(agent)
        if subagent:
            where+=" AND COALESCE(NULLIF(json_extract(data,'$.subagent_id'),''),session_id)=?";params.append(subagent)
        if project:
            where+=" AND COALESCE(json_extract(data,'$.project_id'),'unattributed')=?";params.append(project)
        with self.db() as c:
            # Keep totals, applied rates and request rows on one read snapshot.
            c.execute('BEGIN')
            legacy_read_view(c,where,params)
            from .session_cache_writes import project as project_session_writes
            project_session_writes(c,where,params,end)
            total=sql_summary(c,where,params)
            applied_rates=sql_applied_rate_groups(c,where,params)
            rows=[json.loads(r[0]) for r in c.execute('SELECT data FROM requests WHERE '+where+' ORDER BY started DESC LIMIT ? OFFSET ?',params+[limit,offset])]
            from .cache_progression import query_progression
            read_progression, read_changes = query_progression(c,where,params,start,end,{r['id'] for r in rows})
            groups=[dict(provider=row['provider'],model=row['model'],agent_kind=row['agent_kind'],task=row['task'],**summary_from_sql(row)) for row in c.execute("SELECT provider,model,COALESCE(json_extract(data,'$.agent_kind'),'unknown') AS agent_kind,task,"+SUMMARY_SQL+" FROM requests WHERE "+where+" GROUP BY provider,model,COALESCE(json_extract(data,'$.agent_kind'),'unknown'),task",params)]
            provider_groups=[dict(provider=row['provider'],**summary_from_sql(row)) for row in c.execute('SELECT provider,'+SUMMARY_SQL+' FROM requests WHERE '+where+' GROUP BY provider',params)]
            model_groups=[dict(provider=row['provider'],model=row['model'],**summary_from_sql(row)) for row in c.execute("SELECT provider,COALESCE(json_extract(data,'$.response_model'),model) AS model,"+SUMMARY_SQL+" FROM requests WHERE "+where+" GROUP BY provider,COALESCE(json_extract(data,'$.response_model'),model)",params)]
            trend=sql_trend(c,where,params,start,end)
            attribution=attribution_groups(c,where,params)
            from .pricing import catalog_status
            catalogs=catalog_status(c,provider)
            for row in rows:
                if row['id'] in read_changes:row['cache_read_change']=read_changes[row['id']]
            compwhere=where.replace('requests.data','compressions.data')
            compression_count=c.execute('SELECT COUNT(*) FROM compressions WHERE '+compwhere,params).fetchone()[0]
            comps=[json.loads(r[0]) for r in c.execute('SELECT data FROM compressions WHERE '+compwhere+' ORDER BY started DESC LIMIT 1000',params)]
            # Compression bill is derived from its linked requests, never counted twice.
            for comp in comps:
                aux=[json.loads(r[0]) for r in c.execute('SELECT data FROM requests WHERE compression_id=?',(comp['id'],))]
                comp['auxiliary']=summary(aux);comp['aux_request_ids']=[r['id'] for r in aux]
            health=[dict(process=r['process'],updated=r['updated'],**json.loads(r['data'])) for r in c.execute('SELECT * FROM health ORDER BY updated DESC')]
            rates=[dict(id=r['id'],created=r['created'],**json.loads(r['data'])) for r in c.execute('SELECT * FROM rates ORDER BY id DESC LIMIT 500')]
            tests=[json.loads(r[0]) for r in c.execute('SELECT data FROM tests ORDER BY started DESC LIMIT 100')]
            providers=[r[0] for r in c.execute('SELECT DISTINCT provider FROM requests ORDER BY provider')]
            seq=c.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            quota=[dict(ts=r['ts'],**json.loads(r['data'])) for r in c.execute('SELECT ts,data FROM quota WHERE ts>=? AND ts<? ORDER BY ts DESC LIMIT 200',(start,end))]
            crossing_end=c.execute('SELECT COUNT(*) FROM requests WHERE '+where+' AND (ended IS NULL OR ended>=?)',params+[end]).fetchone()[0]
            crossing=c.execute('SELECT COUNT(*) FROM requests WHERE started<? AND (ended IS NULL OR ended>=?)'+where[len('started>=? AND started<?'):],[start,start]+params[2:]).fetchone()[0]
        return {'generated_at':time.time(),'seq':seq,'window':{'start':start,'end':end,'basis':'request/compaction start time; crossing requests are not split'},
          **attribution,'summary':total,'requests':rows,'request_count':total['attempts'],'next_offset':offset+limit if offset+limit<total['attempts'] else None,
          'compressions':comps,'compression_count':compression_count,'compression_truncated':compression_count>1000,
          'groups':groups,'provider_groups':provider_groups,'model_groups':model_groups,'trend':trend,'price_catalogs':catalogs,
          'applied_rate_groups':applied_rates,'cache_read_progression':read_progression,
          'providers':providers,'health':health,'rates':rates,'tests':tests,'quota_observations':quota,'crossing_start':crossing,'crossing_end':crossing_end}

def summary(rows):
    out={'sessions':len({r.get('session_id') for r in rows if r.get('session_id')}),'attempts':len(rows),'pending':0,'missing_usage':0,'partial_breakdown':0,'aggregate_records':0,
         'known':{k:0 for k in METRICS},'missing_fields':{k:0 for k in METRICS},
         'cost_components':{k:Decimal(0) for k in BUCKETS},'cost_missing_fields':{k:0 for k in BUCKETS},'unpriced_requests':0,'priced_requests':0,'known_cost_usd':Decimal(0)}
    savings_keys=('cache_read_savings_usd','cache_write_premium_usd','cache_savings_usd')
    out['savings']={k:Decimal(0) for k in savings_keys};out['savings_missing']={k:0 for k in savings_keys}
    from .session_cache_writes import blank_summary, add_summary
    out['session_cache_writes']=blank_summary()
    for r in rows:
        r=effective_record(r)
        add_summary(out['session_cache_writes'],r)
        u=r.get('usage') or {}
        if r.get('status')=='pending':out['pending']+=1
        if u.get('total_tokens') is None:out['missing_usage']+=1
        if any(u.get(k) is None for k in BUCKETS):out['partial_breakdown']+=1
        if u.get('request_count',1)>1:out['aggregate_records']+=1
        for k in METRICS:
            if u.get(k) is None: out['missing_fields'][k]+=1
            else:out['known'][k]+=u[k]
        cost=r.get('cost') or {}
        if cost.get('complete'):out['priced_requests']+=1
        else:out['unpriced_requests']+=1
        for k in BUCKETS:
            value=cost.get('components',{}).get(k)
            if value is None:out['cost_missing_fields'][k]+=1
            else:out['cost_components'][k]+=Decimal(value)
        out['known_cost_usd']+=Decimal(cost.get('known_components_usd','0'))
        for k in savings_keys:
            if cost.get(k) is None:out['savings_missing'][k]+=1
            else:out['savings'][k]+=Decimal(cost[k])
    out['savings']={k:str(v) for k,v in out['savings'].items()}
    out['cost_components']={k:str(v) for k,v in out['cost_components'].items()}
    out['known_cost_usd']=str(out['known_cost_usd'])
    # Weighted rate only where every read/input denominator is known across this set.
    out['cache_hit_rate']=out['known']['cache_read_tokens']/out['known']['prompt_tokens'] if out['known']['prompt_tokens'] and not out['missing_fields']['cache_read_tokens'] and not out['missing_fields']['prompt_tokens'] else None
    return out


class DecimalSum:
    def __init__(self):self.total=Decimal(0)
    def step(self,value):
        if value is not None:self.total+=Decimal(str(value))
    def finalize(self):return str(self.total)

def _exprs():
    cols=["COUNT(DISTINCT NULLIF(session_id,'')) AS sessions", "COUNT(*) AS attempts", "COALESCE(SUM(status='pending'),0) AS pending",
      "COALESCE(SUM(json_extract(data,'$.usage.total_tokens') IS NULL),0) AS missing_usage",
      "COALESCE(SUM(json_extract(data,'$.usage.request_count')>1),0) AS aggregate_records",
      "COALESCE(SUM("+' OR '.join("json_extract(data,'$.usage."+k+"') IS NULL" for k in BUCKETS)+"),0) AS partial_breakdown",
      "COALESCE(SUM(json_extract(data,'$.cost.complete')=1),0) AS priced_requests",
      "COALESCE(decimal_sum(json_extract(data,'$.cost.known_components_usd')),'0') AS known_cost_usd"]
    cols += ["COALESCE(SUM(json_extract(data,'$.calculated_cache_writes.tokens')),0) AS cw_tokens",
      "COUNT(json_extract(data,'$.calculated_cache_writes.tokens')) AS cw_compared",
      "COALESCE(SUM(json_extract(data,'$.calculated_cache_writes.status')='baseline'),0) AS cw_baselines",
      "COALESCE(SUM(json_extract(data,'$.calculated_cache_writes.tokens') IS NULL),0) AS cw_missing"]
    for k in METRICS:
        cols += ["COALESCE(SUM(json_extract(data,'$.usage."+k+"')),0) AS k_"+k,"COALESCE(SUM(json_extract(data,'$.usage."+k+"') IS NULL),0) AS m_"+k]
    for k in BUCKETS:
        cols += ["COALESCE(decimal_sum(json_extract(data,'$.cost.components."+k+"')),'0') AS c_"+k,"COALESCE(SUM(json_extract(data,'$.cost.components."+k+"') IS NULL),0) AS cm_"+k]
    for k in ('cache_read_savings_usd','cache_write_premium_usd','cache_savings_usd'):
        cols += ["COALESCE(decimal_sum(json_extract(data,'$.cost."+k+"')),'0') AS s_"+k,"COALESCE(SUM(json_extract(data,'$.cost."+k+"') IS NULL),0) AS sm_"+k]
    return ','.join(cols)
SUMMARY_SQL=_exprs()

def summary_from_sql(row):
    out={k:row[k] for k in ('sessions','attempts','pending','missing_usage','aggregate_records','partial_breakdown','priced_requests','known_cost_usd')}
    from .session_cache_writes import blank_summary
    out['session_cache_writes']=dict(blank_summary(),tokens=row['cw_tokens'],compared_requests=row['cw_compared'],baseline_requests=row['cw_baselines'],missing_requests=row['cw_missing'])
    out['unpriced_requests']=out['attempts']-out['priced_requests']
    keys=('cache_read_savings_usd','cache_write_premium_usd','cache_savings_usd')
    out['savings']={k:row['s_'+k] for k in keys};out['savings_missing']={k:row['sm_'+k] for k in keys}
    for group,prefix,fields in [('known','k_',METRICS),('missing_fields','m_',METRICS),('cost_components','c_',BUCKETS),('cost_missing_fields','cm_',BUCKETS)]:
        out[group]={k:row[prefix+k] for k in fields}
    out['cache_hit_rate']=out['known']['cache_read_tokens']/out['known']['prompt_tokens'] if out['known']['prompt_tokens'] and not out['missing_fields']['cache_read_tokens'] and not out['missing_fields']['prompt_tokens'] else None
    return out
def sql_summary(c,where,params):
    return summary_from_sql(c.execute('SELECT '+SUMMARY_SQL+' FROM requests WHERE '+where,params).fetchone())


def sql_trend(c,where,params,start,end):
    """Aggregate the complete filtered ledger, not the paginated request table.

    UTC calendar buckets clipped to the selected bounds. A bucket with attempts
    but no measured value remains unknown in the UI, not a zero-consumption claim.
    """
    if start==0:
        first=c.execute('SELECT MIN(started) FROM requests WHERE '+where,params).fetchone()[0]
        start=first if first is not None else max(0,end-86400)
    step=3600 if end-start<=172800 else 86400
    if (end-start)/step>400:step=86400*max(1,int((end-start)/86400/400)+1)
    lo=int(start//step)*step
    rows={int(row['bucket']):summary_from_sql(row) for row in c.execute(
      'SELECT CAST(started / ? AS INTEGER) * ? AS bucket,'+SUMMARY_SQL+' FROM requests WHERE '+where+' GROUP BY bucket', [step,step]+params)}
    blank=summary([])
    buckets=[dict(start=max(start,t),end=min(end,t+step),**rows.get(t,blank)) for t in range(lo,int(end)+1,step) if t<end]
    return {'unit':'hour' if step==3600 else ('day' if step==86400 else str(step//86400)+' days'),'timezone':'UTC','seconds':step,'buckets':buckets}


def attribution_groups(c,where,params):
    """Complete filtered-set rollups; a child is a subset, never an extra total."""
    role="COALESCE(json_extract(data,'$.agent_kind'),'unknown')"
    project="COALESCE(json_extract(data,'$.project_id'),'unattributed')"
    root="COALESCE(NULLIF(json_extract(data,'$.root_session_id'),''),NULLIF(session_id,''),'unattributed')"
    child="COALESCE(NULLIF(json_extract(data,'$.subagent_id'),''),NULLIF(session_id,''),'unattributed')"
    subs=sql_summary(c,where+" AND "+role+"='subagent'",params)
    subs['agents']=c.execute('SELECT COUNT(DISTINCT '+child+') FROM requests WHERE '+where+" AND "+role+"='subagent'",params).fetchone()[0]
    roles=[dict(agent_kind=r['key'],**summary_from_sql(r)) for r in c.execute('SELECT '+role+' AS key,'+SUMMARY_SQL+' FROM requests WHERE '+where+' GROUP BY '+role,params)]
    extra=",COALESCE(SUM(CASE WHEN "+role+"='subagent' THEN json_extract(data,'$.usage.total_tokens') ELSE 0 END),0) AS subagent_tokens,COUNT(DISTINCT CASE WHEN "+role+"='subagent' THEN "+child+" END) AS subagents"
    configs=[('project_groups',project,",MAX(json_extract(data,'$.project_label')) AS label,MAX(json_extract(data,'$.project_path')) AS path,MAX(json_extract(data,'$.project_source')) AS basis",''),
      ('session_groups',root,",MAX(json_extract(data,'$.project_label')) AS project_label",''),
      ('subagent_groups',child,",MIN(session_id) AS session_id,MAX(json_extract(data,'$.parent_session_id')) AS parent_session_id,MAX(json_extract(data,'$.root_session_id')) AS root_session_id,MAX(json_extract(data,'$.agent_role')) AS agent_role,MAX(json_extract(data,'$.project_label')) AS project_label", " AND "+role+"='subagent'")]
    out={'subagent_summary':subs,'agent_groups':roles}
    for name,expr,more,condition in configs:
        rows=c.execute('SELECT '+expr+' AS key'+more+extra+','+SUMMARY_SQL+' FROM requests WHERE '+where+condition+' GROUP BY '+expr,params)
        out[name]=[dict(**{k:r[k] for k in r.keys() if k in ('key','label','path','basis','project_label','session_id','parent_session_id','root_session_id','agent_role','subagent_tokens','subagents')},**summary_from_sql(r)) for r in rows]
    out['project_options']=[dict(id=r[0],label=r[1] or 'Unattributed project',path=r[2],basis=r[3]) for r in c.execute('SELECT '+project+",MAX(json_extract(data,'$.project_label')),MAX(json_extract(data,'$.project_path')),MAX(json_extract(data,'$.project_source')) FROM requests GROUP BY "+project)]
    return out


def sql_applied_rate_groups(c,where,params):
    """Actual saved rate snapshots used by the complete filtered request set.

    Never read the current catalogue or infer prices from the model name here.
    Revisions, tiers and providers stay separate; unpriced/pending attempts remain
    visible with a null rate. The aggregates are independent of request pagination.
    All rate provenance is preserved in the response for inspectable source details.
    """
    model="COALESCE(NULLIF(json_extract(data,'$.response_model'),''),NULLIF(model,''),'unknown')"
    tier="COALESCE(NULLIF(json_extract(data,'$.cost.rate.service_tier'),''),NULLIF(json_extract(data,'$.returned_service_tier'),''),NULLIF(json_extract(data,'$.service_tier'),''),'unspecified')"
    rate="json_extract(data,'$.cost.rate')"
    sql=('SELECT provider,'+model+' AS actual_model,'+tier+' AS applied_tier,'+rate+' AS saved_rate,'
         +SUMMARY_SQL+' FROM requests WHERE '+where+' GROUP BY provider,'+model+','+tier+','+rate
         +' ORDER BY provider,actual_model,applied_tier,saved_rate')
    result=[]
    for row in c.execute(sql,params):
        result.append(dict(provider=row['provider'],model=row['actual_model'],
            service_tier=row['applied_tier'],rate=json.loads(row['saved_rate']) if row['saved_rate'] else None,
            **summary_from_sql(row)))
    return result


def legacy_read_view(c,where,params):
    """Project selected legacy-only rows, in a TEMP table for this read connection.

    Aggregates, paging, CSV and applied-rate totals then all see the same values.
    No UPDATE touches main.requests or its append-only events. SQL identifiers
    are fixed internal names; only bound values enter the scope query.
    """
    condition=("json_extract(data,'$.usage.usage_source')='hermes_normalized' "
      "AND json_extract(data,'$.usage.raw_usage') IS NULL "
      "AND COALESCE(json_extract(data,'$.usage.normalization_version'),0)<>2 "
      "AND (json_extract(data,'$.usage.cache_read_tokens')=0 OR json_extract(data,'$.usage.cache_write_tokens')=0)")
    rows=c.execute('SELECT id,data FROM main.requests WHERE ('+where+') AND '+condition,params).fetchall()
    if not rows:return
    c.execute('CREATE TEMP TABLE usage_legacy_projection(id TEXT PRIMARY KEY,data TEXT NOT NULL)')
    c.executemany('INSERT INTO usage_legacy_projection VALUES(?,?)',[(r['id'],jd(effective_record(json.loads(r['data'])))) for r in rows])
    c.execute("""CREATE TEMP VIEW requests AS
      SELECT r.id,r.started,r.ended,r.provider,r.model,r.session_id,r.task,r.compression_id,r.status,
      COALESCE(p.data,r.data) AS data FROM main.requests r
      LEFT JOIN usage_legacy_projection p ON p.id=r.id""")
