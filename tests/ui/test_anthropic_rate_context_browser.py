"""Cache & costs Context cell for direct Anthropic rates: synthetic preview fixture only.

Checks that US-only 1.1x, global, inferred fast and recorded assumptions are
distinguishable in the existing Context text/tooltip, while non-Anthropic
rates keep their previous labels. No provider, account or ledger access.
"""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]

FIXTURE = r'''() => {
 events=[];
 const base={provider:'anthropic',input_tokens:'4',output_tokens:'20',cache_read_tokens:'.2',cache_write_tokens:'5',
   cache_write_1h_tokens:'8',source:'SYNTHETIC Anthropic public pricing',long_context_flat:true,max_prompt_tokens:1000000,
   endpoint_assumption:false,geo_assumption:false,tier_assumption:false,speed_inferred:false,speed_basis:'usage.speed'};
 const add=(id,age,rate,extra={})=>{
  const usage={input_tokens:100,output_tokens:10,cache_read_tokens:0,cache_write_tokens:0,reasoning_tokens:0,prompt_tokens:100,total_tokens:110,usage_source:'SYNTHETIC TEST',request_count:1};
  events.push({id,provider:rate.provider,model:rate.model,response_model:rate.model,service_tier:rate.service_tier,started:now-age,ended:now-age+1,
   session_id:'anth-rate-session',task:'main',agent_kind:'primary',status:'completed',usage,...extra,
   cost:{components:{input_tokens:0,output_tokens:0,cache_read_tokens:0,cache_write_tokens:0},total_usd:0,known_components_usd:0,complete:true,rate,
   cache_read_savings_usd:0,cache_write_premium_usd:0,cache_savings_usd:0}});
 };
 add('global',60,{...base,model:'claude-a-global',service_tier:'standard',inference_geo:'global'});
 add('us',61,{...base,model:'claude-b-us',service_tier:'standard',inference_geo:'us',input_tokens:'4.4',output_tokens:'22'});
 add('assumed',62,{...base,model:'claude-c-assumed',service_tier:'standard',inference_geo:'global',geo_assumption:true,
   endpoint_assumption:true,tier_assumption:true,speed_inferred:true,speed_basis:'assumed standard speed (no fast request)'});
 add('inferred',63,{...base,model:'claude-d-fast',service_tier:'fast',inference_geo:'global',speed_inferred:true,
   speed_basis:'requested fast; completed fast requests report fast'});
 add('reported',64,{...base,model:'claude-e-fast',service_tier:'fast',inference_geo:'us'});
 // Pre-existing saved rate lacking speed_inferred: derived from speed_basis.
 const legacy={...base,model:'claude-f-legacy',service_tier:'fast',inference_geo:'global',speed_basis:'requested fast; completed fast requests report fast'};
 delete legacy.speed_inferred;add('legacy',65,legacy);
 add('haiku',66,{...base,model:'claude-g-haiku',service_tier:'standard',inference_geo:'global',long_context_flat:false,max_prompt_tokens:200000});
 add('oai',67,{provider:'anthropic',model:'claude-h-plain',service_tier:'standard',input_tokens:'1',output_tokens:'2',source:'SYNTHETIC plain'});
 queryClient.invalidateQueries();
}'''

EXPECTED = {
    'claude-a-global': 'Global',
    'claude-b-us': 'US-only 1.1×',
    'claude-c-assumed': 'Global · assumed geo/endpoint/standard speed',
    'claude-d-fast': 'Global · fast inferred',
    'claude-e-fast': 'US-only 1.1×',
    'claude-f-legacy': 'Global · fast inferred',
    'claude-g-haiku': 'Global',
    'claude-h-plain': 'Standard',   # a rate without Anthropic geo metadata keeps the previous label
}
TOOLTIP = {
    'claude-a-global': ['Global routing.', 'Endpoint recorded as api.anthropic.com.', 'Standard speed reported by usage.speed.'],
    'claude-b-us': ['US-only inference: published 1.1× data-residency rate'],
    'claude-c-assumed': ['Global routing assumed', 'Endpoint not recorded; api.anthropic.com assumed.', 'Standard speed assumed'],
    'claude-d-fast': ['Fast speed inferred from the request; the response did not report usage.speed.'],
    'claude-e-fast': ['Fast speed reported by usage.speed.'],
    'claude-g-haiku': ['Priced up to 200,000 prompt tokens.'],
}


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'), headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1700, 'height': 1050}, locale='en-US')
        page.set_default_timeout(8000)
        errors, network = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: network.append(r.url))
        html = (ROOT / 'preview.html').read_text()
        assert 'const demoContexts={' in html
        # Attach a synthetic anthropic provider card, as the provider-colour script does.
        extra = "demoProviders.push({id:'anthropic',label:'anthropic',configured:true,quota:{available:false,windows:[],details:[]}});\n"
        page.set_content(html.replace('const demoContexts={', extra + 'const demoContexts={', 1), wait_until='domcontentloaded')
        page.evaluate(FIXTURE)
        page.get_by_role('navigation', name='Providers', exact=True).get_by_role('tab', name='anthropic', exact=True).click()
        page.get_by_role('navigation', name='Provider subpages', exact=True).get_by_role('tab', name='Cache & costs', exact=True).click()
        rates = page.get_by_test_id('published-rates')
        expect(rates.locator('tbody tr')).to_have_count(len(EXPECTED))
        headers = rates.locator('thead th').all_text_contents()
        assert headers[:5] == ['Provider', 'Exact model', 'Tier', 'Context', 'Requests'], headers
        for row in rates.locator('tbody tr').all():
            cells = row.locator('td')
            model = cells.nth(1).inner_text().strip()
            context = cells.nth(3)
            text = context.inner_text().strip()
            assert text == EXPECTED[model], (model, text)
            span = context.locator('span[title]')
            if model == 'claude-h-plain':
                assert span.count() == 0, 'non-Anthropic-geo rate gained a tooltip'
                continue
            title = span.first.get_attribute('title') or span.first.get_attribute('data-au-tooltip') or ''
            assert title.startswith('Direct Anthropic API list rate.'), (model, title)
            for fragment in TOOLTIP.get(model, []):
                assert fragment in title, (model, fragment, title)
        us, glob = [rates.locator('tbody tr', has_text=m).locator('td').nth(3).inner_text() for m in ('claude-b-us', 'claude-a-global')]
        assert us != glob
        # Shared themed tooltip surface still owns the title on hover.
        target = rates.locator('tbody tr', has_text='claude-d-fast').locator('td').nth(3).locator('span[title]').first
        target.hover()
        page.wait_for_timeout(150)
        bubble = page.locator('[role="tooltip"]').filter(has_text='Fast speed inferred')
        assert bubble.count() >= 1, 'shared tooltip did not show the rate note'
        assert not errors, errors
        assert all(u.startswith(('about:', 'data:', 'blob:')) for u in network), network
        browser.close()
    print('PASS Anthropic Context labels: global/US 1.1× distinct, inferred fast + assumptions marked, plain rate unchanged, shared tooltip')


if __name__ == '__main__':
    run()
