"""Per-profile durable event ledger. Request accounting is independent of Hermes state.db; metadata lookup is read-only."""
from __future__ import annotations
import json, os, socket, sqlite3, threading, time, uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from .accounting import METRICS, BUCKETS, costs, validate_rate, effective_record, usage_priority

LOCK=threading.RLock()
SCHEMA='''
CREATE TABLE IF NOT EXISTS session_context(session_id TEXT PRIMARY KEY, updated REAL NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, item_id TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, started REAL NOT NULL, ended REAL, provider TEXT, model TEXT, session_id TEXT, task TEXT, compression_id TEXT, status TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS request_time ON requests(started);
CREATE INDEX IF NOT EXISTS request_open_identity ON requests(id) WHERE status IN ('pending','usage_received') AND ended IS NULL;
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
        # Existing ledgers are never migrated implicitly. Check for the base
        # table *under the writer lock*, not file existence: two processes can
        # observe the same just-created empty SQLite file before either seeds it.
        for attempt in range(3):
            try:
                with LOCK,self.db() as c:
                    c.execute('PRAGMA journal_mode=WAL')
                    c.execute('BEGIN IMMEDIATE')
                    fresh=not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='requests'").fetchone()
                    from .skills import SCHEMA as SKILLS_SCHEMA
                    # executescript() would COMMIT before the first DDL. Parse
                    # complete statements, including any future compound DDL,
                    # while retaining this one writer transaction.
                    statement=''
                    for line in (SCHEMA+SKILLS_SCHEMA).splitlines(keepends=True):
                        statement+=line
                        if sqlite3.complete_statement(statement):
                            c.execute(statement)
                            statement=''
                    if statement.strip():
                        raise ValueError('Incomplete ledger schema statement.')
                    if fresh:
                        from .incremental import install
                        install(c)
                break
            except sqlite3.OperationalError as exc:
                if 'locked' not in str(exc).lower() or attempt==2:
                    raise
                time.sleep(.05*(attempt+1))
        try: os.chmod(self.path,0o600)
        except OSError: pass
        from .pricing import seed
        seed(self)
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.path,timeout=2)
        c.row_factory=sqlite3.Row
        c.create_aggregate('decimal_sum',1,DecimalSum)
        c.create_function('canonical_rate',1,canonical_rate,deterministic=True)
        from .ownership import register_sql
        register_sql(c)
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
    def request(self,rec,kind,*,expected=None):
        rec=dict(rec); key=rec['id']
        with LOCK,self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT data FROM requests WHERE id=?',(key,)).fetchone()
            data=json.loads(old[0]) if old else {}
            old_cost=data.get('cost')
            # Compare-and-update inside the transaction, not against the earlier
            # cleanup SELECT. Another callback may already have completed it.
            if expected and (not old or any(data.get(k) not in values for k,values in expected.items())):return
            old_status=data.get('status');old_ended=data.get('ended')
            was_final=data.get('status') not in ('pending','usage_received',None)
            # Repeated pre/success notifications must not duplicate or downgrade a row.
            if old and kind=='request_started': return
            previous_usage=data.get('usage') or {}
            incoming=rec.get('usage')
            if isinstance(incoming,dict) and previous_usage:
                weaker=usage_priority(incoming.get('usage_source'))<usage_priority(previous_usage.get('usage_source'))
                empty=all(incoming.get(k) is None for k in METRICS) and any(previous_usage.get(k) is not None for k in METRICS)
                if weaker or empty:
                    for field in ('usage','response_model','returned_service_tier','provider_response_id',
                                  'returned_speed','returned_inference_geo','returned_usage_service_tier'):
                        rec.pop(field,None)
            if old and 'owner' in data:rec.pop('owner',None)
            data.update({k:v for k,v in rec.items() if v is not None})
            if was_final and rec.get('status') in ('pending','usage_received'):data['status']=old_status
            if old_status in ('abandoned_without_usage','abandoned_with_usage'):
                # Delayed hooks remain events, not proof of the abandoned
                # producer's actual end. They may still improve usage evidence.
                data['status']=old_status;data['ended']=old_ended
            if data.get('status')=='ended_without_usage' and any((data.get('usage') or {}).get(k) is not None for k in METRICS):
                data['status']='ended_with_usage'
            if data.get('status')=='abandoned_without_usage' and any((data.get('usage') or {}).get(k) is not None for k in METRICS):
                data['status']='abandoned_with_usage'
            data.setdefault('started',time.time());data.setdefault('status','pending')
            from .attribution import attach
            from .projects import read_projects
            attach(c,data,read_projects(self.root))
            if 'usage' in data:
                # Final records keep the price snapshot chosen at completion, even
                # if a duplicate notification arrives after a catalog refresh.
                rate=old_cost.get('rate') if (was_final or kind=='request_abandoned') and old_cost else self.latest_rate(c,data)
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
            from .projects import read_projects
            attach(c,data,read_projects(self.root))
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
    def test_window(self,key,*,include_data=False):
        with self.db() as c:row=c.execute('SELECT started,ended,data FROM tests WHERE id=?',(key,)).fetchone()
        if not row:raise ValueError('Unknown test marker.')
        if include_data:return row['started'],row['ended'],json.loads(row['data'])
        return row['started'],row['ended']
    def _request_keys(self,start,end,provider='',session='',agent='',project='',session_scope='exact',subagent='',model='',model_provider='',*,limit):
        """Payload-free prefix from the same immutable snapshot used by read."""
        where,params,end=request_predicate(start,end,provider,session,agent,project,session_scope,subagent,model,model_provider)
        with self.db() as c:
            return [tuple(r) for r in c.execute('SELECT started,id FROM requests WHERE '+where+' ORDER BY started DESC,id DESC LIMIT ?',params+[limit])]

    def group_keys(self, field, scope):
        """Lightweight full-window identities/recency from a copied source, not group payloads."""
        where, params, _ = request_predicate(*scope)
        exprs, condition = group_dimensions(field)
        with self.db() as c:
            return [(tuple(row[i] for i in range(len(exprs))), row['latest_started'])
                    for row in c.execute('SELECT '+','.join(exprs)+',MAX(started) AS latest_started FROM requests WHERE '
                                         +where+condition+' GROUP BY '+','.join(exprs), params)]

    def read(self,start=0,end=None,provider='',offset=0,limit=200,session='',agent='',project='',session_scope='exact',subagent='',model='',model_provider='',*,trend_start=None,_detail_ids=None,_group_keys=None,view=None,group=None,_delta_seed=False,_validate_costs=False,_selected_test=None,list_mode='legacy',compression_kind='',_include_peak_private=False,_skip_peak=False):
        from .projection import selected_fields, manifest
        fields=selected_fields(view,group)
        include=lambda name: fields is None or name in fields
        list_field={'requests':'requests','compressions':'compressions','cache':'applied_rate_groups','models':'groups','overview':{'model':'model_groups','time':'trend','project':'project_groups','session':'session_groups','subagent':'subagent_groups'}.get(group)}.get(view)
        if list_mode!='legacy' and list_field is None:raise ValueError('Unsupported record list.')
        page_limit=20000 if list_mode=='all' else limit
        page_offset=0 if list_mode=='all' else offset
        where,params,end=request_predicate(start,end,provider,session,agent,project,session_scope,subagent,model,model_provider)
        with self.db() as c:
            # Keep totals, applied rates and request rows on one read snapshot.
            c.execute('BEGIN')
            from .incremental import watermark
            revision = watermark(c) if view is not None else None
            legacy_read_view(c,where,params)
            from .session_cache_writes import project as project_session_writes
            project_session_writes(c,where,params,end)
            from .session_cache_writes import materialise_summary
            summary_sql=materialise_summary(c,SUMMARY_SQL)
            total=sql_summary(c,where,params,summary_sql)
            group_page=(page_limit,page_offset) if list_mode=='page' else None
            group_where,group_params=where,params
            if _group_keys is not None:
                exprs, _ = group_dimensions(list_field)
                c.execute('CREATE TEMP TABLE read_group_keys('+','.join('x'+str(i) for i in range(len(exprs)))+')')
                c.executemany('INSERT INTO read_group_keys VALUES('+','.join('?' for _ in exprs)+')',_group_keys)
                group_where=(where+' AND EXISTS (SELECT 1 FROM read_group_keys WHERE '+
                             ' AND '.join('x'+str(i)+' IS '+expr for i,expr in enumerate(exprs))+')')
            list_total=None
            if list_mode!='legacy' and list_field=='requests' and total['attempts']>20000 and list_mode=='all':
                raise ValueError('Full record report exceeds the 20,000-row safety limit; narrow the window.')
            if list_mode!='legacy' and list_field in ('groups','model_groups','applied_rate_groups','project_groups','session_groups','subagent_groups'):
                effective="COALESCE(NULLIF(json_extract(data,'$.response_model'),''),NULLIF(model,''),'unknown')"
                role="COALESCE(json_extract(data,'$.agent_kind'),'unknown')"
                project_key="COALESCE(json_extract(data,'$.project_id'),'unattributed')"
                root_key="COALESCE(NULLIF(json_extract(data,'$.root_session_id'),''),NULLIF(session_id,''),'unattributed')"
                child_key="COALESCE(NULLIF(json_extract(data,'$.subagent_id'),''),NULLIF(session_id,''),'unattributed')"
                tier="COALESCE(NULLIF(json_extract(data,'$.cost.rate.service_tier'),''),NULLIF(json_extract(data,'$.returned_service_tier'),''),NULLIF(json_extract(data,'$.service_tier'),''),'unspecified')"
                keys={'groups':'provider,'+effective+','+role+',task',
                      'model_groups':'provider,'+effective,
                      'applied_rate_groups':'provider,'+effective+','+tier+",canonical_rate(json_extract(data,'$.cost.rate'))",
                      'project_groups':project_key,'session_groups':root_key,'subagent_groups':child_key}
                condition=" AND "+role+"='subagent'" if list_field=='subagent_groups' else ''
                list_total=c.execute('SELECT COUNT(*) FROM (SELECT 1 FROM requests WHERE '+where+condition+
                                     ' GROUP BY '+keys[list_field]+')',params).fetchone()[0]
                if list_mode=='all' and list_total>20000:
                    raise ValueError('Full record report exceeds the 20,000-row safety limit; narrow the window.')
            applied_rates=sql_applied_rate_groups(c,group_where if list_field=='applied_rate_groups' and _group_keys is not None else where,
                group_params if list_field=='applied_rate_groups' and _group_keys is not None else params,summary_sql,
                page=group_page if list_field=='applied_rate_groups' and _group_keys is None else None) if include('applied_rate_groups') else None
            rows=[]
            if include('requests'):
                if _detail_ids is None:
                    detail_where,detail_params=where,params+[page_limit,page_offset]
                    paging=' LIMIT ? OFFSET ?'
                else:
                    # TEMP membership avoids SQLite variable limits at the 2000-row API cap.
                    c.execute('CREATE TEMP TABLE read_detail_ids(id TEXT PRIMARY KEY)')
                    c.executemany('INSERT OR IGNORE INTO read_detail_ids VALUES(?)',((key,) for key in _detail_ids))
                    detail_where=where+' AND id IN (SELECT id FROM read_detail_ids)'
                    detail_params=params
                    paging=''
                rows=[json.loads(r[0]) for r in c.execute('SELECT data FROM requests WHERE '+detail_where+' ORDER BY started DESC,id DESC'+paging,detail_params)]
            read_progression, read_changes = None, {}
            if include('cache_read_progression'):
                from .cache_progression import query_progression
                read_progression, read_changes = query_progression(c,where,params,start,end,{r['id'] for r in rows})
            effective_model="COALESCE(NULLIF(json_extract(data,'$.response_model'),''),NULLIF(model,''),'unknown')"
            def group_source(field):
                selected=field==list_field and _group_keys is not None
                bounded=field==list_field and group_page and not selected
                return ((group_where if selected else where),
                        (group_params if selected else params)+(list(group_page or ()) if bounded else []),
                        ' LIMIT ? OFFSET ?' if bounded else '')
            groups=None
            if include('groups'):
                source, arguments, paging=group_source('groups')
                sql=("SELECT provider,"+effective_model+" AS model,COALESCE(json_extract(data,'$.agent_kind'),'unknown') AS agent_kind,task,"
                     "MAX(started) AS latest_started,"+summary_sql+" FROM requests WHERE "+source+
                     " GROUP BY provider,"+effective_model+",COALESCE(json_extract(data,'$.agent_kind'),'unknown'),task"
                     " ORDER BY latest_started DESC,provider,model,agent_kind,task"+paging)
                groups=[dict(provider=r['provider'],model=r['model'],agent_kind=r['agent_kind'],
                             task=r['task'],latest_started=r['latest_started'],**summary_from_sql(r))
                        for r in c.execute(sql,arguments)]
            provider_groups=[dict(provider=row['provider'],latest_started=row['latest_started'],**summary_from_sql(row)) for row in c.execute('SELECT provider,MAX(started) AS latest_started,'+summary_sql+' FROM requests WHERE '+where+' GROUP BY provider ORDER BY latest_started DESC,provider',params)]
            model_groups=None
            if include('model_groups'):
                source, arguments, paging=group_source('model_groups')
                sql=('SELECT provider,'+effective_model+' AS model,MAX(started) AS latest_started,'+summary_sql+
                     ' FROM requests WHERE '+source+' GROUP BY provider,'+effective_model+
                     ' ORDER BY latest_started DESC,provider,model'+paging)
                model_groups=[dict(provider=r['provider'],model=r['model'],latest_started=r['latest_started'],
                                   **summary_from_sql(r)) for r in c.execute(sql,arguments)]
            trend=sql_trend(c,where,params,start,end,summary_sql,trend_start=trend_start)
            attribution=attribution_groups(c,where,params,summary_sql,fields=fields,
                page_field=list_field if group_page and _group_keys is None else None,page=group_page,
                selected=(list_field,group_where,group_params) if _group_keys is not None else None)
            if not _skip_peak:
                from .observed_peak import apply as apply_observed_peak
                peak_report={'window':{'start':start,'end':end},'subagent_summary':attribution['subagent_summary'],
                             'provider_groups':provider_groups,'model_groups':model_groups,'trend':trend,
                             'project_groups':attribution.get('project_groups'),
                             'session_groups':attribution.get('session_groups')}
                scoped,peak_groups,unmatched,seen=apply_observed_peak(
                    c,peak_report,where,params,
                    scoped_filter=bool(provider or session or agent or project or subagent or model or model_provider))
            delta_seed=None
            precise=None
            if (_delta_seed or _validate_costs) and revision is not None and view=='overview' and group in ('time','model'):
                # Decimal(28) legacy sums can round. Group reassociation is
                # safe only when every partial sum fits in that context.
                from .incremental import safe_cost
                precise=(total['attempts']<=100000 and all(
                    safe_cost(json.loads(r[0])) for r in
                    c.execute('SELECT data FROM requests WHERE '+where,params)))
            if _delta_seed and precise:
                step=trend['seconds']
                # Same projected SQL and read transaction as public totals.
                by_provider={}
                group_count=0
                for r in c.execute('SELECT CAST(started / ? AS INTEGER) * ? AS bucket,provider,'+summary_sql+
                                   ' FROM requests WHERE '+where+' GROUP BY bucket,provider',
                                   [step,step]+params):
                    group_count+=1
                    if group_count>2048 or not isinstance(r['provider'],str):
                        by_provider=None
                        break
                    by_provider.setdefault(int(r['bucket']),{})[r['provider']]=summary_from_sql(r)
                if by_provider is not None:
                    role="COALESCE(json_extract(data,'$.agent_kind'),'unknown')"
                    by_subagent={}
                    for r in c.execute('SELECT CAST(started / ? AS INTEGER) * ? AS bucket,'+summary_sql+
                                       ' FROM requests WHERE '+where+' AND '+role+"='subagent' GROUP BY bucket",
                                       [step,step]+params):
                        by_subagent[int(r['bucket'])]=summary_from_sql(r)
                        if len(by_subagent)>400:
                            by_subagent=None
                            break
                    if by_subagent is not None:
                        delta_seed={'provider':by_provider,'subagent':by_subagent}
                        if group=='model':
                            by_model={}
                            for r in c.execute('SELECT CAST(started / ? AS INTEGER) * ? AS bucket,provider,'+
                                               effective_model+' AS model,'+summary_sql+
                                               ' FROM requests WHERE '+where+' GROUP BY bucket,provider,'+effective_model,
                                               [step,step]+params):
                                group_count+=1
                                if group_count>4096:
                                    delta_seed=None
                                    break
                                by_model.setdefault(int(r['bucket']),{})[(r['provider'],r['model'])]=summary_from_sql(r)
                            if delta_seed is not None:
                                # JSON object keys must be strings for signed tokens.
                                delta_seed['model']={bucket:{json.dumps(key):value for key,value in groups.items()}
                                                     for bucket,groups in by_model.items()}
            if include('price_catalogs'):
                from .pricing import catalog_status
                catalogs=catalog_status(c,provider)
            else: catalogs=None
            for row in rows:
                row['execution_state']=c.execute('SELECT execution_state(?)',(jd(row),)).fetchone()[0]
                if row['id'] in read_changes:row['cache_read_change']=read_changes[row['id']]
            compwhere=where.replace('requests.data','compressions.data').replace(
                "COALESCE(NULLIF(json_extract(data,'$.response_model'),''),NULLIF(model,''),'unknown')",
                "COALESCE(NULLIF(json_extract(data,'$.model'),''),'unknown')")
            compression_count=c.execute('SELECT COUNT(*) FROM compressions WHERE '+compwhere,params).fetchone()[0]
            list_compwhere=compwhere+(" AND json_extract(data,'$.kind')=?" if compression_kind else '')
            list_compparams=params+([compression_kind] if compression_kind else [])
            list_compcount=c.execute('SELECT COUNT(*) FROM compressions WHERE '+list_compwhere,list_compparams).fetchone()[0] if list_field=='compressions' and list_mode!='legacy' else compression_count
            comps=[]
            if include('compressions'):
                comps=[json.loads(r[0]) for r in c.execute('SELECT data FROM compressions WHERE '+list_compwhere+' ORDER BY started DESC,id DESC LIMIT ? OFFSET ?',list_compparams+[page_limit,page_offset])] if list_field=='compressions' and list_mode!='legacy' else [json.loads(r[0]) for r in c.execute('SELECT data FROM compressions WHERE '+compwhere+' ORDER BY started DESC,id DESC LIMIT 1000',params)]
                # Compression bill is derived from its linked requests, never counted twice.
                for comp in comps:
                    aux=[json.loads(r[0]) for r in c.execute('SELECT data FROM requests WHERE compression_id=?',(comp['id'],))]
                    comp['auxiliary']=summary(aux);comp['aux_request_ids']=[r['id'] for r in aux]
            health=[dict(process=r['process'],updated=r['updated'],**json.loads(r['data'])) for r in c.execute('SELECT * FROM health ORDER BY updated DESC')] if include('health') else None
            rates=[dict(id=r['id'],created=r['created'],**json.loads(r['data'])) for r in c.execute('SELECT * FROM rates ORDER BY created DESC,id DESC LIMIT 500')] if include('rates') else None
            tests=[json.loads(r[0]) for r in c.execute('SELECT data FROM tests ORDER BY started DESC,id DESC LIMIT 100')]
            # The caller's point lookup is against this same copied ledger. Keep
            # the ordinary recent prefix intact; expose its factual selection
            # once even if it is older than the bounded list.
            if _selected_test is not None and not any(t['id']==_selected_test['id'] for t in tests):
                tests.append(_selected_test)
            providers=[r[0] for r in c.execute('SELECT DISTINCT provider FROM requests ORDER BY provider')]
            seq=c.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            quota=[dict(ts=r['ts'],**json.loads(r['data'])) for r in c.execute('SELECT ts,data FROM quota WHERE ts>=? AND ts<? ORDER BY ts DESC LIMIT 200',(start,end))] if include('quota_observations') else None
            crossing_end=c.execute('SELECT COUNT(*) FROM requests WHERE '+where+' AND (ended IS NULL OR ended>=?)',params+[end]).fetchone()[0] if include('crossing_end') else None
            crossing=c.execute('SELECT COUNT(*) FROM requests WHERE started<? AND (ended IS NULL OR ended>=?)'+where[len('started>=? AND started<?'):],[start,start]+params[2:]).fetchone()[0] if include('crossing_start') else None
        result={'generated_at':time.time(),'seq':seq,'window':{'start':start,'end':end,'basis':'request/compaction start time; crossing requests are not split'},
          **attribution,'summary':total,'requests':rows,'request_count':total['attempts'],'next_offset':offset+limit if offset+limit<total['attempts'] else None,
          'compressions':comps,'compression_count':compression_count,'compression_truncated':compression_count>1000,
          'groups':groups,'provider_groups':provider_groups,'model_groups':model_groups,'trend':trend,'price_catalogs':catalogs,
          'applied_rate_groups':applied_rates,'cache_read_progression':read_progression,
          'providers':providers,'health':health,'rates':rates,'tests':tests,'quota_observations':quota,'crossing_start':crossing,'crossing_end':crossing_end}
        if _include_peak_private and not _skip_peak:
            result.update(_peak_intervals=scoped,_peak_groups=peak_groups,_peak_unmatched=unmatched,_peak_seen=seen)
        if list_mode!='legacy':
            if list_field=='requests':total_rows=result['request_count']
            elif list_field=='compressions':total_rows=list_compcount
            elif list_field=='trend':total_rows=len(trend['buckets'])
            else:total_rows=list_total if list_total is not None else len(result[list_field])
            if list_mode=='all' and total_rows>20000:
                raise ValueError('Full record report exceeds the 20,000-row safety limit; narrow the window.')
            result['list_count']=total_rows
            result['list_next_offset']=page_offset+page_limit if list_mode=='page' and page_offset+page_limit<total_rows else None
            if list_field=='trend':
                result['list_rows']=list(reversed(trend['buckets']))[page_offset:page_offset+page_limit]
            elif list_field not in ('requests','compressions') and not (list_total is not None and group_page):
                result[list_field]=result[list_field][page_offset:page_offset+page_limit]
        if fields is None:return result
        projected={**{key:result[key] for key in fields},'projection':manifest(fields),
                   **{key:result[key] for key in ('_peak_intervals','_peak_groups','_peak_unmatched','_peak_seen') if key in result}}
        if list_mode!='legacy':
            projected.update(list_count=result['list_count'],list_next_offset=result['list_next_offset'])
            if list_field=='trend':projected['list_rows']=result['list_rows']
        if revision is not None:
            projected['incremental']={'version':1,'revision':revision,'mode':'snapshot'}
        if delta_seed is not None:
            projected['_delta_seed']=delta_seed
        if _validate_costs:
            projected['_delta_cost_safe']=bool(precise)
        return projected

def request_predicate(start,end,provider,session,agent,project,session_scope,subagent,model='',model_provider=''):
    if agent not in ('','primary','subagent','unknown'):raise ValueError('Invalid agent filter.')
    if session_scope not in ('exact','family'):raise ValueError('Invalid session scope.')
    end=time.time() if end is None else end
    if start<0 or end<start: raise ValueError('Invalid time window.')
    where='started>=? AND started<?';params=[start,end]
    if provider: where+=' AND provider=?';params.append(provider)
    if model_provider: where+=' AND provider=?';params.append(model_provider)
    if model:
        where+=" AND COALESCE(NULLIF(json_extract(data,'$.response_model'),''),NULLIF(model,''),'unknown')=?"
        params.append(model)
    if session:
        if session_scope=='family':
            where+=" AND (session_id=? OR EXISTS (SELECT 1 FROM json_each(requests.data,'$.session_lineage') WHERE value=?))";params.extend([session,session])
        else:where+=' AND session_id=?';params.append(session)
    if agent:where+=" AND COALESCE(json_extract(data,'$.agent_kind'),'unknown')=?";params.append(agent)
    if subagent:
        where+=" AND COALESCE(NULLIF(json_extract(data,'$.subagent_id'),''),session_id)=?";params.append(subagent)
    if project:
        where+=" AND COALESCE(json_extract(data,'$.project_id'),'unattributed')=?";params.append(project)
    return where,params,end


def summary(rows):
    out={'sessions':len({r.get('session_id') for r in rows if r.get('session_id')}),'attempts':len(rows),'pending':0,'unresolved':0,'abandoned':0,'missing_usage':0,'partial_breakdown':0,'aggregate_records':0,
         'known':{k:0 for k in METRICS},'missing_fields':{k:0 for k in METRICS},
         'missing_reasons':{k:{r:0 for r in ('awaiting_usage','unresolved_execution','abandoned_execution','unverified_accounting','ended_without_usage','unreported_field')} for k in METRICS},
         'cost_components':{k:Decimal(0) for k in BUCKETS},'cost_missing_fields':{k:0 for k in BUCKETS},'unpriced_requests':0,'priced_requests':0,'supplemental_requests':0,'known_cost_usd':Decimal(0)}
    savings_keys=('cache_read_savings_usd','cache_write_premium_usd','cache_savings_usd')
    out['savings']={k:Decimal(0) for k in savings_keys};out['savings_missing']={k:0 for k in savings_keys}
    from .session_cache_writes import blank_summary, add_summary
    out['session_cache_writes']=blank_summary()
    from .ownership import execution_state
    owner_cache={}
    for r in rows:
        r=effective_record(r)
        state=execution_state(r,owner_cache)
        if state=='unresolved':out['unresolved']+=1
        if state=='abandoned':out['abandoned']+=1
        add_summary(out['session_cache_writes'],r)
        u=r.get('usage') or {}
        if state=='owner_live':out['pending']+=1
        if u.get('total_tokens') is None:out['missing_usage']+=1
        if any(u.get(k) is None for k in BUCKETS):out['partial_breakdown']+=1
        if u.get('request_count',1)>1:out['aggregate_records']+=1
        for k in METRICS:
            if u.get(k) is None:
                out['missing_fields'][k]+=1
                reason=('awaiting_usage' if state=='owner_live' else
                        'unresolved_execution' if state=='unresolved' else
                        'abandoned_execution' if state=='abandoned' else
                        'unverified_accounting' if (u.get('field_provenance') or {}).get(k) in ('unverified_normalized_zero','unverified_cache_decomposition') else
                        'ended_without_usage' if r.get('ended') is not None and all(u.get(m) is None for m in METRICS) else 'unreported_field')
                out['missing_reasons'][k][reason]+=1
            else:out['known'][k]+=u[k]
        cost=r.get('cost') or {}
        if r.get('supplemental_valuation'):out['supplemental_requests']+=1
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
    cols=["COUNT(DISTINCT NULLIF(session_id,'')) AS sessions", "COUNT(*) AS attempts", "COALESCE(SUM(execution_state(data)='owner_live'),0) AS pending",
      "COALESCE(SUM(execution_state(data)='unresolved'),0) AS unresolved",
      "COALESCE(SUM(execution_state(data)='abandoned'),0) AS abandoned",
      "COALESCE(SUM(json_extract(data,'$.usage.total_tokens') IS NULL),0) AS missing_usage",
      "COALESCE(SUM(json_extract(data,'$.usage.request_count')>1),0) AS aggregate_records",
      "COALESCE(SUM("+' OR '.join("json_extract(data,'$.usage."+k+"') IS NULL" for k in BUCKETS)+"),0) AS partial_breakdown",
      "COALESCE(SUM(json_extract(data,'$.cost.complete')=1),0) AS priced_requests",
      "COALESCE(SUM(json_extract(data,'$.supplemental_valuation.basis')='current_published_rate_for_past_usage'),0) AS supplemental_requests",
      "COALESCE(decimal_sum(json_extract(data,'$.cost.known_components_usd')),'0') AS known_cost_usd"]
    cols += ["COALESCE(SUM(json_extract(data,'$.calculated_cache_writes.tokens')),0) AS cw_tokens",
      "COUNT(json_extract(data,'$.calculated_cache_writes.tokens')) AS cw_compared",
      "COALESCE(SUM(json_extract(data,'$.calculated_cache_writes.status')='baseline'),0) AS cw_baselines",
      "COALESCE(SUM(json_extract(data,'$.calculated_cache_writes.tokens') IS NULL),0) AS cw_missing"]
    for k in METRICS:
        cols += ["COALESCE(SUM(json_extract(data,'$.usage."+k+"')),0) AS k_"+k,"COALESCE(SUM(json_extract(data,'$.usage."+k+"') IS NULL),0) AS m_"+k]
        reason=("CASE WHEN execution_state(data)='owner_live' THEN 'awaiting_usage' "
                "WHEN execution_state(data)='unresolved' THEN 'unresolved_execution' "
                "WHEN execution_state(data)='abandoned' THEN 'abandoned_execution' "
                "WHEN json_extract(data,'$.usage.field_provenance."+k+"') IN ('unverified_normalized_zero','unverified_cache_decomposition') THEN 'unverified_accounting' "
                "WHEN ended IS NOT NULL AND COALESCE("+','.join("json_extract(data,'$.usage."+m+"')" for m in METRICS)+") IS NULL THEN 'ended_without_usage' ELSE 'unreported_field' END")
        for name in ('awaiting_usage','unresolved_execution','abandoned_execution','unverified_accounting','ended_without_usage','unreported_field'):
            cols.append("COALESCE(SUM(json_extract(data,'$.usage."+k+"') IS NULL AND ("+reason+")='"+name+"'),0) AS mr_"+k+'_'+name)
    for k in BUCKETS:
        cols += ["COALESCE(decimal_sum(json_extract(data,'$.cost.components."+k+"')),'0') AS c_"+k,"COALESCE(SUM(json_extract(data,'$.cost.components."+k+"') IS NULL),0) AS cm_"+k]
    for k in ('cache_read_savings_usd','cache_write_premium_usd','cache_savings_usd'):
        cols += ["COALESCE(decimal_sum(json_extract(data,'$.cost."+k+"')),'0') AS s_"+k,"COALESCE(SUM(json_extract(data,'$.cost."+k+"') IS NULL),0) AS sm_"+k]
    # Avoid Python/JSON calls for terminal history; inspect only open owners.
    from .session_cache_writes import READ_STATE_SQL
    state=READ_STATE_SQL
    return ','.join(cols).replace('execution_state(data)',state)
SUMMARY_SQL=_exprs()

def summary_from_sql(row):
    out={k:row[k] for k in ('sessions','attempts','pending','unresolved','abandoned','missing_usage','aggregate_records','partial_breakdown','priced_requests','supplemental_requests','known_cost_usd')}
    from .session_cache_writes import blank_summary
    out['session_cache_writes']=dict(blank_summary(),tokens=row['cw_tokens'],compared_requests=row['cw_compared'],baseline_requests=row['cw_baselines'],missing_requests=row['cw_missing'])
    out['unpriced_requests']=out['attempts']-out['priced_requests']
    keys=('cache_read_savings_usd','cache_write_premium_usd','cache_savings_usd')
    out['savings']={k:row['s_'+k] for k in keys};out['savings_missing']={k:row['sm_'+k] for k in keys}
    out['missing_reasons']={k:{name:row['mr_'+k+'_'+name] for name in ('awaiting_usage','unresolved_execution','abandoned_execution','unverified_accounting','ended_without_usage','unreported_field')} for k in METRICS}
    for group,prefix,fields in [('known','k_',METRICS),('missing_fields','m_',METRICS),('cost_components','c_',BUCKETS),('cost_missing_fields','cm_',BUCKETS)]:
        out[group]={k:row[prefix+k] for k in fields}
    out['cache_hit_rate']=out['known']['cache_read_tokens']/out['known']['prompt_tokens'] if out['known']['prompt_tokens'] and not out['missing_fields']['cache_read_tokens'] and not out['missing_fields']['prompt_tokens'] else None
    return out
def sql_summary(c,where,params,summary_sql=SUMMARY_SQL):
    return summary_from_sql(c.execute('SELECT '+summary_sql+' FROM requests WHERE '+where,params).fetchone())


def sql_trend(c,where,params,start,end,summary_sql=SUMMARY_SQL,*,trend_start=None):
    """Aggregate the complete filtered ledger, not the paginated request table.

    UTC calendar buckets clipped to the selected bounds. A bucket with attempts
    but no measured value remains unknown in the UI, not a zero-consumption claim.
    """
    if trend_start is not None:
        start=trend_start
    elif start==0:
        first=c.execute('SELECT MIN(started) FROM requests WHERE '+where,params).fetchone()[0]
        start=first if first is not None else max(0,end-86400)
    # Allow short windows above one hour: rolling start and server end are sampled separately.
    duration=end-start
    step=60 if duration<=1800 else (120 if duration<=7200 else (3600 if duration<=172800 else 86400))
    if (end-start)/step>400:step=86400*max(1,int((end-start)/86400/400)+1)
    lo=int(start//step)*step
    rows={int(row['bucket']):summary_from_sql(row) for row in c.execute(
      'SELECT CAST(started / ? AS INTEGER) * ? AS bucket,'+summary_sql+' FROM requests WHERE '+where+' GROUP BY bucket', [step,step]+params)}
    provider_rows={}
    for row in c.execute('SELECT CAST(started / ? AS INTEGER) * ? AS bucket,provider,'+summary_sql+
                         ' FROM requests WHERE '+where+' GROUP BY bucket,provider', [step,step]+params):
        provider_rows.setdefault(int(row['bucket']), []).append(
            {'provider':row['provider'], **summary_from_sql(row)})
    blank=summary([])
    buckets=[dict(start=max(start,t),end=min(end,t+step),**rows.get(t,blank),
                  provider_buckets=sorted(provider_rows.get(t,[]),key=lambda r:r['provider']))
             for t in range(lo,int(end)+1,step) if t<end]
    return {'unit':'minute' if step==60 else ('2 minutes' if step==120 else ('hour' if step==3600 else ('day' if step==86400 else str(step//86400)+' days'))),'timezone':'UTC','seconds':step,'buckets':buckets}


def canonical_rate(value):
    """Normalise saved JSON identity without collapsing distinct rate revisions."""
    if value is None:return None
    try:decoded=json.loads(value) if isinstance(value,str) else value
    except json.JSONDecodeError:decoded=value
    return json.dumps(decoded,sort_keys=True,separators=(',',':'))

def group_dimensions(field):
    """Exact raw SQL grouping identities, shared by key selection and hydration."""
    model="COALESCE(NULLIF(json_extract(data,'$.response_model'),''),NULLIF(model,''),'unknown')"
    role="COALESCE(json_extract(data,'$.agent_kind'),'unknown')"
    project="COALESCE(json_extract(data,'$.project_id'),'unattributed')"
    root="COALESCE(NULLIF(json_extract(data,'$.root_session_id'),''),NULLIF(session_id,''),'unattributed')"
    child="COALESCE(NULLIF(json_extract(data,'$.subagent_id'),''),NULLIF(session_id,''),'unattributed')"
    tier="COALESCE(NULLIF(json_extract(data,'$.cost.rate.service_tier'),''),NULLIF(json_extract(data,'$.returned_service_tier'),''),NULLIF(json_extract(data,'$.service_tier'),''),'unspecified')"
    dimensions={'groups':('provider',model,role,'task'),'model_groups':('provider',model),
                'applied_rate_groups':('provider',model,tier,"canonical_rate(json_extract(data,'$.cost.rate'))"),
                'project_groups':(project,),'session_groups':(root,),'subagent_groups':(child,)}
    return dimensions[field], (' AND '+role+"='subagent'" if field=='subagent_groups' else '')

def attribution_groups(c,where,params,summary_sql=SUMMARY_SQL,*,fields=None,page_field=None,page=None,selected=None):
    """Complete filtered-set rollups; project only selected groups when opted in."""
    include=lambda name: fields is None or name in fields
    role="COALESCE(json_extract(data,'$.agent_kind'),'unknown')"
    project="COALESCE(json_extract(data,'$.project_id'),'unattributed')"
    root="COALESCE(NULLIF(json_extract(data,'$.root_session_id'),''),NULLIF(session_id,''),'unattributed')"
    child="COALESCE(NULLIF(json_extract(data,'$.subagent_id'),''),NULLIF(session_id,''),'unattributed')"
    subs=sql_summary(c,where+" AND "+role+"='subagent'",params,summary_sql)
    subs['agents']=c.execute('SELECT COUNT(DISTINCT '+child+') FROM requests WHERE '+where+" AND "+role+"='subagent'",params).fetchone()[0]
    out={'subagent_summary':subs}
    if include('agent_groups'):
        out['agent_groups']=[dict(agent_kind=r['key'],latest_started=r['latest_started'],**summary_from_sql(r)) for r in c.execute('SELECT '+role+' AS key,MAX(started) AS latest_started,'+summary_sql+' FROM requests WHERE '+where+' GROUP BY '+role,params)]
    extra=",COALESCE(SUM(CASE WHEN "+role+"='subagent' THEN json_extract(data,'$.usage.total_tokens') ELSE 0 END),0) AS subagent_tokens,COUNT(DISTINCT CASE WHEN "+role+"='subagent' THEN "+child+" END) AS subagents"
    configs=[('project_groups',project,",MAX(json_extract(data,'$.project_label')) AS label,MAX(json_extract(data,'$.project_path')) AS path,MAX(json_extract(data,'$.project_source')) AS basis",''),
      ('session_groups',root,",MAX(json_extract(data,'$.project_label')) AS project_label",''),
      ('subagent_groups',child,",MIN(session_id) AS session_id,MAX(json_extract(data,'$.parent_session_id')) AS parent_session_id,MAX(json_extract(data,'$.root_session_id')) AS root_session_id,MAX(json_extract(data,'$.agent_role')) AS agent_role,MAX(json_extract(data,'$.project_label')) AS project_label", " AND "+role+"='subagent'")]
    for name,expr,more,condition in configs:
        if not include(name):continue
        bounded=page if page_field==name else None
        source_where,source_params=(selected[1],selected[2]) if selected is not None and name==selected[0] else (where,params)
        rows=c.execute('SELECT '+expr+' AS key'+more+extra+',MAX(started) AS latest_started,'+summary_sql+' FROM requests WHERE '+source_where+condition+' GROUP BY '+expr+' ORDER BY latest_started DESC,key'+(' LIMIT ? OFFSET ?' if bounded else ''),source_params+list(bounded) if bounded else source_params)
        out[name]=[dict(**{k:r[k] for k in r.keys() if k in ('key','label','path','basis','project_label','session_id','parent_session_id','root_session_id','agent_role','subagent_tokens','subagents','latest_started')},**summary_from_sql(r)) for r in rows]
    out['project_options']=[dict(id=r[0],label=r[1] or 'Unattributed project',path=r[2],basis=r[3]) for r in c.execute('SELECT '+project+",MAX(json_extract(data,'$.project_label')),MAX(json_extract(data,'$.project_path')),MAX(json_extract(data,'$.project_source')) FROM requests GROUP BY "+project)]
    return out


def sql_applied_rate_groups(c,where,params,summary_sql=SUMMARY_SQL,*,page=None):
    """Actual saved rate snapshots used by the complete filtered request set.

    Never read the current catalogue or infer prices from the model name here.
    Revisions, tiers and providers stay separate; unpriced/pending attempts remain
    visible with a null rate. The aggregates are independent of request pagination.
    All rate provenance is preserved in the response for inspectable source details.
    """
    model="COALESCE(NULLIF(json_extract(data,'$.response_model'),''),NULLIF(model,''),'unknown')"
    tier="COALESCE(NULLIF(json_extract(data,'$.cost.rate.service_tier'),''),NULLIF(json_extract(data,'$.returned_service_tier'),''),NULLIF(json_extract(data,'$.service_tier'),''),'unspecified')"
    rate="canonical_rate(json_extract(data,'$.cost.rate'))"
    sql=('SELECT provider,'+model+' AS actual_model,'+tier+' AS applied_tier,'+rate+' AS saved_rate,MAX(started) AS latest_started,'
         +summary_sql+' FROM requests WHERE '+where+' GROUP BY provider,'+model+','+tier+','+rate
         +' ORDER BY MAX(started) DESC,provider,actual_model,applied_tier,saved_rate')
    result=[]
    for row in c.execute(sql+(' LIMIT ? OFFSET ?' if page else ''),params+list(page) if page else params):
        result.append(dict(provider=row['provider'],model=row['actual_model'],
            service_tier=row['applied_tier'],rate=json.loads(row['saved_rate']) if row['saved_rate'] else None,
            latest_started=row['latest_started'],
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
    rows=c.execute('SELECT id,data FROM main.requests WHERE ('+where+') AND '+condition,params)
    first=rows.fetchone()
    if first is None:return
    c.execute('CREATE TEMP TABLE usage_legacy_projection(id TEXT PRIMARY KEY,data TEXT NOT NULL)')
    from itertools import chain
    c.executemany('INSERT INTO usage_legacy_projection VALUES(?,?)',((r['id'],jd(effective_record(json.loads(r['data'])))) for r in chain((first,),rows)))
    c.execute("""CREATE TEMP VIEW requests AS
      SELECT r.id,r.started,r.ended,r.provider,r.model,r.session_id,r.task,r.compression_id,r.status,
      COALESCE(p.data,r.data) AS data FROM main.requests r
      LEFT JOIN usage_legacy_projection p ON p.id=r.id""")
