"""Anthropic OAuth ``limits[]`` windows (e.g. the model-scoped Fable week).

Fake host modules and an ``httpx`` MockTransport only: no Hermes install,
account, network or live home. The fake token resolver reads the selected
profile's *bound* secret scope, so a request carrying a token proves the
plugin used the per-profile scope that ``_run_in_home`` binds.
"""
import importlib.util
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
WORK_TOKEN = 'sk-ant-oat01-synthetic-work'
LAUNCH_TOKEN = 'sk-ant-oat01-synthetic-launch'

LIMITS = [
    {'kind': 'session', 'group': 'session', 'percent': 12, 'severity': 'normal',
     'resets_at': '2030-01-01T00:49:59.891946+00:00', 'scope': None, 'is_active': False},
    {'kind': 'weekly_all', 'group': 'weekly', 'percent': 16, 'severity': 'normal',
     'resets_at': '2030-01-02T17:59:59.891967+00:00', 'scope': None, 'is_active': True},
    {'kind': 'weekly_scoped', 'group': 'weekly', 'percent': 15, 'severity': 'normal',
     'resets_at': '2030-01-02T17:59:59.892117+00:00',
     'scope': {'model': {'id': None, 'display_name': 'Fable'}, 'surface': None}, 'is_active': False},
]


def _payload(limits):
    body = {'five_hour': {'utilization': 12.0, 'resets_at': '2030-01-01T00:49:59+00:00'},
            'seven_day': {'utilization': 16.0, 'resets_at': '2030-01-02T17:59:59+00:00'},
            'seven_day_opus': None, 'seven_day_sonnet': None,
            'extra_usage': {'is_enabled': False}, 'spend': None, 'some_flag': None}
    if limits is not None:
        body['limits'] = limits
    return body


class Host:
    """Per-thread home/scope like the host ContextVars; records every GET."""

    def __init__(self, tmp_path, monkeypatch):
        self.local = threading.local()
        self.requests = []
        self.response = lambda request: httpx.Response(200, json=_payload(LIMITS))
        self.token_kind = 'oauth'
        self.work = tmp_path / 'work'
        self.work.mkdir()
        local = self.local

        constants = ModuleType('hermes_constants')
        def set_home(path):
            previous = getattr(local, 'home', None); local.home = path; return previous
        def reset_home(token):
            local.home = token
        constants.set_hermes_home_override = set_home
        constants.reset_hermes_home_override = reset_home
        constants.get_hermes_home = lambda: Path(getattr(local, 'home', None) or tmp_path / 'launch')
        constants.get_process_hermes_home = lambda: tmp_path / 'launch'
        monkeypatch.setitem(sys.modules, 'hermes_constants', constants)

        agent = ModuleType('agent'); agent.__path__ = []
        scope = ModuleType('agent.secret_scope')
        def set_scope(secrets, *, profile_home=None):
            # Non-None token, as a host ContextVar.set returns.
            previous = (getattr(local, 'scope', None),); local.scope = secrets; return previous
        def reset_scope(token):
            local.scope = token[0]
        scope.set_secret_scope, scope.reset_secret_scope = set_scope, reset_scope
        scope.build_profile_secret_scope = lambda home: {'CLAUDE_CODE_OAUTH_TOKEN': WORK_TOKEN}
        env_loader = ModuleType('hermes_cli.env_loader')
        env_loader.hydrate_profile_secret_sources = lambda home: None
        hermes_cli = ModuleType('hermes_cli'); hermes_cli.__path__ = []

        adapter = ModuleType('agent.anthropic_adapter')
        host = self
        def resolve_anthropic_token():
            # Scoped read: nothing outside a bound profile scope (no env fallback).
            secrets = getattr(local, 'scope', None)
            if secrets is None:
                raise RuntimeError('unscoped secret read')
            token = secrets.get('CLAUDE_CODE_OAUTH_TOKEN')
            return token if host.token_kind == 'oauth' else 'sk-ant-api03-synthetic-key'
        adapter.resolve_anthropic_token = resolve_anthropic_token
        adapter._is_oauth_token = lambda key: 'sk-ant-api' not in key

        account_usage = ModuleType('agent.account_usage')
        def fetch_account_usage(provider):
            # Stand-in for the host fetcher: legacy keys only, via the same transport.
            assert provider == 'anthropic'
            token = resolve_anthropic_token()
            with httpx.Client(timeout=5) as client:
                client.get(USAGE_URL, headers={'Authorization': f'Bearer {token}',
                                               'anthropic-beta': 'oauth-2025-04-20',
                                               'User-Agent': 'claude-code/2.1.0'})
            window = lambda label, used, reset: SimpleNamespace(
                label=label, used_percent=used, reset_at=datetime.fromisoformat(reset), detail=None)
            return SimpleNamespace(
                available=True, source='oauth_usage_api', title='Account limits', plan='Max',
                windows=(window('Current session', 12.0, '2030-01-01T00:49:59+00:00'),
                         window('Current week', 16.0, '2030-01-02T17:59:59+00:00')),
                details=('Extra usage: disabled',), unavailable_reason=None,
                fetched_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
        account_usage.fetch_account_usage = fetch_account_usage
        for name, module in (('agent', agent), ('agent.secret_scope', scope),
                             ('agent.anthropic_adapter', adapter), ('agent.account_usage', account_usage),
                             ('hermes_cli', hermes_cli), ('hermes_cli.env_loader', env_loader)):
            monkeypatch.setitem(sys.modules, name, module)

        def handler(request):
            assert (request.method, str(request.url)) == ('GET', USAGE_URL), request.url
            self.requests.append({'token': request.headers['authorization'].removeprefix('Bearer '),
                                  'beta': request.headers.get('anthropic-beta'),
                                  'agent': request.headers.get('user-agent'),
                                  'home': getattr(local, 'home', None)})
            return self.response(request)
        spec = importlib.util.spec_from_file_location('fixture_anthropic_limits', ROOT / 'dashboard' / 'plugin_api.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        m = self.module
        monkeypatch.setattr(m, '_profile_rows', lambda: [
            {'name': 'work', 'path': str(self.work), 'is_default': False, 'is_server': False, 'gateway_running': False}])
        monkeypatch.setattr(m, '_active_providers', lambda: ({'anthropic'}, {}))
        monkeypatch.setattr(m, '_configured_providers', lambda: {'anthropic'})
        monkeypatch.setattr(m, '_ledger_store', None)
        m._cache.clear()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI(); app.include_router(m.router)
        self.client = TestClient(app)  # built before the provider transport mock below
        original = httpx.Client.__init__
        def mocked(client, *a, **k):
            k['transport'] = httpx.MockTransport(handler)
            original(client, *a, **k)
        monkeypatch.setattr(httpx.Client, '__init__', mocked)

    def quota(self):
        response = self.client.get('/usage', params={'profile': 'work', 'refresh': 'true'})
        assert response.status_code == 200, response.text
        text = response.text
        assert WORK_TOKEN not in text and LAUNCH_TOKEN not in text  # never serialized
        [anthropic] = [p for p in response.json()['providers'] if p['id'] == 'anthropic']
        return anthropic['quota']


@pytest.fixture
def host(tmp_path, monkeypatch):
    return Host(tmp_path, monkeypatch)


HOST_WINDOWS = [('Current session', 12.0, '2030-01-01T00:49:59+00:00'),
                ('Current week', 16.0, '2030-01-02T17:59:59+00:00')]


def _windows(quota):
    return [(w['label'], w['used_percent'], w['reset_at']) for w in quota['windows']]


def _preserved(quota):
    assert quota['available'] is True and quota['source'] == 'oauth_usage_api' and quota['plan'] == 'Max'
    assert quota['details'] == ['Extra usage: disabled'] and quota['title'] == 'Account limits'
    assert quota['fetched_at'] == '2030-01-01T00:00:00+00:00' and quota['unavailable_reason'] is None


def test_limits_array_supplies_session_week_and_scoped_weeks(host):
    # Reverse the payload order: the DTO order is still session, week, scoped.
    host.response = lambda request: httpx.Response(200, json=_payload(list(reversed(LIMITS))))
    quota = host.quota()
    _preserved(quota)
    assert _windows(quota) == [('Current session', 12.0, '2030-01-01T00:49:59.891946+00:00'),
                               ('Current week', 16.0, '2030-01-02T17:59:59.891967+00:00'),
                               ('Fable week', 15.0, '2030-01-02T17:59:59.892117+00:00')]
    assert [w['remaining_percent'] for w in quota['windows']] == [88.0, 84.0, 85.0]
    # Host GET then the plugin's limits GET: both in the selected profile, its own token, core headers.
    assert [(r['token'], r['beta'], r['agent'], r['home']) for r in host.requests] == \
        [(WORK_TOKEN, 'oauth-2025-04-20', 'claude-code/2.1.0', host.work)] * 2
    assert getattr(host.local, 'scope', None) is None and getattr(host.local, 'home', None) is None


def test_other_scoped_models_follow_the_same_label_rule(host):
    extra = [dict(LIMITS[2], percent=40, scope={'model': {'display_name': 'Opus'}}),
             dict(LIMITS[2], percent=5, scope={'model': {'display_name': None}}),  # unnamed: skipped
             {'kind': 'future_kind', 'percent': 1}]
    host.response = lambda request: httpx.Response(200, json=_payload(LIMITS + extra))
    assert [w['label'] for w in host.quota()['windows']] == \
        ['Current session', 'Current week', 'Fable week', 'Opus week']


@pytest.mark.parametrize('limits', [None, [], [{'kind': 'session', 'percent': None}], 'bad'])
def test_absent_or_empty_limits_keep_host_windows(host, limits):
    host.response = lambda request: httpx.Response(200, json=_payload(limits))
    quota = host.quota()
    _preserved(quota)
    assert _windows(quota) == HOST_WINDOWS


@pytest.mark.parametrize('failure', ['http500', 'http401', 'transport', 'not_json'])
def test_limits_get_failure_keeps_host_snapshot(host, failure):
    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) == 1:  # host fetcher's own GET succeeds
            return httpx.Response(200, json=_payload(None))
        if failure == 'transport':
            raise httpx.ConnectError('synthetic refusal', request=request)
        if failure == 'not_json':
            return httpx.Response(200, text='<html>')
        return httpx.Response(500 if failure == 'http500' else 401, json={})
    host.response = respond
    quota = host.quota()
    _preserved(quota)
    assert _windows(quota) == HOST_WINDOWS and len(calls) == 2


def test_non_oauth_token_makes_no_extra_request(host):
    host.token_kind = 'api_key'
    quota = host.quota()
    assert _windows(quota) == HOST_WINDOWS
    assert len(host.requests) == 1  # host fetcher only; the plugin never sends an API key


def test_unavailable_host_snapshot_is_not_upgraded(host, monkeypatch):
    monkeypatch.setattr(sys.modules['agent.account_usage'], 'fetch_account_usage', lambda provider: None)
    quota = host.quota()
    assert quota['available'] is False and quota['windows'] == [] and host.requests == []


def test_token_resolution_stays_in_profile_scope(host):
    # Outside a bound scope the resolver refuses; the plugin swallows it and sends nothing.
    assert host.module._anthropic_limit_windows() is None and host.requests == []
    # Inside the selected profile's worker binding the same call uses that profile's token.
    windows = host.module._run_in_home(host.work, host.module._anthropic_limit_windows)
    assert [w['label'] for w in windows] == ['Current session', 'Current week', 'Fable week']
    assert [r['token'] for r in host.requests] == [WORK_TOKEN]
    assert WORK_TOKEN not in json.dumps(windows)
    assert getattr(host.local, 'scope', None) is None and getattr(host.local, 'home', None) is None
