"""Public provider price catalog; network work is separate from the request observer.

Exact model matching, Decimal prices in USD/million, append-only catalog snapshots.
No auth, OAuth cookies, user prompts, model names or usage sent to pricing sources.
Unrecognised table formats fail closed to the last good snapshot. Selection is
fail-open and labelled: an unknown usage enum selects the provider's documented
default (flagged), and a provider with no catalogue is estimated from OpenRouter
list prices by exact model id (flagged ``list_price_fallback``). Every request
that still has no rate carries one reason from ``UNPRICED_REASONS``.
"""
from __future__ import annotations
import copy, hashlib, json, os, re, threading, time
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from urllib.parse import urljoin, urlsplit
from .accounting import BUCKETS

VERSION='provider-catalog-v2'
RETRY_SECONDS=15*60
MAX_BYTES=16*1024*1024
SOURCES={
 'openai':{'label':'OpenAI','url':'https://developers.openai.com/api/docs/pricing','format':'openai'},
 'openrouter':{'label':'OpenRouter','url':'https://openrouter.ai/api/v1/models','format':'catalog'},
 'nous':{'label':'Nous Portal','url':'https://inference-api.nousresearch.com/v1/models','format':'catalog'},
 'ollama':{'label':'Ollama Cloud','url':'https://ollama.com/pricing','format':'ollama'},
 # Anthropic's official Markdown representation of its public pricing page.
 'anthropic':{'label':'Anthropic','url':'https://platform.claude.com/docs/en/about-claude/pricing.md','format':'anthropic'},
}
# Serving-provider identity only. Direct first-party Claude API ('anthropic')
# never lends its rates to routers, Bedrock, Vertex, Foundry or custom hosts.
PROVIDERS={'openai-codex':'openai','openai-api':'openai','openai':'openai',
           'openrouter':'openrouter','nous':'nous','ollama-cloud':'ollama','ollama':'ollama',
           'anthropic':'anthropic'}
# The inverse rule: a provider with no catalogue here (and a direct Anthropic
# row whose endpoint is custom/unverified) is estimated from OpenRouter list
# prices by exact model id, always labelled pricing_basis='list_price_fallback'.
# Direct catalogues always win; a PROVIDERS row that misses stays unpriced.
FALLBACK_SOURCE='openrouter'
LIST_PRICE_BASIS='OpenRouter list price for this model id; serving provider has no catalogue here, actual charge may differ'
# Serving-provider id -> the OpenRouter vendor prefix of the models it serves
# as first party. Hosts serving many vendors (Copilot, Bedrock, Vertex, Azure,
# Groq, Fireworks, custom, LM Studio...) have no row: the model id decides.
# Mirrored in PRICING_SOURCES.md; exact lookups only, no fuzzy matching.
FALLBACK_PROVIDER_VENDORS={
 'anthropic':'anthropic',
 'gemini':'google','google':'google','google-gemini':'google',
 'xai':'x-ai','xai-oauth':'x-ai','x-ai':'x-ai','grok':'x-ai',
 'deepseek':'deepseek',
 'zai':'z-ai','z-ai':'z-ai','glm':'z-ai',
 'kimi-coding':'moonshotai','kimi-coding-cn':'moonshotai','moonshot':'moonshotai','moonshotai':'moonshotai','kimi':'moonshotai',
 'minimax':'minimax','minimax-cn':'minimax','minimax-oauth':'minimax',
 'alibaba':'qwen','alibaba-coding-plan':'qwen','qwen-oauth':'qwen','dashscope':'qwen','qwen':'qwen',
 'mistral':'mistralai','mistralai':'mistralai',
 'xiaomi':'xiaomi','stepfun':'stepfun','arcee':'arcee-ai','upstage':'upstage',
 'cohere':'cohere','perplexity':'perplexity',
}
# Model-id family prefix -> OpenRouter vendor prefix (anchored regex on the
# lower-cased bare id). Used for every provider, including multi-vendor hosts.
FALLBACK_MODEL_VENDORS=(
 (r'claude-','anthropic'),(r'(?:gpt-|chatgpt-|codex-|o[1-9](?:-|$))','openai'),
 (r'(?:gemini-|gemma-)','google'),(r'grok-','x-ai'),(r'deepseek-','deepseek'),
 (r'glm-','z-ai'),(r'kimi-','moonshotai'),(r'minimax-','minimax'),(r'qwen','qwen'),
 (r'(?:mistral-|devstral-|codestral-|ministral-|magistral-|pixtral-)','mistralai'),
 (r'llama-','meta-llama'),(r'command-','cohere'),(r'nova-','amazon'),(r'sonar','perplexity'),
 (r'mimo-','xiaomi'),(r'step-','stepfun'),(r'hermes-','nousresearch'),
)
UNPRICED_REASONS=('no_catalogue','model_missing','ambiguous','usage_missing','endpoint_unpriced')
UNKNOWN_AS_DEFAULT='unknown_value_as_default'
# OpenAI publishes all tier rows in its pricing.md representation. The pricing
# page's short/long column tooltips publish the boundary, not the model name.
OPENAI_TABLE_URL=SOURCES['openai']['url']+'.md'
_WORKERS={};_LOCK=threading.Lock()

def utc_day(stamp):
    return time.strftime('%Y-%m-%d',time.gmtime(stamp))

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

_OPENAI_COLUMNS=['Model']+[f'{band} context {field}' for band in ('Short','Long')
                  for field in ('input','cached input','cache writes','output')]


def _openai_boundary(page):
    """Read the pricing page's *published* column tooltips, not model capacity."""
    from html import unescape
    text=unescape(page)
    boundaries=[]
    for label,comparison in (('Short','≤'),('Long','>')):
        matches=re.findall(r'"label":\[0,"'+label+r' context"\],"tooltip":\[0,"'+comparison+r'([\d,]+)K input tokens"',text)
        if not matches or len(set(matches))!=1:raise ValueError('OpenAI context boundary unavailable')
        boundaries.append(int(matches[0].replace(',',''))*1000)
    if boundaries[0]!=boundaries[1] or boundaries[0]<=0:
        raise ValueError('OpenAI context boundary inconsistent')
    return boundaries[0]


def parse_openai(body,metadata=None):
    """Parse provider pricing.md flagship tiers and same-provider pricing-page boundary.

    Only exact published IDs and explicitly priced bands are selected. Other
    modalities and sections have different units/semantics and are excluded.
    """
    if metadata is None:raise ValueError('OpenAI pricing boundary evidence unavailable')
    boundary=_openai_boundary(metadata)
    section=re.search(r'(?m)^Flagship models\s*$(.*?)(?=^Cyber models\s*$)',body,re.S|re.M)
    if not section or not re.search(r'Prices per 1M tokens\.',section.group(1)):
        raise ValueError('OpenAI flagship pricing schema unavailable')
    blocks=re.findall(r'(?m)^### (Standard|Batch|Flex|Fast) pricing data\s*$(.*?)(?=^### |^Cyber models\s*$|\Z)',section.group(1),re.S|re.M)
    if [name.lower() for name,_ in blocks]!=['standard','batch','flex','fast']:
        raise ValueError('OpenAI service tier tables incomplete or reordered')
    out=[];seen=set()
    for tier,block in blocks:
        lines=[line.strip() for line in block.splitlines() if line.strip().startswith('|')]
        if len(lines)<3 or [x.strip() for x in lines[0].strip('|').split('|')]!=_OPENAI_COLUMNS:
            raise ValueError('OpenAI price column schema changed')
        if [x.strip() for x in lines[1].strip('|').split('|')]!=['---']*9:
            raise ValueError('OpenAI price table delimiter changed')
        for line in lines[2:]:
            fields=[x.strip() for x in line.strip('|').split('|')]
            if len(fields)!=9:raise ValueError('OpenAI price table row changed')
            model=fields[0]
            # A display annotation is not an exact routable ID. Never silently
            # strip it and accidentally price a distinct alias or modality.
            if not re.fullmatch(r'[\w.:/@+~\-]{1,240}',model):continue
            if (tier,model) in seen:raise ValueError('Duplicate OpenAI model price')
            seen.add((tier,model))
            values=[price(x) for x in fields[1:]]
            if values[0] is None or values[3] is None:continue
            if all(x is None for x in values[4:]):
                # These columns are explicitly the short-context band. An
                # absent long price does not authorise this rate above the
                # published boundary (including for Batch/Flex/Fast).
                out.append(rate(model,tier.lower(),[values[0],values[3],values[1],values[2]],
                     context_band='short',threshold_tokens=boundary,
                     threshold_source=SOURCES['openai']['url']))
            elif values[4] is not None and values[7] is not None:
                for band,index in (('short',0),('long',4)):
                    v=values[index:index+4]
                    out.append(rate(model,tier.lower(),[v[0],v[3],v[1],v[2]],context_band=band,
                         threshold_tokens=boundary,threshold_source=SOURCES['openai']['url']))
            else:raise ValueError('Partial OpenAI long-context band')
    if not out:raise ValueError('OpenAI price table has no supported models')
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

# --- Anthropic (direct Claude API) -------------------------------------------
# Prices come only from the public pricing.md; exact model IDs come only from
# the public per-model overview pages (discovered from models/overview.md).
# Display names are never slugified into IDs. Any schema drift fails closed.
ANTHROPIC_OVERVIEW_URL='https://platform.claude.com/docs/en/models/overview.md'
_ANTHROPIC_PAGE=re.compile(r'https://platform\.claude\.com/docs/en/models/([a-z0-9]+(?:-[a-z0-9]+)*)/overview\.md')
_ANTHROPIC_PAGE_LINK=re.compile(r'https://platform\.claude\.com/docs/en/models/([a-z0-9]+(?:-[a-z0-9]+)*)/overview\b(?!\.)')
_ANTHROPIC_ID=re.compile(r'claude-[a-z0-9]+(?:-[a-z0-9]+)*')
_ANTHROPIC_NAME=re.compile(r'Claude ([A-Z][a-z]+) (\d+)(?:\.(\d+))?')
_ANTHROPIC_MONEY=re.compile(r'\$(\d+(?:\.\d+)?)(?: USD)? / MTok(?:<sup>(\d+)</sup>)?')
_ANTHROPIC_COLUMNS=['Model','Base input tokens','5m cache writes','1h cache writes','Cache hits and refreshes','Output tokens']
ANTHROPIC_MAX_PAGES=40

def _md_section(text,heading,level):
    """Body under one exact Markdown heading, ending at a same-or-higher heading."""
    found=re.search(r'(?m)^'+'#'*level+' '+re.escape(heading)+r'[ \t]*$',text)
    if not found:raise ValueError('Anthropic section unavailable: '+heading)
    rest=text[found.end():]
    end=re.search(r'(?m)^#{1,'+str(level)+r'} ',rest)
    return rest[:end.start()] if end else rest

def _md_tables(section):
    tables=[];current=[]
    for line in section.splitlines()+['']:
        s=line.strip()
        if s.startswith('|') and s.endswith('|') and len(s)>1:
            current.append([c.strip() for c in s[1:-1].split('|')])
        elif current:
            tables.append(current);current=[]
    out=[]
    for t in tables:
        if (len(t)<2 or any(len(r)!=len(t[0]) for r in t)
                or not all(re.fullmatch(r':?-{3,}:?',c) for c in t[1])):
            raise ValueError('Anthropic table shape changed')
        out.append((t[0],t[2:]))
    return out

def _version(name):
    m=_ANTHROPIC_NAME.fullmatch(name)
    if not m:raise ValueError('Unrecognised Anthropic model name')
    return (int(m.group(2)),int(m.group(3) or 0))

def _decimal(text):
    v=price(text)
    if v is None:raise ValueError('Anthropic price missing')
    return Decimal(v)

def _multiplier(section,label):
    for header,rows in _md_tables(section):
        if header[:2]!=['Cache operation','Multiplier']:continue
        for row in rows:
            if row[0]==label:
                m=re.match(r'(\d+(?:\.\d+)?)x base input price',row[1])
                if m:return Decimal(m.group(1))
    raise ValueError('Anthropic prompt-caching multiplier unavailable: '+label)

def _anthropic_names(text):
    """'A, B and C' -> [A,B,C]; every item must be a strict Claude display name."""
    names=[x.strip() for x in re.split(r',\s*(?:and\s+)?|\s+and\s+',text) if x.strip()]
    if not names or not all(_ANTHROPIC_NAME.fullmatch(x) for x in names):
        raise ValueError('Anthropic footnote model list changed')
    return names

def parse_anthropic_model_page(text):
    """Exact Claude API ID (+documented alias) for one public model page."""
    title=re.search(r'(?m)^title: (.+?)\s*$',text)
    name=title.group(1).strip('"') if title else ''
    if not _ANTHROPIC_NAME.fullmatch(name):raise ValueError('Anthropic model page title changed')
    declared=re.search(r'(?m)^Model ID: `([^`]+)`\s*$',text)
    ids={}
    for header,rows in _md_tables(_md_section(text,'Model IDs',3)):
        if header!=['Platform','Model ID']:raise ValueError('Anthropic model ID table changed')
        for row in rows:
            m=re.fullmatch(r'`([^`]+)`',row[1])
            if row[0] in ('Claude API','Claude API alias'):
                if not m or not _ANTHROPIC_ID.fullmatch(m.group(1)):raise ValueError('Anthropic model ID changed')
                ids[row[0]]=m.group(1)
    api=ids.get('Claude API')
    if not api or not declared or declared.group(1)!=api:raise ValueError('Anthropic Claude API ID unavailable')
    alias=ids.get('Claude API alias')
    if alias is not None and alias==api:alias=None
    window=re.search(r'(?m)^Context window: (\d+)(K|M) tokens\b',text)
    if not window:raise ValueError('Anthropic context window unavailable')
    return {'name':name,'id':api,'alias':alias,
            'context_window_tokens':int(window.group(1))*(1000 if window.group(2)=='K' else 1000000)}

def anthropic_page_links(text):
    return sorted(set(_ANTHROPIC_PAGE_LINK.findall(text)))

def _anthropic_overview_ids(text):
    """Cross-check table from models/overview.md: display name -> (API ID, alias)."""
    for header,rows in _md_tables(_md_section(text,'Compare models',2)):
        if header[0]!='Feature':continue
        table={r[0]:r[1:] for r in rows}
        pages,api,alias=table.get('Model page'),table.get('Claude API ID'),table.get('Claude API alias')
        if not pages or not api or not alias:raise ValueError('Anthropic model overview table changed')
        out={}
        for page,i,a in zip(pages,api,alias):
            n=re.fullmatch(r'\[(Claude [^\]]+)\]\([^)]+\)',page)
            ii=re.fullmatch(r'`([^`]+)`',i);aa=re.fullmatch(r'`([^`]+)`',a)
            if not n or not ii or not aa:raise ValueError('Anthropic model overview row changed')
            out[n.group(1)]=(ii.group(1),aa.group(1))
        return out
    raise ValueError('Anthropic model overview table unavailable')

def parse_anthropic(body,overview,pages):
    """Direct Claude API text-token rates: standard and fast speed, global and US geo.

    ``pages`` are public model overview Markdown pages. A price row is only
    emitted for a display name with an exact documented ID; retired rows and
    rows without a model page stay unpriced. Batch, Priority Tier, cloud
    platforms, tools and session runtime are outside this catalogue.
    """
    if not re.search(r'(?m)^This page provides detailed pricing information for Anthropic\'s models and features\. All prices are in USD\.\s*$',body):
        raise ValueError('Anthropic currency statement unavailable')
    model_section=_md_section(body,'Model pricing',2)
    tables=_md_tables(model_section)
    if len(tables)!=1 or tables[0][0]!=_ANTHROPIC_COLUMNS:raise ValueError('Anthropic price column schema changed')
    notes=dict(re.findall(r'<sup>(\d+) ([^<]+)</sup>',model_section))
    default=re.search(r'All other models use the standard (\d+(?:\.\d+)?)x multiplier',model_section)
    if not default:raise ValueError('Anthropic cache-hit multiplier unavailable')
    caching=_md_section(body,'Prompt caching',3)
    write5=_multiplier(caching,'5-minute cache write');write1h=_multiplier(caching,'1-hour cache write')
    standard={};retired=[];limited=set()
    for row in tables[0][1]:
        m=re.fullmatch(r'(Claude [A-Z][a-z]+ \d+(?:\.\d+)?)(?: \(\[([^\]]+)\]\(https://[^)\s]+\)\))?',row[0])
        if not m:raise ValueError('Anthropic model row changed')
        name,annotation=m.group(1),(m.group(2) or '').lower()
        if annotation.startswith('retired'):retired.append(name);continue
        if annotation=='limited availability':limited.add(name)
        elif annotation:raise ValueError('Unrecognised Anthropic model availability annotation')
        if name in standard:raise ValueError('Duplicate Anthropic model price')
        cells=[_ANTHROPIC_MONEY.fullmatch(c) for c in row[1:]]
        if not all(cells):raise ValueError('Anthropic price cell changed')
        base,w5,w1,hit,out=(_decimal(c.group(1)) for c in cells)
        refs=[c.group(2) for c in cells]
        for position,ref in enumerate(refs):
            if ref is None:continue
            text=notes.get(ref)
            if text is None:raise ValueError('Anthropic footnote unavailable')
            if position==3:
                hm=re.fullmatch(r'Cache hits and refreshes on (.+?) (?:is|are) priced at (\d+(?:\.\d+)?)x the base input price\.',text)
                if not hm or name not in _anthropic_names(hm.group(1)) or hit!=base*Decimal(hm.group(2)):
                    raise ValueError('Anthropic cache-hit footnote inconsistent')
            elif position in (0,4):
                # Time-limited/introductory prices are accepted only once the
                # provider states they are now the standard price.
                if name not in text or 'is now the standard price' not in text:
                    raise ValueError('Time-limited Anthropic price needs review')
            else:raise ValueError('Unexpected Anthropic cache-write footnote')
        if refs[3] is None and hit!=base*Decimal(default.group(1)):raise ValueError('Anthropic cache-hit price inconsistent')
        if w5!=base*write5 or w1!=base*write1h:raise ValueError('Anthropic cache-write price inconsistent')
        standard[name]=dict(input=base,output=out,read=hit,write=w5,write_1h=w1)
    if not standard:raise ValueError('Anthropic price table has no supported models')
    fast_section=_md_section(body,'Fast mode pricing',3)
    if 'Prompt caching multipliers' not in fast_section or 'apply on top of fast mode pricing' not in fast_section:
        raise ValueError('Anthropic fast-mode caching statement changed')
    fast_tables=_md_tables(fast_section)
    if len(fast_tables)!=1 or fast_tables[0][0]!=['Model','Input','Output']:raise ValueError('Anthropic fast-mode schema changed')
    fast={}
    for row in fast_tables[0][1]:
        cells=[_ANTHROPIC_MONEY.fullmatch(c) for c in row[1:]]
        if not all(cells) or any(c.group(2) for c in cells):raise ValueError('Anthropic fast-mode price cell changed')
        for name in [x.strip() for x in row[0].split(' / ')]:
            if name not in standard or name in fast:raise ValueError('Anthropic fast-mode model unmatched')
            fast[name]=(_decimal(cells[0].group(1)),_decimal(cells[1].group(1)))
    residency=_md_section(body,'Data residency pricing',3)
    geo=re.search(r'For Claude (\d+)\.(\d+) and later models, specifying US-only inference through the `inference_geo` parameter incurs a (\d+(?:\.\d+)?)x multiplier on all token pricing categories',residency)
    if not geo or 'Global routing (the default) uses standard pricing' not in residency:
        raise ValueError('Anthropic data-residency pricing changed')
    geo_from,geo_mult=(int(geo.group(1)),int(geo.group(2))),Decimal(geo.group(3))
    long=re.search(r'Claude (\d+)\.(\d+) and later models .*?include the full \[1M token context window\]\([^)]*\) at standard pricing',
                   _md_section(body,'Long context pricing',3))
    flat_from=(int(long.group(1)),int(long.group(2))) if long else None
    by_name={}
    for page in pages:
        parsed=parse_anthropic_model_page(page)
        if parsed['name'] in by_name and by_name[parsed['name']]!=parsed:raise ValueError('Conflicting Anthropic model pages')
        by_name[parsed['name']]=parsed
    for name,(api,alias) in _anthropic_overview_ids(overview).items():
        page=by_name.get(name)
        if not page or page['id']!=api or (page['alias'] or page['id'])!=alias:
            raise ValueError('Anthropic model overview disagrees with model page')
    out=[]
    for name,std in standard.items():
        page=by_name.get(name)
        if not page:continue  # published price without a documented exact ID
        version=_version(name)
        flat=bool(flat_from and version>=flat_from)
        speeds:list=[('standard',std,None)]
        if name in fast:
            f_in,f_out=fast[name]
            ratio=std['read']/std['input']
            speeds.append(('fast',dict(input=f_in,output=f_out,read=f_in*ratio,write=f_in*write5,write_1h=f_in*write1h),
                           'Fast-mode input/output published; cache prices apply the published prompt-caching multipliers.'))
        geos=[('global',Decimal(1))]+([('us',geo_mult)] if version>=geo_from else [])
        for model_id in [page['id']]+([page['alias']] if page['alias'] else []):
            for tier,values,derivation in speeds:
                for region,mult in geos:
                    v={k:str(x*mult) for k,x in values.items()}
                    notes_=[n for n in (derivation,('US-only inference: published '+str(geo_mult)+'x data-residency multiplier applied to every token category.') if region=='us' else None) if n]
                    extra=dict(cache_write_1h_tokens=v['write_1h'],inference_geo=region,display_name=name,
                               api_model_id=page['id'],max_prompt_tokens=page['context_window_tokens'],
                               long_context_flat=flat,cache_ttl_breakdown_required=True,
                               availability='limited' if name in limited else 'general')
                    if model_id!=page['id']:extra['alias_of']=page['id']
                    if notes_:extra['note']=' '.join(notes_)
                    out.append(rate(model_id,tier,[v['input'],v['output'],v['read'],v['write']],**extra))
    if not out:raise ValueError('Anthropic catalogue has no documented model IDs')
    return out

def anthropic_catalog(fetcher):
    """Fetch pricing plus a bounded, same-host model-page crawl; all-or-nothing."""
    body=fetcher(SOURCES['anthropic']['url'])
    overview=fetcher(ANTHROPIC_OVERVIEW_URL)
    pending=anthropic_page_links(overview);seen=set();pages=[]
    while pending:
        slug=pending.pop(0)
        if slug in seen:continue
        seen.add(slug)
        if len(seen)>ANTHROPIC_MAX_PAGES:raise ValueError('Anthropic model page discovery exceeded bound')
        text=fetcher('https://platform.claude.com/docs/en/models/'+slug+'/overview.md')
        pages.append(text)
        pending.extend(x for x in anthropic_page_links(text) if x not in seen)
    return parse_anthropic(body,overview,pages)

def anthropic_model_id(model):
    """Hermes' native wire normalisation for direct Claude requests, and no more.

    Mirrors hermes-agent ``normalize_model_name``: strip a leading
    ``anthropic/`` (any case) and turn version dots into hyphens for
    ``claude-`` names. Bedrock/regional IDs are left untouched (and so unpriced).
    """
    if not isinstance(model,str):return None
    if model.lower().startswith('anthropic/'):model=model[len('anthropic/'):]
    if model.lower().startswith('claude-'):model=model.replace('.','-')
    return model

def anthropic_endpoint(base_url):
    """Content-free endpoint class for direct Anthropic pricing eligibility.

    Exact host only. Hermes canonicalises hostnames with ``lower().rstrip('.')``
    (``utils._hostname_of``, ``auxiliary_client._is_anthropic_compatible_host``);
    the single trailing-dot absolute-DNS form ``api.anthropic.com.`` is the
    same first-party host. Only that one dot is removed: ``..`` is not a valid
    DNS name and stays ``custom`` (fail-closed). Nothing else is loosened:
    subdomains, look-alikes, ``/anthropic`` gateway paths and scheme-less
    strings remain ``custom`` (unpriced).
    """
    if not isinstance(base_url,str) or not base_url.strip():return None
    try:host=(urlsplit(base_url.strip()).hostname or '').lower()
    except ValueError:return 'custom'
    if host.endswith('.'):host=host[:-1]
    return 'first_party' if host=='api.anthropic.com' else 'custom'

def endpoint_host_class(base_url):
    """'local' for a loopback/private/link-local/.local host, else None.

    Content-free: only the class is recorded, never the URL or host name.
    """
    import ipaddress
    if not isinstance(base_url,str) or not base_url.strip():return None
    try:host=(urlsplit(base_url.strip()).hostname or '').lower().rstrip('.')
    except ValueError:return None
    if not host:return None
    if host=='localhost' or host.endswith(('.localhost','.local')) or host=='host.docker.internal':return 'local'
    try:ip=ipaddress.ip_address(host)
    except ValueError:return None
    return 'local' if (ip.is_loopback or ip.is_private or ip.is_link_local) and not ip.is_unspecified else None

# Anthropic's documented usage.speed / usage.inference_geo enumerations.
# 'not_available' is documented (Usage & Cost API) for models that do not
# accept the inference_geo parameter; standard (global) pricing applies. It
# selects the global rate, flagged geo_assumption on models with a published
# US rate (see _select_anthropic). Any other value (a new enum the recorder
# kept verbatim, or its 'unrecognised' sentinel) is fail-open: the documented
# default (global geo, standard speed, standard tier) is selected and flagged.
# Values with a known premium (us 1.1x, fast, priority/batch) stay exact.
_ANTHROPIC_SPEEDS={'fast','standard'}
_ANTHROPIC_GEOS={'global':'global','us':'us','not_available':'global'}
_ANTHROPIC_REQUESTED_STANDARD={'unspecified','','auto','default','standard','standard_only'}
_ANTHROPIC_PREMIUM_TIERS={'priority','batch'}
# Documented-value gap in the recorder allow-list, admitted in 0d5ade4: before
# then Anthropic's documented 'not_available' geo was stored as the
# 'unrecognised' sentinel (raw value not kept). Anthropic documents only
# global/us/not_available, 'us' is returned only when requested and Hermes
# never sends inference_geo, so such a row is estimated at the global rate
# (a workspace default geo cannot be excluded, so it is always flagged). The
# rule is read-time only (retrospective projection), bounded to a returned
# 'standard' service tier and standard speed that was neither returned nor
# requested as fast, and keeps its own geo_basis. Every other unknown geo
# (including this sentinel outside those bounds) takes the generic fail-open
# default below with geo_basis UNKNOWN_AS_DEFAULT.
ANTHROPIC_UNRECOGNISED_GEO_AS_GLOBAL='unrecognised_as_global'
ANTHROPIC_NOT_AVAILABLE_GEO_AS_GLOBAL='not_available_as_global'
_ANTHROPIC_UNRECOGNISED_GEO_TIERS={'standard'}

def _unrecognised_geo_as_global(rec,speed,asked_fast,retrospective):
    return (retrospective and rec.get('provider')=='anthropic'
            and rec.get('returned_inference_geo')=='unrecognised'
            and rec.get('returned_usage_service_tier') in _ANTHROPIC_UNRECOGNISED_GEO_TIERS
            and rec.get('returned_speed') in (None,'standard') and speed=='standard' and not asked_fast
            # The recorder keeps no requested geo today; any recorded one disqualifies.
            and rec.get('requested_inference_geo') is None)

def _select_anthropic(s,rec,revision,stamp,retrospective):
    """(rate, reason) for a direct first-party (or unrecorded) endpoint."""
    endpoint=rec.get('anthropic_endpoint')
    # custom host, or an auxiliary client whose endpoint could not be read
    # ('unverified'): never borrow direct first-party rates (source_for
    # routes such rows to the labelled list-price fallback instead).
    if endpoint not in (None,'first_party'):return None,'no_catalogue'
    model=anthropic_model_id(rec.get('response_model') or rec.get('model'))
    if not model:return None,'model_missing'
    returned_tier=rec.get('returned_usage_service_tier')
    if returned_tier in _ANTHROPIC_PREMIUM_TIERS:return None,'endpoint_unpriced'  # Priority/Batch: unpublished here
    tier_basis=UNKNOWN_AS_DEFAULT if returned_tier not in (None,'standard') else None
    requested=str(rec.get('service_tier') or 'unspecified')
    asked_fast=requested=='fast' or rec.get('requested_speed')=='fast'
    returned=rec.get('returned_speed');speed_basis='usage.speed'
    if returned in _ANTHROPIC_SPEEDS:speed=returned
    elif returned is None:
        if asked_fast:speed,speed_basis='fast','requested fast; completed fast requests report fast'
        elif requested in _ANTHROPIC_REQUESTED_STANDARD:speed,speed_basis='standard','assumed standard speed (no fast request)'
        elif requested in _ANTHROPIC_PREMIUM_TIERS:return None,'endpoint_unpriced'
        else:
            speed,speed_basis='standard','unknown requested tier; documented standard speed assumed'
            tier_basis=UNKNOWN_AS_DEFAULT
    elif asked_fast:speed,speed_basis='fast','unknown usage.speed value; requested fast assumed'
    else:speed,speed_basis='standard','unknown usage.speed value; documented standard speed assumed'
    geo=rec.get('returned_inference_geo')
    geo_basis=None
    if geo is not None and geo not in _ANTHROPIC_GEOS:
        geo_basis=(ANTHROPIC_UNRECOGNISED_GEO_AS_GLOBAL if _unrecognised_geo_as_global(rec,speed,asked_fast,retrospective)
                   else UNKNOWN_AS_DEFAULT)
    rate_geo='global' if geo is None or geo_basis else _ANTHROPIC_GEOS[geo]
    known=[r for r in s['rates'] if r['model']==model]
    if not known:return None,'model_missing'
    candidates=[r for r in known if r['service_tier']==speed and r.get('inference_geo')==rate_geo]
    prompt=(rec.get('usage') or {}).get('prompt_tokens')
    # 4.6+ models publish one flat rate over the whole window. Earlier models
    # publish no long-context rate, so price only a known prompt within the
    # documented window; anything else stays unknown rather than guessed.
    candidates=[r for r in candidates if r.get('long_context_flat') or
                (prompt is not None and r.get('max_prompt_tokens') is not None and prompt<=r['max_prompt_tokens'])]
    if len(candidates)!=1:
        return None,('usage_missing' if prompt is None and any(r['service_tier']==speed and r.get('inference_geo')==rate_geo
                                                                for r in known) else 'endpoint_unpriced')
    r=copy.deepcopy(candidates[0])
    geo_possible=any(x['model']==model and x.get('inference_geo')=='us' for x in s['rates'])
    # 'not_available' is documented for models that do not accept
    # inference_geo (standard pricing, no assumption). On a model with a
    # published US rate it is observed but undocumented: the workspace default
    # geo could be 'us', so global is an assumption and flagged as such.
    if geo=='not_available' and geo_possible:geo_basis=ANTHROPIC_NOT_AVAILABLE_GEO_AS_GLOBAL
    reported=returned in _ANTHROPIC_SPEEDS
    return {**r,'provider':rec['provider'],'source':s['source']+' public pricing','source_url':s['source_url'],
       'catalog_revision':revision,'observed_at':s['observed_at'],'content_sha256':s['content_sha256'],
       'origin':s['origin'],'stale_at_request':not retrospective and utc_day(stamp)!=utc_day(s['observed_at']),
       'retrospective':retrospective,'requested_service_tier':requested,
       'tier_assumption':not reported and not asked_fast,
       # True whenever the provider did not return a known usage.speed: a
       # requested fast speed is an inference, not a confirmed returned speed.
       'speed_inferred':not reported,
       'speed_basis':speed_basis,'geo_assumption':bool(geo_basis) or (geo is None and geo_possible),
       **({'geo_basis':geo_basis} if geo_basis else {}),**({'tier_basis':tier_basis} if tier_basis else {}),
       'endpoint_assumption':endpoint is None,
       'basis':'API-equivalent estimate at Anthropic public list prices; not a subscription debit or provider-reported charge',
       'pricing_basis':'provider_catalog_estimate',
       'scope':'text tokens; excludes Batch/Priority Tier, tools, web search, runtime, tax and negotiated discounts'},None

class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        target=urljoin(req.full_url,newurl)
        old=urlsplit(req.full_url);new=urlsplit(target)
        if (old.scheme!='https' or new.scheme!='https' or not old.hostname or
                (old.hostname,443 if old.port is None else old.port)!=
                (new.hostname,443 if new.port is None else new.port)):
            raise ValueError('Cross-origin price redirect refused')
        return super().redirect_request(req,fp,code,msg,headers,target)

def fetch(url):
    # No browser cookies, no credentials, no environment proxy credentials.
    anthropic_page=bool(_ANTHROPIC_PAGE.fullmatch(url)) if isinstance(url,str) else False
    if url not in {s['url'] for s in SOURCES.values()}|{OPENAI_TABLE_URL,ANTHROPIC_OVERVIEW_URL} and not anthropic_page:
        raise ValueError('Unapproved public source')
    accept='text/html' if url==SOURCES['openai']['url'] else 'text/markdown' if url.endswith('.md') else 'application/json,text/html'
    request=Request(url,headers={'User-Agent':'Hermes-AI-Usage-Ledger/2.0','Accept':accept})
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
            # Success is scoped to a UTC calendar day, not a rolling timer.
            # A failed attempt cannot suppress a new day's refresh; retries
            # within the day are bounded even if the last good catalog is older.
            if not force and previous.get('last_success') and utc_day(previous['last_success'])==utc_day(now):continue
            minimum_interval = 60 if force else RETRY_SECONDS if previous.get('status')!='ok' else 0
            if now-previous.get('attempted_at',0)<minimum_interval:continue
            state={**previous,'source_id':sid,'label':source['label'],'source_url':source['url'],'attempted_at':now}
            try:
                if sid=='anthropic':
                    entries=anthropic_catalog(fetcher)
                else:
                    text=fetcher(source['url'])
                    if sid=='openai':
                        entries=parse_openai(fetcher(OPENAI_TABLE_URL),text)
                    else:
                        entries={'catalog':parse_catalog,'ollama':parse_ollama}[source['format']](text)
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
        entry=_WORKERS.get(key)
        if entry and entry[0].is_alive():
            if force:entry[1].set()
            return
        wake=threading.Event()
        if force:
            wake.set()
        def loop():
            while True:
                manual=wake.is_set();wake.clear()
                try:refresh(store,force=manual)
                except Exception:pass # stored refresh status still reports each source failure
                wake.wait(RETRY_SECONDS)
        thread=threading.Thread(target=loop,daemon=True,name='usage-provider-prices');_WORKERS[key]=(thread,wake);thread.start()

def catalog_status(c,provider=''):
    wanted=source_for({'provider':provider})[0] if provider else None
    out=[]
    for sid,definition in SOURCES.items():
        if provider and sid!=wanted:continue
        row=c.execute('SELECT data FROM provider_catalog WHERE source_id=? ORDER BY observed DESC,id DESC LIMIT 1',(sid,)).fetchone()
        state=c.execute('SELECT data FROM pricing_status WHERE source_id=?',(sid,)).fetchone()
        s=json.loads(row[0]) if row else {}
        out.append({**definition,'source_id':sid,**(json.loads(state[0]) if state else {'status':'not_refreshed'}),
             'snapshot_at':s.get('observed_at'),'origin':s.get('origin'),'snapshot_sha256':s.get('content_sha256'),
             'stale':not s or utc_day(s.get('observed_at',0))!=utc_day(time.time()),'rates':s.get('rates',[])})
    return out

def source_for(rec):
    """(catalogue source id, 'direct'|'fallback') for one request record.

    Direct catalogues always win. Everything else, including a direct
    Anthropic row whose endpoint is custom/unverified, uses the OpenRouter
    list-price fallback. Pure: no I/O.
    """
    provider=rec.get('provider')
    sid=PROVIDERS.get(provider)
    if sid=='anthropic' and rec.get('anthropic_endpoint') not in (None,'first_party'):
        return FALLBACK_SOURCE,'fallback'
    if sid:return sid,'direct'
    # A no-catalogue provider served from a loopback/private host is local
    # inference (Ollama, LM Studio, vLLM, llama.cpp): a known $0.
    if rec.get('endpoint_host')=='local':return None,'local'
    return FALLBACK_SOURCE,'fallback'

def lookup(c,rec):
    sid,mode=source_for(rec);model=rec.get('response_model') or rec.get('model')
    if not model:return None
    stamp=rec.get('ended') or time.time()
    if mode=='local':return explain(None,rec,None,stamp)[0]
    row=c.execute('SELECT id,data FROM provider_catalog WHERE source_id=? AND observed<=? ORDER BY observed DESC,id DESC LIMIT 1',(sid,stamp)).fetchone()
    if not row:return None
    return select_rate(json.loads(row['data']),rec,row['id'],stamp)

def select_rate(s,rec,revision,stamp,*,retrospective=False):
    """Exact serving-provider model/tier/band match against one catalog snapshot.

    Retrospective reads may use the latest observed catalog; they never claim it
    was available at request time. No I/O or network work takes place here.
    """
    return explain(s,rec,revision,stamp,retrospective=retrospective)[0]

def unpriced_reason(rec,snapshot,revision=None,stamp=None,*,retrospective=True):
    """Why a request has no complete price: one of UNPRICED_REASONS, or None.

    ``snapshot`` is the latest catalogue for ``source_for(rec)`` (None if the
    ledger holds none). Reads classify with the same selection code that
    prices, so the reason cannot disagree with the rate that was not chosen.
    """
    if snapshot is None and source_for(rec)[1]!='local':return 'no_catalogue'
    return explain(snapshot,rec,revision,stamp if stamp is not None else rec.get('ended') or 0,
                   retrospective=retrospective)[1] or 'endpoint_unpriced'

def gap_reason(data,snapshot=None):
    """UNPRICED_REASONS member for a request without a complete cost, else None.

    ``snapshot`` is the latest catalogue for ``source_for(data)`` (None when
    the ledger holds none); only consulted for rows that carry no rate.
    """
    cost=data.get('cost') or {}
    if cost.get('complete'):return None
    usage=data.get('usage') or {}
    if (data.get('status') in ('pending','usage_received') or data.get('ended') is None
            or all(usage.get(k) is None for k in BUCKETS)):
        return 'usage_missing'
    applied=cost.get('rate')
    if applied:
        missing=[k for k in BUCKETS if (cost.get('components') or {}).get(k) is None]
        if any(usage.get(k) is None or applied.get(k) is not None for k in missing):return 'usage_missing'
        return 'endpoint_unpriced'
    return unpriced_reason(data,snapshot,None,data.get('ended'))

_OPENAI_TIERS={'standard','fast','flex','batch','scale'}
_TIER_ALIASES={'priority':'fast','default':'standard','unspecified':'standard','auto':'standard','':'standard'}

def explain(s,rec,revision,stamp,*,retrospective=False):
    """(rate, None) or (None, reason). Pure selection against one snapshot."""
    sid,mode=source_for(rec);model=rec.get('response_model') or rec.get('model')
    if not model:return None,'model_missing'
    if mode=='local':
        try:return local_rate(rec,stamp,retrospective),None
        except ValueError:return None,'model_missing'
    if not s or s.get('source_id')!=sid:return None,'no_catalogue'
    if mode=='fallback':return _select_list_price(s,rec,model,revision,stamp,retrospective)
    if sid=='anthropic':return _select_anthropic(s,rec,revision,stamp,retrospective)
    t=rec.get('returned_service_tier') or rec.get('service_tier') or 'unspecified'
    tier=_TIER_ALIASES.get(t,t)
    tier_basis=None
    # Fail-open: a tier value outside the documented set is priced at the
    # documented default (standard) and flagged; known premiums stay exact.
    if tier not in _OPENAI_TIERS:tier,tier_basis='standard',UNKNOWN_AS_DEFAULT
    # Catalogs from routed providers are standard list prices; no multiplier guessed.
    if sid!='openai' and tier!='standard':return None,'endpoint_unpriced'
    known=[r for r in s['rates'] if r['model']==model]
    if not known:return None,'model_missing'
    candidates,reason=_band([r for r in known if r['service_tier']==tier],rec)
    if reason:return None,reason
    r=copy.deepcopy(candidates[0])
    return {**r,'provider':rec['provider'],'source':s['source']+' public pricing','source_url':s['source_url'],
       'catalog_revision':revision,'observed_at':s['observed_at'],'content_sha256':s['content_sha256'],
       'origin':s['origin'],'stale_at_request':not retrospective and utc_day(stamp)!=utc_day(s['observed_at']),
       'retrospective':retrospective,
       'requested_service_tier':t,'tier_assumption':t in ('unspecified','auto','') or bool(tier_basis),
       **({'tier_basis':tier_basis} if tier_basis else {}),
       'basis':('subscription API-equivalent estimate; not a subscription debit' if rec['provider']=='openai-codex'
                else 'serving-provider public catalog estimate; not a provider-reported charge'),
       'pricing_basis':('subscription_api_equivalent' if rec['provider']=='openai-codex' else 'provider_catalog_estimate'),
       'scope':'text tokens; excludes tools, media, tax, regional uplifts and provider-specific charges'},None

def _band(candidates,rec):
    """Exact context band or min-prompt override; (candidates, reason)."""
    prompt=(rec.get('usage') or {}).get('prompt_tokens')
    if any(r.get('context_band') for r in candidates):
        if prompt is None:return [],'usage_missing'
        candidates=[r for r in candidates if r['context_band']==('long' if prompt>r['threshold_tokens'] else 'short')]
    elif any(r.get('min_prompt_tokens',0)>0 for r in candidates):
        if prompt is None:return [],'usage_missing'
        candidates=sorted([r for r in candidates if prompt>=r.get('min_prompt_tokens',0)],key=lambda r:r.get('min_prompt_tokens',0),reverse=True)[:1]
    if len(candidates)!=1:return [],('ambiguous' if len(candidates)>1 else 'endpoint_unpriced')
    return candidates,None

_FALLBACK_INDEX={}

def _fallback_index(s):
    """{OpenRouter model id: [rates]} per snapshot digest; bounded memo."""
    key=s.get('content_sha256')
    hit=_FALLBACK_INDEX.get(key)
    if hit is not None:return hit
    index={}
    for r in s.get('rates') or []:index.setdefault(r['model'],[]).append(r)
    if len(_FALLBACK_INDEX)>8:_FALLBACK_INDEX.clear()
    if key:_FALLBACK_INDEX[key]=index
    return index

def fallback_candidates(provider,model):
    """Ordered OpenRouter id tiers for one (provider, model); pure and exact.

    Tier 1 is the model id exactly as recorded. Tier 2 is every id derived
    deterministically: ``vendor/name`` for each vendor from
    FALLBACK_PROVIDER_VENDORS[provider] and the FALLBACK_MODEL_VENDORS family
    prefix, with ``name`` as recorded, lower-cased, and (for ``claude-``) with
    Anthropic's hyphenated version turned into OpenRouter's dotted form. A
    recorded ``vendor/name`` keeps its vendor. No ``:variant`` or ``~alias``
    id is ever derived.
    """
    if not isinstance(model,str) or not model:return []
    first=[model]
    vendor,_,name=model.rpartition('/')
    names=[]
    for n in (name,name.lower()):
        names.append(n)
        dotted=re.sub(r'^(claude-[a-z]+-\d+)-(\d{1,2})$',r'\1.\2',n)
        if dotted!=n:names.append(dotted)
    names=list(dict.fromkeys(names))
    if vendor:vendors=[vendor,vendor.lower()]
    else:
        vendors=[]
        if FALLBACK_PROVIDER_VENDORS.get(provider):vendors.append(FALLBACK_PROVIDER_VENDORS[provider])
        for pattern,v in FALLBACK_MODEL_VENDORS:
            if re.match(pattern,name.lower()):vendors.append(v)
    derived=[v+'/'+n for v in dict.fromkeys(vendors) for n in names]
    return [first,[m for m in dict.fromkeys(derived) if m!=model]]

_LIST_STANDARD={'standard','unspecified','auto','default','','standard_only'}

def local_rate(rec,stamp,retrospective):
    """$0 rate for a no-catalogue provider served from a local host class."""
    t=str(rec.get('returned_service_tier') or rec.get('service_tier') or 'unspecified')
    return {**rate(str(rec.get('response_model') or rec.get('model')),'standard',['0','0','0','0']),
            'provider':rec['provider'],'source':'Local endpoint','source_url':None,
            'catalog_revision':None,'observed_at':None,'content_sha256':None,'origin':'local_endpoint',
            'stale_at_request':False,'retrospective':retrospective,'requested_service_tier':t,
            'tier_assumption':False,'endpoint_host':rec.get('endpoint_host'),
            'basis':LOCAL_BASIS,'pricing_basis':'local_endpoint_zero',
            'scope':'self-hosted inference: no per-token provider charge; hardware and power are not costed'}

LOCAL_BASIS=('Served from a local/private-network endpoint: no per-token provider charge. A local proxy '
             'that forwards to a paid API would be undercounted')

def _select_list_price(s,rec,model,revision,stamp,retrospective):
    """Labelled OpenRouter list price for a provider with no catalogue here."""
    # Known premiums the list does not carry stay unpriced, never discounted.
    t=str(rec.get('returned_service_tier') or rec.get('service_tier') or 'unspecified')
    if (t in ('priority','fast','flex','batch','scale') or rec.get('requested_speed')=='fast'
            or rec.get('returned_speed')=='fast' or rec.get('returned_inference_geo')=='us'
            or rec.get('returned_usage_service_tier') in _ANTHROPIC_PREMIUM_TIERS):
        return None,'endpoint_unpriced'
    index=_fallback_index(s)
    match=None
    for level,tier in zip(('exact','vendor_prefixed'),fallback_candidates(rec.get('provider'),model)):
        found=[m for m in tier if m in index]
        if len(found)>1:return None,'ambiguous'
        if found:match=(level,found[0]);break
    if match is None:return None,'model_missing'
    candidates,reason=_band([r for r in index[match[1]] if r['service_tier']=='standard'],rec)
    if reason:return None,reason
    r=copy.deepcopy(candidates[0])
    return {**r,'provider':rec['provider'],'source':s['source']+' list price (fallback)','source_url':s['source_url'],
       'catalog_revision':revision,'observed_at':s['observed_at'],'content_sha256':s['content_sha256'],
       'origin':s['origin'],'stale_at_request':not retrospective and utc_day(stamp)!=utc_day(s['observed_at']),
       'retrospective':retrospective,'requested_service_tier':t,
       'tier_assumption':t!='standard',**({'tier_basis':UNKNOWN_AS_DEFAULT} if t not in _LIST_STANDARD else {}),
       'fallback_model':match[1],'fallback_match':match[0],
       'basis':LIST_PRICE_BASIS,'pricing_basis':'list_price_fallback',
       'scope':'text tokens at the router list price; excludes serving-provider discounts, uplifts, tools, media and tax'},None
