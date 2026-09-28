"""Offline browser contract for the loud unpriced signal (synthetic preview only).

Fully priced groups show nothing; a group with unpriced requests shows a
compact count marker whose reasons ride on the shared tooltip, and the pane
header shows 'N tokens unpriced' beside the freshness badge. Adjacent cells
keep their size and style.

The Cache & costs Context column and its tooltip read the rate's *_basis /
pricing_basis: one synthetic row per basis (fixture demoPricingBasisRows)
must show its exact label and truthful sentence. Banded (≤/>N) and
minimum-prompt (≥N) rates keep the band text and append the assumption.
"""
import os
import re
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
SHOTS = Path(os.environ.get('TMPDIR', '/tmp')) / 'pricing-coverage'

MIX = """() => {
  let n = 0;
  for (const e of events) {
    if (e.provider !== 'openrouter' || !e.cost) continue;
    n++;
    e.cost = {...e.cost, complete: false, rate: null, total_usd: null, known_components_usd: 0,
              components: {input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null}};
    e.unpriced_reason = n % 4 ? 'model_missing' : 'no_catalogue';
  }
  window.demoChange++;
  for (const s of window.demoSubscribers) s({type: 'changed', mode: 'native-events'});
  return n;
}"""


def settle(page):
    page.evaluate('() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>requestAnimationFrame(r))))')


def cells(table):
    return table.locator('tbody tr').evaluate_all("""rows => rows.map(r => [...r.children].map((td, i) => {
      const b = td.getBoundingClientRect(), s = getComputedStyle(td), v = td.querySelector('.au-field-value') || td;
      const cs = getComputedStyle(v);
      return [Math.round(b.height), i === 1 ? null : Math.round(b.width), s.paddingTop, s.fontSize, cs.color, cs.fontWeight];
    }))""")


def run():
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1700, 'height': 1100})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', lambda route: route.abort())
        page.set_content((ROOT / 'preview.html').read_text())
        page.get_by_role('tab', name='All providers', exact=True).click()
        page.get_by_role('navigation', name='Provider subpages').get_by_role('tab', name='Overview', exact=True).click()
        page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name='Model', exact=True).click()
        table = page.locator('.au-breakdown .au-table')
        expect(table.locator('tbody tr')).not_to_have_count(0)
        # The synthetic preview holds one pending request (no usage yet): its
        # group is marked, the header badge stays absent (0 tokens unpriced)
        # and fully priced groups show nothing. No bare '*' anywhere.
        expect(page.get_by_test_id('unpriced-badge')).to_have_count(0)
        pending = table.get_by_test_id('unpriced-marker')
        expect(pending).to_have_count(1)
        expect(table.locator('tbody tr').filter(has=page.get_by_test_id('unpriced-marker'))).to_contain_text('astra-demo')
        pending.hover()
        expect(page.locator('body > .au-tooltip:visible')).to_contain_text('usage not reported yet')
        page.mouse.move(0, 0)
        settle(page)
        before = cells(table)
        header_before = page.locator('.au-connection').bounding_box()
        assert not any(t.strip().endswith('*') for t in table.locator('tbody td').all_text_contents())

        mixed = page.evaluate(MIX)
        assert mixed > 0
        expect(table.get_by_test_id('unpriced-marker')).to_have_count(2)
        row = table.locator('tbody tr', has_text='router-demo')
        marker = row.get_by_test_id('unpriced-marker')
        expect(marker).to_have_count(1)
        # Count = the group's unpriced requests in the window (events outside it are not counted).
        shown = re.fullmatch(' ⚠\uFE0E([\\d,]+)', marker.inner_text())
        assert shown and 0 < int(shown[1].replace(',', '')) <= mixed, marker.inner_text()
        assert marker.get_attribute('aria-label') == shown[1] + ' unpriced requests'
        assert '*' not in row.inner_text()
        # Fully priced rows: nothing after the amount.
        for other in table.locator('tbody tr').all():
            text = other.inner_text()
            if 'router-demo' not in text and 'astra-demo' not in text:
                assert other.get_by_test_id('unpriced-marker').count() == 0, text
        # Reason on hover through the shared tooltip.
        marker.hover()
        tip = page.locator('body > .au-tooltip:visible')
        expect(tip).to_contain_text(shown[1] + ' requests unpriced')
        expect(tip).to_contain_text('model not in the provider or OpenRouter catalogue')
        expect(tip).to_contain_text('no price catalogue for this provider yet')
        # Pane badge beside the freshness badge, with the top reasons on hover.
        badge = page.get_by_test_id('unpriced-badge')
        expect(badge).to_be_visible()
        assert re.fullmatch(r'[\d.,]+[KMB]? tokens unpriced', badge.inner_text()), badge.inner_text()
        assert badge.evaluate("e => e.nextElementSibling?.classList.contains('au-connection')")
        badge.hover()
        expect(tip).to_contain_text('OpenRouter · router-demo')
        expect(tip).to_contain_text('tokens from')
        page.mouse.move(0, 0)
        settle(page)
        # No size or style change to adjacent cells or the freshness badge.
        after = cells(table)
        assert [[c[:1] + c[2:] for c in r] for r in after] == [[c[:1] + c[2:] for c in r] for r in before], 'cell style changed'
        assert [[c[1] for c in r] for r in after] == [[c[1] for c in r] for r in before], 'adjacent cell width changed'
        freshness = page.locator('.au-connection').bounding_box()
        assert (freshness['width'], freshness['height']) == (header_before['width'], header_before['height'])
        colour = marker.evaluate('e => getComputedStyle(e).color')
        warn = page.evaluate("""() => { const s = document.createElement('span'); s.style.color = 'var(--ui-text-warning,#d2b776)';
            document.querySelector('.au-ledger').appendChild(s); const c = getComputedStyle(s).color; s.remove(); return c; }""")
        assert colour == warn, (colour, warn)
        page.locator('.au-page-header').screenshot(path=str(SHOTS / 'pane-unpriced-badge.png'))
        page.locator('.au-breakdown').screenshot(path=str(SHOTS / 'overview-model-table-mixed.png'))
        # Provider grouping carries the same marker.
        page.get_by_role('tab', name='OpenRouter', exact=True).click()
        page.get_by_role('navigation', name='Provider subpages').get_by_role('tab', name='Overview', exact=True).click()
        expect(page.get_by_test_id('unpriced-marker').first).to_be_visible()
        assert not errors, errors
        pricing_basis_context(page, errors)
        browser.close()
    print('PASS unpriced marker text/tooltip, pane badge present/absent and placement, no adjacent-cell change; screenshots in', SHOTS)



def pricing_basis_context(page, errors):
    """Each *_basis gets its label and its own truthful tooltip sentence."""
    expected = page.evaluate('() => window.demoPricingBasisRows()')
    page.get_by_role('tab', name='OpenRouter', exact=True).click()
    page.get_by_role('navigation', name='Provider subpages').get_by_role('tab', name='Cache & costs', exact=True).click()
    rates = page.get_by_test_id('published-rates')
    expect(rates.locator('tbody tr').first).to_be_visible()
    view_all = page.get_by_role('button', name='View all records', exact=True)
    if view_all.count():                              # more basis rows than one ten-record page
        view_all.first.click()
    expect(rates.locator('tbody tr', has_text='basis-tier')).to_have_count(1)
    by_model = {r.locator('td').nth(1).inner_text().strip(): r for r in rates.locator('tbody tr').all()}
    seen = set()
    for model, (text, *phrases) in expected.items():
        context = by_model[model].locator('td').nth(3)
        assert context.inner_text().strip() == text, (model, context.inner_text(), text)
        if not phrases:                              # plain band: no assumption, no note
            assert context.locator('span[title]').count() == 0, model
            seen.add(model)
            continue
        span = context.locator('span[title]').first
        title = span.get_attribute('title') or span.get_attribute('data-au-tooltip') or ''
        for phrase in phrases:
            assert phrase in title, (model, phrase, title)
        # 'no ... reported' only when the value was truly absent.
        if model != 'basis-absent':
            assert 'no usage.inference_geo was reported' not in title and 'no usage.speed reported' not in title, (model, title)
        seen.add(model)
    assert seen == set(expected)
    tip = page.locator('body > .au-tooltip:visible')
    for model, name in (('basis-tier', 'context-assumed-tier.png'), ('basis-list', 'context-list-price.png')):
        row = by_model[model]
        row.locator('td').nth(3).locator('span[title], span[data-au-tooltip]').first.hover()
        expect(tip).to_contain_text(expected[model][1][:40])
        box = row.bounding_box()
        tb = tip.bounding_box()
        x0, y0 = min(box['x'], tb['x']) - 8, min(box['y'], tb['y']) - 8
        x1, y1 = max(box['x'] + box['width'], tb['x'] + tb['width']) + 8, max(box['y'] + box['height'], tb['y'] + tb['height']) + 8
        page.screenshot(path=str(SHOTS / name), clip={'x': max(0, x0), 'y': max(0, y0), 'width': x1 - max(0, x0), 'height': y1 - max(0, y0)})
        page.mouse.move(0, 0)
    assert not errors, errors


if __name__ == '__main__':
    run()
