"""Offline browser coverage of the packaged timeline, using synthetic data only."""
import os
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
            chart = page.get_by_test_id('usage-chart')
            plot = chart.locator('.au-plot-hit')
            expect(plot).to_be_visible()
            tooltip = plot.locator('title').text_content() or ''
            assert 'Left/Right arrow keys' in tooltip
            assert 'Click and drag to zoom' in tooltip

            def calls():
                return page.evaluate('window.demoCalls.filter(p=>p.startsWith("/ledger?"))')

            def position(fraction):
                box = plot.bounding_box()
                assert box is not None
                return box['x'] + box['width'] * fraction, box['y'] + box['height'] / 2

            def stroke():
                return plot.evaluate('e=>getComputedStyle(e).stroke')

            def mixed_stroke(token, weight):
                # Resolve the host tokens in the rendered SVG, not against CSS text.
                return plot.evaluate('''(e, args) => {
                    const probe = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
                    e.parentNode.appendChild(probe);
                    probe.style.stroke = `color-mix(in srgb,var(${args[0]}) ${args[1]}%,var(--au-surface-bg))`;
                    const result = getComputedStyle(probe).stroke;
                    probe.remove();
                    return result;
                }''', [token, weight])

            assert plot.evaluate('e=>getComputedStyle(e).strokeWidth') == '1px'
            assert plot.evaluate('e=>getComputedStyle(e).vectorEffect') == 'non-scaling-stroke'
            page.mouse.move(*position(.3))
            hover_stroke = stroke()
            assert hover_stroke == mixed_stroke('--ui-text-tertiary', 70)
            assert hover_stroke != 'rgb(255, 255, 255)'
            before = len(calls())
            page.mouse.click(*position(.3))
            expect(chart).to_be_focused()
            focus_stroke = stroke()
            assert focus_stroke == mixed_stroke('--ui-accent', 55)
            assert focus_stroke != hover_stroke
            assert focus_stroke != 'rgb(255, 255, 255)'
            print(f'Computed plot strokes: hover={hover_stroke}, focus={focus_stroke}, width=1px, vector-effect=non-scaling-stroke')
            tip = chart.locator('.au-chart-tip').inner_text()
            chart.press('ArrowRight')
            assert chart.locator('.au-chart-tip').inner_text() != tip
            assert len(calls()) == before, 'Single click and keyboard inspection must not fetch'
            # Normal hand jitter remains a click, not a zoom.
            x, y = position(.3)
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x + 2, y)
            page.mouse.up()
            assert len(calls()) == before

            # Host palette changes update both strokes in place, without remounting.
            page.evaluate("document.documentElement.style.setProperty('--ui-accent', '#6f549d')")
            assert stroke() == mixed_stroke('--ui-accent', 55)
            assert stroke() != focus_stroke
            page.evaluate("document.documentElement.style.removeProperty('--ui-accent')")
            assert stroke() == focus_stroke
            chart.locator('.au-chart-title').click()
            expect(chart).not_to_be_focused()
            page.mouse.move(*position(.3))
            assert stroke() == hover_stroke
            page.evaluate('''() => {
                document.documentElement.style.setProperty('--ui-text-tertiary', '#88829a');
                document.documentElement.style.setProperty('--ui-bg-chrome', '#eeedf5');
            }''')
            assert stroke() == mixed_stroke('--ui-text-tertiary', 70)
            assert stroke() != hover_stroke
            page.evaluate("document.documentElement.style.removeProperty('--ui-text-tertiary'); document.documentElement.style.removeProperty('--ui-bg-chrome')")
            assert stroke() == hover_stroke
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
                assert selection.evaluate('e=>getComputedStyle(e).fill') == 'rgb(167, 153, 239)'
                assert len(calls()) == before, 'Do not fetch while dragging'
                page.mouse.up()
                expect(page.get_by_label('Window start', exact=True)).to_be_visible()
                page.wait_for_function('(n)=>window.demoCalls.filter(p=>p.startsWith("/ledger?")).length>n', arg=before)
                query = parse_qs(urlsplit(calls()[-1]).query)
                lo = data['trend']['buckets'][0]['start']
                hi = data['trend']['buckets'][-1]['end']
                # Pointer-down coordinates are integer CSS pixels in Chromium.
                box = plot.bounding_box()
                assert box is not None
                tolerance = (hi-lo) / box['width'] + 2
                assert abs(float(query['start'][0]) - (lo + .2 * (hi-lo))) <= tolerance, (reverse, query, lo, hi)
                assert abs(float(query['end'][0]) - (lo + .8 * (hi-lo))) <= tolerance, (reverse, query, lo, hi)
                assert query['provider'] == ['openai-codex']
                assert 'test_id' not in query
                assert len(calls()) == before + 1, 'One completed drag, one report refresh'
                expect(selection).to_have_count(0)

            # Cancelled pointer gestures do not apply a time window.
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
            assert not errors, errors
            assert not network, network
            print('PASS plot-only hover/focus, dual tooltip, click/jitter/keyboard, forward/reverse zoom, letterboxed SVG, cancellation and minute labels')
        finally:
            browser.close()


if __name__ == '__main__':
    test_chart_interactions()
