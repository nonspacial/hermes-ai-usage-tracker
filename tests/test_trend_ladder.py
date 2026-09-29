"""Adaptive UTC trend-bucket ladder on disposable fixture ledgers; no live profiles."""
import sqlite3
import time

import pytest

from _hermes_ai_usage_ledger_v2 import aggregate
from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
from _hermes_ai_usage_ledger_v2.storage import SCHEMA, Store, sql_trend

LADDER = [
    (1800, 60, 'minute'),
    (7200, 120, '2 minutes'),
    (21600, 300, '5 minutes'),
    (43200, 600, '10 minutes'),
    (86400, 900, '15 minutes'),
    (172800, 1800, '30 minutes'),
    (604800, 3600, 'hour'),
    (2592000, 21600, '6 hours'),
    (7776000, 86400, 'day'),
]
# The ladder lives inside storage.sql_trend (hot-reloadable read definition),
# so it is observed through tiny disposable reads rather than imported.
TREND_SLACK = 60
TREND_EXACT_LIMITS = {1800, 7200}
ARGS = ('', 0, 50, '', '', '', 'exact', '', '', '')
_EMPTY = sqlite3.connect(':memory:')
_EMPTY.row_factory = sqlite3.Row
_EMPTY.executescript(SCHEMA)
_ORIGIN = 1_800_000_000


def trend_resolution(duration):
    """(step, unit) sql_trend picks for ``duration``, read from an empty in-memory ledger.

    The empty table yields no grouped rows, so a trivial summary projection suffices.
    """
    trend = sql_trend(_EMPTY, '1', [], _ORIGIN, _ORIGIN + duration, 'COUNT(*) AS n')
    return trend['seconds'], trend['unit']


def record(store, key, started, tokens=10, provider='fixture'):
    store.request({'id': key, 'started': started, 'ended': started + 1, 'provider': provider,
                   'model': 'fixture', 'session_id': 'fixture-session', 'status': 'completed',
                   'usage': {'total_tokens': tokens}}, 'request_completed')


def comparable(response):
    return {k: v for k, v in response.items() if k not in ('generated_at', 'incremental')}


def reach(limit):
    """Inclusive maximum a rung accepts: nominal plus rolling slack, bar the fine rungs."""
    return limit + (0 if limit in TREND_EXACT_LIMITS else TREND_SLACK)


def test_ladder_table_boundaries_and_half_hour_grid():
    previous = 0
    for limit, step, unit in LADDER:
        if previous:
            assert trend_resolution(reach(previous) + 0.001) == (step, unit)
        assert trend_resolution(limit) == (step, unit)
        assert trend_resolution(reach(limit)) == (step, unit)
        # Every step lands on the frontend's half-hour selection edges.
        assert 1800 % step == 0 or step % 1800 == 0
        previous = limit
    assert trend_resolution(0) == (60, 'minute')
    # The two fine rungs keep their exact legacy boundaries.
    assert trend_resolution(1800.1) == (120, '2 minutes')
    assert trend_resolution(7200.1) == (300, '5 minutes')
    assert trend_resolution(7776060.001) == (86400, 'day')
    assert trend_resolution(400 * 86400) == (86400, 'day')
    assert trend_resolution(400 * 86400 + 1) == (172800, '2 days')
    assert trend_resolution(1000 * 86400) == (259200, '3 days')
    durations = sorted({*range(0, 8_000_000, 3571), *range(0, 1200 * 86400, 86399),
                        *(reach(limit) + delta for limit, _, _ in LADDER for delta in (0, 0.001))})
    steps = [trend_resolution(d)[0] for d in durations]
    assert steps == sorted(steps)
    assert all(d / trend_resolution(d)[0] <= 400 for d in range(1, 1200 * 86400, 86399))


@pytest.mark.parametrize(('duration', 'step', 'unit'), [
    # Rolling periods: client-computed start, server-resolved end a few seconds later.
    (3600 + 5, 120, '2 minutes'),
    (86400 + 5, 900, '15 minutes'),
    (604800 + 5, 3600, 'hour'),
    (2592000 + 5, 21600, '6 hours'),
    (7776000 + 5, 86400, 'day'),
    # Exactly at the slack edge the rung still holds.
    (86400 + 60, 900, '15 minutes'),
    (604800 + 60, 3600, 'hour'),
    (2592000 + 60, 21600, '6 hours'),
    (7776000 + 60, 86400, 'day'),
    # Just past the slack lands on the next rung.
    (21600 + 61, 600, '10 minutes'),
    (43200 + 61, 900, '15 minutes'),
    (86400 + 61, 1800, '30 minutes'),
    (172800 + 61, 3600, 'hour'),
    (604800 + 61, 21600, '6 hours'),
    (2592000 + 61, 86400, 'day'),
    (7776000 + 61, 86400, 'day'),
])
def test_rolling_slack_keeps_the_intended_rung(duration, step, unit):
    assert trend_resolution(duration) == (step, unit)


@pytest.mark.parametrize(('period', 'step'), [(604800, 3600), (2592000, 21600), (7776000, 86400)])
def test_rolling_omitted_end_read_keeps_the_intended_rung(tmp_path, period, step):
    store = Store(tmp_path)
    start = time.time() - period
    record(store, 'recent', start + period / 2, 3)
    time.sleep(0.01)
    data = store.read(start, None)
    assert data['window']['end'] - start > period
    assert data['trend']['seconds'] == step
    assert sum(b['attempts'] for b in data['trend']['buckets']) == 1


@pytest.mark.parametrize(('duration', 'step', 'unit'),
                         [(limit, step, unit) for limit, step, unit in LADDER] +
                         [(401 * 86400, 172800, '2 days')])
def test_every_rung_is_utc_aligned_half_open_and_clipped(tmp_path, duration, step, unit):
    store = Store(tmp_path)
    start = 1_800_000_007.5
    end = start + duration
    record(store, 'at-start', start, 1)
    record(store, 'before', start - 0.5, 100)
    record(store, 'last', end - 0.5, 10)
    record(store, 'at-end', end, 1000)
    data = store.read(start, end)
    trend = data['trend']
    assert (trend['seconds'], trend['unit'], trend['timezone']) == (step, unit, 'UTC')
    buckets = trend['buckets']
    assert buckets[0]['start'] == start and buckets[-1]['end'] == end
    assert all(a['end'] == b['start'] for a, b in zip(buckets, buckets[1:]))
    assert all(b['start'] % step == 0 for b in buckets[1:])
    assert all(b['end'] - b['start'] <= step for b in buckets)
    assert len(buckets) <= 401
    assert buckets[0]['known']['total_tokens'] == 1
    assert buckets[-1]['known']['total_tokens'] == 10
    assert sum(b['attempts'] for b in buckets) == data['summary']['attempts'] == 2


def test_omitted_end_resolves_to_now_and_uses_actual_duration(tmp_path):
    store = Store(tmp_path)
    now = time.time()
    start = now - 3 * 86400 - 5
    record(store, 'old', start + 10, 1)
    record(store, 'recent', now - 30, 2)
    before = time.time()
    data = store.read(start, None)
    after = time.time()
    window, trend = data['window'], data['trend']
    assert before <= window['end'] <= after
    assert (trend['seconds'], trend['unit']) == trend_resolution(window['end'] - start) == (3600, 'hour')
    assert trend['buckets'][0]['start'] == start and trend['buckets'][-1]['end'] == window['end']
    recent = next(b for b in trend['buckets'] if b['start'] <= now - 30 < b['end'])
    assert recent['known']['total_tokens'] == 2
    assert sum(b['attempts'] for b in trend['buckets']) == 2
    # 'Since a point until now' on a short span stays fine-grained.
    short = store.read(now - 5000, None)['trend']
    assert (short['seconds'], short['unit']) == (120, '2 minutes')


def test_omitted_end_all_profiles_merge_on_the_same_grid(tmp_path):
    now = time.time()
    start = now - 10 * 86400
    record(Store(tmp_path), 'first', now - 3 * 3600, 5)
    record(Store(tmp_path / 'profiles' / 'other'), 'second', now - 3 * 3600 + 60, 7)
    runtime = AnalyticsRuntime()
    try:
        result = aggregate.ledger(runtime, aggregate.discover(tmp_path), start=start, end=None)
    finally:
        runtime._discard(runtime.current)
    trend = result['trend']
    assert (trend['seconds'], trend['unit']) == (21600, '6 hours')
    assert trend['buckets'][0]['start'] == start
    assert trend['buckets'][-1]['end'] == result['window']['end']
    assert all(b['start'] % 21600 == 0 for b in trend['buckets'][1:])
    assert all(a['end'] == b['start'] for a, b in zip(trend['buckets'], trend['buckets'][1:]))
    hit = [b for b in trend['buckets'] if b['attempts']]
    assert sum(b['known']['total_tokens'] for b in hit) == 12 == result['summary']['known']['total_tokens']


@pytest.mark.parametrize(('days', 'step'), [(1.5, 1800), (10, 21600)])
def test_omitted_end_signed_refresh_matches_full_read(tmp_path, days, step):
    store = Store(tmp_path)
    now = time.time()
    start = now - days * 86400
    record(store, 'old', start + 100, 1)
    runtime = AnalyticsRuntime()
    try:
        base = runtime.read(tmp_path, start, None, *ARGS, view='overview', group='time')
        assert base['trend']['seconds'] == step
        sealed_end = base['window']['end']
        record(store, 'new', sealed_end - 60, 2)
        refreshed = runtime.refresh(tmp_path, start, None, *ARGS,
                                    resume_token=base['incremental']['resume_token'], group='time')
        expected = runtime.read(tmp_path, start, sealed_end, *ARGS, view='overview', group='time')
    finally:
        runtime._discard(runtime.current)
    assert refreshed['incremental']['mode'] == 'delta'
    assert refreshed['window']['end'] == sealed_end
    assert comparable(refreshed) == comparable(expected)
    assert refreshed['trend']['seconds'] == step
    assert refreshed['summary']['attempts'] == 2
