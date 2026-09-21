"""Changing pending usage must preserve pane geometry and mounted content."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1100, 'height': 1100})
        page.route('**/*', lambda r: r.abort())
        page.set_content((ROOT / 'preview.html').read_text())
        page.get_by_role('tab', name='All providers', exact=True).click()
        page.add_style_tag(content='#root{max-width:none;flex:none;height:900px;width:600px;margin:0;padding:0}')
        page.wait_for_timeout(200)
        page.evaluate('''() => {window.awaiting=0;const base=rest;rest=async(...args)=>{
          await new Promise(r=>setTimeout(r,120));const out=await base(...args);
          if(args[0].startsWith('/ledger?')&&out.summary){out.summary={...out.summary,pending:window.awaiting,unresolved:23,
            missing_fields:{...out.summary.missing_fields,total_tokens:23+window.awaiting,input_tokens:23+window.awaiting},
            missing_reasons:{total_tokens:{awaiting_usage:window.awaiting,unresolved_execution:23},input_tokens:{awaiting_usage:window.awaiting,unresolved_execution:23}}};}
          return out;
        };queryClient.invalidateQueries()}''')
        page.wait_for_timeout(200)
        failures=[]
        cases=[(w,h,t) for w in (390,460,600,740,980) for h in (900,1400) for t in ['Overview','Requests','Cache & costs','Compressions','Models & tasks','Skills usage']]
        for width,height,tab in cases:
            page.locator('#root').evaluate('(e,s)=>{e.style.width=s[0]+"px";e.style.height=s[1]+"px"}',[width,height])
            page.get_by_role('navigation', name='Provider subpages').get_by_role('tab',name=tab,exact=True).click()
            page.wait_for_timeout(200)
            result=page.evaluate('''async()=>{
                const upper=document.querySelector('.au-upper'),summary=document.querySelector('[data-testid="recorded-summary"]');
                const heights=[upper.getBoundingClientRect().height];let running=true;
                function frame(){heights.push(upper.getBoundingClientRect().height);if(running)requestAnimationFrame(frame)}
                requestAnimationFrame(frame);window.awaiting=window.awaiting?0:1;queryClient.invalidateQueries();
                await new Promise(r=>setTimeout(r,500));running=false;
                return {min:Math.min(...heights),max:Math.max(...heights),same:summary===document.querySelector('[data-testid="recorded-summary"]')};
            }''')
            if result['max']-result['min']>1 or not result['same']:
                failures.append((width,height,tab,result))
        browser.close()
        assert not failures, failures
        print('PASS pending usage transitions preserve summary nodes and frame-by-frame pane height across provider tabs')


if __name__ == '__main__':
    run()
