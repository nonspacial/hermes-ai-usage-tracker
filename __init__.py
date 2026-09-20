"""Quota UI plus passive native request/compression ledger."""
from pathlib import Path
import importlib.util

def _load():
    spec=importlib.util.spec_from_file_location('_ai_usage_bootstrap',Path(__file__).parent/'bootstrap.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    from _hermes_ai_usage_ledger_v2 import recorder,adapters
    return recorder,adapters

def register(ctx):
    recorder,adapters=_load()
    ctx.register_hook('pre_api_request',recorder.pre)
    ctx.register_hook('post_api_request',recorder.post)
    ctx.register_hook('api_request_error',recorder.error)
    ctx.register_hook('on_session_end',recorder.session_end)
    ctx.register_hook('on_session_start',recorder.session_start)
    for event,callback in [('subagent_start',recorder.subagent_start),('subagent_stop',recorder.subagent_stop)]:
        try:
            ctx.register_hook(event,callback);recorder.ADAPTERS[event]='registered'
        except Exception as exc:recorder.ADAPTERS[event]='unavailable: '+type(exc).__name__
    recorder.ADAPTERS['request hooks']='registered'
    adapters.install()
    recorder.start_heartbeat()
    from _hermes_ai_usage_ledger_v2.pricing import start_worker
    start_worker(recorder.store())
