"""Universal OpenRouter list-price fallback: synthetic catalogues, disposable ledgers, no network.

Every provider id without a catalogue of its own is estimated from the
OpenRouter snapshot by exact model id, always labelled list_price_fallback.
Direct catalogues always win; zero list prices are a known $0.
"""
import copy
import json
import time
from decimal import Decimal

import pytest

from _hermes_ai_usage_ledger_v2 import accounting as a, pricing as p, recorder as rec
from _hermes_ai_usage_ledger_v2.storage import Store, summary

OR_ROWS = [
    p.rate('anthropic/claude-opus-9.5', 'standard', ['5', '25', '0.5', '6.25']),
    p.rate('x-ai/grok-9', 'standard', ['3', '15', '0.75', None]),
    p.rate('google/gemini-9-pro', 'standard', ['1.25', '10', '0.31', None]),
    p.rate('deepseek/deepseek-v9', 'standard', ['0.3', '1.2', '0.03', None]),
    p.rate('qwen/qwen9-coder:free', 'standard', ['0', '0', None, None]),
    p.rate('stealth/zero-model', 'standard', ['0', '0', None, None]),
    p.rate('openai/gpt-9', 'standard', ['2', '8', '0.2', None]),
    p.rate('z-ai/glm-9', 'standard', ['0.6', '2.2', '0.11', None]),
    # Two vendors publish the same bare id: a derived match is ambiguous.
    p.rate('mistralai/twin-9', 'standard', ['1', '2', None, None]),
    p.rate('qwen/twin-9', 'standard', ['3', '4', None, None]),
]
USAGE = dict(input_tokens=1000, output_tokens=200, cache_read_tokens=4000, cache_write_tokens=0,
             reasoning_tokens=0, prompt_tokens=5000, total_tokens=5200)


def orsnap(rows=OR_ROWS, stamp=100.0):
    return p._snapshot('openrouter', rows, stamp, 'fixture_provider_catalog')


def select(provider, model, snapshot=None, **extra):
    record = {'provider': provider, 'model': model, 'usage': dict(USAGE), **extra}
    return p.explain(snapshot or orsnap(), record, 7, 200.0)


def test_unknown_provider_prices_by_exact_openrouter_id():
    rate, reason = select('copilot', 'anthropic/claude-opus-9.5')
    assert reason is None and rate['model'] == 'anthropic/claude-opus-9.5'
    assert rate['pricing_basis'] == 'list_price_fallback' and rate['basis'] == p.LIST_PRICE_BASIS
    assert rate['basis'] == ('OpenRouter list price for this model id; serving provider has no catalogue '
                             'here, actual charge may differ')
    assert rate['source'] == 'OpenRouter list price (fallback)' and rate['source_url'] == p.SOURCES['openrouter']['url']
    assert rate['fallback_match'] == 'exact' and rate['fallback_model'] == 'anthropic/claude-opus-9.5'
    assert rate['provider'] == 'copilot' and rate['catalog_revision'] == 7
    cost = a.costs(USAGE, rate)
    assert cost['complete'] is True and Decimal(cost['total_usd']) == Decimal('0.012')


@pytest.mark.parametrize('provider,model,expected', [
    ('gemini', 'gemini-9-pro', 'google/gemini-9-pro'),
    ('xai', 'grok-9', 'x-ai/grok-9'),
    ('xai-oauth', 'grok-9', 'x-ai/grok-9'),
    ('deepseek', 'deepseek-v9', 'deepseek/deepseek-v9'),
    ('zai', 'glm-9', 'z-ai/glm-9'),
    ('zai', 'GLM-9', 'z-ai/glm-9'),
    # Multi-vendor hosts have no provider row: the model family decides.
    ('copilot', 'claude-opus-9-5', 'anthropic/claude-opus-9.5'),
    ('copilot', 'gpt-9', 'openai/gpt-9'),
    ('bedrock', 'claude-opus-9.5', 'anthropic/claude-opus-9.5'),
    ('custom', 'grok-9', 'x-ai/grok-9'),
    ('auto', 'deepseek-v9', 'deepseek/deepseek-v9'),
    ('groq', 'qwen9-coder:free', 'qwen/qwen9-coder:free'),
])
def test_vendor_prefixed_mapping_is_deterministic(provider, model, expected):
    rate, reason = select(provider, model)
    assert reason is None, reason
    assert rate['fallback_model'] == expected and rate['fallback_match'] == 'vendor_prefixed'
    assert rate['pricing_basis'] == 'list_price_fallback'


def test_candidates_are_exact_and_never_fuzzy():
    assert p.fallback_candidates('gemini', 'gemini-9-pro') == [['gemini-9-pro'], ['google/gemini-9-pro']]
    assert p.fallback_candidates('copilot', 'claude-opus-9-5') == [
        ['claude-opus-9-5'], ['anthropic/claude-opus-9-5', 'anthropic/claude-opus-9.5']]
    # A recorded vendor keeps its vendor; no variant or ~alias is derived.
    assert p.fallback_candidates('xai', 'google/gemini-9-pro') == [['google/gemini-9-pro'], []]
    for model in ('gemini-9', 'gemini-9-pro-preview', 'grok', 'unknown-model', 'grok-9:batch'):
        rate, reason = select('xai' if 'grok' in model else 'gemini', model)
        assert rate is None and reason == 'model_missing', model
    assert select('copilot', 'claude-opus-9-5-20990101') == (None, 'model_missing')


def test_ambiguous_candidates_price_nothing():
    # Provider row 'mistral' -> mistralai; family 'qwen' -> qwen: two matches.
    rate, reason = select('mistral', 'qwen-twin')
    assert rate is None and reason == 'model_missing'
    snap = orsnap(OR_ROWS + [p.rate('mistralai/qwen-twin', 'standard', ['1', '1', None, None]),
                             p.rate('qwen/qwen-twin', 'standard', ['2', '2', None, None])])
    assert select('mistral', 'qwen-twin', snap) == (None, 'ambiguous')
    # An exact id wins before any derived form is considered.
    assert select('mistral', 'qwen/qwen-twin', snap)[0]['fallback_match'] == 'exact'


def test_zero_list_price_is_known_zero_cost():
    for provider, model in (('groq', 'qwen/qwen9-coder:free'), ('custom', 'stealth/zero-model')):
        rate, reason = select(provider, model)
        assert reason is None
        cost = a.costs(USAGE, rate)
        assert cost['complete'] is True and Decimal(cost['total_usd']) == 0, model
        assert all(Decimal(v) == 0 for v in cost['components'].values())
    # A non-zero model whose cache-write price is unpublished stays incomplete.
    cost = a.costs(USAGE | {'cache_write_tokens': 10}, select('xai', 'grok-9')[0])
    assert cost['complete'] is False and cost['components']['cache_write_tokens'] is None


def test_direct_catalogue_precedence_and_no_fallback_for_catalogued_providers():
    snap = orsnap()
    for provider in ('openai-codex', 'openai-api', 'nous', 'ollama-cloud', 'openrouter'):
        assert p.source_for({'provider': provider})[1] == 'direct'
    # A direct provider never consults the fallback, even when its own
    # catalogue lacks the model: the router snapshot is the wrong source.
    assert p.explain(snap, {'provider': 'openai-api', 'model': 'openai/gpt-9', 'usage': USAGE}, 1, 200.0) == (None, 'no_catalogue')
    own = p._snapshot('openai', [p.rate('gpt-9', 'standard', ['1', '4', '0.1', None])], 100.0, 'fixture_provider_catalog')
    rate, _ = p.explain(own, {'provider': 'openai-api', 'model': 'gpt-9', 'usage': USAGE}, 1, 200.0)
    assert rate['pricing_basis'] == 'provider_catalog_estimate' and rate['input_tokens'] == '1'
    assert p.explain(own, {'provider': 'openai-api', 'model': 'gpt-10', 'usage': USAGE}, 1, 200.0) == (None, 'model_missing')
    # Direct Anthropic: its own catalogue only while first-party.
    assert p.source_for({'provider': 'anthropic'}) == ('anthropic', 'direct')
    assert p.source_for({'provider': 'anthropic', 'anthropic_endpoint': 'first_party'}) == ('anthropic', 'direct')


@pytest.mark.parametrize('endpoint', ['custom', 'unverified'])
def test_anthropic_custom_endpoint_gets_labelled_list_price(endpoint):
    assert p.source_for({'provider': 'anthropic', 'anthropic_endpoint': endpoint}) == ('openrouter', 'fallback')
    rate, reason = select('anthropic', 'claude-opus-9-5', anthropic_endpoint=endpoint)
    assert reason is None and rate['fallback_model'] == 'anthropic/claude-opus-9.5'
    assert rate['pricing_basis'] == 'list_price_fallback' and 'inference_geo' not in rate
    # Premiums the list does not carry stay unpriced (never discounted).
    for extra in ({'returned_inference_geo': 'us'}, {'returned_speed': 'fast'}, {'service_tier': 'priority'},
                  {'returned_usage_service_tier': 'batch'}):
        assert select('anthropic', 'claude-opus-9-5', anthropic_endpoint=endpoint, **extra) == (None, 'endpoint_unpriced')


def test_unknown_request_tier_takes_list_default_flagged():
    rate, _ = select('xai', 'grok-9', service_tier='turbo')
    assert rate['tier_assumption'] is True and rate['tier_basis'] == p.UNKNOWN_AS_DEFAULT
    plain, _ = select('xai', 'grok-9')
    assert 'tier_basis' not in plain


@pytest.mark.parametrize('endpoint', ['custom', 'unverified'])
@pytest.mark.parametrize('extra,flags,unknown', [
    ({'returned_speed': 'turbo'}, {'speed_inferred': True}, {'usage.speed': 'turbo'}),
    ({'returned_inference_geo': 'eu'}, {'geo_assumption': True, 'geo_basis': p.UNKNOWN_AS_DEFAULT},
     {'usage.inference_geo': 'eu'}),
    ({'returned_inference_geo': 'unrecognised'}, {'geo_assumption': True, 'geo_basis': p.UNKNOWN_AS_DEFAULT},
     {'usage.inference_geo': 'unrecognised'}),
    ({'returned_usage_service_tier': 'priority_plus'}, {'tier_assumption': True, 'tier_basis': p.UNKNOWN_AS_DEFAULT},
     {'usage.service_tier': 'priority_plus'}),
])
def test_list_price_flags_unknown_returned_enums_like_direct_anthropic(endpoint, extra, flags, unknown):
    """Unknown returned enums take the list default, always flagged with the value."""
    rate, reason = select('anthropic', 'claude-opus-9-5', anthropic_endpoint=endpoint, **extra)
    assert reason is None and rate['pricing_basis'] == 'list_price_fallback'
    for key, value in flags.items():
        assert rate[key] == value, (key, rate)
    assert rate['unknown_values'] == unknown
    if 'returned_speed' in extra:
        assert rate['speed_basis'].startswith('unknown usage.speed')
    # tier_assumption is True whenever tier_basis is set.
    assert ('tier_basis' not in rate) or rate['tier_assumption'] is True
    # Priced at the standard list rate (the only row the list carries).
    assert rate['input_tokens'] == select('anthropic', 'claude-opus-9-5', anthropic_endpoint=endpoint)[0]['input_tokens']


def test_list_price_documented_values_carry_no_unknown_flags():
    for extra in ({'returned_speed': 'standard'}, {'returned_inference_geo': 'global'},
                  {'returned_inference_geo': 'not_available'}, {'returned_usage_service_tier': 'standard'}, {}):
        rate, _ = select('anthropic', 'claude-opus-9-5', anthropic_endpoint='custom', **extra)
        for key in ('tier_basis', 'geo_basis', 'speed_basis', 'unknown_values', 'geo_assumption', 'speed_inferred'):
            assert key not in rate, (extra, key)


def test_private_host_follows_the_normal_path():
    """No local-endpoint class: base_url is never classified for no-catalogue providers."""
    for url in ('http://127.0.0.1:11434/v1', 'http://192.168.1.20:8000', 'http://gpu-box.local:8080'):
        assert rec.endpoint_class({'provider': 'custom', 'base_url': url}) == {}
    assert not hasattr(p, 'endpoint_host_class') and not hasattr(p, 'local_rate')
    record = {'provider': 'custom', 'model': 'llama9:70b', 'usage': dict(USAGE)}
    assert p.source_for(record) == ('openrouter', 'fallback')
    assert p.explain(None, record, None, 200.0) == (None, 'no_catalogue')
    assert p.explain(orsnap(), record, 7, 200.0) == (None, 'model_missing')
    # A matching id takes the labelled list price as usual.
    assert select('custom', 'grok-9')[0]['pricing_basis'] == 'list_price_fallback'


def test_other_providers_selections_are_byte_identical_to_direct_path():
    """Catalogued providers' selections equal the pre-change selector output."""
    import importlib.util, io, subprocess, tarfile, tempfile, sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    try:
        blob = subprocess.run(['git', '-C', str(root), 'archive', 'b3fddc8', 'ledger_runtime'],
                              check=True, capture_output=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip('reference commit unavailable')
    folder = Path(tempfile.mkdtemp(prefix='pricing-fallback-reference-'))
    with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
        archive.extractall(folder, filter='data')
    name = '_pricing_fallback_reference'
    spec = importlib.util.spec_from_file_location(name, folder / 'ledger_runtime' / '__init__.py',
                                                  submodule_search_locations=[str(folder / 'ledger_runtime')])
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module; spec.loader.exec_module(module)
    old = importlib.import_module(name + '.pricing')
    try:
        seed = json.loads((root / 'ledger_runtime' / 'provider_rates_seed.json').read_text())['snapshots']
        snaps = {s['source_id']: s for s in seed}
        snaps['openrouter'] = orsnap()
        snaps['nous'] = p._snapshot('nous', OR_ROWS, 100.0, 'fixture_provider_catalog')
        compared = 0
        for provider, sid in p.PROVIDERS.items():
            for r in snaps[sid]['rates']:
                for tier in ('unspecified', 'standard', 'fast', 'priority', 'flex', 'batch', 'auto'):
                    for prompt in (None, 1000, 400000):
                        for extra in ({}, {'returned_speed': 'fast'}, {'returned_inference_geo': 'us'},
                                      {'returned_inference_geo': 'not_available'}, {'anthropic_endpoint': 'first_party'}):
                            record = {'provider': provider, 'model': r['model'], 'service_tier': tier,
                                      'usage': {'prompt_tokens': prompt}, **extra}
                            before = old.select_rate(copy.deepcopy(snaps[sid]), record, 3, 200.0)
                            if before is None:
                                continue
                            assert p.select_rate(copy.deepcopy(snaps[sid]), record, 3, 200.0) == before, record
                            compared += 1
        assert compared > 500
    finally:
        for key in [k for k in sys.modules if k == name or k.startswith(name + '.')]:
            sys.modules.pop(key, None)


# --- Ledger integration ----------------------------------------------------------

def record(key, provider, model, ended, **extra):
    return dict(id=key, provider=provider, model=model, started=ended-1, ended=ended, status='completed',
                session_id='fixture', usage=dict(USAGE), **extra)


def insert(store, stamp):
    with store.db() as c:
        c.execute('INSERT INTO provider_catalog(source_id,observed,data) VALUES(?,?,?)',
                  ('openrouter', stamp, json.dumps(orsnap(stamp=stamp))))


def test_ledger_saves_labelled_fallback_and_values_history_retrospectively(tmp_path):
    s = Store(tmp_path)
    old = time.time() - 86400
    # Recorded before any router snapshot existed: saved unpriced.
    s.request(record('hist', 'xai', 'grok-9', old), 'request_completed')
    with s.db() as c:
        stored = json.loads(c.execute("SELECT data FROM requests WHERE id='hist'").fetchone()[0])
    assert stored['cost']['rate'] is None
    insert(s, time.time() - 10)
    now = time.time() - 1
    s.request(record('live', 'gemini', 'gemini-9-pro', now), 'request_completed')
    s.request(record('free', 'custom', 'stealth/zero-model', now), 'request_completed')
    s.request(record('miss', 'mistral', 'nothing-9', now), 'request_completed')
    d = s.read(start=old - 10, end=time.time())
    rows = {r['id']: r for r in d['requests']}
    assert rows['live']['cost']['rate']['pricing_basis'] == 'list_price_fallback'
    assert rows['live']['cost']['complete'] is True
    assert rows['hist']['cost']['rate']['retrospective'] is True
    assert rows['hist']['supplemental_valuation']['source_id'] == 'openrouter'
    assert rows['free']['cost']['complete'] is True and Decimal(rows['free']['cost']['total_usd']) == 0
    assert rows['miss']['unpriced_reason'] == 'model_missing'
    assert d['summary']['unpriced'] == {'requests': 1, 'tokens': USAGE['total_tokens'],
                                        'reasons': {'no_catalogue': 0, 'model_missing': 1, 'ambiguous': 0,
                                                    'usage_missing': 0, 'endpoint_unpriced': 0}}
    assert d['summary'] == summary(d['requests'])
    with s.db() as c:
        assert json.loads(c.execute("SELECT data FROM requests WHERE id='hist'").fetchone()[0]) == stored


# --- Catalogue staleness on a read miss ------------------------------------------

def test_note_miss_is_offline_gated_bounded_and_network_free(monkeypatch):
    monkeypatch.setattr(p, '_STALE', {})
    monkeypatch.setenv('HERMES_USAGE_PRICING_OFFLINE', '1')
    assert p.note_miss('openrouter') is False and p._STALE == {}
    monkeypatch.delenv('HERMES_USAGE_PRICING_OFFLINE')
    monkeypatch.setattr(p, 'fetch', lambda *_: pytest.fail('note_miss must not fetch'))
    assert p.note_miss('not-a-source') is False
    assert p.note_miss('openrouter') is True
    assert p.note_miss('openrouter') is False          # at most once per hour
    with p._LOCK:
        assert p.stale_sources() == {'openrouter'}
    p._STALE['openrouter'] -= p.MISS_REFRESH_SECONDS   # the hour elapses
    with p._LOCK:
        assert p.stale_sources() == frozenset()
    assert p.note_miss('openrouter') is True


def test_stale_source_lifts_daily_skip_at_most_hourly(tmp_path, monkeypatch):
    s = Store(tmp_path)
    calls = []
    def fetch(url):
        calls.append(url)
        raise OSError('offline fixture')
    now = time.time()
    with s.db() as c:
        for sid in p.SOURCES:
            state = {'source_id': sid, 'status': 'ok', 'last_success': now, 'attempted_at': now - 2 * 3600}
            c.execute('INSERT OR REPLACE INTO pricing_status VALUES(?,?)', (sid, json.dumps(state)))
    p.refresh(s, fetcher=fetch)
    assert calls == []                                  # daily success: skipped
    p.refresh(s, fetcher=fetch, stale=frozenset({'openrouter'}))
    assert calls == [p.SOURCES['openrouter']['url']]    # miss lifts the skip for that source only
    p.refresh(s, fetcher=fetch, stale=frozenset({'openrouter'}))
    assert len(calls) == 1                              # attempted_at bounds it to one per hour


def test_read_miss_marks_source_stale_without_network(tmp_path, monkeypatch):
    marked = []
    monkeypatch.setattr(p, 'note_miss', marked.append)
    monkeypatch.setattr(p, 'fetch', lambda *_: pytest.fail('read must not fetch'))
    s = Store(tmp_path)
    insert(s, time.time() - 10)
    now = time.time() - 1
    s.request(record('hit', 'gemini', 'gemini-9-pro', now), 'request_completed')
    d = s.read(start=now - 10, end=time.time())
    assert marked == []
    s.request(record('miss', 'mistral', 'nothing-9', now), 'request_completed')
    d = s.read(start=now - 10, end=time.time())
    assert {r['id']: r.get('unpriced_reason') for r in d['requests']}['miss'] == 'model_missing'
    assert marked == ['openrouter']
