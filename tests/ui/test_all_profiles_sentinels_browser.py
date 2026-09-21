"""Sentinel UI contracts using real read-side DTOs from disposable SQLite."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def reports():
    spec = importlib.util.spec_from_file_location('_sentinel_bootstrap', ROOT / 'bootstrap.py')
    assert spec and spec.loader
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    from _hermes_ai_usage_ledger_v2 import aggregate
    from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
    from _hermes_ai_usage_ledger_v2.storage import SCHEMA

    now = time.time()
    with tempfile.TemporaryDirectory(prefix='ui-sentinels-') as temp:
        root = Path(temp)
        for profile in (root, root / 'profiles' / 'other'):
            folder = profile / 'usage-ledger'
            folder.mkdir(parents=True)
            with sqlite3.connect(folder / 'events.sqlite3') as db:
                db.executescript(SCHEMA)
                for key, session in [('missing-session', None), ('real-request', 'real-session')]:
                    row = dict(id=key, started=now-60, ended=now-59, provider='fixture',
                               model='fixture', session_id=session, task='main', status='completed',
                               usage={'prompt_tokens': 100, 'total_tokens': 100, 'cache_read_tokens': 0})
                    db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (key, row['started'], row['ended'], 'fixture', 'fixture', session,
                                'main', None, 'completed', json.dumps(row)))
                for key, next_id in [('superseded-compaction', 'superseded'),
                                     ('linked-compaction', 'real-request'), ('waiting-compaction', None)]:
                    row = dict(id=key, started=now-120, ended=now-119, provider='fixture',
                               session_id='real-session', kind='compression', status='completed',
                               next_request_id=next_id)
                    db.execute('INSERT INTO compressions VALUES(?,?,?,?,?,?,?,?)',
                               (key, row['started'], row['ended'], 'fixture', 'real-session',
                                None, next_id, json.dumps(row)))
        runtime = AnalyticsRuntime()
        try:
            selected = runtime.read(root, now-86400, now)
            combined = aggregate.ledger(runtime, aggregate.discover(root), start=now-86400, end=now)
            assert combined['coverage']['status'] == 'complete'
            assert len(combined['session_groups']) == 4
            assert any(g['original_ids']['key'] == 'unattributed' and g['key'].startswith('ap1.')
                       for g in combined['session_groups'])
            assert any(c['original_ids']['next_request_id'] == 'superseded'
                       for c in combined['compressions'] if c.get('next_request_id'))
            return {'selected': selected, 'all': combined}
        finally:
            runtime._discard(runtime.current)


def run():
    payloads = reports()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True,
                                   args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1700, 'height': 1100})
        errors, network = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: network.append(r.url))
        page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
        picker = page.get_by_role('combobox', name='Hermes profile', exact=True)
        expect(picker).to_have_value('profile:infra')
        page.evaluate('''payloads => {
          const original = rest; window.sentinelCalls = [];
          rest = (path, options) => {
            const u = new URL(path, 'https://offline');
            if (u.pathname !== '/ledger') return original(path, options);
            sentinelCalls.push(Object.fromEntries(u.searchParams));
            return Promise.resolve(payloads[u.searchParams.get('profile_scope') === 'all' ? 'all' : 'selected']);
          };
        }''', payloads)
        page.get_by_role('tab', name='All providers', exact=True).click()
        for scope, copies in [('profile:default', 1), ('scope:all', 2)]:
            picker.select_option(scope)
            page.get_by_role('tab', name='Overview', exact=True).click()
            page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name='Session', exact=True).click()
            missing = page.locator('.au-breakdown tbody tr').filter(has_text='unattributed')
            expect(missing).to_have_count(copies)
            expect(missing.locator('button')).to_have_count(0)
            missing.first.locator('td').first.click()
            expect(page.get_by_role('textbox', name='Session ID', exact=True)).to_have_value('')
            expect(page.get_by_role('tab', name='Overview', exact=True)).to_have_attribute('aria-selected', 'true')
            real = page.locator('.au-breakdown tbody button').filter(has_text='real-session').first
            key = real.get_attribute('title')
            assert key
            assert key.startswith('ap1.') if copies == 2 else key == 'real-session'
            real.click()
            expect(page.get_by_role('textbox', name='Session ID', exact=True)).to_have_value(key)
            page.wait_for_function('(key) => sentinelCalls.some(c => c.session === key)', arg=key)
            page.get_by_role('button', name='Show all requests', exact=True).click()
            page.get_by_role('tab', name='Compressions', exact=True).click()
            table = page.get_by_test_id('compression-table')
            expect(table.get_by_text('Superseded by another compaction', exact=True)).to_have_count(copies)
            expect(table.get_by_text('First subsequent attempt', exact=True)).to_have_count(copies)
            expect(table.get_by_text('Awaiting next attempt', exact=True)).to_have_count(copies)
        assert not errors, errors
        assert not network, network
        browser.close()
    print('PASS real backend selected/aggregate DTOs: unattributed groups are inert, real session IDs drill unchanged, superseded/linked/awaiting compression labels remain distinct')


if __name__ == '__main__':
    run()
