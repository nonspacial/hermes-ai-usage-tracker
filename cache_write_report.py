#!/usr/bin/env python3
"""Read-only cache-write evidence report. No imports of Hermes, network or inference.

Reads the plugin's events.sqlite3, NOT Hermes state.db. Does not change records,
reprice requests, or infer writes from uncached input/cache-read differences.
Only metadata and counters are exported, not full request JSON or conversations.
"""
from __future__ import annotations
import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any

PATHS=(('input_tokens_details','cache_write_tokens'),('input_tokens_details','cache_creation_tokens'),('cache_write_tokens',),('cache_creation_tokens',),('cache_creation_input_tokens',))
STATES={'positive','explicit_zero','field_absent','invalid_or_null','conflicting_fields'}


def num(v: Any) -> int | None:
    if isinstance(v,bool) or not isinstance(v,(int,float)):return None
    if isinstance(v,float) and not math.isfinite(v):return None
    return int(v) if v>=0 and int(v)==v else None

def dictionary(value: Any) -> dict:
    return value if isinstance(value,dict) else {}

def fields_in(raw: dict) -> tuple[str,int|None]:
    found=[];invalid=False
    for path in PATHS:
        obj=raw
        for key in path:
            if not isinstance(obj,dict) or key not in obj:break
            obj=obj[key]
        else:
            n=num(obj)
            if n is None:invalid=True
            else:found.append(n)
    values=set(found)
    if len(values)>1:return 'conflicting_fields',None
    if invalid:return 'invalid_or_null',None
    if not found:return 'field_absent',None
    n=found[0];return 'positive' if n>0 else 'explicit_zero',n

def classify(rec: dict) -> tuple[str,str,int|None]:
    evidence=dictionary(rec.get('cache_evidence'));usage=dictionary(rec.get('usage'))
    if evidence.get('boundary')=='decoded_http_json_before_sdk_models':
        state=evidence.get('cache_write_state')
        if state not in STATES:return 'pre_sdk_http_json','invalid_or_null',None
        f=fields_in(dictionary(evidence.get('usage')))
        return 'pre_sdk_http_json',state,f[1] if state in ('positive','explicit_zero') else None
    raw=dictionary(usage.get('raw_usage'))
    if raw:return 'sdk_or_adapter_usage',*fields_in(raw)
    # A normalized-only zero cannot establish that the provider sent that field.
    value=num(usage.get('cache_write_tokens'))
    return 'normalized_only' if usage else 'usage_missing',('normalized_positive_unverified' if value else 'not_provider_verified'),None

def sdk_info() -> dict:
    result={}
    try:result['openai_distribution_version']=importlib.metadata.version('openai')
    except importlib.metadata.PackageNotFoundError:result['openai_distribution_version']=None
    try:
        spec=importlib.util.find_spec('openai')
        roots=list(spec.submodule_search_locations or []) if spec else []
        path=next((Path(root)/'_base_client.py' for root in roots if (Path(root)/'_base_client.py').is_file()),None)
        if not path:return {**result,'boundary_signatures':'SDK source not found in this Python environment'}
        tree=ast.parse(path.read_text(encoding='utf-8'))
        klass=next((x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='BaseClient'),None)
        for name,expected in {'_build_request':{'self','options'},'_process_response_data':{'self','data','cast_to','response'}}.items():
            fn=next((x for x in klass.body if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) and x.name==name),None) if klass else None
            names={x.arg for x in fn.args.args+fn.args.posonlyargs+fn.args.kwonlyargs} if fn else set()
            result[name]='compatible signature' if expected<=names else 'MISSING / SIGNATURE CHANGED'
    except (OSError,SyntaxError,ValueError,ImportError,AttributeError) as exc:
        result['inspection_error']=type(exc).__name__
    return result

def report(db: Path,hours: float=24,now: float|None=None) -> dict:
    now=time.time() if now is None else now
    cutoff=now-hours*3600 if hours else 0
    if not db.is_file():raise FileNotFoundError('Ledger database not found; set --home to the selected Hermes profile or use --db.')
    conn=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
    counts=Counter();models={};examples=[];bad=0;records=0;pending=0
    options=Counter();endpoint=Counter();diag=Counter();mismatch=0
    try:
        for raw, in conn.execute("SELECT data FROM requests WHERE started>=? AND started<=? AND provider IN ('openai-codex','openai') ORDER BY started",(cutoff,now)):
            records+=1
            try:rec=json.loads(raw)
            except (TypeError,ValueError):bad+=1;continue
            if not isinstance(rec,dict):bad+=1;continue
            if rec.get('status') in ('pending','usage_received'):pending+=1
            level,state,amount=classify(rec);counts[level+':'+state]+=1
            model=str(rec.get('response_model') or rec.get('model') or 'unknown')[:240]
            key=(str(rec.get('provider')),model)
            group=models.setdefault(key,{'provider':key[0],'model':model,'requests':0,'pre_sdk_requests':0,'pre_sdk_write_tokens':0,'sdk_write_tokens':0,'classification':Counter()})
            group['requests']+=1;group['classification'][level+':'+state]+=1
            if level=='pre_sdk_http_json':group['pre_sdk_requests']+=1
            if amount is not None:
                if level=='pre_sdk_http_json':group['pre_sdk_write_tokens']+=amount
                elif level=='sdk_or_adapter_usage':group['sdk_write_tokens']+=amount
            evidence=dictionary(rec.get('cache_evidence'));meta=dictionary(rec.get('cache_request')) or dictionary(evidence.get('request'))
            if meta:
                options['explicit_breakpoint_present' if (num(meta.get('explicit_breakpoint_count')) or 0)>0 else 'no_explicit_breakpoint_observed']+=1
                options['cache_options_sent' if meta.get('options_present') else 'cache_options_not_sent']+=1
                options['diagnostic_comparison_requested' if meta.get('comparison_requested') else 'no_diagnostic_comparison_requested']+=1
            else:options['request_settings_not_captured']+=1
            route=evidence.get('endpoint_kind') or rec.get('cache_endpoint_kind') or 'not_captured'
            endpoint[route if route in ('chatgpt_codex_subscription','openai_public_api','other_responses_endpoint') else 'not_captured']+=1
            d=dictionary(evidence.get('diagnostics'));dt=d.get('type')
            diag[dt if dt in ('cache_hit','cache_miss','unavailable','comparison_response_not_found') else 'not_reported_or_unrecognized']+=1
            if level=='pre_sdk_http_json' and amount is not None and num(dictionary(rec.get('usage')).get('cache_write_tokens'))!=amount:mismatch+=1
            if level=='pre_sdk_http_json' and len(examples)<12:
                examples.append({'record_id':str(rec.get('id',''))[:800],'provider_response_id':str(rec.get('provider_response_id',''))[:240],
                    'model':model,'cache_write_state':state,'write_tokens':amount,
                    'raw_input_tokens':num(dictionary(evidence.get('usage')).get('input_tokens')),
                    'cached_tokens':num(dictionary(dictionary(evidence.get('usage')).get('input_tokens_details')).get('cached_tokens')),
                    'diagnostics_type':dt if dt in ('cache_hit','cache_miss','unavailable','comparison_response_not_found') else None,
                    'options_present':meta.get('options_present') if isinstance(meta.get('options_present'),bool) else None,
                    'mode_sent':meta.get('mode_sent') if meta.get('mode_sent') in ('implicit','explicit','other') else None,
                    'explicit_breakpoint_count':num(meta.get('explicit_breakpoint_count')),
                    'cache_key_present':meta.get('cache_key_present') if isinstance(meta.get('cache_key_present'),bool) else None})
        health=[]
        try:
            for data, in conn.execute('SELECT data FROM health ORDER BY updated DESC LIMIT 8'):
                h=dictionary(json.loads(data));adapters=dictionary(h.get('adapters'))
                health.append({'version':h.get('version'),'heartbeat_at':h.get('heartbeat_at') if isinstance(h.get('heartbeat_at'),(float,int)) and math.isfinite(h['heartbeat_at']) else None,
                    'request_hooks_registered':h.get('request_hooks_registered') is True,
                    'cache_probe_adapters':{k:str(v)[:160] for k,v in adapters.items() if k.endswith('._build_request') or k.endswith('._process_response_data') or k=='openai._base_client'}})
        except (sqlite3.Error,ValueError,TypeError):health=[]
    finally:conn.rollback();conn.close()
    return {'report_version':1,'generated_at_utc':datetime.fromtimestamp(now,timezone.utc).isoformat(),
        'window_start_utc':datetime.fromtimestamp(cutoff,timezone.utc).isoformat(),'network_calls':0,'inference_calls':0,
        'request_records':records,'pending_records':pending,'invalid_records':bad,
        'classification':dict(counts),'models':[{**v,'classification':dict(v['classification'])} for _,v in sorted(models.items())],
        'request_settings':dict(options),'endpoints':dict(endpoint),'diagnostics':dict(diag),
        'pre_sdk_vs_current_write_mismatches':mismatch,'recording_processes':health,'sdk_inspection':sdk_info(),'pre_sdk_examples':examples,
        'notes':['Each request row is counted once, never each observation/event.',
                 'Pre-SDK reports classify the decoded HTTP terminal usage. SDK-only and normalized-only data are kept separate.',
                 'Zero is a reported value, not a measurement of physical cache writes. Missing is not zero.',
                 'Existing records without cache_evidence cannot be reconstructed by this report.',
                 'Endpoint rejection or cache semantics cannot be decided from mode/breakpoint presence alone.',
                 'A stopped producer may remain in the historical health list; heartbeat time is not a guarantee of current liveness.']}

def main(argv: list[str]|None=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    target=parser.add_mutually_exclusive_group()
    target.add_argument('--home',type=Path,help='Hermes HOME for the selected profile, e.g. ~/.hermes/profiles/infra')
    target.add_argument('--db',type=Path,help='Explicit usage-ledger/events.sqlite3 path')
    parser.add_argument('--hours',type=float,default=24,help='Request-start lookback in hours; 0 means all recorded (default 24)')
    parser.add_argument('--out',type=Path,help='Optional new JSON report file; existing files are not overwritten')
    args=parser.parse_args(argv)
    if not math.isfinite(args.hours) or args.hours<0:parser.error('--hours must be nonnegative and finite')
    root=(args.home or Path(os.environ.get('HERMES_HOME',str(Path.home()/'.hermes')))).expanduser()
    db=(args.db or root/'usage-ledger'/'events.sqlite3').expanduser()
    try:
        result=report(db,args.hours);text=json.dumps(result,indent=2,allow_nan=False)+'\n'
        if args.out:
            with args.out.expanduser().open('x',encoding='utf-8') as f:f.write(text)
        print(text)
    except (OSError,sqlite3.Error,ValueError,OverflowError) as exc:
        print('Report failed: '+type(exc).__name__+'. Check the ledger path, output path, and permissions.',file=sys.stderr)
        if isinstance(exc,FileNotFoundError):print('Expected ledger: '+str(db),file=sys.stderr)
        return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
