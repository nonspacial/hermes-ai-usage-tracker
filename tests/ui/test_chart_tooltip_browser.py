"""Offline chart tooltip placement and live theme/point coverage (synthetic only)."""
import os
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SCREENSHOT = Path(os.environ.get('HERMES_SCRATCH', '/home/nope/.hermes/profiles/infra/cache/scratch')) / 'chart-point-tooltip-synthetic.png'


# Resolve a theme token to the colour the pane paints, with the same fallback
# the pane stylesheet uses where the host leaves the token undefined.
FALLBACK = {'--ui-text-warning': '#d2b776', '--ui-text-success': '#64bba8'}
PROBE = """token => {const fallback={'--ui-text-warning':'#d2b776','--ui-text-success':'#64bba8'}[token];
  const probe=document.createElement('span');probe.style.color='var('+token+(fallback?','+fallback:'')+')';
  document.querySelector('.au-ledger').appendChild(probe);const colour=getComputedStyle(probe).color;probe.remove();return colour}"""
HOSTILE_MODEL = '<img src=x onerror="window.tipPwned=1">{warn:boom}\\ }'
HOSTILE_PROVIDER = 'evil{key:x}<b>'


def structured_copy(page, errors):
    """Tooltip markup: separate lines, theme-token tones, escaped data, plain description."""
    page.evaluate("""([model,provider]) => {
      for(const e of events){if(e.provider!=='openrouter'||!e.cost)continue;
        e.model=e.response_model=model;e.unpriced_reason='model_missing';
        e.cost={...e.cost,complete:false,rate:null,total_usd:null,known_components_usd:0,
          components:{input_tokens:null,output_tokens:null,cache_read_tokens:null,cache_write_tokens:null}}}
      const copy=JSON.parse(JSON.stringify(events.find(e=>e.provider==='openrouter')));
      events.push({...copy,id:'hostile-provider',provider});
      window.demoChange++;for(const s of window.demoSubscribers)s({type:'changed',mode:'native-events'});
    }""", [HOSTILE_MODEL, HOSTILE_PROVIDER])
    page.get_by_role('navigation', name='Providers', exact=True).get_by_role('tab', name='All providers', exact=True).click()
    page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name='Model').click()
    badge = page.get_by_test_id('unpriced-badge')
    expect(badge).to_be_visible()
    badge.focus()
    tip = page.locator('body > .au-tooltip:visible')
    expect(tip).to_contain_text('have no price')
    lines = tip.locator('.au-tip-line').all_text_contents()
    assert len(lines) >= 4 and lines[1] == 'Costs in this window leave them out' and 'Largest groups' not in tip.text_content(), lines
    # Hostile data renders as literal text inside its accent span: no element,
    # no script, no extra tone, no extra line.
    keys = tip.locator('.au-tip-key').all_text_contents()
    assert any(HOSTILE_MODEL in key for key in keys), keys
    assert tip.locator('img,b').count() == 0 and not page.evaluate('window.tipPwned'), tip.inner_html()
    assert all('{' not in w for w in tip.locator('.au-tip-warn').all_text_contents())
    # Plain-text accessible description: the popup is the described-by target and
    # its text equals the plain title (markup stripped, newlines kept).
    assert badge.get_attribute('aria-describedby') == tip.get_attribute('id')
    plain = badge.get_attribute('data-au-tooltip')
    assert tip.text_content() == plain and plain.count('\n') == len(lines) - 1, plain
    assert '{' not in plain.replace(HOSTILE_MODEL, '').replace(HOSTILE_PROVIDER, ''), plain
    assert page.evaluate("id=>document.getElementById(id).textContent", tip.get_attribute('id')) == plain
    for tone, token in (('warn', '--ui-text-warning'), ('key', '--ui-accent')):
        colours = set(tip.locator('.au-tip-' + tone).evaluate_all('els=>els.map(e=>getComputedStyle(e).color)'))
        assert colours == {page.evaluate(PROBE, token)}, (tone, colours)
    # A host-defined token (set on the owning pane) is used live, not the fallback.
    page.evaluate("document.querySelector('.au-ledger').style.setProperty('--ui-text-warning','#c0501f')")
    page.wait_for_function("getComputedStyle(document.querySelector('body > .au-tooltip .au-tip-warn')).color==='rgb(192, 80, 31)'")
    page.evaluate("document.querySelector('.au-ledger').style.removeProperty('--ui-text-warning')")
    page.mouse.move(0, 0)
    badge.blur()
    expect(badge).to_have_attribute('title', plain)
    # Healthy state uses the success token, each recorder fact on its own line.
    connection = page.get_by_test_id('connection-status')
    expect(connection).to_have_text('Online')
    connection.focus()
    ok = tip.locator('.au-tip-ok')
    expect(ok).to_have_text('Online')
    assert ok.evaluate('e=>getComputedStyle(e).color') == page.evaluate(PROBE, '--ui-text-success')
    status_lines = tip.locator('.au-tip-line').all_text_contents()
    assert status_lines[0] == 'Online' and status_lines[-2:] == ['Click to reconnect and refresh', 'Running agents are not restarted'], status_lines
    page.screenshot(path=str(SCREENSHOT.with_name('tooltip-freshness-badge.png')))
    connection.blur()
    # Muted caveats use the tertiary token.
    page.get_by_test_id('subagent-summary').focus()
    muted = tip.locator('.au-tip-muted')
    expect(muted).to_have_text('These tokens are already in the total')
    assert muted.evaluate('e=>getComputedStyle(e).color') == page.evaluate(PROBE, '--ui-text-tertiary')
    assert tip.locator('.au-tip-line').all_text_contents() == ['Show subagent requests only', 'These tokens are already in the total']
    # Chart provider rows escape a hostile provider id.
    chart = page.get_by_test_id('usage-chart')
    chart.focus()
    chart.press('End')
    for _ in range(60):
        chart.press('ArrowRight')
    rows = tip.locator('.au-tooltip-provider').all_text_contents()
    assert any(row.startswith(HOSTILE_PROVIDER + ' · ') for row in rows), rows
    assert tip.locator('b').count() == 0
    page.screenshot(path=str(SCREENSHOT.with_name('tooltip-all-providers-point.png')))
    badge.focus()
    expect(tip).to_contain_text('have no price')
    page.screenshot(path=str(SCREENSHOT.with_name('tooltip-unpriced-badge.png')))
    assert not errors, errors


def test_chart_tooltip():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
            headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            page = browser.new_page(viewport={'width': 1480, 'height': 900}, locale='en-GB', timezone_id='Europe/Paris')
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
            page.evaluate('window.demoPeakValue=2;window.demoPeakUnmatched=1')
            token_colour = lambda token: page.evaluate(PROBE, token)
            page.evaluate('''() => {const original=rest;rest=async (...args)=>{
                const result=await original(...args);
                if(args[0].startsWith('/ledger?'))window.tooltipData=result;
                return result;
            }}''')
            page.get_by_role('navigation', name='Providers', exact=True).get_by_role('tab', name='Codex', exact=True).click()
            page.wait_for_function('window.demoCalls.filter(p=>p.startsWith("/ledger?")).length >= 2')
            page.wait_for_function('window.tooltipData?.trend?.buckets?.at(-1)?.missing_usage === 1')
            chart = page.get_by_test_id('usage-chart')
            # The approved responsive layout (099060f) compacts the card to
            # 'Peak N · partial'; the full explanation lives in its title.
            card_peak = page.get_by_test_id('subagent-summary').locator('span', has_text=re.compile(r'^Peak '))
            expect(card_peak).to_have_text(re.compile(r'^Peak 2 · partial · '))
            expect(card_peak).to_have_attribute('title', re.compile(r'^[\d.]+% of known tokens · \S+\nPeak subagents: 2\nMost distinct child agents running at once in this window\nCounts matched start/stop pairs only · 1 unmatched event left out', re.S))
            page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name='Model').click()
            expect(page.get_by_role('columnheader', name='Peak subagents')).to_be_visible()
            page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name='Subagents').click()
            expect(page.get_by_role('columnheader', name='Peak subagents')).to_have_count(0)
            page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name='Hour').click()
            plot = chart.locator('.au-plot-hit')
            page.locator('.au-upper').evaluate('e=>e.scrollTop=150')
            # The final synthetic bucket has one request with unknown usage.
            # Read its actual DTO, rather than manufacturing a production date.
            def point(index):
                box = plot.bounding_box()
                assert box
                fraction = page.evaluate('''index => {
                    const rows=window.tooltipData.trend.buckets;
                    return ((rows[index].start+rows[index].end)/2-rows[0].start)/
                        (rows.at(-1).end-rows[0].start);
                }''', index)
                return box['x'] + box['width'] * fraction, box['y'] + box['height'] / 2

            rows = page.evaluate('window.tooltipData.trend.buckets')
            known = next(i for i, row in enumerate(rows) if row['attempts'] and not row['missing_usage'])
            missing = len(rows) - 1
            before = page.evaluate('window.demoCalls.filter(p=>p.startsWith("/ledger?")).length')
            page.mouse.move(*point(known))
            popup = page.locator('.au-tooltip[role="tooltip"]:visible')
            footer = page.get_by_test_id('chart-point-summary')
            expect(footer).to_be_visible()
            known_expected = page.evaluate('''index => {
                const b=window.tooltipData.trend.buckets[index];
                return new Date(b.start*1000).toLocaleString()+' '+new Intl.DateTimeFormat(undefined,{timeZoneName:'short'}).formatToParts(new Date(b.start*1000)).find(p=>p.type==='timeZoneName').value+
                    '\\n'+b.known.total_tokens.toLocaleString(undefined)+
                    ' tokens · '+b.attempts.toLocaleString(undefined)+(b.attempts===1?' request':' requests')+'\\nPeak subagents: 2 · partial';
            }''', known)
            expect(footer).to_have_text(known_expected)
            # Local zone, not UTC: the page runs in Europe/Paris (CEST in
            # summer, CET in winter; the bucket date follows the wall clock).
            assert ' UTC' not in known_expected and any(z in known_expected for z in ('CEST', 'CET', 'GMT+1', 'GMT+2')), known_expected
            assert 'Click the chart, then use ←/→ to step through buckets' in popup.inner_text()
            assert 'Drag 15 minutes or more to zoom' in popup.inner_text()
            assert popup.evaluate('e=>e.lastElementChild.dataset.testid') == 'chart-point-summary'
            assert chart.locator('.au-chart-tip').inner_text() == ''
            assert footer.inner_text() not in chart.inner_text()
            assert footer.evaluate('e=>getComputedStyle(e).color') == chart.locator('.au-line').first.evaluate('e=>getComputedStyle(e).stroke')
            assert plot.get_attribute('aria-describedby') == popup.get_attribute('id')

            page.mouse.move(*point(missing))
            expected = page.evaluate('''() => {
                const b=window.tooltipData.trend.buckets.at(-1);
                return new Date(b.start*1000).toLocaleString()+' '+new Intl.DateTimeFormat(undefined,{timeZoneName:'short'}).formatToParts(new Date(b.start*1000)).find(p=>p.type==='timeZoneName').value+
                    '\\n— tokens · '+b.attempts.toLocaleString(undefined)+
                    (b.attempts===1?' request':' requests')+'\\nIncludes missing usage\\nPeak subagents: 2 · partial';
            }''')
            expect(footer).to_have_text(expected)
            # One fact per line; the missing-usage warning uses the warning token.
            assert footer.locator('.au-tip-line').all_text_contents() == expected.split('\n')
            warn_line = footer.locator('.au-tip-warn')
            assert warn_line.all_text_contents() == ['Includes missing usage']
            assert warn_line.evaluate("e=>getComputedStyle(e).color") == token_colour('--ui-text-warning')
            assert chart.locator('.au-chart-tip').inner_text() == ''
            page.mouse.click(*point(missing))
            expect(chart).to_be_focused()
            expect(footer).to_have_text(expected)
            assert chart.get_attribute('aria-describedby') == popup.get_attribute('id')
            chart.press('ArrowLeft')
            expect(footer).not_to_have_text(expected)
            assert footer.text_content() == page.evaluate("tipPlain(document.querySelector('.au-chart').getAttribute('data-au-chart-summary'))")
            chart.press('ArrowRight')
            expect(footer).to_have_text(expected)
            page.evaluate("document.documentElement.style.setProperty('--ui-accent','#3b9f84')")
            assert footer.evaluate('e=>getComputedStyle(e).color') == 'rgb(59, 159, 132)'
            assert footer.evaluate('e=>getComputedStyle(e).color') == chart.locator('.au-line').first.evaluate('e=>getComputedStyle(e).stroke')
            assert 'Drag 15 minutes or more to zoom' in popup.inner_text()
            page.screenshot(path=str(SCREENSHOT))
            page.evaluate("document.documentElement.style.removeProperty('--ui-accent')")
            assert footer.evaluate('e=>getComputedStyle(e).color') == chart.locator('.au-line').first.evaluate('e=>getComputedStyle(e).stroke')
            chart.evaluate("e=>e.closest('.au-ledger').style.setProperty('--ui-accent','#9a622f')")
            assert footer.evaluate('e=>getComputedStyle(e).color') == 'rgb(154, 98, 47)'
            chart.evaluate("e=>e.closest('.au-ledger').style.removeProperty('--ui-accent')")
            # Pointer capture while dragging must keep the selected point and
            # instructions visible; cancellation must not submit a new range.
            page.mouse.move(*point(missing))
            page.mouse.down()
            page.mouse.move(*point(known), steps=4)
            expect(chart.locator('.au-plot-selection')).to_be_visible()
            expect(footer).to_have_text(known_expected)
            assert 'Drag 15 minutes or more to zoom' in popup.inner_text()
            plot.dispatch_event('pointercancel', {'pointerId': 1})
            page.mouse.up()
            expect(chart.locator('.au-plot-selection')).to_have_count(0)
            page.set_viewport_size({'width': 480, 'height': 700})
            chart.focus()
            chart.press('ArrowRight')
            expect(footer).to_be_visible()
            assert popup.evaluate('e=>{const b=e.getBoundingClientRect();return b.left>=0&&b.right<=innerWidth&&b.top>=0&&b.bottom<=innerHeight}')
            assert page.evaluate('window.demoCalls.filter(p=>p.startsWith("/ledger?")).length') == before
            page.set_viewport_size({'width': 1480, 'height': 900})
            page.get_by_role('group', name='Usage display').get_by_role('button', name='Cost', exact=True).click()
            page.locator('.au-upper').evaluate('e=>e.scrollTop=150')
            page.mouse.move(*point(missing))
            expect(footer).to_contain_text('Partial price coverage · ')
            assert 'includes missing usage' not in footer.inner_text()
            assert chart.locator('.au-chart-tip').inner_text() == ''
            assert not errors, errors
            # Older peak shape above (no running/open_ended): no open-child lines.
            assert 'still running' not in footer.inner_text() and 'without a stop record' not in footer.inner_text()
            assert 'Peak subagents counts start/stop pairs and children still open' in popup.inner_text()
            # Counted open children add one plain line each to every peak tooltip.
            page.evaluate('''() => {window.demoPeakRunning=9;window.demoPeakOpenEnded=2;
              window.demoChange++;for(const s of window.demoSubscribers)s({type:'changed',mode:'native-events'})}''')
            open_lines = ['9 still running', '2 ended without a stop record (counted until their last request)']
            expect(card_peak).to_have_attribute('title', re.compile(
                r'\nPeak subagents: 2\nMost distinct child agents running at once in this window\n'
                r'Counts matched start/stop pairs · 1 unmatched event left out\n' + re.escape('\n'.join(open_lines)) +
                r'\nMissing hooks can make this an undercount\n'))
            expect(card_peak).to_have_text(re.compile(r'^Peak 2 · partial · '))
            page.wait_for_function('window.tooltipData?.subagent_summary?.peak_observed?.running === 9')
            page.locator('.au-upper').evaluate('e=>e.scrollTop=150')
            page.mouse.move(*point(missing))
            expect(footer).to_contain_text('9 still running')
            lines = footer.locator('.au-tip-line').all_text_contents()
            assert lines[-3:] == ['Peak subagents: 2 · partial', *open_lines], lines
            assert footer.locator('.au-tip-line').nth(len(lines) - 1).locator('[class*="au-tip-"]').count() == 0
            assert footer.locator('.au-tip-line').nth(len(lines) - 2).locator('[class*="au-tip-"]').count() == 0
            assert footer.evaluate('e=>getComputedStyle(e).color') == chart.locator('.au-line').first.evaluate('e=>getComputedStyle(e).stroke')
            page.evaluate('window.demoPeakRunning=null;window.demoPeakOpenEnded=null')
            assert not errors, errors
            print('PASS peak open children: running/stop-less lines on card and chart footer; older shape adds none')
            structured_copy(page, errors)
            print('PASS structured tooltip copy: lines, theme-token tones, hostile names escaped, plain accessible description')
            print('PASS synthetic chart tooltip: exact unknown summary, absent pane text, focus/hover/keyboard, instructions, live theme, no fetch')
            print('Screenshot:', SCREENSHOT)
        finally:
            browser.close()


if __name__ == '__main__':
    test_chart_tooltip()
