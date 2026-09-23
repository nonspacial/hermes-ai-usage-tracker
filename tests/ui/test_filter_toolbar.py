"""Offline split-pane regression: filter selectors shrink before wrapping."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1600, 'height': 1200}, locale='en-GB')
        page.route('**/*', lambda route: route.abort())
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_content((ROOT / 'preview.html').read_text())
        # Fixture-only saved window and Desktop-like native select widths. The
        # real screenshot has 16px pane padding and ~992px usable toolbar width.
        page.evaluate('''() => {
            savedTests.push({id:'fixture-test',label:'A saved test window',
                             started:Date.now()/1000-900,ended:Date.now()/1000});
            const base=events.find(e=>e.provider==='openai-codex');
            const second={...base,id:'fixture-second-model',model:'gpt-6-sol',
                          response_model:'gpt-6-sol',started:Date.now()/1000-400,
                          ended:Date.now()/1000-399};
            events.push(second);
            queryClient.invalidateQueries();
        }''')
        page.get_by_role('navigation', name='Providers', exact=True).get_by_role('tab', name='Codex', exact=True).click()
        page.add_style_tag(content='''.p-4{padding:16px}
            .au-filters select[aria-label="Project"]{width:180px}
            .au-filters select[aria-label="Saved tests"]{width:247px}''')
        # Keep the actual controls/data path; only the host's native option
        # widths are represented by fixture CSS above, not production overrides.
        samples = []
        for width in (390, 600, 620, 650, 651, 652, 653, 654, 660, 680, 700, 750, 790, 800, 820, 840, 900, 950, 970, 980, 990, 1000, 1012, 1013, 1014, 1030, 1039, 1050, 1080, 1200):
            page.locator('#root').evaluate('''(e,w) => {
                Object.assign(e.style,{maxWidth:'none',flex:'none',width:w+'px',
                                       height:'1029px',margin:'0',padding:'0'});
            }''', width)
            page.evaluate('() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))')
            sample = page.evaluate('''() => {
                const q=s=>document.querySelector(s), filter=q('.au-filters'),
                    children=[...filter.children], upper=q('.au-upper'),
                    cards=q('.au-totals'), summary=q('.au-usage-summary'),
                    nav=q('.au-subpage-tabs'), reader=q('.au-reader');
                const box=e=>e.getBoundingClientRect();
                const saved=q('select[aria-label="Saved tests"]'),
                    project=q('select[aria-label="Project"]');
                return {filterWidth:box(filter).width,filterHeight:box(filter).height,
                    rows:new Set(children.map(e=>Math.round((box(e).top-box(filter).top)/20))).size,
                    savedWidth:box(saved).width,projectWidth:box(project).width,
                    savedRight:box(saved).right,filterRight:box(filter).right,
                    cardHeight:box(cards).height,cardCount:cards.children.length,
                    cardRows:new Set([...cards.children].map(e=>Math.round(box(e).top))).size,
                    cardColumns:getComputedStyle(cards).gridTemplateColumns.split(' ').length,
                    upperOverflow:upper.scrollHeight-upper.clientHeight,
                    summaryBottom:box(summary).bottom,upperBottom:box(upper).bottom,
                    navHeight:box(nav).height,readerHeight:reader.clientHeight};
            }''')
            if width == 1039:
                print('Fixture content:', page.evaluate('''() => ({
                    provider:document.querySelector('.au-provider-pane').dataset.provider,
                    mode:document.querySelector('.au-mode-buttons [aria-pressed="true"]')?.textContent,
                    period:document.querySelector('.au-period-buttons [aria-pressed="true"]')?.textContent,
                    grouping:document.querySelector('[aria-label="Breakdown grouping"] [aria-pressed="true"]')?.textContent,
                    rows:document.querySelectorAll('.au-reader tbody tr').length,
                    diagnosticsOpen:document.querySelector('[data-testid="usage-diagnostics"]').open,
                    quota:!!document.querySelector('.au-quota-card')})'''))
            samples.append((width, sample))
            assert sample['cardCount'] == 7, sample
            assert sample['savedRight'] <= sample['filterRight'] + .1, (width, sample)
            assert sample['readerHeight'] >= 120, (width, sample)
            if width in (1030, 1039, 1050, 1080):
                assert sample['cardRows'] == 1 and sample['cardColumns'] == 7, (width, sample)
                assert sample['filterHeight'] <= 32, (width, sample)
            if sample['cardColumns'] >= 3:
                assert sample['rows'] == 1, (width, sample)
            if width == 652:
                assert sample['cardColumns'] == 2 and sample['rows'] == 2, sample
            if width == 653:
                assert sample['cardColumns'] == 3 and sample['rows'] == 1, sample
            if width == 390:
                assert sample['rows'] > 1 and sample['upperOverflow'] > 0, sample
        assert next(v for w,v in samples if w == 1012)['cardColumns'] == 3
        assert next(v for w,v in samples if w == 1013)['cardColumns'] == 7
        print('Card/toolbar breakpoint measurements:', [(w, v['filterWidth'], v['cardColumns'], v['rows']) for w,v in samples])
        page.locator('#root').evaluate('(e)=>e.style.width="654px"')
        print('Boundary control widths:', page.evaluate('''() => [...document.querySelectorAll('.au-filters>*')].map(e=>({label:e.getAttribute('aria-label')||e.textContent.trim(),width:e.getBoundingClientRect().width,min:getComputedStyle(e).minWidth,flex:getComputedStyle(e).flex}))'''))
        # Within the same seven-card, one-row and unchanged text-wrap mode,
        # widening the pane must not inflate the card row or bring back a filter wrap.
        assert len({round(v['cardHeight'], 2) for w,v in samples if w in (1030,1039,1050,1080)}) == 1, samples
        screenshot_case = next(v for w,v in samples if w == 1039)
        legacy = page.add_style_tag(content='''.au-ledger .au-filters select{
            flex:0 1 auto!important;min-width:auto!important;max-width:100%!important}''')
        page.locator('#root').evaluate('(e)=>e.style.width="1039px"')
        page.evaluate('() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))')
        legacy_case = page.evaluate('''() => {
            const f=document.querySelector('.au-filters'),u=document.querySelector('.au-upper');
            return {height:f.getBoundingClientRect().height,overflow:u.scrollHeight-u.clientHeight};
        }''')
        legacy.evaluate('(e)=>e.remove()')
        assert legacy_case['height'] >= screenshot_case['filterHeight']+35, (legacy_case,screenshot_case)
        assert legacy_case['overflow'] >= screenshot_case['upperOverflow']+30, (legacy_case,screenshot_case)
        # Verify what a person can read and operate, not only whether the
        # toolbar occupies one row. The canvas uses each *computed* select font;
        # reserve space for Chromium's native arrow and control padding.
        def usable_controls(width, labels):
            page.locator('#root').evaluate('(e,w)=>e.style.width=w+"px"', width)
            page.evaluate('() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))')
            result = page.evaluate('''labels => {
                const filter=document.querySelector('.au-filters');
                const controls=[...filter.querySelectorAll('button,input,select')]
                    .filter(e=>e.getClientRects().length);
                const rect=e=>e.getBoundingClientRect();
                const canvas=document.createElement('canvas'), ctx=canvas.getContext('2d');
                const selected=Object.fromEntries(Object.entries(labels).map(([name,prefix])=>{
                    const e=filter.querySelector('select[aria-label="'+name+'"]');
                    const style=getComputedStyle(e), box=rect(e);
                    ctx.font=style.font;
                    const text=e.selectedOptions[0]?.textContent||'';
                    const space=box.width-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight)
                        -parseFloat(style.borderLeftWidth)-parseFloat(style.borderRightWidth)-23;
                    return [name,{text,prefix,space,needed:ctx.measureText(prefix).width,
                        width:box.width,native:e.tagName==='SELECT',options:e.options.length,
                        focusable:e.tabIndex>=0,visibility:style.visibility}];
                }));
                const session=filter.querySelector('input[aria-label="Session ID"]');
                const sessionStyle=getComputedStyle(session), sessionBox=rect(session);
                ctx.font=sessionStyle.font;
                const sessionSpace=sessionBox.width-parseFloat(sessionStyle.paddingLeft)
                    -parseFloat(sessionStyle.paddingRight)-parseFloat(sessionStyle.borderLeftWidth)
                    -parseFloat(sessionStyle.borderRightWidth);
                const clipped=controls.filter(e=>{
                    const b=rect(e), f=rect(filter);
                    return b.width<1||b.height<1||b.left<f.left-.5||b.right>f.right+.5;
                }).map(e=>e.getAttribute('aria-label')||e.textContent.trim());
                const obscured=controls.filter(e=>{
                    const b=rect(e), x=b.left+b.width/2, y=b.top+b.height/2;
                    return !controls.every(other=>other===e||!((r)=>x>=r.left&&x<r.right&&y>=r.top&&y<r.bottom)(rect(other)));
                }).map(e=>e.getAttribute('aria-label')||e.textContent.trim());
                return {selected,clipped,obscured,rows:new Set(controls.map(e=>Math.round(rect(e).top))).size,
                    session:{space:sessionSpace,
                    needed:ctx.measureText('Session ID').width,
                    placeholder:session.placeholder},buttons:controls.filter(e=>e.tagName==='BUTTON')
                    .map(e=>({label:e.textContent.trim(),disabled:e.disabled}))};
            }''', labels)
            assert not result['clipped'] and not result['obscured'], (width, result)
            assert all(not b['disabled'] for b in result['buttons']), (width, result)
            assert {'Export request CSV'} <= {b['label'] for b in result['buttons']}, (width, result)
            assert {'Start test marker', 'End test marker'} & {b['label'] for b in result['buttons']}, (width, result)
            for name, item in result['selected'].items():
                assert item['native'] and item['focusable'] and item['visibility'] == 'visible', (width, name, item)
                assert item['options'] >= 2 and item['text'].startswith(item['prefix']), (width, name, item)
                assert item['space'] >= item['needed'] + 2, (width, name, item)
            assert result['session']['space'] >= result['session']['needed'], (width, result['session'])
            return result

        for width in (1039, 840, 653, 652, 390, 320):
            usable_controls(width, {'Agent scope':'All agents', 'Project':'All projects',
                                    'Saved tests':'Saved test windows' if width == 1039 else 'Saved test'})
        page.locator('#root').evaluate('(e)=>e.style.width="1039px"')
        project = page.get_by_role('combobox', name='Project', exact=True)
        saved = page.get_by_role('combobox', name='Saved tests', exact=True)
        # Native keyboard navigation, not select_option(), must change the real
        # React filter and the fixture's downstream ledger request.
        first_project = project.locator('option').nth(1).get_attribute('value')
        assert first_project and first_project != project.input_value()
        project.focus()
        assert project.evaluate('(e)=>document.activeElement===e')
        project.press('ArrowDown')
        project.press('Enter')
        expect(project).to_have_value(first_project)
        first_project_label = project.evaluate('(e)=>e.selectedOptions[0].textContent')
        filtered = usable_controls(653, {'Agent scope':'All agents', 'Project':first_project_label[:6],
                                        'Saved tests':'Saved test'})
        # Additional controls change the width budget: a selected project adds
        # Clear filters; selecting a saved window adds two native date inputs.
        # Record these as unresolved three-column cases, not false full-fit passes.
        assert filtered['rows'] > 1, filtered
        print('Project-selected three-column toolbar rows:', filtered['rows'])
        page.wait_for_function('''id => {
            const calls=demoCalls.filter(c=>c.startsWith('/ledger?'));
            return new URL(calls.at(-1),'https://offline.test').searchParams.get('project')===id;
        }''', arg=first_project)
        project.press('Home')
        project.press('Enter')
        expect(project).to_have_value('')
        page.wait_for_function('''() => {
            const calls=demoCalls.filter(c=>c.startsWith('/ledger?'));
            return !new URL(calls.at(-1),'https://offline.test').searchParams.get('project');
        }''')
        saved.focus()
        assert saved.evaluate('(e)=>document.activeElement===e')
        saved.press('ArrowDown')
        saved.press('Enter')
        expect(page.get_by_role('combobox', name='Time window', exact=True)).to_have_value('custom')
        expect(page.get_by_role('textbox', name='Window start')).not_to_have_value('')
        page.wait_for_function('''() => {
            const calls=demoCalls.filter(c=>c.startsWith('/ledger?'));
            return new URL(calls.at(-1),'https://offline.test').searchParams.get('test_id')==='fixture-test';
        }''')
        for width in (1039, 840, 653, 652, 390, 320):
            usable_controls(width, {'Agent scope':'All agents', 'Project':'All projects',
                                    'Saved tests':'Saved test windows' if width == 1039 else 'Saved test'})
        page.locator('#root').evaluate('(e)=>e.style.width="653px"')
        custom = page.evaluate('''() => {
            const f=document.querySelector('.au-filters'), children=[...f.children];
            return {filterWidth:f.getBoundingClientRect().width,rows:new Set(children.map(e=>Math.round(e.getBoundingClientRect().top))).size,
                widths:children.map(e=>[e.getAttribute('aria-label')||e.textContent.trim(),Math.round(e.getBoundingClientRect().width)])};
        }''')
        assert custom['rows'] > 1 and all(w >= 180 for _,w in custom['widths'][:2]), custom
        print('Custom-window three-column toolbar:', custom)
        # The host screenshots are not a DOM trace: the fixture may retain a
        # small allocation mismatch at this height; do not conceal it by
        # enlarging the cap or claiming live visual acceptance.
        assert not errors, errors
        print('Screenshot-width baseline/current toolbar and upper overflow:',
              legacy_case, {'height':screenshot_case['filterHeight'],
                            'overflow':screenshot_case['upperOverflow']})
        print('PASS filter toolbar screenshot-width and neighbours:',
              [(w, v['filterWidth'],v['rows'],round(v['savedWidth']),
                v['upperOverflow'],round(v['cardHeight'],2)) for w,v in samples])
        browser.close()


if __name__ == '__main__':
    run()
