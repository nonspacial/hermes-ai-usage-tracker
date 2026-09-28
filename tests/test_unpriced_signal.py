"""Loud unpriced signal: per-group reasons, header totals, incremental receipts and paging parity.

Disposable ledgers and synthetic catalogues only; no network, no real home.
"""
import json
import time

import pytest

from ledger_runtime import pricing as p
from ledger_runtime.aggregate import discover, ledger as all_profiles
from ledger_runtime.analytics_reload import AnalyticsRuntime
from ledger_runtime.projection import COMMON
from ledger_runtime.storage import Store, summary

REASONS = ('no_catalogue', 'model_missing', 'ambiguous', 'usage_missing', 'endpoint_unpriced')
USAGE = dict(input_tokens=100, output_tokens=20, cache_read_tokens=0, cache_write_tokens=0,
             reasoning_tokens=0, prompt_tokens=100, total_tokens=120)
ARGS = (100., 50_000., '', 0, 50, '', '', '', 'exact', '', '', '')


def put(store, key, started, provider, model, *, usage=USAGE, **extra):
    store.request(dict(id=key, started=started, ended=started + 1, provider=provider, model=model,
                       session_id='s', source='main_hook', task='main', status='completed',
                       usage=dict(usage) if usage else None, **extra), 'request_completed')


def router(store, stamp=50.0):
    rows = [p.rate('x-ai/grok-9', 'standard', ['3', '15', '0.75', '0']),
            p.rate('stealth/zero', 'standard', ['0', '0', None, None])]
    with store.db() as c:
        c.execute('INSERT INTO provider_catalog(source_id,observed,data) VALUES(?,?,?)',
                  ('openrouter', stamp, json.dumps(p._snapshot('openrouter', rows, stamp, 'fixture_provider_catalog'))))


def mixed(root, *, catalogue=True):
    store = Store(root)
    if catalogue:
        router(store)
    put(store, 'priced', 200, 'xai', 'grok-9')                 # list-price fallback
    put(store, 'free', 300, 'custom', 'stealth/zero')          # known $0
    put(store, 'missing', 3800, 'xai', 'grok-unknown')         # model_missing
    put(store, 'premium', 3900, 'openrouter', 'x-ai/grok-9', service_tier='priority')  # router lists standard only
    put(store, 'empty', 7400, 'xai', 'grok-9', usage=None)     # usage_missing
    return store


def reasons(**counts):
    return {name: counts.get(name, 0) for name in REASONS}


def test_summary_groups_and_header_carry_unpriced(tmp_path):
    store = mixed(tmp_path)
    d = store.read(*ARGS[:2], view='overview', group='model')
    assert 'unpriced_total' in COMMON and 'unpriced_total' in d['projection']['included']
    s = d['summary']
    assert s['unpriced']['requests'] == s['unpriced_requests'] == 3
    assert s['unpriced']['tokens'] == 240
    assert s['unpriced']['reasons'] == reasons(model_missing=1, usage_missing=1, endpoint_unpriced=1)
    assert d['unpriced_total'] == {'requests': 3, 'tokens': 240}
    groups = {(g['provider'], g['model']): g for g in d['model_groups']}
    assert groups[('xai', 'grok-9')]['unpriced'] == {'requests': 1, 'tokens': 0, 'reasons': reasons(usage_missing=1)}
    assert groups[('custom', 'stealth/zero')]['unpriced'] == {'requests': 0, 'tokens': 0, 'reasons': reasons()}
    assert groups[('custom', 'stealth/zero')]['known_cost_usd'] == '0'
    assert groups[('xai', 'grok-unknown')]['unpriced']['reasons'] == reasons(model_missing=1)
    providers = {g['provider']: g for g in d['provider_groups']}
    assert providers['xai']['unpriced']['requests'] == 2
    for bucket in d['trend']['buckets']:
        assert set(bucket['unpriced']) == {'requests', 'tokens', 'reasons'}
        for row in bucket['provider_buckets']:
            assert set(row['unpriced']['reasons']) == set(REASONS)
    # SQL and Python summaries agree over the same projected rows.
    full = store.read(*ARGS[:2])
    assert full['summary'] == summary(full['requests'])
    by_id = {r['id']: r for r in full['requests']}
    assert by_id['missing']['unpriced_reason'] == 'model_missing'
    assert by_id['empty']['unpriced_reason'] == 'usage_missing'
    assert 'unpriced_reason' not in by_id['priced'] and 'unpriced_reason' not in by_id['free']
    # Requests DTO carries the reason; applied-rate groups carry the group signal.
    cache = store.read(*ARGS[:2], view='cache')
    assert all('unpriced' in g for g in cache['applied_rate_groups'])
    # Reasons are read-time only: stored rows never gain the field.
    with store.db() as c:
        assert all('unpriced_reason' not in json.loads(r[0]) for r in c.execute('SELECT data FROM requests'))


def test_no_catalogue_reason_before_router_refresh(tmp_path):
    store = mixed(tmp_path, catalogue=False)
    s = store.read(*ARGS[:2])['summary']
    assert s['unpriced']['reasons']['no_catalogue'] >= 2  # grok-9 and grok-unknown have no router snapshot
    assert s['unpriced']['reasons']['usage_missing'] == 1


def test_endpoint_unpriced_reason_for_known_premium(tmp_path):
    store = Store(tmp_path)
    router(store)
    put(store, 'fast', 200, 'xai', 'grok-9', service_tier='priority')
    put(store, 'aux', 300, 'anthropic', 'claude-opus-4-5', returned_usage_service_tier='batch')
    s = store.read(*ARGS[:2])['summary']
    assert s['unpriced']['reasons'] == reasons(endpoint_unpriced=2)


def test_all_profiles_merge_unpriced(tmp_path):
    mixed(tmp_path)
    mixed(tmp_path / 'profiles' / 'other')
    runtime = AnalyticsRuntime()
    try:
        out = all_profiles(runtime, discover(tmp_path), start=ARGS[0], end=ARGS[1], view='overview', group='model')
        assert out['unpriced_total'] == {'requests': 6, 'tokens': 480}
        assert out['summary']['unpriced']['requests'] == 6
        groups = {(g['provider'], g['model']): g for g in out['model_groups']}
        assert groups[('xai', 'grok-unknown')]['unpriced']['reasons']['model_missing'] == 2
        assert all(sum(g['unpriced']['reasons'].values()) == g['unpriced']['requests'] for g in out['model_groups'])
        selected = runtime.read(tmp_path, *ARGS, view='overview', group='model')
        assert selected['unpriced_total'] == {'requests': 3, 'tokens': 240}
    finally:
        runtime._discard(runtime.current)


def test_paging_parity_of_unpriced_groups(tmp_path):
    store = mixed(tmp_path)
    full = store.read(*ARGS[:2], view='overview', group='model')
    pages, offset = [], 0
    while True:
        page = store.read(ARGS[0], ARGS[1], offset=offset, limit=2, view='overview', group='model', list_mode='page')
        pages += page['model_groups']
        assert page['unpriced_total'] == full['unpriced_total']
        if page['list_next_offset'] is None:
            break
        offset = page['list_next_offset']
    assert [(g['provider'], g['model'], g['unpriced']) for g in pages] == \
           [(g['provider'], g['model'], g['unpriced']) for g in full['model_groups']]


def comparable(response):
    return {k: v for k, v in response.items() if k not in ('generated_at', 'incremental')}


@pytest.mark.parametrize('group', ['time', 'model'])
def test_incremental_delta_carries_unpriced_fields(tmp_path, group):
    store = mixed(tmp_path)
    runtime = AnalyticsRuntime()
    try:
        base = runtime.read(tmp_path, *ARGS, view='overview', group=group)
        assert base['incremental']['resume_token']
        put(store, 'later', 7500, 'xai', 'grok-missing-2')
        delta = runtime.refresh(tmp_path, *ARGS, resume_token=base['incremental']['resume_token'], group=group)
        assert delta['incremental']['mode'] == 'delta'
        expected = runtime.read(tmp_path, *ARGS, view='overview', group=group)
        assert comparable(delta) == comparable(expected)
        assert delta['unpriced_total'] == {'requests': 4, 'tokens': 360}
        assert delta['summary']['unpriced']['reasons']['model_missing'] == 2
    finally:
        runtime._discard(runtime.current)


@pytest.mark.parametrize('strip', ['header', 'summary', 'bucket', 'provider_bucket', 'seed'])
def test_receipt_lacking_unpriced_fields_falls_back_to_full_read(tmp_path, strip):
    store = mixed(tmp_path)
    runtime = AnalyticsRuntime()
    try:
        base = runtime.read(tmp_path, *ARGS, view='overview', group='time')
        parts = runtime._unseal(base['incremental']['resume_token'])
        old = parts[8]
        seed = parts[9]
        if strip == 'header':
            old.pop('unpriced_total')
        elif strip == 'summary':
            old['summary'].pop('unpriced')
        elif strip == 'bucket':
            old['trend']['buckets'][0].pop('unpriced')
        elif strip == 'provider_bucket':
            next(b for b in old['trend']['buckets'] if b['provider_buckets'])['provider_buckets'][0].pop('unpriced')
        else:
            for groups in seed['provider'].values():
                for row in groups.values():
                    row.pop('unpriced', None)
        # Re-seal as a legitimately signed pre-change receipt (same key).
        root, profile_key, _, args, view, group, identity, generation = parts[:8]
        token = runtime._seal(root, args, view, group, old, seed, identity, generation, profile_key)
        put(store, 'later', 7500, 'xai', 'grok-missing-2')
        refreshed = runtime.refresh(tmp_path, *ARGS, resume_token=token)
        assert refreshed['incremental']['mode'] == 'snapshot'
        assert refreshed['unpriced_total'] == {'requests': 4, 'tokens': 360}
    finally:
        runtime._discard(runtime.current)


def test_tampered_receipt_still_fails_validation(tmp_path):
    mixed(tmp_path)
    runtime = AnalyticsRuntime()
    try:
        base = runtime.read(tmp_path, *ARGS, view='overview', group='time')
        token = base['incremental']['resume_token']
        with pytest.raises(ValueError):
            runtime.refresh(tmp_path, *ARGS, resume_token=token[:-6] + 'AAAAAA')
    finally:
        runtime._discard(runtime.current)


# --- Parity: already-priced rows and every existing DTO field unchanged ----------

NEW_FIELDS = {'unpriced', 'unpriced_total', 'unpriced_reason'}


def strip_new(value):
    if isinstance(value, dict):
        return {k: strip_new(v) for k, v in value.items() if k not in NEW_FIELDS}
    if isinstance(value, list):
        return [strip_new(v) for v in value]
    return value


@pytest.fixture(scope='module')
def reference_store():
    import importlib, importlib.util, io, subprocess, sys, tarfile, tempfile
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    try:
        blob = subprocess.run(['git', '-C', str(root), 'archive', 'b3fddc8', 'ledger_runtime'],
                              check=True, capture_output=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip('reference commit unavailable')
    folder = Path(tempfile.mkdtemp(prefix='unpriced-parity-reference-'))
    with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
        archive.extractall(folder, filter='data')
    name = '_unpriced_parity_reference'
    spec = importlib.util.spec_from_file_location(name, folder / 'ledger_runtime' / '__init__.py',
                                                  submodule_search_locations=[str(folder / 'ledger_runtime')])
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module; spec.loader.exec_module(module)
    yield importlib.import_module(name + '.storage').Store
    for key in [k for k in sys.modules if k == name or k.startswith(name + '.')]:
        sys.modules.pop(key, None)


def test_priced_rows_and_existing_fields_identical_to_reference(tmp_path, reference_store):
    store = Store(tmp_path)
    catalogues = {}
    with store.db() as c:
        for sid in p.SOURCES:
            row = c.execute('SELECT data FROM provider_catalog WHERE source_id=? ORDER BY observed DESC LIMIT 1', (sid,)).fetchone()
            if row:
                catalogues[sid] = json.loads(row[0])
    stamp = 7000
    for i, (provider, sid) in enumerate(sorted(p.PROVIDERS.items())):
        if sid not in catalogues or not catalogues[sid]['rates']:
            continue
        model = catalogues[sid]['rates'][0]['model']
        extra = {'anthropic_endpoint': 'first_party'} if provider == 'anthropic' else {}
        put(store, f'priced-{i}', stamp + i, provider, model, **extra)
    put(store, 'pending', 7300, 'openai-codex', 'gpt-5.5', usage=None)
    new = {'full': store.read(*ARGS[:2])}
    for group in ('time', 'model'):
        new[group] = store.read(*ARGS[:2], view='overview', group=group)
    old_store = reference_store(tmp_path)
    old = {'full': old_store.read(*ARGS[:2])}
    for group in ('time', 'model'):
        old[group] = old_store.read(*ARGS[:2], view='overview', group=group)
    priced = [r for r in new['full']['requests'] if r['cost'].get('complete')]
    assert {r['provider'] for r in priced} >= {'openai-codex', 'openrouter', 'nous', 'ollama', 'anthropic'}, \
        sorted({r['provider'] for r in priced})
    for key in new:
        a, b = strip_new(new[key]), strip_new(old[key])
        a.pop('generated_at', None); b.pop('generated_at', None)
        for d in (a, b):
            if 'projection' in d:
                d['projection'] = {**d['projection'], 'included': [f for f in d['projection']['included'] if f not in NEW_FIELDS]}
        assert a == b, key
