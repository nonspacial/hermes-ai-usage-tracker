"""Conservative concurrency from persisted child lifecycle notifications, never request durations.

A matched start/stop pair is a child lifetime. An open start (no later stop for
that child session) is bounded only by evidence: it runs until the read's
``now`` while its latest request's owner process is live and its latest request
activity is recent, otherwise until that latest recorded request activity.
Nothing is ever closed at the report's end.
"""
import json
import time

# A child whose start has no recorded request yet is counted as running only
# this soon after its start, and only while its parent's request owner is live.
OPEN_CHILD_GRACE_SECONDS = 120
# A child with requests counts as running only while its latest request owner
# is live and its latest request activity (max started/ended) is younger than
# this. Hermes force-finalises a stalled async child (1200 s stuck in a tool
# plus 120 s grace) without a subagent_stop; this adds margin over that.
OPEN_CHILD_IDLE_SECONDS = 1800


class _ChildKey(str):
    """Child session id that remembers how its interval was bounded.

    Intervals stay 4-tuples (session, identity, start, end) so readers that
    unpack four values (all-profile qualification) keep working; the session
    element passes through them unchanged and carries ``kind``.
    """
    kind = 'matched'


def _open_key(sid, kind):
    key = _ChildKey(sid)
    key.kind = kind
    return key


def kind_of(interval):
    return getattr(interval[0], 'kind', 'matched')


def intervals(connection, open_children=None):
    """Pair ordered child starts/stops. Duplicated/overlapping starts are ambiguous.

    Return matched (session, identity, start, end) and the excluded event count.
    Open starts are counted as excluded here; when ``open_children`` is a list,
    each open start's (session, identity, start, parent session) is appended so
    the caller can bound it from evidence. Never closed at the report's end.
    """
    active = {}  # session -> (timestamp, identity, starts since last stop, parent)
    matched = []
    unmatched = 0
    # Pairing and the unmatched count span all lifecycle history, so this scan
    # is not window-bounded; the event_kind index limits it to lifecycle rows.
    for row in connection.execute("SELECT ts,kind,item_id,data FROM events WHERE kind IN ('subagent_start','subagent_stop') ORDER BY seq"):
        sid = row['item_id']
        if not sid:
            unmatched += 1
            continue
        if row['kind'] == 'subagent_start':
            if sid in active:
                begin, identity, count, parent = active[sid]
                active[sid] = (begin, identity, count + 1, parent)
            else:
                data = json.loads(row['data'])
                active[sid] = (row['ts'], data.get('subagent_id') or sid, 1, data.get('parent_session_id') or '')
        elif sid not in active:
            unmatched += 1
        else:
            begin, identity, count, _ = active.pop(sid)
            if count != 1 or row['ts'] <= begin:
                unmatched += count + 1  # every notification in the rejected pair
            else:
                matched.append((sid, identity, begin, row['ts']))
    for sid, (begin, identity, count, parent) in active.items():
        unmatched += count
        if count == 1 and open_children is not None:
            open_children.append((sid, identity, begin, parent))
    return matched, unmatched, bool(matched)


def _owner_live(record, cache):
    """One inspection per distinct owner per read (same key as execution_state)."""
    from . import ownership
    owner = record.get('owner')
    key = json.dumps(owner, sort_keys=True)
    if key not in cache:
        cache[key] = ownership.inspect_owner(owner)[0]
    return cache[key] == 'live'


def _latest_request(connection, session):
    row = connection.execute('SELECT data FROM main.requests WHERE session_id=? '
                             'ORDER BY started DESC,id DESC LIMIT 1', (session,)).fetchone()
    return json.loads(row['data']) if row is not None else None


def bound_open(connection, open_children, now, cache=None):
    """Resolve open children to counted intervals from recorded evidence.

    Returns the counted (session, identity, start, end) intervals, the
    session carrying its kind. A child with requests runs until ``now`` when
    its latest request owner is live and its latest request activity (max
    started/ended) is less than ``OPEN_CHILD_IDLE_SECONDS`` before ``now``;
    otherwise it is ``open_ended`` at that activity. A child without requests
    runs until ``now`` only within the grace period and while its parent's
    latest request owner is live; otherwise it is excluded.
    """
    cache = {} if cache is None else cache
    counted = []
    for sid, identity, begin, parent in open_children:
        latest = _latest_request(connection, sid)
        if latest is not None:
            row = connection.execute('SELECT MAX(started),MAX(ended) FROM main.requests WHERE session_id=?',
                                     (sid,)).fetchone()
            activity = max(v for v in (row[0], row[1]) if v is not None)
            if now - activity < OPEN_CHILD_IDLE_SECONDS and _owner_live(latest, cache):
                kind, finish = 'running', now
            else:
                kind, finish = 'open_ended', activity
        else:
            parent_request = _latest_request(connection, parent) if parent else None
            if (0 <= now - begin < OPEN_CHILD_GRACE_SECONDS and parent_request is not None
                    and _owner_live(parent_request, cache)):
                kind, finish = 'running', now
            else:
                continue
        if finish > begin:
            counted.append((_open_key(sid, kind), identity, begin, finish))
    return counted


def apply(connection, report, where, params, *, scoped_filter=False, now=None):
    """Recompute every observed peak from one copied-ledger read transaction.

    Shared by full reads and signed request-only reductions. No request duration
    substitutes for a child lifetime; filtered lifetimes need child requests.
    Open children are bounded once per read against ``now`` (read time).
    """
    now = time.time() if now is None else now
    open_children = []
    matched, unmatched, _ = intervals(connection, open_children)
    counted = bound_open(connection, open_children, now)
    unmatched -= len(counted)
    lifetimes = matched + counted
    seen = bool(lifetimes)
    evidence = {}
    # Same query/plan and row order as before. A payload whose decoded
    # agent_kind is "subagent" must contain that literal JSON string unless it
    # uses an escape, so other payloads are skipped without a Python decode.
    for row in connection.execute("SELECT session_id,provider,model,CASE WHEN instr(data,'\"subagent\"')>0 "
                                  "OR instr(data,'\\')>0 THEN data END AS data FROM requests WHERE "+where,params):
        if row['data'] is None:
            continue
        record = json.loads(row['data'])
        if record.get('agent_kind') != 'subagent':
            continue
        evidence.setdefault(row['session_id'], []).append((
            row['provider'], record.get('response_model') or row['model'] or 'unknown',
            record.get('project_id') or 'unattributed',
            record.get('root_session_id') or row['session_id'] or 'unattributed',
            record.get('subagent_id') or row['session_id'] or 'unattributed'))
    scoped = [v for v in lifetimes if v[0] in evidence] if scoped_filter else lifetimes
    peak_groups = {'provider': {}, 'model': {}}
    for interval in scoped:
        for provider_id, model_id, _, _, _ in evidence.get(interval[0], []):
            peak_groups['provider'].setdefault(provider_id, []).append(interval)
            peak_groups['model'].setdefault(json.dumps([provider_id, model_id]), []).append(interval)
    start, end = report['window']['start'], report['window']['end']
    report['subagent_summary']['peak_observed'] = observation(scoped, unmatched, seen, start, end)
    for row in report['provider_groups']:
        row['peak_observed'] = observation(peak_groups['provider'].get(row['provider'], []), unmatched, seen, start, end)
    for row in report.get('model_groups') or []:
        row['peak_observed'] = observation(peak_groups['model'].get(json.dumps([row['provider'], row['model']]), []), unmatched, seen, start, end)
    for name, key in (('project_groups', 2), ('session_groups', 3)):
        for row in report.get(name) or []:
            selected = [v for v in scoped if any(e[key] == row['key'] for e in evidence.get(v[0], []))]
            row['peak_observed'] = observation(selected, unmatched, seen, start, end)
    for bucket in report['trend']['buckets']:
        bucket['peak_observed'] = observation(scoped, unmatched, seen, bucket['start'], bucket['end'])
    return scoped, peak_groups, unmatched, seen


def peak(intervals, start, end):
    """Maximum distinct child identities in [start,end); departures precede arrivals."""
    events = []
    for _, identity, begin, finish in intervals:
        lo, hi = max(begin, start), min(finish, end)
        if lo < hi:
            events.extend(((lo, 1, identity), (hi, -1, identity)))
    counts = {}
    highest = 0
    for _, change, identity in sorted(events, key=lambda e: (e[0], e[1])):
        counts[identity] = counts.get(identity, 0) + change
        if counts[identity] == 0:
            del counts[identity]
        highest = max(highest, len(counts))
    return highest


def observation(intervals, unmatched, seen, start, end):
    """Peak plus the distinct open children of each kind intersecting [start,end)."""
    present = {'running': set(), 'open_ended': set()}
    for interval in intervals:
        kind = kind_of(interval)
        if kind in present and max(interval[2], start) < min(interval[3], end):
            present[kind].add(interval[1])
    return {'value': peak(intervals, start, end) if seen else None,
            'unmatched': unmatched, 'basis': 'matched_child_lifecycle',
            'running': len(present['running']), 'open_ended': len(present['open_ended'])}
