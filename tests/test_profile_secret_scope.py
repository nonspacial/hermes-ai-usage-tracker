"""Selected-profile Hermes home + secret scope binding for quota/reset workers.

Two layers, both offline and on synthetic homes only:

* installed-host: the real ``agent.secret_scope`` guard and the real Anthropic
  quota fetcher, run by the installed Hermes interpreter in a subprocess whose
  ``HOME``/``HERMES_HOME`` are disposable. ``httpx`` gets a MockTransport and
  sockets refuse to connect, so a request can only reach the fixture quota GET.
* host-version compatibility: fake host modules in this venv prove a legacy
  host keeps home-only binding and a partial secret-scope API fails closed.
"""
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import importlib.util
import pytest

ROOT = Path(__file__).resolve().parents[1]
HERMES_PYTHON = Path.home() / '.hermes/hermes-agent/venv/bin/python'

INSTALLED_HOST_CHECK = r'''
import hashlib, json, os, socket, sys, threading, importlib.util, logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

launch, work, empty, plugin_api = (Path(p) for p in sys.argv[1:5])
root = launch.parent

def refuse(*_a, **_k):
    raise AssertionError('socket connection attempted')
socket.socket.connect = refuse
socket.create_connection = refuse

import httpx
requests = []
lock = threading.Lock()
def handler(request):
    assert (request.method, str(request.url)) == ('GET', 'https://api.anthropic.com/api/oauth/usage'), request.url
    with lock:
        requests.append(request.headers['authorization'].removeprefix('Bearer '))
    return httpx.Response(200, json={'five_hour': {'utilization': 12.0, 'resets_at': None},
                                     'seven_day': {'utilization': 34.0, 'resets_at': None}})
_init = httpx.Client.__init__
def mocked_init(self, *a, **k):
    k['transport'] = httpx.MockTransport(handler)
    _init(self, *a, **k)
httpx.Client.__init__ = mocked_init

def inventory():
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}

logging.disable(logging.WARNING)
spec = importlib.util.spec_from_file_location('fixture_scope_plugin_api', plugin_api)
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
module._ledger_store = None
module._active_providers = lambda: ({'anthropic'}, {})  # no state.db in fixture homes

from agent.secret_scope import (UnscopedSecretError, current_secret_scope, get_secret,
                                is_multiplex_active, set_multiplex_active)
from hermes_constants import get_hermes_home_override
from agent.account_usage import fetch_account_usage
from agent.secret_sources.base import ErrorKind, FetchResult, SecretSource
from agent.secret_sources.registry import register_source
from hermes_constants import hermes_home_key

# Synthetic failing external source, registered for the foreign profile only
# (enabled by its fixture config.yaml). Pure Python: no helper process runs. A
# failed source is never marked applied, so the host retries it on every
# hydration: the counter is the number of hydration attempts.
fetches = []
class FailingFixtureSource(SecretSource):
    name, label, shape = 'counter_fixture', 'Counter fixture', 'mapped'
    def fetch(self, cfg, home_path):
        with lock:
            fetches.append(str(home_path))
        return FetchResult().fail('synthetic failing source', ErrorKind.AUTH_FAILED)
assert register_source(FailingFixtureSource(), scope=hermes_home_key(work))

before = inventory()
set_multiplex_active(True)   # what `hermes serve` does on a multi-profile host
assert is_multiplex_active()
# Guard control: an unscoped worker is refused by the host, then swallowed to None
# by fetch_account_usage before any HTTP request (the proven live failure).
try:
    get_secret('ANTHROPIC_TOKEN'); raise AssertionError('guard inactive')
except UnscopedSecretError:
    pass
with ThreadPoolExecutor(max_workers=1) as pool:
    assert pool.submit(fetch_account_usage, 'anthropic').result() is None
assert requests == []

def quota(home, name):
    # As GET /usage does: prepare the selected scope once, pass it to every worker.
    item = module._build_payload(module._prepare_home(home), name)['providers']
    [anthropic] = [p for p in item if p['id'] == 'anthropic']
    return anthropic['quota']

expected = {'infra': 'sk-ant-oat01-launch-env-fixture', 'work': 'sk-ant-oat01-work-file-fixture'}
results = {}
def run(name, home, i):
    q = quota(home, name)
    with lock:
        results.setdefault(name, []).append(q)
with ThreadPoolExecutor(max_workers=6) as pool:
    for f in [pool.submit(run, n, h, i) for i in range(8)
              for n, h in (('infra', launch), ('work', work), ('empty', empty))]:
        f.result()

for name in ('infra', 'work'):
    for q in results[name]:
        assert q['available'] is True, (name, q)
        assert [(w['label'], w['used_percent']) for w in q['windows']] == \
            [('Current session', 12.0), ('Current week', 34.0)], q
for q in results['empty']:
    # No token in this profile's own files: the launch env token must not bleed in.
    assert q['available'] is False and q['source'] == 'usage_api', q
assert sorted(set(requests)) == sorted(expected.values()), sorted(set(requests))
assert requests.count(expected['infra']) == 8 and requests.count(expected['work']) == 8
# Each build binds discovery + probe workers, but hydrates the foreign home once.
assert fetches == [str(work)] * 8, fetches
fetches.clear()

# Exception cleanup on reused executor threads, both selected-profile workers.
def boom():
    assert current_secret_scope() is not None and get_hermes_home_override() is not None
    raise RuntimeError('synthetic probe failure')
def clean():
    return current_secret_scope(), get_hermes_home_override()
with ThreadPoolExecutor(max_workers=1) as pool:
    for home in (launch, work, module._prepare_home(launch), module._prepare_home(work)):
        try:
            pool.submit(module._run_in_home, home, boom).result(); raise AssertionError('no raise')
        except RuntimeError as exc:
            assert str(exc) == 'synthetic probe failure'
        assert pool.submit(clean).result() == (None, None)
        try:
            pool.submit(module._run_reset_home, home, boom).result(); raise AssertionError('no raise')
        except RuntimeError as exc:
            assert str(exc) == 'synthetic probe failure'
        assert pool.submit(clean).result() == (None, None)
    # Own launch profile keeps its launch-env credential; foreign sees only its files.
    own = pool.submit(module._run_in_home, launch, lambda: get_secret('ANTHROPIC_TOKEN')).result()
    other = pool.submit(module._run_in_home, work, lambda: get_secret('ANTHROPIC_TOKEN')).result()
    none = pool.submit(module._run_in_home, empty, lambda: get_secret('ANTHROPIC_TOKEN')).result()
    reset = pool.submit(module._run_reset_home, work, lambda: get_secret('ANTHROPIC_TOKEN')).result()
    assert (own, other, none, reset) == (expected['infra'], expected['work'], None, expected['work'])
    # Sibling nested executor in the actual Nous probe inherits the bound selection.
    import hermes_cli.nous_account as nous_account, agent.account_usage as account_usage
    nous_seen = []
    nous_account.get_nous_portal_account_info = lambda **_k: nous_seen.append(
        (get_hermes_home_override(), get_secret('ANTHROPIC_TOKEN'))) or None
    account_usage.build_nous_credits_snapshot = lambda info: None
    q = pool.submit(module._run_in_home, work, module._probe_nous).result()
    assert q['available'] is False and q['source'] == 'portal-account', q
    prepared = module._prepare_home(work)
    q = pool.submit(module._run_in_home, prepared, module._probe_nous).result()
    assert q['available'] is False and q['source'] == 'portal-account', q
    assert nous_seen == [(str(work), expected['work'])] * 2, nous_seen
    # Foreign-home hydrations above: each plain-home standalone operation hydrates
    # once (cleanup _run_in_home + _run_reset_home, other, reset, plain Nous = 5);
    # each _prepare_home(work) hydrates once however many workers bind it (2).
    # Launch/empty homes have no external source configured.
    assert fetches == [str(work)] * 7, fetches

set_multiplex_active(False)
after = inventory()
# Every pre-existing fixture file is byte-identical. The host's own fetch path
# (also on the pre-fix plugin whenever the fetch proceeds) seeds SOUL.md, a
# config.yaml.good backup and an OAuth-heal cache marker in homes lacking them;
# none is a credential store, and no auth file may appear.
assert all(after.get(k) == v for k, v in before.items()), 'existing fixture file changed'
import fnmatch
host_seed = ('*/SOUL.md', '*/backups/config/config.yaml.good.*', '*/cache/oauth_heal_clean.json')
new = sorted(set(after) - set(before))
assert all(any(fnmatch.fnmatch(p, g) for g in host_seed) for p in new), new
assert not any(n in ('auth.json', '.anthropic_oauth.json', '.env') for p in new for n in [Path(p).name]), new
assert os.environ['ANTHROPIC_TOKEN'] == expected['infra']  # no global env mutation
print('installed host: scoped workers reached mocked quota GET; no bleed, no writes')
'''


def _fixture_homes(tmp_path):
    launch = tmp_path / '.hermes' / 'profiles' / 'infra'
    work = tmp_path / '.hermes' / 'profiles' / 'work'
    empty = tmp_path / '.hermes' / 'profiles' / 'empty'
    for home in (launch, work, empty):
        home.mkdir(parents=True)
        (home / 'config.yaml').write_text('model:\n  provider: anthropic\n')
    # Conflicting secrets: the launch token exists only in the launch process env
    # (systemd / op-run style); the foreign token only in that profile's .env.
    (launch / '.env').write_text('OTHER_SETTING=launch\n')
    (work / '.env').write_text('ANTHROPIC_TOKEN=sk-ant-oat01-work-file-fixture\n')
    (empty / '.env').write_text('OTHER_SETTING=empty\n')
    # Enables the installed-host check's synthetic failing source for this profile only.
    (work / 'config.yaml').write_text('model:\n  provider: anthropic\nsecrets:\n  counter_fixture:\n    enabled: true\n')
    return launch, work, empty


def test_installed_host_scoped_quota_workers(tmp_path):
    if not HERMES_PYTHON.is_file():
        pytest.skip('installed Hermes interpreter unavailable')
    launch, work, empty = _fixture_homes(tmp_path)
    env = {'HOME': str(tmp_path), 'HERMES_HOME': str(launch), 'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
           'ANTHROPIC_TOKEN': 'sk-ant-oat01-launch-env-fixture', 'HERMES_USAGE_PRICING_OFFLINE': '1',
           'NO_PROXY': '*', 'PYTHONPATH': str(HERMES_PYTHON.parents[2]), 'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run([str(HERMES_PYTHON), '-c', INSTALLED_HOST_CHECK, str(launch), str(work), str(empty),
                             str(ROOT / 'dashboard' / 'plugin_api.py')],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    assert 'installed host: scoped workers reached mocked quota GET' in result.stdout


# --- host-version compatibility (fake host modules, no Hermes install) -------

@pytest.fixture
def plugin(monkeypatch, tmp_path):
    constants = ModuleType('hermes_constants')
    state = {'override': None}
    def set_override(path):
        previous = state['override']; state['override'] = path; return previous
    def reset_override(token):
        state['override'] = token
    constants.set_hermes_home_override = set_override
    constants.reset_hermes_home_override = reset_override
    constants.get_hermes_home = lambda: Path(state['override'] or tmp_path / 'launch')
    constants.get_process_hermes_home = lambda: tmp_path / 'launch'
    monkeypatch.setitem(sys.modules, 'hermes_constants', constants)
    agent = ModuleType('agent')
    agent.__path__ = []
    monkeypatch.setitem(sys.modules, 'agent', agent)
    monkeypatch.delitem(sys.modules, 'agent.secret_scope', raising=False)
    spec = importlib.util.spec_from_file_location('fixture_scope_compat', ROOT / 'dashboard' / 'plugin_api.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, state, tmp_path


def test_legacy_host_without_secret_scope_binds_home_only(plugin):
    module, state, tmp_path = plugin
    seen = module._run_in_home(tmp_path / 'work', lambda: state['override'])
    assert Path(seen) == tmp_path / 'work'
    assert state['override'] is None


def test_partial_secret_scope_api_fails_closed(plugin, monkeypatch):
    module, state, tmp_path = plugin
    partial = ModuleType('agent.secret_scope')  # guard module present, binding API absent
    monkeypatch.setitem(sys.modules, 'agent.secret_scope', partial)
    called = []
    with pytest.raises(RuntimeError, match='Selected profile credential scope unavailable'):
        module._run_in_home(tmp_path / 'work', lambda: called.append(True))
    assert called == [] and state['override'] is None


@pytest.mark.parametrize('multiplexed', [False, True])
def test_older_host_own_profile_without_launch_policy(plugin, monkeypatch, multiplexed):
    module, state, tmp_path = plugin
    older = ModuleType('agent.secret_scope')
    bound = []
    older.set_secret_scope = lambda secrets: bound.append(secrets)
    older.reset_secret_scope = lambda token: None
    older.build_profile_secret_scope = lambda home: {}
    older.is_multiplex_active = lambda: multiplexed
    monkeypatch.setitem(sys.modules, 'agent.secret_scope', older)
    monkeypatch.setitem(sys.modules, 'tui_gateway', None)  # no launch_profile_policy
    own = tmp_path / 'launch'
    own.mkdir()
    if multiplexed:  # an unscoped read would be refused: do not run the probe
        with pytest.raises(RuntimeError, match='Selected profile credential scope unavailable'):
            module._run_in_home(own, lambda: pytest.fail('ran unscoped'))
    else:  # single-profile: unscoped reads are the launch env, as before
        assert Path(module._run_in_home(own, lambda: state['override'])) == own
    assert bound == [] and state['override'] is None


# --- route-level partial host: GET /usage renders unavailable, never 500 ------

def _seed_activity(home, providers):
    import sqlite3
    import time
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / 'state.db') as conn:
        conn.execute('CREATE TABLE session_model_usage (billing_provider TEXT, last_seen REAL)')
        conn.executemany('INSERT INTO session_model_usage VALUES (?, ?)',
                         [(p, time.time() - 60) for p in providers])


@pytest.fixture
def partial_route(plugin, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    module, state, tmp_path = plugin
    monkeypatch.setitem(sys.modules, 'agent.secret_scope', ModuleType('agent.secret_scope'))
    work = tmp_path / 'work'
    monkeypatch.setattr(module, '_profile_rows', lambda: [
        {'name': 'work', 'path': str(work), 'is_default': False, 'is_server': False, 'gateway_running': False}])
    touched = []
    def forbidden(name):
        def call(*_a, **_k):
            touched.append(name)
            raise AssertionError(name + ' reached without a credential scope')
        return call
    monkeypatch.setattr(module, '_configured_providers', forbidden('credential discovery'))
    monkeypatch.setattr(module, '_PROBES', {pid: forbidden('probe ' + pid) for pid in module._PROBES})
    monkeypatch.setattr(module, '_ledger_store', forbidden('quota snapshot store'))
    module._cache.clear()
    app = FastAPI()
    app.include_router(module.router)
    return module, state, work, touched, TestClient(app, raise_server_exceptions=False)


def test_partial_host_usage_route_renders_active_providers_unavailable(partial_route):
    module, state, work, touched, client = partial_route
    _seed_activity(work, ['anthropic', 'deepseek', ''])
    before = (work / 'state.db').read_bytes()
    response = client.get('/usage', params={'profile': 'work', 'refresh': 'true'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['profile'] == 'work' and body['cached'] is False
    assert body['credential_discovery']['status'] == 'unavailable'
    assert [p['id'] for p in body['providers']] == ['anthropic', 'deepseek']
    for provider in body['providers']:
        assert provider['active'] is True and provider['configured'] is None
        quota = provider['quota']
        assert quota['available'] is False and quota['source'] == 'scope_unavailable'
        assert 'no credential was accessed' in quota['unavailable_reason']
    assert touched == [] and state['override'] is None
    assert (work / 'state.db').read_bytes() == before


def test_partial_host_usage_route_without_activity_is_not_empty_success(partial_route):
    module, state, work, touched, client = partial_route
    work.mkdir()  # no state.db: provider set unknown, must not read as "no providers"
    response = client.get('/usage', params={'profile': 'work', 'refresh': 'true'})
    assert response.status_code == 503
    assert 'credential scope unavailable' in response.json()['detail']
    assert touched == [] and state['override'] is None and module._cache == {}


def test_activity_discovery_binds_home_without_secret_authority(plugin, monkeypatch):
    module, state, tmp_path = plugin
    full = ModuleType('agent.secret_scope')
    calls = []
    full.set_secret_scope = lambda *a, **k: calls.append('bind') or 'token'
    full.reset_secret_scope = lambda token: calls.append('reset')
    full.build_profile_secret_scope = lambda home: calls.append('build') or {}
    monkeypatch.setitem(sys.modules, 'agent.secret_scope', full)
    env_loader = ModuleType('hermes_cli.env_loader')
    env_loader.hydrate_profile_secret_sources = lambda home: calls.append('hydrate')
    monkeypatch.setitem(sys.modules, 'hermes_cli', ModuleType('hermes_cli'))
    monkeypatch.setitem(sys.modules, 'hermes_cli.env_loader', env_loader)
    work = tmp_path / 'work'
    _seed_activity(work, ['anthropic'])
    active, _ = module._run_in_home_only(work, module._active_providers)
    assert active == {'anthropic'} and calls == [] and state['override'] is None
    module._run_in_home(work, lambda: None)  # contrast: the credential path does bind
    assert calls == ['hydrate', 'build', 'bind', 'reset']


def test_usage_route_does_not_swallow_unrelated_failures(partial_route, monkeypatch):
    module, state, work, touched, client = partial_route
    def unrelated(*_a):
        raise RuntimeError('synthetic unrelated failure')
    monkeypatch.setattr(module, '_build_payload', unrelated)
    monkeypatch.setattr(module, '_scope_unavailable_payload', lambda *_a: pytest.fail('degraded path taken'))
    assert client.get('/usage', params={'profile': 'work', 'refresh': 'true'}).status_code == 500


# --- route-level full host: one hydration per /usage build ------------------

def test_usage_route_hydrates_foreign_scope_once_per_build(plugin, monkeypatch):
    """Failing-source counter: every worker binds, only the build prepares."""
    import threading
    from fastapi.testclient import TestClient
    module, state, tmp_path = plugin
    work = tmp_path / 'work'
    work.mkdir()
    hydrations, binds, lock = [], [], threading.Lock()
    full = ModuleType('agent.secret_scope')
    local = threading.local()
    constants = sys.modules['hermes_constants']  # per-thread, like the host ContextVar
    def set_home(path):
        previous = getattr(local, 'home', None); local.home = path; return previous
    def reset_home(token):
        local.home = token
    monkeypatch.setattr(constants, 'set_hermes_home_override', set_home)
    monkeypatch.setattr(constants, 'reset_hermes_home_override', reset_home)
    monkeypatch.setattr(constants, 'get_hermes_home', lambda: Path(getattr(local, 'home', None) or tmp_path / 'launch'))
    def set_scope(secrets, *, profile_home=None):
        with lock:
            binds.append((dict(secrets), profile_home, threading.get_ident()))
        previous = getattr(local, 'scope', None)
        local.scope = secrets
        return previous
    def reset_scope(token):
        local.scope = token
    full.set_secret_scope, full.reset_secret_scope = set_scope, reset_scope
    full.build_profile_secret_scope = lambda home: {'WORK_TOKEN': 'synthetic-' + Path(home).name}
    monkeypatch.setitem(sys.modules, 'agent.secret_scope', full)
    env_loader = ModuleType('hermes_cli.env_loader')
    def failing_hydrate(home):  # stands in for a failing external helper: never cached
        with lock:
            hydrations.append(Path(home))
        return {}
    env_loader.hydrate_profile_secret_sources = failing_hydrate
    monkeypatch.setitem(sys.modules, 'hermes_cli', ModuleType('hermes_cli'))
    monkeypatch.setitem(sys.modules, 'hermes_cli.env_loader', env_loader)
    monkeypatch.setattr(module, '_profile_rows', lambda: [
        {'name': 'work', 'path': str(work), 'is_default': False, 'is_server': False, 'gateway_running': False}])
    _seed_activity(work, sorted(module._QUOTA_CAPABLE))
    seen = []
    def probe(pid):
        def run():
            scope = local.scope
            with lock:
                seen.append((pid, Path(local.home), dict(scope)))
            scope['LEAK'] = pid  # host config mirror writes into the bound scope
            return module._unavailable('synthetic', source='fixture')
        return run
    monkeypatch.setattr(module, '_configured_providers', lambda: set())
    monkeypatch.setattr(module, '_PROBES', {pid: probe(pid) for pid in module._PROBES})
    monkeypatch.setattr(module, '_ledger_store', None)
    module._cache.clear()
    client = TestClient(_app(module))  # probes run on the route's real executor
    for request in (1, 2):
        response = client.get('/usage', params={'profile': 'work', 'refresh': 'true'})
        assert response.status_code == 200, response.text
        assert 'credential_discovery' not in response.json()
        # One build: one hydration, then discovery + 11 probe workers bind it.
        assert hydrations == [work] * request
        assert len(binds) == 12 * request
    assert sorted(pid for pid, _, _ in seen) == sorted(list(module._QUOTA_CAPABLE) * 2)
    # Every worker saw the selected home and a fresh copy of the prepared scope.
    assert all(home == work and scope == {'WORK_TOKEN': 'synthetic-work'} for _, home, scope in seen)
    assert all(secrets == {'WORK_TOKEN': 'synthetic-work'} and stamp == str(work) for secrets, stamp, _ in binds)
    assert getattr(local, 'home', None) is None and getattr(local, 'scope', None) is None
    assert state['override'] is None
    # A cached response does not bind or hydrate; refresh re-prepares (no stale scope kept).
    assert client.get('/usage', params={'profile': 'work'}).json()['cached'] is True
    assert hydrations == [work] * 2 and len(binds) == 24


def _app(module):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(module.router)
    return app


def test_prepared_scope_failure_degrades_before_any_worker(partial_route, monkeypatch):
    """Partial API found once at preparation: truthful degraded payload, nothing bound."""
    module, state, work, touched, client = partial_route
    prepared = []
    original = module._prepare_home
    monkeypatch.setattr(module, '_prepare_home', lambda home: prepared.append(home) or original(home))
    _seed_activity(work, ['anthropic'])
    body = client.get('/usage', params={'profile': 'work', 'refresh': 'true'}).json()
    assert prepared == [work] and touched == [] and state['override'] is None
    assert body['credential_discovery']['status'] == 'unavailable'
    assert [(p['id'], p['configured'], p['quota']['source']) for p in body['providers']] == \
        [('anthropic', None, 'scope_unavailable')]


def test_standalone_reset_binding_prepares_its_own_scope(plugin, monkeypatch):
    module, state, tmp_path = plugin
    full = ModuleType('agent.secret_scope')
    calls = []
    full.set_secret_scope = lambda secrets, *, profile_home=None: calls.append(('bind', profile_home)) or 'token'
    full.reset_secret_scope = lambda token: calls.append(('reset', token))
    full.build_profile_secret_scope = lambda home: {}
    monkeypatch.setitem(sys.modules, 'agent.secret_scope', full)
    env_loader = ModuleType('hermes_cli.env_loader')
    env_loader.hydrate_profile_secret_sources = lambda home: calls.append(('hydrate', str(home)))
    monkeypatch.setitem(sys.modules, 'hermes_cli', ModuleType('hermes_cli'))
    monkeypatch.setitem(sys.modules, 'hermes_cli.env_loader', env_loader)
    work = tmp_path / 'work'
    work.mkdir()
    for _ in range(2):  # two separate operations: each prepares, binds and resets once
        assert Path(module._run_reset_home(work, lambda: state['override'])) == work
    one = [('hydrate', str(work)), ('bind', str(work)), ('reset', 'token')]
    assert calls == one * 2 and state['override'] is None
