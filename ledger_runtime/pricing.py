"""Public provider price catalog; network work is separate from the request observer.

Exact model matching, Decimal prices in USD/million, append-only catalog snapshots.
No auth, OAuth cookies, user prompts, model names or usage sent to pricing sources.
Unknown aliases and unrecognised table formats fail closed to the last good snapshot.
"""
from __future__ import annotations
import copy, hashlib, json, os, re, threading, time
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from urllib.parse import urlsplit
from .accounting import BUCKETS

VERSION='provider-catalog-v1'
REFRESH_SECONDS=6*3600
RETRY_SECONDS=15*60
MAX_BYTES=16*1024*1024
SOURCES={
 'openai':{'label':'OpenAI','url':'https://developers.openai.com/api/docs/pricing','format':'openai'},
 'openrouter':{'label':'OpenRouter','url':'https://openrouter.ai/api/v1/models','format':'catalog'},
 'nous':{'label':'Nous Portal','url':'https://inference-api.nousresearch.com/v1/models','format':'catalog'},
 'ollama':{'label':'Ollama Cloud','url':'https://ollama.com/pricing','format':'ollama'},
}
PROVIDERS={'openai-codex':'openai','openai-api':'openai','openai':'openai',
           'openrouter':'openrouter','nous':'nous','ollama-cloud':'ollama','ollama':'ollama'}
# Thresholds are model-specific, not inferred from a suffix or context window setting.
# Primary sources: OpenAI model pages for these four models (reviewed 2026-09-20).
LONG_CONTEXT={'gpt-6-astra':272000,'gpt-5.6-sol':272000,
              'gpt-5.6-terra':272000,'gpt-5.6-luna':272000}
_WORKERS={};_LOCK=threading.Lock()

def price(value,per_token=False):
    if value is None or value in ('','-','—','N/A'):return None
    if isinstance(value,bool):raise ValueError('Boolean price')
    v=Decimal(str(value).replace('$','').replace(',','').strip())
    if not v.is_finite() or v<0:raise ValueError('Invalid provider price')
    if per_token:v*=1000000
    if v>1000000:raise ValueError('Price outside text-token parser range')
    return str(v)

def rate(model,tier,values,**extra):
    if not isinstance(model,str) or not re.fullmatch(r'[\w.:/@+~\-]{1,240}',model):
        raise ValueError('Invalid model identifier')
    return dict(model=model,service_tier=tier,**dict(zip(BUCKETS,values)),**extra)

class Tables(HTMLParser):
    """Small non-executing table reader. Captures semantic text, not scripts."""
    def __init__(self):
        super().__init__();self.tables=[];self.rows=None;self.row=None;self.cell=None;self.skip=0;self.text=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.skip+=1
        if self.skip:return
        if tag=='table':self.rows=[]
        if tag=='tr' and self.rows is not None:self.row=[]
        if tag in ('th','td') and self.row is not None:self.cell=[]
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.skip=max(0,self.skip-1);return
        if self.skip:return
        if tag in ('th','td') and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()));self.cell=None
        if tag=='tr' and self.row is not None:
            self.rows.append(self.row);self.row=None
        if tag=='table' and self.rows is not None:self.tables.append(self.rows);self.rows=None
        if tag in ('p','h1','h2','h3','div','section'):self.text.append('\n')
    def handle_data(self,data):
        if self.skip:return
        self.text.append(data+' ')
        if self.cell is not None:self.cell.append(data)

def parse_openai(body):
    """Parse the documented Standard/Batch/Flex/Fast flagship table group.

    Reject incomplete/ambiguous groups rather than assign a hidden tab the wrong tier.
    Other model families need their own validated semantics; they are not guessed.
    """
    p=Tables();p.feed(body);tables=p.tables;text=' '.join(p.text)
    if not tables: # Markdown representation, when supplied by the provider.
        tables=[];block=[]
        for line in body.splitlines()+['']:
            if '|' in line:
                cells=[x.strip().strip('`') for x in line.strip().strip('|').split('|')]
                if not all(re.fullmatch(r'[-: ]*',x) for x in cells):block.append(cells)
            elif block:tables.append(block);block=[]
        text=body
    if not re.search(r'Standard\s+Batch\s+Flex\s+Fast\s+mode',text,re.I):
        raise ValueError('OpenAI tier ordering/schema changed; retained last good catalog')
    groups=[]
    for table in tables:
        rows=[r for r in table if len(r)==9 and r[0] in LONG_CONTEXT]
        if rows:groups.append(rows)
    if len(groups)<4:raise ValueError('OpenAI pricing tables incomplete')
    groups=groups[:4]
    models={r[0] for r in groups[0]}
    if len(models)<4 or any({r[0] for r in g}!=models for g in groups):
        raise ValueError('OpenAI service tier tables do not match')
    out=[]
    for tier,rows in zip(('standard','batch','flex','fast'),groups):
        for row in rows:
            v=[price(x) for x in row[1:]]
            for band,index in (('short',0),('long',4)):
                # HTML order: input, cached input, cache writes, output.
                x=v[index:index+4]
                out.append(rate(row[0],tier,[x[0],x[3],x[1],x[2]],context_band=band,
                     threshold_tokens=LONG_CONTEXT[row[0]],threshold_source='https://developers.openai.com/api/docs/models/'+row[0]))
    return out

def parse_catalog(body):
    obj=json.loads(body);rows=obj.get('data') if isinstance(obj,dict) else None
    if not isinstance(rows,list):raise ValueError('Provider model catalog schema unavailable')
    out=[]
    for r in rows:
        if not isinstance(r,dict):continue
        p=r.get('pricing');model=r.get('id')
        if not isinstance(p,dict) or not model:continue
        if 'prompt' not in p and 'completion' not in p:continue
        def one(prices,minimum=0):
            vals=[price(prices.get(k),True) for k in ('prompt','completion','input_cache_read','input_cache_write')]
            return rate(model,'standard',vals,min_prompt_tokens=minimum,
                        cache_write_1h_tokens=price(prices.get('input_cache_write_1h'),True),
                        note='Provider catalog text-token estimate; routed endpoint and non-token charges can differ.')
        base={k:p.get(k) for k in ('prompt','completion','input_cache_read','input_cache_write','input_cache_write_1h')}
        try:
            entries=[one(base)]
            for change in p.get('overrides',[]):
                threshold=change.get('min_prompt_tokens')
                if not isinstance(threshold,int) or isinstance(threshold,bool) or threshold<0:raise ValueError('Invalid context price override')
                entries.append(one({**base,**change},threshold))
        except (ValueError,ArithmeticError,TypeError,AttributeError):
            # Router aliases can publish -1/variable prices. They stay unpriced;
            # one such entry must not hide valid prices for the whole provider.
            # Discard all bands of this model if any band is invalid.
            continue
        out.extend(entries)
    if not out:raise ValueError('Provider did not return any supported price records')
    return out

def parse_ollama(body):
    p=Tables();p.feed(body);out=[]
    for t in p.tables:
        if not t:continue
        header=[x.lower() for x in t[0]]
        if header not in (['model','input','cached input','output'],['model','input','cached input','cache writes','output']):continue
        for row in t[1:]:
            if len(row)!=len(header):raise ValueError('Ollama price table changed')
            v=dict(zip(header,row));out.append(rate(row[0],'standard',
                [price(v.get('input')),price(v.get('output')),price(v.get('cached input')),price(v.get('cache writes'))],
                note='Published cloud text-token rates. No inferred cache-write charge.'))
    if not out:raise ValueError('No Ollama model pricing table found')
    # Do not silently apply regular prices to a newly introduced peak schedule.
    if re.search(r'peak pricing|peak hours|off.peak', ' '.join(p.text),re.I):
        raise ValueError('Time-dependent Ollama pricing needs parser update; retained last good catalog')
    return out

class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        old=urlsplit(req.full_url);new=urlsplit(newurl)
        if new.scheme!='https' or new.hostname!=old.hostname:raise ValueError('Cross-origin price redirect refused')
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def fetch(url):
    # No browser cookies, no credentials, no environment proxy credentials.
    if url not in {s['url'] for s in SOURCES.values()}:raise ValueError('Unapproved public source')
    request=Request(url,headers={'User-Agent':'Hermes-AI-Usage-Ledger/2.0','Accept':'application/json,text/html,text/markdown'})
    with build_opener(ProxyHandler({}),SafeRedirect()).open(request,timeout=12) as response:
        payload=response.read(MAX_BYTES+1)
        if len(payload)>MAX_BYTES:raise ValueError('Price catalog too large')
        return payload.decode('utf-8')

def _snapshot(source,entries,when,kind='live_public_provider'):
    digest=hashlib.sha256(json.dumps(entries,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return dict(source_id=source,source=SOURCES[source]['label'],source_url=SOURCES[source]['url'],
                observed_at=when,parser_version=VERSION,content_sha256=digest,origin=kind,rates=entries)

def seed(store):
    """Reviewed provider data supplies offline first-start rates; never claims a live fetch."""
    path=Path(__file__).with_name('provider_rates_seed.json')
    try:snapshots=json.loads(path.read_text())['snapshots']
    except (OSError,ValueError):return
    with store.db() as c:
        for s in snapshots:
            if c.execute('SELECT 1 FROM provider_catalog WHERE source_id=? LIMIT 1',(s['source_id'],)).fetchone():continue
            c.execute('INSERT INTO provider_catalog(source_id,observed,data) VALUES(?,?,?)',
                      (s['source_id'],s['observed_at'],json.dumps(s)))

def refresh(store,force=False,fetcher=fetch):
    """Runs only in a background thread or explicit tests, never in a request callback."""
    import fcntl
    lockfile=store.folder/'pricing-refresh.lock'
    with open(lockfile,'a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return False
        for sid,source in SOURCES.items():
            now=time.time()
            with store.db() as c:
                old=c.execute('SELECT data FROM pricing_status WHERE source_id=?',(sid,)).fetchone()
            previous=json.loads(old[0]) if old else {}
            due=REFRESH_SECONDS if previous.get('status')=='ok' else RETRY_SECONDS
            if now-previous.get('attempted_at',0)<(60 if force else due):continue
            state={**previous,'source_id':sid,'label':source['label'],'source_url':source['url'],'attempted_at':now}
            try:
                text=fetcher(source['url'])
                entries={'openai':parse_openai,'catalog':parse_catalog,'ollama':parse_ollama}[source['format']](text)
                s=_snapshot(sid,entries,time.time())
                with store.db() as c:
                    c.execute('INSERT INTO provider_catalog(source_id,observed,data) VALUES(?,?,?)',(sid,s['observed_at'],json.dumps(s)))
                state.update(status='ok',last_success=s['observed_at'],rate_count=len(entries),error=None)
            except Exception as exc:
                # Fixed error class only: do not export potentially sensitive error URLs.
                state.update(status='unavailable',error=type(exc).__name__,message='Public pricing refresh failed; last verified catalog retained. No rates guessed.')
            with store.db() as c:c.execute('INSERT OR REPLACE INTO pricing_status VALUES(?,?)',(sid,json.dumps(state)))
        from .storage import notify
        notify(store.folder)
    return True

def start_worker(store,force=False):
    if os.environ.get('HERMES_USAGE_PRICING_OFFLINE')=='1':return
    key=str(store.path.resolve())
    with _LOCK:
        thread=_WORKERS.get(key)
        if force:
            threading.Thread(target=refresh,args=(store,True),daemon=True,name='usage-price-refresh').start();return
        if thread and thread.is_alive():return
        def loop():
            while True:
                try:refresh(store)
                except Exception:pass # stored refresh status still reports each source failure
                time.sleep(RETRY_SECONDS)
        thread=threading.Thread(target=loop,daemon=True,name='usage-provider-prices');_WORKERS[key]=thread;thread.start()

def catalog_status(c,provider=''):
    wanted=PROVIDERS.get(provider) if provider else None
    out=[]
    for sid,definition in SOURCES.items():
        if provider and sid!=wanted:continue
        row=c.execute('SELECT data FROM provider_catalog WHERE source_id=? ORDER BY observed DESC,id DESC LIMIT 1',(sid,)).fetchone()
        state=c.execute('SELECT data FROM pricing_status WHERE source_id=?',(sid,)).fetchone()
        s=json.loads(row[0]) if row else {}
        out.append({**definition,'source_id':sid,**(json.loads(state[0]) if state else {'status':'not_refreshed'}),
             'snapshot_at':s.get('observed_at'),'origin':s.get('origin'),'snapshot_sha256':s.get('content_sha256'),
             'stale':not s or time.time()-s.get('observed_at',0)>REFRESH_SECONDS,'rates':s.get('rates',[])})
    return out

def lookup(c,rec):
    sid=PROVIDERS.get(rec.get('provider'));model=rec.get('response_model') or rec.get('model')
    if not sid or not model:return None
    t=rec.get('returned_service_tier') or rec.get('service_tier') or 'unspecified'
    tier={'priority':'fast','default':'standard','unspecified':'standard','auto':'standard','':'standard'}.get(t,t)
    # Catalogs from routed providers are standard list prices; no multiplier guessed.
    if sid!='openai' and tier!='standard':return None
    stamp=rec.get('ended') or time.time()
    row=c.execute('SELECT id,data FROM provider_catalog WHERE source_id=? AND observed<=? ORDER BY observed DESC,id DESC LIMIT 1',(sid,stamp)).fetchone()
    if not row:return None
    s=json.loads(row['data']);prompt=(rec.get('usage') or {}).get('prompt_tokens')
    candidates=[r for r in s['rates'] if r['model']==model and r['service_tier']==tier]
    if any(r.get('context_band') for r in candidates):
        if prompt is None:return None
        candidates=[r for r in candidates if r['context_band']==('long' if prompt>r['threshold_tokens'] else 'short')]
    elif any(r.get('min_prompt_tokens',0)>0 for r in candidates):
        if prompt is None:return None
        candidates=sorted([r for r in candidates if prompt>=r.get('min_prompt_tokens',0)],key=lambda r:r.get('min_prompt_tokens',0),reverse=True)[:1]
    if len(candidates)!=1:return None
    r=copy.deepcopy(candidates[0])
    return {**r,'provider':rec['provider'],'source':s['source']+' public pricing','source_url':s['source_url'],
       'catalog_revision':row['id'],'observed_at':s['observed_at'],'content_sha256':s['content_sha256'],
       'origin':s['origin'],'stale_at_request':stamp-s['observed_at']>REFRESH_SECONDS,
       'requested_service_tier':t,'tier_assumption':t in ('unspecified','auto',''),
       'basis':'published API-equivalent token estimate; not subscription debit',
       'scope':'text tokens; excludes tools, media, tax, regional uplifts and provider-specific charges'}
