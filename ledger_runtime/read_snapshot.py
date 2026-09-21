"""Read SQLite through a stable disposable copy, never source WAL sidecars.

SQLite mode=ro can still create/update -shm and -wal in a writable source
folder. Aggregate inspection therefore copies the database and its WAL
using ordinary file reads, rejects concurrent changes, and lets SQLite index
only the scratch copy. Nonempty rollback journals are explicitly unsupported:
file copies cannot establish whether a journal is hot or part of a live writer,
and recovery may depend on a super-journal outside this database.
Do not use immutable=1 on a live WAL database:
that would silently ignore committed WAL records.

Each poll copies the entire DB/WAL (up to three attempts), even for a narrow
window. This trades disk I/O and scratch space for no source SQLite writes.
Stability relies on local filesystem inode/size/nanosecond timestamps, not a
SQLite source lock; hostile writers or filesystems hiding changes are unsupported.
"""
from contextlib import contextmanager
from pathlib import Path
import shutil
import tempfile


class SnapshotBusy(OSError):
    reason = 'snapshot_busy'


class SnapshotUnsupported(OSError):
    reason = 'unsupported_rollback_journal'


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
