"""Offline check: Anthropic Fable week bar and All-providers hover '% left'.

Synthetic preview only (``fixtureAnthropicFable`` in all_profiles_fixture.js);
no account, quota endpoint or ledger is read. Screenshots go to
``$TMPDIR/fable-bar`` as fixture evidence, not product images.
"""
import os
from pathlib import Path
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SCRATCH = Path(os.environ.get('TMPDIR', '/tmp')) / 'fable-bar'
CHROME = os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium')
END = '// END ALL PROFILES OFFLINE FIXTURES\n'
CODEX_TWO = ("demoProviders[0].quota.windows=[{label:'Session',used_percent:30,remaining_percent:70,"
             "reset_at:new Date((now+3600)*1000).toISOString()},{label:'Weekly',used_percent:1,remaining_percent:99,"
             "reset_at:new Date((now+6*86400)*1000).toISOString()}];\n")


def html(fable=True, extra=''):
    text = (ROOT / 'preview.html').read_text()
    assert END in text
    return text.replace(END, END + CODEX_TWO + f'fixtureAnthropicFable({{fable:{str(fable).lower()}{extra}}});\n', 1)


ROWS = '''() => {
  const out={};
  for(const row of document.querySelectorAll('.au-provider-row')){
    const block=row.querySelector('[data-testid="provider-row-quota"]');
    if(!block)continue;
    const b=block.getBoundingClientRect();
    out[row.dataset.provider]={layout:block.dataset.layout||null,width:b.width,
      windows:[...block.querySelectorAll('.au-row-quota-window')].map(w=>{const r=w.getBoundingClientRect();
        return {label:w.querySelector('.au-row-quota-label').textContent,window:w.dataset.window,title:w.getAttribute('title'),
          left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height,
          track:w.querySelector('.au-row-quota-track').getBoundingClientRect().height,
          fill:w.querySelector('.au-row-quota-fill').style.width}})};
  }
  return out;
}'''


def open_page(browser, fable=True, size=(1920, 1080), extra=''):
    page = browser.new_page(viewport={'width': size[0], 'height': size[1]})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.route('**/*', lambda route: route.abort())
    page.set_content(html(fable, extra), wait_until='domcontentloaded')
    page.get_by_role('navigation', name='Providers', exact=True).get_by_role('tab', name='All providers').click()
    expect(page.locator('.au-provider-row[data-provider="anthropic"] [data-testid="provider-row-quota"]')).to_be_visible()
    return page, errors


def check_b1(rows, key):
    a = rows['anthropic']
    assert a['layout'] == 'stacked', (key, a)
    assert [w['label'] for w in a['windows']] == ['Five hour', 'Weekly', 'Fable'], (key, a)
    five, week, fable = a['windows']
    # Row 1: five hour, full block width. Row 2: Weekly and Fable, half each, same row.
    assert abs(five['width'] - a['width']) <= 1, (key, a)
    assert week['top'] >= five['bottom'] - 0.5 and abs(week['top'] - fable['top']) <= 0.5, (key, a)
    assert abs(week['width'] - fable['width']) <= 1 and week['right'] <= fable['left'] + 0.5, (key, a)
    assert abs(week['width'] + fable['width'] + 8 - a['width']) <= 1.5, (key, a)
    assert week['width'] < a['width'] * 0.55, (key, a)
    assert [w['fill'] for w in a['windows']] == ['62%', '55%', '85%'], (key, a)
    # Other providers keep one row of equal side-by-side bars.
    c = rows['openai-codex']
    assert c['layout'] is None and [w['label'] for w in c['windows']] == ['Five hour', 'Weekly'], (key, c)
    assert abs(c['windows'][0]['top'] - c['windows'][1]['top']) <= 0.5, (key, c)
    assert abs(c['windows'][0]['width'] - c['windows'][1]['width']) <= 1, (key, c)


def test_fable_bar():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROME, headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            page, errors = open_page(browser)
            page.evaluate('window.demoCalls.length=0')
            for size in ((1920, 1080), (850, 925), (850, 750), (390, 850)):
                page.set_viewport_size({'width': size[0], 'height': size[1]})
                page.wait_for_timeout(150)
                check_b1(page.evaluate(ROWS), size)
            page.set_viewport_size({'width': 1920, 'height': 1080})
            page.wait_for_timeout(150)
            page.mouse.move(1916, 1076)
            page.wait_for_timeout(100)
            page.screenshot(path=str(SCRATCH / 'all-providers.png'))
            page.locator('.au-hero-total').screenshot(path=str(SCRATCH / 'all-providers-tiles.png'))

            # B2: every provider tile bar shows '<n>% left' on hover only; no size change.
            tip = page.locator('body > .au-tooltip')
            before = page.evaluate(ROWS)
            expected = {'anthropic': ['62% left', '55% left', '85% left'], 'openai-codex': ['70% left', '99% left']}
            for provider, texts in expected.items():
                bars = page.locator(f'.au-provider-row[data-provider="{provider}"] .au-row-quota-window')
                assert bars.count() == len(texts)
                for index, text in enumerate(texts):
                    page.mouse.move(1916, 1076)
                    expect(tip).to_be_hidden()
                    bars.nth(index).locator('.au-row-quota-track').hover(force=True)
                    expect(tip).to_be_visible()
                    expect(tip).to_have_text(text)
                    assert bars.nth(index).get_attribute('data-au-tooltip') == text
                    during = page.evaluate(ROWS)[provider]['windows'][index]
                    was = before[provider]['windows'][index]
                    assert (during['width'], during['height'], during['track']) == (was['width'], was['height'], was['track']), (during, was)
                    if provider == 'anthropic' and index == 2:
                        page.screenshot(path=str(SCRATCH / 'all-providers-fable-hover.png'))
            page.mouse.move(1916, 1076)
            expect(tip).to_be_hidden()
            restored = page.evaluate(ROWS)
            assert [w['title'] for w in restored['anthropic']['windows']] == expected['anthropic'], restored
            # No extra quota request for layout or hover.
            assert not [c for c in page.evaluate('window.demoCalls') if 'usage' in c and 'ledger' not in c]

            # B3: Subscriptions card and Anthropic provider page list the third window.
            nav = page.get_by_role('navigation', name='Providers', exact=True)
            nav.get_by_role('tab', name='Subscriptions').click()
            card = page.locator('.au-quota-home .au-quota-card').filter(has_text='Anthropic')
            expect(card).to_be_visible()
            labels = card.locator('.au-quota-rows span.w-28').all_inner_texts()
            assert labels == ['Current session', 'Current week', 'Fable week'], labels
            assert card.locator('.au-quota-rows .h-full').count() == 3
            assert '85% left' in card.locator('.au-quota-rows').inner_text()
            page.mouse.move(1916, 1076)
            page.wait_for_timeout(100)
            page.screenshot(path=str(SCRATCH / 'subscriptions.png'))
            card.screenshot(path=str(SCRATCH / 'subscriptions-anthropic-card.png'))
            nav.get_by_role('tab', name='Anthropic', exact=True).click()
            card = page.locator('.au-provider-limits .au-quota-card')
            expect(card).to_be_visible()
            assert card.locator('.au-quota-rows span.w-28').all_inner_texts() == ['Current session', 'Current week', 'Fable week']
            assert card.locator('.au-quota-rows .h-full').count() == 3
            card.screenshot(path=str(SCRATCH / 'anthropic-provider-card.png'))
            assert not errors, errors
            page.close()

            # Without a Fable window the Anthropic tile keeps today's single row.
            page, errors = open_page(browser, fable=False)
            a = page.evaluate(ROWS)['anthropic']
            assert a['layout'] is None and [w['label'] for w in a['windows']] == ['Five hour', 'Weekly'], a
            assert abs(a['windows'][0]['top'] - a['windows'][1]['top']) <= 0.5, a
            assert abs(a['windows'][0]['width'] - a['windows'][1]['width']) <= 1, a
            assert not errors, errors
            page.close()
            check_stale_and_unavailable(browser)
            print('PASS: Anthropic tile five hour full width + Weekly/Fable halves (1920/850x925/850x750/390), '
                  'Codex unchanged, no-Fable fallback single row; hover "% left" tooltip on every tile bar, '
                  'hidden off-hover, no size change; Subscriptions and Anthropic page list Fable week third; '
                  'stale Fable muted with stale suffix + explanation tooltip in row and both cards, stacked kept; '
                  'unavailable reason in card details, no extra bar; stale low/watch levels show no alarm badge; '
                  'state none adds no line')
            print('Screenshots in', SCRATCH)
        finally:
            browser.close()


STYLE = '''(sel) => [...document.querySelectorAll(sel)].map(e => {
  const probe=document.createElement('i');e.append(probe);
  const colour=css=>{probe.style.color=css;return getComputedStyle(probe).color};
  const fill=e.querySelector('.au-row-quota-fill, .h-full');
  const out={stale:e.dataset.stale||null,fill:getComputedStyle(fill).backgroundColor,
    muted:colour('var(--ui-text-quaternary)'),accent:colour(providerAccent('anthropic')),
    text:e.textContent,title:e.getAttribute('title')||e.getAttribute('data-au-tooltip')};
  probe.remove();return out;})'''
STALE_NOTE = 'Model-scoped weekly limit could not be refreshed (timed out); showing the value observed at '


def check_stale_and_unavailable(browser):
    # Stale: the backend carried the last known-good Fable week (limits_status.state='stale').
    page, errors = open_page(browser, extra=',stale:true')
    observed = page.evaluate("fmtLocal(new Date((now-1800)*1000).toISOString())")
    assert observed and 'Invalid' not in observed, observed
    for size in ((1920, 1080), (850, 925), (390, 850)):
        page.set_viewport_size({'width': size[0], 'height': size[1]})
        page.wait_for_timeout(150)
        a = page.evaluate(ROWS)['anthropic']
        # Stacked trigger still fires: compact label is still 'Fable' (+ ' stale' suffix span).
        assert a['layout'] == 'stacked', (size, a)
        assert [w['window'] for w in a['windows']] == ['Current session', 'Current week', 'Fable week'], a
        assert [w['label'] for w in a['windows']] == ['Five hour', 'Weekly', 'Fable stale'], a
        five, week, fable = a['windows']
        assert abs(five['width'] - a['width']) <= 1 and abs(week['top'] - fable['top']) <= 0.5, (size, a)
        assert abs(week['width'] - fable['width']) <= 1, (size, a)
        assert fable['fill'] == '85%', a
    page.set_viewport_size({'width': 1920, 'height': 1080})
    page.wait_for_timeout(150)
    bars = page.evaluate(STYLE, '.au-provider-row[data-provider="anthropic"] .au-row-quota-window')
    assert [b['stale'] for b in bars] == [None, None, 'true'], bars
    assert bars[2]['fill'] == bars[2]['muted'] != bars[2]['accent'], bars
    assert bars[0]['fill'] == bars[0]['accent'] and bars[1]['fill'] == bars[1]['accent'], bars
    assert bars[2]['title'] == f'85% left · stale. {STALE_NOTE}{observed}', bars[2]['title']
    meters = page.locator('.au-provider-row[data-provider="anthropic"] .au-row-quota-track[role="meter"]')
    assert [meters.nth(i).get_attribute('aria-label') for i in range(3)] == [
        'Five hour allowance remaining', 'Weekly allowance remaining', 'Fable allowance remaining (stale)']
    tip = page.locator('body > .au-tooltip')
    row_fable = page.locator('.au-provider-row[data-provider="anthropic"] .au-row-quota-window[data-stale="true"]')
    row_fable.locator('.au-row-quota-track').hover(force=True)
    expect(tip).to_be_visible()
    expect(tip).to_have_text(f'85% left · stale. {STALE_NOTE}{observed}')
    page.screenshot(path=str(SCRATCH / 'stale-fable-row-hover.png'))
    page.mouse.move(1916, 1076)
    expect(tip).to_be_hidden()

    nav = page.get_by_role('navigation', name='Providers', exact=True)
    for tab, selector in (('Subscriptions', '.au-quota-home .au-quota-card'), ('Anthropic', '.au-provider-limits .au-quota-card')):
        nav.get_by_role('tab', name=tab, exact=True).click()
        card = page.locator(selector).filter(has_text='Anthropic')
        expect(card).to_be_visible()
        assert card.locator('.au-quota-rows span.w-28').all_inner_texts() == ['Current session', 'Current week', 'Fable week']
        rows = page.evaluate(STYLE, f'{selector} .au-quota-rows > .flex-col')
        rows = [r for r in rows if 'Current session' in r['text'] or 'week' in r['text']]
        assert [r['stale'] for r in rows] == [None, None, 'true'], rows
        assert rows[2]['fill'] == rows[2]['muted'] != rows[2]['accent'] and rows[0]['fill'] == rows[0]['accent'], rows
        assert rows[2]['title'] == STALE_NOTE + observed, rows[2]
        assert card.locator('.au-quota-stale').all_inner_texts() == ['stale']
        assert '85% left' in rows[2]['text']
        assert card.locator('[data-stale="true"]').get_by_text('low', exact=True).count() == 0
        stale_row = card.locator('.au-quota-rows > [data-stale="true"]')
        # The explanation is also in-flow, visually hidden text (no hover needed):
        # exposed to the accessibility tree, clipped to 1px, title tooltip kept.
        hidden = stale_row.locator('.au-sr-only')
        expect(hidden).to_have_count(1)
        expect(hidden).to_have_text(STALE_NOTE + observed + '.')
        geometry = hidden.evaluate('''e => {const s=getComputedStyle(e),r=e.getBoundingClientRect();
          return {position:s.position,clip:s.clip,overflow:s.overflow,width:r.width,height:r.height,
                  hidden:e.closest('[aria-hidden="true"]')!==null}}''')
        assert geometry['position'] == 'absolute' and geometry['overflow'] == 'hidden', geometry
        assert geometry['clip'] == 'rect(0px, 0px, 0px, 0px)' and geometry['width'] <= 1 and geometry['height'] <= 1, geometry
        assert not geometry['hidden'], geometry
        assert STALE_NOTE + observed in stale_row.aria_snapshot(), stale_row.aria_snapshot()
        assert stale_row.get_attribute('title') == STALE_NOTE + observed
        stale_row.hover()
        expect(tip).to_be_visible()
        expect(tip).to_have_text(STALE_NOTE + observed)
        card.screenshot(path=str(SCRATCH / f'stale-fable-{tab.lower()}-card.png'))
        page.mouse.move(1916, 1076)
        expect(tip).to_be_hidden()
        # No reason line in details when a window was carried.
        assert 'Model-scoped weekly limit unavailable' not in card.inner_text()
    assert not errors, errors
    page.close()

    # Unavailable, nothing carried: no extra bar; the reason joins the card details only.
    page, errors = open_page(browser, extra=",unavailable:'http_429'")
    a = page.evaluate(ROWS)['anthropic']
    assert a['layout'] is None and [w['label'] for w in a['windows']] == ['Five hour', 'Weekly'], a
    assert 'unavailable' not in page.locator('.au-provider-row[data-provider="anthropic"]').inner_text().lower()
    nav = page.get_by_role('navigation', name='Providers', exact=True)
    for tab, selector in (('Subscriptions', '.au-quota-home .au-quota-card'), ('Anthropic', '.au-provider-limits .au-quota-card')):
        nav.get_by_role('tab', name=tab, exact=True).click()
        card = page.locator(selector).filter(has_text='Anthropic')
        expect(card).to_be_visible()
        assert card.locator('.au-quota-rows span.w-28').all_inner_texts() == ['Current session', 'Current week']
        assert card.locator('.au-quota-rows .h-full').count() == 2 and card.locator('[data-stale]').count() == 0
        detail = card.locator('.au-quota-rows > div', has_text='Model-scoped weekly limit unavailable')
        expect(detail).to_have_count(1)
        expect(detail).to_have_text('Model-scoped weekly limit unavailable (HTTP 429).')
        assert 'text-(--ui-text-quaternary)' in detail.get_attribute('class')
        card.screenshot(path=str(SCRATCH / f'unavailable-fable-{tab.lower()}-card.png'))
    assert not errors, errors
    page.close()

    # A stale window at an alarming level keeps the muted tone: no low/watch badge.
    for left, badge in ((4, 'low'), (20, 'watch')):
        page, errors = open_page(browser, extra=f',stale:true,fableLeft:{left}')
        nav = page.get_by_role('navigation', name='Providers', exact=True)
        nav.get_by_role('tab', name='Subscriptions', exact=True).click()
        card = page.locator('.au-quota-home .au-quota-card').filter(has_text='Anthropic')
        expect(card).to_be_visible()
        stale_row = card.locator('.au-quota-rows > [data-stale="true"]')
        expect(stale_row).to_have_count(1)
        expect(stale_row).to_contain_text(f'{left}% left')
        assert stale_row.get_by_text(badge, exact=True).count() == 0
        assert stale_row.get_by_text('low', exact=True).count() == 0 and stale_row.get_by_text('watch', exact=True).count() == 0
        assert not errors, errors
        page.close()

    # Authoritative empty answer and never a known-good read (state 'none'): no line, no bar.
    page, errors = open_page(browser, extra=",unavailable:'empty',state:'none'")
    nav = page.get_by_role('navigation', name='Providers', exact=True)
    for tab, selector in (('Subscriptions', '.au-quota-home .au-quota-card'), ('Anthropic', '.au-provider-limits .au-quota-card')):
        nav.get_by_role('tab', name=tab, exact=True).click()
        card = page.locator(selector).filter(has_text='Anthropic')
        expect(card).to_be_visible()
        assert card.locator('.au-quota-rows span.w-28').all_inner_texts() == ['Current session', 'Current week']
        assert 'Model-scoped weekly limit' not in card.inner_text()
    assert not errors, errors
    page.close()


if __name__ == '__main__':
    test_fable_bar()
