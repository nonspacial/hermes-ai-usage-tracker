"""Local producer evidence, never heartbeat expiry or provider-execution proof.

Only Linux identities in the same host and PID namespace are inspectable. No
signals, leases, TTLs or session ordering authorise abandonment. Unknown stays
unknown. Process identity is captured afresh after fork; generation is immutable
for the lifetime of this imported producer in that process.
"""
from __future__ import annotations
import errno
import json
import os
from pathlib import Path
import socket
import sys
import uuid

_GENERATION = (os.getpid(), uuid.uuid4().hex)


def _after_fork():
    global _GENERATION
    _GENERATION = (os.getpid(), uuid.uuid4().hex)


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


def environment():
    if sys.platform != 'linux':
        raise OSError('Unsupported identity platform')
    host = Path('/etc/machine-id').read_text().strip()
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if not host or not boot:
        raise ValueError('Missing identity')
    # Both are required: a different proc mount must not inspect another PID map.
    namespace = os.readlink('/proc/self/ns/pid')
    if int(Path('/proc/self/stat').read_text().split(' ', 1)[0]) != os.getpid():
        raise ValueError('Foreign proc mount')
    return dict(host=host + ':' + socket.gethostname(), boot=boot, namespace=namespace)


def process(pid):
    raw = Path(f'/proc/{pid}/stat').read_text()
    fields = raw[raw.rindex(')') + 2:].split()
    return fields[19], fields[0]  # starttime (field 22), state (field 3)


def identity():
    global _GENERATION
    pid = os.getpid()
    if _GENERATION is None or _GENERATION[0] != pid:
        _GENERATION = (pid, uuid.uuid4().hex)
    owner = dict(version=1, pid=pid, generation=_GENERATION[1])
    try:
        owner.update(environment())
        owner['start'] = process(pid)[0]
    except (OSError, ValueError, IndexError):
        pass
    return owner


def inspect_owner(owner):
    """Return (live/dead/unknown, fixed reason); never infer actual end time."""
    if not isinstance(owner, dict) or type(owner.get('version')) is not int or owner['version'] != 1:
        return 'unknown', 'missing_owner_identity'
    if (type(owner.get('pid')) is not int or not 0 < owner['pid'] < 2**31 or
            any(not isinstance(owner.get(k), str) or not owner[k]
                for k in ('host', 'boot', 'namespace', 'start', 'generation'))):
        return 'unknown', 'incomplete_owner_identity'
    try:
        if (str(uuid.UUID(owner['boot'])) != owner['boot'] or
                uuid.UUID(owner['generation']).hex != owner['generation'] or
                not owner['start'].isascii() or not owner['start'].isdigit()):
            return 'unknown', 'malformed_owner_identity'
    except ValueError:
        return 'unknown', 'malformed_owner_identity'
    try:
        local = environment()
        if owner['host'] != local['host']:
            return 'unknown', 'foreign_host'
        if owner['boot'] != local['boot']:
            return 'dead', 'previous_boot'
        if owner['namespace'] != local['namespace']:
            return 'unknown', 'foreign_pid_namespace'
        try:
            start, state = process(owner['pid'])
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ESRCH):
                raise
            # A hidden proc entry alone is not absence. ESRCH from the kernel in
            # this exact PID namespace is independent of proc hidepid policy.
            try:
                os.kill(owner['pid'], 0)
            except ProcessLookupError:
                return 'dead', 'process_absent'
            return 'unknown', 'process_uninspectable'
        if start != owner['start']:
            return 'dead', 'pid_reused'
        if state in ('Z', 'X', 'x'):
            # stat describes the thread-group leader, not whole-process death.
            # pthread_exit can leave that leader a zombie with working threads.
            # Even terminal leader states need independent death evidence;
            # leave them unresolved until identity reuse or confirmed absence.
            return 'unknown', 'terminal_leader_state'
        return 'live', 'matching_process_identity'
    except (OSError, ValueError, IndexError):
        return 'unknown', 'inspection_unavailable'


def execution_state(record, cache=None):
    if record.get('status') in ('abandoned_without_usage', 'abandoned_with_usage'):
        return 'abandoned'
    if record.get('status') not in ('pending', 'usage_received') or record.get('ended') is not None:
        return 'closed'
    owner = record.get('owner')
    key = json.dumps(owner, sort_keys=True)
    if cache is not None and key in cache:
        state = cache[key]
    else:
        state = inspect_owner(owner)[0]
        if cache is not None:
            cache[key] = state
    # Dead but not yet reconciled is unresolved, not live or a fabricated end.
    return 'owner_live' if state == 'live' else 'unresolved'


def register_sql(c):
    cache = {}
    c.create_function('execution_state', 1,
                      lambda data: execution_state(json.loads(data), cache))


def reconcile(store, limit=256):
    """One indexed, rotating bounded batch at producer lifecycle boundaries.

    Retain every row/event. CAS rechecks exact owner + status + unknown end under
    the existing SQLite write transaction, protecting concurrent completion.
    """
    after = getattr(store, '_reconcile_after', '')
    with store.db() as c:
        rows = c.execute("SELECT id,data FROM requests WHERE status IN ('pending','usage_received') "
                         "AND ended IS NULL AND id>? ORDER BY id LIMIT ?", (after, limit)).fetchall()
    store._reconcile_after = rows[-1]['id'] if len(rows) == limit else ''
    cache = {}
    for row in rows:
        rec = json.loads(row['data'])
        owner = rec.get('owner')
        key = json.dumps(owner, sort_keys=True)
        if key not in cache:
            cache[key] = inspect_owner(owner)
        state, reason = cache[key]
        if state != 'dead':
            continue
        import time
        store.request({'id': row['id'], 'status': 'abandoned_without_usage',
                       'execution_outcome': 'abandoned',
                       'reconciliation': {'reason': reason, 'observed_at': time.time(),
                                          'actual_end_known': False}},
                      'request_abandoned', expected={'owner': (owner,),
                      'status': ('pending', 'usage_received'), 'ended': (None,)})
