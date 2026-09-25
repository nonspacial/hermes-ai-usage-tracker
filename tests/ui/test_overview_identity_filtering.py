"""Offline browser contract for five Overview identity filters and scoped peers."""
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1800, 'height': 1100})
        errors, outbound = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda req: outbound.append(req.url))
        page.route('**/*', lambda route: route.abort())
        page.set_content((ROOT / 'preview.html').read_text())
        page.get_by_role('tab', name='All providers', exact=True).click()
        sub = page.get_by_role('navigation', name='Provider subpages')
        groups = page.get_by_role('group', name='Breakdown grouping')
        rows = page.locator('.au-breakdown tbody tr')
        nav = page.get_by_test_id('request-navigation')

        cell = rows.first.locator('td').nth(1)
        baseline = cell.evaluate('(el) => getComputedStyle(el).backgroundColor')
        cell.hover()
        assert cell.evaluate('(el) => getComputedStyle(el).backgroundColor') == baseline
        label = rows.first.locator('button.au-drill')
        before = label.evaluate('(el) => getComputedStyle(el).backgroundColor')
        label.hover()
        assert label.evaluate('(el) => getComputedStyle(el).backgroundColor') == before
        assert label.evaluate('(el) => getComputedStyle(el).textDecorationLine') == 'underline'

        def query(path='/ledger?'):
            return page.evaluate('''prefix => Object.fromEntries(
                new URL(demoCalls.filter(value => value.startsWith(prefix)).at(-1),
                        'https://offline.test').searchParams)''', path)

        def clear():
            nav.get_by_test_id('request-show-all').click()
            expect(nav).to_have_count(0)

        for grouping in ('Model', 'Hour', 'Project', 'Session', 'Subagents'):
            groups.get_by_role('button', name=grouping, exact=True).click()
            target = rows.filter(has=page.locator('button.au-drill')).first
            assert target.count(), grouping
            target.locator('button.au-drill').click()
            expect(sub.get_by_role('tab', name='Overview')).to_have_attribute('aria-selected', 'true')
            expect(nav).to_be_visible()
            args = query()
            assert args['provider'] == '' and args['start'] and args['limit'] == '200'
            assert page.get_by_test_id('recorded-summary').is_visible()
            assert page.get_by_test_id('usage-chart').is_visible()
            if grouping == 'Model':
                assert args['model'] and args['model_provider']
                assert not args['project'] and not args['session']
                assert nav.get_by_role('button', name='Remove model filter').is_visible()
                page.wait_for_function('() => !!window.demoStored["usage-view-v1:profile%3Ainfra"]?.filters?.model')
                page.evaluate('''() => {ReactDOM.render(null, document.getElementById('root'));
                  ReactDOM.render(h(() => window.page(), {}), document.getElementById('root'))}''')
                expect(nav.get_by_role('button', name='Remove model filter')).to_be_visible()
                assert query()['model'] == args['model']
            elif grouping == 'Hour':
                assert float(args['bucket_end']) > float(args['bucket_start'])
                assert nav.get_by_role('button', name='Remove bucket filter').is_visible()
            elif grouping == 'Project':
                assert args['project']
            elif grouping == 'Session':
                assert args['session'] and args['session_scope'] == 'family'
            else:
                assert args['subagent'] and args['agent'] == 'subagent'
            # The same semantic query drives the other pages, not just the chart.
            sub.get_by_role('tab', name='Requests').click()
            expect(page.get_by_test_id('request-list')).to_be_visible()
            assert query()['model'] == args['model']
            if grouping == 'Hour':
                assert query()['bucket_start'] == args['bucket_start']
            sub.get_by_role('tab', name='Compressions').click()
            expect(page.get_by_test_id('compression-events')).to_be_visible()
            sub.get_by_role('tab', name='Skills usage').click()
            expect(page.get_by_test_id('skills-usage')).to_be_visible()
            assert query('/ledger/skills?').get('model') == args['model']
            assert query('/ledger/skills?').get('model_provider') == args['model_provider']
            sub.get_by_role('tab', name='Overview').click()
            if grouping == 'Hour':
                page.get_by_role('group', name='Time window').get_by_role('button', name='7 days').click()
                expect(nav).to_have_count(0)
                assert 'bucket_start' not in query()
                page.get_by_role('group', name='Time window').get_by_role('button', name='Past 24h').click()
            else:
                if grouping == 'Project':
                    nav.get_by_test_id('request-back').click()
                    expect(nav).to_have_count(0)
                else:
                    clear()
            expect(page.get_by_test_id('usage-totals')).to_be_visible()

        # A selected model means effective response model AND its provider.
        page.evaluate('''() => {
          const clone = JSON.parse(JSON.stringify(events[0]));
          clone.id='returned-collision'; clone.provider='nous';
          clone.model='some-request'; clone.response_model=demoModels['openai-codex'];
          clone.started=Date.now()/1000-70;clone.ended=clone.started+.1;
          events.push(clone);
          const unknown={...demoSkillEvents[0],id:'unattributed-model',model:'unknown'};
          demoSkillEvents.push(unknown);
          compressions.push({...compressions[0],id:'unattributed-comp',model:'unknown'});
          window.demoChange++; for(const fn of window.demoSubscribers)fn({type:'changed',mode:'native-events'});
        }''')
        groups.get_by_role('button', name='Model').click()
        codex = rows.filter(has=page.locator('button.au-drill')).filter(has_text='Codex').first
        codex.locator('button.au-drill').click()
        args = query()
        assert args['model_provider'] == 'openai-codex'
        sub.get_by_role('tab', name='Requests').click()
        assert page.get_by_test_id('request-list').locator('tbody tr').evaluate_all(
            ' (rows) => rows.every(r => r.children[1].querySelector(".au-field-value").textContent === "openai-codex")')
        sub.get_by_role('tab', name='Compressions').click()
        assert not page.get_by_test_id('compression-events').get_by_text('unattributed-comp').count()
        sub.get_by_role('tab', name='Skills usage').click()
        expect(page.get_by_role('combobox', name='Skills model')).to_be_disabled()
        assert query('/ledger/skills?')['model_provider'] == 'openai-codex'
        assert query('/ledger/skills?')['aggregate_only'] == 'true'
        assert not page.get_by_test_id('skills-usage').get_by_text('unattributed-model').count()
        sub.get_by_role('tab', name='Overview').click()
        nav.get_by_role('button', name='Remove model filter').click()
        expect(nav).to_have_count(0)

        # A narrow row offers separate identity and expansion controls.
        page.locator('.au-ledger').evaluate('(e) => e.style.width="390px"')
        expect(page.locator('.au-breakdown .au-table')).to_have_attribute('data-layout', 'records')
        first = rows.first
        identity = first.locator('.au-record-filter')
        disclosure = first.locator('.au-record-disclosure')
        assert identity.is_visible() and disclosure.is_visible()
        assert disclosure.locator('button').count() == 0
        assert 'Model' in disclosure.get_attribute('aria-label')
        assert first.locator('.au-record-filter').inner_text().splitlines()[0] in disclosure.get_attribute('aria-label')
        disclosure.click()
        expect(first).to_have_attribute('data-expanded', 'true')
        expect(nav).to_have_count(0)
        identity.focus()
        identity.press('Enter')
        expect(nav).to_be_visible()
        expect(nav.get_by_role('button', name='Remove model filter')).to_be_focused()
        expect(sub.get_by_role('tab', name='Overview')).to_have_attribute('aria-selected', 'true')
        # A provider switch must not send an old provider's effective-model
        # predicate (or its project/session/bucket identity) to the new page.
        page.get_by_role('combobox', name='Agent scope').select_option('subagent')
        switch_start = len(page.evaluate('demoCalls'))
        page.get_by_role('combobox', name='Main page').select_option('nous')
        assert page.evaluate('''n=>demoCalls.slice(n).filter(p=>p.startsWith('/ledger?'))
          .every(p=>{const q=new URL(p,'https://offline.test').searchParams;
            return q.get('provider')==='nous'&&!q.get('model')&&!q.get('model_provider')})''', switch_start)
        assert query()['provider'] == 'nous' and query()['model'] == '' and query()['model_provider'] == ''
        assert query()['agent'] == 'subagent' and 'bucket_start' not in query()
        assert not nav.get_by_role('button', name='Remove model filter').count()
        page.get_by_role('combobox', name='Main page').select_option('')
        assert query()['provider'] == '' and query()['model_provider'] == ''
        page.get_by_role('combobox', name='Main page').select_option('openai-codex')
        assert query()['provider'] == 'openai-codex' and query()['model_provider'] == ''
        page.evaluate('''() => {ReactDOM.render(null, document.getElementById('root'));
          ReactDOM.render(h(() => window.page(), {}), document.getElementById('root'))}''')
        assert query()['provider'] == 'openai-codex' and query()['model_provider'] == ''
        page.evaluate('''() => {ReactDOM.render(null, document.getElementById('root'));
          const key='usage-view-v1:profile%3Ainfra';
          window.demoStored[key]={...window.demoStored[key],provider:'nous',
            filters:{...window.demoStored[key].filters,model:demoModels['openai-codex'],modelProvider:'openai-codex'}};
          ReactDOM.render(h(() => window.page(), {}), document.getElementById('root'))}''')
        expect(page.get_by_role('navigation', name='Providers', exact=True).get_by_role('tab', name='Nous Portal')).to_have_attribute('aria-selected', 'true')
        assert query()['provider'] == 'nous' and query()['model_provider'] == ''
        assert not nav.get_by_role('button', name='Remove model filter').count()
        # Other row-bound filters and the selected time bucket are also scoped
        # to their source provider, while the outer 24h window and agent remain.
        page.get_by_role('combobox', name='Project').select_option('repo-hermes')
        page.get_by_role('textbox', name='Session ID').fill('demo-session-1')
        page.get_by_role('navigation', name='Providers', exact=True).get_by_role('tab', name='Codex').click()
        assert query()['project'] == query()['session'] == ''
        assert query()['agent'] == 'subagent'
        groups.get_by_role('button', name='Hour').click()
        target = rows.filter(has=page.locator('button.au-drill')).first
        target.locator('button.au-drill').click()
        assert 'bucket_start' in query()
        page.get_by_role('navigation', name='Providers', exact=True).get_by_role('tab', name='Nous Portal').click()
        assert 'bucket_start' not in query()
        expect(page.get_by_role('group', name='Time window').get_by_role('button', name='Past 24h')).to_have_attribute('aria-pressed', 'true')
        assert not errors, errors
        assert not outbound, outbound
        browser.close()
    print('PASS five Overview filters, peer pages, provider/model collision, separate narrow controls; offline only')


if __name__ == '__main__':
    run()
