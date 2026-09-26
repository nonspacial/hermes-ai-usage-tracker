"""Conservative concurrency from persisted child lifecycle notifications, never request durations."""
import json


def intervals(connection):
    """Pair ordered child starts/stops. Duplicated/overlapping starts are ambiguous.

    Return matched (session, identity, start, end) and unmatched event count.
    An open start is deliberately not closed at the report's end.
    """
    active = {}  # session -> (timestamp, identity, starts since last stop)
    matched = []
    unmatched = 0
    for row in connection.execute("SELECT ts,kind,item_id,data FROM events WHERE kind IN ('subagent_start','subagent_stop') ORDER BY seq"):
        sid = row['item_id']
        if not sid:
            unmatched += 1
            continue
        if row['kind'] == 'subagent_start':
            if sid in active:
                begin, identity, count = active[sid]
                active[sid] = (begin, identity, count + 1)
            else:
                active[sid] = (row['ts'], json.loads(row['data']).get('subagent_id') or sid, 1)
        elif sid not in active:
            unmatched += 1
        else:
            begin, identity, count = active.pop(sid)
            if count != 1 or row['ts'] <= begin:
                unmatched += count + 1  # every notification in the rejected pair
            else:
                matched.append((sid, identity, begin, row['ts']))
    unmatched += sum(count for _, _, count in active.values())
    return matched, unmatched, bool(matched)


def apply(connection, report, where, params, *, scoped_filter=False):
    """Recompute every observed peak from one copied-ledger read transaction.

    Shared by full reads and signed request-only reductions. No request duration
    substitutes for a child lifetime; filtered lifetimes need child requests.
    """
    matched, unmatched, seen = intervals(connection)
    evidence = {}
    for row in connection.execute('SELECT session_id,provider,model,data FROM requests WHERE '+where,params):
        record = json.loads(row['data'])
        if record.get('agent_kind') != 'subagent':
            continue
        evidence.setdefault(row['session_id'], []).append((
            row['provider'], record.get('response_model') or row['model'] or 'unknown',
            record.get('project_id') or 'unattributed',
            record.get('root_session_id') or row['session_id'] or 'unattributed',
            record.get('subagent_id') or row['session_id'] or 'unattributed'))
    scoped = [v for v in matched if v[0] in evidence] if scoped_filter else matched
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
    return {'value': peak(intervals, start, end) if seen else None,
            'unmatched': unmatched, 'basis': 'matched_child_lifecycle'}
