"""Exact bounded bucket replacement for fixed Overview/time reads.

No persistent response cache. The caller owns one bounded RAM snapshot. Every
unsupported writer, changed scope, missing history or concurrent commit asks for
an ordinary full read; a fast path never invents an unchanged result.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import sqlite3
import time

from .aggregate import merge_summaries
from .incremental import changes, watermark
from .session_cache_writes import stream_key
from .storage import request_predicate

MAX_BUCKETS = 4
MAX_BUCKET_ROWS = 2048


def _bucket(started, step):
    return int(started // step) * step


def _next_bucket(connection, row, start, end, step):
    """A changed predecessor can alter its next stream observation outside scope."""
    data = json.loads(row['data'])
    key = stream_key(data)
    if key is None:
        return set()
    candidates = connection.execute(
        'SELECT data,started,id FROM requests WHERE session_id=? AND '
        '(started>? OR (started=? AND id>?)) AND started<? '
        'ORDER BY started,id LIMIT 257',
        (row['session_id'], row['started'], row['started'], row['id'], end)).fetchall()
    if len(candidates) > 256:
        return None
    for candidate in candidates:
        next_data = json.loads(candidate['data'])
        if stream_key(next_data) == key:
            if next_data.get('status') == 'pending' and (next_data.get('usage') or {}).get('cache_read_tokens') is None:
                continue
            return {_bucket(candidate['started'], step)} if candidate['started'] >= start else set()
    return set()


def replace_buckets(reader, args, response, seed, since, *, group='time', maximum=128, apply_observed_peak):
    """Return (response, seed) or None. args are Store.read positional args.

    Only fixed positive windows and Overview/time are supported. A narrow
    Store.read per affected bucket is intentional: it uses the production SQL
    projection, including predecessor history and exact Decimal arithmetic.
    """
    start, end = args[:2]
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or start <= 0 or end <= start:
        return None
    step = response['trend']['seconds']
    old_start=response['window']['start']
    old_end=response['window']['end']
    old_buckets={_bucket(row['start'],step):row for row in response['trend']['buckets']}
    buckets=list(range(_bucket(start,step),_bucket(math.nextafter(end,-math.inf),step)+1,step))
    if len(buckets)>400 or not buckets:
        return None
    with reader.db() as connection:
        connection.execute('BEGIN')
        feed = changes(connection, since, maximum=maximum)
        if feed is None:
            return None
        high, entries = feed
        if response['incremental']['revision'] != since:
            return None
        # Process-state changes are not journaled. Open owners always take the
        # full read, even when no row changed since the cached snapshot.
        # Positional layout: start,end,provider,offset,limit,session,agent,
        # project,session_scope,subagent,model,model_provider.
        where, params, _ = request_predicate(start,end,args[2],args[5],args[6],args[7],
                                              args[8],args[9],args[10],args[11])
        if connection.execute('SELECT 1 FROM requests WHERE '+where+
                              " AND status IN ('pending','usage_received') AND ended IS NULL LIMIT 1",params).fetchone():
            return None
        affected = set()
        if start!=old_start or end!=old_end:
            # Both clipped boundary buckets change even with an empty feed.
            affected.update((buckets[0],buckets[-1]))
        old_options = {r['id']:r for r in response['project_options']}
        for entry in entries:
            if entry['kind'] == 'events':
                continue  # seq is refreshed under the same final snapshot.
            if entry['kind'] != 'requests':
                return None
            if entry['old_row'] and not entry['new_row']:
                return None  # Deletion may change global project/provider extrema.
            if entry['old_row'] and entry['new_row']:
                previous=json.loads(entry['old_row'])
                current=json.loads(entry['new_row'])
                old_data=json.loads(previous['data'])
                new_data=json.loads(current['data'])
                if previous['provider']!=current['provider'] or any(
                    old_data.get(field)!=new_data.get(field) for field in
                    ('project_id','project_label','project_path','project_source')):
                    return None  # Global option/provider membership may change.
            for text in (entry['old_row'], entry['new_row']):
                if not text:
                    continue
                row = json.loads(text)
                data = json.loads(row['data'])
                project = data.get('project_id') or 'unattributed'
                option = old_options.get(project)
                if option is None or any(data.get(field) is not None and data.get(field) != option.get(name)
                                         for field,name in (('project_label','label'),('project_path','path'),
                                                            ('project_source','basis'))):
                    return None
                if row['provider'] not in response['providers']:
                    return None
                if start <= row['started'] < end:
                    affected.add(_bucket(row['started'],step))
                successors = _next_bucket(connection,row,start,end,step)
                if successors is None:
                    return None
                affected.update(successors)
        affected.update(b for b in buckets if b not in old_buckets)
        if len(affected) > MAX_BUCKETS:
            return None
        # A scalar-count query over the selected index bounds is cheap; it
        # resolves distinct sessions without approximating bucket overlaps.
        if any(connection.execute('SELECT COUNT(*) FROM requests WHERE '+where+
                                  ' AND started>=? AND started<?',params+
                                  [max(start,b),min(end,b+step)]).fetchone()[0] > MAX_BUCKET_ROWS
               for b in affected):
            return None
    new = deepcopy(response)
    # JSON object keys in the sealed client token are strings, while freshly
    # calculated SQLite bucket numbers are integers. Normalize before replace.
    kinds=('provider','subagent','model') if group=='model' else ('provider','subagent')
    new_seed={kind:{int(k):v for k,v in seed[kind].items() if int(k) in buckets}
              for kind in kinds}
    new['window']={'start':start,'end':end,'basis':response['window']['basis']}
    new['trend']['buckets']=[{'start':max(start,b),'end':min(end,b+step),
                              **{k:v for k,v in old_buckets[b].items() if k not in ('start','end')}}
                             if b in old_buckets else None for b in buckets]
    for bucket in sorted(affected):
        low, high_bound = max(start,bucket),min(end,bucket+step)
        narrowed = (low,high_bound,*args[2:])
        part = reader.read(*narrowed,view='overview',group=group,_validate_costs=True,_skip_peak=True)
        if not part.pop('_delta_cost_safe',False):
            return None
        if part.get('incremental',{}).get('revision') != high:
            return None
        target=buckets.index(bucket)
        # The narrow read may use finer trend edges than the original bucket.
        # Its full-window provider groups cover the entire clipped original bucket.
        new['trend']['buckets'][target] = {'start':low,'end':high_bound,**part['summary'],
            'provider_buckets':sorted(
                ({k:v for k,v in row.items() if k!='latest_started'}
                 for row in part['provider_groups']),key=lambda row:row['provider'])}
        new_seed['provider'][bucket] = {r['provider']:{k:v for k,v in r.items() if k!='provider'}
                                        for r in part['provider_groups']}
        new_seed['subagent'][bucket] = {k:v for k,v in part['subagent_summary'].items() if k!='agents'}
        if group=='model':
            new_seed['model'][bucket]={json.dumps((r['provider'],r['model'])):
                                        {k:v for k,v in r.items() if k not in ('provider','model')}
                                        for r in part['model_groups']}
    with reader.db() as connection:
        connection.execute('BEGIN')
        if watermark(connection) != high:
            return None
        where,params,_ = request_predicate(start,end,args[2],args[5],args[6],args[7],
                                           args[8],args[9],args[10],args[11])
        new['summary']=merge_summaries([{k:v for k,v in r.items() if k not in ('start','end','peak_observed','provider_buckets')}
                                        for r in new['trend']['buckets']])
        new['summary']['sessions']=connection.execute('SELECT COUNT(DISTINCT NULLIF(session_id,\'\')) '
                                                      'FROM requests WHERE '+where,params).fetchone()[0]
        providers = []
        for row in connection.execute('SELECT provider,MAX(started) AS latest,'
                                      'COUNT(DISTINCT NULLIF(session_id,\'\')) AS sessions '
                                      'FROM requests WHERE '+where+' GROUP BY provider '
                                      'ORDER BY latest DESC,provider',params):
            contributions=[groups[row['provider']] for groups in new_seed['provider'].values()
                           if row['provider'] in groups]
            value=merge_summaries(contributions)
            value['sessions']=row['sessions']
            value['latest_started']=row['latest']
            providers.append({'provider':row['provider'],**value})
        new['provider_groups']=providers
        if group=='model':
            model_expr="COALESCE(NULLIF(json_extract(data,'$.response_model'),''),NULLIF(model,''),'unknown')"
            models=[]
            for row in connection.execute('SELECT provider,'+model_expr+' AS effective_model,MAX(started) AS latest,'
                                          "COUNT(DISTINCT NULLIF(session_id,'')) AS sessions "
                                          'FROM requests WHERE '+where+' GROUP BY provider,'+model_expr+' '
                                          'ORDER BY latest DESC,provider,effective_model',params):
                key=json.dumps((row['provider'],row['effective_model']))
                values=[groups[key] for groups in new_seed['model'].values() if key in groups]
                value=merge_summaries(values)
                value['sessions']=row['sessions']
                value['latest_started']=row['latest']
                models.append({'provider':row['provider'],'model':row['effective_model'],**value})
            new['model_groups']=models
        sub=merge_summaries(list(new_seed['subagent'].values()))
        role="COALESCE(json_extract(data,'$.agent_kind'),'unknown')"
        child="COALESCE(NULLIF(json_extract(data,'$.subagent_id'),''),NULLIF(session_id,''),'unattributed')"
        row=connection.execute('SELECT COUNT(DISTINCT NULLIF(session_id,\'\')),'
                               'COUNT(DISTINCT '+child+') FROM requests WHERE '+where+
                               ' AND '+role+"='subagent'",params).fetchone()
        sub['sessions'],sub['agents']=row
        new['subagent_summary']=sub
        new['request_count']=new['summary']['attempts']
        if new['request_count']>100000:
            return None
        offset,limit=args[3:5]
        new['next_offset']=offset+limit if offset+limit<new['request_count'] else None
        new['seq']=connection.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        # An event-only commit between journal checks is not safe. Source
        # events are paired with tracked writers; arbitrary events invalidate.
        if watermark(connection) != high:
            return None
        apply_observed_peak(connection, new, where, params,
                            scoped_filter=bool(args[2] or args[5] or args[6] or args[7] or
                                               args[9] or args[10] or args[11]))
    new['generated_at']=time.time()
    new['incremental']={'version':1,'revision':high,'mode':'delta','changed_buckets':len(affected)}
    return new,new_seed
