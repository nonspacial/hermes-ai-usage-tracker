"""Offline Skills coverage and committed rolling-window reads."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def skills_calls(page):
    return page.evaluate("""() => demoCalls.filter(path => path.startsWith('/ledger/skills?')).map(path => {
        const q = new URL(path, 'https://offline.test').searchParams;
        return {start:Number(q.get('start')), end:q.get('end'), model:q.get('model'),
                test:q.get('test_id'), scope:q.get('profile_scope')};
    })""")


def await_report(page, previous):
    page.wait_for_function("n => demoCalls.filter(path => path.startsWith('/ledger/skills?')).length > n", arg=previous)
    expect(page.get_by_test_id('skills-usage').get_by_text('Loading skills report…')).to_have_count(0)
    return skills_calls(page)[-1]


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
                                    headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1500, 'height': 1000})
        page.clock.install()
        errors, network = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: network.append(r.url))
        page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
        page.get_by_role('tab', name='All providers', exact=True).click()
        page.get_by_role('tab', name='Skills usage', exact=True).click()
        surface = page.get_by_test_id('skills-usage')
        context = page.get_by_test_id('skill-context')
        views = page.get_by_role('group', name='Skills view', exact=True)
        views.get_by_role('button', name='Context footprint', exact=True).click()
        expect(context).to_contain_text('Estimated total')
        assert context.get_by_text('~0 tokens', exact=False).count() == 0
        picker = page.get_by_role('combobox', name='Hermes profile', exact=True)
        picker.select_option('scope:all')
        page.get_by_role('tab', name='All providers', exact=True).click()
        page.get_by_role('tab', name='Skills usage', exact=True).click()
        views.get_by_role('button', name='Context footprint', exact=True).click()
        expect(context).to_contain_text('Estimated total')
        expect(page.get_by_test_id('profile-coverage').last).to_contain_text('complete')
        assert '· partial' not in context.locator('.au-metrics').inner_text()

        page.evaluate("window.demoAggregateCoverage='unavailable'")
        before = len(skills_calls(page))
        page.get_by_role('button', name='Refresh', exact=True).first.click()
        await_report(page, before)
        expect(page.get_by_test_id('profile-coverage').last).to_contain_text('unavailable · 0 / 2 readable')
        assert 'Unavailable' in context.locator('.au-metrics').inner_text()
        assert '~0 tokens' not in context.locator('.au-metrics').inner_text()
        assert 'Recorded main-skill loads\n—' in surface.inner_text()

        page.evaluate("window.demoAggregateCoverage='partial'")
        before = len(skills_calls(page))
        page.get_by_role('button', name='Refresh', exact=True).first.click()
        await_report(page, before)
        expect(page.get_by_test_id('profile-coverage').last).to_contain_text('partial · 1 / 2 readable')
        expect(context.locator('.au-metrics')).to_contain_text('tokens · partial')
        expect(surface.locator('.au-metrics').first).to_contain_text('loads')
        assert '· partial' in surface.locator('.au-metrics').first.inner_text()

        # A readable profile with no selected loads is a known zero, but still partial across profiles.
        page.evaluate("window.demoSkillEventsBackup=demoSkillEvents.splice(0)")
        before = len(skills_calls(page))
        page.get_by_role('button', name='Refresh', exact=True).first.click()
        await_report(page, before)
        expect(context.locator('.au-metrics')).to_contain_text('~0 tokens · partial')
        page.evaluate("demoSkillEvents.push(...window.demoSkillEventsBackup);window.demoAggregateCoverage=undefined")
        before = len(skills_calls(page))
        page.get_by_role('button', name='Refresh', exact=True).first.click()
        await_report(page, before)
        expect(context.locator('.au-metrics')).to_contain_text('tokens')
        assert '· partial' not in context.locator('.au-metrics').inner_text()

        # A selected readable, empty observation window is an unqualified known zero.
        picker.select_option('profile:infra')
        page.get_by_role('tab', name='All providers', exact=True).click()
        page.get_by_role('tab', name='Skills usage', exact=True).click()
        views.get_by_role('button', name='Context footprint', exact=True).click()
        page.evaluate("window.demoSkillsMissing=true")
        before = len(skills_calls(page))
        page.get_by_role('button', name='Refresh', exact=True).first.click()
        await_report(page, before)
        expect(context.locator('.au-metrics')).to_contain_text('Not recorded')
        assert '~0 tokens' not in context.locator('.au-metrics').inner_text()
        page.evaluate("window.demoSkillsMissing=false;window.demoSkillEventsBackup=demoSkillEvents.splice(0)")
        before = len(skills_calls(page))
        page.get_by_role('button', name='Refresh', exact=True).first.click()
        await_report(page, before)
        expect(context.locator('.au-metrics')).to_contain_text('~0 tokens')
        assert '· partial' not in context.locator('.au-metrics').inner_text()
        page.evaluate("demoSkillEvents.push(...window.demoSkillEventsBackup)")

        # Rolling periods are sampled at each committed report read, not by event polling.
        before = len(skills_calls(page))
        page.get_by_role('button', name='Refresh', exact=True).first.click()
        first = await_report(page, before)
        page.clock.fast_forward(90000)
        page.evaluate('window.addDemoEvent()')
        page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')
        assert len(skills_calls(page)) == before + 1 and skills_calls(page)[-1] == first, skills_calls(page)
        page.get_by_role('tab', name='Requests', exact=True).click()
        before = len(skills_calls(page))
        page.get_by_role('tab', name='Skills usage', exact=True).click()
        reopened = await_report(page, before)
        assert reopened['start'] > first['start'] + 80 and reopened['end'] is None, (first, reopened)
        # The old mounted owner may still be in flight when the same scope is
        # reopened. Its completion must neither replace nor block the new read.
        views.get_by_role('button', name='Frequency', exact=True).click()
        page.get_by_role('tab', name='Requests', exact=True).click()
        page.evaluate('window.holdSkillsRequests=[]')
        before = len(skills_calls(page))
        page.get_by_role('tab', name='Skills usage', exact=True).click()
        page.wait_for_function('window.holdSkillsRequests.length===1')
        held = skills_calls(page)[-1]
        assert len(skills_calls(page)) == before + 1
        page.clock.fast_forward(90000)
        page.evaluate("""() => {
            const event={...demoSkillEvents.find(e=>e.kind==='skill_load'),
                id:'reopen-fresh-probe',skill:'reopen-fresh-probe',ts:Date.now()/1000-5};
            demoSkillEvents.push(event);
        }""")
        page.get_by_role('tab', name='Requests', exact=True).click()
        before = len(skills_calls(page))
        page.get_by_role('tab', name='Skills usage', exact=True).click()
        page.wait_for_function('window.holdSkillsRequests.length===2')
        newer = skills_calls(page)[-1]
        assert len(skills_calls(page)) == before + 1 and newer['start'] > held['start'] + 80, (held, newer)
        page.evaluate('window.holdSkillsRequests[1]()')
        legend = surface.get_by_role('button', name='reopen-fresh-probe · 1 loads · inspect', exact=True)
        expect(legend).to_be_visible()
        page.evaluate('window.holdSkillsRequests[0]();window.holdSkillsRequests=null')
        page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')
        expect(legend).to_be_visible()
        page.evaluate("demoSkillEvents.splice(demoSkillEvents.findIndex(e=>e.id==='reopen-fresh-probe'),1)")
        page.clock.fast_forward(7200000)
        before = len(skills_calls(page))
        page.get_by_role('button', name='Refresh', exact=True).first.click()
        refreshed = await_report(page, before)
        assert refreshed['start'] > reopened['start'] + 7100 and refreshed['end'] is None, (reopened, refreshed)
        page.clock.fast_forward(60000)
        before = len(skills_calls(page))
        surface.get_by_label('Skills model', exact=True).select_option('portal-demo')
        filtered = await_report(page, before)
        assert filtered['start'] > refreshed['start'] and filtered['model'] == 'portal-demo', (refreshed, filtered)

        # Custom inputs and saved marker facts remain fixed through reopen and Refresh.
        period = page.get_by_role('group', name='Time window', exact=True)
        period.get_by_role('button', name='Custom', exact=True).click()
        start, end = page.evaluate("[localHalfHour(now-7200),localHalfHour(now+7200)]")
        page.get_by_label('Window start').fill(start)
        before = len(skills_calls(page))
        page.get_by_label('Window end').fill(end)
        await_report(page, before)
        before = len(skills_calls(page))
        surface.get_by_label('Skills model', exact=True).select_option('portal-demo')
        custom = await_report(page, before)
        assert custom['end'] is not None and custom['test'] is None and custom['model'] == 'portal-demo', custom
        page.clock.fast_forward(7200000)
        page.get_by_role('tab', name='Requests', exact=True).click()
        before = len(skills_calls(page))
        page.get_by_role('tab', name='Skills usage', exact=True).click()
        custom_reopen = await_report(page, before)
        before = len(skills_calls(page))
        page.get_by_role('button', name='Refresh', exact=True).first.click()
        custom_refresh = await_report(page, before)
        assert all((v['start'], v['end'], v['model']) == (custom['start'], custom['end'], custom['model'])
                   for v in (custom_reopen, custom_refresh)), (custom, custom_reopen, custom_refresh)
        page.evaluate("savedTests.push({id:'synthetic-skills-marker',started:now-3600,ended:now+3600,label:'Synthetic marker'})")
        # Marker selection uses the same committed fixed Custom window but its factual bounds are server-side.
        page.get_by_role('tab', name='Requests', exact=True).click()
        page.get_by_role('tab', name='Skills usage', exact=True).click()
        before = len(skills_calls(page))
        page.get_by_label('Saved tests').select_option('synthetic-skills-marker')
        marked = await_report(page, before)
        assert marked['test'] == 'synthetic-skills-marker' and marked['end'] is not None, marked
        page.clock.fast_forward(7200000)
        page.get_by_role('tab', name='Requests', exact=True).click()
        before = len(skills_calls(page))
        page.get_by_role('tab', name='Skills usage', exact=True).click()
        fixed_reopen = await_report(page, before)
        before = len(skills_calls(page))
        page.get_by_role('button', name='Refresh', exact=True).first.click()
        fixed_refresh = await_report(page, before)
        assert all((v['start'], v['end'], v['test'], v['model']) ==
                   (marked['start'], marked['end'], marked['test'], marked['model'])
                   for v in (fixed_reopen, fixed_refresh)), (marked, fixed_reopen, fixed_refresh)
        assert not errors, errors
        assert not network, network
        browser.close()
    print('PASS Skills coverage unknown/partial/known-zero and committed rolling/fixed reads')


if __name__ == '__main__':
    run()
