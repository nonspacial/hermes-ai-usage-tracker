"""Dashboard cache writes: positive read deltas in a session's request stream.

This is the user's dashboard definition, not a replacement for provider usage.
It is calculated before time/page/project filtering. Raw ledger rows and saved
prices remain unchanged. Each profile has its own Store/database; providers,
accounts (when supplied), models, sessions, subagents and helper streams stay
separate. Restarting a process or changing the service tier does not reset it.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Iterable

BASIS = 'max(0, current cache reads - previous cache reads), per session stream'


def count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def stream_key(row):
    sid = row.get('session_id')
    if not sid:
        return None
    task = row.get('task') or 'main'
    # Main-hook task IDs often equal the session ID; they are not separate caches.
    stream = 'main' if row.get('source') == 'main_hook' or task in ('main', sid) else 'helper:' + str(task)
    return (sid, row.get('provider'), row.get('response_model') or row.get('model'),
            row.get('api_mode'), row.get('account_id'), row.get('subagent_id') or '', stream)


def observation(row, previous=None):
    current = count((row.get('usage') or {}).get('cache_read_tokens'))
    prior = count((previous.get('usage') or {}).get('cache_read_tokens')) if previous else None
    out = dict(tokens=None, basis=BASIS, method='session_read_delta', version=1,
               status='missing_reads', current_request_id=row.get('id'),
               previous_request_id=previous.get('id') if previous else None,
               current_read_tokens=current, previous_read_tokens=prior,
               delta_tokens=None, provider_reported_write_tokens=(row.get('usage') or {}).get('cache_write_tokens'),
               attribution='current_request_start', included_in_processed_tokens=False,
               included_in_provider_cost=False)
    if stream_key(row) is None:
        out['status'] = 'missing_session'
    elif row.get('status') == 'pending' and current is None:
        out['status'] = 'pending'
    elif (row.get('usage') or {}).get('request_count', 1) != 1:
        out['status'] = 'aggregate_reading'
    elif current is None:
        pass
    elif previous is None:
        out['status'] = 'baseline'
    elif (previous.get('usage') or {}).get('request_count', 1) != 1 or prior is None:
        out['status'] = 'previous_read_missing'
    else:
        out.update(status='calculated', delta_tokens=current-prior, tokens=max(0, current-prior))
    return out


def calculate(rows: Iterable[dict], selected_ids=None):
    """Pure reference implementation. Input order is request start, not arrival."""
    previous = {}
    output = {}
    for row in sorted(rows, key=lambda r: (r['started'], r['id'])):
        key = stream_key(row)
        result = observation(row, previous.get(key) if key is not None else None)
        if selected_ids is None or row['id'] in selected_ids:
            output[row['id']] = result
        # An active row without usage is not a completed cache observation.
        if key is not None and result['status'] != 'pending':
            previous[key] = row
    return output


def blank_summary():
    return dict(tokens=0, compared_requests=0, baseline_requests=0, missing_requests=0,
                basis=BASIS, method='session_read_delta')


def add_summary(summary, row):
    value = row.get('calculated_cache_writes') or {}
    tokens = count(value.get('tokens'))
    if tokens is None:
        summary['missing_requests'] += 1
        if value.get('status') == 'baseline':
            summary['baseline_requests'] += 1
    else:
        summary['tokens'] += tokens
        summary['compared_requests'] += 1


def _sequence_rows(c, sid, end):
    # Read only numeric counters/routing identity needed by the calculation.
    # This uses the original stored read count, including legacy normalized
    # readings. The displayed measure is explicitly calculated, not raw usage.
    fields = ('response_model', 'api_mode', 'account_id', 'subagent_id', 'source')
    extra = ','.join("json_extract(data,'$."+k+"') AS "+k for k in fields)
    sql = ("SELECT id,session_id,started,provider,model,task,status,"+extra+
           ",json_extract(data,'$.usage.cache_read_tokens') AS reads,"
           "json_extract(data,'$.usage.cache_write_tokens') AS writes,"
           "json_extract(data,'$.usage.request_count') AS request_count "
           "FROM main.requests WHERE session_id=? AND started<? ORDER BY started,id")
    for r in c.execute(sql, (sid, end)):
        d = dict(r)
        d['usage'] = dict(cache_read_tokens=d.pop('reads'), cache_write_tokens=d.pop('writes'),
                          request_count=d.pop('request_count') or 1)
        yield d


def materialise_summary(c, sql):
    """Extract aggregate inputs once per snapshot, not once per rollup.

    Keep SQLite's JSON scalar types (including NULL and decimal strings); never
    round costs through REAL. Only selected rows have projected accounting, and
    the original full-ledger view remains available for predecessor lookups.
    """
    if not c.execute("SELECT 1 FROM sqlite_temp_master WHERE name='session_write_projection'").fetchone():
        return sql
    expressions = dict.fromkeys(re.findall(r"json_extract\(data,'[^']+'\)", sql))
    fields = {expr: 'summary_value_' + str(i) for i, expr in enumerate(expressions)}
    c.execute('CREATE TEMP TABLE read_summary_values AS SELECT id,' +
              ','.join(expr + ' AS ' + name for expr, name in fields.items()) +
              ' FROM session_write_projection')
    c.execute('CREATE UNIQUE INDEX temp.read_summary_id ON read_summary_values(id)')
    c.execute('DROP VIEW temp.requests')
    c.execute('CREATE TEMP VIEW requests AS SELECT r.id,r.started,r.ended,r.provider,r.model,'
              'r.session_id,r.task,r.compression_id,r.status,COALESCE(p.data,r.data) AS data,' +
              ','.join('v.' + name for name in fields.values()) +
              ' FROM main.requests r LEFT JOIN session_write_projection p ON p.id=r.id'
              ' LEFT JOIN read_summary_values v ON v.id=r.id')
    for expr, name in fields.items():
        sql = sql.replace(expr, name)
    return sql


def project(c, where, params, end):
    """Attach calculated values in a read-connection TEMP view, never on disk.

    Calculations see predecessor reads before the selected date range and all
    adjacent session observations irrespective of project/agent/page filters.
    SQL summary/grouping functions then consume the same projected values.
    """
    selected = c.execute('SELECT id,session_id FROM requests WHERE '+where, params).fetchall()
    if not selected:
        return
    selected_by_session = defaultdict(set)
    for r in selected:
        selected_by_session[r['session_id']].add(r['id'])
    changes = {}
    for sid, ids in selected_by_session.items():
        if sid:
            # The iterator streams history. Only the selected detail is retained.
            previous = {}
            for r in _sequence_rows(c, sid, end):
                key = stream_key(r)
                result = observation(r, previous.get(key))
                if r['id'] in ids:
                    changes[r['id']] = result
                if result['status'] != 'pending':
                    previous[key] = r
    c.execute('CREATE TEMP TABLE session_write_projection(id TEXT PRIMARY KEY,data TEXT NOT NULL)')
    for r in c.execute('SELECT id,data FROM requests WHERE '+where, params):
        data = json.loads(r['data'])
        data['calculated_cache_writes'] = changes.get(r['id']) or observation(data)
        c.execute('INSERT INTO session_write_projection VALUES(?,?)',
                  (r['id'], json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False)))
    # Selected rows already include any legacy usage projection. Unselected rows
    # use main.requests exactly; future Store.read connections rebuild the view.
    c.execute('DROP VIEW IF EXISTS temp.requests')
    c.execute('''CREATE TEMP VIEW requests AS
        SELECT r.id,r.started,r.ended,r.provider,r.model,r.session_id,r.task,r.compression_id,r.status,
               COALESCE(p.data,r.data) AS data
        FROM main.requests r LEFT JOIN session_write_projection p ON p.id=r.id''')
