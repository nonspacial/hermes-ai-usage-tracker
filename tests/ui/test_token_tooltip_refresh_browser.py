"""Focused offline UI contract: token precision, shared tooltip and Refresh states."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def run():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'), headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            page = browser.new_page(viewport={'width': 1160, 'height': 850})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
            expect(page.get_by_test_id('quota-home')).to_be_visible()
            # Status-bar contribution lives outside the pane; mount its real
            # component in the offline harness rather than assuming pane CSS.
            page.evaluate("""() => {window.chipMount=document.createElement('div');document.body.appendChild(chipMount);ReactDOM.render(h(UsageChip),chipMount)}""")
            chip = page.locator('.au-usage-chip')
            expect(chip).to_be_visible()
            chip.focus()
            chip_tip = page.locator('body > .au-tooltip:visible')
            expect(chip_tip).to_contain_text('Weekly window')
            assert chip.get_attribute('data-au-tooltip') == chip_tip.inner_text()
            assert chip.get_attribute('aria-describedby') == chip_tip.get_attribute('id')
            page.evaluate('ReactDOM.unmountComponentAtNode(chipMount);chipMount.remove()')
            values = page.evaluate("[short(0), short(999), short(0.2875), short(1234), short(1234567), short(1234567890), short(1234567891), count(1234567891), money(1.5)]")
            assert values[:3] == ['0', '999', '0.29'], values
            assert [value.split('.')[1][:4] for value in values[3:7]] == ['2340', '2346', '2346', '2346'], values
            assert all(value.split('.')[1][4:] for value in values[3:7]), values
            # Billionths become visible when they cross the fourth decimal rounding boundary.
            assert page.evaluate('short(1234500000) !== short(1234600000)')
            assert values[7] == '1,234,567,891' and values[8].startswith('$1.5000'), values
            control = page.get_by_test_id('quota-home').get_by_test_id('codex-resets')
            auto = control.get_by_role('checkbox', name='Auto use banked Codex reset')
            auto.focus()
            canonical = control.locator('.au-reset-tooltip')
            expect(canonical).to_be_visible()
            assert canonical.evaluate('e=>{const b=e.getBoundingClientRect();return b.left>=0&&b.right<=innerWidth&&getComputedStyle(e).whiteSpace==="normal"}')
            page.screenshot(path='/home/nope/.hermes/profiles/infra/cache/scratch/token-tooltip-auto-wide.png')
            badge = control.get_by_role('button', name='Resets: 0')
            badge.focus()
            tip = page.locator('body > .au-tooltip:visible')
            expect(tip).to_contain_text('No banked resets')
            assert badge.get_attribute('aria-describedby') == tip.get_attribute('id')
            # Pointer over an untitled sibling must not dismiss a keyboard owner's description.
            badge.evaluate("e=>e.parentElement.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}))")
            expect(tip).to_contain_text('No banked resets')
            assert badge.get_attribute('aria-describedby') == tip.get_attribute('id')
            def appearance():
                return page.evaluate('''() => {
                    const a=document.querySelector('.au-reset-tooltip'),b=document.querySelector('body > .au-tooltip');
                    const style=e=>getComputedStyle(e);
                    const box=b.getBoundingClientRect();
                    return {background:style(a).backgroundColor, popup:style(b).backgroundColor,
                      shadow:style(b).boxShadow,whiteSpace:style(b).whiteSpace,
                      max:box.width,body:box.right<=innerWidth && box.left>=0};
                }''')
            a = appearance()
            assert a['background'] == a['popup'] and a['whiteSpace'] == 'normal' and a['shadow'] != 'none' and a['max'] <= 310 and a['body'], a
            # The owner pane (not documentElement) can override theme tokens live.
            page.evaluate("document.querySelector('.au-ledger').style.setProperty('--au-surface-bg','#292d40')")
            page.wait_for_function("getComputedStyle(document.querySelector('body > .au-tooltip')).backgroundColor==='rgb(41, 45, 64)'")
            scoped = appearance()
            assert scoped['background'] == scoped['popup'] != a['popup'], (a, scoped)
            page.evaluate("document.querySelector('.au-ledger').style.removeProperty('--au-surface-bg')")
            page.wait_for_function("getComputedStyle(document.querySelector('body > .au-tooltip')).backgroundColor===getComputedStyle(document.querySelector('.au-reset-tooltip')).backgroundColor")
            page.screenshot(path='/home/nope/.hermes/profiles/infra/cache/scratch/token-tooltip-reset-wide.png')
            def audit_visible_titles():
                return page.evaluate('''() => {
                  const root=document.querySelector('.au-ledger'), popup=document.querySelector('body > .au-tooltip');
                  const nodes=[...root.querySelectorAll('[title]')].filter(e=>e.getAttribute('title') && e.tagName!=='OPTION');
                  const svg=[...root.querySelectorAll('svg title')].filter(e=>e.textContent);
                  const failures=[];
                  for(const node of [...nodes,...svg]){
                    const target=node.tagName.toLowerCase()==='title'?node.parentElement:node;
                    const text=node.tagName.toLowerCase()==='title'?node.textContent:node.getAttribute('title');
                    target.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));
                    const expected=target.matches('[data-testid="connection-status"]')?
                      text+target.getAttribute('data-au-freshness')+(target.getAttribute('data-au-freshness-age')||''):text;
                    if(popup.textContent!==expected || popup.style.display==='none' || target.getAttribute('aria-describedby')!==popup.id)
                      failures.push(target.tagName+': '+text);
                    target.dispatchEvent(new MouseEvent('mouseout',{bubbles:true}));
                  }
                  return {native:nodes.length,svg:svg.length,failures};
                }''')
            inventory = audit_visible_titles()
            assert inventory['native'] >= 5 and not inventory['failures'], inventory
            badge.evaluate('e=>e.blur()')
            page.get_by_role('button', name='Refresh', exact=True).focus()
            expect(tip).to_contain_text('Refresh data')
            refresh = page.get_by_role('button', name='Refresh', exact=True)
            assert refresh.get_attribute('aria-describedby') == tip.get_attribute('id')
            # Exercise the actual themed hover rule, then change the host accent.
            refresh.hover()
            def hover_state():
                return refresh.evaluate('''e=>({bg:getComputedStyle(e).backgroundColor,
                  border:getComputedStyle(e).borderTopColor,color:getComputedStyle(e).color,
                  accent:getComputedStyle(e).getPropertyValue('--ui-accent').trim(),
                  selected:getComputedStyle(e).getPropertyValue('--au-selected-bg').trim()})''')
            before = hover_state()
            page.evaluate("document.documentElement.style.setProperty('--ui-accent','#20c98b')")
            after = hover_state()
            assert before['bg'] != after['bg'] and before['border'] != after['border'], (before, after)
            assert after['accent'] == '#20c98b' and after['border'] == 'rgb(32, 201, 139)', after
            assert refresh.evaluate("e=>getComputedStyle(e).outlineColor") == 'rgb(32, 201, 139)'
            page.screenshot(path='/home/nope/.hermes/profiles/infra/cache/scratch/token-refresh-accent-hover.png')
            calls = page.evaluate('demoCalls.length')
            refresh.evaluate('(e)=>{e.disabled=true}')
            disabled = hover_state()
            assert disabled['border'] != after['border'] and disabled['bg'] != after['bg'], (disabled, after)
            refresh.evaluate('e=>e.click()')
            assert page.evaluate('demoCalls.length') == calls
            refresh.evaluate('(e)=>{e.disabled=false}')
            # Inspect long explanations at a narrow pane edge, with a different surface token.
            page.evaluate("""() => {for(const [key,value] of Object.entries({
              '--ui-bg-chrome':'#f1f2e8','--ui-text-secondary':'#30323b',
              '--ui-text-primary':'#191c25','--ui-stroke-secondary':'#aeb2ba',
              '--ui-base':'#21232a'}))document.documentElement.style.setProperty(key,value)}""")
            page.set_viewport_size({'width': 390, 'height': 760})
            badge.focus()
            expect(tip).to_contain_text('No banked resets')
            narrow = appearance()
            assert narrow['popup'] != a['popup'] and narrow['body'] and narrow['max'] <= 310, (a, narrow)
            auto.focus()
            expect(canonical).to_be_visible()
            assert canonical.evaluate('e=>{const b=e.getBoundingClientRect();return b.left>=0&&b.right<=innerWidth}')
            # Short-height viewport retains all warning copy inside the screen.
            page.evaluate("""() => {for(const key of ['--ui-bg-chrome','--ui-text-secondary','--ui-text-primary','--ui-stroke-secondary','--ui-base'])document.documentElement.style.removeProperty(key)}""")
            page.set_viewport_size({'width': 320, 'height': 180})
            auto.scroll_into_view_if_needed()
            auto.focus()
            expect(canonical).to_be_visible()
            assert canonical.evaluate('''e=>{const b=e.getBoundingClientRect();return b.top>=0&&b.bottom<=innerHeight&&b.left>=0&&b.right<=innerWidth&&e.scrollHeight<=e.clientHeight}''')
            page.screenshot(path='/home/nope/.hermes/profiles/infra/cache/scratch/token-tooltip-auto-320x180.png')
            # SVG titles are still present when idle: chart gesture and pie accessibility.
            page.set_viewport_size({'width': 1160, 'height': 850})
            page.get_by_role('navigation', name='Providers', exact=True).get_by_role('tab', name='Codex', exact=True).click()
            chart = page.get_by_test_id('usage-chart')
            expect(chart).to_be_visible()
            page.wait_for_function("document.querySelectorAll('body > .au-tooltip').length===1")
            page.mouse.move(0, 0)
            chart.focus()
            page.wait_for_function('''() => {const chart=document.querySelector('.au-chart'),tip=document.querySelector('body > .au-tooltip');
              return document.activeElement===chart && tip?.style.display==='block' &&
                tip.textContent.includes('Left/Right') && chart.getAttribute('aria-describedby')===tip.id}''')
            plot = page.locator('.au-plot-hit').first
            if plot.count():
                title = plot.locator('title')
                assert 'Left/Right' in (title.text_content() or '')
                plot.hover(force=True)
                expect(tip).to_contain_text('Left/Right')
                plot.evaluate("e=>e.querySelector('title').textContent='Updated chart gesture explanation'")
                expect(tip).to_have_text('Updated chart gesture explanation')
                assert plot.get_attribute('aria-label') == 'Updated chart gesture explanation'
                page.mouse.move(0, 0)
                assert title.text_content() == 'Updated chart gesture explanation'
                plot.evaluate("e=>e.querySelector('title').textContent='Click to interact with Left/Right arrow keys. Click and drag at least 15 minutes from the start to preview a 30-minute or longer window on half-hour boundaries.'")
                chart.blur()
                plot.hover(force=True)
                page.screenshot(path='/home/nope/.hermes/profiles/infra/cache/scratch/token-tooltip-chart-wide.png')
                page.mouse.move(0, 0)
                assert 'Left/Right' in (title.text_content() or '')
            inventory = audit_visible_titles()
            assert inventory['native'] >= 10 and inventory['svg'] >= 1 and not inventory['failures'], inventory
            subpages = page.get_by_role('navigation', name='Provider subpages')
            for name in ('Requests', 'Cache & costs', 'Compressions', 'Models & tasks'):
                subpages.get_by_role('tab', name=name, exact=True).click()
                expect(page.get_by_role('tabpanel', name=name)).to_be_visible()
                inventory = audit_visible_titles()
                assert inventory['native'] >= 5 and not inventory['failures'], (name, inventory)
            page.set_viewport_size({'width': 1500, 'height': 1100})
            subpages.get_by_role('tab', name='Skills usage').click()
            skills = page.get_by_test_id('skill-frequency')
            expect(skills.get_by_role('img', name='Skill load frequency')).to_be_visible()
            slice_title = skills.locator('svg path title').first
            slice_text = slice_title.text_content() or ''
            assert 'loads' in slice_text or '%' in slice_text
            skills.locator('svg path').first.scroll_into_view_if_needed()
            slice_box = skills.locator('svg path').first.bounding_box()
            assert slice_box and 0 <= slice_box['y'] < 1100, slice_box
            skills.locator('svg path').first.hover(position={'x':slice_box['width']*.7,'y':slice_box['height']*.5})
            expect(tip).to_contain_text(slice_text)
            page.mouse.move(0, 0)
            inventory = audit_visible_titles()
            assert inventory['svg'] >= 1 and not inventory['failures'], inventory
            slices = skills.locator('svg path[role="button"],svg circle[role="button"]')
            assert slices.count() > 0
            slice = slices.first
            assert slice.get_attribute('tabindex') == '0' and slice.get_attribute('aria-label')
            for key in ('Enter', 'Space'):
                # Choose a different selected slice each time to prove activation.
                other = slices.nth(1)
                other.click()
                expect(other).to_have_attribute('aria-pressed', 'true')
                slice.focus()
                expect(tip).to_contain_text(slice.get_attribute('aria-label') or '')
                slice.press(key)
                expect(slice).to_have_attribute('aria-pressed', 'true')
                legend = skills.locator('.au-skill-legend button').first
                expect(legend).to_have_attribute('aria-pressed', 'true')
                page.get_by_test_id('skills-usage').get_by_role('button', name='Clear skill selection').click()
                expect(slice).to_have_attribute('aria-pressed', 'false')
            # Exercise the aggregate and an open JSON panel, not only mounted titles.
            page.get_by_role('navigation', name='Providers', exact=True).get_by_role('tab', name='All providers').click()
            all_inventory = audit_visible_titles()
            assert all_inventory['native'] >= 5 and not all_inventory['failures'], all_inventory
            subpages.get_by_role('tab', name='Requests', exact=True).click()
            details = page.get_by_test_id('usage-details').nth(1)
            row = details.locator('xpath=ancestor::tr')
            if row.locator('.au-record-disclosure').is_visible():
                row.locator('.au-record-disclosure').click()
            details.locator('summary').click()
            expect(details.get_by_test_id('copy-json')).to_be_visible()
            details.get_by_test_id('copy-json').focus()
            expect(tip).to_have_text('Copy full JSON')
            assert not audit_visible_titles()['failures']
            # Saved-marker control is conditional; synthesize one through the
            # same offline fixture catalogue, then inspect its actual button.
            page.evaluate("""() => {
              const end=Math.floor(Date.now()/1800000)*1800-600;
              savedTests.unshift({id:'tooltip-marker',label:'Tooltip marker',started:end-3600,ended:end});
              window.demoChange++;for(const fn of window.demoSubscribers)fn({type:'changed',mode:'native-events'});
            }""")
            page.get_by_role('combobox', name='Saved tests').select_option('tooltip-marker')
            marker = page.get_by_role('button', name='Remove saved test marker filter')
            expect(marker).to_be_visible()
            marker.focus()
            expect(tip).to_contain_text('saved marker')
            assert marker.get_attribute('aria-describedby') == tip.get_attribute('id')
            assert not errors, errors
            print('token/tooltip/Refresh focused browser checks passed')

        finally:
            browser.close()

if __name__ == '__main__':
    run()
