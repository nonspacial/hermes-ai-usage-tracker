"""Offline mount/unmount restoration of the real plugin component (synthetic data only)."""
from pathlib import Path
import os
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def run():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
            headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            page = browser.new_page(viewport={'width': 1480, 'height': 1000})
            page.set_default_timeout(8000)
            errors, network = [], []
            page.on('pageerror', lambda exc: errors.append(str(exc)))
            page.on('request', lambda req: network.append(req.url))
            page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
            tabs = page.get_by_role('navigation', name='Providers', exact=True)
            tabs.get_by_role('tab', name='Codex', exact=True).click()
            page.get_by_role('group', name='Usage display').get_by_role('button', name='Cost').click()
            page.get_by_role('group', name='Time window').get_by_role('button', name='7 days').click()
            page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name='Project').click()
            page.get_by_role('combobox', name='Agent scope').select_option('subagent')
            page.get_by_role('combobox', name='Project').select_option('repo-hermes')
            page.get_by_role('textbox', name='Session ID').fill('demo-session-1')
            page.get_by_role('navigation', name='Provider subpages').get_by_role('tab', name='Requests').click()
            stored = page.evaluate('window.demoStored["usage-view-v1:profile%3Ainfra"]')
            assert stored['profile'] == 'profile:infra' and stored['version'] == 1
            assert stored['provider'] == 'openai-codex' and stored['tab'] == 'Requests'
            assert stored['period'] == '7d' and stored['group'] == 'project'
            assert stored['filters']['session'] == 'demo-session-1'
            assert 'auto' not in str(stored).lower() and 'token' not in str(stored).lower()
            before = len(page.evaluate('window.demoCalls'))
            page.evaluate('''() => {ReactDOM.render(null, document.getElementById('root'));
                ReactDOM.render(h(() => window.page(), {}), document.getElementById('root'))}''')
            expect(tabs.get_by_role('tab', name='Codex', exact=True)).to_have_attribute('aria-selected', 'true')
            expect(page.get_by_role('navigation', name='Provider subpages').get_by_role('tab', name='Requests')).to_have_attribute('aria-selected', 'true')
            expect(page.get_by_role('group', name='Time window').get_by_role('button', name='7 days')).to_have_attribute('aria-pressed', 'true')
            expect(page.get_by_role('group', name='Usage display').get_by_role('button', name='Cost')).to_have_attribute('aria-pressed', 'true')
            assert page.get_by_role('combobox', name='Agent scope').input_value() == 'subagent'
            assert page.get_by_role('combobox', name='Project').input_value() == 'repo-hermes'
            assert page.get_by_role('textbox', name='Session ID').input_value() == 'demo-session-1'
            calls = page.evaluate('window.demoCalls.slice(%s)' % before)
            assert any('/ledger?' in c and 'provider=openai-codex' in c and 'session=demo-session-1' in c for c in calls), calls
            assert sum(c.startswith('/ledger?') for c in calls) <= 2, calls
            assert not any('consume' in c or '/reset' in c for c in calls), calls
            # Navigation state is per profile; switching to an unsaved profile starts at Subscriptions.
            page.get_by_role('combobox', name='Hermes profile').select_option('profile:default')
            expect(page.get_by_test_id('quota-home')).to_be_visible()
            tabs.get_by_role('tab', name='Nous Portal').click()
            page.get_by_role('combobox', name='Hermes profile').select_option('profile:infra')
            expect(tabs.get_by_role('tab', name='Codex')).to_have_attribute('aria-selected', 'true')
            assert page.get_by_role('textbox', name='Session ID').input_value() == 'demo-session-1'
            subpages = page.get_by_role('navigation', name='Provider subpages')
            subpages.get_by_role('tab', name='Skills usage').click()
            model = page.get_by_label('Skills model')
            first_model = model.locator('option').nth(1).get_attribute('value')
            if first_model:
                model.select_option(first_model)
            page.get_by_role('group', name='Skills view').get_by_role('button', name='Context footprint').click()
            expect(page.get_by_test_id('skill-context')).to_be_visible()
            page.evaluate('''() => {ReactDOM.render(null, document.getElementById('root'));
                ReactDOM.render(h(() => window.page(), {}), document.getElementById('root'))}''')
            expect(subpages.get_by_role('tab', name='Skills usage')).to_have_attribute('aria-selected', 'true')
            if first_model:
                expect(page.get_by_label('Skills model')).to_have_value(first_model)
            expect(page.get_by_role('group', name='Skills view').get_by_role('button', name='Context footprint')).to_have_attribute('aria-pressed', 'true')
            # Active period changes retain the pre-existing Skills clearing rule;
            # a remount without a period change above did not clear the model.
            page.get_by_role('group', name='Time window').get_by_role('button', name='30 days').click()
            expect(page.get_by_label('Skills model')).to_have_value('')
            page.evaluate('''() => {ReactDOM.render(null, document.getElementById('root'));
                ReactDOM.render(h(() => window.page(), {}), document.getElementById('root'))}''')
            expect(page.get_by_label('Skills model')).to_have_value('')
            # Custom bounds are restored as local input values, not rolling timestamps.
            page.get_by_role('group', name='Time window').get_by_role('button', name='Custom').click()
            page.get_by_label('Window start').fill('2026-09-01T09:30')
            page.get_by_label('Window end').fill('2026-09-05T10:30')
            page.evaluate('''() => {ReactDOM.render(null, document.getElementById('root'));
                ReactDOM.render(h(() => window.page(), {}), document.getElementById('root'))}''')
            expect(page.get_by_label('Window start')).to_have_value('2026-09-01T09:30')
            expect(page.get_by_label('Window end')).to_have_value('2026-09-05T10:30')
            # Old saved free-minute windows cannot bypass the new selectable
            # range invariant or trigger an unconstrained ledger read.
            page.evaluate('''() => {ReactDOM.render(null, document.getElementById('root'));
                const key='usage-view-v1:profile%3Ainfra';
                window.demoStored[key]={...window.demoStored[key],period:'custom',
                    customStart:'2026-09-01T09:31',customEnd:'2026-09-01T09:45'};
                ReactDOM.render(h(() => window.page(), {}), document.getElementById('root'))}''')
            expect(page.get_by_role('group',name='Time window').get_by_role('button',name='Past 24h')).to_have_attribute('aria-pressed','true')
            expect(page.get_by_label('Window start')).to_have_count(0)
            # The empty All providers ID and the aggregate scope have separate records.
            tabs.get_by_role('tab', name='All providers').click()
            page.get_by_role('navigation', name='Provider subpages').get_by_role('tab', name='Requests').click()
            page.evaluate('''() => {ReactDOM.render(null, document.getElementById('root'));
                ReactDOM.render(h(() => window.page(), {}), document.getElementById('root'))}''')
            expect(tabs.get_by_role('tab', name='All providers')).to_have_attribute('aria-selected', 'true')
            expect(subpages.get_by_role('tab', name='Requests')).to_have_attribute('aria-selected', 'true')
            page.get_by_role('combobox', name='Hermes profile').select_option('scope:all')
            expect(page.get_by_test_id('quota-home')).to_be_visible()
            tabs.get_by_role('tab', name='All providers').click()
            subpages.get_by_role('tab', name='Compressions').click()
            assert page.evaluate('window.demoStored["usage-view-v1:aggregate%3Aall"].profile') == 'aggregate:all'
            page.evaluate('''() => {ReactDOM.render(null, document.getElementById('root'));
                ReactDOM.render(h(() => window.page(), {}), document.getElementById('root'))}''')
            expect(tabs.get_by_role('tab', name='All providers')).to_have_attribute('aria-selected', 'true')
            expect(subpages.get_by_role('tab', name='Compressions')).to_have_attribute('aria-selected', 'true')
            page.get_by_role('combobox', name='Hermes profile').select_option('profile:infra')
            expect(tabs.get_by_role('tab', name='All providers')).to_have_attribute('aria-selected', 'true')
            # A malformed/old per-profile record cannot select a foreign profile or unsafe filter.
            page.evaluate('''() => {ReactDOM.render(null, document.getElementById('root'));
                window.demoStored['usage-view-v1:profile%3Ainfra']={version:0,profile:'default',provider:'nous',tab:'Requests',auto:true};
                ReactDOM.render(h(() => window.page(), {}), document.getElementById('root'))}''')
            expect(tabs.get_by_role('tab', name='Subscriptions')).to_have_attribute('aria-selected', 'true')
            assert page.get_by_test_id('quota-home').count() == 1
            page.evaluate('''() => {ReactDOM.render(null, document.getElementById('root'));
                window.demoStored['usage-view-v1:profile%3Ainfra']={version:1,profile:'profile:infra',
                    provider:{id:'nous'},tab:'not-a-tab',period:'last-century',filters:{session:42,agent:'root'},auto:true};
                ReactDOM.render(h(() => window.page(), {}), document.getElementById('root'))}''')
            expect(tabs.get_by_role('tab', name='Subscriptions')).to_have_attribute('aria-selected', 'true')
            assert page.evaluate('window.demoStored["usage-view-v1:profile%3Ainfra"].period') == '24h'
            # Intercept only the synthetic quota catalogue, before the mounted
            # component reads storage. Hold it to prove no premature ledger GET.
            page.evaluate('''() => {
                const original=rest;
                rest=(path,options) => {
                    const u=new URL(path,'https://offline.test');
                    if(u.pathname!=='/usage'||u.searchParams.get('profile_scope')==='all')
                        return original(path,options);
                    window.demoCalls.push(path);
                    const profile=u.searchParams.get('profile')||'infra';
                    const ids=profile==='default'?['nous']:['openai-codex'];
                    const response={profile,providers:demoProviders.filter(p=>ids.includes(p.id)),profiles:[],generated_at:new Date().toISOString(),probe_seconds:0};
                    if(window.badQuota==='providers')response.providers='not a catalogue';
                    if(window.badQuota==='profile')response.profile='other-profile';
                    if(window.badQuota==='error')return new Promise((_,reject)=>window.pendingQuota.push(()=>reject(new Error('Synthetic quota failure'))));
                    if(window.holdQuota)return new Promise(resolve=>window.pendingQuota.push(()=>resolve(response)));
                    return response;
                };
                window.pendingQuota=[];
            }''')

            def remount(profile, provider, hold=True):
                page.evaluate('''({profile,provider,hold}) => {
                    ReactDOM.render(null,document.getElementById('root'));
                    $profile.set(profile);
                    window.demoStored['usage-view-v1:profile%3A'+profile]={version:1,
                        profile:'profile:'+profile,provider,tab:'Requests',period:'7d'};
                    window.holdQuota=hold;
                    window.pendingQuota=[];
                    window.demoCalls=[];
                    ReactDOM.render(h(() => window.page(), {}),document.getElementById('root'));
                }''', {'profile': profile, 'provider': provider, 'hold': hold})

            def ledger_calls():
                return page.evaluate('window.demoCalls.filter(c=>c.startsWith("/ledger?"))')

            remount('infra', 'bad')
            page.wait_for_function('window.pendingQuota.length === 1')
            expect(tabs.get_by_role('tab', name='Subscriptions')).to_have_attribute('aria-selected', 'true')
            assert not ledger_calls(), ledger_calls()
            assert page.evaluate('window.demoStored["usage-view-v1:profile%3Ainfra"].provider') == 'bad'
            page.evaluate('window.pendingQuota.shift()()')
            expect(page.get_by_test_id('quota-home')).to_be_visible()
            page.wait_for_function('window.demoStored["usage-view-v1:profile%3Ainfra"].provider === "__quota_home__"')
            assert not ledger_calls(), ledger_calls()

            # A valid ID in one profile is stale in another, even with the same
            # storage schema and a previous profile's available catalogue.
            remount('default', 'openai-codex')
            page.wait_for_function('window.pendingQuota.length === 1')
            assert not ledger_calls(), ledger_calls()
            page.evaluate('window.pendingQuota.shift()()')
            expect(page.get_by_test_id('quota-home')).to_be_visible()
            assert not ledger_calls(), ledger_calls()

            remount('default', 'nous')
            page.wait_for_function('window.pendingQuota.length === 1')
            assert not ledger_calls(), ledger_calls()
            page.evaluate('window.pendingQuota.shift()()')
            expect(tabs.get_by_role('tab', name='Nous Portal')).to_have_attribute('aria-selected', 'true')
            assert any('provider=nous' in c and 'profile=default' in c for c in ledger_calls()), ledger_calls()

            remount('infra', 'openai-codex')
            page.wait_for_function('window.pendingQuota.length === 1')
            assert not ledger_calls(), ledger_calls()
            assert page.evaluate('window.demoStored["usage-view-v1:profile%3Ainfra"].provider') == 'openai-codex'
            page.evaluate('window.pendingQuota.shift()()')
            expect(tabs.get_by_role('tab', name='Codex')).to_have_attribute('aria-selected', 'true')
            assert any('provider=openai-codex' in c and 'profile=infra' in c for c in ledger_calls()), ledger_calls()

            for malformed in ('providers', 'profile'):
                page.evaluate('window.badQuota = %s' % repr(malformed))
                remount('infra', 'openai-codex')
                page.wait_for_function('window.pendingQuota.length === 1')
                assert not ledger_calls(), ledger_calls()
                page.evaluate('window.pendingQuota.shift()()')
                expect(tabs.get_by_role('tab', name='Subscriptions')).to_have_attribute('aria-selected', 'true')
                assert not ledger_calls(), ledger_calls()
            page.evaluate('window.badQuota = "error"')
            remount('infra', 'openai-codex')
            page.wait_for_function('window.pendingQuota.length === 1')
            page.evaluate('window.pendingQuota.shift()()')
            page.wait_for_function('window.pendingQuota.length === 1')
            assert page.evaluate('window.demoStored["usage-view-v1:profile%3Ainfra"].provider') == 'openai-codex'
            assert not ledger_calls(), ledger_calls()
            page.evaluate('window.pendingQuota.shift()()')
            expect(tabs.get_by_role('tab', name='Subscriptions')).to_have_attribute('aria-selected', 'true')
            page.wait_for_function('window.demoStored["usage-view-v1:profile%3Ainfra"].provider === "__quota_home__"')
            assert not ledger_calls(), ledger_calls()
            page.evaluate('window.badQuota = null')

            remount('infra', '')
            expect(tabs.get_by_role('tab', name='All providers')).to_have_attribute('aria-selected', 'true')
            assert any('provider=&' in c for c in ledger_calls()), ledger_calls()
            # An explicit All providers selection is valid without waiting for quota.
            page.evaluate('window.pendingQuota.shift()()')
            assert not network, network
            assert not errors, errors
            print('PASS remount preferences, delayed valid catalogue, bad/foreign/malformed/unavailable fallback, All providers and GET-only')
        finally:
            browser.close()


if __name__ == '__main__':
    run()
