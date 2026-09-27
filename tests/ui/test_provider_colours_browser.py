"""Isolated synthetic colour-scope checks; never reads a real account or ledger."""
import os
from pathlib import Path
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SCRATCH = Path(os.environ.get('HERMES_SCRATCH', '/home/nope/.hermes/profiles/infra/cache/scratch'))
CHROME = os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium')
EXTRA = ['ollama-cloud', 'anthropic', 'deepseek', 'gemini', 'qwen']


def fixture(reverse=False):
    html = (ROOT / 'preview.html').read_text()
    extra = """
for(const id of ['ollama-cloud','anthropic','deepseek','gemini','qwen']){
 const event=JSON.parse(JSON.stringify(events[0]));event.provider=id;
 event.id='colour-'+id;event.started=now-900;event.ended=now-899;
 events.push(event);
 demoProviders.push({id,label:id,configured:true,quota:{available:true,plan:'Free',
   windows:[{label:'Weekly',remaining_percent:55},{label:'Daily',remaining_percent:40}]}});
}
// Anthropic shaped like the host's OAuth usage mapping as serialised by
// dashboard/plugin_api.py: five_hour -> 'Current session', seven_day ->
// 'Current week', seven_day_sonnet -> 'Sonnet week' (synthetic percentages).
demoProviders.find(p=>p.id==='anthropic').quota.windows=[
 {label:'Current session',used_percent:38,remaining_percent:62,reset_at:new Date((now+3600)*1000).toISOString(),detail:null},
 {label:'Current week',used_percent:45,remaining_percent:55,reset_at:new Date((now+4*86400)*1000).toISOString(),detail:null},
 {label:'Sonnet week',used_percent:20,remaining_percent:80,reset_at:new Date((now+4*86400)*1000).toISOString(),detail:null}];
"""
    if reverse:
        extra += 'demoProviders.reverse();events.reverse();\n'
    assert 'const demoContexts={' in html
    return html.replace('const demoContexts={', extra + 'const demoContexts={', 1)


def colour_state(page):
    return page.evaluate('''() => {
      const panel=document.querySelector('[data-testid="provider-page"]');
      const css=(selector,field)=>{const el=document.querySelector(selector);return el?getComputedStyle(el)[field]:null};
      const card=document.querySelector('.au-provider-limits .au-quota-card') || document.querySelector('.au-quota-home .au-quota-card');
      const selected=[...document.querySelectorAll('.au-ledger button[aria-selected="true"],.au-ledger button[aria-pressed="true"]')]
        .map(el=>{const s=getComputedStyle(el);return [s.color,s.borderColor,s.backgroundColor,s.boxShadow]});
      const badge=card?.querySelector('.au-provider-type-badge');
      const fill=index=>{const el=card?.querySelectorAll('.au-quota-rows .h-full')[index];return el?getComputedStyle(el).backgroundColor:null};
      const fills=card?Object.fromEntries([...card.querySelectorAll('.au-quota-rows .h-full')].map(el=>
        [el.closest('.flex-col').querySelector('span').textContent,getComputedStyle(el).backgroundColor])):null;
      return {line:css('.au-chart .au-line','stroke'),area:css('.au-chart .au-area','fill'),
        point:css('.au-chart .au-point','fill'),focus:css('.au-chart .au-plot-hit','stroke'),
        selected,body:css('.au-big','color'),amount:css('.au-provider-cost','color'),
        label:css('.au-chart-title','color'),accent:getComputedStyle(panel).getPropertyValue('--ui-accent').trim(),
        card:card?{badge:badge?getComputedStyle(badge).backgroundColor:null,
          border:badge?getComputedStyle(badge).borderColor:null,
          foreground:badge?getComputedStyle(badge).color:null,
          weekly:fill(0),daily:fill(1),fills}:null};
    }''')


def colours(page):
    return page.evaluate('''() => {
      const probe=document.createElement('canvas'),ctx=probe.getContext('2d');probe.width=probe.height=1;
      const rgb=colour=>{ctx.fillStyle=colour;ctx.fillRect(0,0,1,1);return [...ctx.getImageData(0,0,1,1).data].slice(0,3)};
      const labels=['openai-codex','nous','ollama','ollama-cloud','openrouter','anthropic','deepseek','gemini','qwen'];
      return Object.fromEntries(labels.map(id=>{const el=document.createElement('i');el.style.color=providerAccent(id);
        document.body.append(el);const colour=getComputedStyle(el).color;el.remove();return [id,{colour,rgb:rgb(colour)}]}));
    }''')


def assert_palette(page):
    values=colours(page)
    assert len({tuple(v['rgb']) for v in values.values()})==len(values),values
    for id in EXTRA[1:]:
        for other in values:
            if other==id:continue
            a,b=values[id]['rgb'],values[other]['rgb']
            assert sum((x-y)**2 for x,y in zip(a,b))>=28**2,(id,other,a,b)
    # Compare perceptual separation in browser-visible sRGB rather than CSS expressions.
    # Codex/OpenRouter must not be near-identical lavender marks.
    a,b=values['openai-codex']['rgb'],values['openrouter']['rgb']
    assert sum((x-y)**2 for x,y in zip(a,b))**.5>80,(a,b)
    assert page.evaluate("providerAccent('unknown')") == 'var(--ui-text-secondary)'
    return values


def badge_contrast(page):
    return page.evaluate('''() => {
      const badge=document.querySelector('.au-provider-limits .au-provider-type-badge');
      const ctx=document.createElement('canvas').getContext('2d');
      const rgb=value=>{ctx.fillStyle=value;ctx.fillRect(0,0,1,1);return [...ctx.getImageData(0,0,1,1).data].slice(0,3)};
      const lum=value=>rgb(value).map(x=>{x/=255;return x<=.04045?x/12.92:((x+.055)/1.055)**2.4})
        .reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
      const fg=lum(getComputedStyle(badge).color),bg=lum(getComputedStyle(badge).backgroundColor);
      return (Math.max(fg,bg)+.05)/(Math.min(fg,bg)+.05);
    }''')


def test_colours():
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=CHROME,headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        try:
            page=browser.new_page(viewport={'width':1480,'height':900})
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.set_content(fixture(),wait_until='domcontentloaded')
            nav=page.get_by_role('navigation',name='Providers',exact=True)
            nav.get_by_role('tab',name='All providers').click()
            panel=page.get_by_test_id('provider-page')
            expect(panel.locator('.au-provider-row')).to_have_count(9)
            page.get_by_test_id('usage-chart').focus();page.get_by_test_id('usage-chart').press('ArrowRight')
            original=colour_state(page);baseline=assert_palette(page)
            assert original['area'] is None
            assert original['line'] in {v['colour'] for v in baseline.values()}
            assert panel.locator('.au-chart .au-provider-series').count()==9
            assert panel.locator('.au-chart .au-area').count()==0
            assert original['card'] is None
            assert panel.locator('.au-chart .au-provider-series').evaluate_all('''paths=>{
              const rows=[...document.querySelectorAll('.au-provider-row')];
              return paths.every((path,index)=>getComputedStyle(path).stroke===
                getComputedStyle(rows[index].querySelector('.au-dot')).backgroundColor);
            }''')
            dots=page.evaluate('''() => [...document.querySelectorAll('.au-provider-row')]
              .map(row=>getComputedStyle(row.querySelector('.au-dot')).backgroundColor)''')
            assert len(set(dots))==9,dots
            tooltip_rows=page.locator('.au-tooltip:visible .au-tooltip-provider').all_text_contents()
            assert len(tooltip_rows)==9 and any('Ollama Cloud (ollama)' in row for row in tooltip_rows)
            assert any('Ollama Cloud (ollama-cloud)' in row for row in tooltip_rows)
            page.screenshot(path=str(SCRATCH/'provider-colours-all-synthetic.png'))
            for id,label in [('openai-codex','Codex'),('nous','Nous Portal'),('ollama','Ollama Cloud'),('ollama-cloud','ollama-cloud'),('openrouter','OpenRouter'),*[(id,id) for id in EXTRA[1:]]]:
                nav.get_by_role('tab',name=label,exact=True).click()
                expect(panel).to_have_attribute('data-provider',id)
                chart=page.get_by_test_id('usage-chart');chart.focus();chart.press('ArrowRight')
                state=colour_state(page)
                assert state['line']==state['area']==state['point']==baseline[id]['colour'],(id,state,baseline[id])
                assert state['selected']==original['selected'],(id,state['selected'],original['selected'])
                for key in ('body','amount','label','focus','accent'):
                    assert state[key]==original[key],(id,key,state[key],original[key])
                if id=='anthropic':
                    fills=state['card']['fills']
                    assert fills['Current session']==fills['Current week']==fills['Sonnet week']==baseline[id]['colour'],(id,fills)
                    assert fills['Current week']!=baseline['openai-codex']['colour'],(id,fills)
                    assert fills['Current week']==state['line']==state['card']['border'],(id,state)
                    page.locator('.au-provider-limits .au-quota-card').screenshot(path=str(SCRATCH/'provider-colours-anthropic-card-page-synthetic.png'))
                elif id not in ('nous','ollama','openrouter'):
                    assert state['card']['weekly']==baseline[id]['colour'],(id,state['card'])
                    # Every measured allowance bar (weekly and shorter windows) uses the provider colour.
                    if id!='openai-codex':assert state['card']['daily']==baseline[id]['colour'],(id,state['card'])
                if id not in ('ollama','openrouter'):
                    assert state['card']['border']==baseline[id]['colour'],(id,state['card'])
                    assert page.evaluate('''() => {
                      const badge=document.querySelector('.au-provider-type-badge');
                      const probe=document.createElement('i');probe.style.color='var(--ui-text-secondary)';
                      badge.parentElement.append(probe);
                      const same=getComputedStyle(probe).color===getComputedStyle(badge).color;
                      probe.remove();return same;
                    }''')
                    assert page.evaluate('''() => {
                      const card=document.querySelector('.au-provider-limits .au-quota-card');
                      const badge=card.querySelector('.au-provider-type-badge');
                      const original=card.querySelector('small');
                      return original&&getComputedStyle(original).fontSize===getComputedStyle(badge).fontSize&&
                        getComputedStyle(original).fontWeight===getComputedStyle(badge).fontWeight&&
                        original.getBoundingClientRect().height===badge.getBoundingClientRect().height;
                    }''')
                    assert badge_contrast(page)>=4.5,(id,badge_contrast(page))
                footer=page.get_by_test_id('chart-point-summary');expect(footer).to_be_visible()
                assert footer.evaluate('e=>getComputedStyle(e).color')==baseline['openai-codex']['colour']
                assert 'click' in page.locator('.au-tooltip:visible').last.inner_text().lower()
                if id in ('openai-codex','openrouter'):
                    page.screenshot(path=str(SCRATCH/f'provider-colours-{id}-synthetic.png'))
            nav.get_by_role('tab',name='Subscriptions').click()
            cards=page.locator('.au-quota-home .au-quota-card')
            expect(cards).to_have_count(9)
            for i,id in enumerate(['openai-codex','nous','ollama','openrouter',*EXTRA]):
                card=cards.nth(i)
                assert card.locator('.au-provider-type-badge').evaluate('e=>getComputedStyle(e).borderColor')==baseline[id]['colour'] if card.locator('.au-provider-type-badge').count() else id in ('ollama','openrouter')
                if id=='anthropic':
                    rows={row.locator('span').first.text_content():row.locator('.h-full').evaluate('e=>getComputedStyle(e).backgroundColor')
                          for row in card.locator('.au-quota-rows > .flex-col').all()}
                    assert rows=={'Current session':baseline[id]['colour'],'Current week':baseline[id]['colour'],
                                  'Sonnet week':baseline[id]['colour']},rows
                    card.screenshot(path=str(SCRATCH/'provider-colours-anthropic-card-subscriptions-synthetic.png'))
                elif card.locator('.au-quota-rows .h-full').count():
                    assert card.locator('.au-quota-rows .h-full').first.evaluate('e=>getComputedStyle(e).backgroundColor')==baseline[id]['colour']
            nav.get_by_role('tab',name='All providers').click()
            page.get_by_role('combobox',name='Hermes profile').select_option(label='All profiles')
            page.get_by_role('combobox',name='Hermes profile').select_option(label='infra')
            expect(panel.locator('.au-provider-row')).to_have_count(9)
            expect(panel.locator('.au-chart .au-provider-series')).to_have_count(9)
            assert colours(page)==baseline
            assert colour_state(page)['line'] in {v['colour'] for v in baseline.values()}
            # An asynchronous quota catalogue arrival adds a real navigation/card
            # entry without reallocating any colour already assigned.
            page.evaluate('''() => {
              demoProviders.push({id:'late-provider',label:'Late provider',configured:true,
                quota:{available:true,plan:'Free',windows:[{label:'Reported weekly window',remaining_percent:55}]}});
              queryClient.invalidateQueries({queryKey:['ai-usage-tracker','usage']});
            }''')
            expect(nav.get_by_role('tab',name='Late provider')).to_be_visible()
            assert colours(page)==baseline
            nav.get_by_role('tab',name='Late provider').click()
            expect(panel.locator('.au-provider-type-badge')).to_be_visible()
            assert badge_contrast(page)>=4.5
            assert panel.locator('.au-quota-rows .h-full').first.evaluate('e=>getComputedStyle(e).backgroundColor') == page.evaluate('''() => {
              const el=document.createElement('i');el.style.color=providerAccent('late-provider');document.body.append(el);
              const value=getComputedStyle(el).color;el.remove();return value;
            }''')
            nav.get_by_role('tab',name='All providers').click()
            # Change host tokens in place: no plugin remount, and text/control surfaces retain host semantics.
            page.evaluate('''() => {
              for(const [key,value] of Object.entries({'--ui-accent':'#199071','--ui-cyan':'#418aa1',
                '--ui-green':'#68b48d','--ui-orange':'#db7843','--ui-yellow':'#cea642',
                '--ui-red':'#d86b81','--ui-blue':'#3a67d0','--ui-purple':'#9878d2','--ui-warm':'#bd875d'}))
                document.documentElement.style.setProperty(key,value);
            }''')
            changed=assert_palette(page)
            assert changed['nous']!=baseline['nous']
            assert colour_state(page)['line'] in {v['colour'] for v in changed.values()}
            nav.get_by_role('tab',name='OpenRouter').click()
            state=colour_state(page)
            assert state['line']==changed['openrouter']['colour']
            assert state['selected'][0][0]==changed['openai-codex']['colour']
            page.evaluate('''() => {
              for(const [key,value] of Object.entries({'--ui-bg-chrome':'#f8faff','--dt-background':'#f8faff',
                '--ui-base':'#17171a','--ui-text-primary':'#17171a','--ui-text-secondary':'#555967',
                '--ui-text-tertiary':'#626671','--ui-accent':'#0053fd',
                '--ui-cyan':'#327285','--ui-green':'#286a51','--ui-orange':'#ac5832',
                '--ui-yellow':'#916824','--ui-red':'#ae3b50',
                '--ui-purple':'#7155a3','--ui-warm':'#9b5747','--ui-blue':'#0053fd'}))document.documentElement.style.setProperty(key,value);
              const style=document.createElement('style');style.textContent='html,body,#root,#root .au-ledger{background-color:#f8faff!important;color-scheme:light!important}#root .border{background-color:#e9ebf1!important;border-color:#9299a6!important}';document.head.append(style);
            }''')
            light=assert_palette(page)
            assert light['ollama-cloud']['colour']!=light['openai-codex']['colour']
            assert colour_state(page)['line'] in {v['colour'] for v in light.values()}
            page.screenshot(path=str(SCRATCH/'provider-colours-openrouter-light-synthetic.png'))
            nav.get_by_role('tab',name='anthropic',exact=True).click()
            assert badge_contrast(page)>=4.5,badge_contrast(page)
            fills=colour_state(page)['card']['fills']
            assert fills['Current session']==fills['Current week']==fills['Sonnet week']==light['anthropic']['colour'],(fills,light['anthropic'])
            page.screenshot(path=str(SCRATCH/'provider-colours-anthropic-light-synthetic.png'))
            # Fresh reordered catalogue yields the same reserved mapping; late IDs retain existing slots.
            reversed_page=browser.new_page()
            reversed_page.set_content(fixture(reverse=True),wait_until='domcontentloaded')
            reversed_page.get_by_role('navigation',name='Providers',exact=True).get_by_role('tab',name='All providers').click()
            assert colours(reversed_page)==baseline
            reversed_page.evaluate("registerProviderColours(['late-provider','later-provider'])")
            first=page.evaluate("() => {registerProviderColours(['late-a']);const a=providerAccent('late-a');registerProviderColours(['late-b']);return [a,providerAccent('late-b')]}")
            second=reversed_page.evaluate("() => {registerProviderColours(['late-b']);const b=providerAccent('late-b');registerProviderColours(['late-a']);return [providerAccent('late-a'),b]}")
            assert first==second,(first,second)
            assert reversed_page.evaluate("providerColourSlots.get('nous')") == 0
            assert reversed_page.evaluate("providerColourSlots.get('late-provider') !== providerColourSlots.get('later-provider')")
            # Beyond named/secondary slots, generated host-relative hues remain
            # valid CSS and slots do not wrap/reuse. No unlimited perceptual claim.
            assert reversed_page.evaluate('''() => {
              registerProviderColours(Array.from({length:25},(_,n)=>'extra-'+n));
              const slots=[...providerColourSlots.values()];
              const generated=[...providerColourSlots.entries()].filter(([id,slot])=>slot>=16);
              return slots.length===new Set(slots).size && generated.length>0 &&
                generated.every(([id,slot])=>{
                  const probe=document.createElement('i');probe.style.color=providerSlotColour(slot);
                  document.body.append(probe);const value=getComputedStyle(probe).color;probe.remove();
                  return value.startsWith('color(')||value.startsWith('rgb(')||value.startsWith('oklch(');
                });
            }''')
            reversed_page.close()
            # Force the exact same first identity seed for two genuinely attached
            # unknowns. Catalogue reconciliation must resolve the rendered clash
            # canonically even when discovery is staggered in opposite orders.
            for accent in (None, '#0053fd'):
                outcomes=[]
                for order in (['collision-a','collision-b'],['collision-b','collision-a']):
                    collision_page=browser.new_page()
                    collision_page.set_content(fixture(),wait_until='domcontentloaded')
                    if accent:
                        collision_page.evaluate("value=>document.documentElement.style.setProperty('--ui-accent',value)",accent)
                    outcomes.append(collision_page.evaluate('''order => {
                      const original=providerColourHash;
                      providerColourHash=id=>id.startsWith('collision-')?0x12345678:original(id);
                      const seed=providerColourHash('collision-a');
                      if(seed!==providerColourHash('collision-b'))throw Error('Fixture seed did not collide');
                      for(const id of order)registerProviderColours([id]);
                      const ids=['openai-codex','nous','ollama','ollama-cloud','openrouter',...order];
                      const rgb=ids.map(id=>providerRenderedColour(providerAccent(id)));
                      if(new Set(rgb.map(row=>row.join(','))).size!==rgb.length)throw Error('Rendered colour collision');
                      return Object.fromEntries(order.map(id=>[id,{slot:providerColourSlots.get(id),rgb:providerRenderedColour(providerAccent(id))}]));
                    }''',order))
                    collision_page.close()
                assert outcomes[0]==outcomes[1],(accent,outcomes)
                assert outcomes[0]['collision-a']['slot']!=outcomes[0]['collision-b']['slot']
            assert not errors,errors
            print('PASS: nine distinct rendered colours in dark/light, forced collision resolved independent of staggered arrival, unchanged surfaces')
            print('Synthetic screenshots:',*[str(SCRATCH/f'provider-colours-{name}-synthetic.png') for name in ('all','openai-codex','openrouter','openrouter-light')])
        finally:
            browser.close()


if __name__=='__main__':
    test_colours()
