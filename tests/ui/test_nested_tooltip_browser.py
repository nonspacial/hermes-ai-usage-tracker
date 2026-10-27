"""Offline browser contract: nested titles never raise a native tooltip over the shared bubble.

While the shared themed bubble shows for an element, every ancestor up to the
pane root keeps no non-empty `title` (and no SVG `<title>` text), so the
browser cannot draw an ancestor's native tooltip on top. All titles return
exactly on mouseout/blur/target change/unmount/dispose; a newer value written
meanwhile (as React would) wins over the held one. Synthetic preview only.
"""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]

# Same synthetic unpriced OpenRouter rows as test_unpriced_signal_browser.py,
# keeping the originals so the marker can be unmounted again.
MIX = """() => {
  window.nestedSaved = [];
  for (const e of events) {
    if (e.provider !== 'openrouter' || !e.cost) continue;
    window.nestedSaved.push([e, e.cost, e.unpriced_reason]);
    e.cost = {...e.cost, complete: false, rate: null, total_usd: null, known_components_usd: 0,
              components: {input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null}};
    e.unpriced_reason = 'model_missing';
  }
  window.demoChange++;
  for (const s of window.demoSubscribers) s({type: 'changed', mode: 'native-events'});
  return window.nestedSaved.length;
}"""
UNMIX = """() => {
  for (const [e, cost, reason] of window.nestedSaved) { e.cost = cost; if (reason === undefined) delete e.unpriced_reason; else e.unpriced_reason = reason; }
  window.demoChange++;
  for (const s of window.demoSubscribers) s({type: 'changed', mode: 'native-events'});
}"""
# Titles of the element and every ancestor up to the pane root.
CHAIN = """(e, rootSel) => { const root = document.querySelector(rootSel), out = [];
  for (let n = e; n && root.contains(n); n = n.parentElement) {
    const svg = n.namespaceURI === 'http://www.w3.org/2000/svg' ? n.querySelector(':scope > title') : null;
    out.push({tag: n.tagName, testid: n.getAttribute('data-testid'), title: n.getAttribute('title'),
              svg: svg ? svg.textContent : null, held: n.getAttribute('data-au-tooltip')});
  }
  return out; }"""
LEAKS = """rootSel => [...document.querySelector(rootSel).querySelectorAll('[data-au-tooltip]')].length"""


def settle(page):
    page.evaluate('() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>requestAnimationFrame(r))))')


def chain(locator, root='.au-ledger'):
    return locator.evaluate(CHAIN, root)


def assert_suppressed(entries, where):
    titled = [e for e in entries if e['title'] or e['svg']]
    assert not titled, (where, 'native title still present while bubble shows', titled)


def published_rates(page, tip):
    rates = page.get_by_test_id('published-rates')
    marker = rates.get_by_test_id('unpriced-marker').first
    expect(marker).to_be_visible()
    resting = chain(marker)
    section = next(e for e in resting if e['testid'] == 'published-rates')
    assert section['title'].startswith('Rates in USD per 1M tokens'), section
    assert sum(1 for e in resting if e['title']) >= 2, resting      # genuinely nested
    # Nested marker: only its own bubble, no ancestor native title.
    marker.hover()
    expect(tip).to_contain_text('unpriced')
    expect(tip).not_to_contain_text('Rates in USD per 1M tokens')
    assert_suppressed(chain(marker), 'published-rates marker')
    page.mouse.move(0, 0)
    expect(tip).to_be_hidden()
    assert chain(marker) == resting, 'titles not restored after mouseout'
    assert page.evaluate(LEAKS, '.au-ledger') == 0
    # Ancestor-only area still shows the ancestor's own bubble, and moving on
    # to the nested marker swaps to the marker without restoring the ancestor.
    row = marker.locator('xpath=ancestor::tr[1]')
    plain = row.locator('td').nth(4)
    assert plain.get_attribute('title') is None and plain.locator('[title]').count() == 0
    plain.hover()
    expect(tip).to_contain_text('Rates in USD per 1M tokens')
    assert rates.get_attribute('title') is None and rates.get_attribute('data-au-tooltip').startswith('Rates in USD')
    marker.hover()
    expect(tip).to_contain_text('unpriced')
    expect(tip).not_to_contain_text('Rates in USD per 1M tokens')
    assert_suppressed(chain(marker), 'ancestor → marker')
    plain.hover()
    expect(tip).to_contain_text('Rates in USD per 1M tokens')
    assert_suppressed(chain(plain), 'marker → ancestor')
    page.mouse.move(0, 0)
    expect(tip).to_be_hidden()
    assert chain(marker) == resting
    # A newer ancestor title written during display (as a React re-render
    # would) is suppressed too and survives the restore.
    marker.hover()
    expect(tip).to_contain_text('unpriced')
    rates.evaluate("e => e.setAttribute('title', 'Newer section title')")
    page.wait_for_function("() => !document.querySelector('[data-testid=published-rates]').hasAttribute('title')")
    assert_suppressed(chain(marker), 'after ancestor re-render')
    page.mouse.move(0, 0)
    expect(tip).to_be_hidden()
    assert rates.get_attribute('title') == 'Newer section title'
    rates.evaluate("(e, t) => e.setAttribute('title', t)", section['title'])
    # The hovered marker unmounting (now fully priced) drops its bubble; the
    # browser's synthetic mouseover may hand the bubble to the surviving
    # section, which then stays silent itself, and mouseout restores it.
    marker.hover()
    expect(tip).to_contain_text('unpriced')
    page.evaluate(UNMIX)
    expect(rates.get_by_test_id('unpriced-marker')).to_have_count(0)
    expect(tip).not_to_contain_text('unpriced')
    if tip.count():
        expect(tip).to_contain_text('Rates in USD per 1M tokens')
        assert rates.get_attribute('title') is None
    page.mouse.move(0, 0)
    expect(tip).to_be_hidden()
    assert rates.get_attribute('title') == section['title']
    assert page.evaluate(LEAKS, '.au-ledger') == 0


def other_nested_sites(page, tip):
    """Every nested pane site found in the preview: metric notes (three levels) and the chart hit area."""
    page.get_by_role('navigation', name='Provider subpages').get_by_role('tab', name='Overview', exact=True).click()
    sites = page.evaluate("""() => { const root = document.querySelector('.au-ledger'), found = [];
      const titled = n => n.getAttribute('title') || (n.namespaceURI === 'http://www.w3.org/2000/svg' && n.querySelector(':scope > title')?.textContent);
      for (const n of root.querySelectorAll('*')) {
        if (n.tagName === 'title' || n.tagName === 'OPTION' || !titled(n) || !n.getClientRects().length) continue;
        let depth = 0; for (let a = n.parentElement; a && root.contains(a); a = a.parentElement) if (titled(a)) depth++;
        if (depth) { n.setAttribute('data-nested-probe', String(found.length)); found.push(depth); }
      }
      return found; }""")
    assert sites and max(sites) >= 2, sites
    for index in range(len(sites)):
        node = page.locator(f'[data-nested-probe="{index}"]')
        resting = chain(node)
        node.hover(force=True)
        expect(tip).to_be_visible()
        assert_suppressed(chain(node), f'nested site {index}: {resting}')
        page.mouse.move(0, 0)
        expect(tip).to_be_hidden()
        assert chain(node) == resting, ('not restored', index, resting, chain(node))
    assert page.evaluate(LEAKS, '.au-ledger') == 0
    page.evaluate("() => document.querySelectorAll('[data-nested-probe]').forEach(n => n.removeAttribute('data-nested-probe'))")
    return len(sites)


SYNTHETIC = """() => {
  const host = document.createElement('div'); host.id = 'nested-host'; host.title = 'Root title';
  host.innerHTML = '<section id="nested-section" title="Section title"><p id="nested-plain">plain</p>'
    + '<button id="nested-button" title="Button title">B <span id="nested-inner">inner</span></button>'
    + '<svg width="80" height="40"><g id="nested-g"><title>Group title</title>'
    + '<rect id="nested-rect" width="40" height="40"><title>Rect title</title></rect></g></svg></section>';
  document.body.appendChild(host);
  window.nestedDispose = installTooltips(host);
}"""


def synthetic_root(page, tip):
    """installTooltips directly: SVG ancestor <title>, keyboard focus and dispose."""
    page.evaluate(SYNTHETIC)
    tip = page.locator('body > .au-tooltip:visible')
    resting = chain(page.locator('#nested-rect'), '#nested-host')
    page.locator('#nested-rect').hover()
    expect(tip).to_have_text('Rect title')
    assert_suppressed(chain(page.locator('#nested-rect'), '#nested-host'), 'svg rect in titled <g>')
    page.locator('#nested-plain').hover()
    expect(tip).to_have_text('Section title')
    assert_suppressed(chain(page.locator('#nested-plain'), '#nested-host'), 'plain in section')
    # An untitled child of the owner keeps the owner's bubble, not an ancestor's.
    page.locator('#nested-button').hover(position={'x': 2, 'y': 2})
    expect(tip).to_have_text('Button title')
    page.locator('#nested-inner').hover()
    expect(tip).to_have_text('Button title')
    assert_suppressed(chain(page.locator('#nested-inner'), '#nested-host'), 'owner child')
    page.mouse.move(0, 0)
    expect(tip).to_be_hidden()
    assert chain(page.locator('#nested-rect'), '#nested-host') == resting
    # Keyboard focus behaves the same as hover.
    button = page.locator('#nested-button')
    button_rest = chain(button, '#nested-host')
    button.focus()
    expect(tip).to_have_text('Button title')
    assert_suppressed(chain(button, '#nested-host'), 'focused button')
    page.evaluate('document.activeElement.blur()')
    expect(tip).to_be_hidden()
    assert chain(button, '#nested-host') == button_rest
    # Dispose while shown restores everything and leaves no marker attributes.
    page.locator('#nested-rect').hover()
    expect(tip).to_have_text('Rect title')
    page.evaluate('window.nestedDispose()')
    assert chain(page.locator('#nested-rect'), '#nested-host') == resting
    assert page.evaluate(LEAKS, '#nested-host') == 0
    page.evaluate("document.getElementById('nested-host').remove()")


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1700, 'height': 1100})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', lambda route: route.abort())
        page.set_content((ROOT / 'preview.html').read_text())
        tip = page.locator('body > .au-tooltip:visible')
        page.get_by_role('tab', name='All providers', exact=True).click()
        assert page.evaluate(MIX) > 0
        page.get_by_role('tab', name='OpenRouter', exact=True).click()
        page.get_by_role('navigation', name='Provider subpages').get_by_role('tab', name='Cache & costs', exact=True).click()
        expect(page.get_by_test_id('published-rates').locator('tbody tr').first).to_be_visible()
        published_rates(page, tip)
        count = other_nested_sites(page, tip)
        synthetic_root(page, tip)
        assert not errors, errors
        browser.close()
    print(f'PASS nested titles: published-rates marker/ancestor/re-render/unmount, {count} other nested pane sites, '
          'SVG ancestor <title>, keyboard focus and dispose restore')


if __name__ == '__main__':
    run()
