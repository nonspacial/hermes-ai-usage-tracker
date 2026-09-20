"""Recorder leases, independent of token counters, quota and provider networking.

Health is profile-specific. A responding HTTP server alone is NOT evidence that
request observers loaded. Historical event timestamps are never used as leases.
"""
from __future__ import annotations
import json
import math
import time

HEARTBEAT_SECONDS = 15
LEASE_SECONDS = 60


def summarize_health(rows, now=None):
    now = time.time() if now is None else float(now)
    active, legacy, stale, warnings = [], [], [], []
    for row in rows:
        raw = row.get('data', {})
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(data, dict):
                raise ValueError('Invalid health record')
        except (ValueError, TypeError):
            warnings.append('Unrecognized recorder status')
            continue
        beat = data.get('heartbeat_at')
        if not isinstance(beat, (int, float)) or not math.isfinite(beat):
            legacy.append(data)
            continue
        age = now - beat
        if age < -5:
            warnings.append('Recorder clock is ahead of the backend')
            stale.append(data)
        elif age <= LEASE_SECONDS and data.get('request_hooks_registered') is True:
            active.append(data)
        else:
            stale.append(data)
    failures = 0
    for data in active:
        failures += max(0, int(data.get('recorder_failures') or 0))
        adapters = data.get('adapters') or {}
        unavailable = [str(k) for k,v in adapters.items() if any(w in str(v).lower() for w in ('unavailable', 'unsupported', 'failed', 'mismatch'))]
        if unavailable:
            warnings.append('Unavailable capture adapters: ' + ', '.join(unavailable))
        # Historical failure counts do not permanently label a recovered recorder offline.
        last_failure = data.get('last_failure_at') or 0
        if last_failure and 0 <= now-last_failure <= 300:
            warnings.append('Recent recording error; some usage may be missing')
    if active:
        state = 'limited' if warnings else 'online'
        message = f'{len(active)} recording process(es) reporting for this profile.'
    elif legacy and not stale:
        state = 'unverified'
        message = 'An older recorder has no liveness heartbeat. Reload the updated plugin in its producer to verify recording.'
    else:
        state = 'not_recording'
        message = 'No live recorder reporting for this profile. Start or reload a Hermes producer with the Python plugin enabled.'
    return {'state':state,'message':message,'active_processes':len(active),'stale_processes':len(stale),
            'legacy_processes':len(legacy),'warnings':list(dict.fromkeys(warnings)),
            'failures':failures,'checked_at':now,'heartbeat_seconds':HEARTBEAT_SECONDS,
            'lease_seconds':LEASE_SECONDS,'scope':'loaded observers in this profile; not provider reachability or complete route coverage'}


def status(store, now=None):
    with store.db() as c:
        rows = [dict(r) for r in c.execute('SELECT process,updated,data FROM health')]
    return summarize_health(rows, now)
