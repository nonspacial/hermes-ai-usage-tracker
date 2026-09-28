"""Anthropic OAuth ``limits[]`` windows (e.g. the model-scoped Fable week).

Fake host modules and an ``httpx`` MockTransport only: no Hermes install,
account, network or live home. The fake token resolver reads the selected
profile's *bound* secret scope, so a request carrying a token proves the
plugin used the per-profile scope that ``_run_in_home`` binds.
"""
import contextvars
import importlib.util
import json
import sys
import threading
import time
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


class _Bound:
    """Home/scope held in ContextVars, as the host holds them (copied contexts carry them)."""

    def __init__(self):
        object.__setattr__(self, '_vars', {'home': contextvars.ContextVar('home', default=None),
                                           'scope': contextvars.ContextVar('scope', default=None)})

    def __getattr__(self, name):
        return self._vars[name].get()

    def __setattr__(self, name, value):
        self._vars[name].set(value)


class Host:
    """Per-context home/scope like the host ContextVars; records every GET."""

    def __init__(self, tmp_path, monkeypatch):
        self.local = _Bound()
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


FABLE = ('Fable week', 15.0, '2030-01-02T17:59:59.892117+00:00')
SESSION = ('Current session', 12.0, '2030-01-01T00:49:59.891946+00:00')
WEEK = ('Current week', 16.0, '2030-01-02T17:59:59.891967+00:00')


def _raw_response(limits):
    # Python's JSON encoder writes NaN/Infinity tokens, which response.json() parses back.
    return httpx.Response(200, content=json.dumps(_payload(limits)).encode(),
                          headers={'content-type': 'application/json'})


def _limit(kind, percent, **extra):
    base = {'session': LIMITS[0], 'weekly_all': LIMITS[1], 'weekly_scoped': LIMITS[2]}.get(kind, LIMITS[1])
    return dict(base, kind=kind, percent=percent, **extra)


# (limits[] payload, expected windows). Host windows are the baseline and are
# never dropped; only fully usable entries override (by label) or append.
MERGE_CASES = {
    # Reviewer repro: bad session + unknown week kind + valid Fable.
    'partial_only_fable_usable': (
        [_limit('session', None), _limit('weekly_all_v2', 30), LIMITS[2]], HOST_WINDOWS + [FABLE]),
    'nan_session': ([_limit('session', float('nan'))], HOST_WINDOWS),
    'inf_session_nan_week': ([_limit('session', float('inf')), _limit('weekly_all', float('-inf')), LIMITS[2]],
                             HOST_WINDOWS + [FABLE]),
    'string_percent': ([_limit('session', '12'), _limit('weekly_scoped', '15')], HOST_WINDOWS),
    'bool_percent': ([_limit('session', True)], HOST_WINDOWS),
    'huge_int_percent_drops_only_itself': ([_limit('session', 10**400), LIMITS[2]], HOST_WINDOWS + [FABLE]),
    'negative_percent': ([_limit('session', -1), LIMITS[2]], HOST_WINDOWS + [FABLE]),
    'over_100_percent': ([_limit('weekly_all', 100.5), LIMITS[2]], HOST_WINDOWS + [FABLE]),
    'bounds_0_and_100': ([_limit('session', 0), _limit('weekly_all', 100)],
                         [('Current session', 0.0, SESSION[2]), ('Current week', 100.0, WEEK[2])]),
    'unknown_kind_only': ([_limit('weekly_all_v2', 30), {'kind': None, 'percent': 3}], HOST_WINDOWS),
    'not_dicts': ([None, 3, 'session', ['x']], HOST_WINDOWS),
    'duplicate_kinds_first_usable_wins': (
        [_limit('session', float('nan')), _limit('session', 20), _limit('session', 30),
         LIMITS[2], dict(LIMITS[2], percent=99)],
        [('Current session', 20.0, SESSION[2]), HOST_WINDOWS[1], FABLE]),
    'missing_display_name': (
        [dict(LIMITS[2], scope=None), dict(LIMITS[2], scope={'model': None}),
         dict(LIMITS[2], scope={'model': {'display_name': '  '}}), dict(LIMITS[2], scope={'model': {}}),
         dict(LIMITS[2], scope='Fable')], HOST_WINDOWS),
    'unparseable_reset_rejected': ([_limit('session', 50, resets_at='soon'), _limit('weekly_all', 50, resets_at=[])],
                                   HOST_WINDOWS),
    'epoch_resets_at': ([_limit('session', 40, resets_at=1893458999), _limit('weekly_scoped', 15, resets_at=1893592799.5)],
                        [('Current session', 40.0, '2030-01-01T00:49:59+00:00'), HOST_WINDOWS[1],
                         ('Fable week', 15.0, '2030-01-02T13:59:59.500000+00:00')]),
    'non_finite_epoch_rejected': ([_limit('session', 40, resets_at=float('nan'))], HOST_WINDOWS),
    'null_reset_keeps_host_reset': ([_limit('session', 40, resets_at=None), _limit('weekly_scoped', 15, resets_at=None)],
                                    [('Current session', 40.0, HOST_WINDOWS[0][2]), HOST_WINDOWS[1],
                                     ('Fable week', 15.0, None)]),
    'z_suffix_reset': ([_limit('weekly_all', 16, resets_at='2030-01-02T17:59:59Z')],
                       [HOST_WINDOWS[0], ('Current week', 16.0, '2030-01-02T17:59:59+00:00')]),
    'scoped_weeks_in_payload_order': (
        [dict(LIMITS[2], percent=40, scope={'model': {'display_name': 'Opus'}}), LIMITS[1], LIMITS[2], LIMITS[0]],
        [SESSION, WEEK, ('Opus week', 40.0, FABLE[2]), FABLE]),
}


@pytest.mark.parametrize('limits,expected', MERGE_CASES.values(), ids=MERGE_CASES.keys())
def test_limits_merge_never_drops_host_windows(host, limits, expected):
    host.response = lambda request: _raw_response(limits)
    quota = host.quota()
    _preserved(quota)
    assert _windows(quota) == expected
    for window in quota['windows']:
        assert window['remaining_percent'] == 100.0 - window['used_percent']
    assert len(host.requests) == 2


def test_scoped_week_overrides_host_window_of_the_same_label(host, monkeypatch):
    # A host legacy ``seven_day_opus`` window is overridden in place, never duplicated.
    fetch = sys.modules['agent.account_usage'].fetch_account_usage
    def with_opus(provider):
        snapshot = fetch(provider)
        opus = SimpleNamespace(label='Opus week', used_percent=70.0, reset_at=None, detail='host detail')
        return SimpleNamespace(**{**vars(snapshot), 'windows': snapshot.windows + (opus,)})
    monkeypatch.setattr(sys.modules['agent.account_usage'], 'fetch_account_usage', with_opus)
    opus = dict(LIMITS[2], percent=40, scope={'model': {'display_name': 'Opus'}})
    host.response = lambda request: httpx.Response(200, json=_payload([LIMITS[2], opus]))
    quota = host.quota()
    assert _windows(quota) == HOST_WINDOWS + [('Opus week', 40.0, FABLE[2]), FABLE]
    assert quota['windows'][2]['detail'] == 'host detail'


@pytest.mark.parametrize('limits', [None, [], [{'kind': 'session', 'percent': None}], 'bad',
                                    [_limit('session', float('nan')), _limit('weekly_all_v2', 3)]])
def test_absent_or_unusable_limits_serve_byte_identical_host_dto(host, limits):
    host.response = lambda request: _raw_response(limits)
    quota = host.quota()
    _preserved(quota)
    assert _windows(quota) == HOST_WINDOWS
    assert len(host.requests) == 2  # the limits GET ran, then changed nothing
    # main's Anthropic probe was ``_probe_standard('anthropic')`` (preserved, unchanged).
    main = host.module._run_in_home(host.work, lambda: host.module._probe_standard('anthropic'))
    assert json.dumps(quota, sort_keys=True) == json.dumps(main, sort_keys=True)


@pytest.mark.parametrize('failure', ['http500', 'http401', 'transport', 'not_json', 'read_timeout'])
def test_limits_get_failure_keeps_host_snapshot(host, failure):
    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) == 1:  # host fetcher's own GET succeeds
            return httpx.Response(200, json=_payload(None))
        if failure == 'transport':
            raise httpx.ConnectError('synthetic refusal', request=request)
        if failure == 'read_timeout':
            raise httpx.ReadTimeout('synthetic read timeout', request=request)
        if failure == 'not_json':
            return httpx.Response(200, text='<html>')
        return httpx.Response(500 if failure == 'http500' else 401, json={})
    host.response = respond
    quota = host.quota()
    _preserved(quota)
    assert _windows(quota) == HOST_WINDOWS and len(calls) == 2


def test_hanging_limits_get_is_bounded_by_the_remaining_probe_budget(host, monkeypatch):
    # The second GET gets only what is left of _PROBE_TIMEOUT; host windows return on time.
    monkeypatch.setattr(host.module, '_PROBE_TIMEOUT', 0.6)
    release = threading.Event()
    calls = []
    def respond(request):
        calls.append(time.monotonic())
        if len(calls) == 1:
            time.sleep(0.2)  # host fetch uses part of the budget
            return httpx.Response(200, json=_payload(LIMITS))
        release.wait(10)  # stalled limits response (released at teardown)
        raise httpx.ReadTimeout('synthetic stalled read', request=request)
    host.response = respond
    started = time.monotonic()
    try:
        quota = host.quota()
        elapsed = time.monotonic() - started
    finally:
        release.set()
    _preserved(quota)
    assert _windows(quota) == HOST_WINDOWS and len(calls) == 2
    assert elapsed < 0.6 + 0.5, elapsed  # within budget plus test overhead, not host + full timeout


def test_exhausted_budget_skips_the_limits_get(host, monkeypatch):
    monkeypatch.setattr(host.module, '_PROBE_TIMEOUT', 0.2)
    def respond(request):
        time.sleep(0.3)
        return httpx.Response(200, json=_payload(LIMITS))
    host.response = respond
    assert _windows(host.quota()) == HOST_WINDOWS and len(host.requests) == 1


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
    assert host.module._anthropic_limits(5.0) == [] and host.requests == []
    # Inside the selected profile's worker binding the same call uses that profile's token,
    # including on the budget-bounded helper thread (context copied, home recorded).
    limits = host.module._run_in_home(host.work, lambda: host.module._anthropic_limits(5.0))
    assert [limit.label for limit in limits] == ['Current session', 'Current week', 'Fable week']
    assert [(r['token'], r['home']) for r in host.requests] == [(WORK_TOKEN, host.work)]
    assert WORK_TOKEN not in json.dumps(limits)
    assert getattr(host.local, 'scope', None) is None and getattr(host.local, 'home', None) is None
