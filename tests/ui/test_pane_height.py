"""Bounded real-pane allocation and keyed arrival retention; synthetic/offline only."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]

FIXTURE = """() => {
 const base=rest;window.arrivals=0;
 rest=async(path,...args)=>{
  const d=await base(path,...args);
  const copies=(source,kind)=>Array.from({length:80+window.arrivals},(_,i)=>{
   const n=i-window.arrivals,r=JSON.parse(JSON.stringify(source));
   return {...r,id:kind+n,key:kind+n,model:kind+n,name:kind+n,skill:kind+n,
    session_id:'session-'+n,started:now-100-n,ts:now-100-n};
  });
  if(path.startsWith('/ledger?')){
   for(const name of ['requests','compressions','groups','model_groups','applied_rate_groups'])
    d[name]=copies(d[name][0],name);
   d.request_count=d.requests.length;d.next_offset=null;
  }
  if(path.startsWith('/ledger/skills?')){
   d.events=copies(d.events[0],'events');d.event_count=d.events.length;
   d.skills=copies(d.skills.find(x=>x.loads>0),'skills');
   d.snapshots=d.snapshots.map(s=>({...s,categories:copies(s.categories?.[0]||{label:'Category',tokens:10},'category').map(c=>({...c,label:c.id}))}));
  }
  return d;
 };
 window.demoChange++;for(const callback of window.demoSubscribers)callback({type:'changed',mode:'native-events'});
}"""


def settle(page):
    page.evaluate('() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>requestAnimationFrame(r))))')


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 2400, 'height': 2300})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.route('**/*', lambda r: r.abort())
        page.set_content((ROOT / 'preview.html').read_text())
        page.add_style_tag(content='#root{max-width:none;flex:none;height:900px;width:2200px;margin:0;padding:0}')
        page.get_by_role('tab', name='All providers', exact=True).click()
        page.evaluate(FIXTURE)
        nav = page.get_by_role('navigation', name='Provider subpages')
        results = []
        for width, height in [(2200, 900), (390, 900), (2200, 420), (390, 420), (2200, 2100), (390, 2100), (320, 260)]:
            page.locator('#root').evaluate('(e,s)=>{e.style.width=s[0]+"px";e.style.height=s[1]+"px"}', [width, height])
            # Skills now has aggregate reports, not an 80-entry feed; its own
            # browser check covers the chart and narrow geometry separately.
            for tab in ['Overview', 'Requests', 'Cache & costs', 'Compressions', 'Models & tasks']:
                nav.get_by_role('tab', name=tab, exact=True).click()
                settle(page)
                reader = page.locator('.au-reader')
                entries = reader.locator('[data-scroll-key]')
                expect(entries).to_have_count(80 + page.evaluate('arrivals'))
                geometry = page.evaluate('''() => {
                 const root=document.querySelector('.au-pane'),upper=document.querySelector('.au-upper'),lower=document.querySelector('.au-reader'),pane=document.querySelector('.au-provider-pane');
                 return {root:root.clientHeight,total:root.scrollHeight,upper:upper.clientHeight,lower:lower.clientHeight,cap:parseFloat(pane.style.getPropertyValue('--au-upper-cap')),height:pane.clientHeight,x:lower.scrollWidth-lower.clientWidth};
                }''')
                assert geometry['total'] <= geometry['root'] + 1, (tab, geometry)
                if 600 <= geometry['height'] <= 1100:
                    assert geometry['lower'] >= 119, (tab, geometry)
                else:
                    assert geometry['upper'] <= geometry['height'] * 2 / 3 + 1, (tab, geometry)
                assert geometry['lower'] > 70, (tab, geometry)
                assert geometry['x'] <= 1, (tab, width, geometry)
                if height <= 420:
                    assert abs(geometry['cap'] - geometry['height'] / 2) <= 1, (tab, geometry)
                if height == 2100 and tab == 'Requests':
                    assert geometry['cap'] > geometry['height'] / 2, geometry
                    reader.evaluate('e=>e.scrollTop=0')
                    assert entries.nth(9).evaluate('e=>e.getBoundingClientRect().bottom<=e.closest(".au-reader").getBoundingClientRect().bottom+1')
                    if width == 2200:
                        assert geometry['upper'] < geometry['cap'] - 50, geometry  # cap, not a forced allocation
                upper=page.locator('.au-upper')
                upper.focus()
                upper.press('Control+End')
                page.wait_for_function('(()=>{const e=document.querySelector(".au-upper");return e.scrollTop+e.clientHeight>=e.scrollHeight-1})()')
                upper.press('Control+Home')
                page.wait_for_function('document.querySelector(".au-upper").scrollTop===0')
                target = entries.nth(30)
                disclosure=target.locator('.au-record-disclosure')
                opened=False
                if disclosure.count() and disclosure.is_visible():
                    disclosure.click()
                    opened=True
                settle(page)
                target.evaluate('e=>{const r=e.closest(".au-reader");r.scrollTop+=e.getBoundingClientRect().top-r.getBoundingClientRect().top-45;window.held=e}')
                settle(page)
                before = target.evaluate('e=>e.getBoundingClientRect().top-e.closest(".au-reader").getBoundingClientRect().top')
                assert reader.evaluate('e=>e.scrollTop>0')
                root_height = page.locator('.au-pane').evaluate('e=>e.scrollHeight')
                page.evaluate("arrivals+=3;window.demoChange++;for(const callback of window.demoSubscribers)callback({type:'changed',mode:'native-events'})")
                expect(entries).to_have_count(80 + page.evaluate('arrivals'))
                settle(page)
                delta = page.evaluate('held.getBoundingClientRect().top-held.closest(".au-reader").getBoundingClientRect().top') - before
                assert abs(delta) <= 3, (tab, width, height, delta)
                assert page.locator('.au-pane').evaluate('e=>e.scrollHeight') == root_height
                assert page.evaluate('held.isConnected')
                if opened:
                    assert page.evaluate('held.open || held.dataset.expanded==="true"')
                # Last record remains reachable, including a tall narrow card.
                entries.last.scroll_into_view_if_needed()
                assert entries.last.evaluate('e=>e.getBoundingClientRect().top<e.closest(".au-reader").getBoundingClientRect().bottom')
                reader.focus()
                reader.press('Control+Home')
                settle(page)
                page.wait_for_function('document.querySelector(".au-reader").scrollTop===0')
                results.append((width, height, tab, geometry['cap']))
            page.screenshot(path=str(ROOT / 'tests/ui/artifacts' / f'pane-{width}-{height}.png'))
        assert not errors, errors
        assert len(results) == 35
        print('PASS 35 event-pane/view combinations: actual pane heights 260/420/900/2100, widths 320/390/2200; upper caps, lower bounds, keyboard/end reachability, prepend anchor and stable root height')
        print(results)
        browser.close()


if __name__ == '__main__':
    run()
