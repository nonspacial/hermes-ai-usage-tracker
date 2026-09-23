"""JSON bodies must not change the ten-normal-line budget (offline only)."""
import math
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def settle(page):
    page.evaluate('() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>requestAnimationFrame(r))))')


def geometry(page):
    return page.evaluate('''() => {
      const pane=document.querySelector('.au-provider-pane'),reader=document.querySelector('.au-reader');
      const rows=[...reader.querySelectorAll('tbody tr')];
      const span=rows.at(-1).getBoundingClientRect().bottom-rows[0].getBoundingClientRect().top;
      return {height:pane.clientHeight,cap:parseFloat(pane.style.getPropertyValue('--au-upper-cap')),
        rows:rows.length,line:Math.max(...rows.map(r=>r.getBoundingClientRect().height)),
        chrome:Math.max(0,reader.firstElementChild.getBoundingClientRect().height-span)+document.querySelector('.au-subpage-tabs').getBoundingClientRect().height};
    }''')


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 2800, 'height': 2300})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.route('**/*', lambda r: r.abort())
        page.set_content((ROOT / 'preview.html').read_text())
        page.add_style_tag(content='#root{max-width:none;flex:none;height:2100px;width:2700px;margin:0;padding:0}')
        page.get_by_role('tab', name='All providers', exact=True).click()
        page.get_by_role('navigation', name='Provider subpages').get_by_role('tab', name='Requests', exact=True).click()
        settle(page)
        details = page.locator('.au-reader .au-json-details')
        assert details.count() > 1
        expect(page.locator('.au-reader .au-table')).to_have_attribute('data-layout', 'table')
        baseline = geometry(page)
        assert baseline['line'] > 0
        checks = []

        def check(label):
            settle(page)
            g = geometry(page)
            reserve = g['rows'] * baseline['line'] if g['rows'] < 10 else 10 * baseline['line']
            expected = math.floor(max(g['height']/2, min(g['height'] if g['rows'] < 10 else 2*g['height']/3,
                                      g['height']-g['chrome']-reserve)))
            if 500 < g['height'] < 1400:
                nav_height = page.locator('.au-subpage-tabs').evaluate('e=>e.getBoundingClientRect().height')
                short_reserve = max(nav_height + 120, min(g['height']*.45, g['chrome'] + 2*baseline['line']))
                base_cap = max(g['height']/2, min(g['height'] if g['rows'] < 10 else 2*g['height']/3,
                                                  g['height']-g['chrome']-reserve))
                weight = min(1, (g['height']-500)/100, (1400-g['height'])/300)
                expected = math.floor(base_cap + weight*(max(base_cap,g['height']-short_reserve)-base_cap))
            assert g['cap'] == expected, (label, g, baseline, expected)
            assert not page.locator('.au-measure-lines').count()
            checks.append((label, g['height'], g['cap']))

        check('closed')
        details.evaluate_all('(nodes)=>nodes.forEach(n=>n.open=true)')
        settle(page)
        assert page.locator('.au-reader .au-json-details[open]').count() == details.count()
        assert geometry(page)['line'] > baseline['line']
        check('all-open')
        assert geometry(page)['cap'] == baseline['cap']
        # Re-measure while scrolled; a transient collapsed layout must not clamp it.
        page.locator('.au-reader').evaluate('e=>{e.scrollTop=e.scrollHeight;window.savedScroll=e.scrollTop;e.dispatchEvent(new Event("au-reflow",{bubbles:true}))}')
        settle(page)
        assert page.locator('.au-reader').evaluate('e=>Math.abs(e.scrollTop-window.savedScroll)') <= 1
        for height in [420, 1100, 1400, 2100]:
            page.locator('#root').evaluate('(e,h)=>e.style.height=h+"px"', height)
            check('all-open-resize')
        details.evaluate_all('(nodes)=>nodes.forEach(n=>n.open=false)')
        check('closed-again')
        assert geometry(page)['cap'] == baseline['cap']

        # The returned page becomes a single already-expanded keyed record.
        details.first.evaluate('e=>{e.open=true;window.keptDetails=e}')
        settle(page)
        page.evaluate('''() => {
          const base=rest;
          rest=async(path,...args)=>{const d=await base(path,...args);
            if(path.startsWith('/ledger?')){d.requests=d.requests.slice(0,1);d.request_count=1;d.next_offset=null;}return d;};
          window.demoChange++;for(const callback of window.demoSubscribers)callback({type:'changed',mode:'native-events'});
        }''')
        expect(details).to_have_count(1)
        settle(page)
        assert page.evaluate('keptDetails.isConnected && keptDetails.open')
        check('single-already-open')
        # Different font metrics prove this is not merely a stale cached row height.
        page.add_style_tag(content='.au-reader td{font-size:20px;line-height:2}')
        settle(page)
        opened = geometry(page)
        details.first.evaluate('e=>e.open=false')
        settle(page)
        baseline = geometry(page)
        assert opened['cap'] == baseline['cap'], (opened, baseline)
        check('single-new-metrics-closed')
        details.first.evaluate('e=>e.open=true')
        for height in [420, 1100, 1400, 2100]:
            page.locator('#root').evaluate('(e,h)=>e.style.height=h+"px"', height)
            check('single-open-resize')
        assert not errors, errors
        print('PASS exact clamp formula; all-open/closed, single already-expanded record, changed font metrics, scroll retention:', checks)
        browser.close()


if __name__ == '__main__':
    run()
