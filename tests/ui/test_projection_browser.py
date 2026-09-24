"""Committed view/group owns its payload; exports still ask for full snapshots."""
import os
from pathlib import Path
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1700, 'height': 1100})
        errors, outbound = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: outbound.append(request.url))
        page.route('**/*', lambda route: route.abort())
        page.set_content((ROOT / 'preview.html').read_text())
        page.get_by_role('tab', name='All providers', exact=True).click()
        expect(page.locator('.au-breakdown tbody tr').first).to_be_visible()
        page.evaluate('''()=>{
          window.heldViews=[];window.projectionPaths=[];
          const original=rest;
          rest=async(path,options)=>{
            const u=new URL(path,'https://offline');
            if(u.pathname==='/ledger' && u.searchParams.has('view')){
              projectionPaths.push(path);
              if(window.holdProjection)return new Promise(resolve=>heldViews.push({path,deliver:async()=>resolve(await original(path,options))}));
            }
            return original(path,options);
          };
          window.downloads=[];downloadFile=(name,body)=>downloads.push({name,body});
        }''')
        assert page.evaluate("new URL(demoCalls.filter(path=>path.startsWith('/ledger?')).at(-1),'https://offline').searchParams.get('group')") == 'model'
        page.evaluate('window.holdProjection=true')
        page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name='Project').click()
        expect(page.get_by_test_id('ledger-view-pending')).to_contain_text('Overview · project')
        expect(page.locator('.au-breakdown tbody tr')).to_have_count(0)
        expect(page.get_by_test_id('usage-totals')).to_be_visible()  # shared same-scope summary
        assert page.evaluate("new URL(projectionPaths.at(-1),'https://offline').searchParams.get('group')") == 'project'
        page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name='Session').click()
        expect(page.get_by_test_id('ledger-view-pending')).to_contain_text('Overview · session')
        page.wait_for_function('heldViews.length>=2')
        page.get_by_role('tab', name='Requests', exact=True).click()
        expect(page.get_by_test_id('ledger-view-pending')).to_contain_text('Requests')
        page.wait_for_function('heldViews.length>=3')
        page.evaluate('heldViews[0].deliver();heldViews[1].deliver()')  # older groups finish after tab changes
        expect(page.get_by_test_id('request-list')).to_have_count(0)
        expect(page.get_by_test_id('ledger-view-pending')).to_contain_text('Requests')
        page.evaluate('heldViews[2].deliver();window.holdProjection=false')
        expect(page.get_by_test_id('request-list')).to_be_visible()
        # Returning to a previously served view must not restore its unlabelled
        # old rows. An older response arriving after the return is also inert.
        page.evaluate('window.holdProjection=true')
        page.get_by_role('tab', name='Overview', exact=True).click()
        page.wait_for_function('heldViews.length>=4')
        page.get_by_role('tab', name='Requests', exact=True).click()
        expect(page.get_by_test_id('ledger-view-pending')).to_contain_text('Requests')
        expect(page.get_by_test_id('request-list')).to_have_count(0)
        expect(page.get_by_test_id('usage-totals')).to_be_visible()
        page.wait_for_function('heldViews.length>=5')
        page.evaluate('heldViews[3].deliver()')
        expect(page.get_by_test_id('ledger-view-pending')).to_contain_text('Requests')
        page.evaluate('heldViews[4].deliver();window.holdProjection=false')
        expect(page.get_by_test_id('request-list')).to_be_visible()
        request_query = page.evaluate("Object.fromEntries(new URL(projectionPaths.at(-1),'https://offline').searchParams)")
        assert request_query['view'] == 'requests' and 'group' not in request_query
        # Unlike a new commitment, a routine same-view refresh retains the
        # already-served request body while the next response is outstanding.
        page.evaluate('window.holdProjection=true;window.demoChange++;for(const fn of demoSubscribers)fn({type:"changed",mode:"native-events"})')
        page.wait_for_function('heldViews.length>=6')
        expect(page.get_by_test_id('request-list')).to_be_visible()
        expect(page.get_by_test_id('ledger-view-pending')).to_have_count(0)
        page.evaluate('heldViews[5].deliver();window.holdProjection=false')
        for tab, view, target in [('Cache & costs', 'cache', 'component-cost-cards'),
                                  ('Compressions', 'compressions', 'compression-events'),
                                  ('Models & tasks', 'models', None),
                                  ('Skills usage', 'skills', 'skills-usage')]:
            page.get_by_role('tab', name=tab, exact=True).click()
            expect(page.get_by_test_id('provider-subpage')).to_have_attribute('data-subpage', tab)
            page.wait_for_function('''view=>new URL(projectionPaths.at(-1),'https://offline').searchParams.get('view')===view''', arg=view)
            if target:
                expect(page.get_by_test_id(target)).to_be_visible()
            else:
                expect(page.get_by_test_id('provider-subpage').locator('table tbody tr').first).to_be_visible()
        # Exercise sparse DTOs from the actual offline transport, not a full
        # fixture silently reused behind a projected query.
        shape = page.evaluate('''async()=>{
          const expected=['requests','cache_read_progression','price_catalogs','applied_rate_groups',
           'compressions','compression_truncated','groups','model_groups','project_groups',
           'session_groups','subagent_groups','agent_groups','health','rates','quota_observations',
           'crossing_start','crossing_end'];
          const check=async(extra,view,group)=>{
            const p=new URLSearchParams(extra);p.set('view',view);if(group)p.set('group',group);
            const dto=await demoRest('/ledger?'+p);
            const {included,omitted,version}=dto.projection;
            if(version!==1||included.some(k=>!Object.hasOwn(dto,k))||omitted.some(k=>Object.hasOwn(dto,k))||
               expected.some(k=>!included.includes(k)&&!omitted.includes(k)))throw new Error('Not a sparse v1 DTO');
            return dto;
          };
          const selected={};for(const [view,group] of [['overview','model'],['overview','time'],
           ['overview','project'],['overview','session'],['overview','subagent'],['requests'],
           ['cache'],['compressions'],['models'],['skills']]){
             selected[view+':'+(group||'')]=await check('profile=infra',view,group);
          }
          const all=await check('profile_scope=all','cache');
          demoAggregateCoverage='unavailable';
          const unknown=await check('profile_scope=all','cache');
          demoAggregateCoverage='partial';
          const partial=await check('profile_scope=all','requests');
          demoAggregateCoverage='complete';
          const full=await demoRest('/ledger?profile=infra');
          return {selected:Object.fromEntries(Object.entries(selected).map(([k,v])=>
             [k,{included:v.projection.included,omitted:v.projection.omitted}])),
           all:all.projection,unknown:{summary:unknown.summary,request_count:unknown.request_count,
             compression_count:unknown.compression_count,coverage:unknown.coverage.status},
           partial:{coverage:partial.coverage.status,summary:partial.summary},
           full:{projection:full.projection,requests:full.requests.length,price_catalogs:full.price_catalogs.length}};
        }''')
        assert shape['selected']['requests:']['included'] != shape['selected']['cache:']['included']
        assert 'price_catalogs' in shape['selected']['cache:']['included']
        assert 'price_catalogs' in shape['selected']['requests:']['omitted']
        for group, field in [('model', 'model_groups'), ('project', 'project_groups'),
                             ('session', 'session_groups'), ('subagent', 'subagent_groups')]:
            assert field in shape['selected']['overview:'+group]['included']
            assert field in shape['selected']['overview:time']['omitted']
        for view, field in [('requests', 'requests'), ('cache', 'applied_rate_groups'),
                            ('compressions', 'compression_truncated'), ('models', 'groups')]:
            assert field in shape['selected'][view+':']['included']
            assert field in shape['selected']['skills:']['omitted']
        assert shape['unknown'] == {'summary': None, 'request_count': None,
                                    'compression_count': None, 'coverage': 'unavailable'}
        assert shape['partial']['coverage'] == 'partial' and shape['partial']['summary'] is not None
        assert shape['full']['projection'] is None and shape['full']['requests'] and shape['full']['price_catalogs']
        page.get_by_role('button', name='Export request CSV', exact=True).click()
        page.wait_for_function('downloads.length===1')
        paths = page.evaluate("scopeCalls.filter(c=>c.path.startsWith('/ledger?')&&new URL(c.path,'https://offline').searchParams.get('limit')==='2000').map(c=>c.path)")
        assert paths and all('view=' not in path and 'group=' not in path for path in paths)
        assert not errors, errors
        assert not outbound, outbound
        browser.close()
    print('PASS sparse v1 views/aggregate unknowns, rapid return/late responses, same-view refresh, full CSV')


if __name__ == '__main__':
    run()
