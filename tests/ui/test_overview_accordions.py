"""Offline browser contract for all five Overview breakdown disclosures."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def settle(page):
    page.evaluate('() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>requestAnimationFrame(r))))')


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 2200, 'height': 1200})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', lambda route: route.abort())
        page.set_content((ROOT / 'preview.html').read_text())
        page.add_style_tag(content='#root{max-width:none;flex:none;height:900px;width:2100px;margin:0;padding:0}')
        page.get_by_role('tab', name='All providers', exact=True).click()
        nav = page.get_by_role('navigation', name='Provider subpages')
        nav.get_by_role('tab', name='Overview', exact=True).click()
        group = page.get_by_role('group', name='Breakdown grouping')
        pane = page.locator('.au-ledger')
        for label in ('Model', 'Hour', 'Project', 'Session', 'Subagents'):
            group.get_by_role('button', name=label, exact=True).click()
            table = page.locator('.au-breakdown .au-table')
            expect(table).to_have_attribute('data-layout', 'table')
            expect(table.locator('tbody tr')).not_to_have_count(0)
            baseline = table.locator('tbody .au-field-value').all_text_contents()
            headers = table.locator('thead th').all_text_contents()
            assert headers[1:5] == ['Cost · API estimate', 'Share', 'Processed tokens', 'Sessions']
            assert table.locator('tbody tr').first.locator('td').nth(1).is_visible()
            if label == 'Model':
                expect(table.locator('tbody tr').first.locator('td').first.locator('.au-field-value > div > .au-muted')).to_be_visible()
            pane.evaluate('(e)=>e.style.width="390px"')
            expect(table).to_have_attribute('data-layout', 'records')
            rows = table.locator('tbody tr')
            first = rows.first
            button = first.locator('.au-record-disclosure')
            expect(button).to_have_attribute('aria-expanded', 'false')
            assert first.locator('.au-record-identity').inner_text().strip()
            if label == 'Model':
                identity = first.locator('.au-record-identity')
                assert identity.evaluate('(e)=>e.querySelector("div, button") === null')
                assert identity.locator('.au-muted').inner_text().strip()
            summaries = button.locator('.au-record-summary')
            assert summaries.count() == 2
            assert summaries.nth(0).locator('strong').inner_text() == first.locator('td').nth(1).locator('.au-field-value').inner_text().strip()
            assert summaries.nth(1).locator('strong').inner_text() == first.locator('td').nth(3).locator('.au-field-value').inner_text().strip()
            assert summaries.nth(0).locator('.au-record-summary-label').inner_text() == 'Cost · API estimate'
            assert summaries.nth(1).locator('.au-record-summary-label').inner_text().startswith('Processed tokens')
            assert all(not first.locator('td').nth(i).is_visible() for i in range(1, len(headers)))
            if label == 'Model':
                page.locator('.au-reader').screenshot(path=str(ROOT / 'tests/ui/artifacts/overview-accordions-narrow.png'))
            button.focus()
            button.press('Enter')
            expect(button).to_have_attribute('aria-expanded', 'true')
            assert all(first.locator('td').nth(i).is_visible() for i in range(1, len(headers)))
            assert table.locator('tbody .au-field-value').all_text_contents() == baseline
            assert first.locator('.au-field-label').all_text_contents() == headers
            assert first.locator('td').nth(2).inner_text().strip().endswith('%') or first.locator('td').nth(2).inner_text().strip().endswith('—')
            if rows.count() > 1:
                other = rows.nth(1).locator('.au-record-disclosure')
                expect(other).to_have_attribute('aria-expanded', 'false')
                other.click()
                expect(button).to_have_attribute('aria-expanded', 'true')
                expect(other).to_have_attribute('aria-expanded', 'true')
            first.evaluate('(e)=>window.keptOverviewRow=e')
            page.evaluate('queryClient.invalidateQueries()')
            settle(page)
            assert first.evaluate('(e)=>e===window.keptOverviewRow')
            expect(button).to_have_attribute('aria-expanded', 'true')
            button.focus()
            pane.evaluate('(e)=>e.style.width=""')
            expect(table).to_have_attribute('data-layout', 'table')
            expect(first).to_be_focused()
            assert table.locator('tbody .au-field-value').all_text_contents() == baseline
            pane.evaluate('(e)=>e.style.width="390px"')
            expect(table).to_have_attribute('data-layout', 'records')
            expect(button).to_have_attribute('aria-expanded', 'true')
            expect(button).to_be_focused()
            assert first.evaluate('(e)=>!e.hasAttribute("tabindex")')
            button.press('Space')
            expect(button).to_have_attribute('aria-expanded', 'false')
            assert table.evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
            pane.evaluate('(e)=>e.style.width=""')
        group.get_by_role('button', name='Project', exact=True).click()
        drill = page.locator('.au-breakdown tbody .au-record-heading button.au-drill').first
        drill.focus()
        drilled_row = drill.locator('xpath=ancestor::tr')
        pane.evaluate('(e)=>e.style.width="390px"')
        expect(page.locator('.au-breakdown .au-table')).to_have_attribute('data-layout', 'records')
        expect(drilled_row.locator('.au-record-disclosure')).to_be_focused()
        expect(drilled_row).to_have_attribute('data-expanded', 'true')
        picker = page.get_by_role('combobox', name='Hermes profile', exact=True)
        picker.select_option('profile:default')
        group.get_by_role('button', name='Project', exact=True).click()
        pane.evaluate('(e)=>e.style.width="390px"')
        expect(page.locator('.au-breakdown tbody tr').first.locator('.au-record-disclosure')).to_be_visible()
        picker.select_option('scope:all')
        expect(page.get_by_test_id('profile-coverage')).to_contain_text('complete')
        pane = page.locator('.au-ledger')
        pane.evaluate('(e)=>e.style.width="390px"')
        aggregate = page.locator('.au-breakdown .au-table')
        expect(aggregate).to_have_attribute('data-layout', 'records')
        aggregate_keys = aggregate.locator('tbody tr').evaluate_all('(rows)=>rows.map(r=>r.dataset.rowId)')
        assert len(aggregate_keys) == len(set(aggregate_keys))
        aggregate.locator('.au-record-disclosure').first.click()
        expect(aggregate.locator('tbody tr').first).to_have_attribute('data-expanded', 'true')
        picker.select_option('profile:infra')
        pane = page.locator('.au-ledger')
        pane.evaluate('(e)=>e.style.width="390px"')
        expect(page.locator('.au-breakdown .au-table')).to_have_attribute('data-layout', 'records')
        assert all('ap1.' not in key for key in page.locator('.au-breakdown tbody tr').evaluate_all('(rows)=>rows.map(r=>r.dataset.rowId)'))
        # Exercise aggregate unknown versus true zero and partial-price markers
        # with fixture-owned DTOs, not host or account data.
        group.get_by_role('button', name='Model', exact=True).click()
        page.evaluate('''() => {
          const base=rest;
          window.addedOverviewRows=0;
          rest=async(path,...args)=>{
            const d=await base(path,...args);
            if(path.startsWith('/ledger?')){
              const original=d.model_groups[0];
              const clone=(model,patch)=>({...original,...patch,model});
              d.model_groups=[
                clone('zero-model',{attempts:0,unpriced_requests:0,known_cost_usd:0,known:{...original.known,total_tokens:0}}),
                clone('unknown-model',{attempts:2,unpriced_requests:2,missing_fields:{...original.missing_fields,total_tokens:2},cost_missing_fields:Object.fromEntries(['input_tokens','output_tokens','cache_read_tokens','cache_write_tokens'].map(k=>[k,2]))}),
                clone('partial-model',{attempts:2,unpriced_requests:1,known_cost_usd:1.25,missing_fields:{...original.missing_fields,total_tokens:1},known:{...original.known,total_tokens:400},cost_missing_fields:{input_tokens:1,output_tokens:2,cache_read_tokens:2,cache_write_tokens:2}}),
                ...Array.from({length:window.addedOverviewRows},(_,i)=>clone('prepended-'+i,{})),
                ...d.model_groups
              ];
            }return d;
          };
          window.demoChange++;for(const callback of window.demoSubscribers)callback({type:'changed',mode:'native-events'});
        }''')
        pane.evaluate('(e)=>e.style.width="390px"')
        table = page.locator('.au-breakdown .au-table')
        expect(table.locator('tbody tr').first.locator('.au-record-identity')).to_contain_text('zero-model')
        rows = table.locator('tbody tr')
        for index, expected in enumerate(('$0.00', '— *', '$1.25 *')):
            button = rows.nth(index).locator('.au-record-disclosure')
            expect(button.locator('.au-record-summary').first.locator('strong')).to_have_text(expected)
        expect(rows.nth(0).locator('.au-record-summary').nth(1).locator('strong')).to_have_text('0')
        expect(rows.nth(1).locator('.au-record-summary').nth(1).locator('strong')).to_have_text('—')
        expect(rows.nth(2).locator('.au-record-summary').nth(1).locator('strong')).to_have_text('400')
        expect(rows.nth(2).locator('.au-record-summary').nth(1).locator('.au-record-summary-label')).to_contain_text('known subtotal')
        assert 'incomplete/unpriced' in (rows.nth(2).locator('.au-record-summary').first.get_attribute('title') or '')
        reader = page.locator('.au-reader')
        anchor = rows.last
        anchor.locator('.au-record-disclosure').click()
        anchor.scroll_into_view_if_needed()
        reader.evaluate('(e)=>e.scrollTop=Math.max(0,e.scrollTop-40)')
        settle(page)
        key = anchor.get_attribute('data-scroll-key')
        before = anchor.evaluate('(e)=>e.getBoundingClientRect().top-e.closest(".au-reader").getBoundingClientRect().top')
        previous_count = rows.count()
        page.evaluate('''() => {window.addedOverviewRows=20;window.demoChange++;for(const callback of window.demoSubscribers)callback({type:'changed',mode:'native-events'})}''')
        expect(rows).to_have_count(previous_count+20)
        settle(page)
        assert anchor.get_attribute('data-scroll-key') == key
        expect(anchor.locator('.au-record-disclosure')).to_have_attribute('aria-expanded', 'true')
        after = anchor.evaluate('(e)=>e.getBoundingClientRect().top-e.closest(".au-reader").getBoundingClientRect().top')
        assert abs(after-before) <= 2, (before, after)
        assert not errors, errors
        browser.close()
    print('PASS five Overview groupings: header/body parity, independent keyboard disclosure, refresh identity, focus and wide/narrow reflow')


if __name__ == '__main__':
    run()
