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


class Unavailable(Exception):
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
def _state(home: Path):
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
    conn = sqlite3.connect(path, timeout=0.1, isolation_level=None)
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('CREATE TABLE IF NOT EXISTS state (binding TEXT PRIMARY KEY, episode TEXT, attempt TEXT, result TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS optin (binding TEXT NOT NULL, profile TEXT NOT NULL, auto INTEGER NOT NULL, PRIMARY KEY(binding,profile))')
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


def _row(conn, binding, home, account):
    key = _account_key(account)
    conn.execute('INSERT OR IGNORE INTO state(binding) VALUES (?)', (key,))
    state = conn.execute('SELECT episode, attempt, result FROM state WHERE binding=?', (key,)).fetchone()
    optin = conn.execute('SELECT auto FROM optin WHERE binding=? AND profile=?',
                         (binding, _profile_key(home))).fetchone()
    return (optin[0] if optin else 0, *state)


def observe(home: Path) -> dict:
    token, account, binding = _credentials()
    with AppServer(token, account) as app:
        limits = app.limits()
    view = _view(limits, binding)
    with _state(home) as conn:
        auto, episode, attempt, outcome = _row(conn, binding, home, account)
    view.update(auto=bool(auto), blocked=bool(attempt), last_outcome=outcome,
                redeemable=view['count'] is not None and view['count'] > 0 and view['exhausted'] and not attempt)
    return view


def set_auto(home: Path, binding: str, enabled: bool) -> dict:
    token, account, current = _credentials()
    if binding != current or type(enabled) is not bool:
        raise Unavailable('Selected Codex account changed; refresh before opting in.')
    with AppServer(token, account) as app:
        view = _view(app.limits(), current)
    with _state(home) as conn:
        _row(conn, current, home, account)
        conn.execute('INSERT INTO optin(binding,profile,auto) VALUES (?,?,?) '
                     'ON CONFLICT(binding,profile) DO UPDATE SET auto=excluded.auto',
                     (current, _profile_key(home), int(enabled)))
    return {**view, 'auto': enabled}


def redeem(home: Path, *, expected_binding: str, expected_episode: str | None,
           expected_count: int, automatic: bool = False, is_enabled=None) -> dict:
    token, account, binding = _credentials()
    if binding != expected_binding:
        raise Unavailable('Selected Codex account changed; refresh before redeeming.')
    # Only the short admission/finalisation transactions lock SQLite. Network
    # calls never hold the global DB writer lock: other accounts are independent.
    try:
        with AppServer(token, account) as app:
            before = _view(app.limits(), binding)
            if (before['count'] is None or before['count'] < 1
                    or before['count'] != expected_count or not before['exhausted']
                    or before['episode'] != expected_episode):
                raise Unavailable('Balance or limit changed; refresh before redeeming.')
            attempt = str(uuid.uuid4())
            with _state(home) as conn:
                auto, prior, pending, _ = _row(conn, binding, home, account)
                if pending or (automatic and (not auto or is_enabled is None or not is_enabled())):
                    raise Unavailable('Redemption is blocked or automatic use is disabled.')
                if prior == expected_episode:
                    raise Unavailable('Balance or limit changed; refresh before redeeming.')
                conn.execute('UPDATE state SET attempt=?, episode=?, result=? WHERE binding=?',
                             (attempt, expected_episode, 'pending', _account_key(account)))
            # Commit of the shared pending key precedes the irreversible RPC.
            # A crash or unknown response leaves it blocking every profile.
            result = app.call('account/rateLimitResetCredit/consume', {'idempotencyKey': attempt})
            outcome = result.get('outcome')
            if outcome not in ('reset', 'alreadyRedeemed', 'nothingToReset', 'noCredit'):
                raise Unavailable('Unknown Codex redemption result.')
            after = _view(app.limits(), binding)
            with _state(home) as conn:
                updated = conn.execute('UPDATE state SET attempt=NULL, result=? WHERE binding=? AND attempt=?',
                                       (outcome, _account_key(account), attempt))
                if updated.rowcount != 1:
                    raise Unavailable('Shared reset attempt changed unexpectedly.')
            return {'outcome': outcome, 'view': {**after, 'auto': bool(auto), 'blocked': False}}
    except sqlite3.OperationalError as exc:
        raise Unavailable('Another redemption is in progress.') from exc


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
                    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                        if not conn.execute('SELECT 1 FROM optin WHERE profile=? AND auto=1 LIMIT 1',
                                            (_profile_key(home),)).fetchone():
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
