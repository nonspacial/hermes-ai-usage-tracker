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


def html(fable=True):
    text = (ROOT / 'preview.html').read_text()
    assert END in text
    return text.replace(END, END + CODEX_TWO + f'fixtureAnthropicFable({{fable:{str(fable).lower()}}});\n', 1)


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


def open_page(browser, fable=True, size=(1920, 1080)):
    page = browser.new_page(viewport={'width': size[0], 'height': size[1]})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.route('**/*', lambda route: route.abort())
    page.set_content(html(fable), wait_until='domcontentloaded')
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
            print('PASS: Anthropic tile five hour full width + Weekly/Fable halves (1920/850x925/850x750/390), '
                  'Codex unchanged, no-Fable fallback single row; hover "% left" tooltip on every tile bar, '
                  'hidden off-hover, no size change; Subscriptions and Anthropic page list Fable week third')
            print('Screenshots in', SCRATCH)
        finally:
            browser.close()


if __name__ == '__main__':
    test_fable_bar()
