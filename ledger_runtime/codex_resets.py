"""Account-bound earned Codex resets, through the documented app-server RPC only.

No auth files are opened here. Hermes' profile-scoped credential resolver supplies an
in-memory ChatGPT token; a disposable, credential-free Codex home is used for each
RPC session. Neither token, raw responses, nor account identifiers enter the UI.
An uncertain consume is deliberately NOT retried automatically.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import selectors
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

POLL_SECONDS = 30
RPC_SECONDS = 9
# Absolute budget for app-server RPCs; cleanup is separately bounded and may
# extend the call by at most its grace/kill waits (plus local temp removal).
TOTAL_SECONDS = 38
CLEANUP_GRACE_SECONDS = 2
CLEANUP_KILL_SECONDS = 2
WINDOWS = {300: 'five-hour', 10080: 'weekly'}
OUTCOMES = ('reset', 'alreadyRedeemed', 'nothingToReset', 'noCredit')
# SQLite busy budget for observe/opt-in/admission: long enough to ride out another
# profile's short transaction (or a reader's SHARED lock under journal_mode=delete).
BUSY_SECONDS = 1.0
# One post-spend write per attempt (record the known outcome and clear the attempt
# in a single transaction) gets its own bounded budget of short tries; every try
# re-verifies the attempt id before writing.
FINALISE_SECONDS = 5.0
FINALISE_TRY_SECONDS = 1.0
FINALISE_BACKOFF = (0.05, 0.25)
FINALISE_MAX_TRIES = 20
# A live redeem records its outcome within the app-server deadline plus one
# finalisation budget; an older attempt without an outcome is reported unrecorded.
# Wall-clock steps only change that pending/unrecorded label: blocking depends on
# the stored attempt alone, never on elapsed time.
INFLIGHT_SECONDS = TOTAL_SECONDS + FINALISE_SECONDS + 30
# Journal states from _write_outcome.
NOT_WRITTEN, WRITTEN, DURABLE = 0, 1, 2
# Settled `attempts` rows kept per account (the current attempt is always kept).
ATTEMPTS_KEEP = 200
# At most this many directory entries are examined per orphan-journal sweep.
SWEEP_LIMIT = 64


class Unavailable(Exception):
    pass


class Unrecorded(Unavailable):
    """Codex returned a definite consume outcome, but recording it did not finish.

    ``journal`` is the _write_outcome state (a bool means DURABLE/NOT_WRITTEN).
    DURABLE: the next check clears the attempt. WRITTEN: the journal is readable
    but its durability is unconfirmed, so the next check may clear it (not after
    a crash). NOT_WRITTEN: the account stays blocked as ``unrecorded``.
    """
    def __init__(self, outcome: str, journal):
        if journal is True or journal is False:
            journal = DURABLE if journal else NOT_WRITTEN
        what = ('Reset was used but' if outcome in ('reset', 'alreadyRedeemed')
                else 'Codex returned %s but it' % outcome)
        tail = ('it will be reconciled on the next check.' if journal == DURABLE
                else 'it may be reconciled on the next check. If it stays blocked, check your '
                     'Codex account before taking further action.' if journal == WRITTEN
                else 'redemption stays blocked. Check your Codex account before taking further action.')
        super().__init__(what + ' could not be recorded; ' + tail)
        self.outcome, self.journal = outcome, journal
        self.reconcilable = journal == DURABLE


class _AttemptChanged(Unavailable):
    pass


def _principal(token: str) -> tuple[str, str]:
    """JWT claims are only a proposed binding; the provider read must validate it."""
    try:
        parts = token.split('.')
        if len(parts) != 3:
            raise ValueError('not a JWT')
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        account = claims['https://api.openai.com/auth']['chatgpt_account_id']
        subject = claims['sub']
        exp = claims['exp']
        if not all(isinstance(x, str) and 0 < len(x) <= 256 for x in (account, subject)):
            raise ValueError('missing identity')
        if isinstance(exp, bool) or not isinstance(exp, (int, float)) or exp < time.time() + 45:
            raise ValueError('expired token')
        return account, subject
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        raise Unavailable('Codex credential has no verified account binding.') from exc


def _credentials() -> tuple[str, str, str]:
    from hermes_cli.auth import resolve_codex_runtime_credentials
    try:
        creds = resolve_codex_runtime_credentials(read_only=True, refresh_if_expiring=False)
        token = creds['api_key']
        account, subject = _principal(token)
        base = urlsplit(str(creds.get('base_url') or 'https://chatgpt.com/backend-api/codex'))
        # Never forward a Hermes pool token to a custom endpoint or silently
        # reinterpret API-key auth as a ChatGPT subscription.
        if base.scheme != 'https' or base.hostname != 'chatgpt.com' or base.port is not None:
            raise Unavailable('Codex reset requires a ChatGPT subscription credential.')
        binding = hashlib.sha256((account + '\0' + subject).encode()).hexdigest()
        return token, account, binding
    except Unavailable:
        raise
    except Exception as exc:
        raise Unavailable('No account-bound ChatGPT credential is available.') from exc


class AppServer:
    """Short-lived account-only JSON-RPC client; no model turn or tool is started."""
    def __init__(self, token: str, account: str):
        self.token, self.account = token, account
        self.proc = None
        self.tmp = None
        self.pending = b''
        self.next_id = 0
        self.deadline = time.monotonic() + TOTAL_SECONDS

    def __enter__(self):
        binary = shutil.which('codex')
        if not binary:
            raise Unavailable('Codex CLI is not installed.')
        self.tmp = tempfile.TemporaryDirectory(prefix='codex-resets-')
        home = Path(self.tmp.name)
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(home),
               'CODEX_HOME': str(home), 'RUST_LOG': 'error'}
        try:
            self.proc = subprocess.Popen([binary, 'app-server'], cwd=str(home), env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=(os.name == 'posix'))
            self.call('initialize', {'clientInfo': {'name': 'ai-usage-tracker', 'title': 'AI usage +', 'version': '1'},
                                     'capabilities': {'experimentalApi': True}})
            self._send({'method': 'initialized', 'params': {}})
            login = self.call('account/login/start', {'type': 'chatgptAuthTokens',
                         'accessToken': self.token, 'chatgptAccountId': self.account})
            if login.get('type') != 'chatgptAuthTokens':
                raise Unavailable('Codex declined external ChatGPT authentication.')
            auth = self.call('account/read', {'refreshToken': False}).get('account')
            if not isinstance(auth, dict) or auth.get('type') != 'chatgpt':
                raise Unavailable('Codex is not using a ChatGPT account.')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _send(self, value, until=None):
        until = min(self.deadline, until if until is not None else time.monotonic() + RPC_SECONDS)
        payload = (json.dumps(value, separators=(',', ':')) + '\n').encode()
        try:
            fd = self.proc.stdin.fileno()
            os.set_blocking(fd, False)
            with selectors.DefaultSelector() as sel:
                sel.register(fd, selectors.EVENT_WRITE)
                offset = 0
                while offset < len(payload):
                    remaining = until - time.monotonic()
                    if remaining <= 0 or not sel.select(remaining):
                        raise Unavailable('Codex app-server timed out; redemption status may be uncertain.')
                    try:
                        offset += os.write(fd, payload[offset:])
                    except BlockingIOError:
                        continue
        except (BrokenPipeError, OSError, AttributeError) as exc:
            raise Unavailable('Codex app-server connection failed.') from exc

    def call(self, method, params=None):
        until = min(self.deadline, time.monotonic() + RPC_SECONDS)
        self.next_id += 1
        rid = self.next_id
        self._send({'id': rid, 'method': method, 'params': params or {}}, until)
        with selectors.DefaultSelector() as sel:
            sel.register(self.proc.stdout, selectors.EVENT_READ)
            while time.monotonic() < until:
                if b'\n' not in self.pending:
                    if not sel.select(max(0, until - time.monotonic())):
                        break
                    chunk = os.read(self.proc.stdout.fileno(), 65536)
                    if not chunk or len(self.pending) + len(chunk) > 2_000_000:
                        raise Unavailable('Codex app-server reply unavailable or oversized.')
                    self.pending += chunk
                while b'\n' in self.pending:
                    line, self.pending = self.pending.split(b'\n', 1)
                    try:
                        msg = json.loads(line)
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise Unavailable('Invalid Codex app-server reply.') from exc
                    if msg.get('id') == rid:
                        if 'error' in msg or not isinstance(msg.get('result'), dict):
                            raise Unavailable('Codex app-server rejected the request.')
                        return msg['result']
                    if msg.get('method') == 'account/chatgptAuthTokens/refresh' and 'id' in msg:
                        # A different/rotated token requires a NEW authenticated read;
                        # this attempt fails closed instead of answering with another identity.
                        self._send({'id': msg['id'], 'error': {'code': -32000, 'message': 'Refresh requires a new session'}}, until)
                        raise Unavailable('Codex token refresh required; retry with a fresh account read.')
                    if 'id' in msg and 'method' in msg:
                        self._send({'id': msg['id'], 'error': {'code': -32601, 'message': 'Unsupported request'}}, until)
        raise Unavailable('Codex app-server timed out; redemption status may be uncertain.')

    def limits(self):
        result = self.call('account/rateLimits/read')
        if result.get('accountId') != self.account:
            raise Unavailable('Codex did not confirm the selected account identity.')
        return result

    def __exit__(self, *_):
        if self.proc is not None:
            try:
                if os.name == 'posix' and self.proc.poll() is None:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                elif self.proc.poll() is None:
                    self.proc.terminate()
                self.proc.wait(timeout=CLEANUP_GRACE_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    if os.name == 'posix':
                        os.killpg(self.proc.pid, signal.SIGKILL)
                    else:
                        self.proc.kill()
                    self.proc.wait(timeout=CLEANUP_KILL_SECONDS)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            finally:
                if self.proc.stdin:
                    self.proc.stdin.close()
                if self.proc.stdout:
                    self.proc.stdout.close()
        if self.tmp:
            self.tmp.cleanup()


def _episode(limits: dict) -> str | None:
    # A rounded 100% is NOT an exhaustion event. Both provider-classified
    # rate_limit_reached and explicit ordinary-usage refusal are mandatory.
    if limits.get('ordinaryUsageAllowed') is not False:
        return None
    buckets = limits.get('rateLimitsByLimitId')
    if isinstance(buckets, dict):
        bucket = buckets.get('codex')
    else:
        bucket = limits.get('rateLimits')
        if isinstance(bucket, dict) and bucket.get('limitId') != 'codex':
            bucket = None
    if not isinstance(bucket, dict) or bucket.get('rateLimitReachedType') != 'rate_limit_reached':
        return None
    matches = []
    for key in ('primary', 'secondary'):
        window = bucket.get(key)
        if not isinstance(window, dict) or window.get('windowDurationMins') not in WINDOWS:
            continue
        reset = window.get('resetsAt')
        if (type(window.get('usedPercent')) is int and window['usedPercent'] == 100
                and type(reset) is int and reset > time.time() and reset <= time.time() + 9 * 86400):
            matches.append((window['windowDurationMins'], reset))
    if not matches:
        return None
    return hashlib.sha256(repr(sorted(matches)).encode()).hexdigest()


def _view(limits: dict, binding: str) -> dict:
    summary = limits.get('rateLimitResetCredits')
    count = summary.get('availableCount') if isinstance(summary, dict) else None
    if type(count) is not int or count < 0:
        count = None
    episode = _episode(limits)
    return {'count': count, 'exhausted': episode is not None, 'episode': episode,
            'binding': binding, 'observed_at': time.time(),
            'credits': [{'id': c['id'], 'expires_at': c.get('expiresAt')}
                        for c in (summary.get('credits') or []) if isinstance(c, dict)
                        and c.get('status') == 'available' and isinstance(c.get('id'), str)] if isinstance(summary, dict) else []}


def _coord_root(home: Path) -> Path:
    from hermes_constants import get_default_hermes_root
    configured = Path(get_default_hermes_root())
    if configured.is_symlink():
        raise Unavailable('Shared reset authority must not be a symlink.')
    root = configured.resolve(strict=True)
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise Unavailable('Shared reset root is not owner-controlled.')
    selected = Path(home).resolve(strict=True)
    if selected != root and (selected.parent != root / 'profiles' or Path(home).is_symlink()):
        raise Unavailable('Selected profile has no shared reset authority.')
    return root


def _db_path(home: Path) -> Path:
    root = _coord_root(home)
    folder = root / 'codex-reset-authority'
    try:
        folder.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        created = False
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Unavailable('Shared reset authority is not owner-private.')
    if created:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return folder / 'state.sqlite3'


def _profile_key(home: Path) -> str:
    return hashlib.sha256(str(Path(home).resolve(strict=True)).encode()).hexdigest()


@contextmanager
def _state(home: Path, busy: float | None = None):
    path = _db_path(home)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        created = True
    except FileExistsError:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        created = False
    try:
        info = os.fstat(fd)
        named = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_uid != os.getuid() or info.st_mode & 0o077 or
                (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
            raise Unavailable('Shared reset database is not owner-private.')
        if created:
            os.fsync(fd)
            parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    finally:
        os.close(fd)
    # Private directory and SQLite transaction coordinate cooperating processes.
    conn = sqlite3.connect(path, timeout=BUSY_SECONDS if busy is None else busy, isolation_level=None)
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('CREATE TABLE IF NOT EXISTS state (binding TEXT PRIMARY KEY, episode TEXT, attempt TEXT, result TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS optin (binding TEXT NOT NULL, profile TEXT NOT NULL, auto INTEGER NOT NULL, PRIMARY KEY(binding,profile))')
        # Additive attempt journal (older authorities gain it on first open; the
        # state/optin shapes are unchanged). Keyed by attempt id, so a value can
        # never be applied to a different attempt.
        conn.execute('CREATE TABLE IF NOT EXISTS attempts (attempt TEXT PRIMARY KEY, binding TEXT NOT NULL, started REAL NOT NULL, outcome TEXT)')
        yield conn
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()


def _account_key(account: str) -> str:
    return hashlib.sha256(('codex-reset-account\0' + account).encode()).hexdigest()


def _canonical_attempt(attempt) -> bool:
    try:
        return str(uuid.UUID(attempt)) == attempt
    except (ValueError, TypeError, AttributeError):
        return False


def _outcome_path(home: Path, attempt: str) -> Path | None:
    if not _canonical_attempt(attempt):
        return None
    return _db_path(home).parent / ('outcome-' + attempt)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_outcome(home: Path, key: str, attempt: str, outcome: str) -> int:
    """Journal a definite consume outcome outside SQLite's lock.

    Written once per attempt (O_EXCL) in the owner-private authority folder, so
    no SQLite reader or writer can delay it. A short or torn write reads as
    unknown. Returns NOT_WRITTEN, WRITTEN (complete, durability unconfirmed: a
    later check may still reconcile it, but a crash can lose it) or DURABLE
    (file and directory fsynced).
    """
    try:
        path = _outcome_path(home, attempt)
        if path is None:
            return NOT_WRITTEN
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except (OSError, Unavailable):
        return NOT_WRITTEN
    data = (key + '\n' + attempt + '\n' + outcome + '\n').encode()
    state = NOT_WRITTEN
    try:
        try:
            offset = 0
            while offset < len(data):
                written = os.write(fd, data[offset:])
                if written <= 0:
                    return NOT_WRITTEN
                offset += written
            state = WRITTEN
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_dir(path.parent)
        return DURABLE
    except OSError:
        return state


def _read_outcome(home: Path, key: str, attempt: str) -> str | None:
    """The journalled outcome for exactly this account and attempt, else None.

    Like the database, the journal must be an owner-private regular file with a
    single link; its body must name the same account key and attempt id.
    """
    try:
        path = _outcome_path(home, attempt)
        if path is None:
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except (OSError, Unavailable):
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 256):
            return None
        lines = os.read(fd, 257).decode('ascii').split('\n')
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)
    if (len(lines) != 4 or lines[0] != key or lines[1] != attempt
            or lines[2] not in OUTCOMES or lines[3]):
        return None
    return lines[2]


def _discard_outcome(home: Path, attempt: str) -> None:
    # Only after the attempt is cleared in SQLite; a leftover file is inert
    # because reconciliation requires the state row to still hold its attempt,
    # and _housekeep sweeps it later.
    try:
        path = _outcome_path(home, attempt)
        if path is not None:
            path.unlink()
    except (OSError, Unavailable):
        pass


def _housekeep(conn, home: Path, key: str, settled: list) -> None:
    """Bounded best-effort cleanup inside the caller's write transaction.

    Sweeps ``outcome-*`` journals whose attempt no account still holds pending.
    The write lock stops any new attempt being admitted meanwhile, and a
    journal is only written after its attempt was committed, so every journal
    of a live attempt is in ``state``. Attempts cleared by this uncommitted
    transaction (``settled``) are kept: _settle removes them after COMMIT.
    Then prunes this account's settled ``attempts`` rows beyond the newest
    ATTEMPTS_KEEP; the current attempt and unsettled rows are never pruned.
    """
    try:
        live = {row[0] for row in conn.execute('SELECT attempt FROM state WHERE attempt IS NOT NULL')}
        live.update(settled)
        with os.scandir(_db_path(home).parent) as entries:
            for examined, entry in enumerate(entries):
                if examined >= SWEEP_LIMIT:
                    break
                attempt = entry.name[len('outcome-'):]
                if (entry.name.startswith('outcome-') and _canonical_attempt(attempt)
                        and attempt not in live):
                    try:
                        os.unlink(entry.path)
                    except OSError:
                        pass
    except (OSError, sqlite3.Error, Unavailable):
        pass
    try:
        conn.execute(
            'DELETE FROM attempts WHERE binding=? AND outcome IS NOT NULL '
            'AND attempt NOT IN (SELECT attempt FROM state WHERE attempt IS NOT NULL) '
            'AND attempt NOT IN (SELECT attempt FROM attempts WHERE binding=? AND outcome IS NOT NULL '
            'ORDER BY started DESC, rowid DESC LIMIT ?)', (key, key, ATTEMPTS_KEEP))
    except sqlite3.Error:
        pass


def _record(conn, key: str, attempt: str, outcome: str) -> None:
    """Record a definite outcome for this attempt only; idempotent per attempt."""
    done = conn.execute('SELECT outcome FROM attempts WHERE attempt=? AND binding=?',
                        (attempt, key)).fetchone()
    if done and done[0] is not None:
        if done[0] != outcome:
            raise _AttemptChanged('Shared reset attempt changed unexpectedly.')
        return  # already recorded by another check's reconciliation
    updated = conn.execute('UPDATE state SET attempt=NULL, result=? WHERE binding=? AND attempt=?',
                           (outcome, key, attempt))
    if updated.rowcount != 1:
        raise _AttemptChanged('Shared reset attempt changed unexpectedly.')
    conn.execute('INSERT INTO attempts(attempt,binding,started,outcome) VALUES (?,?,?,?) '
                 'ON CONFLICT(attempt) DO UPDATE SET outcome=excluded.outcome',
                 (attempt, key, time.time(), outcome))


def _row(conn, binding, home, account, settled=None):
    key = _account_key(account)
    conn.execute('INSERT OR IGNORE INTO state(binding) VALUES (?)', (key,))
    episode, attempt, result = conn.execute(
        'SELECT episode, attempt, result FROM state WHERE binding=?', (key,)).fetchone()
    if attempt:
        known = _read_outcome(home, key, attempt)
        if known is not None:
            # Reconcile a spend whose definite outcome was journalled but not
            # recorded here; the episode stays spent, so it cannot repeat.
            _record(conn, key, attempt, known)
            if settled is not None:
                settled.append(attempt)
            attempt, result = None, known
        elif result == 'pending':
            started = conn.execute('SELECT started FROM attempts WHERE attempt=?', (attempt,)).fetchone()
            if started is None or time.time() - started[0] > INFLIGHT_SECONDS:
                result = 'unrecorded'  # display only: no live redeem can still record it
    optin = conn.execute('SELECT auto FROM optin WHERE binding=? AND profile=?',
                         (binding, _profile_key(home))).fetchone()
    return (optin[0] if optin else 0, episode, attempt, result)


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _finalise(home: Path, key: str, attempt: str, outcome: str, journal) -> None:
    """Record a definite post-spend outcome with its own bounded busy budget.

    Every try is a fresh BEGIN IMMEDIATE transaction that re-verifies the attempt.
    Never retries the consume RPC and never reports lock contention as another
    redemption: an unrecordable outcome raises the distinct Unrecorded error.
    """
    deadline = time.monotonic() + FINALISE_SECONDS
    for tries in range(FINALISE_MAX_TRIES):
        remaining = deadline - time.monotonic()
        try:
            with _state(home, busy=max(0.0, min(FINALISE_TRY_SECONDS, remaining))) as conn:
                _record(conn, key, attempt, outcome)
            return
        except _AttemptChanged:
            raise
        except sqlite3.OperationalError:
            pass
        except Exception as exc:
            raise Unrecorded(outcome, journal) from exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        _pause(min(FINALISE_BACKOFF[min(tries, len(FINALISE_BACKOFF) - 1)], remaining))
    raise Unrecorded(outcome, journal)


def _mark_unrecorded(home: Path, key: str, attempt: str) -> None:
    # Best effort: the attempt stays pending (blocking) either way.
    try:
        with _state(home) as conn:
            conn.execute("UPDATE state SET result='unrecorded' WHERE binding=? AND attempt=?", (key, attempt))
    except Exception:
        pass


def _settle(home: Path, settled: list) -> None:
    for attempt in settled:
        _discard_outcome(home, attempt)


def observe(home: Path) -> dict:
    token, account, binding = _credentials()
    with AppServer(token, account) as app:
        limits = app.limits()
    view = _view(limits, binding)
    settled = []
    with _state(home) as conn:
        auto, episode, attempt, outcome = _row(conn, binding, home, account, settled)
        _housekeep(conn, home, _account_key(account), settled)
    _settle(home, settled)
    # The episode already spent (or attempted) stays unredeemable while the
    # provider still reports it, so neither the badge nor the monitor retries it.
    view.update(auto=bool(auto), blocked=bool(attempt), last_outcome=outcome,
                redeemable=(view['count'] is not None and view['count'] > 0 and view['exhausted']
                            and not attempt and view['episode'] != episode))
    return view


def set_auto(home: Path, binding: str, enabled: bool) -> dict:
    token, account, current = _credentials()
    if binding != current or type(enabled) is not bool:
        raise Unavailable('Selected Codex account changed; refresh before opting in.')
    with AppServer(token, account) as app:
        view = _view(app.limits(), current)
    settled = []
    with _state(home) as conn:
        _row(conn, current, home, account, settled)
        conn.execute('INSERT INTO optin(binding,profile,auto) VALUES (?,?,?) '
                     'ON CONFLICT(binding,profile) DO UPDATE SET auto=excluded.auto',
                     (current, _profile_key(home), int(enabled)))
    _settle(home, settled)
    return {**view, 'auto': enabled}


def redeem(home: Path, *, expected_binding: str, expected_episode: str | None,
           expected_count: int, automatic: bool = False, is_enabled=None) -> dict:
    token, account, binding = _credentials()
    if binding != expected_binding:
        raise Unavailable('Selected Codex account changed; refresh before redeeming.')
    key = _account_key(account)
    # Only the short admission/finalisation transactions lock SQLite. Network
    # calls never hold the global DB writer lock: other accounts are independent.
    with AppServer(token, account) as app:
        before = _view(app.limits(), binding)
        if (before['count'] is None or before['count'] < 1
                or before['count'] != expected_count or not before['exhausted']
                or before['episode'] != expected_episode):
            raise Unavailable('Balance or limit changed; refresh before redeeming.')
        attempt = str(uuid.uuid4())
        settled, refusal = [], None
        try:
            with _state(home) as conn:
                # Reconciles a journalled earlier outcome first; that commits even
                # when this admission is then refused.
                auto, prior, pending, _ = _row(conn, binding, home, account, settled)
                _housekeep(conn, home, key, settled)
                if pending or (automatic and (not auto or is_enabled is None or not is_enabled())):
                    refusal = 'Redemption is blocked or automatic use is disabled.'
                elif prior == expected_episode:
                    refusal = 'Balance or limit changed; refresh before redeeming.'
                else:
                    conn.execute('UPDATE state SET attempt=?, episode=?, result=? WHERE binding=?',
                                 (attempt, expected_episode, 'pending', key))
                    conn.execute('INSERT INTO attempts(attempt,binding,started) VALUES (?,?,?)',
                                 (attempt, key, time.time()))
        except sqlite3.OperationalError as exc:
            raise Unavailable('Another redemption is in progress.') from exc
        _settle(home, settled)
        if refusal:
            raise Unavailable(refusal)
        # Commit of the shared pending key precedes the irreversible RPC.
        # A crash or unknown response leaves it blocking every profile.
        try:
            result = app.call('account/rateLimitResetCredit/consume', {'idempotencyKey': attempt})
            outcome = result.get('outcome')
            if outcome not in OUTCOMES:
                raise Unavailable('Unknown Codex redemption result.')
        except BaseException:
            _mark_unrecorded(home, key, attempt)
            raise
        # The definite outcome is journalled before anything else can fail, so a
        # later check can reconcile it without another consume.
        journal = _write_outcome(home, key, attempt, outcome)
        try:
            after = _view(app.limits(), binding)
        except Unavailable as exc:
            # Limits must be read back before this call clears the attempt; the
            # next check (a fresh read) reconciles a journalled outcome instead.
            raise Unrecorded(outcome, journal) from exc
        _finalise(home, key, attempt, outcome, journal)
        # Unconditional: a short or unconfirmed write can leave a file too.
        _discard_outcome(home, attempt)
        return {'outcome': outcome, 'view': {**after, 'auto': bool(auto), 'blocked': False}}


def automatic_tick(home: Path, is_enabled) -> dict | None:
    # A full, account-bound service read IS the authoritative event observation;
    # no generic 429, dashboard percentage, or sparse notification is a trigger.
    view = observe(home)
    if not view['auto'] or not view['redeemable']:
        return None
    return redeem(home, expected_binding=view['binding'], expected_episode=view['episode'],
                  expected_count=view['count'], automatic=True, is_enabled=is_enabled)


_stop = threading.Event()
_worker: threading.Thread | None = None


def start_monitor(profile_rows, run_in_home, is_enabled):
    global _worker
    if _worker and _worker.is_alive():
        return
    _stop.clear()
    def loop():
        while not _stop.wait(POLL_SECONDS):
            # The host can disable an already-mounted plugin at runtime.
            # Stop acting even before its backend process is restarted.
            try:
                if not is_enabled():
                    continue
            except Exception:
                continue
            for row in profile_rows():
                if _stop.is_set():
                    break
                home = Path(row['path'])
                # No auth/ledger access for profiles that never opted in.
                try:
                    db = _coord_root(home) / 'codex-reset-authority' / 'state.sqlite3'
                except Exception:
                    continue
                if (not db.parent.is_dir() or db.parent.is_symlink() or
                        db.parent.stat().st_mode & 0o077 or not db.is_file() or db.is_symlink()):
                    continue
                try:
                    # Closed at once: under journal_mode=delete even a reader's
                    # SHARED lock delays another process's COMMIT.
                    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=BUSY_SECONDS)
                    try:
                        opted = conn.execute('SELECT 1 FROM optin WHERE profile=? AND auto=1 LIMIT 1',
                                             (_profile_key(home),)).fetchone()
                    finally:
                        conn.close()
                    if not opted:
                        continue
                    run_in_home(home, lambda: automatic_tick(home, is_enabled))
                except Exception:
                    # A blocked/uncertain state is visible in the UI; never retry
                    # an ambiguous consume by synthesizing a new attempt.
                    continue
    _worker = threading.Thread(target=loop, name='codex-reset-monitor', daemon=True)
    _worker.start()


def stop_monitor():
    _stop.set()
    if _worker and _worker.is_alive():
        _worker.join(timeout=2 * TOTAL_SECONDS + 5)
