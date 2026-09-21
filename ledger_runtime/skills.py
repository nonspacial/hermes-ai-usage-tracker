"""Append-only, content-free skill observations and estimated context snapshots.

No prompt builders, filesystem skill reads or provider calls are used here.
The API opens an existing database read-only; only producers create the schema.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from . import recorder as r
from .storage import jd, notify

SCHEMA = '''
CREATE TABLE IF NOT EXISTS skill_events (
 id TEXT PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL,
 session_id TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
 skill TEXT, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS skill_events_time ON skill_events(ts,id);
CREATE INDEX IF NOT EXISTS skill_events_session ON skill_events(session_id,ts);
'''
KINDS = {'skill_load', 'context_snapshot', 'compression_before', 'compression_after', 'turn_end'}
IDENTIFIER = re.compile(r'[A-Za-z0-9_.:/@+\-]{1,240}\Z')
SKILL_INDEX = re.compile(r'<available_skills>.*?</available_skills>', re.S)
LABELS = {'system_prompt': 'System prompt (undivided)', 'skills_index': 'Skill index',
          'skills': 'Retained skill results', 'conversation': 'Conversation and other tool results'}
SNAPSHOT_LIMIT = 500


def ident(value):
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else ''


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def skill_name(value):
    value = ident(value)
    if value.startswith('/') or any(p in ('', '.', '..') for p in value.split('/')) or re.match(r'^[A-Za-z]:/', value):
        return ''
    return value


def rough(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return (len(text) + 3) // 4


def parsed(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            obj = json.loads(value)
            return obj if isinstance(obj, dict) else {}
        except (ValueError, TypeError):
            pass
    return {}


def append(store, event):
    """Insert immutable observations; duplicate native hook IDs are ignored."""
    if event['kind'] not in KINDS or not event.get('session_id'):
        return
    # Callers construct explicit allowlists; never persist hook kwargs/results.
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM skill_events WHERE id=?', (event['id'],)).fetchone():
            return
        if event['kind'] == 'skill_load':
            event['repeat'] = bool(event['success'] and c.execute(
                "SELECT 1 FROM skill_events WHERE session_id=? AND skill=? AND "
                "json_extract(data,'$.success')=1 AND json_extract(data,'$.is_reference')=? "
                "AND COALESCE(json_extract(data,'$.file_path'),'')=? LIMIT 1",
                (event['session_id'], event['skill'], event['is_reference'], event.get('file_path', ''))).fetchone())
        c.execute('INSERT INTO skill_events VALUES(?,?,?,?,?,?,?,?)',
                  (event['id'], event['ts'], event['kind'], event['session_id'],
                   event['provider'], event['model'], event.get('skill'), jd(event)))
        # Existing change notifications use this sequence, without duplicating data.
        store.event(c, 'skill_observation', event['id'], {'kind': event['kind']})
    notify(store.folder)


def metadata(store, kw, request=None):
    sid = ident(kw.get('session_id'))
    if not sid:
        return None
    from .attribution import capture
    ctx = capture(store, sid, ident(kw.get('platform')))
    request = request or {}
    out: dict = {'session_id': sid, 'provider': ident(request.get('provider') or kw.get('provider')) or 'unknown',
           'model': ident(request.get('model') or kw.get('model')) or 'unknown'}
    for field in ('turn_id', 'request_id'):
        value = ident(request.get('id') if field == 'request_id' else kw.get(field) or request.get(field))
        if value:
            out[field] = value
    for field in ('project_id', 'subagent_id'):
        out[field] = ident(ctx.get(field)) or None
    out['agent_kind'] = ctx.get('agent_kind') if ctx.get('agent_kind') in ('primary', 'subagent') else 'unknown'
    out['session_lineage'] = [ident(s) for s in ctx.get('session_lineage', []) if ident(s)]
    # Project labels are display metadata, not conversation text; exclude full paths.
    label = ctx.get('project_label')
    if isinstance(label, str) and len(label) <= 240 and not any(ord(ch) < 32 for ch in label):
        out['project_label'] = label
    return out


def request_for(store, kw):
    aid, sid, turn = (ident(kw.get(k)) for k in ('api_request_id', 'session_id', 'turn_id'))
    if not aid or not sid:
        return {}
    with store.db() as c:
        rows = c.execute("SELECT data FROM requests WHERE session_id=? AND json_extract(data,'$.api_request_id')=? "
                         "AND json_extract(data,'$.turn_id')=? AND json_extract(data,'$.process')=?",
                         (sid, aid, turn, r.PROCESS)).fetchall()
    return json.loads(rows[0][0]) if len(rows) == 1 else {}


@r.safe
def tool(**kw):
    if kw.get('tool_name') != 'skill_view':
        return
    call = ident(kw.get('tool_call_id'))
    sid, turn = ident(kw.get('session_id')), ident(kw.get('turn_id'))
    if not call or not sid or not turn:
        return  # Never merge callbacks by text or guess an identity.
    resolved = r.request_context(kw)
    if not resolved:
        return
    store = r.store(resolved['root'])
    request = request_for(store, kw)
    if not request:
        return
    result, args = parsed(kw.get('result')), parsed(kw.get('args'))
    # The lookup name is stable across main, reference and failed responses;
    # native main results may instead contain a different frontmatter name.
    skill = skill_name(args.get('name') if 'name' in args else result.get('name'))
    if not skill:
        return
    file_path = args.get('file_path')
    reference = bool(file_path)
    safe_path = ident(file_path)
    if safe_path.startswith('/') or '..' in safe_path.split('/'):
        safe_path = ''
    success = result.get('success') is True and kw.get('status') != 'error'
    meta = metadata(store, kw, request)
    if not meta:
        return
    content = result.get('content')
    event = {**meta, 'id': f'{r.PROCESS}:skill:{request["id"]}:{call}', 'ts': time.time(),
             'kind': 'skill_load', 'skill': skill, 'success': success, 'is_reference': reference,
             'estimated_tokens': rough(content) if success and isinstance(content, str) else None,
             'source': 'post_tool_call', 'attribution': 'load_not_retention',
             'content_returned': success and isinstance(content, str),
             'deduplicated': result.get('dedup') is True}
    if safe_path:
        event['file_path'] = safe_path
    append(store, event)


def context(messages, system_prompt):
    """Estimate only directly visible raw material; unknown components stay absent.

    Skill attribution requires a skill_view tool-call/result pair in this same
    message list. No previous load is treated as proof of retained content.
    """
    if not isinstance(messages, list):
        return {'context_used': None, 'categories': [], 'retained_skills': [], 'attribution': 'unknown'}
    calls = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        for call in message.get('tool_calls') or []:
            if not isinstance(call, dict):
                continue
            fn = call.get('function') or {}
            if isinstance(fn, dict) and fn.get('name') == 'skill_view':
                calls[call.get('id')] = skill_name(parsed(fn.get('arguments')).get('name'))
        if message.get('type') == 'function_call' and message.get('name') == 'skill_view':
            calls[message.get('call_id')] = skill_name(parsed(message.get('arguments')).get('name'))
    totals = dict.fromkeys(LABELS, 0)
    retained = set()
    system_parts = [m.get('content') for m in messages if isinstance(m, dict) and m.get('role') in ('system', 'developer')]
    # Out-of-band instructions are not added twice to in-band system messages.
    system = '\n'.join(s if isinstance(s, str) else json.dumps(s, ensure_ascii=False) for s in system_parts)
    if isinstance(system_prompt, (str, list)) and system_prompt not in system_parts:
        extra = system_prompt if isinstance(system_prompt, str) else json.dumps(system_prompt, ensure_ascii=False)
        system = '\n'.join(part for part in (system, extra) if part)
    index = '\n'.join(SKILL_INDEX.findall(system))
    totals['skills_index'] = rough(index) if index else 0
    totals['system_prompt'] = rough(SKILL_INDEX.sub('', system)) if system else 0
    for message in messages:
        if not isinstance(message, dict) or message.get('role') in ('system', 'developer'):
            continue
        call_id = message.get('tool_call_id') or message.get('call_id')
        payload = parsed(message.get('content', message.get('output')))
        skill = calls.get(call_id)
        is_result = message.get('role') == 'tool' or message.get('type') == 'function_call_output'
        if is_result and skill and payload.get('success') is True and isinstance(payload.get('content'), str):
            retained.add(skill)
            totals['skills'] += rough(message)
        else:
            totals['conversation'] += rough(message)
    return {'context_used': sum(totals.values()),
            'categories': [{'id': k, 'label': LABELS[k], 'tokens': v} for k, v in totals.items() if v],
            'retained_skills': sorted(retained),
            'attribution': 'direct_tool_pairs_only' if retained else 'unknown'}


@r.safe
def snapshot(store, meta, key, kind, messages=None, system_prompt=None, context_max=None, compression_id=None,
             context_used=None, source=None):
    if not meta:
        return
    event = {**meta, **context(messages, system_prompt), 'id': key, 'ts': time.time(), 'kind': kind,
             'context_max': number(context_max),
             'source': 'rough_chars_v1' if isinstance(messages, list) else 'unavailable_at_boundary'}
    if number(context_used) is not None:
        event['context_used'] = number(context_used)
        event['source'] = source or 'rough_chars_v1'
    if compression_id:
        event['compression_id'] = compression_id
    append(store, event)


@r.safe
def pre(store, request, kw):
    snapshot(store, metadata(store, kw, request), request['id'] + ':context', 'context_snapshot',
             kw.get('request_messages'), kw.get('system_prompt'),
             context_used=kw.get('approx_input_tokens'), source='native_preflight_estimate_categories_chars_v1')


@r.safe
def turn_end(store, kw):
    sid, turn = ident(kw.get('session_id')), ident(kw.get('turn_id'))
    if not sid or not turn:
        return
    with store.db() as c:
        rows = c.execute("SELECT data FROM requests WHERE session_id=? AND json_extract(data,'$.turn_id')=? "
                         "AND json_extract(data,'$.process')=? ORDER BY started DESC,id DESC LIMIT 1",
                         (sid, turn, r.PROCESS)).fetchall()
    request = json.loads(rows[0][0]) if rows else {}
    # This hook carries no messages or occupancy: retain an honest boundary marker.
    snapshot(store, metadata(store, kw, request), f'{r.PROCESS}:turn:{sid}:{turn}', 'turn_end')


@r.safe
def compression(item, agent, messages, system, kind):
    if not item:
        return
    root, rec = item
    store = r.store(root)
    kw = {'session_id': getattr(agent, 'session_id', ''), 'provider': getattr(agent, 'provider', ''),
          'model': getattr(agent, 'model', ''), 'platform': getattr(agent, 'platform', ''),
          'turn_id': getattr(agent, '_current_turn_id', '')}
    meta = metadata(store, kw)
    cc = getattr(agent, 'context_compressor', None)
    snapshot(store, meta, rec['id'] + ':' + kind, kind, messages, system,
             getattr(cc, '_resolved_context_length', None), rec['id'])


@r.safe
def compression_result(item, agent, result):
    if not item or not isinstance(result, tuple) or len(result) != 2:
        return
    root, rec = item
    with r.store(root).db() as c:
        row = c.execute('SELECT data FROM compressions WHERE id=?', (rec['id'],)).fetchone()
    # A returned tuple is not evidence of commit: aborted attempts return one too.
    if not row or json.loads(row[0]).get('status') != 'committed':
        return
    compression(item, agent, result[0], result[1], 'compression_after')


def read(root, *, start: float=0, end=None, provider='', session='', session_scope='exact', agent='', project='',
         subagent='', model='', skill='', offset=0, limit=200, test_id='', _detail_limit=None):
    """A coherent, read-only report, with full-period (not page) aggregates."""
    end = time.time() if end is None else end
    if number(start) is None or number(end) is None or end < start:
        raise ValueError('Invalid time window.')
    if offset < 0 or not 1 <= limit <= 200:
        raise ValueError('Invalid pagination.')
    if session_scope not in ('exact', 'family') or agent not in ('', 'primary', 'subagent', 'unknown'):
        raise ValueError('Invalid scope filter.')
    out = {'version': 1, 'generated_at': time.time(), 'window': {'start': start, 'end': end},
           'coverage': {'status': 'not_recorded', 'since': None,
                        'note': 'No skills observations recorded. Historical activity is not reconstructed.'},
           'summary': {'loads': 0, 'references': 0, 'failures': 0, 'sessions': 0},
           'skills': [], 'model_options': [], 'events': [], 'event_count': 0, 'next_offset': None,
           'snapshots': [], 'snapshot_count': 0, 'snapshots_truncated': False}
    path = Path(root) / 'usage-ledger' / 'events.sqlite3'
    if not path.is_file():
        if test_id:
            raise ValueError('Unknown test marker.')
        return out
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if test_id:
            row = c.execute('SELECT started,ended FROM tests WHERE id=?', (test_id,)).fetchone() if 'tests' in tables else None
            if not row:
                raise ValueError('Unknown test marker.')
            start, end = row[0], row[1] if row[1] is not None else end
            out['window'] = {'start': start, 'end': end}
        if 'skill_events' not in tables:
            return out
        since = c.execute('SELECT MIN(ts) FROM skill_events').fetchone()[0]
        if since is not None:
            out['coverage'] = {'status': 'partial', 'since': since,
                'note': 'Observed hooks only; older history and uninstrumented producers are unknown. '
                        'Context categories estimate visible messages/instructions only, excluding tool schemas; not provider tokens. '
                        'Turn-end occupancy and unrecognised/pruned skill attribution remain unknown.'}
        where = 'ts>=? AND ts<?'
        params = [start, end]
        for column, value in (('provider', provider), ('model', model)):
            if value:
                where += ' AND ' + column + '=?'
                params.append(value)
        if session:
            where += ' AND (session_id=?'
            params.append(session)
            if session_scope == 'family':
                where += " OR EXISTS (SELECT 1 FROM json_each(skill_events.data,'$.session_lineage') WHERE value=?)"
                params.append(session)
            where += ')'
        for field, value, default in (('agent_kind', agent, 'unknown'), ('project_id', project, 'unattributed')):
            if value:
                where += f" AND COALESCE(json_extract(data,'$.{field}'),?)=?"
                params.extend([default, value])
        if subagent:
            where += " AND COALESCE(NULLIF(json_extract(data,'$.subagent_id'),''),session_id)=?"
            params.append(subagent)
        out['model_options'] = [row[0] for row in c.execute('SELECT DISTINCT model FROM skill_events WHERE ' + where + ' ORDER BY model', params)]
        # Reduce aggregates in SQL rather than retaining an unbounded event list.
        group_sql = """SELECT skill,
          SUM(CASE WHEN json_extract(data,'$.success')=1 AND json_extract(data,'$.is_reference')=0 THEN 1 ELSE 0 END),
          SUM(CASE WHEN json_extract(data,'$.success')=1 AND json_extract(data,'$.is_reference')=1 THEN 1 ELSE 0 END),
          SUM(CASE WHEN json_extract(data,'$.success')=0 THEN 1 ELSE 0 END),
          COUNT(DISTINCT session_id),
          SUM(CASE WHEN json_extract(data,'$.repeat')=1 AND json_extract(data,'$.is_reference')=0 THEN 1 ELSE 0 END),
          CASE WHEN SUM(CASE WHEN json_extract(data,'$.success')=1 AND json_extract(data,'$.is_reference')=0
                             AND json_extract(data,'$.estimated_tokens') IS NULL THEN 1 ELSE 0 END)>0 THEN NULL
          ELSE SUM(CASE WHEN json_extract(data,'$.success')=1 AND json_extract(data,'$.is_reference')=0 THEN json_extract(data,'$.estimated_tokens') END) END
          FROM skill_events WHERE """
        groups = c.execute(group_sql + where + " AND kind='skill_load' GROUP BY skill ORDER BY 2 DESC,skill", params)
        keys = ('name', 'loads', 'references', 'failures', 'sessions', 'repeat_loads', 'estimated_tokens')
        out['skills'] = [dict(zip(keys, row)) for row in groups]
        for field in ('loads', 'references', 'failures'):
            out['summary'][field] = sum(g[field] for g in out['skills'])
        out['summary']['sessions'] = c.execute('SELECT COUNT(DISTINCT session_id) FROM skill_events WHERE ' + where + " AND kind='skill_load'", params).fetchone()[0]
        detail_where, detail_params = where, list(params)
        if skill:
            # Context is session-level, not apportioned to individual skill loads.
            # Show direct retained matches only; unknown attribution is never invented.
            detail_where += " AND (skill=? OR EXISTS (SELECT 1 FROM json_each(skill_events.data,'$.retained_skills') WHERE value=?))"
            detail_params.extend([skill, skill])
        out['event_count'] = c.execute('SELECT COUNT(*) FROM skill_events WHERE ' + detail_where, detail_params).fetchone()[0]
        order = ' ORDER BY ts DESC,id DESC'
        out['events'] = [json.loads(row[0]) for row in c.execute('SELECT data FROM skill_events WHERE ' + detail_where + order + ' LIMIT ? OFFSET ?', detail_params + [limit if _detail_limit is None else _detail_limit, offset])]
        out['next_offset'] = offset + limit if offset + limit < out['event_count'] else None
        snap_where = detail_where + " AND kind!='skill_load'"
        out['snapshot_count'] = c.execute('SELECT COUNT(*) FROM skill_events WHERE ' + snap_where, detail_params).fetchone()[0]
        out['snapshots'] = [json.loads(row[0]) for row in c.execute('SELECT data FROM skill_events WHERE ' + snap_where + order + ' LIMIT ?', detail_params + [SNAPSHOT_LIMIT])]
        out['snapshots_truncated'] = out['snapshot_count'] > SNAPSHOT_LIMIT
    return out
