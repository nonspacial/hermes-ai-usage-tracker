"""Offline chart overlay and palette regression against the generated preview."""
import os
import re
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[2]
SCRATCH=Path(os.environ.get('HERMES_SCRATCH','/home/nope/.hermes/profiles/infra/cache/scratch'))
CHROME=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium')


def fixture():
    html=(ROOT/'preview.html').read_text()
    inject="""
const template=JSON.parse(JSON.stringify(events[0]));events.splice(0,events.length);
for(const [id,hours,tokens,cost] of [
 ['alpha',3,10,.01],['alpha',2,100,.4],['beta',3,80,.8],['beta',2,20,.02],
 ['beta',1,null,null]]){
 const e=JSON.parse(JSON.stringify(template));e.id=id+'-'+hours;e.provider=id;
 e.started=now-hours*3600+30;e.ended=e.started+1;e.session_id=e.id;
 e.usage=tokens===null?undefined:{...template.usage,total_tokens:tokens,input_tokens:tokens,
  output_tokens:0,cache_read_tokens:0,cache_write_tokens:0,prompt_tokens:tokens};
 e.cost=cost===null?{rate:template.cost.rate,complete:false}:{...template.cost,known_components_usd:cost,total_usd:cost,
  components:{input_tokens:cost,output_tokens:0,cache_read_tokens:0,cache_write_tokens:0},complete:true};
 events.push(e);
}
for(const id of ['alpha','beta'])demoProviders.push({id,label:id,configured:true,
 quota:{available:true,plan:'Free',windows:[{label:'Weekly',remaining_percent:55}]}});
"""
    return html.replace('events[4]=demoContinuation(events[4],events[4].id,events[4].started,512000);',
                        'events[4]=demoContinuation(events[4],events[4].id,events[4].started,512000);'+inject,1)


def chart_values(page,mode):
    return page.evaluate('''() => {
      const paths=[...document.querySelectorAll('.au-chart .au-provider-series')];
      const lines=[...document.querySelectorAll('.au-chart .au-gridline')];
      const bottom=+lines[0].getAttribute('y1'),top=+lines.at(-1).getAttribute('y1');
      const series=paths.map(path=>({id:path.dataset.provider,
        stroke:getComputedStyle(path).stroke,
        points:[...path.getAttribute('d').matchAll(/[ML] ([0-9.]+),([0-9.]+)/g)].map(m=>[+m[1],+m[2]])}));
      return {series,bottom,top,areas:document.querySelectorAll('.au-chart .au-area').length,
        aggregate:document.querySelector('.au-big')?.textContent,
        dots:[...document.querySelectorAll('.au-provider-row')].map(row=>getComputedStyle(row.querySelector('.au-dot')).backgroundColor),
        calls:scopeCalls.filter(x=>new URL(x.path,'https://offline.test').pathname==='/ledger').map(x=>x.path)};
    }''')


def test_overlay():
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=CHROME,headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        try:
            page=browser.new_page(viewport={'width':1480,'height':900})
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.set_content(fixture(),wait_until='domcontentloaded')
            nav=page.get_by_role('navigation',name='Providers',exact=True)
            nav.get_by_role('tab',name='All providers').click()
            panel=page.get_by_test_id('provider-page')
            expect(panel.locator('.au-chart .au-provider-series')).to_have_count(2)
            for mode in ['Tokens','Cost']:
                if mode=='Cost':
                    page.get_by_role('button',name='Cost',exact=True).first.click()
                chart=chart_values(page,mode)
                assert chart['areas']==0 and len(chart['series'])==2,chart
                assert not any(re.search(r'[?&]provider=[^&]+',path) for path in chart['calls']),chart['calls']
                assert chart['series'][0]['stroke']==chart['dots'][0] and chart['series'][1]['stroke']==chart['dots'][1]
                assert chart['aggregate'] not in ('0','—','$0.00'),chart['aggregate']
                # Both traces share the same axis, and differ in shape rather than
                # being rescaled copies of an all-provider subtotal.
                by_id={item['id']:[(x,y) for x,y in item['points'] if y<chart['bottom']-1] for item in chart['series']}
                assert len(by_id['alpha'])==2 and len(by_id['beta'])==2,by_id
                assert by_id['alpha'][0][0]==by_id['beta'][0][0]
                assert by_id['alpha'][1][0]==by_id['beta'][1][0]
                assert by_id['alpha'][0][1]>by_id['beta'][0][1] # beta larger first bucket
                assert by_id['alpha'][1][1]<by_id['beta'][1][1] # alpha larger second bucket
                assert all(chart['top']<=y<=chart['bottom'] for values in by_id.values() for _,y in values)
                chart_el=page.get_by_test_id('usage-chart');chart_el.focus();chart_el.press('ArrowRight')
                page.wait_for_function('document.querySelector(".au-chart")?.getAttribute("data-au-chart-summary")')
                expect(page.get_by_test_id('chart-point-summary')).to_be_visible()
                tooltip=page.locator('#'+str(chart_el.get_attribute('aria-describedby')))
                expect(tooltip.locator('.au-tooltip-provider')).to_have_count(2)
                expect(tooltip.locator('.au-tooltip-provider-dot')).to_have_count(2)
                assert tooltip.locator('.au-tooltip-provider-dot').evaluate_all('''dots=>dots.every((dot,index)=>
                  getComputedStyle(dot).backgroundColor===getComputedStyle(
                    document.querySelectorAll('.au-provider-series')[index]).stroke)''')
                assert tooltip.locator('.au-tooltip-provider').first.evaluate('e=>getComputedStyle(e).color')==page.locator('.au-tooltip:visible').last.evaluate('e=>getComputedStyle(e).color')
                page.screenshot(path=str(SCRATCH/f'provider-overlay-{mode.lower()}-synthetic.png'))
            unknown=page.evaluate('''() => {
              const path=scopeCalls.filter(x=>new URL(x.path,'https://offline.test').pathname==='/ledger').at(-1).path;
              const q=new URL(path,'https://offline.test').searchParams;
              const report=trendFor(events,Number(q.get('start')),Date.now()/1000);
              const stamp=events.find(row=>row.id==='beta-1').started;
              const bucket=report.buckets.find(row=>row.start<=stamp&&row.end>stamp);
              const plot=document.querySelector('.au-plot-hit').getBoundingClientRect();
              return [plot.left+plot.width*((bucket.start+bucket.end)/2-report.buckets[0].start)/
                (report.buckets.at(-1).end-report.buckets[0].start),plot.top+plot.height/2];
            }''')
            page.mouse.move(*unknown)
            expect(page.locator('.au-tooltip:visible .au-tooltip-provider').first).to_be_visible()
            assert any('beta · — · 1 requests · partial price coverage' in line
                       for line in page.locator('.au-tooltip:visible .au-tooltip-provider').all_text_contents())
            page.get_by_role('button',name='Tokens',exact=True).first.click()
            page.mouse.move(*unknown)
            expect(page.locator('.au-tooltip:visible .au-tooltip-provider').first).to_be_visible()
            labels=page.locator('.au-tooltip:visible .au-tooltip-provider').all_text_contents()
            assert any('beta · — tokens · 1 requests · includes missing usage' in line for line in labels),labels
            assert any('alpha · 0 tokens · 0 requests' in line for line in labels),labels
            # One half-hour-grid drag is one shared report read, never one per line.
            hit=panel.locator('.au-plot-hit');hit.scroll_into_view_if_needed()
            box=hit.bounding_box();assert box
            before=len(chart_values(page,'Cost')['calls'])
            y=box['y']+box['height']/2
            page.mouse.move(box['x']+box['width']*.2,y)
            page.mouse.down()
            page.mouse.move(box['x']+box['width']*.8,y,steps=6)
            expect(panel.locator('.au-plot-selection')).to_be_visible()
            assert len(chart_values(page,'Cost')['calls'])==before
            page.mouse.up()
            page.wait_for_function('(before)=>scopeCalls.filter(x=>new URL(x.path,"https://offline.test").pathname==="/ledger").length>before',arg=before)
            assert len(chart_values(page,'Cost')['calls'])==before+1
            assert not re.search(r'[?&]provider=[^&]+',chart_values(page,'Cost')['calls'][-1])
            page.get_by_role('group',name='Time window').get_by_role('button',name='Past 24h',exact=True).click()
            # Selected provider retains its old single filled line and exact single-provider values.
            nav.get_by_role('tab',name='alpha',exact=True).click()
            expect(panel.locator('.au-chart .au-line')).to_have_count(1)
            expect(panel.locator('.au-chart .au-area')).to_have_count(1)
            page.screenshot(path=str(SCRATCH/'provider-overlay-individual-synthetic.png'))
            assert not errors,errors
            print('PASS: real synthetic per-provider bucket shapes, shared axes, no All fill, Cost/Tokens, coloured marks, unchanged individual area')
            print('Screenshots:',*[str(SCRATCH/f'provider-overlay-{x}-synthetic.png') for x in ('tokens','cost','individual')])
        finally:
            browser.close()


if __name__=='__main__':test_overlay()
