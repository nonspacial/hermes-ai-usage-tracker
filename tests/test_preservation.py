"""Regression checks against fingerprints of the actual uploaded quota plugin."""
import ast,hashlib,importlib.util,json
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
ROOT=Path(__file__).resolve().parents[1]

def test_original_desktop_components_unchanged():
    source=(ROOT/'desktop/plugin.js').read_text()
    for name,sha in json.loads((ROOT/'PRESERVED_UPSTREAM.json').read_text())['desktop_functions'].items():
        # Header now has the explicitly requested split refresh control. Its
        # existing quota controls and new actions are covered by browser suites.
        # ProviderCard now has requested responsive quota columns, covered by
        # test_quota_columns_browser; quota content and probes are unchanged.
        # ProfilePicker and useUsage intentionally support tagged All profiles
        # selection and scoped reads. The all-profiles browser suites cover
        # persistence, individual profiles, stale replies and read-only polling.
        # Keep the original-source manifest hashes as historical evidence.
        # UsageChip's approved display labels changed; normalise only those
        # exact strings before checking its immutable historical fingerprint.
        if name in ('PageHeader', 'ProviderCard', 'ProfilePicker', 'useUsage'):
            continue
        start=source.index('function '+name+'(');body=source[start:source.index('\n}',start)+2]
        if name == 'UsageChip':
            preview=(ROOT/'preview.html').read_text()
            start=preview.index('function '+name+'(')
            assert body==preview[start:preview.index('\n}',start)+2],name
            for approved, historical in (
                (": 'AI usage +'", ": 'AI usage'"),
                (' (pinned on the AI usage + page)', ' (pinned on the AI usage page)'),
                ('`AI usage +: ${detail}`', '`AI usage: ${detail}`'),
                (" : data ? 'AI usage +' : 'AI usage +…'", " : data ? 'AI usage' : 'AI usage…'"),
            ):
                assert body.count(approved)==1,(name,approved)
                body=body.replace(approved,historical)
        if name == 'QuotaBar':
            # Approved provider-identity colour: every measured allowance fill uses
            # providerAccent (Codex -> host --ui-accent). Normalise only these two
            # exact substitutions; covered by test_provider_colours_browser and
            # test_provider_row_quota_browser. Tone badges/labels stay original.
            assert body==preview_body(name),name
            for approved, historical in (
                ('function QuotaBar({ window, providerId }) {', 'function QuotaBar({ window }) {'),
                ("%`, background: providerAccent(providerId) }", "%`, background: 'var(--ui-accent)' }"),
            ):
                assert body.count(approved)==1,(name,approved)
                body=body.replace(approved,historical)
        assert hashlib.sha256(body.encode()).hexdigest()==sha,name

def preview_body(name):
    preview=(ROOT/'preview.html').read_text()
    start=preview.index('function '+name+'(')
    return preview[start:preview.index('\n}',start)+2]
_RUN_IN_HOME_DOC = """Run ``fn`` with ``home`` bound as this thread's Hermes home.

    The override lives in a ContextVar, so it is thread-local: a request can
    resolve another profile's credentials/state without disturbing the server's
    own profile (and ThreadPoolExecutor workers do NOT inherit the caller's
    context, so every worker binds it itself).
    """
_RUN_IN_HOME_INLINE = """
token = set_hermes_home_override(home)
try:
    return fn()
finally:
    reset_hermes_home_override(token)
"""

def _normalise_run_in_home(functions):
    run, bound = functions['_run_in_home'], functions['_bound_call']
    assert ast.unparse(run.body[-1]) == \
        'return _bound_call(home, fn, set_hermes_home_override, reset_hermes_home_override)'
    imports = [node for node in ast.walk(run) if isinstance(node, ast.ImportFrom)]
    assert [(node.module, [a.name for a in node.names]) for node in imports] == \
        [('hermes_constants', ['reset_hermes_home_override', 'set_hermes_home_override'])]
    # _bound_call: set home with the given setter, run fn inside try, reset that
    # exact token last in finally (after the secret scope, if one was bound).
    assert [a.arg for a in bound.args.args][:4] == ['home', 'fn', 'set_home', 'reset_home']
    assert ast.unparse(bound.body[2]) == 'home_token = set_home(home)'
    guarded = bound.body[4]
    assert isinstance(guarded, ast.Try) and not guarded.handlers
    assert ast.unparse(guarded.body[-1]) == 'return fn()'
    assert ast.unparse(guarded.finalbody[-1]) == 'reset_home(home_token)'
    assert sum(ast.unparse(n) == 'reset_home(home_token)' for n in ast.walk(bound)
               if isinstance(n, ast.Expr)) == 1
    assert isinstance(run.body[0], ast.Expr) and isinstance(run.body[0].value, ast.Constant)
    run.body[0].value.value = _RUN_IN_HOME_DOC
    run.body[-1:] = ast.parse(_RUN_IN_HOME_INLINE).body

def test_original_quota_probe_functions_unchanged():
    tree=ast.parse((ROOT/'dashboard/plugin_api.py').read_text())
    functions={n.name:n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
    for name,sha in json.loads((ROOT/'PRESERVED_UPSTREAM.json').read_text())['quota_backend_functions'].items():
        # Historical hash remains a provenance record. Normalise only the
        # intentional unavailable-reason correction, then check the rest of the
        # probe against upstream; response behaviour is tested separately.
        if name == '_probe_standard':
            corrected = 'Subscription limits unavailable — quota could not be read for this profile.'
            replacements = 0
            for node in ast.walk(functions[name]):
                if isinstance(node, ast.Constant) and node.value == corrected:
                    node.value = 'No credentials for this provider in this profile.'
                    replacements += 1
            assert replacements == 1
        # Worker binding now adds the selected profile's secret scope by delegating
        # to _bound_call with the same home set/reset pair. Pin that delegation and
        # _bound_call's home ordering, restore the historical inline binding and
        # docstring, then check the rest (guards, legacy fallback) against upstream.
        # Scope behaviour: tests/test_profile_secret_scope.py.
        if name == '_run_in_home':
            _normalise_run_in_home(functions)
        # Source-only activity discovery now binds only the home (no secret scope
        # or hydration); normalise exactly that one call back to _run_in_home.
        if name == '_build_payload':
            calls = [node for node in ast.walk(functions[name]) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Name) and node.func.id == '_run_in_home_only']
            assert [ast.unparse(node) for node in calls] == ['_run_in_home_only(home, _active_providers)']
            calls[0].func = ast.Name(id='_run_in_home', ctx=ast.Load())
        # Nous's nested worker now inherits the bound context; normalise only
        # that submit argument, then check the rest of the probe.
        if name == '_probe_nous':
            submits = [node for node in ast.walk(functions[name]) if isinstance(node, ast.Call)
                       and isinstance(node.func, ast.Attribute) and node.func.attr == 'submit']
            assert len(submits) == 1 and ast.unparse(submits[0].args[0]) == 'contextvars.copy_context().run'
            submits[0].args[:2] = [submits[0].args[1]]
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
