"""Regression checks against fingerprints of the actual uploaded quota plugin."""
import ast,hashlib,importlib.util,json
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
ROOT=Path(__file__).resolve().parents[1]

def test_original_desktop_components_unchanged():
    source=(ROOT/'desktop/plugin.js').read_text()
    for name,sha in json.loads((ROOT/'PRESERVED_UPSTREAM.json').read_text())['desktop_functions'].items():
        start=source.index('function '+name+'(');body=source[start:source.index('\n}',start)+2]
        assert hashlib.sha256(body.encode()).hexdigest()==sha,name

def test_original_quota_probe_functions_unchanged():
    tree=ast.parse((ROOT/'dashboard/plugin_api.py').read_text())
    functions={n.name:n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
    for name,sha in json.loads((ROOT/'PRESERVED_UPSTREAM.json').read_text())['quota_backend_functions'].items():
        assert hashlib.sha256(ast.dump(functions[name],include_attributes=False).encode()).hexdigest()==sha,name

def test_quota_routes_survive_ledger_bootstrap_failure(monkeypatch,tmp_path):
    loader=importlib.util.spec_from_file_location
    def fail_ledger(name,*args,**kwargs):
        if name=='_ai_usage_backend_bootstrap':raise RuntimeError('Synthetic missing ledger dependency')
        return loader(name,*args,**kwargs)
    monkeypatch.setattr(importlib.util,'spec_from_file_location',fail_ledger)
    spec=loader('test_quota_only_api',ROOT/'dashboard/plugin_api.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert module._ledger_store is None
    assert {r.path for r in module.router.routes}=={'/usage','/profiles','/health'}
    monkeypatch.setattr(module,'_resolve_profile',lambda profile:(tmp_path,'default',None))
    monkeypatch.setattr(module,'_build_payload',lambda home,profile:{'providers':[{'id':'synthetic-quota'}]})
    app=FastAPI();app.include_router(module.router);client=TestClient(app)
    response=client.get('/usage');assert response.status_code==200
    assert response.json()['providers']==[{'id':'synthetic-quota'}]
    assert client.get('/health').json()['ok']
