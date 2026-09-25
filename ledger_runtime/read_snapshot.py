"""Read SQLite through disposable copies with scope-specific source effects.

SQLite mode=ro can still create/update -shm and -wal in a writable source
folder. Aggregate inspection therefore copies the database and its WAL
using ordinary file reads, rejects concurrent changes, and lets SQLite index
only the scratch copy. Nonempty rollback journals are explicitly unsupported:
file copies cannot establish whether a journal is hot or part of a live writer,
and recovery may depend on a super-journal outside this database.
Do not use immutable=1 on a live WAL database:
that would silently ignore committed WAL records.

Selected reads instead use a read-only SQLite source connection and SQLite's
backup API. That connection may normally create/update source WAL/SHM sidecars;
it cannot create a missing main database or write usage rows. A single backup
step holds a consistent WAL read view even with a continuously writing recorder.

Aggregate polls copy the entire DB/WAL (up to three attempts), even for a narrow
window. This trades disk I/O and scratch space for no source SQLite writes.
Stability relies on local filesystem inode/size/nanosecond timestamps, not a
SQLite source lock; hostile writers or filesystems hiding changes are unsupported.
"""
from contextlib import closing, contextmanager
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time


class SnapshotBusy(OSError):
    reason = 'snapshot_busy'


class SnapshotUnsupported(OSError):
    reason = 'unsupported_rollback_journal'

class SnapshotUnavailable(OSError):
    reason = 'snapshot_unavailable'

@contextmanager
def selected_snapshot_root(root):
    """Back up an existing selected ledger in one SQLite snapshot, not raw files.

    One backup step prevents page-by-page restarts/starvation under WAL writes.
    The two-second connection/busy budget bounds lock retries; a successful
    full copy is allowed to finish. Python's backup progress callback also
    interrupts repeated SQLITE_BUSY/LOCKED attempts when the budget expires.
    """
    source = (Path(root) / 'usage-ledger' / 'events.sqlite3').resolve()
    if not source.is_file():
        raise SnapshotUnavailable('Selected ledger disappeared during snapshot.')
    try:
        scratch = tempfile.TemporaryDirectory(prefix='usage-selected-snapshot-')
    except OSError as exc:
        raise SnapshotUnavailable('Selected ledger scratch unavailable.') from exc
    with scratch as folder:
        target = Path(folder)
        ledger = target / 'usage-ledger'
        try:
            ledger.mkdir()
        except OSError as exc:
            raise SnapshotUnavailable('Selected ledger scratch unavailable.') from exc
        deadline = time.monotonic() + 2
        # closing(), not Connection.__exit__, actually releases SQLite handles.
        try:
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=2)) as reader:
                with closing(sqlite3.connect(ledger / 'events.sqlite3', timeout=0)) as copy:
                    def progress(status, remaining, total):
                        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and time.monotonic() >= deadline:
                            raise SnapshotBusy('Selected ledger locked during backup; retry shortly.')
                    reader.backup(copy, pages=-1, progress=progress, sleep=.01)
        except sqlite3.Error as exc:
            if not source.is_file():
                raise SnapshotUnavailable('Selected ledger disappeared during snapshot.') from exc
            if (getattr(exc, 'sqlite_errorcode', None) or 0) & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise SnapshotBusy('Selected ledger locked during backup; retry shortly.') from exc
            raise SnapshotUnavailable('Selected ledger backup unavailable.') from exc
        yield target


def signature(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@contextmanager
def snapshot_root(root):
    source = (Path(root) / 'usage-ledger' / 'events.sqlite3').resolve()
    files = [source, Path(str(source) + '-wal'), Path(str(source) + '-journal')]
    # A busy ledger is explicitly unavailable, never a guessed partial copy.
    for _ in range(3):
        with tempfile.TemporaryDirectory(prefix='usage-read-snapshot-') as folder:
            target = Path(folder)
            ledger = target / 'usage-ledger'
            ledger.mkdir()
            before = [signature(path) for path in files]
            if before[0] is None:
                raise FileNotFoundError('Ledger disappeared during snapshot.')
            if before[2] is not None and before[2][2] > 0:
                raise SnapshotUnsupported('Nonempty rollback journal; snapshot unavailable.')
            try:
                for path, state, suffix in zip(files, before, ('', '-wal', '-journal')):
                    if state is not None:
                        shutil.copyfile(path, ledger / ('events.sqlite3' + suffix))
            except FileNotFoundError:
                continue
            if before != [signature(path) for path in files]:
                continue
            yield target
            return
    raise SnapshotBusy('Ledger changed during snapshot; retry on the next poll.')
