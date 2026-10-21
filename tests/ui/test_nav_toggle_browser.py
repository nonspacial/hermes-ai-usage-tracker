"""A second click on the AI usage + sidebar row returns to the view the user left."""
from pathlib import Path
import os
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]

# Synthetic stand-in for the host's sidebar nav row: the real row always
# navigates to its route on click (bubble phase), as `selectSidebarItem` does.
FAKE_ROW = '''() => {
  window.hostRowClicks=0;window.pluginNavs=[];
  host.navigate=p=>{window.pluginNavs.push(p);location.hash='#'+p};
  const row=document.createElement('button');row.id='fake-nav-row';
  row.innerHTML='<span data-tour="sidebar-nav-ai-usage-tracker:nav">AI usage +</span>';
  row.addEventListener('click',()=>{window.hostRowClicks++;location.hash='#/ai-usage'});
  const other=document.createElement('button');other.id='fake-other-row';
  other.innerHTML='<span data-tour="sidebar-nav-artifacts">Artifacts</span>';
  other.addEventListener('click',()=>{location.hash='#/artifacts'});
  document.body.prepend(row,other);
}'''


def state(page):
    return page.evaluate('() => ({hash:location.hash,host:window.hostRowClicks,navs:window.pluginNavs.slice()})')


def run():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
            headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            page = browser.new_page(viewport={'width': 1150, 'height': 850})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
            page.evaluate(FAKE_ROW)
            page.evaluate("() => { location.hash = '#/session-a' }")
            # First click: host opens the page, plugin only remembers the session.
            page.click('#fake-nav-row')
            s = state(page)
            assert s == {'hash': '#/ai-usage', 'host': 1, 'navs': []}, s
            # Second click (on the inner label too): plugin returns to the session,
            # host handler does not re-open the page.
            page.click('#fake-nav-row span')
            s = state(page)
            assert s == {'hash': '#/session-a', 'host': 1, 'navs': ['/session-a']}, s
            # Toggle again from a different origin view.
            page.click('#fake-other-row')
            page.click('#fake-nav-row')
            page.click('#fake-nav-row')
            s = state(page)
            assert s['hash'] == '#/artifacts' and s['host'] == 2 and s['navs'][-1] == '/artifacts', s
            # Other rows are untouched; modified clicks fall through to the host.
            page.click('#fake-nav-row')
            page.click('#fake-nav-row', modifiers=['Control'])
            s = state(page)
            assert s['hash'] == '#/ai-usage' and s['host'] == 4 and len(s['navs']) == 2, s
            # Opening from the status-bar chip also remembers the view it left.
            page.evaluate('''() => {
              const chip=document.createElement('button');chip.id='fake-chip';chip.className='au-usage-chip';
              chip.innerHTML='<span>62% · Codex</span>';
              chip.addEventListener('click',()=>host.navigate('/ai-usage'));
              document.body.prepend(chip);location.hash='#/cron';
            }''')
            page.click('#fake-chip span')
            page.click('#fake-nav-row')
            s = state(page)
            assert s['hash'] == '#/cron' and s['host'] == 4 and s['navs'][-2:] == ['/ai-usage', '/cron'], s
            # Plugin disposal removes the capture listener.
            page.evaluate('() => window.disposePlugin()')
            page.click('#fake-nav-row')
            page.click('#fake-nav-row')
            s = state(page)
            assert s['host'] == 6 and len(s['navs']) == 4, ('disposed listener still active', s)
            assert not errors, errors
            print('PASS sidebar row toggles AI usage + closed, returns to the previous view, disposes cleanly')
        finally:
            browser.close()


if __name__ == '__main__':
    run()
