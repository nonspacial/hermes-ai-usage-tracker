"""Offline check: All providers summary rows show compact, provider-coloured quota bars.

Synthetic preview only; no account, quota endpoint or ledger is read.
"""
import os
from pathlib import Path
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SCRATCH = Path(os.environ.get('HERMES_SCRATCH', os.environ.get('TMPDIR', '/tmp')))
CHROME = os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium')


def fixture(codex_windows, anthropic_sonnet=False):
    # Anthropic windows mirror the host OAuth mapping serialised by
    # dashboard/plugin_api.py: five_hour -> 'Current session', seven_day -> 'Current week'
    # (seven_day_sonnet -> 'Sonnet week' for the real-shaped three-window account).
    sonnet = (",\n {label:'Sonnet week',used_percent:20,remaining_percent:80,reset_at:new Date((now+4*86400)*1000).toISOString(),detail:null}"
              if anthropic_sonnet else '')
    extra = """
{const event=JSON.parse(JSON.stringify(events[0]));event.provider='anthropic';
 event.id='row-quota-anthropic';event.started=now-900;event.ended=now-899;events.push(event);}
{const event=JSON.parse(JSON.stringify(events[0]));event.provider='openrouter';
 event.id='row-quota-openrouter';event.started=now-800;event.ended=now-799;events.push(event);}
demoProviders.push({id:'anthropic',label:'Anthropic',configured:true,quota:{available:true,source:'oauth_usage_api',
 title:'Account limits',plan:'Max',details:[],windows:[
 {label:'Current session',used_percent:38,remaining_percent:62,reset_at:new Date((now+3600)*1000).toISOString(),detail:null},
 {label:'Current week',used_percent:45,remaining_percent:55,reset_at:new Date((now+4*86400)*1000).toISOString(),detail:null}%s]}});
demoProviders[0].quota.windows=%s;
""" % (sonnet, codex_windows)
    html = (ROOT / 'preview.html').read_text()
    assert 'const demoContexts={' in html
    return html.replace('const demoContexts={', extra + 'const demoContexts={', 1)


CODEX_ONE = "[{label:'Weekly',used_percent:1,remaining_percent:99,reset_at:new Date((now+6*86400)*1000).toISOString()}]"
CODEX_TWO = ("[{label:'Session',used_percent:30,remaining_percent:70,reset_at:new Date((now+3600)*1000).toISOString()},"
             "{label:'Weekly',used_percent:1,remaining_percent:99,reset_at:new Date((now+6*86400)*1000).toISOString()}]")


def rows(page):
    return page.evaluate('''() => {
      const probe=document.createElement('i');document.querySelector('.au-ledger').append(probe);
      const colour=css=>{probe.style.color=css;return getComputedStyle(probe).color};
      const out={};
      for(const row of document.querySelectorAll('.au-provider-row')){
        const id=row.dataset.provider, block=row.querySelector('[data-testid="provider-row-quota"]');
        const windows=[...row.querySelectorAll('.au-row-quota-window')].map(w=>{
          const label=w.querySelector('.au-row-quota-label'),track=w.querySelector('.au-row-quota-track'),
            fill=w.querySelector('.au-row-quota-fill');
          return {label:label.textContent,children:w.children.length,labelBottom:label.getBoundingClientRect().bottom,
            trackTop:track.getBoundingClientRect().top,fill:getComputedStyle(fill).backgroundColor,
            width:fill.style.width,value:track.getAttribute('aria-valuenow')};
        });
        const dot=getComputedStyle(row.querySelector('.au-dot')).backgroundColor;
        const text=[...row.children].filter(c=>c!==block).map(c=>c.getBoundingClientRect());
        const textBottom=Math.max(...text.map(r=>r.bottom));
        const s=block?getComputedStyle(block):null;
        out[id]={windows,dot,expected:colour(providerAccent(id)),
          blockTop:block?block.getBoundingClientRect().top:null,textBottom,last:row.lastElementChild===block,
          chrome:s?[s.borderTopWidth,s.backgroundColor,s.padding,s.boxShadow]:null,
          blockText:block?block.textContent:'',
          badge:!!block?.querySelector('.au-provider-type-badge,.au-quota-card,button,[class*="reset"]')};
      }
      probe.remove();return out;
    }''')


def geometry(page):
    return page.evaluate('''() => {
      const r=s=>{const e=document.querySelector(s);return e?e.getBoundingClientRect():null};
      const pane=document.querySelector('.au-pane');
      const hero=r('.au-hero-total'),chart=r('.au-chart svg'),plot=r('.au-plot-hit'),upper=r('.au-upper'),
        nav=r('.au-subpage-tabs'),quality=r('.au-quality-line'),reader=r('.au-reader');
      const blocks=[...document.querySelectorAll('[data-testid="provider-row-quota"]')].map(b=>b.getBoundingClientRect());
      const rows=[...document.querySelectorAll('.au-provider-row')].map(e=>e.getBoundingClientRect());
      return {mobile:pane.classList.contains('au-mobile'),hero:{left:hero.left,right:hero.right,bottom:hero.bottom},
        chart:{left:chart.left,top:chart.top,bottom:chart.bottom},plot:plot.height,upper:upper.bottom,nav:nav.top,
        navBottom:nav.bottom,quality:quality.bottom,readerTop:reader.top,
        blocks:blocks.map(b=>({left:b.left,right:b.right,top:b.top,bottom:b.bottom,width:b.width})),
        rows:rows.map(b=>({left:b.left,right:b.right,top:b.top,bottom:b.bottom})),
        overflowX:document.querySelector('.au-hero-total').scrollWidth-document.querySelector('.au-hero-total').clientWidth};
    }''')


def bars_hidden(page):
    page.evaluate("()=>{const s=document.createElement('style');s.id='au-test-hide-row-quota';"
                  "s.textContent='.au-row-quota{display:none!important}';document.head.append(s)}")
    page.wait_for_timeout(80)
    try:
        return geometry(page)
    finally:
        page.evaluate("document.getElementById('au-test-hide-row-quota').remove()")
        page.wait_for_timeout(80)


SIZES = ((850, 750), (850, 850), (980, 850), (1300, 850), (1920, 1080), (3840, 2160), (390, 850))
# Fixed pane-size breakpoints: the split needs width >= 850 and height >= the
# width band's minimum (16px insets + upper floor + 50px nav + 160px reader).
# Each pair is immediately below / at the band's height breakpoint.
BOUNDARIES = ((850, 924), (850, 925), (932, 924), (932, 925), (933, 902), (933, 903),
              (971, 902), (971, 903), (972, 893), (972, 894), (1467, 893), (1467, 894),
              (1468, 788), (1468, 789), (1920, 788), (1920, 789), (849, 925))


def split_mode(width, height):
    content = width - 32
    need = 893 if content <= 900 else 871 if content < 940 else 862 if content < 1436 else 757
    return width >= 850 and height - 32 >= need


PAGES = ('All providers', 'Codex', 'Nous Portal', 'Ollama Cloud', 'OpenRouter', 'Anthropic')
SUBPAGES = ('Overview', 'Requests', 'Cache & costs', 'Compressions', 'Models & tasks', 'Skills usage')
FIT = '''() => {
  const q=s=>document.querySelector(s), r=e=>e.getBoundingClientRect();
  const pane=q('.au-pane'), upper=q('.au-upper'), nav=q('.au-subpage-tabs'), reader=q('.au-reader');
  // Ordinary upper content only: closed disclosures/overlays are not layout content.
  const skip='.au-reset-tooltip,.au-custom-panel,.au-filter-action-panel,details:not([open])>:not(summary)';
  const visible=root=>[...root.querySelectorAll('*')].filter(e=>r(e).height>0&&!e.closest(skip));
  const content=Math.max(...visible(upper).map(e=>r(e).bottom));
  const scroll=e=>e.scrollHeight-e.clientHeight;
  return {mobile:pane.classList.contains('au-mobile'),upper:r(upper).bottom,upperHeight:r(upper).height,
    nav:r(nav).top,navBottom:r(nav).bottom,readerHeight:r(reader).height,content,
    quality:r(q('.au-quality-line')).bottom,chart:r(q('.au-chart svg')).bottom,chartTop:r(q('.au-chart svg')).top,
    plot:r(q('.au-plot-hit')).height,hero:r(q('.au-hero-total')).bottom,
    rootExcess:scroll(pane),upperExcess:scroll(upper),rootOverflow:getComputedStyle(pane).overflowY,
    upperOverflow:getComputedStyle(upper).overflowY,readerOverflow:getComputedStyle(reader).overflowY,
    readerExcess:scroll(reader),docExcess:document.documentElement.scrollHeight-innerHeight,
    nested:[...pane.querySelectorAll('*')].filter(e=>scroll(e)>1&&['auto','scroll'].includes(getComputedStyle(e).overflowY)).length,
    bars:document.querySelectorAll('[data-testid=\\\"provider-row-quota\\\"]').length};
}'''
# Single-scroller reachability: scroll the root to its end, then the last
# visible reader content must lie inside the pane and after navigation.
REACH = '''() => {
  const pane=document.querySelector('.au-pane'), reader=document.querySelector('.au-reader');
  const nav=document.querySelector('.au-subpage-tabs'), r=e=>e.getBoundingClientRect();
  pane.scrollTop=pane.scrollHeight;
  // Exclude non-layout content: clip-path screen-reader-only subtrees (record-mode
  // thead) and closed <details> bodies, which Chromium hides with content-visibility
  // but still reports stale rectangles for.
  const hidden=e=>{if(e.closest('details:not([open])')&&!e.closest('summary'))return true;
    for(let n=e;n&&n!==reader;n=n.parentElement)if(getComputedStyle(n).clipPath!=='none')return true;return false};
  const items=[...reader.querySelectorAll('*')].filter(e=>r(e).height>0&&!hidden(e));
  const last=Math.max(r(nav).bottom,...items.map(e=>r(e).bottom));
  const out={max:pane.scrollHeight-pane.clientHeight,top:pane.scrollTop,last,paneBottom:r(pane).bottom,
    navBottom:r(nav).bottom,readerTop:r(reader).top};
  pane.scrollTop=0;return out;
}'''


def check_mode(g, key, width, height):
    assert g['docExcess'] <= 1, (key, g)
    if g['mobile']:
        # Single-scroller policy: the plugin root alone scrolls; upper and reader
        # flow in it without nested vertical scrollbars or clipping.
        assert g['rootOverflow'] == 'auto' and g['nested'] == 0, (key, g)
        assert g['upperOverflow'] == 'visible' and g['readerOverflow'] == 'visible', (key, g)
        assert g['upperExcess'] <= 1 and g['readerExcess'] <= 1, (key, g)
        assert g['content'] <= g['nav'] + 1 and g['quality'] <= g['nav'] + 1, (key, g)
        assert g['hero'] <= g['chartTop'] + 1 and g['plot'] >= 139.5, (key, g)
        return
    assert g['rootExcess'] <= 1 and g['upperExcess'] <= 1, (key, g)
    assert g['content'] <= g['upper'] + 1 and g['quality'] <= g['nav'] + 1, (key, g)
    assert g['chart'] <= g['nav'] + 1 and g['plot'] >= 139.5, (key, g)
    # Lower navigation stays fully visible above a usable >=160px reader.
    assert g['readerHeight'] >= 159.5 and g['navBottom'] <= height - 16 - 159.5, (key, g)


def fit_matrix(browser):
    # Real-shaped worst fixture-known catalogue: five providers, Codex two windows,
    # Anthropic three windows, Nous details, two no-quota providers.
    page = browser.new_page(viewport={'width': 1920, 'height': 1080})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.route('**/*', lambda route: route.abort())
    open_all(page, fixture(CODEX_TWO, anthropic_sonnet=True))
    providers = page.get_by_role('navigation', name='Providers', exact=True)
    subpages = page.get_by_role('navigation', name='Provider subpages', exact=True)
    report = {}
    for width, height in SIZES + BOUNDARIES:
        split = split_mode(width, height)
        page.set_viewport_size({'width': width, 'height': height})
        page.wait_for_function('(m)=>document.querySelector(".au-pane").classList.contains("au-mobile")===m',
                               arg=not split)
        dividers = {}
        for name in PAGES:
            if providers.get_by_role('tab', name=name, exact=True).is_visible():
                providers.get_by_role('tab', name=name, exact=True).click()
            else:
                page.get_by_role('combobox', name='Main page').select_option(label=name)
            for sub in (SUBPAGES if name == 'All providers' else ('Overview',)):
                subpages.get_by_role('tab', name=sub, exact=True).click()
                expect(page.get_by_test_id('provider-subpage')).to_have_attribute('data-subpage', sub)
                page.wait_for_timeout(60)
                g = page.evaluate(FIT)
                key = (width, height, name, sub)
                # The mode is a function of pane size only, identical for every page.
                assert g['mobile'] == (not split), (key, g)
                if name == 'All providers':
                    assert g['bars'] == 2, (key, g)
                check_mode(g, key, width, height)
                if g['mobile']:
                    if name == 'All providers' and sub in ('Requests', 'Skills usage'):
                        # Lower content is reached with the one page scrollbar.
                        reach = page.evaluate(REACH)
                        assert reach['max'] > 0 and abs(reach['top'] - reach['max']) <= 1, (key, reach)
                        assert reach['navBottom'] <= reach['readerTop'] + 1, (key, reach)
                        assert reach['last'] <= reach['paneBottom'] + 1, (key, reach)
                    report[(width, height)] = 'single scroller'
                    continue
                dividers[(name, sub)] = (g['upper'], g['nav'])
                report[(width, height)] = (round(g['upperHeight'], 1), round(g['readerHeight'], 1))
            subpages.get_by_role('tab', name='Overview', exact=True).click()
        if dividers:
            uppers = [v[0] for v in dividers.values()]
            navs = [v[1] for v in dividers.values()]
            # Divider is a function of pane size only: identical across pages and subpages.
            assert len(dividers) == 11, (width, height, dividers)
            assert max(uppers) - min(uppers) < 0.5 and max(navs) - min(navs) < 0.5, (width, height, dividers)
        if (width, height) in ((850, 750), (980, 850), (1920, 1080)):
            # In the single scroller the stable root gutter can take the content box
            # below the 818px tab query, where the existing Main page select replaces tabs.
            if providers.get_by_role('tab', name='All providers', exact=True).is_visible():
                providers.get_by_role('tab', name='All providers', exact=True).click()
            else:
                page.get_by_role('combobox', name='Main page').select_option(label='All providers')
            expect(page.get_by_test_id('provider-subpage')).to_have_attribute('data-subpage', 'Overview')
            expect(page.locator('[data-testid="provider-row-quota"]')).to_have_count(2)
            page.locator('.au-pane').evaluate('e=>e.scrollTop=0')
            # Park the pointer in the pane's bottom-right inset so no hover tooltip is captured.
            page.mouse.move(width - 4, height - 4)
            page.wait_for_timeout(120)
            page.screenshot(path=str(SCRATCH / f'row-quota-fit-{width}x{height}.png'))
            if not split_mode(width, height):
                # The same single scroller reaches lower navigation and records.
                page.locator('.au-pane').evaluate('''e=>{const nav=e.querySelector('.au-subpage-tabs');
                  e.scrollTop+=nav.getBoundingClientRect().top-e.getBoundingClientRect().top-40}''')
                page.wait_for_timeout(120)
                page.screenshot(path=str(SCRATCH / f'row-quota-fit-{width}x{height}-lower.png'))
                page.locator('.au-pane').evaluate('e=>e.scrollTop=0')
    assert not errors, errors
    page.close()
    return report


def default_catalogue_fit(browser):
    # Unmodified preview catalogue (Codex one weekly window) at the 850x925 split boundary.
    page = browser.new_page(viewport={'width': 850, 'height': 925})
    page.route('**/*', lambda route: route.abort())
    page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
    page.get_by_role('navigation', name='Providers', exact=True).get_by_role('tab', name='All providers').click()
    expect(page.locator('[data-testid="provider-row-quota"]')).to_have_count(1)
    page.wait_for_function('()=>!document.querySelector(".au-pane").classList.contains("au-mobile")')
    g = geometry(page)
    assert max(g['hero']['bottom'], g['quality']) <= g['upper'] + 1, g
    page.close()


def open_all(page, html):
    page.set_content(html, wait_until='domcontentloaded')
    page.get_by_role('navigation', name='Providers', exact=True).get_by_role('tab', name='All providers').click()
    expect(page.locator('.au-provider-row[data-provider="anthropic"]')).to_be_visible()


def test_row_quota():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROME, headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            for variant, codex, expected_codex in (('codex1', CODEX_ONE, ['Weekly']), ('codex2', CODEX_TWO, ['Five hour', 'Weekly'])):
                page = browser.new_page(viewport={'width': 1920, 'height': 1080})
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.route('**/*', lambda route: route.abort())
                open_all(page, fixture(codex))
                page.evaluate('window.demoCalls.length=0')
                state = rows(page)
                assert [w['label'] for w in state['anthropic']['windows']] == ['Five hour', 'Weekly'], state['anthropic']
                assert [w['label'] for w in state['openai-codex']['windows']] == expected_codex, state['openai-codex']
                assert state['openrouter']['windows'] == [] and state['openrouter']['blockTop'] is None, state['openrouter']
                for id in ('anthropic', 'openai-codex'):
                    row = state[id]
                    assert row['last'] and row['blockTop'] >= row['textBottom'] - 0.5, (id, row)
                    assert not row['badge'] and row['chrome'][0] == '0px' and row['chrome'][1] == 'rgba(0, 0, 0, 0)', (id, row)
                    assert row['chrome'][3] == 'none' and row['blockText'] == ''.join(w['label'] for w in row['windows']), (id, row)
                    for w in row['windows']:
                        assert w['children'] == 2 and w['labelBottom'] <= w['trackTop'] + 0.5, (id, w)
                        assert w['fill'] == row['dot'] == row['expected'], (id, w, row['dot'])
                assert state['anthropic']['windows'][0]['width'] == '62%' and state['anthropic']['windows'][1]['width'] == '55%'
                assert state['anthropic']['dot'] != state['openai-codex']['dot']
                # Codex keeps the host accent on its fills.
                assert state['openai-codex']['windows'][0]['fill'] == page.evaluate('''() => {const i=document.createElement('i');
                  i.style.color='var(--ui-accent)';document.querySelector('.au-ledger').append(i);const c=getComputedStyle(i).color;i.remove();return c}''')
                for width, height in ((1920, 1080), (850, 925), (850, 850), (850, 750), (390, 850)):
                    page.set_viewport_size({'width': width, 'height': height})
                    page.wait_for_function('(m)=>document.querySelector(".au-pane").classList.contains("au-mobile")===m',
                                           arg=not split_mode(width, height))
                    page.wait_for_timeout(150)
                    g = geometry(page)
                    base = bars_hidden(page)
                    # In fixed-split mode the divider (upper/lower split) and subpage tabs never move
                    # for the bars; the single scroller (narrow or short pane) flows by design.
                    if not g['mobile']:
                        assert abs(g['upper'] - base['upper']) <= 0.5 and abs(g['nav'] - base['nav']) <= 0.5, (variant, width, g, base)
                    else:
                        assert g['quality'] <= g['nav'] + 1 and g['hero']['bottom'] <= g['chart']['top'] + 1, (variant, width, g)
                    assert g['overflowX'] <= 1, (variant, width, g)
                    for b in g['blocks']:
                        assert b['width'] > 20 and b['left'] >= g['hero']['left'] - 1 and b['right'] <= g['hero']['right'] + 1, (variant, width, g)
                    if not g['mobile']:
                        assert all(b['right'] <= g['chart']['left'] + 1 for b in g['blocks']), (variant, width, g)
                        # The approved upper floor contains summary, bars and quality line.
                        assert max(g['hero']['bottom'], g['quality']) <= g['upper'] + 1, (variant, width, g)
                        assert g['chart']['bottom'] <= g['nav'] + 1 and g['plot'] >= 139.5, (variant, width, g)
                    else:
                        assert all(b['bottom'] <= g['chart']['top'] + 1 for b in g['blocks']), (variant, width, g)
                    for a, b in zip(g['rows'], g['rows'][1:]):
                        if a['left'] == b['left']:
                            assert a['bottom'] <= b['top'] + 1, (variant, width, g['rows'])
                    page.screenshot(path=str(SCRATCH / f'row-quota-{variant}-{width}x{height}.png'))
                    if width == 1920:
                        page.locator('.au-hero-total').screenshot(path=str(SCRATCH / f'row-quota-{variant}-summary-1920.png'))
                # Rendering and resizing reuse the loaded catalogue: no per-provider quota calls.
                assert not [c for c in page.evaluate('window.demoCalls') if 'usage' in c and 'ledger' not in c], page.evaluate('window.demoCalls')
                if variant == 'codex2':
                    page.set_viewport_size({'width': 1920, 'height': 1080})
                    # Subscriptions and individual Anthropic card keep provider colour on both bars.
                    nav = page.get_by_role('navigation', name='Providers', exact=True)
                    nav.get_by_role('tab', name='Subscriptions').click()
                    card = page.locator('.au-quota-home .au-quota-card').filter(has_text='Anthropic')
                    fills = card.locator('.au-quota-rows .h-full').evaluate_all('els=>els.map(e=>getComputedStyle(e).backgroundColor)')
                    assert fills == [state['anthropic']['expected']] * 2, fills
                    card.screenshot(path=str(SCRATCH / 'row-quota-anthropic-subscriptions-card.png'))
                    nav.get_by_role('tab', name='Anthropic', exact=True).click()
                    card = page.locator('.au-provider-limits .au-quota-card')
                    fills = card.locator('.au-quota-rows .h-full').evaluate_all('els=>els.map(e=>getComputedStyle(e).backgroundColor)')
                    assert fills == [state['anthropic']['expected']] * 2, fills
                    assert page.locator('[data-testid="provider-row-quota"]').count() == 0
                    card.screenshot(path=str(SCRATCH / 'row-quota-anthropic-provider-card.png'))
                    # All profiles has no combined subscription quota, so no bars.
                    nav.get_by_role('tab', name='All providers').click()
                    page.get_by_role('combobox', name='Hermes profile').select_option(label='All profiles')
                    nav.get_by_role('tab', name='All providers').click()
                    expect(page.locator('.au-provider-row').first).to_be_visible()
                    assert page.locator('[data-testid="provider-row-quota"]').count() == 0
                assert not errors, errors
                page.close()
            default_catalogue_fit(browser)
            report = fit_matrix(browser)
            print('PASS: All-providers row bars (Anthropic 2, Codex 1/2, no-quota 0), labels above bars, provider colours, '
                  'no card chrome, unchanged divider, 1920/850/390 fit, default catalogue fits 850x925, Subscriptions/provider Anthropic fills')
            print('PASS: 5-provider/3-window fit matrix + breakpoint pairs, pane-size-only mode, >=160px split reader, '
                  'root-only single scroller with reachable lower content; (upper, reader) px:', report)
            print('Screenshots in', SCRATCH)
        finally:
            browser.close()


if __name__ == '__main__':
    test_row_quota()
