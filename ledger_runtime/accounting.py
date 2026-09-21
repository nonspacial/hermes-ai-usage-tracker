"""Token semantics and exact decimal arithmetic. Never tokenize text or call APIs."""
from decimal import Decimal, InvalidOperation
from typing import Any

BUCKETS = ('input_tokens','output_tokens','cache_read_tokens','cache_write_tokens')
METRICS = BUCKETS + ('reasoning_tokens','prompt_tokens','total_tokens')
RAW_KEYS = {'input_tokens','output_tokens','prompt_tokens','completion_tokens','total_tokens',
    'cache_read_input_tokens','cache_creation_input_tokens','prompt_cache_hit_tokens',
    'prompt_cache_miss_tokens','cached_tokens','cache_write_tokens','cache_creation_tokens',
    'reasoning_tokens','input_tokens_details','output_tokens_details','prompt_tokens_details',
    'completion_tokens_details','cache_creation','ephemeral_5m_input_tokens','ephemeral_1h_input_tokens',
    'promptTokenCount','candidatesTokenCount','totalTokenCount','cachedContentTokenCount','thoughtsTokenCount'}

def usage_priority(source):
    return {'wire_terminal_usage':100,'native_terminal_usage':80,'native_gemini_usage':80,
            'native_anthropic_stream_usage':80,'native_assembled_usage':20,
            'hermes_normalized':0,'missing':0,None:0}.get(source,10)

def mapping(value: Any) -> dict:
    if isinstance(value, dict): return value
    if hasattr(value, 'model_dump'):
        try: return value.model_dump(exclude_none=True)
        except Exception: return {}
    if hasattr(value, '__dict__'): return vars(value)
    return {}

def num(x):
    if isinstance(x, bool): return None
    if isinstance(x, (int,float)) and x >= 0 and int(x) == x: return int(x)
    return None

def safe_usage(raw: Any, depth: int = 0) -> dict:
    """An allowlist, NOT a redactor: content, headers and strings cannot enter."""
    if depth > 3: return {}
    out = {}
    for key,value in mapping(raw).items():
        if key not in RAW_KEYS: continue
        if num(value) is not None: out[key] = num(value)
        elif isinstance(value,dict) or hasattr(value,'model_dump') or hasattr(value,'__dict__'):
            sub = safe_usage(value,depth+1)
            if sub: out[key] = sub
    return out

def first(*args):
    return next((num(v) for v in args if num(v) is not None),None)

def normalize(raw=None, canonical=None, provider='', api_mode=''):
    """Unknown fields stay null. Canonical hook zeros are explicitly normalized."""
    c = mapping(canonical)
    u = safe_usage(raw) or safe_usage(c.get('_ai_usage_raw_usage'))
    if not u:
        c = mapping(canonical)
        vals = {k:num(c.get(k)) for k in METRICS}
        if c:
            if vals['prompt_tokens'] is None and all(vals[k] is not None for k in ('input_tokens','cache_read_tokens','cache_write_tokens')):
                vals['prompt_tokens'] = sum(vals[k] for k in ('input_tokens','cache_read_tokens','cache_write_tokens'))
            if vals['total_tokens'] is None and vals['prompt_tokens'] is not None and vals['output_tokens'] is not None:
                vals['total_tokens'] = vals['prompt_tokens']+vals['output_tokens']
        original=dict(vals)
        warnings=['raw_usage_unavailable'] if c else []
        provenance={k:'hermes_normalized' if vals[k] is not None else 'missing' for k in METRICS}
        # CanonicalUsage uses default zeros. Without a retained response we cannot
        # distinguish an explicitly reported zero from an absent cache field.
        unknown_zero=False
        for k in ('cache_read_tokens','cache_write_tokens'):
            if vals[k]==0:
                vals[k]=None;provenance[k]='unverified_normalized_zero';unknown_zero=True
        if unknown_zero:
            warnings.append('normalized_cache_zero_not_provider_verified')
            vals['input_tokens']=None;provenance['input_tokens']='unverified_cache_decomposition'
        return {**vals,'usage_source':'hermes_normalized' if c else 'missing',
                'raw_usage':None,'warnings':warnings,'request_count':num(c.get('request_count')) or 1,
                'field_provenance':provenance,'normalized_usage':original if c else None,
                'normalization_version':2}
    d = u.get('input_tokens_details',u.get('prompt_tokens_details',{}))
    o = u.get('output_tokens_details',u.get('completion_tokens_details',{}))
    rd = first(d.get('cached_tokens'),u.get('cache_read_input_tokens'),u.get('prompt_cache_hit_tokens'),u.get('cached_tokens'),u.get('cachedContentTokenCount'))
    wr = first(d.get('cache_write_tokens'),d.get('cache_creation_tokens'),d.get('cache_creation_input_tokens'),u.get('cache_creation_input_tokens'),u.get('cache_write_tokens'),u.get('cache_creation_tokens'))
    out = first(u.get('output_tokens'),u.get('completion_tokens'),u.get('candidatesTokenCount'))
    reason = first(o.get('reasoning_tokens'),u.get('reasoning_tokens'),u.get('thoughtsTokenCount'))
    inp = first(u.get('input_tokens'),u.get('prompt_tokens'),u.get('promptTokenCount'))
    gemini = any(k in u for k in ('promptTokenCount','candidatesTokenCount','totalTokenCount','thoughtsTokenCount'))
    if gemini and out is not None and reason is not None:
        # Native candidate counts exclude thinking; output includes it once.
        out += reason
    warnings=[]
    # Explicit Anthropic cache keys establish exclusive input semantics, including
    # Anthropic-native shapes returned through aggregators. Generic prompt_tokens
    # is inclusive; never guess exclusive just from the model name.
    anth = 'input_tokens' in u and ('cache_read_input_tokens' in u or 'cache_creation_input_tokens' in u or api_mode in ('anthropic','anthropic_messages'))
    if anth:
        base=inp
        prompt=(inp+rd+wr) if inp is not None and rd is not None and wr is not None else None
        if rd is None or wr is None: warnings.append('absent_cache_fields_not_assumed_zero')
    else:
        prompt=inp
        # Only calculate an exact exclusive bucket when cache decomposition is known.
        base=inp-rd-wr if inp is not None and rd is not None and wr is not None else None
        if base is not None and base<0:
            base=None; warnings.append('cache_components_exceed_input')
        if rd is None or wr is None: warnings.append('cache_breakdown_incomplete')
    total=prompt+out if prompt is not None and out is not None else None
    reported_total = first(u.get('total_tokens'),u.get('totalTokenCount'))
    if reported_total is not None and total is not None and reported_total!=total:
        warnings.append('provider_total_differs_from_input_plus_output')
    if reported_total is not None:
        total = reported_total
    if reason is not None and out is not None and reason>out: warnings.append('reasoning_exceeds_output')
    return dict(input_tokens=base,output_tokens=out,cache_read_tokens=rd,cache_write_tokens=wr,
                reasoning_tokens=reason,prompt_tokens=prompt,total_tokens=total,request_count=1,
                raw_usage=u,usage_source='response_usage',warnings=warnings,normalization_version=2,
                field_provenance={k:('missing' if v is None else 'response_field' if k=='total_tokens' and reported_total is not None else 'derived_from_response' if k in ('input_tokens','prompt_tokens','total_tokens') or (k=='output_tokens' and gemini and reason is not None) else 'response_field') for k,v in dict(input_tokens=base,prompt_tokens=prompt,total_tokens=total,output_tokens=out,cache_read_tokens=rd,cache_write_tokens=wr,reasoning_tokens=reason).items()})

def decimal_value(value):
    if value is None or value == '': return None
    v=Decimal(str(value))
    if not v.is_finite() or v<0: raise ValueError('Rates must be finite and nonnegative.')
    return v

def validate_rate(rate):
    allowed={'provider','model','service_tier','source','note',*BUCKETS}
    r={k:v for k,v in rate.items() if k in allowed}
    for k in ('provider','model'):
        if not isinstance(r.get(k),str) or not r[k].strip() or len(r[k])>240: raise ValueError('Provider and exact model are required.')
    r['service_tier']=str(r.get('service_tier') or 'unspecified')[:80]
    r['source']=str(r.get('source') or 'user-entered USD per million')[:200]
    r['note']=str(r.get('note') or '')[:300]
    for k in BUCKETS:
        v=decimal_value(r.get(k)); r[k]=str(v) if v is not None else None
    return r

def costs(usage,rate):
    parts={}
    for k in BUCKETS:
        n=usage.get(k);p=decimal_value(rate.get(k)) if rate else None
        ttl=(usage.get('raw_usage') or {}).get('cache_creation',{})
        if k=='cache_write_tokens' and ttl.get('ephemeral_1h_input_tokens',0)>0:
            one=ttl['ephemeral_1h_input_tokens'];five=ttl.get('ephemeral_5m_input_tokens')
            hour=decimal_value(rate.get('cache_write_1h_tokens')) if rate else None
            val=(Decimal(one)*hour+Decimal(five)*p)/Decimal(1000000) if hour is not None and p is not None and five is not None and one+five==n else None
        else:
            val=Decimal(0) if n==0 else (Decimal(n)*p/Decimal(1000000) if n is not None and p is not None else None)
        parts[k]=str(val) if val is not None else None
    complete=all(v is not None for v in parts.values())
    ordinary=decimal_value(rate.get('input_tokens')) if rate else None
    def difference(bucket,reverse=False):
        n=usage.get(bucket);actual=parts.get(bucket)
        if n==0:return Decimal(0)
        if n is None or actual is None or ordinary is None:return None
        base=Decimal(n)*ordinary/Decimal(1000000)
        return Decimal(actual)-base if reverse else base-Decimal(actual)
    read=difference('cache_read_tokens');premium=difference('cache_write_tokens',True)
    net=read-premium if read is not None and premium is not None else None
    return {'components':parts,'total_usd':str(sum(Decimal(v) for v in parts.values())) if complete else None,
            'known_components_usd':str(sum(Decimal(v) for v in parts.values() if v is not None)),
            'cache_read_savings_usd':str(read) if read is not None else None,
            'cache_write_premium_usd':str(premium) if premium is not None else None,
            'cache_savings_usd':str(net) if net is not None else None,
            'savings_basis':'same request/model/tier/context rates versus uncached input, minus cache-write premium; can be negative',
            'complete':complete,'rate':rate,'basis':'API-equivalent estimate, not subscription debit'}

def builtin_rate(provider,model,tier):
    """Read an exact local Hermes snapshot only. No network or fuzzy model aliases."""
    if tier not in ('default','standard','unspecified',None,''): return None
    try:
        from agent.usage_pricing import _OFFICIAL_DOCS_PRICING
        alias={'openai-codex':'openai','openai-api':'openai','google-gemini':'google'}
        entry=_OFFICIAL_DOCS_PRICING.get((alias.get(provider,provider),model.lower()))
        if entry is None: return None
        fields={k:str(getattr(entry,k.replace('_tokens','_cost_per_million'))) if getattr(entry,k.replace('_tokens','_cost_per_million'),None) is not None else None for k in BUCKETS}
        return {**fields,'provider':provider,'model':model,'service_tier':tier or 'unspecified',
                'source':'Hermes local pricing snapshot (standard tier)',
                'pricing_version':str(getattr(entry,'pricing_version','unknown')),
                'note':'Unverified against current invoice; explicit fast/priority tiers require a rate override.'}
    except (ImportError,AttributeError,TypeError): return None


def effective_record(record):
    """Read-only interpretation of old normalized-only cache zeros.

    Preserve the stored record and its original amounts. The displayed estimate
    uses the SAME saved unit rates but excludes components now known to be
    unverifiable. Nothing is written back or fetched from a pricing catalogue.
    """
    usage=record.get('usage') or {}
    if (usage.get('usage_source')!='hermes_normalized' or usage.get('raw_usage')
            or usage.get('normalization_version')==2
            or not any(usage.get(k)==0 for k in ('cache_read_tokens','cache_write_tokens'))):
        return record
    result=dict(record)
    result['stored_accounting']={'usage':usage,'cost':record.get('cost')}
    result['usage']=normalize(canonical=usage)
    result['cost']=costs(result['usage'],(record.get('cost') or {}).get('rate'))
    result['view_adjustments']=['Legacy normalized cache zeros are unverified. Original stored accounting is retained below; amounts here use unchanged saved rates and known components only.']
    return result
