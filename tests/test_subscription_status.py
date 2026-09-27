"""The selected-profile subscription card must not infer sign-in from a failed quota read."""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def quota_route(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / 'dashboard' / 'plugin_api.py'
    spec = importlib.util.spec_from_file_location('fixture_subscription_status', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = tmp_path / 'infra'
    monkeypatch.setattr(module, '_profile_rows', lambda: [
        {'name': 'infra', 'path': str(home), 'is_default': False,
         'is_server': True, 'gateway_running': False},
    ])
    monkeypatch.setattr(module, '_run_in_home', lambda selected, fn: fn())
    monkeypatch.setattr(module, '_active_providers', lambda: ({'anthropic'}, {}))
    monkeypatch.setattr(module, '_configured_providers', lambda: set())
    monkeypatch.setattr(module, '_ledger_store', None)
    # No host auth import, credential lookup or provider request: only this inert
    # account-usage result crosses the plugin's existing probe boundary.
    agent = ModuleType('agent')
    account_usage = ModuleType('agent.account_usage')
    monkeypatch.setitem(sys.modules, 'agent', agent)
    monkeypatch.setitem(sys.modules, 'agent.account_usage', account_usage)
    setattr(agent, 'account_usage', account_usage)
    app = FastAPI()
    app.include_router(module.router)
    return module, account_usage, TestClient(app)


@pytest.mark.parametrize('provider', ['anthropic', 'openai-codex', 'openrouter'])
@pytest.mark.parametrize('scenario', ['missing token', 'quota read failed'])
def test_none_does_not_assert_missing_credentials(quota_route, scenario, provider, monkeypatch):
    module, account_usage, client = quota_route
    monkeypatch.setattr(module, '_active_providers', lambda: ({provider}, {}))

    def synthetic_fetch(requested):
        assert requested == provider
        token = None if scenario == 'missing token' else 'synthetic-token'
        if token is None:
            return None
        try:
            raise ConnectionError('synthetic quota failure')
        except ConnectionError:
            return None  # mirrors the host's lossy exception path

    account_usage.fetch_account_usage = synthetic_fetch
    response = client.get('/usage', params={'profile': 'infra', 'refresh': 'true'})
    assert response.status_code == 200
    item = response.json()['providers'][0]
    assert item['id'] == provider
    assert item['quota']['available'] is False
    assert item['quota']['source'] == 'usage_api'
    assert item['quota']['unavailable_reason'] == (
        'Subscription limits unavailable — quota could not be read for this profile.'
    )
    assert 'No credentials' not in item['quota']['unavailable_reason']


def test_successful_account_quota_is_unchanged(quota_route):
    _, account_usage, client = quota_route
    account_usage.fetch_account_usage = lambda provider: SimpleNamespace(
        available=True, source='usage_api', title='Claude limits', plan='Pro',
        windows=[SimpleNamespace(label='Weekly', used_percent=25, reset_at=None, detail=None)],
        details=[], unavailable_reason=None, fetched_at=None,
    )
    item = client.get('/usage', params={'profile': 'infra', 'refresh': 'true'}).json()['providers'][0]
    assert item['quota']['available'] is True
    assert item['quota']['title'] == 'Claude limits'
    assert item['quota']['plan'] == 'Pro'
    assert item['quota']['windows'][0]['remaining_percent'] == 75
    assert item['quota']['unavailable_reason'] is None


def test_explicit_oauth_only_status_is_preserved(quota_route):
    _, account_usage, client = quota_route
    reason = 'Subscription limits require OAuth; an API key cannot read them.'
    account_usage.fetch_account_usage = lambda provider: SimpleNamespace(
        available=False, source='usage_api', title='Claude limits', plan=None,
        windows=[], details=[], unavailable_reason=reason, fetched_at=None,
    )
    item = client.get('/usage', params={'profile': 'infra', 'refresh': 'true'}).json()['providers'][0]
    assert item['quota']['available'] is False
    assert item['quota']['unavailable_reason'] == reason
