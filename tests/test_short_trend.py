"""Short trend windows use aligned, UTC two-minute buckets on fixture ledgers."""

import pytest

from _hermes_ai_usage_ledger_v2 import aggregate
from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
from _hermes_ai_usage_ledger_v2.storage import Store


def record(store, key, started, tokens=10, provider='fixture'):
    data = {'id': key, 'started': started, 'ended': started + 1,
            'provider': provider, 'model': 'fixture', 'session_id': 'fixture-session',
            'status': 'completed'}
    if tokens is not None:
        data['usage'] = {'total_tokens': tokens}
    store.request(data, 'request_completed')


def test_short_trend_is_utc_aligned_clipped_and_independent_of_page(tmp_path):
    store = Store(tmp_path)
    # The selected interval is exactly one hour but straddles UTC hour boundaries.
    start, end = 7205, 10805
    for key, ts, tokens in [('before', 7204, 90), ('first', 7205, 10),
                            ('edge-before', 7319, 20), ('edge-after', 7320, None),
                            ('last', 10804, 40), ('after', 10805, 90)]:
        record(store, key, ts, tokens)

    data = store.read(start, end, limit=1)
    trend = data['trend']
    buckets = trend['buckets']
    assert (trend['unit'], trend['seconds'], trend['timezone']) == ('2 minutes', 120, 'UTC')
    assert len(buckets) == 31
    assert [(b['start'], b['end']) for b in buckets[:2]] == [(7205, 7320), (7320, 7440)]
    assert (buckets[-1]['start'], buckets[-1]['end']) == (10800, 10805)
    assert all(left['end'] == right['start'] for left, right in zip(buckets, buckets[1:]))
    assert [b['attempts'] for b in buckets[:3]] == [2, 1, 0]
    assert buckets[1]['missing_usage'] == 1
    assert buckets[1]['missing_fields']['total_tokens'] == 1
    assert buckets[-1]['known']['total_tokens'] == 40
    assert len(data['requests']) == 1
    assert data['summary']['attempts'] == sum(b['attempts'] for b in buckets) == 4
    assert data['summary']['known']['total_tokens'] == sum(b['known']['total_tokens'] for b in buckets) == 70


def test_short_trend_respects_filters_and_empty_windows(tmp_path):
    store = Store(tmp_path)
    record(store, 'selected', 7201, 11, provider='selected')
    record(store, 'other', 7321, 20, provider='other')
    selected = store.read(7200, 10800, provider='selected')['trend']
    assert len(selected['buckets']) == 30
    assert selected['buckets'][0]['attempts'] == 1
    assert sum(bucket['attempts'] for bucket in selected['buckets']) == 1
    empty = store.read(7200, 10800, provider='absent')['trend']
    assert empty['unit'] == '2 minutes' and empty['seconds'] == 120
    assert len(empty['buckets']) == 30
    assert all(bucket['attempts'] == 0 for bucket in empty['buckets'])


@pytest.mark.parametrize(('duration', 'unit', 'seconds'), [
    (3600.5, '2 minutes', 120),
    (7200, '2 minutes', 120),
    (7201, 'hour', 3600),
    (172800, 'hour', 3600),
    (172801, 'day', 86400),
])
def test_longer_windows_preserve_existing_granularity(tmp_path, duration, unit, seconds):
    trend = Store(tmp_path).read(7200, 7200 + duration)['trend']
    assert (trend['unit'], trend['seconds'], trend['timezone']) == (unit, seconds, 'UTC')


def test_all_profiles_merge_short_buckets_on_the_same_utc_grid(tmp_path):
    first = Store(tmp_path)
    second = Store(tmp_path / 'profiles' / 'other')
    record(first, 'first', 7201, 10)
    record(second, 'second', 7321, 20)
    runtime = AnalyticsRuntime()
    try:
        result = aggregate.ledger(runtime, aggregate.discover(tmp_path), start=7200, end=10800)
    finally:
        runtime._discard(runtime.current)
    trend = result['trend']
    assert (trend['unit'], trend['seconds'], trend['timezone']) == ('2 minutes', 120, 'UTC')
    assert len(trend['buckets']) == 30
    assert [b['attempts'] for b in trend['buckets'][:3]] == [1, 1, 0]
    assert sum(b['known']['total_tokens'] for b in trend['buckets']) == result['summary']['known']['total_tokens'] == 30
