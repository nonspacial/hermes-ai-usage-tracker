#!/usr/bin/env python3
"""Offline compatibility inspection: AST only, no imports of provider clients."""
import argparse,ast,importlib.util,json,os,subprocess,sys
from pathlib import Path
EXPECTED={
 'run_agent.py':{'_usage_summary_for_api_request_hook':['self','response']},
 'agent/auxiliary_client.py':{'_relay_sync_completion':['client','kwargs'],'_relay_async_completion':['client','kwargs']},
 'agent/conversation_compression.py':{'compress_context':['agent','messages','system_message'],'_emit_compression_attempt_telemetry':['agent','commit_status','split_status']},
 'agent/codex_runtime.py':{'_consume_codex_event_stream':['event_iter','on_event'],'run_codex_stream':['agent','api_kwargs']},
 'agent/anthropic_adapter.py':{'create_anthropic_message':['client','api_kwargs']},
 'agent/context_compressor.py':{'_micro_compact':['self','messages'],'_emit_micro_compaction_telemetry':['self','tokens_before','tokens_after','outcome']}}
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--repo',type=Path);p.add_argument('--out',type=Path);a=p.parse_args()
    root=a.repo
    if not root:
        spec=importlib.util.find_spec('run_agent')
        if spec and spec.origin:root=Path(spec.origin).parent
    if not root:
        print('Run in the Hermes Python environment, or add --repo /path/to/hermes-agent.');return 2
    root=root.expanduser().resolve();checks={}
    for file,functions in EXPECTED.items():
        try:
            tree=ast.parse((root/file).read_text());defs={n.name:n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
            for fn,required in functions.items():
                n=defs.get(fn);names=[] if n is None else [x.arg for x in n.args.posonlyargs+n.args.args+n.args.kwonlyargs]
                checks[file+':'+fn]='compatible signature' if set(required)<=set(names) else 'MISSING / SIGNATURE CHANGED'
        except (OSError,SyntaxError) as e:checks[file]=type(e).__name__
    try:commit=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],stderr=subprocess.DEVNULL,text=True,timeout=5).strip()
    except Exception:commit=None
    result={'checked_repo':str(root),'installed_commit':commit,'checks':checks,'network_calls':0,'inference_calls':0,
      'note':'Signature compatibility is not runtime coverage proof. Check the connection badge and individual records after a main call, auxiliary call and compression.'}
    print(json.dumps(result,indent=2))
    if a.out:a.out.write_text(json.dumps(result,indent=2)+'\n')
    return 0 if checks and all(v=='compatible signature' for v in checks.values()) else 2
if __name__=='__main__':raise SystemExit(main())
