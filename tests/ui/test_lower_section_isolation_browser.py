"""Lower tab/group switches keep the upper hero painted; top-level changes repaint it.

Offline preview only (synthetic fixture). A committed lower tab or Overview
breakdown group is a new projected `/ledger` read, but under an unchanged
semantic scope (connection, profile, provider, period/custom bounds, filters)
the upper summary hero must stay painted from the previous validated response's
shared header while only the lower section shows its own pending state.
"""
import os
from pathlib import Path
from urllib.parse import parse_qsl

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]

# Installs a held projected-read transport and an upper-section mutation probe.
# The probe records any committed DOM state in which the hero is missing or the
# summary shows its pending/unavailable status, and any removal of hero nodes.
INSTALL = '''()=>{
  window.held=[];window.projected=[];window.hold=false;
  const original=rest;
  rest=(path,options)=>{
    const u=new URL(path,'https://offline');
    if(u.pathname==='/ledger'&&u.searchParams.has('view')&&(options?.method||'GET')==='GET'){
      projected.push(path);
      if(hold)return new Promise((resolve,reject)=>held.push({path,deliver:()=>original(path,options).then(resolve,reject)}));
    }
    return original(path,options);
  };
  window.heroEvents=[];
  const upper=()=>document.querySelector('.au-upper');
  const blank=()=>{
    const u=upper();if(!u)return 'no-upper';
    if(!u.querySelector('[data-testid=usage-hero]'))return 'no-hero';
    if(u.querySelector('[data-testid=recorded-summary][role=status]'))return 'summary-pending';
    return null;
  };
  window.heroObserver=new MutationObserver(records=>{
    for(const r of records)for(const n of r.removedNodes){
      if(n.nodeType===1&&(n.matches?.('[data-testid=usage-hero],[data-testid=usage-totals],[data-testid=usage-chart]')||
         n.querySelector?.('[data-testid=usage-hero],[data-testid=usage-totals],[data-testid=usage-chart]')))
        heroEvents.push('removed');
    }
    const state=blank();if(state)heroEvents.push(state);
  });
  heroObserver.observe(document.body,{childList:true,subtree:true,characterData:true,attributes:true});
}'''

HERO = '''()=>{
  const u=document.querySelector('.au-upper');
  const q=s=>u.querySelector(s);
  return {
    totals:q('[data-testid=usage-totals]')?.innerText||null,
    total:q('.au-hero-total .au-big')?.innerText||null,
    providers:[...u.querySelectorAll('.au-provider-row')].map(n=>n.innerText),
    paths:[...u.querySelectorAll('[data-testid=usage-chart] svg path')].map(p=>p.getAttribute('d')),
    title:q('[data-testid=usage-chart] .au-chart-title')?.innerText||null,
    quality:q('.au-quality-line > span')?.innerText||null,
    pending:!!q('[data-testid=recorded-summary][role=status]'),
  };
}'''


def open_custom(page, start, end):
    period = page.get_by_role('group', name='Time window')
    if period.is_visible():
        period.get_by_role('button', name='Custom', exact=True).click()
    else:
        # Narrow panes expose the same period state through a select.
        page.get_by_role('combobox', name='Time window').select_option(label='Custom')
    page.evaluate("document.querySelectorAll('.au-custom-range').forEach(d=>{d.open=true})")
    page.get_by_label('Window start', exact=True).fill(start)
    page.get_by_label('Window end', exact=True).fill(end)


def settle(page):
    """Wait until the hero is painted and no projected read remains held."""
    expect(page.get_by_test_id('usage-hero')).to_be_visible()
    page.wait_for_function('!document.querySelector(".au-upper [data-testid=recorded-summary][role=status]")')
    page.wait_for_timeout(400)


def lower_switch(page, label, click, expect_view, expect_group, ready):
    """Hold the new projected read, check upper isolation, then deliver."""
    before = page.evaluate(HERO)
    assert not before['pending'] and before['totals'] and before['paths'], (label, before)
    reads = page.evaluate('projected.length')
    held = page.evaluate('held.length')
    page.evaluate('heroEvents.length=0;hold=true')
    click()
    page.wait_for_function('n=>held.length>n', arg=held)
    # While the projected read is outstanding the lower section is either
    # pending (new scope) or its own labelled same-scope cached entry (revisit).
    page.wait_for_timeout(250)
    lower = page.evaluate('''()=>{const s=document.querySelector('[data-testid=provider-subpage]');
      return s.querySelector('[data-testid=ledger-view-pending]')?'pending':'cached'}''')
    # No previous rows under the new label while pending.
    stale_rows = page.evaluate('''()=>{const s=document.querySelector('[data-testid=provider-subpage]');
      return s.querySelectorAll('.au-breakdown tbody tr,[data-testid=request-list] [data-scroll-key]').length}''')
    during = page.evaluate(HERO)
    events_during = page.evaluate('[...heroEvents]')
    new = page.evaluate('n=>projected.slice(n)', reads)
    page.evaluate('hold=false;for(const r of held.splice(0))r.deliver()')
    ready()
    page.wait_for_timeout(300)
    after_events = page.evaluate('[...heroEvents]')
    total_new = page.evaluate('n=>projected.slice(n)', reads)
    q = [dict(parse_qsl(p.split('?', 1)[1])) for p in total_new]
    return {'label': label, 'before': before, 'during': during, 'events': after_events, 'lower': lower,
            'stale_rows': stale_rows,
            'events_during': events_during, 'reads': q, 'reads_during': len(new),
            'expect_view': expect_view, 'expect_group': expect_group}


def check(result):
    label = result['label']
    problems = []
    b, d = result['before'], result['during']
    for key in ('totals', 'total', 'providers', 'paths', 'title', 'quality'):
        if b[key] != d[key]:
            problems.append(f'{key} changed during pending')
    if d['pending']:
        problems.append('summary pending during lower switch')
    if result['events']:
        problems.append('hero blank/removed events: ' + ','.join(sorted(set(result['events']))))
    reads = result['reads']
    # First visits of a view/group are pending; revisits show their own
    # labelled same-scope renderer-RAM entry at once.
    revisit = label.endswith('Requests->Overview') or label.endswith('group->Model')
    if result['lower'] != ('cached' if revisit else 'pending'):
        problems.append('lower section ' + result['lower'])
    if result['lower'] == 'pending' and result['stale_rows']:
        problems.append('previous rows shown under the new label')
    if len(reads) != 1:
        problems.append(f'{len(reads)} projected GETs (want exactly 1)')
    elif reads[0].get('view') != result['expect_view'] or reads[0].get('group') != result['expect_group']:
        problems.append(f'wrong read {reads[0].get("view")}/{reads[0].get("group")}')
    return label, problems


def scenario(page, name):
    sub = page.get_by_role('navigation', name='Provider subpages')
    groups = lambda: page.get_by_role('group', name='Breakdown grouping')
    rows = page.locator('.au-breakdown tbody tr')
    out = []
    settle(page)
    # Overview -> Requests -> Overview
    out.append(lower_switch(page, name + ': Overview->Requests',
                            lambda: sub.get_by_role('tab', name='Requests', exact=True).click(),
                            'requests', None, lambda: expect(page.get_by_test_id('request-list')).to_be_visible()))
    settle(page)
    out.append(lower_switch(page, name + ': Requests->Overview',
                            lambda: sub.get_by_role('tab', name='Overview', exact=True).click(),
                            'overview', None,
                            lambda: expect(rows.first).to_be_visible()))
    # The Overview group comes back from saved state; record the actual one.
    out[-1]['expect_group'] = out[-1]['reads'][0].get('group') if out[-1]['reads'] else None
    settle(page)
    buttons = groups().get_by_role('button')
    names = [buttons.nth(i).inner_text() for i in range(buttons.count())]
    time_label = [n for n in names if n not in ('Model', 'Project', 'Session', 'Subagents')][0]
    current = page.evaluate("new URL(projected.at(-1),'https://offline').searchParams.get('group')")
    order = [('Model', 'model'), (time_label, 'time'), ('Project', 'project')]
    if current == 'model':
        order = order[1:] + [('Model', 'model')]
    for text, group in order:
        out.append(lower_switch(page, f'{name}: group->{text}',
                                lambda text=text: groups().get_by_role('button', name=text, exact=True).click(),
                                'overview', group,
                                lambda: expect(page.locator('.au-breakdown tbody tr').first).to_be_visible()))
        settle(page)
    return out


def top_level_repaints(page):
    """A committed period change is a new semantic scope: the hero goes pending."""
    settle(page)
    before = page.evaluate(HERO)
    page.evaluate('heroEvents.length=0;hold=true')
    page.get_by_role('group', name='Time window').get_by_role('button', name='7 days', exact=True).click()
    page.wait_for_function('held.length>0')
    during = page.evaluate(HERO)
    events = page.evaluate('[...heroEvents]')
    page.evaluate('hold=false;for(const r of held.splice(0))r.deliver()')
    settle(page)
    after = page.evaluate(HERO)
    assert during['pending'] and not during['paths'], ('period change must not bridge the old hero', during)
    assert events, 'period change should blank the hero while its read is outstanding'
    assert after['paths'] and after['paths'] != before['paths'], 'hero must repaint for the new period'


NOTIFY = "demoChange++;for(const fn of demoSubscribers)fn({type:'changed',mode:'native-events'})"

# Wraps `/ledger/analytics`: `failAnalytics` rejects it (identity unvalidated),
# `holdAnalytics` queues it in `heldAnalytics` until released.
ANALYTICS = '''()=>{
  window.failAnalytics=false;window.holdAnalytics=false;window.heldAnalytics=[];
  const inner=rest;
  rest=(path,options)=>{
    if(new URL(path,'https://offline').pathname==='/ledger/analytics'){
      if(failAnalytics)return Promise.reject(new Error('synthetic analytics failure'));
      if(holdAnalytics)return new Promise((resolve,reject)=>heldAnalytics.push({deliver:()=>inner(path,options).then(resolve,reject)}));
    }
    return inner(path,options);
  };
}'''

# Marks synthetic OpenRouter rows unpriced (as test_unpriced_signal_browser.py).
UNPRICED = '''()=>{
  let n=0;
  for(const e of events){
    if(e.provider!=='openrouter'||!e.cost)continue;n++;
    e.cost={...e.cost,complete:false,rate:null,total_usd:null,known_components_usd:0,
      components:{input_tokens:null,output_tokens:null,cache_read_tokens:null,cache_write_tokens:null}};
    e.unpriced_reason='model_missing';
  }
  demoChange++;for(const fn of demoSubscribers)fn({type:'changed',mode:'native-events'});
  return n;
}'''


def fresh_page(browser, width=1800, height=1200):
    page = browser.new_page(viewport={'width': width, 'height': height})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/*', lambda route: route.abort())
    page.set_content((ROOT / 'preview.html').read_text())
    page.get_by_role('tab', name='All providers', exact=True).click()
    expect(page.locator('.au-breakdown tbody tr').first).to_be_visible()
    page.evaluate(INSTALL)
    page.evaluate(ANALYTICS)
    return page, errors


def group_button(page, name):
    return page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name=name, exact=True)


def notify_and_settle(page):
    n = page.evaluate('projected.length')
    page.evaluate(NOTIFY)
    page.wait_for_function('n=>projected.length>n', arg=n)
    settle(page)


def release(page):
    page.evaluate('hold=false;holdAnalytics=false;for(const r of held.splice(0))r.deliver();for(const r of heldAnalytics.splice(0))r.deliver()')


def unvalidated_header_never_bridges(browser):
    """A header stored while its own analytics check failed must not bridge a
    lower switch, above all once the backend revision has changed."""
    page, errors = fresh_page(browser)
    settle(page)
    group_button(page, 'Model').click()
    settle(page)
    page.evaluate('failAnalytics=true')
    notify_and_settle(page)
    stale = page.evaluate(HERO)
    assert stale['paths'] and stale['totals'], stale
    page.evaluate("failAnalytics=false;window.demoAnalyticsRevision='synthetic-analytics-r2';heroEvents.length=0;hold=true")
    group_button(page, 'Project').click()
    page.wait_for_function('held.length>0')
    page.wait_for_timeout(300)
    during = page.evaluate(HERO)
    release(page)
    settle(page)
    assert during['pending'], ('unvalidated header bridged', during)
    assert not during['paths'] and during['totals'] != stale['totals'], ('old totals/paths painted', during)
    assert not errors, errors
    page.close()


def revision_change_after_check_clears(browser):
    """A validated header bridges until the new owner's check, then clears on
    a changed analytics revision."""
    page, errors = fresh_page(browser)
    settle(page)
    group_button(page, 'Model').click()
    settle(page)
    before = page.evaluate(HERO)
    page.evaluate("window.demoAnalyticsRevision='synthetic-analytics-r3';heroEvents.length=0;hold=true;holdAnalytics=true")
    group_button(page, 'Project').click()
    page.wait_for_function('heldAnalytics.length>0')
    page.wait_for_timeout(250)
    unchecked = page.evaluate(HERO)
    page.evaluate('holdAnalytics=false;for(const r of heldAnalytics.splice(0))r.deliver()')
    page.wait_for_function('held.length>0')
    page.wait_for_timeout(250)
    checked = page.evaluate(HERO)
    release(page)
    settle(page)
    assert not unchecked['pending'] and unchecked['paths'] == before['paths'], ('validated header not bridged before check', unchecked)
    assert checked['pending'] and not checked['paths'], ('revision change after check kept the old hero', checked)
    assert not errors, errors
    page.close()


def older_revisit_keeps_newer_header(browser):
    """A labelled cached revisit (older seq) never replaces a newer header."""
    page, errors = fresh_page(browser)
    settle(page)
    group_button(page, 'Model').click()
    settle(page)
    old = page.evaluate(HERO)
    group_button(page, 'Project').click()
    settle(page)
    page.evaluate('addDemoEvent()')
    page.wait_for_function('t=>document.querySelector("[data-testid=usage-totals]")?.innerText!==t', arg=old['totals'])
    settle(page)
    newer = page.evaluate(HERO)
    assert newer['totals'] != old['totals'], 'fixture change did not alter totals'
    page.evaluate('heroEvents.length=0;hold=true')
    group_button(page, 'Model').click()
    page.wait_for_function('held.length>0')
    page.wait_for_timeout(250)
    lower = page.evaluate('''()=>document.querySelector('[data-testid=provider-subpage] [data-testid=ledger-view-pending]')?'pending':'cached\'''')
    during = page.evaluate(HERO)
    release(page)
    settle(page)
    assert lower == 'cached', ('expected a labelled cached revisit', lower)
    assert during['totals'] == newer['totals'] and during['paths'] == newer['paths'], ('older revisit replaced the newer header', during)
    assert not page.evaluate('[...heroEvents]'), page.evaluate('[...heroEvents]')
    assert not errors, errors
    page.close()


def fresh_lower_seq_replaces_header(browser):
    """A same-view notify read from the live owner always replaces the held
    header, even when its `seq` is lower (ledger restore/rollback keeps the
    analytics revision and inventory)."""
    page, errors = fresh_page(browser)
    settle(page)
    group_button(page, 'Model').click()
    settle(page)
    base = page.evaluate(HERO)
    page.evaluate('addDemoEvent()')
    page.wait_for_function('t=>document.querySelector("[data-testid=usage-totals]")?.innerText!==t', arg=base['totals'])
    settle(page)
    higher = page.evaluate(HERO)
    assert higher['totals'] != base['totals'], 'fixture change did not alter totals'
    # Roll the synthetic ledger back: the next same-view read has a lower seq.
    page.evaluate('events.pop()')
    notify_and_settle(page)
    page.wait_for_timeout(300)
    after = page.evaluate(HERO)
    assert after['totals'] == base['totals'] and after['totals'] != higher['totals'], \
        ('fresh lower-seq read did not replace the held header', after)
    assert not errors, errors
    page.close()


def seq_outranks_received(browser):
    """Ledger order beats renderer receipt time: a held header with a higher
    `seq` but an older `received` survives a cached revisit whose entry has a
    lower `seq` and a newer `received`."""
    page, errors = fresh_page(browser)
    settle(page)
    group_button(page, 'Model').click()
    settle(page)
    base = page.evaluate(HERO)
    group_button(page, 'Project').click()
    settle(page)
    page.evaluate('addDemoEvent()')
    page.wait_for_function('t=>document.querySelector("[data-testid=usage-totals]")?.innerText!==t', arg=base['totals'])
    settle(page)
    high = page.evaluate(HERO)  # Project entry: seq n+1, received r1.
    # Roll back, then read Model fresh: Model entry has seq n, received r2 > r1.
    page.evaluate('events.pop()')
    group_button(page, 'Model').click()
    page.wait_for_function('t=>document.querySelector("[data-testid=usage-totals]")?.innerText===t', arg=base['totals'])
    settle(page)
    # Cached Project revisit (seq n+1, older r1) takes the header back.
    page.evaluate('heroEvents.length=0;hold=true')
    group_button(page, 'Project').click()
    page.wait_for_function('held.length>0')
    page.wait_for_timeout(250)
    project = page.evaluate(HERO)
    # Cached Model revisit (seq n, newer r2) must not replace it.
    n = page.evaluate('held.length')
    group_button(page, 'Model').click()
    page.wait_for_function('n=>held.length>n', arg=n)
    page.wait_for_timeout(250)
    lower = page.evaluate('''()=>document.querySelector('[data-testid=provider-subpage] [data-testid=ledger-view-pending]')?'pending':'cached\'''')
    during = page.evaluate(HERO)
    release(page)
    page.wait_for_function('t=>document.querySelector("[data-testid=usage-totals]")?.innerText===t', arg=base['totals'])
    settle(page)
    assert project['totals'] == high['totals'], ('higher-seq cached revisit did not take the header', project)
    assert lower == 'cached', ('expected a labelled cached revisit', lower)
    assert during['totals'] == high['totals'] and during['paths'] == high['paths'], \
        ('newer received replaced a higher-seq header', during)
    assert not errors, errors
    page.close()


def unpriced_badge_persists(browser):
    """The header `tokens unpriced` badge crosses a lower switch."""
    page, errors = fresh_page(browser)
    settle(page)
    group_button(page, 'Model').click()
    settle(page)
    assert page.evaluate(UNPRICED) > 0
    badge = page.get_by_test_id('unpriced-badge')
    expect(badge).to_have_count(1)
    settle(page)
    text = badge.inner_text()
    page.evaluate('hold=true')
    group_button(page, 'Project').click()
    page.wait_for_function('held.length>0')
    page.wait_for_timeout(250)
    during = page.evaluate("[...document.querySelectorAll('[data-testid=unpriced-badge]')].map(n=>n.innerText)")
    release(page)
    settle(page)
    assert during == [text], ('unpriced badge lost across the switch', text, during)
    assert not errors, errors
    page.close()


def frozen_view_all_owns_header(browser):
    """A frozen View all DTO keeps its own header while live reads move on."""
    page, errors = fresh_page(browser)
    page.evaluate('''()=>{const first=events[0];
      for(let i=0;i<14;i++){const e=JSON.parse(JSON.stringify(first));
        e.id='freeze-'+i;e.started=Date.now()/1000-900+i;e.ended=e.started+0.1;events.push(e)}}''')
    notify_and_settle(page)
    page.get_by_role('navigation', name='Provider subpages').get_by_role('tab', name='Requests', exact=True).click()
    pager = page.get_by_test_id('record-pagination')
    expect(pager).to_contain_text('Showing 10 of')
    settle(page)
    page.get_by_role('button', name='View all records').click()
    expect(pager).to_contain_text('Frozen')
    frozen = page.evaluate(HERO)
    page.evaluate('addDemoEvent()')
    n = page.evaluate('projected.length')
    page.wait_for_function('n=>projected.length>n', arg=n)
    page.wait_for_timeout(800)
    still = page.evaluate(HERO)
    page.get_by_role('button', name='Exit frozen view').click()
    page.wait_for_function('t=>document.querySelector("[data-testid=usage-totals]")?.innerText!==t', arg=frozen['totals'])
    live = page.evaluate(HERO)
    assert still['totals'] == frozen['totals'] and still['paths'] == frozen['paths'], ('frozen header replaced', still)
    assert live['totals'] != frozen['totals'], 'exit should show the newer live header'
    assert not errors, errors
    page.close()


def all_profiles_lower_switches(browser):
    page, errors = fresh_page(browser)
    page.get_by_role('combobox', name='Hermes profile', exact=True).select_option('scope:all')
    page.get_by_role('tab', name='All providers', exact=True).click()
    expect(page.locator('.au-breakdown tbody tr').first).to_be_visible()
    results = scenario(page, 'all-profiles')
    assert not errors, errors
    page.close()
    return results


def closed_custom_range(page):
    return page.evaluate('''()=>{const t=Math.floor(Date.now()/1800000)*1800000;
      const f=v=>{const d=new Date(v);return new Date(v-d.getTimezoneOffset()*60000).toISOString().slice(0,16)};
      return [f(t-6*3600000),f(t+1800000)]}''')


def single_scroller_group_keeps_scroll(browser, custom=False):
    """In the one-page scroller a group switch must not move the page."""
    page = browser.new_page(viewport={'width': 1000, 'height': 760})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/*', lambda route: route.abort())
    page.set_content((ROOT / 'preview.html').read_text())
    page.get_by_role('tab', name='All providers', exact=True).click()
    expect(page.locator('.au-breakdown tbody tr').first).to_be_visible()
    page.evaluate(INSTALL)
    if custom:
        open_custom(page, *closed_custom_range(page))
        settle(page)
        # Close the disclosure: the check is on the ordinary closed-Custom page.
        page.evaluate("document.querySelectorAll('.au-custom-range').forEach(d=>{d.open=false})")
        group_button(page, 'Model').click()
    settle(page)
    root = '.au-mobile'
    assert page.locator(root).count() == 1, 'expected single-scroller mode'
    page.evaluate('''()=>{const r=document.querySelector('.au-mobile');
      const c=document.querySelector('[data-testid=breakdown-controls]');
      r.scrollTop+=c.getBoundingClientRect().top-r.getBoundingClientRect().top-120}''')
    top = page.evaluate("document.querySelector('.au-mobile').scrollTop")
    assert top > 100, top
    page.evaluate('heroEvents.length=0;hold=true')
    page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name='Project', exact=True).click()
    page.wait_for_function('held.length>0')
    page.wait_for_timeout(200)
    during = page.evaluate("document.querySelector('.au-mobile').scrollTop")
    page.evaluate('hold=false;for(const r of held.splice(0))r.deliver()')
    expect(page.locator('.au-breakdown tbody tr').first).to_be_visible()
    events = page.evaluate('[...heroEvents]')
    assert during == top, ('group switch moved the single-scroller page', top, during)
    assert not events, events
    assert not errors, errors
    page.close()


def run():
    report = os.environ.get('LOWER_ISOLATION_REPORT')
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1800, 'height': 1200})
        errors, outbound = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda req: outbound.append(req.url))
        page.route('**/*', lambda route: route.abort())
        page.set_content((ROOT / 'preview.html').read_text())
        page.get_by_role('tab', name='All providers', exact=True).click()
        expect(page.locator('.au-breakdown tbody tr').first).to_be_visible()
        page.evaluate(INSTALL)
        results = []
        page.get_by_role('group', name='Time window').get_by_role('button', name='Past 24h', exact=True).click()
        results += scenario(page, '24h')
        # Closed Custom over the fixture's recent synthetic activity.
        start, end = closed_custom_range(page)
        open_custom(page, start, end)
        results += scenario(page, 'custom-closed')
        open_custom(page, start, '')
        results += scenario(page, 'custom-open')
        results += all_profiles_lower_switches(browser)
        failures = []
        for result in results:
            label, problems = check(result)
            print(('FAIL ' if problems else 'ok   ') + label + ' [lower ' + result['lower'] + ']' +
                  ((' — ' + '; '.join(problems)) if problems else ''))
            if problems:
                failures.append(label)
        top_level_repaints(page)
        print('ok   period change repaints the hero')
        assert not errors, errors
        assert not outbound, outbound
        if failures and not report:
            raise AssertionError('lower-section isolation failed: ' + ', '.join(failures))
        for check_fn in (unvalidated_header_never_bridges, revision_change_after_check_clears,
                         older_revisit_keeps_newer_header,
                         fresh_lower_seq_replaces_header, seq_outranks_received, unpriced_badge_persists, frozen_view_all_owns_header):
            check_fn(browser)
            print('ok   ' + check_fn.__name__.replace('_', ' '))
        single_scroller_group_keeps_scroll(browser)
        print('ok   single-scroller group switch keeps page position')
        single_scroller_group_keeps_scroll(browser, custom=True)
        print('ok   single-scroller group switch keeps page position (closed Custom)')
        browser.close()
    print('PASS lower-section isolation' if not failures else 'REPORT: %d failures' % len(failures))


if __name__ == '__main__':
    run()
