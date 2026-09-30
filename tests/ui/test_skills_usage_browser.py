"""Synthetic Skills report: stable aggregate reads and existing themed layout."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
                                    headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1500, 'height': 1100})
        page.clock.install()
        errors, network = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: network.append(r.url))
        page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
        page.get_by_role('tab', name='All providers', exact=True).click()
        page.get_by_role('tab', name='Skills usage', exact=True).click()
        surface = page.get_by_test_id('skills-usage')
        chart = page.get_by_test_id('skill-frequency')
        expect(chart.get_by_role('img', name='Skill load frequency')).to_be_visible()
        legend = chart.get_by_role('button', name='frontend-ui-iteration · 2 loads · inspect', exact=True)
        expect(legend).to_be_visible()
        assert not page.get_by_test_id('skill-timeline').count()
        assert page.evaluate("demoCalls.filter(x=>x.includes('/ledger/skills')).every(x=>x.includes('aggregate_only=true'))")
        # Pie slices are focusable named buttons (desktop/AGENTS.md); select one by
        # its accessible name rather than a raw SVG path, which the pie and heading overlap.
        slice_ = chart.get_by_role('button', name='frontend-ui-iteration: 2 (50.0%)', exact=True)
        expect(slice_).to_have_attribute('aria-pressed', 'false')
        # The pie starts partly below the nested reader's fold; Playwright's
        # click auto-scroll does not reveal a partly visible SVG path, so scroll
        # the named pie into view as a user would, then click the slice itself.
        chart.get_by_role('img', name='Skill load frequency').scroll_into_view_if_needed()
        slice_.click()
        expect(slice_).to_have_attribute('aria-pressed', 'true')
        expect(legend).to_have_attribute('aria-pressed', 'true')
        expect(surface).to_contain_text('Inspecting frontend-ui-iteration')
        # Singular units: this skill has one reference read and one failed load.
        expect(surface).to_contain_text('2 loads · 0 repeat · 1 reference · 1 failure')
        expect(chart.get_by_role('button', name='hermes-agent · 2 loads · inspect', exact=True)).to_be_visible()
        legend.focus()
        legend.press('Enter')
        expect(surface.get_by_role('button', name='Clear skill selection')).to_have_count(0)
        expect(slice_).to_have_attribute('aria-pressed', 'false')
        legend.press('Enter')
        expect(surface.get_by_role('button', name='Clear skill selection')).to_be_visible()
        # Singular legend and selected-skill wording (one load each under this model);
        # a model change clears the selection, then keyboard Space selects a slice.
        surface.get_by_label('Skills model', exact=True).select_option('portal-demo')
        single = chart.get_by_role('button', name='frontend-ui-iteration · 1 load · inspect', exact=True)
        expect(single).to_be_visible()
        expect(single).to_contain_text('1 load · 50.0%')
        expect(chart.get_by_role('button', name='hermes-agent · 1 load · inspect', exact=True)).to_be_visible()
        expect(surface.get_by_role('button', name='Clear skill selection')).to_have_count(0)
        single_slice = chart.get_by_role('button', name='frontend-ui-iteration: 1 (50.0%)', exact=True)
        assert single_slice.locator('title').text_content().endswith('1 load · 50.0%'), single_slice.locator('title').text_content()
        single_slice.focus()
        single_slice.press(' ')
        expect(single_slice).to_have_attribute('aria-pressed', 'true')
        expect(surface).to_contain_text('1 load · 0 repeat · 0 references · 1 failure')
        single_slice.press('Enter')
        expect(surface.get_by_role('button', name='Clear skill selection')).to_have_count(0)
        surface.get_by_label('Skills model', exact=True).select_option('')
        expect(legend).to_be_visible()
        legend.click()
        expect(surface.get_by_role('button', name='Clear skill selection')).to_be_visible()
        expect(surface).to_contain_text('2 loads · 0 repeat · 1 reference · 1 failure')
        views = page.get_by_role('group', name='Skills view', exact=True)
        views.get_by_role('button', name='Context footprint', exact=True).click()
        context = page.get_by_test_id('skill-context')
        expect(context).to_contain_text('Estimated total')
        expect(context).to_contain_text('Average per load')
        expect(context).to_contain_text('estimated tokens')
        expect(context.get_by_role('img', name='Estimated returned skill content by skill')).to_be_visible()
        assert not page.get_by_label('Context snapshot').count()
        page.evaluate("demoSkillEvents.find(e=>e.id==='skill-demo-1').estimated_tokens=null")
        page.get_by_role('button', name='Refresh', exact=True).first.click()
        expect(context).to_contain_text('complete total and share are unavailable')
        expect(context).to_contain_text('Not recorded')
        expect(context.get_by_role('img', name='Estimated returned skill content by skill')).to_have_count(0)
        page.evaluate("demoSkillEvents.find(e=>e.id==='skill-demo-1').estimated_tokens=1900")
        page.get_by_role('button', name='Refresh', exact=True).first.click()
        expect(context.get_by_role('img', name='Estimated returned skill content by skill')).to_be_visible()
        views.get_by_role('button', name='Catalogue overhead', exact=True).click()
        catalogue = page.get_by_test_id('skill-catalogue')
        expect(surface.get_by_role('button', name='Clear skill selection')).to_have_count(0)
        assert 'Inspecting frontend-ui-iteration' not in surface.inner_text()
        expect(catalogue).to_contain_text('Description inclusions')
        expect(catalogue).to_contain_text('Observed main loads')
        expect(catalogue).to_contain_text('not model attention')
        assert catalogue.evaluate("e=>e.classList.contains('au-box')&&getComputedStyle(e).backgroundColor!=='rgba(0, 0, 0, 0)'")
        page.screenshot(path=str(Path(os.environ['TMPDIR']) / 'skills-catalogue-wide.png'))
        views.get_by_role('button', name='Context footprint', exact=True).click()
        expect(surface.get_by_role('button', name='Clear skill selection')).to_be_visible()
        views.get_by_role('button', name='Catalogue overhead', exact=True).click()
        surface.get_by_label('Skills model', exact=True).select_option('portal-demo')
        expect(catalogue).to_contain_text('Description exposure unavailable')
        surface.get_by_label('Skills model', exact=True).select_option('')
        expect(catalogue).to_contain_text('Description inclusions')
        period = page.get_by_role('group', name='Time window', exact=True)
        period.get_by_role('button', name='7 days', exact=True).click()
        views.get_by_role('button', name='Frequency', exact=True).click()
        expect(chart.get_by_role('button', name='frontend-ui-iteration · 6 loads · inspect', exact=True)).to_be_visible()
        before = page.evaluate("demoCalls.filter(x=>x.includes('/ledger/skills')).length")
        page.clock.fast_forward(61000)
        page.evaluate('window.addDemoEvent()')
        page.evaluate('() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
        assert page.evaluate("demoCalls.filter(x=>x.includes('/ledger/skills')).length") == before
        page.evaluate('window.holdSkillsRequests=[]')
        page.get_by_role('button', name='Refresh', exact=True).first.click()
        page.wait_for_function("n=>demoCalls.filter(x=>x.includes('/ledger/skills')).length>n", arg=before)
        expect(surface).to_contain_text('Updating…')
        expect(chart.get_by_role('button', name='frontend-ui-iteration · 6 loads · inspect', exact=True)).to_be_visible()
        surface.get_by_label('Skills model', exact=True).select_option('portal-demo')
        expect(surface).to_contain_text('Loading skills report')
        page.wait_for_function('window.holdSkillsRequests.length>=2')
        page.evaluate('window.holdSkillsRequests.shift()()')
        expect(surface).to_contain_text('Loading skills report')
        page.evaluate('window.holdSkillsRequests.forEach(resolve=>resolve());window.holdSkillsRequests=null')
        expect(chart.get_by_role('button', name='frontend-ui-iteration · 3 loads · inspect', exact=True)).to_be_visible()
        surface.get_by_label('Skills model', exact=True).select_option('')
        expect(chart.get_by_role('button', name='frontend-ui-iteration · 6 loads · inspect', exact=True)).to_be_visible()
        for width, columns in ((1900, 3), (1200, 3), (850, 2), (390, 1), (320, 1)):
            page.set_viewport_size({'width': width, 'height': 1000})
            page.evaluate('() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
            sizes = chart.locator('.au-skill-chart').evaluate('''e => {
                const pie=e.querySelector('svg').getBoundingClientRect();
                const legend=e.querySelector('.au-skill-legend');
                return {pie:pie.width,height:pie.height,chart:e.clientWidth,
                    columns:getComputedStyle(legend).gridTemplateColumns.split(' ').length,
                    below:legend.getBoundingClientRect().top>=pie.bottom};
            }''')
            assert sizes['columns'] == columns and sizes['below'], sizes
            assert abs(sizes['pie']-min(320,sizes['chart'])) < 2, sizes
            assert abs(sizes['pie']-sizes['height']) < 2, sizes
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            if width == 390:
                page.screenshot(path=str(Path(os.environ['TMPDIR']) / 'skills-frequency-narrow.png'))
        assert not errors, errors
        assert not network, network
        browser.close()
    print('PASS three aggregate views, stable reads and manual Refresh, model/period filters, unchanged theme chart geometry')


if __name__ == '__main__':
    run()
