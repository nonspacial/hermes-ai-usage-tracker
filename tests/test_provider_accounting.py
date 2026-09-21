"""Native protocol fixtures; no provider connections or historical rewrites."""
import pytest
from _hermes_ai_usage_ledger_v2.accounting import normalize


@pytest.mark.parametrize('mode', ['anthropic', 'anthropic_messages'])
def test_anthropic_missing_cache_keeps_exclusive_input(mode):
    u = normalize({'input_tokens': 100, 'output_tokens': 20}, api_mode=mode)
    assert u['input_tokens'] == 100
    assert u['prompt_tokens'] is None and u['total_tokens'] is None
    assert u['cache_read_tokens'] is None and u['cache_write_tokens'] is None


def test_nested_compatible_cache_creation_spelling():
    u = normalize({'prompt_tokens': 100, 'completion_tokens': 20,
                   'prompt_tokens_details': {'cached_tokens': 40, 'cache_creation_input_tokens': 10}})
    assert u['cache_write_tokens'] == 10 and u['input_tokens'] == 50
    assert u['total_tokens'] == 120


def test_native_gemini_thinking_and_reported_total():
    raw = {'promptTokenCount': 100, 'candidatesTokenCount': 20,
           'thoughtsTokenCount': 30, 'totalTokenCount': 150}
    u = normalize(raw, provider='gemini')
    assert u['reasoning_tokens'] == 30 and u['output_tokens'] == 50
    assert u['total_tokens'] == 150 and u['prompt_tokens'] == 100
    assert u['cache_read_tokens'] is None and u['cache_write_tokens'] is None
    assert u['raw_usage'] == raw
    assert u['field_provenance']['total_tokens'] == 'response_field'
    assert u['field_provenance']['output_tokens'] == 'derived_from_response'


@pytest.mark.parametrize('cache', [0, 40])
def test_native_gemini_explicit_cache_count(cache):
    u = normalize({'promptTokenCount': 100, 'candidatesTokenCount': 20,
                   'cachedContentTokenCount': cache, 'totalTokenCount': 120})
    assert u['cache_read_tokens'] == cache
    assert u['field_provenance']['cache_read_tokens'] == 'response_field'


def test_provider_reported_total_is_not_replaced():
    u = normalize({'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 155})
    assert u['total_tokens'] == 155
    assert 'provider_total_differs_from_input_plus_output' in u['warnings']
    assert u['raw_usage']['total_tokens'] == 155


def test_gemini_reported_total_without_candidate_count():
    u = normalize({'promptTokenCount': 100, 'totalTokenCount': 130})
    assert u['total_tokens'] == 130 and u['output_tokens'] is None


def test_gemini_allowlist_excludes_content():
    u = normalize({'promptTokenCount': 100, 'totalTokenCount': 130, 'text': 'private'})
    assert 'text' not in u['raw_usage']
