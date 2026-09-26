"""Offline regression: original provider quota placement and complete-page actions."""
import os
from pathlib import Path
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SHOTS = Path(os.environ['TMPDIR']) / 'quota-top-paging'


def run():
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1920, 'height': 1080})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', lambda route: route.abort())
        html = (ROOT / 'preview.html').read_text()
        page.set_content(html)
        page.get_by_role('tab', name='All providers', exact=True).click()
        providers = page.get_by_role('navigation', name='Providers', exact=True)
        subpages = page.get_by_role('navigation', name='Provider subpages', exact=True)
        for width, height in ((3840, 2160), (1920, 1080), (980, 850), (851, 850), (850, 850), (849, 850), (800, 850), (390, 850)):
            page.set_viewport_size({'width': width, 'height': height})
            for name in ('Codex', 'Nous Portal', 'Ollama Cloud', 'OpenRouter'):
                tab = providers.get_by_role('tab', name=name, exact=True)
                if tab.is_visible():
                    tab.click()
                else:
                    page.get_by_role('combobox', name='Main page').select_option(label=name)
                quota = page.get_by_test_id('provider-limits')
                expect(quota).to_be_visible()
                geometry = page.evaluate('''() => {
                    const pane=document.querySelector('[data-testid="provider-page"]');
                    const quota=pane.querySelector('[data-testid="provider-limits"]');
                    const nav=pane.querySelector('[data-testid="main-navigation"]');
                    const controls=pane.querySelector('.au-view-controls');
                    const summary=pane.querySelector('[data-testid="recorded-summary"]');
                    const box=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,right:r.right,bottom:r.bottom,width:r.width}};
                    return {pane:box(pane),quota:box(quota),nav:box(nav),controls:box(controls),summary:box(summary),
                        quotaParent:quota.parentElement.className,detailCount:quota.querySelectorAll('.au-quota-rows>*').length,
                        hidden:!!quota.querySelector('.au-quota-detail-disclosure')};
                }''')
                assert geometry['quotaParent'] == 'au-upper', geometry
                assert geometry['nav']['bottom'] <= geometry['quota']['y'] + 1, geometry
                assert geometry['quota']['bottom'] <= geometry['controls']['y'] + 1, geometry
                assert geometry['quota']['bottom'] <= geometry['summary']['y'] + 1, geometry
                assert abs(geometry['quota']['width'] - geometry['pane']['width']) < 3, geometry
                assert not geometry['hidden'], geometry
                if width == 390:
                    owners = page.evaluate('''() => {
                        const root=document.querySelector('.au-pane'), upper=document.querySelector('.au-upper');
                        const reader=document.querySelector('.au-reader');
                        return {root:root.scrollHeight-root.clientHeight,
                            rootOverflow:getComputedStyle(root).overflowY,
                            upperOverflow:getComputedStyle(upper).overflowY,
                            readerOverflow:getComputedStyle(reader).overflowY};
                    }''')
                    assert owners['root'] > 0 and owners['rootOverflow'] == 'auto', owners
                    assert owners['upperOverflow'] == 'visible' and owners['readerOverflow'] == 'visible', owners
                if width in (1920, 980, 851, 850):
                    bounds = page.evaluate('''() => {
                        const rect=s=>document.querySelector(s).getBoundingClientRect();
                        const upper=rect('.au-upper'), chart=rect('.au-chart svg'), plot=rect('.au-plot-hit');
                        const quality=rect('.au-quality-line'), nav=rect('.au-subpage-tabs');
                        return {upperBottom:upper.bottom, chartBottom:chart.bottom, plotHeight:plot.height,
                            qualityBottom:quality.bottom, navTop:nav.top};
                    }''')
                    assert bounds['chartBottom'] <= bounds['upperBottom'] + 1, (width,name,bounds)
                    assert bounds['chartBottom'] <= bounds['navTop'] + 1, (width,name,bounds)
                    assert bounds['qualityBottom'] <= bounds['upperBottom'] + 1, (width,name,bounds)
                    assert bounds['plotHeight'] >= 139.5, (width,name,bounds)
                if name == 'Nous Portal' and width in (3840, 1920, 980, 390):
                    fit = page.evaluate('''() => {
                        const upper=document.querySelector('.au-upper'), reader=document.querySelector('.au-reader');
                        const chart=document.querySelector('.au-chart svg'), plot=document.querySelector('.au-plot-hit');
                        const quality=document.querySelector('.au-quality-line');
                        const quota=document.querySelector('[data-testid="provider-limits"]');
                        const nav=document.querySelector('.au-subpage-tabs');
                        const cards=[...document.querySelectorAll('.au-totals>*')];
                        const rect=e=>e.getBoundingClientRect();
                        return {upperHeight:rect(upper).height,upperBottom:rect(upper).bottom,
                            upperOverflow:upper.scrollHeight-upper.clientHeight,
                            quotaHeight:rect(quota).height, chartBottom:rect(chart).bottom,
                            plotHeight:rect(plot).height,qualityBottom:rect(quality).bottom,
                            navTop:rect(nav).top, readerTop:rect(reader).top,
                            cardRows:new Set(cards.map(e=>Math.round(rect(e).top))).size,
                            cards:cards.map(e=>({width:rect(e).width,scrollWidth:e.scrollWidth,clientWidth:e.clientWidth,
                                children:[...e.children].map(c=>({text:c.textContent,width:rect(c).width,
                                    right:rect(c).right,cardRight:rect(e).right,scrollWidth:c.scrollWidth,clientWidth:c.clientWidth}))}))};
                    }''')
                    print(f'FIT {width}x{height} Nous: ' + str({k:v for k,v in fit.items() if k != 'cards'}))
                    if width in (1920, 980, 851, 850):
                        assert fit['upperOverflow'] <= 1, fit
                        assert fit['chartBottom'] <= fit['upperBottom'] + 1, fit
                        assert fit['qualityBottom'] <= fit['upperBottom'] + 1, fit
                        assert fit['chartBottom'] <= fit['navTop'] + 1, fit
                        assert fit['plotHeight'] >= 139.5, fit
                        assert fit['cardRows'] == (1 if width >= 972 else 2), fit
                        assert len(fit['cards']) == 7, fit
                        assert all(c['scrollWidth'] <= c['clientWidth'] + 1 for c in fit['cards']), fit
                if name == 'Nous Portal':
                    assert geometry['detailCount'] >= 6, geometry
                if (width, name) in ((1920, 'Codex'), (1920, 'Nous Portal'), (980, 'Nous Portal'), (390, 'Codex')):
                    page.screenshot(path=str(SHOTS / f'quota-top-{width}-{name.replace(" ", "-")}.png'))
            if width >= 980:
                providers.get_by_role('tab', name='All providers', exact=True).click()
                subpages.get_by_role('tab', name='Skills usage', exact=True).click()
                views = page.get_by_role('group', name='Skills view', exact=True)
                dividers = []
                for view in ('Frequency', 'Context footprint', 'Catalogue overhead'):
                    views.get_by_role('button', name=view, exact=True).click()
                    expect(page.get_by_test_id('skills-usage')).to_be_visible()
                    dividers.append(page.locator('.au-reader').evaluate('e=>e.getBoundingClientRect().top'))
                assert max(dividers)-min(dividers) < 1, (width, dividers)
        # Below 850px the pane is a single-scroller mobile layout; at and
        # above 850px it retains the fixed split without clipping Nous.
        for width in (800, 849, 850, 851, 900, 979):
            page.set_viewport_size({'width': width, 'height': 850})
            page.wait_for_function('(w)=>document.querySelector(".au-pane").classList.contains("au-mobile")===(w<850)', arg=width)
            choice = providers.get_by_role('tab', name='Nous Portal', exact=True)
            if choice.is_visible():
                choice.click()
            else:
                page.get_by_role('combobox', name='Main page').select_option(label='Nous Portal')
            narrow = page.evaluate('''() => {
                const r=s=>document.querySelector(s).getBoundingClientRect();
                const upper=r('.au-upper'),chart=r('.au-chart svg'),plot=r('.au-plot-hit');
                const nav=r('.au-subpage-tabs'),quality=r('.au-quality-line');
                return {upperBottom:upper.bottom,chartBottom:chart.bottom,plotHeight:plot.height,
                    qualityBottom:quality.bottom,navTop:nav.top,
                    metricRows:new Set([...document.querySelectorAll('.au-totals>*')].map(e=>Math.round(e.getBoundingClientRect().top))).size};
            }''')
            print(f'BREAKPOINT {width}x850 Nous: {narrow}')
            assert narrow['plotHeight'] >= 139.5, narrow
            if width >= 850:
                assert narrow['chartBottom'] <= narrow['upperBottom'] + 1, narrow
                assert narrow['qualityBottom'] <= narrow['upperBottom'] + 1, narrow
                assert narrow['metricRows'] == (1 if width >= 972 else 2), narrow
        # Exercise a narrow tile inside a wide browser, not just a narrow viewport.
        page.set_viewport_size({'width': 1800, 'height': 850})
        page.locator('#root').evaluate('e=>{e.style.width="800px";e.style.flex="none"}')
        page.wait_for_function("document.querySelector('.au-pane').classList.contains('au-mobile')")
        assert page.locator('.au-pane').evaluate('e=>e.getBoundingClientRect().width') == 800
        page.locator('#root').evaluate('e=>{e.style.width="";e.style.flex=""}')
        page.set_viewport_size({'width': 849, 'height': 850})
        page.wait_for_function("document.querySelector('.au-pane').classList.contains('au-mobile')")
        page.get_by_role('combobox', name='Main page').select_option(label='All providers')
        subpages.get_by_role('tab', name='Requests', exact=True).click()
        disclosure = page.locator('.au-reader .au-record-disclosure').first
        expect(disclosure).to_be_visible()
        disclosure.click()
        expect(disclosure).to_have_attribute('aria-expanded', 'true')
        inspector = page.locator('.au-reader .au-json-details').first
        inspector.locator('summary').click()
        expect(inspector).to_have_attribute('open', '')
        inspector.evaluate('e=>window.originalInspector=e')
        copy = inspector.get_by_role('button', name='Copy JSON to clipboard')
        copy.focus()
        for width in (849, 850, 851, 849):
            page.set_viewport_size({'width': width, 'height': 850})
            page.wait_for_function('(w)=>document.querySelector(".au-pane").classList.contains("au-mobile")===(w<850)', arg=width)
            expect(copy).to_be_focused()
            assert inspector.evaluate('e=>e===window.originalInspector && e.open')
            expect(disclosure).to_have_attribute('aria-expanded', 'true')
            scroll = page.evaluate('''() => {
                const q=s=>document.querySelector(s), info=s=>{const e=q(s);return {excess:e.scrollHeight-e.clientHeight,overflow:getComputedStyle(e).overflowY}};
                return {root:info('.au-pane'),upper:info('.au-upper'),reader:info('.au-reader'),
                    json:info('.au-json-text'),table:info('.au-table'),
                    doc:document.documentElement.scrollHeight-innerHeight};
            }''')
            assert scroll['doc'] <= 1 and scroll['upper']['excess'] <= 1, (width, scroll)
            if width < 850:
                assert scroll['root']['excess'] > 0 and scroll['root']['overflow'] == 'auto', (width, scroll)
                assert scroll['reader']['overflow'] == 'visible', (width, scroll)
                assert scroll['json']['overflow'] == 'visible' and scroll['json']['excess'] <= 1, (width, scroll)
                assert scroll['table']['overflow'] == 'visible', (width, scroll)
            else:
                assert scroll['root']['excess'] <= 1 and scroll['reader']['overflow'] == 'auto', (width, scroll)
        page.get_by_role('combobox', name='Main page').select_option(label='All providers')
        subpages.get_by_role('tab', name='Skills usage', exact=True).click()
        views = page.get_by_role('group', name='Skills view', exact=True)
        for width in (849, 850, 851):
            page.set_viewport_size({'width': width, 'height': 850})
            page.wait_for_function('(w)=>document.querySelector(".au-pane").classList.contains("au-mobile")===(w<850)', arg=width)
            nav_positions = []
            for view in ('Frequency', 'Context footprint', 'Catalogue overhead'):
                views.get_by_role('button', name=view, exact=True).click()
                expect(page.get_by_test_id('skills-usage')).to_be_visible()
                nav_positions.append(page.locator('.au-reader').evaluate('e=>e.getBoundingClientRect().top'))
                if width < 850:
                    nested = page.evaluate('''() => [...document.querySelectorAll('.au-pane *')]
                        .filter(e => e.scrollHeight > e.clientHeight + 1 &&
                            ['auto','scroll'].includes(getComputedStyle(e).overflowY))
                        .map(e => ({className:e.className?.baseVal||e.className,
                            excess:e.scrollHeight-e.clientHeight}))''')
                    assert not nested, (view, nested)
            if width >= 850:
                assert max(nav_positions)-min(nav_positions) < 1, (width, nav_positions)
        # A short but wide route tile has the same single root owner as a narrow one.
        page.get_by_role('combobox', name='Main page').select_option(label='Nous Portal') if page.get_by_role('combobox', name='Main page').is_visible() else providers.get_by_role('tab', name='Nous Portal', exact=True).click()
        subpages.get_by_role('tab', name='Overview', exact=True).click()
        for width, height in ((850, 600), (980, 600), (1920, 600), (850, 700),
                              (849, 749), (850, 749), (851, 749),
                              (849, 750), (850, 750), (851, 750),
                              (849, 751), (850, 751), (851, 751)):
            page.set_viewport_size({'width': width, 'height': height})
            mobile = width < 850 or height < 750
            page.wait_for_function('(m)=>document.querySelector(".au-pane").classList.contains("au-mobile")===m', arg=mobile)
            sample = page.evaluate('''() => {
                const root=document.querySelector('.au-pane'), upper=document.querySelector('.au-upper');
                const reader=document.querySelector('.au-reader'), nav=document.querySelector('.au-subpage-tabs');
                const quality=document.querySelector('.au-quality-line'), chart=document.querySelector('.au-chart svg');
                const plot=document.querySelector('.au-plot-hit');
                const r=e=>e.getBoundingClientRect();
                const scroll=e=>({excess:e.scrollHeight-e.clientHeight, overflow:getComputedStyle(e).overflowY});
                return {size:[r(root).width,r(root).height], root:scroll(root),reader:scroll(reader),
                    upper:scroll(upper),chartBottom:r(chart).bottom,qualityBottom:r(quality).bottom,
                    navTop:r(nav).top,upperBottom:r(upper).bottom,plotHeight:r(plot).height,
                    quotaBottom:r(document.querySelector('[data-testid="provider-limits"]')).bottom,
                    sections:[...upper.querySelectorAll(':scope > *, .au-usage-summary > *, .au-hero > *')].map(e=>({name:e.className?.baseVal||e.className,top:r(e).top,bottom:r(e).bottom,height:r(e).height})),
                    documentExcess:document.documentElement.scrollHeight-innerHeight,
                    nested:[...root.querySelectorAll('*')].filter(e=>e.scrollHeight>e.clientHeight+1 &&
                        ['auto','scroll'].includes(getComputedStyle(e).overflowY))
                        .map(e=>e.className?.baseVal||e.className)};
            }''')
            print(f'SHORT {width}x{height}: {sample}')
            assert sample['size'] == [width, height] and sample['documentExcess'] <= 1, sample
            if mobile:
                assert sample['root']['overflow'] == 'auto' and sample['root']['excess'] > 0, sample
                assert not sample['nested'], sample
            else:
                assert sample['root']['excess'] <= 1 and sample['reader']['overflow'] == 'auto', sample
                assert sample['upper']['excess'] <= 1, sample
                assert sample['quotaBottom'] < sample['chartBottom'] <= sample['upperBottom'] + 1, sample
                assert sample['qualityBottom'] <= sample['navTop'] + 1, sample
                assert sample['plotHeight'] >= 139.5, sample
            if (width, height) in ((850, 600), (980, 600), (1920, 600), (850, 700), (850, 750)):
                if mobile:
                    page.locator('.au-pane').evaluate('e=>e.scrollTop=0')
                page.screenshot(path=str(SHOTS / f'responsive-{width}x{height}.png'))
        page.set_viewport_size({'width': 850, 'height': 750})
        for name in ('All providers', 'Codex', 'Nous Portal', 'Ollama Cloud', 'OpenRouter'):
            providers.get_by_role('tab', name=name, exact=True).click()
            bounds=page.evaluate('''() => {
                const r=s=>document.querySelector(s).getBoundingClientRect();
                return {overflow:document.querySelector('.au-upper').scrollHeight-document.querySelector('.au-upper').clientHeight,
                    quality:r('.au-quality-line').bottom,chart:r('.au-chart svg').bottom,
                    nav:r('.au-subpage-tabs').top,plot:r('.au-plot-hit').height};
            }''')
            print('750 FIT',name,bounds)
            assert bounds['overflow']<=1 and bounds['quality']<=bounds['nav']+1, (name,bounds)
            assert bounds['chart']<=bounds['nav']+1 and bounds['plot']>=139.5, (name,bounds)
        subpages.get_by_role('tab', name='Skills usage', exact=True).click()
        positions=[]
        for name in ('Frequency', 'Context footprint', 'Catalogue overhead'):
            page.get_by_role('group', name='Skills view').get_by_role('button', name=name).click()
            positions.append(page.locator('.au-reader').evaluate('e=>e.getBoundingClientRect().top'))
        assert max(positions)-min(positions)<1, positions
        # Transfer a keyed visible row's viewport offset, not either owner's scrollTop.
        transfer = browser.new_page(viewport={'width': 849, 'height': 850})
        transfer.on('pageerror', lambda error: errors.append(str(error)))
        transfer.route('**/*', lambda route: route.abort())
        transfer.set_content(html)
        transfer.evaluate('''() => {
            const first=events[0];events=[];
            for(let i=0;i<30;i++){
                const e=JSON.parse(JSON.stringify(first));e.id='anchor-'+i;
                e.started=Date.now()/1000-1000+i;e.task='anchor-'+i;events.push(e);
            }
        }''')
        transfer.get_by_role('combobox', name='Main page').select_option(label='All providers')
        transfer.get_by_role('tab', name='Requests', exact=True).click()
        pager = transfer.get_by_test_id('record-pagination')
        for expected in ('Showing 20 of 30 records', 'Showing 30 of 30 records'):
            pager.get_by_role('button', name='Load more').click()
            expect(pager).to_contain_text(expected)
        transfer.locator('.au-pane').evaluate('e=>e.scrollTop=979')
        transfer.wait_for_timeout(80)
        assert transfer.locator('.au-pane').evaluate('e=>e.scrollTop') == 979
        transfer.set_viewport_size({'width': 850, 'height': 850})
        transfer.wait_for_function("!document.querySelector('.au-pane').classList.contains('au-mobile')")
        transfer.set_viewport_size({'width': 849, 'height': 850})
        transfer.wait_for_function("document.querySelector('.au-pane').classList.contains('au-mobile')")
        transfer.wait_for_timeout(80)
        root_back=transfer.locator('.au-pane').evaluate('e=>e.scrollTop')
        print('ROOT979', root_back)
        assert abs(root_back-979)<2
        first = transfer.locator('.au-reader tbody tr[data-scroll-key]').first
        first.locator('.au-record-disclosure').click()
        first.locator('.au-json-details summary').click()
        inspector = first.locator('.au-json-details')
        expect(inspector).to_have_attribute('open', '')
        inspector.evaluate('e=>window.originalInspector=e')
        copy = inspector.get_by_role('button', name='Copy JSON to clipboard')
        copy.focus()
        def anchor(mode):
            return transfer.evaluate('''mode => {
                const root=document.querySelector('.au-pane'),reader=document.querySelector('.au-reader');
                const owner=mode==='root'?root:reader,row=reader.querySelectorAll('tbody tr[data-scroll-key]')[3];
                return {key:row.dataset.scrollKey,offset:row.getBoundingClientRect().top-owner.getBoundingClientRect().top,
                    top:owner.scrollTop,max:owner.scrollHeight-owner.clientHeight,
                    focus:document.activeElement?.getAttribute('aria-label')};
            }''', mode)
        def position(mode):
            transfer.evaluate('''mode => {
                const owner=document.querySelector(mode==='root'?'.au-pane':'.au-reader');
                const row=document.querySelectorAll('.au-reader tbody tr[data-scroll-key]')[3];
                owner.scrollTop+=row.getBoundingClientRect().top-owner.getBoundingClientRect().top-60;
            }''', mode)
            transfer.wait_for_timeout(80)
            result=anchor(mode)
            assert 0 <= result['offset'] < 140 and result['top'] > 0, result
            return result
        position('root')
        for width, height in ((850, 850), (849, 850), (850, 850), (850, 749),
                              (850, 750), (851, 749), (851, 750)):
            oldmode='root' if transfer.locator('.au-pane.au-mobile').count() else 'reader'
            before=anchor(oldmode)
            transfer.set_viewport_size({'width': width, 'height': height})
            newmode='root' if width<850 or height<750 else 'reader'
            transfer.wait_for_function('(m)=>document.querySelector(".au-pane").classList.contains("au-mobile")===(m==="root")', arg=newmode)
            transfer.wait_for_timeout(80)
            after=anchor(newmode)
            print(f'ANCHOR {width}x{height}: {oldmode} {before} -> {newmode} {after}')
            assert after['key']==before['key'], (before,after)
            assert abs(after['offset']-before['offset'])<3 or after['top'] in (0,after['max']), (before,after)
            expect(copy).to_be_focused()
            assert inspector.evaluate('e=>e===window.originalInspector && e.open')
            expect(first.locator('.au-record-disclosure')).to_have_attribute('aria-expanded', 'true')
            position(newmode)
        # The reverse reproduction begins at an actual 250px reader offset.
        transfer.locator('.au-reader').evaluate('e=>e.scrollTop=250')
        transfer.wait_for_timeout(80)
        before = transfer.evaluate('''() => {
            const owner=document.querySelector('.au-reader'),bounds=owner.getBoundingClientRect();
            const row=[...owner.querySelectorAll('tbody tr[data-scroll-key]')].find(e=>e.getBoundingClientRect().bottom>bounds.top);
            return {key:row.dataset.scrollKey,offset:row.getBoundingClientRect().top-bounds.top,top:owner.scrollTop};
        }''')
        assert before['top'] == 250, before
        transfer.set_viewport_size({'width': 849, 'height': 850})
        transfer.wait_for_function("document.querySelector('.au-pane').classList.contains('au-mobile')")
        transfer.wait_for_timeout(80)
        after = transfer.evaluate('''key => {
            const owner=document.querySelector('.au-pane'),row=[...owner.querySelectorAll('[data-scroll-key]')]
                .find(e=>e.dataset.scrollKey===key);
            return {offset:row.getBoundingClientRect().top-owner.getBoundingClientRect().top,top:owner.scrollTop};
        }''', before['key'])
        print('READER250', before, '->', after)
        assert abs(after['offset']-before['offset'])<3 and after['top']>0, (before,after)
        transfer.set_viewport_size({'width': 850, 'height': 850})
        transfer.wait_for_function("!document.querySelector('.au-pane').classList.contains('au-mobile')")
        transfer.wait_for_timeout(80)
        # Delete the actual first visible anchor, not a different row above it.
        deletion=transfer.evaluate('''() => {
            const reader=document.querySelector('.au-reader'),rows=[...reader.querySelectorAll('tbody tr[data-scroll-key]')];
            reader.scrollTop+=rows[3].getBoundingClientRect().top-reader.getBoundingClientRect().top;
            const visible=rows.filter(e=>e.getBoundingClientRect().bottom>reader.getBoundingClientRect().top);
            return {anchor:visible[0].dataset.scrollKey,next:visible[1].dataset.scrollKey,
                offset:visible[1].getBoundingClientRect().top-reader.getBoundingClientRect().top};
        }''')
        transfer.wait_for_timeout(80)
        transfer.locator(f'.au-reader tbody tr[data-scroll-key="{deletion["anchor"]}"]').evaluate('e=>e.remove()')
        transfer.wait_for_timeout(80)
        transfer.set_viewport_size({'width': 849, 'height': 850})
        transfer.wait_for_function("document.querySelector('.au-pane').classList.contains('au-mobile')")
        transfer.wait_for_timeout(80)
        fallback=transfer.evaluate('''key => {
            const root=document.querySelector('.au-pane'),row=[...root.querySelectorAll('[data-scroll-key]')]
                .find(e=>e.dataset.scrollKey===key);
            return {key:row?.dataset.scrollKey,offset:row?.getBoundingClientRect().top-root.getBoundingClientRect().top};
        }''', deletion['next'])
        print('DELETED ANCHOR fallback', deletion, '->', fallback)
        assert fallback['key']==deletion['next'] and abs(fallback['offset']-deletion['offset'])<3, (deletion,fallback)
        assert transfer.locator(f'.au-reader tbody tr[data-scroll-key="{deletion["anchor"]}"]').count()==0
        transfer.close()
        # A clamped mobile position is only retained within its original page/filter.
        for mobile, desktop, change in (((849, 850), (850, 850), 'page'),
                                        ((850, 749), (850, 750), 'filter')):
            scoped=browser.new_page(viewport={'width':mobile[0],'height':mobile[1]})
            scoped.on('pageerror', lambda error: errors.append(str(error)))
            scoped.route('**/*', lambda route: route.abort())
            scoped.set_content(html)
            scoped.evaluate('''() => {
                const first=events[0];events=[];
                for(let i=0;i<30;i++)events.push({...first,id:'scope-'+i,
                    started:Date.now()/1000-1000+i,task:'scope-'+i});
            }''')
            if scoped.get_by_role('combobox', name='Main page').is_visible():
                scoped.get_by_role('combobox', name='Main page').select_option(label='All providers')
            else:
                scoped.get_by_role('tab', name='All providers', exact=True).click()
            scoped.get_by_role('tab', name='Requests', exact=True).click()
            controls=scoped.get_by_test_id('record-pagination')
            for expected in ('Showing 20 of 30 records', 'Showing 30 of 30 records'):
                controls.get_by_role('button', name='Load more').click()
                expect(controls).to_contain_text(expected)
            scoped.locator('.au-pane').evaluate('e=>e.scrollTop=979')
            scoped.wait_for_timeout(80)
            assert scoped.locator('.au-pane').evaluate('e=>e.scrollTop')==979
            scoped.set_viewport_size({'width':desktop[0],'height':desktop[1]})
            scoped.wait_for_function("!document.querySelector('.au-pane').classList.contains('au-mobile')")
            scoped.wait_for_timeout(80)
            assert scoped.locator('.au-reader').evaluate('e=>e.scrollTop')==0
            if change=='page':
                scoped.get_by_role('tab', name='Overview', exact=True).click()
                expect(scoped.get_by_test_id('provider-subpage')).to_have_attribute('data-subpage','Overview')
            else:
                scoped.get_by_role('combobox', name='Agent scope').select_option('primary')
                expect(scoped.get_by_role('combobox', name='Agent scope')).to_have_value('primary')
            scoped.set_viewport_size({'width':mobile[0],'height':mobile[1]})
            scoped.wait_for_function("document.querySelector('.au-pane').classList.contains('au-mobile')")
            scoped.wait_for_timeout(80)
            scroll_state=scoped.locator('.au-pane').evaluate('e=>({top:e.scrollTop,max:e.scrollHeight-e.clientHeight})')
            assert scroll_state['max']>0 and scroll_state['top']==0, (mobile,desktop,change,scroll_state)
            scoped.close()
        # Fresh fixtures, not the number of rendered rows, determine completeness.
        for count in (0, 1, 9, 10, 11):
            check = browser.new_page(viewport={'width': 1500, 'height': 1000})
            check.on('pageerror', lambda error: errors.append(str(error)))
            check.route('**/*', lambda route: route.abort())
            check.set_content(html)
            check.evaluate('''n=>{
                const first=events[0];events=[];
                for(let i=0;i<n;i++){
                    const e=JSON.parse(JSON.stringify(first));
                    e.id='completeness-'+i;e.started=Date.now()/1000-1000+i;e.task='item-'+i;events.push(e);
                }
            }''', count)
            check.get_by_role('tab', name='All providers', exact=True).click()
            check.get_by_role('tab', name='Requests', exact=True).click()
            pager = check.get_by_test_id('record-pagination')
            expect(pager).to_contain_text(f'Showing {min(count, 10)} of {count} records')
            for action in ('Load more', 'View all records'):
                expect(pager.get_by_role('button', name=action, exact=True)).to_have_count(1 if count > 10 else 0)
            if count == 11:
                check.evaluate('''()=>{
                    window.originalPageRest=rest;
                    rest=(path,options)=>{
                        const q=new URL(path,'https://offline').searchParams;
                        if(q.get('list_mode')==='page'&&q.get('offset')==='10')
                            return new Promise((resolve,reject)=>{window.rejectPage=reject});
                        return originalPageRest(path,options);
                    };
                }''')
                pager.get_by_role('button', name='Load more').click()
                expect(pager.get_by_role('button', name='Loading…')).to_be_disabled()
                expect(pager.get_by_role('button', name='View all records')).to_be_disabled()
                check.evaluate("rejectPage(new Error('Synthetic next-page failure'))")
                expect(pager.get_by_role('alert')).to_contain_text('Synthetic next-page failure')
                expect(pager.get_by_role('button', name='Load more')).to_be_enabled()
                expect(pager.get_by_role('button', name='View all records')).to_be_enabled()
                check.evaluate('rest=originalPageRest')
                pager.get_by_role('button', name='Load more').click()
                expect(pager).to_contain_text('Showing 11 of 11 records')
                expect(pager.get_by_role('button', name='Load more')).to_have_count(0)
                expect(pager.get_by_role('button', name='View all records')).to_have_count(0)
                # Complete pagination does not prevent a later changed selection being paged.
                check.evaluate('''()=>{const e={...events[0],id:'new-record',started:Date.now()/1000-10};events.push(e);
                    demoChange++;for(const fn of demoSubscribers)fn({type:'changed',mode:'native-events'})}''')
                expect(pager).to_contain_text('Showing 10 of 12 records')
                pager.get_by_role('button', name='View all records').click()
                expect(pager).to_contain_text('Showing 12 of 12 records')
                expect(pager.get_by_role('button', name='Exit frozen view')).to_be_visible()
                expect(pager.get_by_role('button', name='Load more')).to_have_count(0)
                expect(pager.get_by_role('button', name='View all records')).to_have_count(0)
                pager.get_by_role('button', name='Exit frozen view').click()
                expect(pager).to_contain_text('Showing 10 of 12 records')
            check.close()
        assert not errors, errors
        browser.close()
    print('PASS width/height-coupled 850×750 split, short single root, keyed transfer/fallback, quota/Skills fit and conditional paging')


if __name__ == '__main__':
    run()
