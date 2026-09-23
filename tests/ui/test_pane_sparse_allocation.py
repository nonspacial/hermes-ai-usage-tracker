"""Dense -> sparse -> dense allocation in real, narrow host panes; offline."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def settle(page):
    page.evaluate('() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>requestAnimationFrame(r))))')


def geometry(page):
    return page.evaluate('''() => {
      const upper=document.querySelector('.au-upper'),reader=document.querySelector('.au-reader'),
        pane=document.querySelector('.au-provider-pane'),nav=document.querySelector('.au-subpage-tabs');
      const saved=upper.getAttribute('style');
      upper.style.maxHeight='none';upper.style.flex='0 0 auto';
      const natural=upper.getBoundingClientRect().height;
      if(saved===null)upper.removeAttribute('style');else upper.setAttribute('style',saved);
      return {natural,columns:getComputedStyle(upper.querySelector('.au-totals')).gridTemplateColumns.split(' ').length,
        upper:upper.clientHeight,overflow:upper.scrollHeight-upper.clientHeight,
        lower:reader.clientHeight,need:reader.firstElementChild.getBoundingClientRect().height,
        height:pane.clientHeight,nav:nav.getBoundingClientRect().height,
        divider:nav.getBoundingClientRect().top,root:pane.scrollHeight,
        cap:parseFloat(pane.style.getPropertyValue('--au-upper-cap'))};
    }''')


def run():
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True,
                                 args=['--no-sandbox','--disable-dev-shm-usage'])
        page=browser.new_page(viewport={'width':2400,'height':2300})
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.route('**/*',lambda r:r.abort())
        page.set_content((ROOT/'preview.html').read_text())
        page.add_style_tag(content='#root{max-width:none;flex:none;height:900px;width:2200px;margin:0;padding:0}')
        page.get_by_role('tab',name='All providers',exact=True).click()
        page.evaluate('''() => {
          const base=rest;window.sparseCount=2;
          rest=async(path,...args)=>{const d=await base(path,...args);
            if(path.startsWith('/ledger?')){
              d.requests=Array.from({length:40},(_,i)=>({...d.requests[0],id:'dense-'+i,session_id:'dense-'+i}));
              d.compressions=Array.from({length:window.sparseCount},(_,i)=>({...d.compressions[0],id:'sparse-'+i,session_id:'sparse-'+i}));
              d.request_count=40;d.next_offset=null;
            }return d;};window.demoChange++;for(const callback of window.demoSubscribers)callback({type:'changed',mode:'native-events'});
        }''')
        nav=page.get_by_role('navigation',name='Provider subpages')
        # Overview groups never collapse: all stacked fields count towards
        # their normal height, even when fewer than ten groups are present.
        page.locator('#root').evaluate('(e)=>{e.style.width="460px";e.style.height="1400px"}')
        nav.get_by_role('tab',name='Overview',exact=True).click()
        expect(page.locator('.au-reader tbody tr')).to_have_count(4)
        expect(page.locator('.au-reader .au-table')).to_have_attribute('data-layout','records')
        expect(page.locator('.au-reader .au-table')).to_have_attribute('data-accordions','false')
        settle(page)
        overview=geometry(page)
        assert overview['need']+overview['nav']>overview['height']/2,overview
        assert abs(overview['upper']-overview['height']/2)<=1,overview
        assert page.locator('.au-reader tbody tr>td:not(:first-child)').evaluate_all(
            'cells=>cells.every(cell=>getComputedStyle(cell).display!=="none")')
        page.locator('.au-reader tbody tr').last.scroll_into_view_if_needed()
        assert page.locator('.au-reader tbody tr').last.evaluate(
            'e=>e.getBoundingClientRect().bottom<=e.closest(".au-reader").getBoundingClientRect().bottom+1')
        print('PASS narrow non-collapsible Overview:',overview)
        results=[]
        for width in [982,980,740,600,460,982]:
            for height in [420,900,1400,2100]:
                page.locator('#root').evaluate('(e,s)=>{e.style.width=s[0]+"px";e.style.height=s[1]+"px"}',[width,height])
                for count in [0,1,2,9]:
                    page.evaluate("(n)=>{window.sparseCount=n;window.demoChange++;for(const callback of window.demoSubscribers)callback({type:'changed',mode:'native-events'})}",count)
                    nav.get_by_role('tab',name='Requests',exact=True).click()
                    expect(page.locator('.au-reader tbody tr')).to_have_count(40)
                    settle(page)
                    dense=geometry(page)
                    if 600 <= dense['height'] <= 1100:
                        assert dense['lower']>=119,dense
                    elif dense['height'] <= 500 or dense['height'] >= 1400:
                        assert dense['upper']<=dense['height']*2/3+1,dense
                    page.locator('.au-reader [data-scroll-key]').last.scroll_into_view_if_needed()
                    nav.get_by_role('tab',name='Compressions',exact=True).click()
                    expect(page.locator('.au-reader tbody tr')).to_have_count(count)
                    settle(page)
                    sparse=geometry(page)
                    assert sparse['columns']==(7 if width>980 else 3 if width>620 else 2),sparse
                    assert sparse['upper']<=sparse['natural']+1,sparse
                    assert abs(sparse['natural']-dense['natural'])<=1,(dense,sparse)
                    if sparse['natural']+sparse['need']+sparse['nav']<=sparse['height']-2:
                        assert abs(sparse['upper']-sparse['natural'])<=1,(width,height,count,sparse)
                        assert sparse['overflow']<=1,sparse
                    assert page.locator('.au-upper').evaluate('e=>Math.abs(e.getBoundingClientRect().bottom-e.nextElementSibling.getBoundingClientRect().top)<=1')
                    # If both regions can fit, upper scrolling and empty lower
                    # space must not coexist. This catches phantom row reserves.
                    if sparse['need']+sparse['nav'] < sparse['height']/2:
                        assert sparse['overflow']<=1 or sparse['lower']<=sparse['need']+2,(width,height,count,dense,sparse)
                    if dense['overflow']>1 and sparse['need']+sparse['nav']<dense['height']-dense['upper']-3:
                        assert sparse['divider']>dense['divider']+1,(width,height,count,dense,sparse)
                    assert sparse['root']<=sparse['height']+1,sparse
                    assert nav.evaluate('e=>e.getBoundingClientRect().bottom<=e.parentElement.getBoundingClientRect().bottom+1')
                    if count:
                        last=page.locator('.au-reader [data-scroll-key]').last
                        last.scroll_into_view_if_needed()
                        assert last.evaluate('e=>e.getBoundingClientRect().top<e.closest(".au-reader").getBoundingClientRect().bottom')
                    nav.get_by_role('tab',name='Requests',exact=True).click()
                    expect(page.locator('.au-reader tbody tr')).to_have_count(40)
                    settle(page)
                    again=geometry(page)
                    assert abs(again['upper']-dense['upper'])<=1,(dense,again)
                    assert abs(again['cap']-dense['cap'])<=1,(dense,again)
                    results.append((width,height,count,round(dense['upper']),round(sparse['upper'])))
        assert not errors,errors
        print('PASS',len(results),'dense/sparse/dense transitions; width,height,rows,dense upper,sparse upper:',results)
        browser.close()


if __name__=='__main__':
    run()
