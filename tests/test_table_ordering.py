"""Tables follow latest request starts across the whole filtered dataset."""
from _hermes_ai_usage_ledger_v2.storage import Store


def add(store, key, started, group):
    store.request(dict(id=key, started=started, ended=started + 1,
        provider='provider-' + group, model='model-' + group,
        response_model='returned-' + group, task='task-' + group,
        session_id='session-' + group, root_session_id='session-' + group,
        project_id='project-' + group, project_label=group,
        subagent_id='child-' + group, agent_kind='subagent',
        source='main_hook', status='completed'), 'request_completed')


def assert_groups(data, order):
    for name, key, prefix in (
        ('groups', 'model', 'model-'),
        ('model_groups', 'model', 'returned-'),
        ('provider_groups', 'provider', 'provider-'),
        ('project_groups', 'key', 'project-'),
        ('session_groups', 'key', 'session-'),
        ('subagent_groups', 'key', 'child-'),
        ('applied_rate_groups', 'model', 'returned-'),
    ):
        assert [row[key] for row in data[name]] == [prefix + g for g in order], name


def test_all_group_tables_use_latest_call_not_label_or_page(tmp_path):
    store = Store(tmp_path)
    # Deliberately insert out of time order. Alpha is old until its latest call.
    for key, started, group in [('a-old', 10, 'alpha'), ('a-new', 30, 'alpha'),
                                ('b', 20, 'beta'), ('z', 25, 'zeta')]:
        add(store, key, started, group)
    data = store.read(0, 40, offset=2, limit=1)
    assert [r['id'] for r in data['requests']] == ['b']
    assert data['request_count'] == 4
    assert_groups(data, ['alpha', 'zeta', 'beta'])
    # The newest call outside a chosen window must not affect that window.
    assert_groups(store.read(0, 29, limit=1), ['zeta', 'beta', 'alpha'])
    assert_groups(store.read(0, 40, provider='provider-beta'), ['beta'])
    assert_groups(store.read(0, 40, project='project-zeta'), ['zeta'])
    assert_groups(store.read(100, 200), [])


def test_equal_group_times_have_stable_identity_ties(tmp_path):
    store = Store(tmp_path)
    for group in ['zeta', 'alpha', 'beta']:
        add(store, group, 10, group)
    assert_groups(store.read(0, 20), ['alpha', 'beta', 'zeta'])


def test_existing_request_and_compression_order_and_chart_preserved(tmp_path):
    store = Store(tmp_path)
    for key, started in [('middle', 4000), ('new', 8000), ('old', 100)]:
        add(store, key, started, key)
        store.compression(dict(id=key, started=started, ended=started + 1,
                               session_id='s', kind='compression'))
    data = store.read(0, 10000, limit=2)
    assert [r['id'] for r in data['requests']] == ['new', 'middle']
    assert [r['id'] for r in store.read(0, 10000, offset=2, limit=2)['requests']] == ['old']
    assert [r['id'] for r in data['compressions']] == ['new', 'middle', 'old']
    starts = [b['start'] for b in data['trend']['buckets']]
    assert starts == sorted(starts)


def test_saved_rate_revisions_sort_by_last_call_not_rate_value(tmp_path, monkeypatch):
    store = Store(tmp_path)
    rate = {'provider': 'provider-alpha', 'model': 'model-alpha',
            'service_tier': 'standard', 'input_tokens': '1'}
    monkeypatch.setattr(store, 'latest_rate', lambda *_: dict(rate))
    for key, started, price in [('old', 10, '1'), ('new', 30, '9'), ('middle', 20, '5')]:
        rate['input_tokens'] = price
        store.request(dict(id=key, started=started, ended=started + 1,
            provider='provider-alpha', model='model-alpha', session_id='s',
            task='main', status='completed', usage={'input_tokens': 10}), 'request_completed')
    rows = store.read(0, 40, limit=1)['applied_rate_groups']
    assert [g['rate']['input_tokens'] for g in rows] == ['9', '5', '1']
