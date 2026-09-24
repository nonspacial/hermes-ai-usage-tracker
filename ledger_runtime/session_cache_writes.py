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
from decimal import Decimal
from typing import Iterable

BASIS = 'max(0, current cache reads - previous cache reads), per session stream'

# The same short-circuit expression is used by the unprojected summary and by
# its per-read TEMP materialisation. Closed history never inspects process state.
READ_STATE_SQL = "(CASE WHEN status IN ('abandoned_without_usage','abandoned_with_usage') THEN 'abandoned' WHEN status IN ('pending','usage_received') AND ended IS NULL THEN execution_state(data) ELSE 'closed' END)"


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
    c.execute('CREATE TEMP TABLE read_summary_values AS SELECT id,ended,' +
              ','.join(expr + ' AS ' + name for expr, name in fields.items()) +
              ',' + READ_STATE_SQL + ' AS read_execution_state FROM ('
              'SELECT p.id,p.data,r.ended,r.status FROM session_write_projection p '
              'JOIN main.requests r ON r.id=p.id)')
    c.execute('CREATE UNIQUE INDEX temp.read_summary_id ON read_summary_values(id)')
    # Each missing-field category is identical in every total, trend and
    # attribution grouping. Compute it once after scalar/state projection,
    # rather than running the same CASE six times per metric per rollup.
    projected_sql = sql
    for expr, name in fields.items():
        projected_sql = projected_sql.replace(expr, name)
    projected_sql = projected_sql.replace(READ_STATE_SQL, 'read_execution_state')
    reasons = dict.fromkeys(re.findall(r'\(CASE WHEN .*? END\)', projected_sql))
    for i in range(len(reasons)):
        c.execute('ALTER TABLE read_summary_values ADD COLUMN summary_reason_' + str(i) + ' TEXT')
    if reasons:
        c.execute('UPDATE read_summary_values SET ' + ','.join(
            'summary_reason_' + str(i) + '=' + expr for i, expr in enumerate(reasons)))
    c.execute('DROP VIEW temp.requests')
    c.execute('CREATE TEMP VIEW requests AS SELECT r.id,r.started,r.ended,r.provider,r.model,'
              'r.session_id,r.task,r.compression_id,r.status,COALESCE(p.data,r.data) AS data,' +
              'v.read_execution_state,' + ','.join('v.' + name for name in fields.values()) + ',' +
              ','.join('v.summary_reason_' + str(i) for i in range(len(reasons))) +
              ' FROM main.requests r LEFT JOIN session_write_projection p ON p.id=r.id'
              ' LEFT JOIN read_summary_values v ON v.id=r.id')
    for i, expr in enumerate(reasons):
        projected_sql = projected_sql.replace(expr, 'summary_reason_' + str(i))
    return projected_sql


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
    # The selected set is already bounded by the query, not the page. One
    # provider catalog per read snapshot serves all its eligible rows; no
    # network and no per-request catalog query or durable ledger mutation.
    from .pricing import PROVIDERS, select_rate
    from .accounting import costs
    catalogs = {}
    c.execute('CREATE TEMP TABLE session_write_projection(id TEXT PRIMARY KEY,data TEXT NOT NULL)')
    for r in c.execute('SELECT id,data FROM requests WHERE '+where, params):
        data = json.loads(r['data'])
        data['calculated_cache_writes'] = changes.get(r['id']) or observation(data)
        original = data.get('cost') or {}
        source = PROVIDERS.get(data.get('provider'))
        if (source and data.get('ended') is not None and data.get('status') not in ('pending','usage_received')
                and not original.get('rate') and not original.get('complete')
                and Decimal(original.get('known_components_usd') or '0') == 0
                and data.get('usage') and any(count(data['usage'].get(k)) for k in
                    ('input_tokens','output_tokens','cache_read_tokens','cache_write_tokens'))):
            if source not in catalogs:
                row = c.execute('SELECT id,data FROM provider_catalog WHERE source_id=? ORDER BY observed DESC,id DESC LIMIT 1', (source,)).fetchone()
                catalogs[source] = (row['id'], json.loads(row['data'])) if row else None
            snapshot = catalogs[source]
            rate = select_rate(snapshot[1], data, snapshot[0], data['ended'], retrospective=True) if snapshot else None
            if rate:
                # The displayed cost is a TEMP read projection. Its original
                # (possibly legacy-adjusted) cost and usage remain inspectable;
                # main.requests and events are never updated by this path.
                data.setdefault('stored_accounting', {'usage': data['usage'], 'cost': data.get('cost')})
                data['cost'] = costs(data['usage'], rate)
                data['supplemental_valuation'] = {
                    'basis': 'current_published_rate_for_past_usage',
                    'not_historical_charge': True, 'source_id': source,
                    'catalog_revision': snapshot[0], 'observed_at': rate['observed_at'],
                    'request_ended_at': data['ended'], 'model': rate['model'],
                    'service_tier': rate['service_tier'], 'context_band': rate.get('context_band'),
                    'source_url': rate['source_url'], 'content_sha256': rate['content_sha256']}
        c.execute('INSERT INTO session_write_projection VALUES(?,?)',
                  (r['id'], json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False)))
    # Selected rows already include any legacy usage projection. Unselected rows
    # use main.requests exactly; future Store.read connections rebuild the view.
    c.execute('DROP VIEW IF EXISTS temp.requests')
    c.execute('''CREATE TEMP VIEW requests AS
        SELECT r.id,r.started,r.ended,r.provider,r.model,r.session_id,r.task,r.compression_id,r.status,
               COALESCE(p.data,r.data) AS data
        FROM main.requests r LEFT JOIN session_write_projection p ON p.id=r.id''')
