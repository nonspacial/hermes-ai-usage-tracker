"""Offline rendered named-project badge regression; no live plugin or data."""
import os
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
                                    headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width':1500, 'height':1100})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
        page.evaluate("""() => {
            for (const e of events) if (e.project_id === 'repo-hermes') {
                e.project_label = 'Hermes Agent'; e.project_source = 'named_project';
            }
            queryClient.invalidateQueries();
        }""")
        page.get_by_role('navigation', name='Providers', exact=True).get_by_role('tab', name='Codex', exact=True).click()
        page.get_by_role('navigation', name='Provider subpages', exact=True).get_by_role('tab', name='Overview', exact=True).click()
        page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name='Project', exact=True).click()
        named = page.locator('.au-breakdown tbody button').filter(has_text='Hermes Agent')
        expect(named).to_have_count(1)
        expect(named.locator('small')).to_have_text('Named project')
        page.locator('.au-breakdown').screenshot(path=str(ROOT / 'tests/ui/artifacts/named-project.png'))
        assert not errors, errors
        browser.close()
        print('PASS canonical project label and Named project badge rendered once')


if __name__ == '__main__':
    run()
