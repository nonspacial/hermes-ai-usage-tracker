"""Auxiliary provider resolution: placeholder labels from Hermes core resolve to
the concrete provider of the client the relay funnel actually received.

Synthetic module/context/clients and a disposable ledger only; no core import,
no network, no live ledger. The fake module mirrors core's
``_RELAY_AUX_CALL_CONTEXT`` contextvar populated by ``_set_relay_auxiliary_route``.
"""
import asyncio
import contextvars
import json
import time
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from _hermes_ai_usage_ledger_v2 import adapters as ad, recorder as r


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(ad, 'install', lambda: None)
    monkeypatch.setattr(r, 'health', lambda *a, **kw: None)
    monkeypatch.setattr(r, '_STORES', {})
    tokens = r.CURRENT.set(None), r.AUX.set(None)
    yield tmp_path
    r.CURRENT.reset(tokens[0])
    r.AUX.reset(tokens[1])


@pytest.fixture
def priced(env):
    import test_anthropic_pricing as T  # synthetic catalogue, no network
    T.insert(r.store(str(env)), T.catalog(), time.time() - 10)
    return env


class AnthropicAuxiliaryClient:
    """Name and ``base_url`` shape of core's wrapper (class name is a signal)."""
    def __init__(self, base_url=None):
        if base_url is not None:
            self.base_url = base_url


class AsyncAnthropicAuxiliaryClient(AnthropicAuxiliaryClient):
    pass


class CodexAuxiliaryClient:
    """Core reuses this wrapper for several providers: its name is no signal."""
    def __init__(self, base_url=None):
        if base_url is not None:
            self.base_url = base_url


class PlainClient:
    def __init__(self, base_url=None):
        if base_url is not None:
            self.base_url = base_url


class Unreadable:
    @property
    def base_url(self):
        raise RuntimeError('no endpoint')


def module(label, task='title_generation', api_mode='anthropic_messages'):
    var = contextvars.ContextVar('auxiliary_relay_call', default=None)
    var.set({'task': task, 'provider': label, 'model': 'claude-opus-9-5', 'api_mode': api_mode})
    return NS(_RELAY_AUX_CALL_CONTEXT=var, _RUNTIME_MAIN_CONTEXT=contextvars.ContextVar('rt', default=None))


def run(env, label, client, *, options=None, asynchronous=False, response=None):
    """Drive the installed relay funnel wrapper exactly as core calls it."""
    response = response or NS(model='claude-opus-9-5', usage=NS(prompt_tokens=10, completion_tokens=2))
    mod = module(label)
    if asynchronous:
        async def funnel(client, kwargs, *, provider=None, api_mode=None, create=None):
            return response
        mod._relay_async_completion = funnel
        mod._relay_sync_completion = lambda client, kwargs, **kw: None
    else:
        def funnel(client, kwargs, *, provider=None, api_mode=None, create=None):
            return response
        mod._relay_sync_completion = funnel
        mod._relay_async_completion = lambda client, kwargs, **kw: None
    ad.install_aux(mod)
    relay = mod._relay_async_completion if asynchronous else mod._relay_sync_completion
    # Core passes provider=resolved_provider; the context carries the same label.
    call = relay(client, {'model': 'claude-opus-9-5', 'messages': ['AUX SECRET']}, **(options or {}))
    assert (asyncio.run(call) if asynchronous else call) is response
    with r.store(str(env)).db() as c:
        [(raw,)] = c.execute("SELECT data FROM requests WHERE json_extract(data,'$.source')='auxiliary_adapter'").fetchall()
        c.execute('DELETE FROM requests')
    for secret in ('AUX SECRET', 'base_url', 'api.anthropic.com', 'chatgpt.com', 'gw.example', 'openrouter.ai', 'x.ai'):
        assert secret not in raw
    return json.loads(raw)


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('base_url', ['https://api.anthropic.com', 'https://api.anthropic.com./'])
def test_auto_label_with_anthropic_client_resolves_and_prices(priced, base_url, asynchronous):
    client = AsyncAnthropicAuxiliaryClient(base_url) if asynchronous else AnthropicAuxiliaryClient(base_url)
    native = {'id': 'msg', 'model': 'claude-opus-9-5', 'usage': {
        'input_tokens': 1000, 'output_tokens': 500, 'cache_read_input_tokens': 20000, 'cache_creation_input_tokens': 3000,
        'cache_creation': {'ephemeral_5m_input_tokens': 1000, 'ephemeral_1h_input_tokens': 2000},
        'speed': 'standard', 'inference_geo': 'global', 'service_tier': 'standard'}}
    create_message = ad.anthropic_wrapper(lambda client, api_kwargs, **kw: native)
    response = NS(model='claude-opus-9-5', usage=NS(prompt_tokens=1000, completion_tokens=500))
    mod = module('auto')
    if asynchronous:
        async def funnel(client, kwargs, *, provider=None, api_mode=None, create=None):
            create_message(client, {}); return response
        mod._relay_async_completion = funnel; mod._relay_sync_completion = lambda c, k, **kw: None
    else:
        def funnel(client, kwargs, *, provider=None, api_mode=None, create=None):
            create_message(client, {}); return response
        mod._relay_sync_completion = funnel; mod._relay_async_completion = lambda c, k, **kw: None
    ad.install_aux(mod)
    relay = mod._relay_async_completion if asynchronous else mod._relay_sync_completion
    call = relay(client, {'model': 'claude-opus-9-5', 'messages': []}, provider='auto', api_mode='anthropic_messages')
    assert (asyncio.run(call) if asynchronous else call) is response
    with r.store(str(priced)).db() as c:
        [(raw,)] = c.execute('SELECT data FROM requests').fetchall()
    record = json.loads(raw)
    assert record['provider'] == 'anthropic' and record['requested_provider'] == 'auto'
    assert record['anthropic_endpoint'] == 'first_party' and record['task'] == 'title_generation'
    assert record['usage']['usage_source'] == 'native_anthropic_usage'
    assert Decimal(record['cost']['total_usd']) == Decimal('0.039') and record['cost']['complete']
    assert 'api.anthropic.com' not in raw


@pytest.mark.parametrize('label,client,provider', [
    ('auto', PlainClient('https://chatgpt.com/backend-api/codex'), 'openai-codex'),
    ('auto', CodexAuxiliaryClient('https://chatgpt.com/backend-api/codex/'), 'openai-codex'),
    ('main', PlainClient('https://api.openai.com/v1'), 'openai'),
    ('auxiliary', PlainClient('https://api.individual.githubcopilot.com'), 'copilot'),
    ('custom', PlainClient('https://api.x.ai/v1'), 'xai'),
    ('', PlainClient('https://openrouter.ai/api/v1'), 'openrouter'),
    ('unknown', PlainClient('https://OPENROUTER.AI./api/v1'), 'openrouter'),
])
def test_placeholder_labels_classify_client_hostname(env, label, client, provider):
    record = run(env, label, client)
    assert record['provider'] == provider
    assert record['requested_provider'] == (label or 'unknown')
    assert 'anthropic_endpoint' not in record


@pytest.mark.parametrize('label,provider', [
    ('fallback_chain[1](anthropic)', 'anthropic'),
    ('fallback_providers[0](openrouter)', 'openrouter'),
    ('main-agent(openai-codex)', 'openai-codex'),
])
def test_fallback_label_is_unwrapped_like_core(env, label, provider):
    # The unwrapped id wins over the hostname: the label is core's own claim.
    record = run(env, label, PlainClient('https://gw.example/v1'))
    assert record['provider'] == provider and record['requested_provider'] == label
    if provider == 'anthropic':
        assert record['anthropic_endpoint'] == 'custom'  # gateway stays unpriced


def test_fallback_label_with_placeholder_inside_falls_through_to_client(env):
    record = run(env, 'fallback_chain[0](auto)', PlainClient('https://api.anthropic.com'))
    assert record['provider'] == 'anthropic' and record['anthropic_endpoint'] == 'first_party'
    assert record['requested_provider'] == 'fallback_chain[0](auto)'


@pytest.mark.parametrize('client', [None, object(), Unreadable(), PlainClient(), PlainClient(''), PlainClient(42),
                                    PlainClient('https://gw.example/anthropic'), PlainClient('not a url'),
                                    PlainClient('https://api.anthropic.com.evil.example'),
                                    PlainClient('https://notopenrouter.ai'), PlainClient('https://fox.ai'),
                                    PlainClient('https://githubcopilot.com.evil.example'),
                                    PlainClient('https://[::1'), CodexAuxiliaryClient()])
def test_unresolvable_client_keeps_reported_label(env, client):
    record = run(env, 'auto', client)
    assert record['provider'] == 'auto'
    assert 'requested_provider' not in record and 'anthropic_endpoint' not in record


class UnreadableAnthropicAuxiliaryClient(Unreadable):
    pass


UnreadableAnthropicAuxiliaryClient.__name__ = 'AnthropicAuxiliaryClient'


@pytest.mark.parametrize('client', [AnthropicAuxiliaryClient(), AsyncAnthropicAuxiliaryClient(),
                                    AnthropicAuxiliaryClient(''), UnreadableAnthropicAuxiliaryClient()])
def test_anthropic_class_is_secondary_signal_only_when_endpoint_unreadable(env, client):
    record = run(env, 'auto', client)
    assert record['provider'] == 'anthropic' and record['requested_provider'] == 'auto'
    assert record['anthropic_endpoint'] == 'unverified'  # never valued at first-party rates


def test_anthropic_class_with_readable_custom_endpoint_is_not_guessed(env):
    record = run(env, 'auto', AnthropicAuxiliaryClient('https://gw.example/anthropic'))
    assert record['provider'] == 'auto' and 'requested_provider' not in record


@pytest.fixture(scope='module')
def head_adapters():
    """The pre-change adapters (main cdc2f7c) loaded beside the current package."""
    import subprocess, sys, types
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    try:
        source = subprocess.run(['git', '-C', str(root), 'show', 'cdc2f7c:ledger_runtime/adapters.py'],
                                check=True, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip('reference commit unavailable outside the repository')
    name = '_hermes_ai_usage_ledger_v2._aux_reference_adapters'
    module = types.ModuleType(name)
    module.__package__ = '_hermes_ai_usage_ledger_v2'
    sys.modules[name] = module
    exec(compile(source, 'cdc2f7c:ledger_runtime/adapters.py', 'exec'), vars(module))
    yield module
    sys.modules.pop(name, None)


@pytest.mark.parametrize('label,client', [
    ('anthropic', PlainClient('https://chatgpt.com')),            # concrete: client never consulted
    ('anthropic', AnthropicAuxiliaryClient('https://api.anthropic.com')),
    ('openrouter', PlainClient('https://api.anthropic.com')),
    ('nous', None),
    ('custom:work-gateway', PlainClient('https://api.openai.com/v1')),
    ('auto', PlainClient('https://gw.example/v1')),                # unresolvable placeholder: unchanged
    ('auto', Unreadable()),
])
def test_unresolved_rows_are_byte_identical_to_pre_change(env, monkeypatch, head_adapters, label, client):
    import uuid
    rows = []
    for impl in (head_adapters, ad):
        counter = iter(range(1, 100))
        monkeypatch.setattr(uuid, 'uuid4', lambda: uuid.UUID(int=next(counter)))
        monkeypatch.setattr(time, 'time', lambda: 1_700_000_000.0)
        monkeypatch.setattr(ad, 'install_aux', impl.install_aux)
        rows.append(json.dumps(run(env, label, client, options={'provider': label, 'api_mode': 'anthropic_messages'}),
                               sort_keys=False))
    assert rows[0] == rows[1]
    assert json.loads(rows[1])['provider'] == label and 'requested_provider' not in rows[1]


def test_options_provider_takes_precedence_then_resolves(env):
    # Core passes provider= explicitly; a placeholder there resolves the same way.
    record = run(env, 'openrouter', PlainClient('https://api.anthropic.com'), options={'provider': 'auto'})
    assert record['provider'] == 'anthropic' and record['requested_provider'] == 'auto'


def test_resolution_failure_fails_closed(env, monkeypatch):
    def boom(*a, **kw): raise RuntimeError('classifier broke')
    monkeypatch.setattr(ad, 'aux_host_provider', boom)
    record = run(env, 'auto', PlainClient('https://api.anthropic.com'))
    assert record['provider'] == 'auto' and 'requested_provider' not in record


def test_resolved_provider_feeds_usage_normalisation(env):
    """aux_end normalises with the resolved provider (not the phantom label)."""
    response = {'model': 'claude-opus-9-5', 'usage': {'input_tokens': 10, 'output_tokens': 2,
                                                      'cache_read_input_tokens': 5, 'cache_creation_input_tokens': 1}}
    record = run(env, 'auto', PlainClient('https://api.anthropic.com'), response=response)
    assert record['provider'] == 'anthropic' and record['status'] == 'completed'
    assert record['usage']['cache_read_tokens'] == 5 and record['usage']['cache_write_tokens'] == 1


def run_codex_direct(env, base_url):
    """Drive the installed Codex MoA direct-stream wrapper (``_CodexCompletionsAdapter.create``)."""
    response = NS(model='gpt-9', usage=NS(prompt_tokens=10, completion_tokens=2))

    class _CodexCompletionsAdapter:
        def __init__(self):
            self._client = PlainClient(base_url)

        def create(self, **kwargs):
            return response

    mod = module('auto', api_mode='codex_responses')
    mod._relay_sync_completion = lambda client, kwargs, **kw: None
    mod._relay_async_completion = lambda client, kwargs, **kw: None
    mod._CodexCompletionsAdapter = _CodexCompletionsAdapter
    ad.install_aux(mod)
    assert _CodexCompletionsAdapter().create(model='gpt-9', messages=['AUX SECRET']) is response
    with r.store(str(env)).db() as c:
        [(raw,)] = c.execute("SELECT data FROM requests WHERE json_extract(data,'$.source')='auxiliary_adapter'").fetchall()
        c.execute('DELETE FROM requests')
    for secret in ('AUX SECRET', 'base_url', 'chatgpt.com', 'githubcopilot.com', 'x.ai', 'gw.example'):
        assert secret not in raw
    return json.loads(raw)


@pytest.mark.parametrize('base_url,provider', [
    ('https://chatgpt.com/backend-api/codex', 'openai-codex'),
    ('https://CHATGPT.COM./backend-api/codex', 'openai-codex'),
    ('https://api.individual.githubcopilot.com', 'copilot'),
    ('https://githubcopilot.com', 'copilot'),
    ('https://api.x.ai/v1', 'xai'),
    # Look-alikes and unknown hosts are never guessed.
    ('https://chatgpt.com.evil.example', 'unknown'),
    ('https://githubcopilot.com.evil.example', 'unknown'),
    ('https://fox.ai', 'unknown'),
    ('https://notx.ai', 'unknown'),
    ('https://gw.example/v1', 'unknown'),
    ('', 'unknown'),
])
def test_codex_moa_direct_path_uses_the_same_host_table(env, base_url, provider):
    record = run_codex_direct(env, base_url)
    assert record['provider'] == provider and record['api_mode'] == 'codex_responses'
    assert 'requested_provider' not in record and 'anthropic_endpoint' not in record
