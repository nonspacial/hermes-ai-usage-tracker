"""Offline actual-pane height/width audit, independent of monitor pixels."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[2]

def run():
    out=ROOT/'tests/ui/artifacts';out.mkdir(exist_ok=True)
    results=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'],headless=True,args=['--no-sandbox'])
        page=browser.new_page(viewport={'width':1800,'height':1800})
        page.route('**/*',lambda r:r.abort())
        page.set_content((ROOT/'preview.html').read_text())
        page.get_by_role('tab',name='All providers',exact=True).click()
        page.add_style_tag(content='#root{max-width:none;flex:none;margin:0;padding:0}')
        for width in (600,980,1500):
            previous={}
            for height in (499,500,501,599,600,601,699,700,701,720,849,850,851,1099,1100,1101,1199,1200,1201,1399,1400,1401,1600):
                # Root includes the main tabs; vary the actual provider-pane
                # height rather than mistaking the fixture's outer height for it.
                page.locator('#root').evaluate('(e,w)=>{e.style.width=w+"px"}',width)
                offset=page.evaluate('document.querySelector("#root").clientHeight-document.querySelector(".au-provider-pane").clientHeight')
                page.locator('#root').evaluate('(e,h)=>{e.style.height=h+"px"}',height+offset)
                page.wait_for_timeout(150)
                row=page.evaluate('''()=>{
                    const root=document.querySelector('.au-pane'),u=document.querySelector('.au-upper'),
                      r=document.querySelector('.au-reader'),chart=document.querySelector('.au-chart svg'),
                      nav=document.querySelector('.au-subpage-tabs'),hero=document.querySelector('.au-hero');
                    const pos=e=>e&&e.getBoundingClientRect().bottom-u.getBoundingClientRect().top;
                    return {paneHeight:document.querySelector('.au-provider-pane').clientHeight,
                      upper:u.clientHeight,natural:u.scrollHeight,lower:r.clientHeight,
                      chart:chart.getBoundingClientRect().height,
                      chartTop:chart.getBoundingClientRect().top-u.getBoundingClientRect().top,
                      chartBottom:pos(chart),
                      navTop:nav.getBoundingClientRect().top-root.getBoundingClientRect().top,
                      heroBottom:pos(hero),cap:parseFloat(document.querySelector('.au-provider-pane').style.getPropertyValue('--au-upper-cap')),
                      overflow:root.scrollHeight-root.clientHeight,x:root.scrollWidth-root.clientWidth};
                }''')
                row.update(width=width,height=height);results.append(row)
                assert row['paneHeight']==height,row
                assert row['overflow']<=1 and row['x']<=1,row
                assert row['lower']>=70,row
                if 600<=height<=1100:
                    assert row['lower']>=119,row
                if width==1500 and height==850:
                    assert row['chartBottom']<=row['upper']+1,row
                if width==1500 and height in (849,850):
                    # Short, wide Overview fits naturally; a tiny scrollbar
                    # must not be masked by overflow clipping.
                    assert row['natural']<=row['upper']+1,row
                if height==850 and width in (980,1500):
                    page.locator('#root').screenshot(path=str(out/f'height-density-{width}.png'))
                # Adjacent heights must not lose chart visibility or move
                # navigation upwards: this tests DOM geometry, not CSS strings.
                if height-1 in previous:
                    before=previous[height-1]
                    assert row['upper']>=before['upper']-2,(before,row)
                    assert row['chartBottom']<=before['chartBottom']+3,(before,row)
                    visible=lambda x:max(0,min(x['upper'],x['chartBottom'])-x['chartTop'])
                    assert visible(row)>=visible(before)-1,(before,row)
                    assert row['navTop']>=before['navTop']-2,(before,row)
                    assert row['cap']>=before['cap']-2,(before,row)
                previous[height]=row
        browser.close()
    (out/'height-density.json').write_text(json.dumps(results,indent=2))
    print(f'PASS {len(results)} actual-pane geometry samples across 3 widths; adjacent heights, chart, navigation, containment')

if __name__=='__main__':run()
