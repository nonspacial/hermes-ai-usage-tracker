"""Read-only cache-read progression, NOT cache-write accounting.

A positive difference between two read counters describes reuse observed on the
later request. It cannot identify which request populated the cache, or whether
that work was a billable input-cache write. No estimate is merged into usage,
costs, rate snapshots or the persistent ledger. Same-session adjacency matters:
helpers, other sessions, concurrent calls and known context resets are not mixed.
"""
from __future__ import annotations

import bisect
import json
import math
from collections import Counter, defaultdict

from .accounting import effective_record


def integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def stamp(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def route(row):
    return (row.get('provider'), row.get('response_model') or row.get('model'),
            row.get('api_mode'), row.get('returned_service_tier') or row.get('service_tier'))


def compare(previous, current, reset_times=()):
    """Return a diagnostic for a single adjacent pair, never a write estimate."""
    result = {'kind': 'observed_read_difference', 'current_request_id': current['id'],
              'previous_request_id': previous.get('id') if previous else None,
              'prefix_identity_verified': False, 'included_in_usage_or_cost': False,
              'status': 'excluded', 'read_delta_tokens': None,
              'positive_read_growth_tokens': None, 'reason': None}
    reason = None
    if previous is None:
        reason = 'no_previous_main_request'
    elif not current.get('process') or not previous.get('process'):
        reason = 'process_unavailable'
    elif previous['process'] != current['process']:
        reason = 'producer_changed'
    elif route(previous) != route(current):
        reason = 'provider_model_or_tier_changed'
    elif previous.get('status') != 'completed' or current.get('status') != 'completed':
        reason = 'not_both_completed'
    else:
        lo, hi, ended = stamp(previous.get('started')), stamp(current.get('started')), stamp(previous.get('ended'))
        if lo is None or hi is None or ended is None or not lo < hi or ended < lo or ended > hi:
            reason = 'overlapping_or_invalid_timing'
        elif (i := bisect.bisect_left(reset_times, lo)) < len(reset_times) and reset_times[i] <= hi:
            reason = 'compression_or_compaction_between_requests'
        else:
            pu, cu = previous.get('usage') or {}, current.get('usage') or {}
            if pu.get('request_count', 1) != 1 or cu.get('request_count', 1) != 1:
                reason = 'aggregate_not_single_request'
            else:
                before, after = integer(pu.get('cache_read_tokens')), integer(cu.get('cache_read_tokens'))
                pi, ci = integer(pu.get('prompt_tokens')), integer(cu.get('prompt_tokens'))
                if before is None or after is None or pi is None or ci is None:
                    reason = 'read_or_input_count_missing'
                elif before > pi or after > ci:
                    reason = 'invalid_read_decomposition'
                else:
                    pm, cm = previous.get('cache_request') or {}, current.get('cache_request') or {}
                    # Fingerprints are local to this producer; compare only here.
                    keys = ('cache_key_present', 'cache_key_fingerprint', 'instructions_fingerprint',
                            'tools_fingerprint', 'mode_sent', 'ttl_sent', 'retention_sent',
                            'explicit_breakpoint_count', 'reasoning_effort_sent')
                    if pm and cm and any(pm.get(k) != cm.get(k) for k in keys):
                        reason = 'observed_cache_settings_changed'
                    else:
                        result.update(status='compared', previous_read_tokens=before,
                            current_read_tokens=after, previous_prompt_tokens=pi,
                            current_prompt_tokens=ci, gap_seconds=hi-ended,
                            read_delta_tokens=after-before,
                            positive_read_growth_tokens=max(0, after-before),
                            previous_provider_write_tokens=pu.get('cache_write_tokens'),
                            current_provider_write_tokens=cu.get('cache_write_tokens'))
    result['reason'] = reason
    return result


def analyse(records, selected_ids, resets=None):
    """Rows are the complete main-request sequence for the selected sessions."""
    resets = resets or {}
    previous = {}
    observations = {}
    for row in sorted(records, key=lambda r: (r['started'], r['id'])):
        if row.get('source') != 'main_hook' or not row.get('session_id'):
            continue
        sid = row['session_id']
        if row['id'] in selected_ids:
            observations[row['id']] = compare(previous.get(sid), row, resets.get(sid, ()))
        previous[sid] = row
    valid = [o for o in observations.values() if o['status'] == 'compared']
    skipped = Counter(o['reason'] for o in observations.values() if o['status'] != 'compared')
    result = {'basis': 'positive changes in consecutive same-session main-request cache reads',
              'attribution': 'later request start time', 'not_a_write_count': True,
              'included_in_usage_or_cost': False, 'eligible_pairs': len(valid),
              'excluded_pairs': sum(skipped.values()), 'excluded_reasons': dict(skipped),
              'positive_read_growth_tokens': sum(o['positive_read_growth_tokens'] for o in valid) if valid else None,
              'read_drop_tokens': sum(max(0, -o['read_delta_tokens']) for o in valid) if valid else None,
              'caveat': 'Prefix identity, cache machine and causal writer are not verified. Growth can repeat after a miss; never price it as cache writes.'}
    return result, observations


# Project only the fields needed to compare reads. Do not load saved prices,
# full diagnostics, attribution paths or original stored_accounting per row.
_PROJECTION = "json_object(" + ",".join(
    "'" + key + "'," + (key if key in ('id','session_id','started','ended','provider','model','status')
                           else "json_extract(data,'$." + key + "')")
    for key in ('id','session_id','started','ended','provider','model','status',
                'response_model','process','api_mode','source','returned_service_tier',
                'service_tier','cache_request','usage')) + ")"


def query_progression(c, where, params, start, end, detail_ids=None):
    """Streaming full-window comparison; only page-level details are retained.

    Other main attempts in the same session preserve adjacency across filters.
    One predecessor outside the selected window establishes a boundary reading;
    neither its tokens nor a guessed write charge are added to the selected set.
    """
    selected = c.execute("SELECT id,session_id FROM requests WHERE " + where +
        " AND session_id<>'' AND json_extract(data,'$.source')='main_hook'", params).fetchall()
    selected_ids = {r['id'] for r in selected}
    result, observations = analyse([], set())
    if not selected_ids:
        return result, observations
    sessions = sorted({r['session_id'] for r in selected})
    skipped = Counter()
    growth = drops = 0
    for sid in sessions:
        before = c.execute("SELECT " + _PROJECTION + " FROM requests WHERE session_id=? AND started<? "
            "AND json_extract(data,'$.source')='main_hook' ORDER BY started DESC,id DESC LIMIT 1", (sid, start)).fetchone()
        previous = effective_record(json.loads(before[0])) if before else None
        lo = previous['started'] if previous else start
        resets = [r[0] for r in c.execute("SELECT started FROM compressions WHERE (session_id=? OR session_after=?) AND started>=? AND started<? ORDER BY started", (sid, sid, lo, end))]
        rows = c.execute("SELECT " + _PROJECTION + " FROM requests WHERE session_id=? AND started>=? AND started<? "
            "AND json_extract(data,'$.source')='main_hook' ORDER BY started,id", (sid, start, end))
        for item in rows:
            current = effective_record(json.loads(item[0]))
            if current['id'] in selected_ids:
                observation = compare(previous, current, resets)
                if observation['status'] == 'compared':
                    result['eligible_pairs'] += 1
                    growth += observation['positive_read_growth_tokens']
                    drops += max(0, -observation['read_delta_tokens'])
                else:
                    skipped[observation['reason']] += 1
                if detail_ids is None or current['id'] in detail_ids:
                    observations[current['id']] = observation
            previous = current
    result.update(excluded_pairs=sum(skipped.values()), excluded_reasons=dict(skipped),
                  positive_read_growth_tokens=growth if result['eligible_pairs'] else None,
                  read_drop_tokens=drops if result['eligible_pairs'] else None)
    return result, observations
