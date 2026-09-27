"""Offline pane-width regression for stable mobile graph geometry and mapping."""
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
WIDTHS = (850, 849, 800, 789, 788, 787, 700, 390)


def split_mode(width, height):
    # Fixed pane-size breakpoints shared with the row-quota/quota-top tests.
    content = width - 32
    need = 893 if content <= 900 else 871 if content < 940 else 862 if content < 1436 else 757
    return width >= 850 and height - 32 >= need


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'],
                                    headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            page = browser.new_page(viewport={'width': 1920, 'height': 850}, locale='en-GB')
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', lambda route: route.abort())
            page.set_content((ROOT / 'preview.html').read_text())
            page.get_by_role('tab', name='All providers', exact=True).click()
            heights = {}
            # 850x850 is a short medium pane: it now uses the single scroller and
            # must keep the 849px graph height. 850x925 is the split boundary.
            for width, window, height in (*((w, w, 850) for w in WIDTHS), (800, 1920, 850), (850, 850, 925)):
                page.set_viewport_size({'width': window, 'height': height})
                # Set or clear the narrow-tile width so a later case is not left inside it.
                page.evaluate('''w=>{document.querySelector('.au-pane').parentElement.style.width=w?w+'px':''}''',
                              width if window != width else 0)
                page.wait_for_function('''([w,m])=>{
                    const pane=document.querySelector('.au-pane');
                    return Math.abs(pane.getBoundingClientRect().width-w)<1 &&
                           pane.classList.contains('au-mobile')===m;
                }''', arg=[width, not split_mode(width, height)])
                page.wait_for_function('''() => {
                    const svg=document.querySelector('.au-chart svg');
                    return svg.clientHeight>190 &&
                           Math.abs(svg.viewBox.baseVal.width-svg.clientWidth)<1 &&
                           Math.abs(svg.viewBox.baseVal.height-svg.clientHeight)<1;
                }''')
                geometry = page.evaluate('''() => {
                    const q=s=>document.querySelector(s),r=e=>e.getBoundingClientRect();
                    const root=q('.au-pane'),svg=q('.au-chart svg'),hit=q('.au-plot-hit');
                    const labels=[...svg.querySelectorAll('text.au-axis')].slice(-3);
                    return {pane:r(root).width,chart:r(q('.au-chart')).width,
                        svg:r(svg).height,svgWidth:r(svg).width,plot:r(hit).height,
                        plotWidth:r(hit).width,viewBox:svg.getAttribute('viewBox'),
                        labelFont:getComputedStyle(labels[0]).fontSize,
                        labels:labels.map(e=>({left:r(e).left,right:r(e).right,top:r(e).top,bottom:r(e).bottom})),
                        svgLeft:r(svg).left,svgRight:r(svg).right,svgBottom:r(svg).bottom,
                        rootOverflow:getComputedStyle(root).overflowY,
                        readerOverflow:getComputedStyle(q('.au-reader')).overflowY,
                        documentExcess:document.documentElement.scrollHeight-document.documentElement.clientHeight};
                }''')
                heights[(width, window, height)] = geometry
                assert abs(geometry['pane'] - width) < 1, geometry
                assert geometry['plotWidth'] > 0 and geometry['svgWidth'] <= width, geometry
                assert geometry['labelFont'] == '11px', geometry
                assert all(label['left'] >= geometry['svgLeft']-2 and
                           label['right'] <= geometry['svgRight']+2 and
                           label['bottom'] <= geometry['svgBottom']+2
                           for label in geometry['labels']), geometry
                if not split_mode(width, height):
                    assert geometry['rootOverflow'] == 'auto' and geometry['readerOverflow'] == 'visible', geometry
                    assert abs(geometry['svg'] - 196.3125) < 1, geometry
                    if (849, 849, 850) in heights:
                        assert abs(geometry['plot'] - heights[(849, 849, 850)]['plot']) < 1, geometry
                else:
                    assert geometry['rootOverflow'] == 'hidden' and geometry['readerOverflow'] == 'auto', geometry
                    assert geometry['plot'] > heights[(849, 849, 850)]['plot'], geometry
                assert geometry['documentExcess'] <= 1, geometry
                print(f'{width}x{height} pane / {window}px window: SVG {geometry["svg"]:.2f}px, plot {geometry["plot"]:.2f}px')

            assert abs(heights[(850, 850, 850)]['plot'] - heights[(849, 849, 850)]['plot']) < 1, heights
            # The chart hit target is spatially reachable, keyboard inspection
            # updates the same tooltip, and a drag maps its screen fractions to
            # exactly the submitted half-hour boundaries in each mobile mode.
            page.evaluate('''() => {const original=rest;rest=async (...args)=>{
                const response=await original(...args);
                if(args[0].startsWith('/ledger?'))window.chartData=response;
                return response;
            }}''')
            for width in (849, 789, 390):
                page.set_viewport_size({'width': width, 'height': 850})
                page.evaluate("document.querySelector('.au-pane').parentElement.style.width='';window.chartData=null")
                period = page.get_by_role('combobox', name='Time window', exact=True)
                period.select_option(label='Past hour')
                page.wait_for_function('''() => document.querySelector('.au-pane').classList.contains('au-mobile') && window.chartData?.trend?.seconds === 120''')
                page.wait_for_function('''() => document.querySelector('.au-plot-hit') &&
                    window.demoCalls.filter(v=>v.startsWith('/ledger?')).length > 0''')
                chart = page.get_by_test_id('usage-chart')
                plot = chart.locator('.au-plot-hit')
                plot.scroll_into_view_if_needed()
                box = plot.bounding_box()
                assert box is not None, box
                assert box['height'] >= 164 and box['width'] > 0, box
                x = lambda f: box['x'] + box['width'] * f
                y = box['y'] + box['height'] / 2
                page.mouse.click(x(.25), y)
                expect(chart).to_be_focused()
                page.mouse.move(x(.75), y)
                summary = chart.get_attribute('data-au-chart-summary')
                assert summary and summary != 'None'
                chart.press('ArrowLeft')
                assert chart.get_attribute('data-au-chart-summary') != summary
                calls = page.evaluate('window.demoCalls.filter(v=>v.startsWith("/ledger?"))')
                data = page.evaluate('window.chartData')
                buckets = data['trend']['buckets']
                first, last = buckets[0]['start'], buckets[-1]['end']
                edges = [t for t in (first, *(row['end'] for row in buckets)) if t % 1800 == 0]
                nearest = lambda f: min(edges, key=lambda t: abs(t - (first + f * (last - first))))
                expected = (nearest(.2), nearest(.8))
                page.mouse.move(x(.2), y)
                page.mouse.down()
                page.mouse.move(x(.8), y, steps=6)
                expect(chart.locator('.au-plot-selection')).to_be_visible()
                shade = chart.locator('.au-plot-selection').bounding_box()
                assert shade and shade['height'] >= 164, (shade, box)
                expected_left = x((expected[0]-first)/(last-first))
                expected_right = x((expected[1]-first)/(last-first))
                assert abs(shade['x']-expected_left) < 2 and abs(shade['x']+shade['width']-expected_right) < 2, (shade, expected_left, expected_right)
                page.mouse.up()
                page.wait_for_function('(n)=>window.demoCalls.filter(v=>v.startsWith("/ledger?")).length>n', arg=len(calls))
                query = parse_qs(urlsplit(page.evaluate('window.demoCalls.filter(v=>v.startsWith("/ledger?")).at(-1)')).query)
                assert (float(query['start'][0]), float(query['end'][0])) == expected, (width, query, expected)
                assert float(query['end'][0])-float(query['start'][0]) >= 1800, query
                assert float(query['start'][0])%1800 == float(query['end'][0])%1800 == 0, query
                assert len(page.evaluate('window.demoCalls.filter(v=>v.startsWith("/ledger?"))')) == len(calls)+1
                print(f'{width}px pointer/keyboard/drag: one aligned range request')
            assert not errors, errors
        finally:
            browser.close()


if __name__ == '__main__':
    run()
