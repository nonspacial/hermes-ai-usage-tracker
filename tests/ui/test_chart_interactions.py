"""Offline browser coverage of the packaged timeline, using synthetic data only."""
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def test_chart_interactions():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
            headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            page = browser.new_page(viewport={'width': 1480, 'height': 900}, locale='en-GB')
            errors, network = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: network.append(request.url))
            page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
            page.evaluate('''() => {const original=rest;rest=async (...args)=>{
                const result=await original(...args);
                if(args[0].startsWith('/ledger?'))window.chartData=result;
                return result;
            }}''')
            page.get_by_role('navigation', name='Providers', exact=True).get_by_role('tab', name='Codex', exact=True).click()
            page.wait_for_function('window.demoCalls.filter(p=>p.startsWith("/ledger?")).length >= 2')
            chart = page.get_by_test_id('usage-chart')
            plot = chart.locator('.au-plot-hit')
            expect(plot).to_be_visible()
            tooltip = plot.locator('title').text_content() or ''
            assert 'Left/Right arrow keys' in tooltip
            assert '15 minutes' in tooltip and 'half-hour boundaries' in tooltip

            def calls():
                return page.evaluate('window.demoCalls.filter(p=>p.startsWith("/ledger?"))')

            def position(fraction):
                # The short upper pane scrolls internally; expose the whole plot
                # instead of sending pointer events through the lower sticky tabs.
                page.locator('.au-upper').evaluate('e=>e.scrollTop=150')
                box = plot.bounding_box()
                assert box is not None
                return box['x'] + box['width'] * fraction, box['y'] + box['height'] / 2

            def stroke():
                return plot.evaluate('e=>getComputedStyle(e).stroke')

            selected_button = page.get_by_role('navigation', name='Providers', exact=True).get_by_role('tab', name='Codex', exact=True)

            def selected_background():
                return selected_button.evaluate('e=>getComputedStyle(e).backgroundColor')

            def line_stroke():
                return chart.locator('.au-line').first.evaluate('e=>getComputedStyle(e).stroke')

            def button_text():
                return selected_button.evaluate('e=>getComputedStyle(e).color')

            assert plot.evaluate('e=>getComputedStyle(e).strokeWidth') == '1px'
            assert plot.evaluate('e=>getComputedStyle(e).vectorEffect') == 'non-scaling-stroke'
            page.mouse.move(*position(.3))
            hover_stroke = stroke()
            assert hover_stroke == selected_background()
            assert hover_stroke != 'rgb(255, 255, 255)'
            before = len(calls())
            page.mouse.click(*position(.3))
            expect(chart).to_be_focused()
            footer = page.get_by_test_id('chart-point-summary')
            bubble = page.locator('.au-tooltip[role="tooltip"]:visible')
            expect(footer).to_be_visible()
            assert 'Click to interact with Left/Right arrow keys' in bubble.inner_text()
            assert 'Click and drag at least 15 minutes' in bubble.inner_text()
            assert footer.inner_text() == chart.get_attribute('data-au-chart-summary')
            assert chart.locator('.au-chart-tip').inner_text() == ''
            assert footer.inner_text() not in chart.inner_text(), 'Point details must not occupy the pane'
            assert bubble.evaluate('e=>e.lastElementChild.dataset.testid') == 'chart-point-summary'
            assert footer.evaluate('e=>getComputedStyle(e).color') == line_stroke()
            assert chart.evaluate('e=>e.getAttribute("aria-describedby")') == bubble.get_attribute('id')
            focus_stroke = stroke()
            assert focus_stroke == line_stroke() == button_text()
            assert focus_stroke != hover_stroke
            assert focus_stroke != 'rgb(255, 255, 255)'
            print(f'Computed plot strokes: hover={hover_stroke}, focus={focus_stroke}, width=1px, vector-effect=non-scaling-stroke')
            tip = footer.inner_text()
            chart.press('ArrowRight')
            expect(footer).not_to_have_text(tip)
            assert footer.inner_text() == chart.get_attribute('data-au-chart-summary')
            # Pointer inspection can change the selected point without hiding the instructions.
            page.mouse.move(*position(.8))
            expect(footer).not_to_have_text(tip)
            assert footer.inner_text() == plot.get_attribute('data-au-chart-summary')
            assert 'Click and drag at least 15 minutes' in bubble.inner_text()
            assert footer.evaluate('e=>getComputedStyle(e).color') == line_stroke()
            assert len(calls()) == before, 'Single click and keyboard inspection must not fetch'
            # Normal hand jitter remains a click, not a zoom.
            x, y = position(.3)
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x + 2, y)
            page.mouse.up()
            assert len(calls()) == before

            # Host palette changes update the stroke and its real control/line peers in place.
            page.evaluate("document.documentElement.style.setProperty('--ui-accent', '#6f549d')")
            assert stroke() == line_stroke() == button_text()
            assert stroke() != focus_stroke
            assert footer.evaluate('e=>getComputedStyle(e).color') == line_stroke()
            page.evaluate("document.documentElement.style.removeProperty('--ui-accent')")
            assert stroke() == focus_stroke
            chart.locator('.au-chart-title').click()
            expect(chart).not_to_be_focused()
            page.mouse.move(*position(.3))
            assert stroke() == selected_background() == hover_stroke
            page.evaluate("document.documentElement.style.setProperty('--ui-accent', '#6f549d')")
            assert stroke() == selected_background()
            assert stroke() != hover_stroke
            accent_hover_stroke = stroke()
            page.evaluate("document.documentElement.style.setProperty('--ui-bg-chrome', '#eeedf5')")
            assert stroke() == selected_background()
            assert stroke() != accent_hover_stroke
            page.evaluate("document.documentElement.style.removeProperty('--ui-accent'); document.documentElement.style.removeProperty('--ui-bg-chrome')")
            assert stroke() == selected_background() == hover_stroke
            page.mouse.click(*position(.3))
            expect(chart).to_be_focused()

            # Non-focusable content and the chart title must both dismiss focus.
            for outside in (page.locator('.au-hero-total'), chart.locator('.au-chart-title')):
                outside.click()
                expect(chart).not_to_be_focused()
                assert stroke() == 'rgba(0, 0, 0, 0)'
                page.mouse.click(*position(.3))
                expect(chart).to_be_focused()

            for reverse, height in ((False, 900), (True, 620)):
                page.set_viewport_size({'width': 1480, 'height': height})
                page.evaluate('window.chartData=null')
                page.get_by_role('group', name='Time window').get_by_role('button', name='Past hour', exact=True).click()
                page.wait_for_function('window.chartData?.trend?.seconds === 120')
                data = page.evaluate('window.chartData')
                assert 30 <= len(data['trend']['buckets']) <= 31
                assert ':' in (chart.locator('svg text').last.text_content() or ''), 'Minute-level axis labels'
                page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name='2 minutes', exact=True).click()
                labels = page.get_by_role('region', name='Usage breakdown').locator('tbody tr td:first-child').all_text_contents()
                assert len(labels) >= 30 and len(set(labels)) == len(labels)
                assert all(':' in label for label in labels)
                plot.scroll_into_view_if_needed()
                before = len(calls())
                a, b = (.8, .2) if reverse else (.2, .8)
                page.mouse.move(*position(a))
                page.mouse.down()
                page.mouse.move(*position(b), steps=6)
                selection = chart.locator('.au-plot-selection')
                expect(selection).to_be_visible()
                assert selection.evaluate('e=>getComputedStyle(e).fill') == line_stroke()
                assert selection.evaluate('e=>getComputedStyle(e).fillOpacity') == '0.3'
                page.evaluate("document.documentElement.style.setProperty('--ui-accent', '#3b9f84')")
                assert selection.evaluate('e=>getComputedStyle(e).fill') == line_stroke() == 'rgb(59, 159, 132)'
                page.evaluate("document.documentElement.style.removeProperty('--ui-accent')")
                assert len(calls()) == before, 'Do not fetch while dragging'
                boundaries = [data['trend']['buckets'][0]['start'],
                              *[bucket['end'] for bucket in data['trend']['buckets']]]
                boundaries = [t for t in boundaries if t % 1800 == 0]
                lo, hi = data['trend']['buckets'][0]['start'], data['trend']['buckets'][-1]['end']
                nearest = lambda fraction: min(boundaries, key=lambda t: abs(t - (lo + fraction * (hi - lo))))
                snapped = sorted((nearest(a), nearest(b)))
                box = plot.bounding_box()
                shade = selection.bounding_box()
                assert box and shade
                left = box['x'] + box['width'] * (snapped[0]-lo)/(hi-lo)
                right = box['x'] + box['width'] * (snapped[1]-lo)/(hi-lo)
                assert abs(shade['x']-left)<2 and abs(shade['x']+shade['width']-right)<2, (shade, left, right)
                page.mouse.up()
                expect(page.get_by_label('Window start', exact=True)).to_be_visible()
                page.wait_for_function('(n)=>window.demoCalls.filter(p=>p.startsWith("/ledger?")).length>n', arg=before)
                query = parse_qs(urlsplit(calls()[-1]).query)
                assert float(query['start'][0]) == snapped[0], (reverse, query, snapped)
                assert float(query['end'][0]) == snapped[1], (reverse, query, snapped)
                assert query['provider'] == ['openai-codex']
                assert 'test_id' not in query
                assert len(calls()) == before + 1, 'One completed drag, one report refresh'
                expect(selection).to_have_count(0)

            # Midpoint activation is measured in chart time, not arbitrary
            # pointer pixels. Retreat cancels a previously visible preview.
            page.get_by_role('group', name='Time window').get_by_role('button', name='Past hour', exact=True).click()
            page.wait_for_function('window.chartData?.trend?.seconds === 120')
            plot.scroll_into_view_if_needed()
            expect(plot.locator('title')).to_contain_text('15 minutes')
            data = page.evaluate('window.chartData')
            first, last = data['trend']['buckets'][0]['start'], data['trend']['buckets'][-1]['end']
            edges = [t for t in [first, *[b['end'] for b in data['trend']['buckets']]] if t % 1800 == 0]
            for reverse in (False, True):
                anchor = edges[-1] if reverse else edges[0]
                fraction = (anchor-first)/(last-first)
                direction = -1 if reverse else 1
                before = len(calls())
                page.mouse.move(*position(fraction));page.mouse.down()
                page.mouse.move(*position(fraction+direction*890/(last-first)),steps=3)
                expect(chart.locator('.au-plot-selection')).to_have_count(0)
                page.mouse.move(*position(fraction+direction*910/(last-first)),steps=3)
                expect(chart.locator('.au-plot-selection')).to_be_visible()
                page.mouse.move(*position(fraction+direction*600/(last-first)),steps=3)
                expect(chart.locator('.au-plot-selection')).to_have_count(0)
                page.mouse.up()
                assert len(calls()) == before, 'Backtracking before release must cancel'
                page.mouse.move(*position(fraction));page.mouse.down()
                page.mouse.move(*position(fraction+direction*910/(last-first)),steps=4)
                expect(chart.locator('.au-plot-selection')).to_be_visible()
                page.mouse.up()
                page.wait_for_function('(n)=>window.demoCalls.filter(p=>p.startsWith("/ledger?")).length>n',arg=before)
                query=parse_qs(urlsplit(calls()[-1]).query)
                assert float(query['end'][0])-float(query['start'][0])==1800
                assert float(query['start'][0])%1800==float(query['end'][0])%1800==0
                assert page.evaluate('window.chartData.trend.seconds')==60
                assert page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name='Minute', exact=True).count()==1
                assert 'drag zoom is unavailable' in (plot.locator('title').text_content() or '')
                before=len(calls())
                page.mouse.move(*position(.1));page.mouse.down();page.mouse.move(*position(.9),steps=3);page.mouse.up()
                assert len(calls())==before, 'At 30 minutes drag zoom is disabled'
                page.get_by_role('group', name='Time window').get_by_role('button', name='Past hour', exact=True).click()
                page.wait_for_function('window.chartData?.trend?.seconds === 120')
                expect(plot.locator('title')).to_contain_text('15 minutes')
                plot.scroll_into_view_if_needed()

            # Clipped first/last boundaries must also be selectable, and both
            # calendar resolutions use the returned bucket edges, not math on
            # a synthetic fixed hour/day grid.
            for label, seconds in (('Past 24h', 3600), ('7 days', 86400)):
                page.evaluate('window.chartData=null')
                page.get_by_role('group', name='Time window').get_by_role('button', name=label, exact=True).click()
                page.wait_for_function('(step)=>window.chartData?.trend?.seconds === step', arg=seconds)
                data = page.evaluate('window.chartData')
                plot.scroll_into_view_if_needed()
                before = len(calls())
                page.mouse.move(*position(.01));page.mouse.down()
                page.mouse.move(*position(.99), steps=6)
                expect(chart.locator('.au-plot-selection')).to_be_visible()
                page.mouse.up()
                page.wait_for_function('(n)=>window.demoCalls.filter(p=>p.startsWith("/ledger?")).length>n', arg=before)
                query = parse_qs(urlsplit(calls()[-1]).query)
                boundaries = [data['trend']['buckets'][0]['start'],
                              *[bucket['end'] for bucket in data['trend']['buckets']]]
                boundaries = [t for t in boundaries if t % 1800 == 0]
                lo, hi = data['trend']['buckets'][0]['start'], data['trend']['buckets'][-1]['end']
                assert float(query['start'][0]) == min(boundaries, key=lambda t: abs(t-(lo+.01*(hi-lo))))
                assert float(query['end'][0]) == min(boundaries, key=lambda t: abs(t-(lo+.99*(hi-lo))))

            page.evaluate('window.chartData=null')
            page.get_by_role('group', name='Time window').get_by_role('button', name='Past hour', exact=True).click()
            plot.scroll_into_view_if_needed()
            page.wait_for_function('window.chartData?.trend?.seconds === 120')
            expect(chart.locator('.au-chart-title')).to_contain_text('2 minutes bucket')
            before = len(calls())
            x, y = position(0)
            page.mouse.move(x+1,y);page.mouse.down();page.mouse.move(x+7,y);page.mouse.up()
            assert len(calls()) == before, ('Two gestures snapping to one boundary cannot zoom',before,calls()[-2:])
            assert chart.locator('.au-plot-selection').count() == 0

            # Manual controls share the half-hour grid and do not query while
            # a pasted/off-grid or shorter-than-half-hour interval is invalid.
            page.get_by_role('group', name='Time window').get_by_role('button', name='Custom', exact=True).click()
            beginning=page.get_by_label('Window start',exact=True)
            ending=page.get_by_label('Window end',exact=True)
            assert beginning.get_attribute('step')==ending.get_attribute('step')=='1800'
            before=len(calls())
            beginning.fill('2026-09-24T12:07')
            ending.fill('2026-09-24T12:30')
            expect(page.get_by_role('alert').filter(has_text='at least 30 minutes apart')).to_be_visible()
            assert len(calls())==before
            ending.fill('2026-09-24T12:00')
            beginning.fill('2026-09-24T12:00')
            assert len(calls())==before
            ending.fill('2026-09-24T12:30')
            page.wait_for_function('(n)=>window.demoCalls.filter(p=>p.startsWith("/ledger?")).length>n',arg=before)
            query=parse_qs(urlsplit(calls()[-1]).query)
            assert float(query['end'][0])-float(query['start'][0])==1800

            # Cancelled pointer gestures do not apply a time window.
            page.evaluate('window.chartData=null')
            page.get_by_role('group', name='Time window').get_by_role('button', name='Past hour', exact=True).click()
            page.wait_for_function('window.chartData?.trend?.seconds === 120')
            plot.scroll_into_view_if_needed()
            before = len(calls())
            page.mouse.move(*position(.2))
            page.mouse.down()
            page.mouse.move(*position(.6), steps=4)
            expect(chart.locator('.au-plot-selection')).to_be_visible()
            plot.dispatch_event('pointercancel', {'pointerId': 1})
            page.mouse.up()
            expect(chart.locator('.au-plot-selection')).to_have_count(0)
            assert len(calls()) == before
            # Fixture-only measurement markers retain their factual timestamps;
            # the viewing inputs align without rewriting the test record.
            page.get_by_role('button', name='Start test marker').click()
            page.wait_for_function('savedTests.length > 0')
            marker=page.evaluate('savedTests[0]')
            expect(page.get_by_label('Window start',exact=True)).to_have_value(datetime.fromtimestamp(
                (marker['started']//1800)*1800).strftime('%Y-%m-%dT%H:%M'))
            page.get_by_role('button', name='End test marker').click()
            page.wait_for_function('savedTests[0].ended != null')
            ended=page.evaluate('savedTests[0]')
            assert ended['started']==marker['started'] and ended['ended']-ended['started']<1800
            beginning=page.get_by_label('Window start',exact=True).input_value()
            ending=page.get_by_label('Window end',exact=True).input_value()
            assert (datetime.fromisoformat(ending)-datetime.fromisoformat(beginning)).total_seconds()>=1800
            assert parse_qs(urlsplit(calls()[-1]).query).get('test_id')==[marker['id']]
            # A saved marker whose real timestamps lie inside the aligned
            # viewing inputs must survive chart zoom and manual Custom edits.
            # An adjacent record in the aligned fringe is excluded until the
            # user explicitly removes the visible marker filter.
            page.evaluate('''() => {
              const end=Math.floor(Date.now()/1800000)*1800-600;
              const started=end-3*3600+73;
              savedTests.unshift({id:'boundary-fixture',label:'Boundary fixture',started,ended:end});
              const r={...events[0],id:'outside-marker-fixture',provider:'openai-codex',
                started:Math.floor(started/1800)*1800+1,ended:Math.floor(started/1800)*1800+2};
              events.push(r);window.demoChange++;for(const fn of window.demoSubscribers)fn({type:'changed',mode:'native-events'});
            }''')
            page.get_by_role('combobox', name='Saved tests').select_option('boundary-fixture')
            page.wait_for_function('window.chartData?.window?.start === savedTests[0].started')
            assert 'outside-marker-fixture' not in [r['id'] for r in page.evaluate('window.chartData.requests')]
            plot.scroll_into_view_if_needed()
            before=len(calls())
            page.mouse.move(*position(.15));page.mouse.down()
            page.mouse.move(*position(.75),steps=5)
            expect(chart.locator('.au-plot-selection')).to_be_visible()
            page.mouse.up()
            page.wait_for_function('(n)=>window.demoCalls.filter(p=>p.startsWith("/ledger?")).length>n',arg=before)
            query=parse_qs(urlsplit(calls()[-1]).query)
            assert query['test_id']==['boundary-fixture']
            assert page.get_by_role('button',name='Remove saved test marker filter').is_visible()
            page.get_by_label('Window start',exact=True).fill(datetime.fromtimestamp(
                (page.evaluate('savedTests[0].started')//1800)*1800).strftime('%Y-%m-%dT%H:%M'))
            page.get_by_label('Window end',exact=True).fill(datetime.fromtimestamp(
                ((page.evaluate('savedTests[0].ended')+1799)//1800)*1800).strftime('%Y-%m-%dT%H:%M'))
            assert parse_qs(urlsplit(calls()[-1]).query)['test_id']==['boundary-fixture']
            assert 'outside-marker-fixture' not in [r['id'] for r in page.evaluate('window.chartData.requests')]
            page.get_by_role('button',name='Remove saved test marker filter').click()
            page.wait_for_function('window.chartData?.requests?.some(r=>r.id==="outside-marker-fixture")')
            assert 'test_id' not in parse_qs(urlsplit(calls()[-1]).query)
            assert 'outside-marker-fixture' in [r['id'] for r in page.evaluate('window.chartData.requests')]
            assert not errors, errors
            assert not network, network
            print('PASS plot-only hover/focus, dual tooltip, click/jitter/keyboard, forward/reverse zoom, letterboxed SVG, cancellation and minute labels')
        finally:
            browser.close()


if __name__ == '__main__':
    test_chart_interactions()
