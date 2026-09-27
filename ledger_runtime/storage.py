"""Per-profile durable event ledger. Request accounting is independent of Hermes state.db; metadata lookup is read-only."""
from __future__ import annotations
import contextvars, json, os, socket, sqlite3, threading, time, uuid, weakref
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from .accounting import METRICS, BUCKETS, costs, validate_rate, effective_record, usage_priority

LOCK=threading.RLock()
SCHEMA='''
CREATE TABLE IF NOT EXISTS session_context(session_id TEXT PRIMARY KEY, updated REAL NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, item_id TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, started REAL NOT NULL, ended REAL, provider TEXT, model TEXT, session_id TEXT, task TEXT, compression_id TEXT, status TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS request_time ON requests(started);
CREATE INDEX IF NOT EXISTS request_open_identity ON requests(id) WHERE status IN ('pending','usage_received') AND ended IS NULL;
CREATE INDEX IF NOT EXISTS request_session_time ON requests(session_id,started);
CREATE INDEX IF NOT EXISTS request_provider ON requests(provider,started);
CREATE INDEX IF NOT EXISTS request_comp ON requests(compression_id);
CREATE TABLE IF NOT EXISTS compressions(id TEXT PRIMARY KEY, started REAL NOT NULL, ended REAL, provider TEXT, session_id TEXT, session_after TEXT, next_request_id TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS comp_time ON compressions(started);
CREATE TABLE IF NOT EXISTS provider_catalog(id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, observed REAL NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS catalog_time ON provider_catalog(source_id,observed);
CREATE TABLE IF NOT EXISTS pricing_status(source_id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rates(id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tests(id TEXT PRIMARY KEY, started REAL NOT NULL, ended REAL, label TEXT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS health(process TEXT PRIMARY KEY, updated REAL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quota(seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, data TEXT NOT NULL);
'''
# Pure read-cost indexes: no data semantics, so reads are identical with or
# without them. Store() adds each one to new and existing ledgers on open (in
# its own savepoint, so a legacy row an expression cannot index skips that
# index instead of failing the open). Schema inventories treat each as
# optional-but-exact: absent is admissible, any other definition is not.
READ_INDEX_SQL={
    'event_kind':'CREATE INDEX IF NOT EXISTS event_kind ON events(kind)',
    # json_valid guards keep writes from ever failing on this index.
    'request_project_options':'CREATE INDEX IF NOT EXISTS request_project_options ON requests('+','.join(
        "CASE WHEN json_valid(data) THEN "+expr+" END" for expr in (
            "COALESCE(json_extract(data,'$.project_id'),'unattributed')", "json_extract(data,'$.project_label')",
            "json_extract(data,'$.project_path')", "json_extract(data,'$.project_source')"))+')',
}

def home():
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError: return Path(os.environ.get('HERMES_HOME',str(Path.home()/'.hermes')))

def iso(ts): return datetime.fromtimestamp(ts,timezone.utc).isoformat()
def jd(obj): return json.dumps(obj,ensure_ascii=False,separators=(',',':'),allow_nan=False)

_NOTIFY={}
_NOTIFY_LOCK=threading.Lock()
# A cached listener list is reused only while the folder's mtime is unchanged
# and was already older than this when listed (a same-tick bind stays visible
# on coarse-timestamp filesystems), and never for longer than NOTIFY_REGLOB.
NOTIFY_SETTLE=2.0
NOTIFY_REGLOB=5.0

def _listeners(folder):
    key=str(folder)
    try: mtime=os.stat(folder).st_mtime
    except OSError: return []
    now=time.time()
    with _NOTIFY_LOCK:
        hit=_NOTIFY.get(key)
        if hit and hit[0]==mtime and hit[1]-mtime>=NOTIFY_SETTLE and now-hit[1]<NOTIFY_REGLOB:
            return hit[2]
    paths=[str(p) for p in folder.glob('notify-*.sock')]
    with _NOTIFY_LOCK: _NOTIFY[key]=(mtime,now,paths)
    return paths

def _owner_dead(path):
    # api.py names listeners notify-<pid>-<hex>.sock; unknown names are kept.
    try: pid=int(Path(path).name.split('-')[1])
    except (IndexError,ValueError): return False
    if pid<=0: return False
    try: os.kill(pid,0)
    except ProcessLookupError: return True
    except OSError: return False
    return False

def notify(folder):
    # Producers emit tiny hints after durable writes. Missed hints never lose records.
    # Every currently listed listener gets a datagram on every call; a vanished
    # or refused listener drops the cache so the next call lists afresh.
    if not hasattr(socket,'AF_UNIX'): return
    paths=_listeners(folder)
    if not paths: return
    stale=False
    s=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM)
    try:
        s.settimeout(.005)
        for path in paths:
            try: s.sendto(b'changed',path)
            except (FileNotFoundError,ConnectionRefusedError) as exc:
                stale=True
                # Refused = nothing bound to that path. Unlink only when the
                # owning backend PID is also gone; a live owner cleans up itself.
                if isinstance(exc,ConnectionRefusedError) and _owner_dead(path):
                    try: os.unlink(path)
                    except OSError: pass
            except OSError: pass
    finally: s.close()
    if stale:
        with _NOTIFY_LOCK: _NOTIFY.pop(str(folder),None)

def synchronous_mode():
    """FULL unless HERMES_USAGE_LEDGER_SYNCHRONOUS=NORMAL explicitly opts in."""
    return 'NORMAL' if os.environ.get('HERMES_USAGE_LEDGER_SYNCHRONOUS','').strip().upper()=='NORMAL' else 'FULL'

_WRITERS=weakref.WeakSet()
_WRITERS_LOCK=threading.Lock()
_FORK_ORPHANS=[]
_HOOKS_REGISTERED=False
_EXITING=False
# At rest the ledger must look as it did with close-after-every-write: every
# commit in the main file, WAL empty. So a persistent Store is settled with
# PRAGMA wal_checkpoint(TRUNCATE) when its last in-process unit of work ends
# (a unit is the outermost producer hook, hook_scope, or a checkout outside a
# hook): like the former "last close checkpoints", concurrent hook threads of
# one process never checkpoint under each other, only the last to finish.
# Hooks thereby pay one checkpoint instead of connect/PRAGMA/close per write.
# The settled handle stays open for the rest of the burst only: once it has
# been unused for IDLE_SECONDS a daemon thread (one per process, never a hook
# thread) checkpoints and closes it, so at rest there is no open handle and,
# as with per-write close, no -wal/-shm sidecar. A settle never waits on the
# hook thread; one that cannot complete (another connection reading an old
# snapshot, another process writing) is retried by the same thread every
# SETTLE_RETRY_SECONDS, waiting up to SETTLE_RETRY_WAIT_MS each time, at most
# SETTLE_RETRIES times (the next write settles again in any case).
IDLE_SECONDS=1.0
SETTLE_RETRY_SECONDS=1.0
SETTLE_RETRY_WAIT_MS=1000
SETTLE_RETRIES=30
_IDLE=weakref.WeakKeyDictionary()
_IDLE_COND=threading.Condition()
_REAPER=None
_HOOK_SETTLE: contextvars.ContextVar['_HookUnits | None']=contextvars.ContextVar('usage_hook_settle',default=None)

def _identity(path):
    try: st=os.stat(path)
    except OSError: return None
    return (st.st_dev,st.st_ino)

def _checkpoint(c,wait_ms=0):
    """PRAGMA wal_checkpoint(TRUNCATE); hooks never wait on busy readers or
    writers (wait_ms=0), the idle reaper waits up to wait_ms. True when every
    frame reached the main file and the WAL was truncated."""
    try:
        c.execute(f'PRAGMA busy_timeout={int(wait_ms)}')
        try: row=c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        finally: c.execute('PRAGMA busy_timeout=2000')
        return row is not None and row[0]==0 and row[1]==row[2]
    except sqlite3.Error: return False
    finally:
        if isinstance(c,_WriterConnection): c.end_checkout()

def _schedule_retry(store,attempt=1):
    # attempt 0 = idle close (due IDLE_SECONDS after the handle's last use).
    global _REAPER
    if attempt>SETTLE_RETRIES: return
    due=(store.__dict__.get('_last_used') or time.monotonic())+IDLE_SECONDS if attempt==0 else time.monotonic()+SETTLE_RETRY_SECONDS
    with _IDLE_COND:
        if _EXITING: return
        _IDLE[store]=(due,attempt)
        if _REAPER is None:
            _REAPER=threading.Thread(target=_reap,name='usage-ledger-settle',daemon=True)
            _REAPER.start()
        else: _IDLE_COND.notify()

def _reap():
    # Exits (and is restarted by the next checkout) once nothing is pending.
    global _REAPER
    while True:
        with _IDLE_COND:
            while True:
                now=time.monotonic()
                if _EXITING or not _IDLE:
                    _REAPER=None; return
                ready=[(s,v[1]) for s,v in list(_IDLE.items()) if v[0]<=now]
                if ready:
                    for s,_ in ready: _IDLE.pop(s,None)
                    break
                _IDLE_COND.wait(min(v[0] for v in _IDLE.values())-now)
        for s,attempt in ready:
            try:
                # A unit in progress settles (and re-arms the close) when it ends.
                if s.__dict__.get('_units'): pass
                elif attempt==0: s._idle_close()
                elif s._settle(SETTLE_RETRY_WAIT_MS): _schedule_retry(s,0)
                else: _schedule_retry(s,attempt+1)
            except Exception: pass   # never let the retry thread die on one store
            del s
        ready=None

class _WriterConnection(sqlite3.Connection):
    """Tracks cursors of one checkout so none outlives it with an active
    statement (which would pin a stale WAL read snapshot for later checkouts)."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.checkout_cursors=[]
    def cursor(self,*args,**kwargs):
        cur=super().cursor(*args,**kwargs)
        self.checkout_cursors.append(weakref.ref(cur))
        return cur
    def execute(self,sql,parameters=(),/):
        return self.cursor().execute(sql,parameters)
    def executemany(self,sql,parameters,/):
        return self.cursor().executemany(sql,parameters)
    def executescript(self,script,/):
        return self.cursor().executescript(script)
    def end_checkout(self):
        refs,self.checkout_cursors=self.checkout_cursors,[]
        for ref in refs:
            cur=ref()
            if cur is not None: cur.close()

def _close_writers():
    # Committed data is already durable (per-commit sync). Closing at exit only
    # checkpoints the WAL as the former per-write close did; a connection busy
    # in a daemon thread is left alone rather than closed underneath it. Any
    # later write in this exiting process uses a fresh per-call connection.
    global _EXITING
    with _IDLE_COND:
        _EXITING=True
        _IDLE_COND.notify_all()
    with _WRITERS_LOCK: stores=list(_WRITERS)
    for store in stores:
        store.close_writer(timeout=1)

def _after_fork():
    # Never use or close an inherited SQLite handle in a forked child. The
    # child is single-threaded here, so no lock is taken; every module lock a
    # parent thread may have held at fork time is re-created instead (it would
    # never be released in the child). The parent's settle-retry thread does
    # not exist in the child; the next incomplete settle starts a new one.
    global LOCK,_WRITERS_LOCK,_NOTIFY_LOCK,_IDLE_COND,_IDLE,_REAPER
    LOCK=threading.RLock()
    _WRITERS_LOCK=threading.Lock()
    _NOTIFY_LOCK=threading.Lock()
    _IDLE_COND=threading.Condition()
    _IDLE=weakref.WeakKeyDictionary()
    _REAPER=None
    for store in list(_WRITERS):
        state=store.__dict__.get('_writer')
        if state is not None:_FORK_ORPHANS.append(state)
        store.__dict__['_writer']=None
        store.__dict__['_writer_lock']=threading.Lock()
        store.__dict__['_units']=0      # parent threads' units never end here

def _register_process_hooks():
    # Lazily, by the first persistent writer only: analytics reload
    # generations re-execute this module but never open a persistent writer.
    global _HOOKS_REGISTERED
    with _WRITERS_LOCK:
        if _HOOKS_REGISTERED: return
        _HOOKS_REGISTERED=True
    import atexit
    atexit.register(_close_writers)
    if hasattr(os,'register_at_fork'):
        os.register_at_fork(after_in_child=_after_fork)

_HOOK_PROJECTS: contextvars.ContextVar[dict | None]=contextvars.ContextVar('usage_hook_projects',default=None)

class _HookUnits(set):
    """Persistent Stores whose unit of work is the current hook. ``closed``
    once the hook has ended (a copied context may still reference it)."""
    closed=False

@contextmanager
def hook_scope():
    """One producer hook: projects.db claims are read at most once per root,
    and the hook is one unit of work for each persistent Store it writes:
    when the outermost scope exits and no other unit of that Store is in
    progress in this process, the Store is settled (WAL checkpointed into the
    main file and truncated).

    Work started with contextvars.copy_context() (or asyncio tasks) inside a
    hook shares these per-hook objects: while the hook runs its writes join
    the hook's unit; once the hook has returned they are their own units.
    """
    outer=_HOOK_PROJECTS.get() is None
    if not outer:
        yield; return
    units=_HookUnits()
    token=_HOOK_PROJECTS.set({}); settle_token=_HOOK_SETTLE.set(units)
    try: yield
    finally:
        _HOOK_SETTLE.reset(settle_token); _HOOK_PROJECTS.reset(token)
        with _WRITERS_LOCK: units.closed=True; stores=list(units)
        for store in stores:
            store._end_unit()

def projects_for(root):
    from .projects import read_projects
    cache=_HOOK_PROJECTS.get()
    if cache is None: return read_projects(root)
    key=str(root)
    if key not in cache: cache[key]=read_projects(root)
    return cache[key]

class Store:
    # Opt-in per instance (recorder producers): keep one write connection open
    # instead of connect/PRAGMA/commit/close per operation. Other Store users
    # (dashboard/API processes, tests) keep a fresh connection per db() call.
    persistent=False
    def __init__(self,root=None):
        self.root=Path(root) if root else home()
        self.folder=self.root/'usage-ledger'
        self.folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=self.folder/'events.sqlite3'
        # Existing ledgers are never migrated implicitly. Check for the base
        # table *under the writer lock*, not file existence: two processes can
        # observe the same just-created empty SQLite file before either seeds it.
        for attempt in range(3):
            try:
                with LOCK,self.db() as c:
                    c.execute('PRAGMA journal_mode=WAL')
                    c.execute('BEGIN IMMEDIATE')
                    fresh=not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='requests'").fetchone()
                    from .skills import SCHEMA as SKILLS_SCHEMA
                    # executescript() would COMMIT before the first DDL. Parse
                    # complete statements, including any future compound DDL,
                    # while retaining this one writer transaction.
                    statement=''
                    for line in (SCHEMA+SKILLS_SCHEMA).splitlines(keepends=True):
                        statement+=line
                        if sqlite3.complete_statement(statement):
                            c.execute(statement)
                            statement=''
                    if statement.strip():
                        raise ValueError('Incomplete ledger schema statement.')
                    for sql in READ_INDEX_SQL.values():
                        c.execute('SAVEPOINT read_index')
                        try:
                            c.execute(sql)
                        except sqlite3.OperationalError as exc:
                            if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
                                raise
                            c.execute('ROLLBACK TO read_index')
                        c.execute('RELEASE read_index')
                    if fresh:
                        from .incremental import install
                        install(c)
                break
            except sqlite3.OperationalError as exc:
                if 'locked' not in str(exc).lower() or attempt==2:
                    raise
                time.sleep(.05*(attempt+1))
        try: os.chmod(self.path,0o600)
        except OSError: pass
        from .pricing import seed
        seed(self)
    @contextmanager
    def db(self):
        if not self.persistent:
            c=self._connect()
            try:
                yield c; c.commit()
            except BaseException:
                c.rollback(); raise
            finally: c.close()
            return
        own=self._begin_unit()
        try:
            with self._checkout() as c:
                yield c
        finally:
            if own: self._end_unit()
    def _begin_unit(self):
        # Inside a hook the unit is the hook (ended by hook_scope); outside a
        # hook it is this checkout. True when the caller must end it.
        units=_HOOK_SETTLE.get()
        with _WRITERS_LOCK:
            hook=units is not None and not units.closed
            if hook and self in units: return False
            self.__dict__['_units']=self.__dict__.get('_units',0)+1
            if hook: units.add(self)
        return not hook
    def _end_unit(self):
        # The last in-process unit to end settles (never raises).
        with _WRITERS_LOCK:
            n=max(0,self.__dict__.get('_units',0)-1); self.__dict__['_units']=n
        if n==0: self.settle()
    @contextmanager
    def _checkout(self):
        if not _EXITING:
            lock=self.__dict__.get('_writer_lock')
            if lock is None:
                with _WRITERS_LOCK: lock=self.__dict__.setdefault('_writer_lock',threading.Lock())
            # Uncontended (the usual hook path): reuse the open connection.
            # Re-entrant or cross-thread use keeps the former independent
            # connection semantics instead of sharing a transaction or waiting.
            if lock.acquire(blocking=False):
                try:
                    with self._persistent_db() as c:
                        yield c
                finally:
                    self.__dict__['_last_used']=time.monotonic()
                    lock.release()
                return
        # A fresh-connection fallback is settled with its unit like any other
        # (beside a live persistent handle it leaves the WAL in place).
        c=self._connect()
        try:
            yield c; c.commit()
        except BaseException:
            c.rollback(); raise
        finally: c.close()
    def settle(self):
        """Settle now without waiting; hand an incomplete settle to the retry
        thread, and a settled open handle to the idle close."""
        try:
            if not self._settle(0): _schedule_retry(self)
            elif self.__dict__.get('_writer') is not None: _schedule_retry(self,0)
        except Exception: pass   # at-rest hygiene never fails a write
    def _idle_close(self):
        """Settle-thread only: checkpoint and close a handle unused for
        IDLE_SECONDS (a busy or recently used one is re-armed)."""
        lock=self.__dict__.get('_writer_lock')
        if lock is None or self.__dict__.get('_writer') is None: return
        if time.monotonic()<(self.__dict__.get('_last_used') or 0)+IDLE_SECONDS or not lock.acquire(blocking=False):
            _schedule_retry(self,0); return
        done=True
        try:
            state=self.__dict__.get('_writer')
            if state is None or self.__dict__.get('_units'): return   # a live unit re-arms it
            if state[2]==os.getpid() and state[1] is not None and state[1]==_identity(self.path):
                done=_checkpoint(state[0],SETTLE_RETRY_WAIT_MS)
            self._discard_writer()   # the last close also removes -wal/-shm
        finally: lock.release()
        if not done: _schedule_retry(self)
    def _settle(self,wait_ms=0):
        """TRUNCATE-checkpoint the ledger (the at-rest guarantee). True when
        the WAL is empty, or when another thread holds the handle (a checkout
        belongs to a unit that settles when it ends; a concurrent settle
        re-checks ``_settle_due`` after releasing it).

        A replaced/removed file is never checkpointed through the stale
        handle (its WAL was settled at its last write; the next checkout
        reconnects). Without a usable handle (discarded after a reader's TEMP
        objects or an error, or closed) a short connection settles instead.
        """
        lock=self.__dict__.get('_writer_lock')
        if lock is None: return True
        self.__dict__['_settle_due']=True
        done=True
        while self.__dict__.get('_settle_due'):
            if not lock.acquire(blocking=False): return True
            try:
                self.__dict__['_settle_due']=False
                state=self.__dict__.get('_writer')
                if state is None:
                    # mode=rw: a removed ledger is never re-created by a settle.
                    try:
                        uri=Path(os.path.abspath(self.path)).as_uri()+'?mode=rw'
                        with closing(sqlite3.connect(uri,uri=True,timeout=0)) as c: done=_checkpoint(c,wait_ms)
                    except sqlite3.Error: done=not self.path.exists()
                elif state[2]==os.getpid() and state[1] is not None and state[1]==_identity(self.path):
                    done=_checkpoint(state[0],wait_ms)
            finally: lock.release()
        return done
    def _connect(self,*,shared=False):
        # shared: the persistent writer is used by whichever thread holds
        # _writer_lock (never concurrently), so thread affinity is not checked.
        c=(sqlite3.connect(self.path,timeout=2,check_same_thread=False,factory=_WriterConnection) if shared
           else sqlite3.connect(self.path,timeout=2))
        c.row_factory=sqlite3.Row
        c.create_aggregate('decimal_sum',1,DecimalSum)
        c.create_function('canonical_rate',1,canonical_rate,deterministic=True)
        from .ownership import register_sql
        register_sql(c)
        c.execute('PRAGMA synchronous='+synchronous_mode())
        return c
    @contextmanager
    def _persistent_db(self):
        """One commit per checkout, as before; only connect/PRAGMA/close are saved.

        Caller holds _writer_lock. The handle is discarded (reconnect next time)
        after any error, TEMP objects left by a reader, an unfinished
        transaction, or a replaced/removed database file.
        """
        state=self.__dict__.get('_writer')
        identity=_identity(self.path)
        if state is not None and (identity is None or state[1]!=identity or state[2]!=os.getpid()):
            self._discard_writer()
            state=None
        from .ownership import register_sql
        if state is not None:
            try:
                # Fresh per-checkout execution_state cache and row factory,
                # exactly as a newly opened connection would have.
                state[0].row_factory=sqlite3.Row
                register_sql(state[0])
            except sqlite3.Error:
                self._discard_writer()
                state=None
        if state is None:
            c=self._connect(shared=True)
            state=(c,_identity(self.path),os.getpid())
            self.__dict__['_writer']=state
            with _WRITERS_LOCK: _WRITERS.add(self)
            _register_process_hooks()
        c=state[0]
        try:
            yield c
            c.end_checkout(); c.commit()
        except BaseException:
            try:
                c.end_checkout(); c.rollback()
            finally: self._discard_writer()
            raise
        try:
            # TEMP objects (e.g. a read() projection view shadowing requests)
            # or any pending transaction must never leak into the next write.
            dirty=c.in_transaction or c.execute('SELECT 1 FROM sqlite_temp_master LIMIT 1').fetchone() is not None
            c.end_checkout()
        except sqlite3.Error:
            dirty=True
        if dirty: self._discard_writer()
    def _discard_writer(self):
        state=self.__dict__.get('_writer')
        self.__dict__['_writer']=None
        if state is None: return
        if state[2]!=os.getpid():
            _FORK_ORPHANS.append(state); return
        try: state[0].close()
        except sqlite3.Error: pass
    def close_writer(self,timeout=-1):
        """Close the persistent handle (checkpointing as a last close does)."""
        lock=self.__dict__.get('_writer_lock')
        if lock is None: return True
        if not lock.acquire(timeout=timeout): return False
        try: self._discard_writer()
        finally: lock.release()
        return True
    def __del__(self):
        state=self.__dict__.get('_writer')
        if state is not None and state[2]==os.getpid():
            try: state[0].close()
            except Exception: pass
    def event(self,c,kind,key,data):
        c.execute('INSERT INTO events(ts,kind,item_id,data) VALUES(?,?,?,?)',(time.time(),kind,key,jd(data)))
    def latest_rate(self,c,rec):
        from .pricing import lookup
        return lookup(c,rec)
    def request(self,rec,kind,*,expected=None):
        rec=dict(rec); key=rec['id']
        with LOCK,self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT data FROM requests WHERE id=?',(key,)).fetchone()
            data=json.loads(old[0]) if old else {}
            old_cost=data.get('cost')
            # Compare-and-update inside the transaction, not against the earlier
            # cleanup SELECT. Another callback may already have completed it.
            if expected and (not old or any(data.get(k) not in values for k,values in expected.items())):return
            old_status=data.get('status');old_ended=data.get('ended')
            was_final=data.get('status') not in ('pending','usage_received',None)
            # Repeated pre/success notifications must not duplicate or downgrade a row.
            if old and kind=='request_started': return
            previous_usage=data.get('usage') or {}
            incoming=rec.get('usage')
            if isinstance(incoming,dict) and previous_usage:
                weaker=usage_priority(incoming.get('usage_source'))<usage_priority(previous_usage.get('usage_source'))
                empty=all(incoming.get(k) is None for k in METRICS) and any(previous_usage.get(k) is not None for k in METRICS)
                if weaker or empty:
                    for field in ('usage','response_model','returned_service_tier','provider_response_id',
                                  'returned_speed','returned_inference_geo','returned_usage_service_tier'):
                        rec.pop(field,None)
            if old and 'owner' in data:rec.pop('owner',None)
            data.update({k:v for k,v in rec.items() if v is not None})
            if was_final and rec.get('status') in ('pending','usage_received'):data['status']=old_status
            if old_status in ('abandoned_without_usage','abandoned_with_usage'):
                # Delayed hooks remain events, not proof of the abandoned
                # producer's actual end. They may still improve usage evidence.
                data['status']=old_status;data['ended']=old_ended
            if data.get('status')=='ended_without_usage' and any((data.get('usage') or {}).get(k) is not None for k in METRICS):
                data['status']='ended_with_usage'
            if data.get('status')=='abandoned_without_usage' and any((data.get('usage') or {}).get(k) is not None for k in METRICS):
                data['status']='abandoned_with_usage'
            data.setdefault('started',time.time());data.setdefault('status','pending')
            from .attribution import attach
            attach(c,data,projects_for(self.root))
            if 'usage' in data:
                # Final records keep the price snapshot chosen at completion, even
                # if a duplicate notification arrives after a catalog refresh.
                rate=old_cost.get('rate') if (was_final or kind=='request_abandoned') and old_cost else self.latest_rate(c,data)
                data['cost']=costs(data['usage'],rate)
            if kind=='request_started' and data.get('source')=='main_hook' and data.get('session_id'):
                candidates=c.execute("SELECT id,data FROM compressions WHERE session_after=? AND ended IS NOT NULL AND ended<=? AND next_request_id IS NULL ORDER BY ended DESC",(data['session_id'],data['started'])).fetchall()
                for i,row in enumerate(candidates):
                    comp=json.loads(row['data'])
                    if i==0:
                        comp['next_request_id']=key; comp['next_request_approx_tokens']=data.get('approx_input_tokens')
                    else: comp['next_request_id']='superseded';comp['next_context_status']='another compression intervened'
                    self._put_comp(c,comp)
            c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET ended=excluded.ended,provider=excluded.provider,model=excluded.model,session_id=excluded.session_id,task=excluded.task,compression_id=excluded.compression_id,status=excluded.status,data=excluded.data',
                (key,data['started'],data.get('ended'),data.get('provider','unknown'),data.get('model','unknown'),data.get('session_id',''),data.get('task','main'),data.get('compression_id'),data['status'],jd(data)))
            if data.get('usage'):
                for row in c.execute('SELECT data FROM compressions WHERE next_request_id=?',(key,)).fetchall():
                    comp=json.loads(row[0]);comp['next_request_prompt_tokens']=data['usage'].get('prompt_tokens');comp['next_usage_source']=data['usage'].get('usage_source');self._put_comp(c,comp)
            self.event(c,kind,key,rec)
        notify(self.folder)
    def _put_comp(self,c,data):
        c.execute('INSERT INTO compressions VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET ended=excluded.ended,provider=excluded.provider,session_after=excluded.session_after,next_request_id=excluded.next_request_id,data=excluded.data',
            (data['id'],data['started'],data.get('ended'),data.get('provider','unknown'),data.get('session_id',''),data.get('session_after'),data.get('next_request_id'),jd(data)))
    def compression(self,rec,kind='compression_updated'):
        with LOCK,self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT data FROM compressions WHERE id=?',(rec['id'],)).fetchone()
            data=json.loads(old[0]) if old else {}
            data.update({k:v for k,v in rec.items() if v is not None});data.setdefault('started',time.time())
            from .attribution import attach
            attach(c,data,projects_for(self.root))
            self._put_comp(c,data);self.event(c,kind,rec['id'],rec)
        notify(self.folder)
    def health(self,process,data):
        with self.db() as c: c.execute('INSERT OR REPLACE INTO health VALUES(?,?,?)',(process,time.time(),jd(data)))
    def save_quota(self,payload):
        # Deliberately keep quota windows, not provider details / account identity.
        data={'generated_at':payload.get('generated_at'),'providers':[]}
        for p in payload.get('providers',[]):
            q=p.get('quota') or {}
            windows=[{k:w.get(k) for k in ('label','remaining_percent','reset_at','used_percent')} for w in q.get('windows',[]) if isinstance(w,dict)]
            data['providers'].append({'provider':p.get('id'),'available':q.get('available'),'windows':windows})
        with self.db() as c: c.execute('INSERT INTO quota(ts,data) VALUES(?,?)',(time.time(),jd(data)))
    def rate(self,data):
        r=validate_rate(data)
        with self.db() as c:
            cur=c.execute('INSERT INTO rates(created,data) VALUES(?,?)',(time.time(),jd(r)));self.event(c,'rate_created',str(cur.lastrowid),r)
        notify(self.folder);return r
    def test(self,action,label='',key=None):
        now=time.time()
        with self.db() as c:
            if action=='start':
                key=uuid.uuid4().hex
                data={'id':key,'started':now,'label':str(label or 'Reset test')[:160]}
                c.execute('INSERT INTO tests VALUES(?,?,?,?,?)',(key,now,None,data['label'],jd(data)))
            elif action=='stop':
                row=c.execute('SELECT data FROM tests WHERE id=?',(key,)).fetchone()
                if not row: raise ValueError('Unknown test.')
                data=json.loads(row[0]);data['ended']=now
                c.execute('UPDATE tests SET ended=?,data=? WHERE id=?',(now,jd(data),key))
            else: raise ValueError('Action must be start or stop.')
            self.event(c,'test_'+action,key,data)
        notify(self.folder);return data
    def test_window(self,key,*,include_data=False):
        with self.db() as c:row=c.execute('SELECT started,ended,data FROM tests WHERE id=?',(key,)).fetchone()
        if not row:raise ValueError('Unknown test marker.')
        if include_data:return row['started'],row['ended'],json.loads(row['data'])
        return row['started'],row['ended']
    def _request_keys(self,start,end,provider='',session='',agent='',project='',session_scope='exact',subagent='',model='',model_provider='',*,limit):
        """Payload-free prefix from the same immutable snapshot used by read."""
        where,params,end=request_predicate(start,end,provider,session,agent,project,session_scope,subagent,model,model_provider)
        with self.db() as c:
            return [tuple(r) for r in c.execute('SELECT started,id FROM requests WHERE '+where+' ORDER BY started DESC,id DESC LIMIT ?',params+[limit])]

    def group_keys(self, field, scope):
        """Lightweight full-window identities/recency from a copied source, not group payloads."""
        where, params, _ = request_predicate(*scope)
        exprs, condition = group_dimensions(field)
        with self.db() as c:
            return [(tuple(row[i] for i in range(len(exprs))), row['latest_started'])
                    for row in c.execute('SELECT '+','.join(exprs)+',MAX(started) AS latest_started FROM requests WHERE '
                                         +where+condition+' GROUP BY '+','.join(exprs), params)]

    def read(self,start=0,end=None,provider='',offset=0,limit=200,session='',agent='',project='',session_scope='exact',subagent='',model='',model_provider='',*,trend_start=None,_detail_ids=None,_group_keys=None,view=None,group=None,_delta_seed=False,_validate_costs=False,_selected_test=None,list_mode='legacy',compression_kind='',_include_peak_private=False,_skip_peak=False):
        from .projection import selected_fields, manifest
        fields=selected_fields(view,group)
        include=lambda name: fields is None or name in fields
        list_field={'requests':'requests','compressions':'compressions','cache':'applied_rate_groups','models':'groups','overview':{'model':'model_groups','time':'trend','project':'project_groups','session':'session_groups','subagent':'subagent_groups'}.get(group)}.get(view)
        if list_mode!='legacy' and list_field is None:raise ValueError('Unsupported record list.')
        page_limit=20000 if list_mode=='all' else limit
        page_offset=0 if list_mode=='all' else offset
        where,params,end=request_predicate(start,end,provider,session,agent,project,session_scope,subagent,model,model_provider)
        with self.db() as c:
            # Keep totals, applied rates and request rows on one read snapshot.
            c.execute('BEGIN')
            from .incremental import watermark
            revision = watermark(c) if view is not None else None
            legacy_read_view(c,where,params)
            from .session_cache_writes import project as project_session_writes
            projected_costs=project_session_writes(c,where,params,end)
            from .session_cache_writes import materialise_summary
            summary_sql=materialise_summary(c,SUMMARY_SQL)
            total=sql_summary(c,where,params,summary_sql)
            group_page=(page_limit,page_offset) if list_mode=='page' else None
            group_where,group_params=where,params
            if _group_keys is not None:
                exprs, _ = group_dimensions(list_field)
                c.execute('CREATE TEMP TABLE read_group_keys('+','.join('x'+str(i) for i in range(len(exprs)))+')')
                c.executemany('INSERT INTO read_group_keys VALUES('+','.join('?' for _ in exprs)+')',_group_keys)
                group_where=(where+' AND EXISTS (SELECT 1 FROM read_group_keys WHERE '+
                             ' AND '.join('x'+str(i)+' IS '+expr for i,expr in enumerate(exprs))+')')
            list_total=None
            if list_mode!='legacy' and list_field=='requests' and total['attempts']>20000 and list_mode=='all':
                raise ValueError('Full record report exceeds the 20,000-row safety limit; narrow the window.')
            if list_mode!='legacy' and list_field in ('groups','model_groups','applied_rate_groups','project_groups','session_groups','subagent_groups'):
                effective="COALESCE(NULLIF(json_extract(data,'$.response_model'),''),NULLIF(model,''),'unknown')"
                role="COALESCE(json_extract(data,'$.agent_kind'),'unknown')"
                project_key="COALESCE(json_extract(data,'$.project_id'),'unattributed')"
                root_key="COALESCE(NULLIF(json_extract(data,'$.root_session_id'),''),NULLIF(session_id,''),'unattributed')"
                child_key="COALESCE(NULLIF(json_extract(data,'$.subagent_id'),''),NULLIF(session_id,''),'unattributed')"
                tier="COALESCE(NULLIF(json_extract(data,'$.cost.rate.service_tier'),''),NULLIF(json_extract(data,'$.returned_service_tier'),''),NULLIF(json_extract(data,'$.service_tier'),''),'unspecified')"
                keys={'groups':'provider,'+effective+','+role+',task',
                      'model_groups':'provider,'+effective,
                      'applied_rate_groups':'provider,'+effective+','+tier+",canonical_rate(json_extract(data,'$.cost.rate'))",
                      'project_groups':project_key,'session_groups':root_key,'subagent_groups':child_key}
                condition=" AND "+role+"='subagent'" if list_field=='subagent_groups' else ''
                list_total=c.execute('SELECT COUNT(*) FROM (SELECT 1 FROM requests WHERE '+where+condition+
                                     ' GROUP BY '+keys[list_field]+')',params).fetchone()[0]
                if list_mode=='all' and list_total>20000:
                    raise ValueError('Full record report exceeds the 20,000-row safety limit; narrow the window.')
            applied_rates=sql_applied_rate_groups(c,group_where if list_field=='applied_rate_groups' and _group_keys is not None else where,
                group_params if list_field=='applied_rate_groups' and _group_keys is not None else params,summary_sql,
                page=group_page if list_field=='applied_rate_groups' and _group_keys is None else None) if include('applied_rate_groups') else None
            rows=[]
            if include('requests'):
                if _detail_ids is None:
                    detail_where,detail_params=where,params+[page_limit,page_offset]
                    paging=' LIMIT ? OFFSET ?'
                else:
                    # TEMP membership avoids SQLite variable limits at the 2000-row API cap.
                    c.execute('CREATE TEMP TABLE read_detail_ids(id TEXT PRIMARY KEY)')
                    c.executemany('INSERT OR IGNORE INTO read_detail_ids VALUES(?)',((key,) for key in _detail_ids))
                    detail_where=where+' AND id IN (SELECT id FROM read_detail_ids)'
                    detail_params=params
                    paging=''
                rows=[json.loads(r[0]) for r in c.execute('SELECT data FROM requests WHERE '+detail_where+' ORDER BY started DESC,id DESC'+paging,detail_params)]
            read_progression, read_changes = None, {}
            if include('cache_read_progression'):
                from .cache_progression import query_progression
                read_progression, read_changes = query_progression(c,where,params,start,end,{r['id'] for r in rows})
            effective_model="COALESCE(NULLIF(json_extract(data,'$.response_model'),''),NULLIF(model,''),'unknown')"
            def group_source(field):
                selected=field==list_field and _group_keys is not None
                bounded=field==list_field and group_page and not selected
                return ((group_where if selected else where),
                        (group_params if selected else params)+(list(group_page or ()) if bounded else []),
                        ' LIMIT ? OFFSET ?' if bounded else '')
            groups=None
            if include('groups'):
                source, arguments, paging=group_source('groups')
                sql=("SELECT provider,"+effective_model+" AS model,COALESCE(json_extract(data,'$.agent_kind'),'unknown') AS agent_kind,task,"
                     "MAX(started) AS latest_started,"+summary_sql+" FROM requests WHERE "+source+
                     " GROUP BY provider,"+effective_model+",COALESCE(json_extract(data,'$.agent_kind'),'unknown'),task"
                     " ORDER BY latest_started DESC,provider,model,agent_kind,task"+paging)
                groups=[dict(provider=r['provider'],model=r['model'],agent_kind=r['agent_kind'],
                             task=r['task'],latest_started=r['latest_started'],**summary_from_sql(r))
                        for r in c.execute(sql,arguments)]
            provider_groups=[dict(provider=row['provider'],latest_started=row['latest_started'],**summary_from_sql(row)) for row in c.execute('SELECT provider,MAX(started) AS latest_started,'+summary_sql+' FROM requests WHERE '+where+' GROUP BY provider ORDER BY latest_started DESC,provider',params)]
            model_groups=None
            if include('model_groups'):
                source, arguments, paging=group_source('model_groups')
                sql=('SELECT provider,'+effective_model+' AS model,MAX(started) AS latest_started,'+summary_sql+
                     ' FROM requests WHERE '+source+' GROUP BY provider,'+effective_model+
                     ' ORDER BY latest_started DESC,provider,model'+paging)
                model_groups=[dict(provider=r['provider'],model=r['model'],latest_started=r['latest_started'],
                                   **summary_from_sql(r)) for r in c.execute(sql,arguments)]
            trend=sql_trend(c,where,params,start,end,summary_sql,trend_start=trend_start)
            attribution=attribution_groups(c,where,params,summary_sql,fields=fields,
                page_field=list_field if group_page and _group_keys is None else None,page=group_page,
                selected=(list_field,group_where,group_params) if _group_keys is not None else None)
            if not _skip_peak:
                from .observed_peak import apply as apply_observed_peak
                peak_report={'window':{'start':start,'end':end},'subagent_summary':attribution['subagent_summary'],
                             'provider_groups':provider_groups,'model_groups':model_groups,'trend':trend,
                             'project_groups':attribution.get('project_groups'),
                             'session_groups':attribution.get('session_groups')}
                scoped,peak_groups,unmatched,seen=apply_observed_peak(
                    c,peak_report,where,params,
                    scoped_filter=bool(provider or session or agent or project or subagent or model or model_provider))
            delta_seed=None
            precise=None
            if (_delta_seed or _validate_costs) and revision is not None and view=='overview' and group in ('time','model'):
                # Decimal(28) legacy sums can round. Group reassociation is
                # safe only when every partial sum fits in that context.
                from .incremental import safe_cost
                precise=False
                if total['attempts']<=100000:
                    # Reuse the projection's decoded costs (same rows, same
                    # values). Any raising row makes the outcome depend on scan
                    # order, so that case re-runs the original ordered check;
                    # so does any row-set difference from the original query.
                    precise=cost_admission(projected_costs,safe_cost)
                    if precise is not None and set(projected_costs)!={r[0] for r in
                            c.execute('SELECT id FROM requests WHERE '+where,params)}:
                        precise=None
                    if precise is None:
                        precise=all(safe_cost(json.loads(r[0])) for r in
                                    c.execute('SELECT data FROM requests WHERE '+where,params))
            if _delta_seed and precise:
                step=trend['seconds']
                # Same projected SQL and read transaction as public totals.
                by_provider={}
                group_count=0
                for r in c.execute('SELECT CAST(started / ? AS INTEGER) * ? AS bucket,provider,'+summary_sql+
                                   ' FROM requests WHERE '+where+' GROUP BY bucket,provider',
                                   [step,step]+params):
                    group_count+=1
                    if group_count>2048 or not isinstance(r['provider'],str):
                        by_provider=None
                        break
                    by_provider.setdefault(int(r['bucket']),{})[r['provider']]=summary_from_sql(r)
                if by_provider is not None:
                    role="COALESCE(json_extract(data,'$.agent_kind'),'unknown')"
                    by_subagent={}
                    for r in c.execute('SELECT CAST(started / ? AS INTEGER) * ? AS bucket,'+summary_sql+
                                       ' FROM requests WHERE '+where+' AND '+role+"='subagent' GROUP BY bucket",
                                       [step,step]+params):
                        by_subagent[int(r['bucket'])]=summary_from_sql(r)
                        if len(by_subagent)>400:
                            by_subagent=None
                            break
                    if by_subagent is not None:
                        delta_seed={'provider':by_provider,'subagent':by_subagent}
                        if group=='model':
                            by_model={}
                            for r in c.execute('SELECT CAST(started / ? AS INTEGER) * ? AS bucket,provider,'+
                                               effective_model+' AS model,'+summary_sql+
                                               ' FROM requests WHERE '+where+' GROUP BY bucket,provider,'+effective_model,
                                               [step,step]+params):
                                group_count+=1
                                if group_count>4096:
                                    delta_seed=None
                                    break
                                by_model.setdefault(int(r['bucket']),{})[(r['provider'],r['model'])]=summary_from_sql(r)
                            if delta_seed is not None:
                                # JSON object keys must be strings for signed tokens.
                                delta_seed['model']={bucket:{json.dumps(key):value for key,value in groups.items()}
                                                     for bucket,groups in by_model.items()}
            if include('price_catalogs'):
                from .pricing import catalog_status
                catalogs=catalog_status(c,provider)
            else: catalogs=None
            for row in rows:
                row['execution_state']=c.execute('SELECT execution_state(?)',(jd(row),)).fetchone()[0]
                if row['id'] in read_changes:row['cache_read_change']=read_changes[row['id']]
            compwhere=where.replace('requests.data','compressions.data').replace(
                "COALESCE(NULLIF(json_extract(data,'$.response_model'),''),NULLIF(model,''),'unknown')",
                "COALESCE(NULLIF(json_extract(data,'$.model'),''),'unknown')")
            compression_count=c.execute('SELECT COUNT(*) FROM compressions WHERE '+compwhere,params).fetchone()[0]
            list_compwhere=compwhere+(" AND json_extract(data,'$.kind')=?" if compression_kind else '')
            list_compparams=params+([compression_kind] if compression_kind else [])
            list_compcount=c.execute('SELECT COUNT(*) FROM compressions WHERE '+list_compwhere,list_compparams).fetchone()[0] if list_field=='compressions' and list_mode!='legacy' else compression_count
            comps=[]
            if include('compressions'):
                comps=[json.loads(r[0]) for r in c.execute('SELECT data FROM compressions WHERE '+list_compwhere+' ORDER BY started DESC,id DESC LIMIT ? OFFSET ?',list_compparams+[page_limit,page_offset])] if list_field=='compressions' and list_mode!='legacy' else [json.loads(r[0]) for r in c.execute('SELECT data FROM compressions WHERE '+compwhere+' ORDER BY started DESC,id DESC LIMIT 1000',params)]
                # Compression bill is derived from its linked requests, never counted twice.
                for comp in comps:
                    aux=[json.loads(r[0]) for r in c.execute('SELECT data FROM requests WHERE compression_id=?',(comp['id'],))]
                    comp['auxiliary']=summary(aux);comp['aux_request_ids']=[r['id'] for r in aux]
            health=[dict(process=r['process'],updated=r['updated'],**json.loads(r['data'])) for r in c.execute('SELECT * FROM health ORDER BY updated DESC')] if include('health') else None
            rates=[dict(id=r['id'],created=r['created'],**json.loads(r['data'])) for r in c.execute('SELECT * FROM rates ORDER BY created DESC,id DESC LIMIT 500')] if include('rates') else None
            tests=[json.loads(r[0]) for r in c.execute('SELECT data FROM tests ORDER BY started DESC,id DESC LIMIT 100')]
            # The caller's point lookup is against this same copied ledger. Keep
            # the ordinary recent prefix intact; expose its factual selection
            # once even if it is older than the bounded list.
            if _selected_test is not None and not any(t['id']==_selected_test['id'] for t in tests):
                tests.append(_selected_test)
            # temp.requests is main.requests LEFT JOIN a PK-keyed projection with
            # r.provider, so the covering request_provider index gives the same list.
            providers=[r[0] for r in c.execute('SELECT DISTINCT provider FROM main.requests ORDER BY provider')]
            seq=c.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            quota=[dict(ts=r['ts'],**json.loads(r['data'])) for r in c.execute('SELECT ts,data FROM quota WHERE ts>=? AND ts<? ORDER BY ts DESC LIMIT 200',(start,end))] if include('quota_observations') else None
            crossing_end=c.execute('SELECT COUNT(*) FROM requests WHERE '+where+' AND (ended IS NULL OR ended>=?)',params+[end]).fetchone()[0] if include('crossing_end') else None
            crossing=c.execute('SELECT COUNT(*) FROM requests WHERE started<? AND (ended IS NULL OR ended>=?)'+where[len('started>=? AND started<?'):],[start,start]+params[2:]).fetchone()[0] if include('crossing_start') else None
        result={'generated_at':time.time(),'seq':seq,'window':{'start':start,'end':end,'basis':'request/compaction start time; crossing requests are not split'},
          **attribution,'summary':total,'requests':rows,'request_count':total['attempts'],'next_offset':offset+limit if offset+limit<total['attempts'] else None,
          'compressions':comps,'compression_count':compression_count,'compression_truncated':compression_count>1000,
          'groups':groups,'provider_groups':provider_groups,'model_groups':model_groups,'trend':trend,'price_catalogs':catalogs,
          'applied_rate_groups':applied_rates,'cache_read_progression':read_progression,
          'providers':providers,'health':health,'rates':rates,'tests':tests,'quota_observations':quota,'crossing_start':crossing,'crossing_end':crossing_end}
        if _include_peak_private and not _skip_peak:
            result.update(_peak_intervals=scoped,_peak_groups=peak_groups,_peak_unmatched=unmatched,_peak_seen=seen)
        if list_mode!='legacy':
            if list_field=='requests':total_rows=result['request_count']
            elif list_field=='compressions':total_rows=list_compcount
            elif list_field=='trend':total_rows=len(trend['buckets'])
            else:total_rows=list_total if list_total is not None else len(result[list_field])
            if list_mode=='all' and total_rows>20000:
                raise ValueError('Full record report exceeds the 20,000-row safety limit; narrow the window.')
            result['list_count']=total_rows
            result['list_next_offset']=page_offset+page_limit if list_mode=='page' and page_offset+page_limit<total_rows else None
            if list_field=='trend':
                result['list_rows']=list(reversed(trend['buckets']))[page_offset:page_offset+page_limit]
            elif list_field not in ('requests','compressions') and not (list_total is not None and group_page):
                result[list_field]=result[list_field][page_offset:page_offset+page_limit]
        if fields is None:return result
        projected={**{key:result[key] for key in fields},'projection':manifest(fields),
                   **{key:result[key] for key in ('_peak_intervals','_peak_groups','_peak_unmatched','_peak_seen') if key in result}}
        if list_mode!='legacy':
            projected.update(list_count=result['list_count'],list_next_offset=result['list_next_offset'])
            if list_field=='trend':projected['list_rows']=result['list_rows']
        if revision is not None:
            projected['incremental']={'version':1,'revision':revision,'mode':'snapshot'}
        if delta_seed is not None:
            projected['_delta_seed']=delta_seed
        if _validate_costs:
            projected['_delta_cost_safe']=bool(precise)
        return projected

def request_predicate(start,end,provider,session,agent,project,session_scope,subagent,model='',model_provider=''):
    if agent not in ('','primary','subagent','unknown'):raise ValueError('Invalid agent filter.')
    if session_scope not in ('exact','family'):raise ValueError('Invalid session scope.')
    end=time.time() if end is None else end
    if start<0 or end<start: raise ValueError('Invalid time window.')
    where='started>=? AND started<?';params=[start,end]
    if provider: where+=' AND provider=?';params.append(provider)
    if model_provider: where+=' AND provider=?';params.append(model_provider)
    if model:
        where+=" AND COALESCE(NULLIF(json_extract(data,'$.response_model'),''),NULLIF(model,''),'unknown')=?"
        params.append(model)
    if session:
        if session_scope=='family':
            where+=" AND (session_id=? OR EXISTS (SELECT 1 FROM json_each(requests.data,'$.session_lineage') WHERE value=?))";params.extend([session,session])
        else:where+=' AND session_id=?';params.append(session)
    if agent:where+=" AND COALESCE(json_extract(data,'$.agent_kind'),'unknown')=?";params.append(agent)
    if subagent:
        where+=" AND COALESCE(NULLIF(json_extract(data,'$.subagent_id'),''),session_id)=?";params.append(subagent)
    if project:
        where+=" AND COALESCE(json_extract(data,'$.project_id'),'unattributed')=?";params.append(project)
    return where,params,end


def summary(rows):
    out={'sessions':len({r.get('session_id') for r in rows if r.get('session_id')}),'attempts':len(rows),'pending':0,'unresolved':0,'abandoned':0,'missing_usage':0,'partial_breakdown':0,'aggregate_records':0,
         'known':{k:0 for k in METRICS},'missing_fields':{k:0 for k in METRICS},
         'missing_reasons':{k:{r:0 for r in ('awaiting_usage','unresolved_execution','abandoned_execution','unverified_accounting','ended_without_usage','unreported_field')} for k in METRICS},
         'cost_components':{k:Decimal(0) for k in BUCKETS},'cost_missing_fields':{k:0 for k in BUCKETS},'unpriced_requests':0,'priced_requests':0,'supplemental_requests':0,'known_cost_usd':Decimal(0)}
    savings_keys=('cache_read_savings_usd','cache_write_premium_usd','cache_savings_usd')
    out['savings']={k:Decimal(0) for k in savings_keys};out['savings_missing']={k:0 for k in savings_keys}
    from .session_cache_writes import blank_summary, add_summary
    out['session_cache_writes']=blank_summary()
    from .ownership import execution_state
    owner_cache={}
    for r in rows:
        r=effective_record(r)
        state=execution_state(r,owner_cache)
        if state=='unresolved':out['unresolved']+=1
        if state=='abandoned':out['abandoned']+=1
        add_summary(out['session_cache_writes'],r)
        u=r.get('usage') or {}
        if state=='owner_live':out['pending']+=1
        if u.get('total_tokens') is None:out['missing_usage']+=1
        if any(u.get(k) is None for k in BUCKETS):out['partial_breakdown']+=1
        if u.get('request_count',1)>1:out['aggregate_records']+=1
        for k in METRICS:
            if u.get(k) is None:
                out['missing_fields'][k]+=1
                reason=('awaiting_usage' if state=='owner_live' else
                        'unresolved_execution' if state=='unresolved' else
                        'abandoned_execution' if state=='abandoned' else
                        'unverified_accounting' if (u.get('field_provenance') or {}).get(k) in ('unverified_normalized_zero','unverified_cache_decomposition') else
                        'ended_without_usage' if r.get('ended') is not None and all(u.get(m) is None for m in METRICS) else 'unreported_field')
                out['missing_reasons'][k][reason]+=1
            else:out['known'][k]+=u[k]
        cost=r.get('cost') or {}
        if r.get('supplemental_valuation'):out['supplemental_requests']+=1
        if cost.get('complete'):out['priced_requests']+=1
        else:out['unpriced_requests']+=1
        for k in BUCKETS:
            value=cost.get('components',{}).get(k)
            if value is None:out['cost_missing_fields'][k]+=1
            else:out['cost_components'][k]+=Decimal(value)
        out['known_cost_usd']+=Decimal(cost.get('known_components_usd','0'))
        for k in savings_keys:
            if cost.get(k) is None:out['savings_missing'][k]+=1
            else:out['savings'][k]+=Decimal(cost[k])
    out['savings']={k:str(v) for k,v in out['savings'].items()}
    out['cost_components']={k:str(v) for k,v in out['cost_components'].items()}
    out['known_cost_usd']=str(out['known_cost_usd'])
    # Weighted rate only where every read/input denominator is known across this set.
    out['cache_hit_rate']=out['known']['cache_read_tokens']/out['known']['prompt_tokens'] if out['known']['prompt_tokens'] and not out['missing_fields']['cache_read_tokens'] and not out['missing_fields']['prompt_tokens'] else None
    return out


class DecimalSum:
    def __init__(self):self.total=Decimal(0)
    def step(self,value):
        if value is not None:self.total+=Decimal(str(value))
    def finalize(self):return str(self.total)

def _exprs():
    cols=["COUNT(DISTINCT NULLIF(session_id,'')) AS sessions", "COUNT(*) AS attempts", "COALESCE(SUM(execution_state(data)='owner_live'),0) AS pending",
      "COALESCE(SUM(execution_state(data)='unresolved'),0) AS unresolved",
      "COALESCE(SUM(execution_state(data)='abandoned'),0) AS abandoned",
      "COALESCE(SUM(json_extract(data,'$.usage.total_tokens') IS NULL),0) AS missing_usage",
      "COALESCE(SUM(json_extract(data,'$.usage.request_count')>1),0) AS aggregate_records",
      "COALESCE(SUM("+' OR '.join("json_extract(data,'$.usage."+k+"') IS NULL" for k in BUCKETS)+"),0) AS partial_breakdown",
      "COALESCE(SUM(json_extract(data,'$.cost.complete')=1),0) AS priced_requests",
      "COALESCE(SUM(json_extract(data,'$.supplemental_valuation.basis')='current_published_rate_for_past_usage'),0) AS supplemental_requests",
      "COALESCE(decimal_sum(json_extract(data,'$.cost.known_components_usd')),'0') AS known_cost_usd"]
    cols += ["COALESCE(SUM(json_extract(data,'$.calculated_cache_writes.tokens')),0) AS cw_tokens",
      "COUNT(json_extract(data,'$.calculated_cache_writes.tokens')) AS cw_compared",
      "COALESCE(SUM(json_extract(data,'$.calculated_cache_writes.status')='baseline'),0) AS cw_baselines",
      "COALESCE(SUM(json_extract(data,'$.calculated_cache_writes.tokens') IS NULL),0) AS cw_missing"]
    for k in METRICS:
        cols += ["COALESCE(SUM(json_extract(data,'$.usage."+k+"')),0) AS k_"+k,"COALESCE(SUM(json_extract(data,'$.usage."+k+"') IS NULL),0) AS m_"+k]
        reason=("CASE WHEN execution_state(data)='owner_live' THEN 'awaiting_usage' "
                "WHEN execution_state(data)='unresolved' THEN 'unresolved_execution' "
                "WHEN execution_state(data)='abandoned' THEN 'abandoned_execution' "
                "WHEN json_extract(data,'$.usage.field_provenance."+k+"') IN ('unverified_normalized_zero','unverified_cache_decomposition') THEN 'unverified_accounting' "
                "WHEN ended IS NOT NULL AND COALESCE("+','.join("json_extract(data,'$.usage."+m+"')" for m in METRICS)+") IS NULL THEN 'ended_without_usage' ELSE 'unreported_field' END")
        for name in ('awaiting_usage','unresolved_execution','abandoned_execution','unverified_accounting','ended_without_usage','unreported_field'):
            cols.append("COALESCE(SUM(json_extract(data,'$.usage."+k+"') IS NULL AND ("+reason+")='"+name+"'),0) AS mr_"+k+'_'+name)
    for k in BUCKETS:
        cols += ["COALESCE(decimal_sum(json_extract(data,'$.cost.components."+k+"')),'0') AS c_"+k,"COALESCE(SUM(json_extract(data,'$.cost.components."+k+"') IS NULL),0) AS cm_"+k]
    for k in ('cache_read_savings_usd','cache_write_premium_usd','cache_savings_usd'):
        cols += ["COALESCE(decimal_sum(json_extract(data,'$.cost."+k+"')),'0') AS s_"+k,"COALESCE(SUM(json_extract(data,'$.cost."+k+"') IS NULL),0) AS sm_"+k]
    # Avoid Python/JSON calls for terminal history; inspect only open owners.
    from .session_cache_writes import READ_STATE_SQL
    state=READ_STATE_SQL
    return ','.join(cols).replace('execution_state(data)',state)
SUMMARY_SQL=_exprs()

def summary_from_sql(row):
    out={k:row[k] for k in ('sessions','attempts','pending','unresolved','abandoned','missing_usage','aggregate_records','partial_breakdown','priced_requests','supplemental_requests','known_cost_usd')}
    from .session_cache_writes import blank_summary
    out['session_cache_writes']=dict(blank_summary(),tokens=row['cw_tokens'],compared_requests=row['cw_compared'],baseline_requests=row['cw_baselines'],missing_requests=row['cw_missing'])
    out['unpriced_requests']=out['attempts']-out['priced_requests']
    keys=('cache_read_savings_usd','cache_write_premium_usd','cache_savings_usd')
    out['savings']={k:row['s_'+k] for k in keys};out['savings_missing']={k:row['sm_'+k] for k in keys}
    out['missing_reasons']={k:{name:row['mr_'+k+'_'+name] for name in ('awaiting_usage','unresolved_execution','abandoned_execution','unverified_accounting','ended_without_usage','unreported_field')} for k in METRICS}
    for group,prefix,fields in [('known','k_',METRICS),('missing_fields','m_',METRICS),('cost_components','c_',BUCKETS),('cost_missing_fields','cm_',BUCKETS)]:
        out[group]={k:row[prefix+k] for k in fields}
    out['cache_hit_rate']=out['known']['cache_read_tokens']/out['known']['prompt_tokens'] if out['known']['prompt_tokens'] and not out['missing_fields']['cache_read_tokens'] and not out['missing_fields']['prompt_tokens'] else None
    return out
def sql_summary(c,where,params,summary_sql=SUMMARY_SQL):
    return summary_from_sql(c.execute('SELECT '+summary_sql+' FROM requests WHERE '+where,params).fetchone())


def sql_trend(c,where,params,start,end,summary_sql=SUMMARY_SQL,*,trend_start=None):
    """Aggregate the complete filtered ledger, not the paginated request table.

    UTC calendar buckets clipped to the selected bounds. A bucket with attempts
    but no measured value remains unknown in the UI, not a zero-consumption claim.
    """
    if trend_start is not None:
        start=trend_start
    elif start==0:
        first=c.execute('SELECT MIN(started) FROM requests WHERE '+where,params).fetchone()[0]
        start=first if first is not None else max(0,end-86400)
    # Allow short windows above one hour: rolling start and server end are sampled separately.
    duration=end-start
    step=60 if duration<=1800 else (120 if duration<=7200 else (3600 if duration<=172800 else 86400))
    if (end-start)/step>400:step=86400*max(1,int((end-start)/86400/400)+1)
    lo=int(start//step)*step
    rows={int(row['bucket']):summary_from_sql(row) for row in c.execute(
      'SELECT CAST(started / ? AS INTEGER) * ? AS bucket,'+summary_sql+' FROM requests WHERE '+where+' GROUP BY bucket', [step,step]+params)}
    provider_rows={}
    for row in c.execute('SELECT CAST(started / ? AS INTEGER) * ? AS bucket,provider,'+summary_sql+
                         ' FROM requests WHERE '+where+' GROUP BY bucket,provider', [step,step]+params):
        provider_rows.setdefault(int(row['bucket']), []).append(
            {'provider':row['provider'], **summary_from_sql(row)})
    blank=summary([])
    buckets=[dict(start=max(start,t),end=min(end,t+step),**rows.get(t,blank),
                  provider_buckets=sorted(provider_rows.get(t,[]),key=lambda r:r['provider']))
             for t in range(lo,int(end)+1,step) if t<end]
    return {'unit':'minute' if step==60 else ('2 minutes' if step==120 else ('hour' if step==3600 else ('day' if step==86400 else str(step//86400)+' days'))),'timezone':'UTC','seconds':step,'buckets':buckets}


def canonical_rate(value):
    """Normalise saved JSON identity without collapsing distinct rate revisions."""
    if value is None:return None
    try:decoded=json.loads(value) if isinstance(value,str) else value
    except json.JSONDecodeError:decoded=value
    return json.dumps(decoded,sort_keys=True,separators=(',',':'))

def group_dimensions(field):
    """Exact raw SQL grouping identities, shared by key selection and hydration."""
    model="COALESCE(NULLIF(json_extract(data,'$.response_model'),''),NULLIF(model,''),'unknown')"
    role="COALESCE(json_extract(data,'$.agent_kind'),'unknown')"
    project="COALESCE(json_extract(data,'$.project_id'),'unattributed')"
    root="COALESCE(NULLIF(json_extract(data,'$.root_session_id'),''),NULLIF(session_id,''),'unattributed')"
    child="COALESCE(NULLIF(json_extract(data,'$.subagent_id'),''),NULLIF(session_id,''),'unattributed')"
    tier="COALESCE(NULLIF(json_extract(data,'$.cost.rate.service_tier'),''),NULLIF(json_extract(data,'$.returned_service_tier'),''),NULLIF(json_extract(data,'$.service_tier'),''),'unspecified')"
    dimensions={'groups':('provider',model,role,'task'),'model_groups':('provider',model),
                'applied_rate_groups':('provider',model,tier,"canonical_rate(json_extract(data,'$.cost.rate'))"),
                'project_groups':(project,),'session_groups':(root,),'subagent_groups':(child,)}
    return dimensions[field], (' AND '+role+"='subagent'" if field=='subagent_groups' else '')

def attribution_groups(c,where,params,summary_sql=SUMMARY_SQL,*,fields=None,page_field=None,page=None,selected=None):
    """Complete filtered-set rollups; project only selected groups when opted in."""
    include=lambda name: fields is None or name in fields
    role="COALESCE(json_extract(data,'$.agent_kind'),'unknown')"
    project="COALESCE(json_extract(data,'$.project_id'),'unattributed')"
    root="COALESCE(NULLIF(json_extract(data,'$.root_session_id'),''),NULLIF(session_id,''),'unattributed')"
    child="COALESCE(NULLIF(json_extract(data,'$.subagent_id'),''),NULLIF(session_id,''),'unattributed')"
    subs=sql_summary(c,where+" AND "+role+"='subagent'",params,summary_sql)
    subs['agents']=c.execute('SELECT COUNT(DISTINCT '+child+') FROM requests WHERE '+where+" AND "+role+"='subagent'",params).fetchone()[0]
    out={'subagent_summary':subs}
    if include('agent_groups'):
        out['agent_groups']=[dict(agent_kind=r['key'],latest_started=r['latest_started'],**summary_from_sql(r)) for r in c.execute('SELECT '+role+' AS key,MAX(started) AS latest_started,'+summary_sql+' FROM requests WHERE '+where+' GROUP BY '+role,params)]
    extra=",COALESCE(SUM(CASE WHEN "+role+"='subagent' THEN json_extract(data,'$.usage.total_tokens') ELSE 0 END),0) AS subagent_tokens,COUNT(DISTINCT CASE WHEN "+role+"='subagent' THEN "+child+" END) AS subagents"
    configs=[('project_groups',project,",MAX(json_extract(data,'$.project_label')) AS label,MAX(json_extract(data,'$.project_path')) AS path,MAX(json_extract(data,'$.project_source')) AS basis",''),
      ('session_groups',root,",MAX(json_extract(data,'$.project_label')) AS project_label",''),
      ('subagent_groups',child,",MIN(session_id) AS session_id,MAX(json_extract(data,'$.parent_session_id')) AS parent_session_id,MAX(json_extract(data,'$.root_session_id')) AS root_session_id,MAX(json_extract(data,'$.agent_role')) AS agent_role,MAX(json_extract(data,'$.project_label')) AS project_label", " AND "+role+"='subagent'")]
    for name,expr,more,condition in configs:
        if not include(name):continue
        bounded=page if page_field==name else None
        source_where,source_params=(selected[1],selected[2]) if selected is not None and name==selected[0] else (where,params)
        rows=c.execute('SELECT '+expr+' AS key'+more+extra+',MAX(started) AS latest_started,'+summary_sql+' FROM requests WHERE '+source_where+condition+' GROUP BY '+expr+' ORDER BY latest_started DESC,key'+(' LIMIT ? OFFSET ?' if bounded else ''),source_params+list(bounded) if bounded else source_params)
        out[name]=[dict(**{k:r[k] for k in r.keys() if k in ('key','label','path','basis','project_label','session_id','parent_session_id','root_session_id','agent_role','subagent_tokens','subagents','latest_started')},**summary_from_sql(r)) for r in rows]
    out['project_options']=project_options(c)
    return out


def project_options(c):
    """All-time project filter options (never window- or filter-scoped).

    The json_valid-guarded expressions match request_project_options, so an
    indexed ledger answers from main.requests without decoding every payload
    (an unindexed one scans as before). The original query read the TEMP
    ``requests`` view; its window-bounded projected rows are checked to carry
    the same project fields as main.requests. A NULL guarded key (a row
    json_valid rejects) or any projected difference/error runs the original
    unguarded query, preserving its exact result or error.
    """
    project="COALESCE(json_extract(data,'$.project_id'),'unattributed')"
    fields=(project,"json_extract(data,'$.project_label')","json_extract(data,'$.project_path')","json_extract(data,'$.project_source')")
    guarded=["CASE WHEN json_valid(data) THEN "+expr+" END" for expr in fields]
    exact=True
    try:
        for table in ('session_write_projection','usage_legacy_projection'):
            if exact and c.execute("SELECT 1 FROM sqlite_temp_master WHERE type='table' AND name=?",(table,)).fetchone():
                exact=not c.execute('SELECT 1 FROM temp.'+table+' p JOIN main.requests r ON r.id=p.id WHERE NOT ('+
                                    ' AND '.join(expr.replace('data','p.data')+' IS '+expr.replace('data','r.data') for expr in fields)+
                                    ') LIMIT 1').fetchone()
    except sqlite3.Error:
        exact=False
    rows=c.execute('SELECT '+guarded[0]+' AS key,'+','.join('MAX('+expr+')' for expr in guarded[1:])+
                   ' FROM main.requests GROUP BY '+guarded[0]+' ORDER BY key').fetchall() if exact else None
    if rows is None or any(r[0] is None for r in rows):
        rows=c.execute('SELECT '+project+' AS key,'+','.join('MAX('+expr+')' for expr in fields[1:])+
                       ' FROM requests GROUP BY '+project+' ORDER BY key').fetchall()
    return [dict(id=r[0],label=r[1] or 'Unattributed project',path=r[2],basis=r[3]) for r in rows]


def cost_admission(projected_costs,safe_cost):
    """safe_cost over already-decoded projected costs, without re-decoding rows.

    safe_cost reads only record['cost']. Returns the same boolean as the
    ordered all() scan when no value raises; None when any value raises, since
    the original outcome (False versus the exception) then depends on order.
    """
    results=[]
    for cost in projected_costs.values():
        try:results.append(safe_cost({'cost':cost}))
        except Exception:return None
    return all(results)


def sql_applied_rate_groups(c,where,params,summary_sql=SUMMARY_SQL,*,page=None):
    """Actual saved rate snapshots used by the complete filtered request set.

    Never read the current catalogue or infer prices from the model name here.
    Revisions, tiers and providers stay separate; unpriced/pending attempts remain
    visible with a null rate. The aggregates are independent of request pagination.
    All rate provenance is preserved in the response for inspectable source details.
    """
    model="COALESCE(NULLIF(json_extract(data,'$.response_model'),''),NULLIF(model,''),'unknown')"
    tier="COALESCE(NULLIF(json_extract(data,'$.cost.rate.service_tier'),''),NULLIF(json_extract(data,'$.returned_service_tier'),''),NULLIF(json_extract(data,'$.service_tier'),''),'unspecified')"
    rate="canonical_rate(json_extract(data,'$.cost.rate'))"
    sql=('SELECT provider,'+model+' AS actual_model,'+tier+' AS applied_tier,'+rate+' AS saved_rate,MAX(started) AS latest_started,'
         +summary_sql+' FROM requests WHERE '+where+' GROUP BY provider,'+model+','+tier+','+rate
         +' ORDER BY MAX(started) DESC,provider,actual_model,applied_tier,saved_rate')
    result=[]
    for row in c.execute(sql+(' LIMIT ? OFFSET ?' if page else ''),params+list(page) if page else params):
        result.append(dict(provider=row['provider'],model=row['actual_model'],
            service_tier=row['applied_tier'],rate=json.loads(row['saved_rate']) if row['saved_rate'] else None,
            latest_started=row['latest_started'],
            **summary_from_sql(row)))
    return result


def legacy_read_view(c,where,params):
    """Project selected legacy-only rows, in a TEMP table for this read connection.

    Aggregates, paging, CSV and applied-rate totals then all see the same values.
    No UPDATE touches main.requests or its append-only events. SQL identifiers
    are fixed internal names; only bound values enter the scope query.
    """
    condition=("json_extract(data,'$.usage.usage_source')='hermes_normalized' "
      "AND json_extract(data,'$.usage.raw_usage') IS NULL "
      "AND COALESCE(json_extract(data,'$.usage.normalization_version'),0)<>2 "
      "AND (json_extract(data,'$.usage.cache_read_tokens')=0 OR json_extract(data,'$.usage.cache_write_tokens')=0)")
    rows=c.execute('SELECT id,data FROM main.requests WHERE ('+where+') AND '+condition,params)
    first=rows.fetchone()
    if first is None:return
    c.execute('CREATE TEMP TABLE usage_legacy_projection(id TEXT PRIMARY KEY,data TEXT NOT NULL)')
    from itertools import chain
    c.executemany('INSERT INTO usage_legacy_projection VALUES(?,?)',((r['id'],jd(effective_record(json.loads(r['data'])))) for r in chain((first,),rows)))
    c.execute("""CREATE TEMP VIEW requests AS
      SELECT r.id,r.started,r.ended,r.provider,r.model,r.session_id,r.task,r.compression_id,r.status,
      COALESCE(p.data,r.data) AS data FROM main.requests r
      LEFT JOIN usage_legacy_projection p ON p.id=r.id""")
