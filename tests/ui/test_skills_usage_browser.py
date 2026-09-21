"""Skills tab against synthetic REST data: real controls, never live accounts."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
ART = Path(__file__).parent / 'artifacts'


def top(locator):
    box = locator.bounding_box()
    assert box is not None
    return box['y']


def run():
    ART.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'), headless=True,
                                   args=['--no-sandbox', '--disable-dev-shm-usage'])
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
        expect(chart.get_by_role('button', name='frontend-ui-iteration · 2 loads · inspect', exact=True)).to_be_visible()
        assert chart.locator('.bar').count() == 0
        chart.locator('svg path').first.click()
        detail = page.get_by_test_id('skill-drilldown')
        expect(detail).to_be_visible()
        legend = chart.get_by_role('button', name='frontend-ui-iteration · 2 loads · inspect', exact=True)
        legend.focus()
        legend.press('Enter')
        expect(detail).to_have_count(0)
        legend.press('Enter')
        expect(detail).to_be_visible()
        expect(detail).to_contain_text('references/table-and-viewport-patterns.md')
        expect(detail).to_contain_text('Failed load')
        expect(detail).to_contain_text('Reference read')
        # Selecting a skill never changes the pie denominator.
        expect(chart.get_by_role('button', name='hermes-agent · 2 loads · inspect', exact=True)).to_be_visible()
        expect(detail.locator('tbody tr').first).to_contain_text('Before compression')
        surface.get_by_role('button', name='Clear skill selection').click()
        period = page.get_by_role('group', name='Time window', exact=True)
        period.get_by_role('button', name='7 days', exact=True).click()
        expect(chart.get_by_role('button', name='frontend-ui-iteration · 6 loads · inspect', exact=True)).to_be_visible()
        surface.get_by_label('Skills model', exact=True).select_option('portal-demo')
        expect(chart.get_by_role('button', name='frontend-ui-iteration · 3 loads · inspect', exact=True)).to_be_visible()
        surface.get_by_label('Skills model', exact=True).select_option('')
        page.get_by_label('Project', exact=True).select_option('repo-marine')
        expect(chart.get_by_role('button', name='frontend-ui-iteration · 1 loads · inspect', exact=True)).to_be_visible()
        page.get_by_role('button', name='Clear filters', exact=True).click()
        period.get_by_role('button', name='Past 24h', exact=True).click()
        views = page.get_by_role('group', name='Skills view', exact=True)
        views.get_by_role('button', name='Context footprint', exact=True).click()
        context = page.get_by_test_id('skill-context')
        expect(context).to_contain_text('7,000')
        snapshot = page.get_by_label('Context snapshot', exact=True)
        snapshot.select_option('snapshot-demo-1')
        expect(context).to_contain_text('18,000')
        expect(context).to_contain_text('Before compression')
        snapshot.select_option('snapshot-demo-2')
        expect(context).to_contain_text('7,000')
        assert surface.locator('.au-snapshot-picker').evaluate('(e)=>e.getBoundingClientRect().bottom<=e.nextElementSibling.getBoundingClientRect().top')
        assert surface.locator('.au-snapshot-picker').evaluate('(e)=>getComputedStyle(e).justifyContent')=='flex-end'
        context.get_by_role('button', name='Before compression · 18,000 tokens', exact=True).click()
        expect(context).to_contain_text('Compression demo-comp-0')
        expect(snapshot).to_have_value('snapshot-demo-1')
        views.get_by_role('button', name='Session timeline', exact=True).click()
        timeline = page.get_by_test_id('skill-timeline')
        expect(timeline).to_contain_text('Before compression')
        expect(timeline).to_contain_text('After compression')
        expect(timeline.locator('details[open]')).to_have_count(0)
        header=timeline.locator('details').first.locator('summary')
        expect(header).to_contain_text('demo-session-0')
        expect(header).to_contain_text('Context footprint')
        expect(header).to_contain_text('Not recorded')
        expect(header).to_contain_text('Recorded')
        header.focus()
        header.press('Enter')
        timeline.get_by_role('button', name='Inspect snapshot', exact=True).first.click()
        expect(context).to_be_visible()
        expect(context).to_contain_text('Turn completed')
        expect(context).to_contain_text('Context: Not recorded')
        views.get_by_role('button', name='Session timeline', exact=True).click()
        timeline.locator('details').first.locator('summary').click()
        timeline.get_by_role('button', name='demo-session-0', exact=True).first.click()
        expect(page.get_by_label('Session ID', exact=True)).to_have_value('demo-session-0')
        expect(timeline).to_be_visible()
        assert all('demo-session-0' in x for x in timeline.locator('details>summary').all_text_contents())
        page.get_by_role('button', name='Clear filters', exact=True).click()
        views.get_by_role('button', name='Frequency', exact=True).click()
        chart.get_by_role('button', name='frontend-ui-iteration · 2 loads · inspect', exact=True).click()
        # Populate more than a detail page; frequency must cover all events.
        page.evaluate('''() => {const source=demoSkillEvents.find(e=>e.id==='skill-demo-1');
            for(let i=0;i<63;i++)demoSkillEvents.push({...source,id:'extra-'+i,ts:now-10-i});
            queryClient.invalidateQueries()}''')
        expect(chart.get_by_role('button', name='frontend-ui-iteration · 65 loads · inspect', exact=True)).to_be_visible()
        expect(detail.locator('tbody tr')).to_have_count(50)
        detail.get_by_role('button', name='Next events', exact=True).click()
        expect(detail.locator('tbody tr')).to_have_count(63 + 4 + 2 - 50)
        expect(chart.get_by_role('button', name='frontend-ui-iteration · 65 loads · inspect', exact=True)).to_be_visible()
        for width in (1900, 1200, 850, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            expect(detail).to_be_visible()
            page.evaluate('() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert detail.locator('.au-table').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
            sizes = chart.locator('.au-skill-chart').evaluate('''e => {
                const pie=e.querySelector('svg').getBoundingClientRect();
                const legend=e.querySelector('.au-skill-legend');
                const columns=getComputedStyle(legend).gridTemplateColumns.split(' ').length;
                return {pie:pie.width,height:pie.height,chart:e.clientWidth,columns,below:legend.getBoundingClientRect().top>=pie.bottom};
            }''')
            expected = min(320, sizes['chart'])
            assert sizes['columns'] == (3 if width >= 1200 else 2 if width == 850 else 1), sizes
            assert sizes['below'], sizes
            assert abs(sizes['pie'] - expected) < 2, sizes
            assert abs(sizes['pie'] - sizes['height']) < 2, sizes
            assert sizes['pie'] > 180, sizes
        page.set_viewport_size({'width': 850, 'height': 1000})
        chart.screenshot(path=str(ART / 'skills-frequency-850.png'))
        # Missing history is not a zero-use assertion.
        page.evaluate('demoSkillsMissing=true;queryClient.invalidateQueries()')
        expect(surface.locator('.au-metrics .au-number').first).to_have_text('—')
        expect(page.get_by_test_id('skills-coverage')).to_have_count(0)
        expect(chart).to_contain_text('No recorded values')
        page.evaluate('failSkills=true;queryClient.invalidateQueries()')
        expect(surface.get_by_role('alert')).to_contain_text('Skills history unavailable')
        page.evaluate('failSkills=false;demoSkillsMissing=false')
        surface.get_by_role('button', name='Retry skills history').click()
        expect(surface.get_by_role('alert')).to_have_count(0)
        page.set_viewport_size({'width': 1500, 'height': 1100})
        page.evaluate('window.holdSkillsRequests=[]')
        period.get_by_role('button', name='7 days', exact=True).click()
        page.wait_for_function('window.holdSkillsRequests.length>=1')
        page.evaluate('() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
        old_requests = page.evaluate('window.holdSkillsRequests.length')
        period.get_by_role('button', name='Past hour', exact=True).click()
        page.wait_for_function('(n)=>window.holdSkillsRequests.length>n', arg=old_requests)
        page.evaluate('(n)=>window.holdSkillsRequests.slice(0,n).forEach(resolve=>resolve())', old_requests)
        expect(surface).to_contain_text('Loading skills history')
        page.evaluate('window.holdSkillsRequests.forEach(resolve=>resolve());window.holdSkillsRequests=null')
        expect(surface).not_to_contain_text('Loading skills history')
        expect(chart.get_by_role('button', name='frontend-ui-iteration · 65 loads · inspect', exact=True)).to_be_visible()
        views.get_by_role('button', name='Session timeline', exact=True).click()
        for width in (1500,390):
            page.set_viewport_size({'width':width,'height':1000})
            entry=timeline.locator('[data-event-id="extra-20"]')
            entry.evaluate('e=>{e.open=true;e.scrollIntoView({block:"center"});window.retainedEntry=e}')
            page.evaluate('() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
            scroller=page.locator('.au-reader')
            assert scroller.evaluate('(e)=>e.scrollTop>0&&e.scrollHeight>e.clientHeight&&e.getBoundingClientRect().bottom<=document.querySelector(".au-pane").getBoundingClientRect().bottom+1')
            document_scroll=page.evaluate('scrollY')
            before=top(entry)
            page.evaluate('''w=>{window.holdSkillsRequests=[];demoSkillEvents.push({...demoSkillEvents[0],id:'incoming-'+w,ts:now-1})}''',width)
            page.clock.fast_forward(61000)
            page.wait_for_function('window.holdSkillsRequests.length>=1')
            expect(surface).not_to_contain_text('Loading skills history')
            assert entry.evaluate('e=>e===window.retainedEntry&&e.open')
            assert abs(top(entry)-before)<=3
            page.evaluate('window.holdSkillsRequests.forEach(resolve=>resolve());window.holdSkillsRequests=null')
            expect(timeline.locator(f'[data-event-id="incoming-{width}"]')).to_have_count(1)
            page.evaluate('() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
            assert entry.evaluate('e=>e===window.retainedEntry&&e.open')
            assert abs(top(entry)-before)<=3,(width,before,entry.bounding_box())
            assert abs(page.evaluate('scrollY')-document_scroll)<=3
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(ART/f'timeline-accordion-{width}.png'))
        assert not errors, errors
        assert not network, network
        browser.close()
    print('PASS frequency pie and drill-down; time/model/project/session filters; separate references/failures; paginated details with full aggregates')
    print('PASS context snapshots and compression timeline; missing/error/retry states; responsive record fallback; no external requests or script errors')


if __name__ == '__main__':
    run()
