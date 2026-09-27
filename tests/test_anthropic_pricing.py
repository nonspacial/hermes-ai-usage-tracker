"""Direct Anthropic pricing: synthetic schema fixtures, disposable ledgers, no network.

Fixtures copy the *shape* of Anthropic's public pricing.md / model overview
Markdown with synthetic model names. They prove parser and accounting
behaviour, not live compatibility or any account's actual charges.
"""
import json
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from _hermes_ai_usage_ledger_v2 import accounting as a, adapters as ad, aggregate, pricing as p, recorder as rec
from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
from _hermes_ai_usage_ledger_v2.storage import Store, summary

DOC = 'https://platform.claude.com/docs/en'
ROWS = [
    # name, annotation, base, 5m, 1h, hit, out
    ('Claude Opus 9.5', '', '$4 / MTok', '$5 / MTok', '$8 / MTok', '$0.20 / MTok<sup>2</sup>', '$20 / MTok'),
    ('Claude Opus 9', '', '$5 / MTok', '$6.25 / MTok', '$10 / MTok', '$0.50 / MTok', '$25 / MTok'),
    ('Claude Sonnet 9', '', '$2 / MTok<sup>3</sup>', '$2.50 / MTok', '$4 / MTok', '$0.20 / MTok', '$10 / MTok<sup>3</sup>'),
    ('Claude Haiku 4.5', '', '$1 / MTok', '$1.25 / MTok', '$2 / MTok', '$0.10 / MTok', '$5 / MTok'),
    ('Claude Nova 9', f' ([limited availability](https://example.invalid/x))', '$10 / MTok', '$12.50 / MTok', '$20 / MTok', '$1 / MTok', '$50 / MTok'),
    ('Claude Opus 4.1', f' ([retired, except on Bedrock and Google Cloud]({DOC}/about-claude/model-deprecations))', '$15 / MTok', '$18.75 / MTok', '$30 / MTok', '$1.50 / MTok', '$75 / MTok'),
    ('Claude Orphan 9', '', '$3 / MTok', '$3.75 / MTok', '$6 / MTok', '$0.30 / MTok', '$15 / MTok'),
]
NOTE2 = '*<sup>2 Cache hits and refreshes on Claude Opus 9.5 are priced at 0.05x the base input price.</sup>*'
NOTE3 = ('*<sup>3 The $2/$10 per million input/output token pricing for Claude Sonnet 9, announced at launch as '
         'introductory pricing through August 31, 2026, is now the standard price.</sup>*')
FAST = '| Claude Opus 9.5                 | $8 / MTok  | $40 / MTok |\n| Claude Opus 9 | $10 / MTok | $50 / MTok |'


def pricing_md(rows=ROWS, note3=NOTE3, fast=FAST, extra_column=False):
    head = '| Model | Base input tokens | 5m cache writes | 1h cache writes | Cache hits and refreshes | Output tokens |'
    rule = '| :--- | :--- | :--- | :--- | :--- | :--- |'
    if extra_column:
        head = head[:-1] + ' Batch |'
        rule += ' --- |'
    body = '\n'.join('| ' + ' | '.join([r[0] + r[1], *r[2:]] + (['$1 / MTok'] if extra_column else [])) + ' |' for r in rows)
    return f"""---
title: Pricing
---

This page provides detailed pricing information for Anthropic's models and features. All prices are in USD.

## Model pricing

{head}
{rule}
{body}

{NOTE2}

*<sup>All other models use the standard 0.1x multiplier.</sup>*

{note3}

## Feature-specific pricing

### Prompt caching

| Cache operation      | Multiplier              | Duration |
| -------------------- | ----------------------- | -------- |
| 5-minute cache write | 1.25x base input price  | 5 min    |
| 1-hour cache write   | 2x base input price     | 1 hour   |
| Cache read (hit)     | 0.1x base input price   | Same     |

### Data residency pricing

For Claude 4.6 and later models, specifying US-only inference through the `inference_geo` parameter incurs a 1.1x multiplier on all token pricing categories, including input tokens, output tokens, cache writes, and cache reads. Global routing (the default) uses standard pricing.

### Fast mode pricing

Fast mode is available for some models.

| Model | Input | Output |
| ----- | ----- | ------ |
{fast}

Fast mode pricing stacks with other pricing modifiers:

* [Prompt caching multipliers]({DOC}/about-claude/pricing#prompt-caching) apply on top of fast mode pricing

### Long context pricing

Claude 4.6 and later models and [Claude Preview](https://example.invalid) include the full [1M token context window]({DOC}/build-with-claude/context-windows) at standard pricing.

### Tool use pricing

Ignored.
"""


def page(name, slug, api, alias=None, window='1M', links=()):
    alias_row = f'| Claude API alias | `{alias}` |\n' if alias else ''
    more = ' '.join(f'[x]({DOC}/models/{s}/overview)' for s in links)
    return f"""---
title: {name}
url: {DOC}/models/{slug}/overview
---

Model ID: `{api}`

Context window: {window} tokens · Max output: 64K tokens

{more}

### Model IDs

| Platform | Model ID |
| :--- | :--- |
| Claude API | `{api}` |
{alias_row}| [Amazon Bedrock](https://example.invalid) | `anthropic.{api}` |

### Pricing

Not used.
"""


PAGES = {
    'opus-9-5': page('Claude Opus 9.5', 'opus-9-5', 'claude-opus-9-5', links=('opus-9', 'haiku-4-5')),
    'opus-9': page('Claude Opus 9', 'opus-9', 'claude-opus-9'),
    'sonnet-9': page('Claude Sonnet 9', 'sonnet-9', 'claude-sonnet-9'),
    'haiku-4-5': page('Claude Haiku 4.5', 'haiku-4-5', 'claude-haiku-4-5-20251001', 'claude-haiku-4-5', '200K'),
    'nova-9': page('Claude Nova 9', 'nova-9', 'claude-nova-9'),
}


def overview(api_alias=('claude-haiku-4-5-20251001', 'claude-haiku-4-5')):
    return f"""---
title: Models overview
---

## Compare models

| Feature | Claude Opus 9.5 | Claude Haiku 4.5 |
| :--- | :--- | :--- |
| Model page | [Claude Opus 9.5]({DOC}/models/opus-9-5/overview) | [Claude Haiku 4.5]({DOC}/models/haiku-4-5/overview) |
| Claude API ID | `claude-opus-9-5` | `{api_alias[0]}` |
| Claude API alias | `claude-opus-9-5` | `{api_alias[1]}` |

Legacy: [Claude Sonnet 9]({DOC}/models/sonnet-9/overview), [Claude Nova 9]({DOC}/models/nova-9/overview)

## Using the Models API
"""


def fetcher_for(body=None, ov=None, pages=PAGES, calls=None):
    def fetch(url):
        # Same allowlist as the real transport, without opening a socket.
        assert url in (p.SOURCES['anthropic']['url'], p.ANTHROPIC_OVERVIEW_URL) or p._ANTHROPIC_PAGE.fullmatch(url), url
        if calls is not None:
            calls.append(url)
        if url == p.SOURCES['anthropic']['url']:
            return pricing_md() if body is None else body
        if url == p.ANTHROPIC_OVERVIEW_URL:
            return overview() if ov is None else ov
        slug = url.split('/models/')[1].split('/')[0]
        if slug not in pages:
            raise ConnectionError('fixture 404')
        return pages[slug]
    return fetch


def catalog():
    return p.anthropic_catalog(fetcher_for())


def find(rows, model, tier='standard', geo='global'):
    [r] = [r for r in rows if r['model'] == model and r['service_tier'] == tier and r['inference_geo'] == geo]
    return r


def snap(rows=None, stamp=100.0):
    return p._snapshot('anthropic', catalog() if rows is None else rows, stamp, 'fixture_provider_catalog')


def select(record, snapshot=None):
    record = {'provider': 'anthropic', 'usage': {'prompt_tokens': 1000}, **record}
    return p.select_rate(snapshot or snap(), record, 1, 200.0)


# --- Parser --------------------------------------------------------------------

def test_parser_maps_exact_ids_aliases_speeds_and_geo():
    rows = catalog()
    models = {r['model'] for r in rows}
    # Only documented model-page IDs; no slug from 'Claude Orphan 9', no retired row.
    assert models == {'claude-opus-9-5', 'claude-opus-9', 'claude-sonnet-9', 'claude-nova-9',
                      'claude-haiku-4-5-20251001', 'claude-haiku-4-5'}
    std = find(rows, 'claude-opus-9-5')
    assert [std[k] for k in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'cache_write_1h_tokens')] == \
        ['4', '20', '0.20', '5', '8']
    fast = find(rows, 'claude-opus-9-5', 'fast')
    assert tuple(Decimal(fast[k]) for k in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'cache_write_1h_tokens')) == \
        (8, 40, Decimal('0.4'), 10, 16)
    assert Decimal(find(rows, 'claude-opus-9', 'fast')['cache_read_tokens']) == 1
    assert not [r for r in rows if r['model'] == 'claude-sonnet-9' and r['service_tier'] == 'fast']
    us = find(rows, 'claude-opus-9-5', 'standard', 'us')
    assert tuple(Decimal(us[k]) for k in ('input_tokens', 'output_tokens', 'cache_read_tokens')) == (Decimal('4.4'), 22, Decimal('0.22'))
    # Pre-4.6 models do not accept inference_geo; no US variant is invented.
    assert not [r for r in rows if r['model'].startswith('claude-haiku') and r['inference_geo'] == 'us']
    alias = find(rows, 'claude-haiku-4-5')
    assert alias['alias_of'] == 'claude-haiku-4-5-20251001' and alias['max_prompt_tokens'] == 200000
    assert alias['long_context_flat'] is False and find(rows, 'claude-opus-9')['long_context_flat'] is True
    assert find(rows, 'claude-nova-9')['availability'] == 'limited'
    assert all(r['cache_ttl_breakdown_required'] is True for r in rows)


def _replace(old, new):
    return pricing_md().replace(old, new, 1)


@pytest.mark.parametrize('body', [
    '<html>login</html>',
    _replace('All prices are in USD.', 'All prices are in EUR.'),
    _replace('| Cache hits and refreshes |', '| Cache reads |'),
    pricing_md(extra_column=True),
    _replace('$6.25 / MTok', '$6.00 / MTok'),                      # cache-write multiplier breaks
    _replace('$0.50 / MTok', '$0.40 / MTok'),                      # default hit multiplier breaks
    _replace('priced at 0.05x', 'priced at 0.04x'),                # footnoted hit inconsistent
    _replace('is now the standard price.', 'ends on August 31, 2026.'),  # expiring intro price
    _replace('(https://example.invalid/x)', '(https://example.invalid/x)').replace('limited availability', 'preview only'),
    _replace('| Claude Opus 9 | $10 / MTok | $50 / MTok |', '| Claude Opus 7 | $10 / MTok | $50 / MTok |'),
    _replace('apply on top of fast mode pricing', 'do not apply'),
    _replace('incurs a 1.1x multiplier', 'incurs a regional multiplier'),
    _replace('| :--- | :--- | :--- | :--- | :--- | :--- |', '| - | - | - | - | - | - |'),
    _replace('| Claude Haiku 4.5 | $1 / MTok |', '| Claude Haiku 4.5 | $1 / MTok | $9 |'),
])
def test_parser_schema_or_value_drift_fails_closed(body):
    with pytest.raises(ValueError):
        p.anthropic_catalog(fetcher_for(body=body))


@pytest.mark.parametrize('pages,ov', [
    ({**PAGES, 'haiku-4-5': PAGES['haiku-4-5'].replace('| Claude API | `claude-haiku-4-5-20251001` |', '| Claude API | Claude Haiku |')}, None),
    ({**PAGES, 'opus-9': PAGES['opus-9'].replace('Model ID: `claude-opus-9`', 'Model ID: `claude-opus-9-0`')}, None),
    ({**PAGES, 'opus-9': PAGES['opus-9'].replace('Context window: 1M', 'Context: 1M')}, None),
    ({k: v for k, v in PAGES.items() if k != 'haiku-4-5'}, None),                   # page fetch fails
    (PAGES, overview(('claude-haiku-4-5-20251002', 'claude-haiku-4-5'))),           # overview disagrees
])
def test_model_id_evidence_drift_fails_closed(pages, ov):
    with pytest.raises((ValueError, ConnectionError)):
        p.anthropic_catalog(fetcher_for(ov=ov, pages=pages))


def test_bounded_same_host_page_crawl_and_fetch_allowlist(monkeypatch):
    calls = []
    p.anthropic_catalog(fetcher_for(calls=calls))
    assert calls[:2] == [p.SOURCES['anthropic']['url'], p.ANTHROPIC_OVERVIEW_URL]
    assert sorted(calls[2:]) == sorted(f'{DOC}/models/{s}/overview.md' for s in PAGES)
    for url in ('https://platform.claude.com/docs/en/models/../../admin/overview.md',
                'https://platform.claude.com/docs/en/models/opus-9/overview.md?x=1',
                'https://evil.example/docs/en/models/opus-9/overview.md',
                'http://platform.claude.com/docs/en/models/opus-9/overview.md'):
        with pytest.raises(ValueError, match='Unapproved'):
            p.fetch(url)
    monkeypatch.setattr(p, 'ANTHROPIC_MAX_PAGES', 2)
    with pytest.raises(ValueError, match='bound'):
        p.anthropic_catalog(fetcher_for())


# --- Selection -----------------------------------------------------------------

@pytest.mark.parametrize('model,expected', [
    ('claude-opus-9-5', 'claude-opus-9-5'),
    ('anthropic/claude-opus-9.5', 'claude-opus-9-5'),       # Hermes wire normalisation
    ('Anthropic/claude-opus-9.5', 'claude-opus-9-5'),
    ('claude-haiku-4-5', 'claude-haiku-4-5'),               # documented alias
    ('claude-haiku-4-5-20251001', 'claude-haiku-4-5-20251001'),
    ('claude-haiku-4.5-20251001', 'claude-haiku-4-5-20251001'),
    ('claude-haiku-4-5-20251002', None),                    # undocumented snapshot
    ('claude-opus-9-5-latest', None),
    ('anthropic.claude-opus-9-5', None),                    # Bedrock ID
    ('us.anthropic.claude-opus-9-5', None),
    ('claude-orphan-9', None),
    ('claude-opus-4-1', None),                              # retired row
])
def test_exact_ids_and_documented_aliases_only(model, expected):
    assert (select({'model': model}) or {}).get('model') == expected


def test_standard_fast_geo_and_tier_boundaries():
    s = snap()
    base = {'model': 'claude-opus-9-5'}
    std = select(base, s)
    assert std['service_tier'] == 'standard' and std['tier_assumption'] is True and std['geo_assumption'] is True
    assert std['pricing_basis'] == 'provider_catalog_estimate' and 'not a subscription debit' in std['basis']
    fast = select({**base, 'returned_speed': 'fast'}, s)
    assert fast['service_tier'] == 'fast' and fast['tier_assumption'] is False and fast['speed_basis'] == 'usage.speed'
    # Legacy records carry only the requested fast flag in service_tier.
    assert select({**base, 'service_tier': 'fast'}, s)['service_tier'] == 'fast'
    assert select({**base, 'requested_speed': 'fast'}, s)['service_tier'] == 'fast'
    # Reported standard wins over a fast request (documented fallback models).
    assert select({**base, 'service_tier': 'fast', 'returned_speed': 'standard'}, s)['service_tier'] == 'standard'
    # Fast requested where no fast price is published: unpriced, never standard.
    assert select({'model': 'claude-sonnet-9', 'service_tier': 'fast'}, s) is None
    assert select({'model': 'claude-sonnet-9', 'returned_speed': 'fast'}, s) is None
    us = select({**base, 'returned_inference_geo': 'us', 'returned_speed': 'fast'}, s)
    assert us['inference_geo'] == 'us' and Decimal(us['input_tokens']) == Decimal('8.8') and us['geo_assumption'] is False
    for unknown in ({'returned_speed': 'unrecognised'}, {'returned_inference_geo': 'eu'},
                    {'returned_inference_geo': 'unrecognised'}, {'returned_usage_service_tier': 'priority'},
                    {'returned_usage_service_tier': 'batch'}, {'service_tier': 'turbo'},
                    {'anthropic_endpoint': 'custom'}):
        assert select({**base, **unknown}, s) is None, unknown
    assert select({'model': 'claude-haiku-4-5', 'returned_inference_geo': 'us'}, s) is None
    assert select({'model': 'claude-haiku-4-5'}, s)['geo_assumption'] is False
    assert select({**base, 'anthropic_endpoint': 'first_party'}, s)['endpoint_assumption'] is False


def test_context_window_boundaries():
    s = snap()
    def haiku(prompt):
        return select({'model': 'claude-haiku-4-5', 'usage': {'prompt_tokens': prompt}}, s)
    assert haiku(200000)['model'] == 'claude-haiku-4-5'
    assert haiku(200001) is None and haiku(None) is None
    for prompt in (None, 200001, 900000):
        assert select({'model': 'claude-opus-9', 'usage': {'prompt_tokens': prompt}}, s)['input_tokens'] == '5'


@pytest.mark.parametrize('provider', ['openrouter', 'nous', 'custom', 'anthropic-bedrock', 'copilot', 'opencode-zen'])
def test_router_and_custom_routes_never_borrow_direct_rates(provider):
    s = snap()
    for model in ('claude-opus-9-5', 'anthropic/claude-opus-9.5'):
        assert p.select_rate(s, {'provider': provider, 'model': model, 'usage': {'prompt_tokens': 1}}, 1, 200.0) is None


def test_endpoint_classification_is_host_exact():
    assert p.anthropic_endpoint(None) is None and p.anthropic_endpoint('') is None
    assert p.anthropic_endpoint('https://api.anthropic.com') == 'first_party'
    assert p.anthropic_endpoint('https://API.anthropic.com/v1/') == 'first_party'
    # Hermes canonicalises hostnames with lower().rstrip('.'): absolute DNS form is the same host.
    assert p.anthropic_endpoint('https://api.anthropic.com./v1') == 'first_party'
    for url in ('https://api.anthropic.com.evil.example', 'https://proxy.example/anthropic',
                'https://gateway.ai.cloudflare.com/v1/x/anthropic', 'not a url',
                'https://api.anthropic.com.evil.example.', 'https://x.api.anthropic.com', 'https://api.anthropic.com..',
                'api.anthropic.com', 'https://evil.example/api.anthropic.com'):
        assert p.anthropic_endpoint(url) == 'custom', url


def test_requested_only_fast_speed_is_flagged_inferred():
    s = snap()
    inferred = select({'model': 'claude-opus-9-5', 'requested_speed': 'fast'}, s)
    assert inferred['service_tier'] == 'fast' and inferred['speed_inferred'] is True
    assert inferred['tier_assumption'] is False and inferred['speed_basis'].startswith('requested fast')
    assert select({'model': 'claude-opus-9-5', 'returned_speed': 'fast'}, s)['speed_inferred'] is False
    assert select({'model': 'claude-opus-9-5'}, s)['speed_inferred'] is True   # assumed standard
    assert select({'model': 'claude-opus-9-5', 'anthropic_endpoint': 'unverified'}, s) is None


# --- Accounting ----------------------------------------------------------------

RAW = {'input_tokens': 1000, 'output_tokens': 500, 'cache_read_input_tokens': 20000,
       'cache_creation_input_tokens': 3000,
       'cache_creation': {'ephemeral_5m_input_tokens': 1000, 'ephemeral_1h_input_tokens': 2000}}


def test_cache_ttl_costs_are_exact_and_not_double_counted():
    usage = a.normalize(RAW, api_mode='anthropic_messages')
    assert usage['prompt_tokens'] == 24000 and usage['input_tokens'] == 1000   # exclusive input
    for tier, (inp, out, hit, w5, w1) in {'standard': ('4', '20', '0.20', '5', '8'),
                                          'fast': ('8', '40', '0.4', '10', '16')}.items():
        rate = select({'model': 'claude-opus-9-5', 'returned_speed': tier, 'usage': usage})
        c = a.costs(usage, rate)
        M = Decimal(1000000)
        expected = {'input_tokens': 1000*Decimal(inp)/M, 'output_tokens': 500*Decimal(out)/M,
                    'cache_read_tokens': 20000*Decimal(hit)/M,
                    'cache_write_tokens': (1000*Decimal(w5)+2000*Decimal(w1))/M}
        assert {k: Decimal(v) for k, v in c['components'].items()} == expected
        assert Decimal(c['total_usd']) == sum(expected.values()) and c['complete']
    assert Decimal(a.costs(usage, select({'model': 'claude-opus-9-5', 'usage': usage}))['total_usd']) == Decimal('0.039')


@pytest.mark.parametrize('cache_creation', [None, {'ephemeral_5m_input_tokens': 1000},
                                            {'ephemeral_5m_input_tokens': 1000, 'ephemeral_1h_input_tokens': 1999}])
def test_missing_or_inconsistent_ttl_split_leaves_writes_unknown(cache_creation):
    raw = {k: v for k, v in RAW.items() if k != 'cache_creation'}
    if cache_creation is not None:
        raw['cache_creation'] = cache_creation
    usage = a.normalize(raw, api_mode='anthropic_messages')
    c = a.costs(usage, select({'model': 'claude-opus-9-5', 'usage': usage}))
    assert c['components']['cache_write_tokens'] is None and c['total_usd'] is None and not c['complete']
    assert c['components']['input_tokens'] is not None and c['cache_write_premium_usd'] is None


def test_zero_writes_price_completely_without_split():
    usage = a.normalize({'input_tokens': 10, 'output_tokens': 5, 'cache_read_input_tokens': 0,
                         'cache_creation_input_tokens': 0}, api_mode='anthropic_messages')
    c = a.costs(usage, select({'model': 'claude-opus-9-5', 'usage': usage}))
    assert c['complete'] and c['components']['cache_write_tokens'] == '0'


# --- Recorder metadata -----------------------------------------------------------

def test_recorder_keeps_only_documented_usage_enums():
    meta = rec.response_metadata({'model': 'claude-opus-9-5', 'id': 'msg_1', 'content': 'secret text',
                                  'usage': {'input_tokens': 1, 'speed': 'fast', 'inference_geo': 'us',
                                            'service_tier': 'standard', 'server_tool_use': {'x': 1}}})
    assert meta == {'response_model': 'claude-opus-9-5', 'provider_response_id': 'msg_1', 'returned_speed': 'fast',
                    'returned_inference_geo': 'us', 'returned_usage_service_tier': 'standard'}
    odd = rec.usage_enums({'speed': 'warp' * 100, 'inference_geo': {'nested': 1}, 'service_tier': 7})
    assert odd == {'returned_speed': 'unrecognised', 'returned_inference_geo': 'unrecognised',
                   'returned_usage_service_tier': 'unrecognised'}
    assert rec.body_settings({'request': {'body': {'extra_body': {'speed': 'fast'}}}})['requested_speed'] == 'fast'
    assert 'requested_speed' not in rec.body_settings({'request': {'body': {'extra_body': {'speed': 'x' * 500}}}})
    assert rec.endpoint_class({'provider': 'anthropic', 'base_url': 'https://proxy.example'}) == {'anthropic_endpoint': 'custom'}
    assert rec.endpoint_class({'provider': 'openrouter', 'base_url': 'https://api.anthropic.com'}) == {}


def test_stream_accumulator_carries_enums_without_raw_fields(monkeypatch):
    captured = []
    monkeypatch.setattr(ad.r, 'capture_raw', lambda response, **kw: captured.append(response))
    token = ad._ANTHROPIC_SCOPE.set({'context': {'id': 'x'}, 'usage': {}, 'response': {}})
    try:
        ad.observe_anthropic_usage({'type': 'message_start', 'message': {'id': 'm', 'model': 'claude-opus-9-5',
                                    'usage': {'input_tokens': 5, 'inference_geo': 'global'}}})
        ad.observe_anthropic_usage({'type': 'message_delta', 'usage': {'output_tokens': 7, 'speed': 'fast'}})
    finally:
        ad._ANTHROPIC_SCOPE.reset(token)
    last = captured[-1]
    assert rec.response_metadata(last)['returned_speed'] == 'fast'
    assert rec.response_metadata(last)['returned_inference_geo'] == 'global'
    assert a.safe_usage(last['usage']) == {'input_tokens': 5, 'output_tokens': 7}


# --- Ledger integration ----------------------------------------------------------

USAGE = a.normalize(RAW, api_mode='anthropic_messages')


def record(key, ended, model='claude-opus-9-5', provider='anthropic', **extra):
    return dict(id=key, provider=provider, model=model, started=ended-1, ended=ended, status='completed',
                session_id='fixture', api_mode='anthropic_messages', usage=json.loads(json.dumps(USAGE)), **extra)


def insert(store, rows, stamp):
    with store.db() as c:
        c.execute("DELETE FROM provider_catalog WHERE source_id='anthropic'")
        c.execute('INSERT INTO provider_catalog(source_id,observed,data) VALUES(?,?,?)',
                  ('anthropic', stamp, json.dumps(p._snapshot('anthropic', rows, stamp, 'fixture_provider_catalog'))))


def test_new_ledger_seeds_reviewed_anthropic_catalogue(tmp_path):
    s = Store(tmp_path)
    with s.db() as c:
        [status] = p.catalog_status(c, 'anthropic')
    assert status['origin'] == 'bundled_provider_snapshot_reviewed_2026-09-27'
    assert status['url'] == 'https://platform.claude.com/docs/en/about-claude/pricing.md'
    rates = {(r['model'], r['service_tier'], r['inference_geo']): r for r in status['rates']}
    reviewed = {  # model: input, output, cache hit, 5m write, 1h write (USD/MTok) at review
        'claude-fable-5-1': ('10', '50', '0.25', '12.50', '20'), 'claude-mythos-5-1': ('10', '50', '0.25', '12.50', '20'),
        'claude-fable-5': ('10', '50', '1', '12.50', '20'), 'claude-mythos-5': ('10', '50', '1', '12.50', '20'),
        'claude-opus-5-5': ('4', '20', '0.20', '5', '8'), 'claude-opus-5': ('5', '25', '0.50', '6.25', '10'),
        'claude-opus-4-8': ('5', '25', '0.50', '6.25', '10'), 'claude-opus-4-7': ('5', '25', '0.50', '6.25', '10'),
        'claude-opus-4-6': ('5', '25', '0.50', '6.25', '10'), 'claude-opus-4-5-20251101': ('5', '25', '0.50', '6.25', '10'),
        'claude-opus-4-5': ('5', '25', '0.50', '6.25', '10'), 'claude-sonnet-5': ('2', '10', '0.20', '2.50', '4'),
        'claude-sonnet-4-6': ('3', '15', '0.30', '3.75', '6'), 'claude-sonnet-4-5-20250929': ('3', '15', '0.30', '3.75', '6'),
        'claude-sonnet-4-5': ('3', '15', '0.30', '3.75', '6'), 'claude-haiku-4-5-20251001': ('1', '5', '0.10', '1.25', '2'),
        'claude-haiku-4-5': ('1', '5', '0.10', '1.25', '2')}
    assert {m for m, t, g in rates if t == 'standard' and g == 'global'} == set(reviewed)
    for model, values in reviewed.items():
        r = rates[(model, 'standard', 'global')]
        assert tuple(Decimal(r[k]) for k in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens',
                                             'cache_write_1h_tokens')) == tuple(Decimal(v) for v in values)
    assert {m for m, t, g in rates if t == 'fast' and g == 'global'} == {'claude-opus-5-5', 'claude-opus-5', 'claude-opus-4-8'}
    assert [Decimal(rates[('claude-opus-5-5', 'fast', 'global')][k]) for k in ('input_tokens', 'output_tokens')] == [8, 40]
    assert [Decimal(rates[('claude-opus-5', 'fast', 'global')][k]) for k in ('input_tokens', 'output_tokens')] == [10, 50]
    assert not any(g == 'us' and (m.startswith('claude-haiku') or '4-5' in m) for m, t, g in rates)
    assert status['snapshot_sha256'] == p._snapshot('anthropic', status['rates'], 0)['content_sha256']


def test_completion_price_is_saved_and_later_catalogue_does_not_reprice(tmp_path):
    s = Store(tmp_path)
    insert(s, catalog(), time.time()-10)
    s.request(record('now', time.time()-1, returned_speed='fast'), 'request_completed')
    saved = s.read()['requests'][0]['cost']
    assert saved['rate']['service_tier'] == 'fast' and saved['rate']['retrospective'] is False
    assert Decimal(saved['total_usd']) == Decimal('0.078')
    insert(s, [dict(r, input_tokens='99') for r in catalog()], time.time())
    s.request({**record('now', time.time()-1)}, 'request_completed')
    assert s.read()['requests'][0]['cost'] == saved


def test_retrospective_projection_parity_and_unchanged_bytes(tmp_path):
    stores = [Store(tmp_path), Store(tmp_path/'profiles'/'other')]
    old = time.time()-86400*3
    raw = []
    for n, s in enumerate(stores):
        with s.db() as c:
            c.execute("DELETE FROM provider_catalog WHERE source_id='anthropic'")
        s.request(record(f'std-{n}', old), 'request_completed')
        s.request(record(f'fast-{n}', old+1, service_tier='fast'), 'request_completed')
        s.request(record(f'nosplit-{n}', old+2) | {'usage': a.normalize(
            {k: v for k, v in RAW.items() if k != 'cache_creation'}, api_mode='anthropic_messages')}, 'request_completed')
        s.request(record(f'router-{n}', old+3, provider='openrouter', model='anthropic/claude-opus-9.5'), 'request_completed')
        s.request(record(f'custom-{n}', old+4, anthropic_endpoint='custom'), 'request_completed')
        with s.db() as c:
            raw.append(c.execute('SELECT id,data FROM requests ORDER BY id').fetchall())
            raw[-1] = [tuple(r) for r in raw[-1]]
            events = c.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        assert s.read()['summary']['known_cost_usd'] == '0'
        # Catalogue observed after the requests: only a labelled retrospective estimate.
        insert(s, catalog(), time.time()-5)
        d = s.read(start=old-10, end=time.time())
        rows = {r['id']: r for r in d['requests']}
        assert rows[f'std-{n}']['supplemental_valuation']['source_id'] == 'anthropic'
        assert rows[f'std-{n}']['supplemental_valuation']['inference_geo'] == 'global'
        assert rows[f'std-{n}']['cost']['rate']['retrospective'] is True
        assert rows[f'std-{n}']['stored_accounting']['cost']['rate'] is None
        assert Decimal(rows[f'std-{n}']['cost']['total_usd']) == Decimal('0.039')
        assert Decimal(rows[f'fast-{n}']['cost']['total_usd']) == Decimal('0.078')
        assert rows[f'nosplit-{n}']['cost']['components']['cache_write_tokens'] is None
        assert not rows[f'nosplit-{n}']['cost']['complete']
        for key in (f'router-{n}', f'custom-{n}'):
            assert 'supplemental_valuation' not in rows[key] and rows[key]['cost']['rate'] is None
        assert d['summary'] == summary(d['requests'])
        assert d['summary']['supplemental_requests'] == 3
        assert sum(Decimal(g['known_cost_usd']) for g in d['applied_rate_groups']) == Decimal(d['summary']['known_cost_usd'])
        assert sum(Decimal(g['known_cost_usd']) for g in d['provider_groups']) == Decimal(d['summary']['known_cost_usd'])
        with s.db() as c:
            assert [tuple(r) for r in c.execute('SELECT id,data FROM requests ORDER BY id').fetchall()] == raw[-1]
            assert c.execute('SELECT COUNT(*) FROM events').fetchone()[0] == events
    runtime = AnalyticsRuntime()
    try:
        end = time.time()+1
        report = aggregate.ledger(runtime, aggregate.discover(tmp_path), start=end-86400*7, end=end)
        assert report['coverage']['status'] == 'complete'
        assert report['summary']['supplemental_requests'] == 6
        per_profile = Decimal(0)
        for s in stores:
            per_profile += Decimal(s.read(start=end-86400*7, end=end)['summary']['known_cost_usd'])
        assert Decimal(report['summary']['known_cost_usd']) == per_profile
    finally:
        runtime._discard(runtime.current)
    for s, before in zip(stores, raw):
        with s.db() as c:
            assert [tuple(r) for r in c.execute('SELECT id,data FROM requests ORDER BY id').fetchall()] == before


# --- Auxiliary capture path -------------------------------------------------------

class AuxAnthropicClient:
    """Shape of Hermes' AnthropicAuxiliaryClient: the relay receives this object and
    it exposes the configured ``base_url`` (see ``auxiliary_hooks._AuxCallHooks``)."""
    def __init__(self, base_url):
        if base_url is not None:
            self.base_url = base_url


@pytest.fixture
def aux_env(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(ad, 'install', lambda: None)
    monkeypatch.setattr(rec, 'health', lambda *a, **kw: None)
    monkeypatch.setattr(rec, '_STORES', {})
    tokens = rec.CURRENT.set(None), rec.AUX.set(None)
    insert(rec.store(str(tmp_path)), catalog(), time.time() - 10)
    yield tmp_path
    rec.CURRENT.reset(tokens[0])
    rec.AUX.reset(tokens[1])


def run_aux(env, base_url, provider='anthropic', asynchronous=False):
    """Drive the installed relay wrapper with a native Anthropic message underneath."""
    usage = dict(RAW, speed='standard', inference_geo='global', service_tier='standard')
    native = {'id': 'msg_aux', 'model': 'claude-opus-9-5', 'usage': usage, 'content': 'AUX SECRET'}
    create_message = ad.anthropic_wrapper(lambda client, api_kwargs, **kw: native)
    adapted = type('Adapted', (), {'model': 'claude-opus-9-5',
                                   'usage': type('U', (), {'prompt_tokens': 1000, 'completion_tokens': 500})()})()
    if asynchronous:
        async def _relay_async_completion(client, kwargs, *, provider=None, api_mode=None, create=None):
            create_message(client, {'model': kwargs['model']})
            return adapted
        module = SimpleNamespace(_relay_sync_completion=lambda client, kwargs, **kw: None,
                                 _relay_async_completion=_relay_async_completion)
    else:
        def _relay_sync_completion(client, kwargs, *, provider=None, api_mode=None, create=None):
            create_message(client, {'model': kwargs['model']})
            return adapted
        module = SimpleNamespace(_relay_sync_completion=_relay_sync_completion,
                                 _relay_async_completion=lambda client, kwargs, **kw: None)
    ad.install_aux(module)
    relay = module._relay_async_completion if asynchronous else module._relay_sync_completion
    call = relay(AuxAnthropicClient(base_url), {'model': 'claude-opus-9-5', 'messages': ['AUX SECRET']},
                 provider=provider, api_mode='anthropic_messages')
    if asynchronous:
        import asyncio
        assert asyncio.run(call) is adapted
    else:
        assert call is adapted
    with rec.store(str(env)).db() as c:
        [(raw,)] = c.execute("SELECT data FROM requests WHERE json_extract(data,'$.source')='auxiliary_adapter'").fetchall()
        c.execute("DELETE FROM requests")
    assert 'AUX SECRET' not in raw and 'base_url' not in raw and 'proxy.example' not in raw
    return json.loads(raw)


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('base_url,endpoint,priced', [
    ('https://api.anthropic.com', 'first_party', True),
    ('https://api.anthropic.com./', 'first_party', True),
    ('https://proxy.example/anthropic', 'custom', False),     # custom gateway: never first-party valuation
    ('https://api.anthropic.com.evil.example', 'custom', False),
    (None, 'unverified', False),                               # provenance unavailable: unpriced
])
def test_auxiliary_capture_preserves_endpoint_identity(aux_env, base_url, endpoint, priced, asynchronous):
    record_ = run_aux(aux_env, base_url, asynchronous=asynchronous)
    assert record_['anthropic_endpoint'] == endpoint and record_['status'] == 'completed'
    assert record_['usage']['usage_source'] == 'native_anthropic_usage'
    assert record_['returned_speed'] == 'standard' and record_['returned_inference_geo'] == 'global'
    rate = record_['cost']['rate']
    if priced:
        assert rate['endpoint_assumption'] is False and rate['speed_inferred'] is False
        assert Decimal(record_['cost']['total_usd']) == Decimal('0.039')
    else:
        assert rate is None and record_['cost']['total_usd'] is None


def test_auxiliary_custom_endpoint_stays_unpriced_retrospectively(aux_env):
    with rec.store(str(aux_env)).db() as c:
        c.execute("DELETE FROM provider_catalog WHERE source_id='anthropic'")
    captured = {}
    for base_url, key in (('https://proxy.example/anthropic', 'custom'), (None, 'unverified'), ('https://api.anthropic.com', 'first')):
        captured[key] = run_aux(aux_env, base_url)
        assert captured[key]['cost']['rate'] is None
    s = rec.store(str(aux_env))
    for key, record_ in captured.items():
        s.request({**record_, 'id': key}, 'request_completed')
    insert(s, catalog(), time.time())
    rows = {r['id']: r for r in s.read(start=0, end=time.time() + 1)['requests']}
    assert 'supplemental_valuation' in rows['first']
    for key in ('custom', 'unverified'):
        assert 'supplemental_valuation' not in rows[key] and rows[key]['cost']['rate'] is None


def test_auxiliary_non_anthropic_route_has_no_endpoint_class(aux_env):
    assert 'anthropic_endpoint' not in run_aux(aux_env, 'https://api.anthropic.com', provider='openrouter')


def test_refresh_stores_live_catalogue_and_failure_keeps_last_good(tmp_path):
    s = Store(tmp_path)
    def only_anthropic(fetch):
        def wrapped(url):
            if url.startswith('https://platform.claude.com/'):
                return fetch(url)
            raise ConnectionError('other providers offline in fixture')
        return wrapped
    broken = {k: v for k, v in PAGES.items() if k != 'opus-9'}
    assert p.refresh(s, force=True, fetcher=only_anthropic(fetcher_for(pages=broken)))
    with s.db() as c:
        [status] = p.catalog_status(c, 'anthropic')
    assert status['status'] == 'unavailable' and status['origin'] == 'bundled_provider_snapshot_reviewed_2026-09-27'
    with s.db() as c:
        c.execute("DELETE FROM pricing_status WHERE source_id='anthropic'")
    assert p.refresh(s, force=True, fetcher=only_anthropic(fetcher_for()))
    with s.db() as c:
        [status] = p.catalog_status(c, 'anthropic')
    assert status['status'] == 'ok' and status['origin'] == 'live_public_provider'
    assert {r['model'] for r in status['rates']} >= {'claude-opus-9-5', 'claude-haiku-4-5'}
