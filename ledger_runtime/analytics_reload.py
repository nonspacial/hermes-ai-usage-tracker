"""Versioned analytics readers; never reload recorder hooks or producer state.

Only a fixed set of read-side modules can be swapped. A generation is checked
against a disposable database before publication; in-flight readers retain their
old modules until they finish. No user-supplied paths, imports or source bodies.
"""
from __future__ import annotations

from contextlib import contextmanager
import ast
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import types
import uuid

from . import accounting, pricing, ownership, projection
from .storage import SCHEMA

RELOADABLE = ('session_cache_writes', 'cache_progression', 'storage')
READ_DEFINITIONS = {'summary', 'DecimalSum', '_exprs', 'summary_from_sql',
                    'sql_summary', 'sql_trend', 'attribution_groups',
                    'sql_applied_rate_groups', 'legacy_read_view', 'request_predicate'}


class RestartRequired(Exception):
    pass


class AnalyticsRuntime:
    def __init__(self, folder=None):
        self.folder = Path(folder or Path(__file__).parent).resolve()
        self.lock = threading.RLock()
        self.reload_lock = threading.Lock()
        self.baseline = self._protected()
        self.current = self._build()

    def _protected(self):
        # Changes outside the read-side boundary cannot be silently half-loaded.
        paths = list(self.folder.glob('*.py')) + list(self.folder.glob('*.json'))
        paths += [self.folder.parent / name for name in ('bootstrap.py', '__init__.py', 'plugin.yaml')]
        paths += list((self.folder.parent / 'dashboard').glob('*'))
        protected = {str(p.relative_to(self.folder.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in paths if p.is_file() and not (p.parent == self.folder and p.stem in RELOADABLE)}
        # storage.py contains both readers and recorder writers. Only its known
        # read definitions may change without restarting the producer code.
        tree = ast.parse((self.folder / 'storage.py').read_bytes())
        nodes = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in READ_DEFINITIONS:
                continue
            if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) and t.id == 'SUMMARY_SQL' for t in node.targets):
                continue
            if isinstance(node, ast.ClassDef) and node.name == 'Store':
                node.body = [item for item in node.body if not (isinstance(item, ast.FunctionDef) and item.name == 'read')]
            nodes.append(node)
        protected['storage_writer_contract'] = hashlib.sha256(ast.dump(ast.Module(body=nodes, type_ignores=[]), include_attributes=False).encode()).hexdigest()
        return protected

    @staticmethod
    def _discard(generation):
        for name in generation['modules']:
            sys.modules.pop(name, None)

    def _build(self):
        sources = {name: (self.folder / (name + '.py')).read_bytes() for name in RELOADABLE}
        revision = hashlib.sha256(b''.join(name.encode() + b'\0' + sources[name] for name in RELOADABLE)).hexdigest()
        prefix = '_ai_usage_analytics_' + uuid.uuid4().hex
        generation = {'revision': revision, 'modules': [], 'readers': 0, 'retired': False}
        try:
            package = types.ModuleType(prefix)
            package.__path__ = []  # No fallback imports from mutable files on disk.
            sys.modules[prefix] = package
            generation['modules'].append(prefix)
            # Catalogue inspection is stable; its worker and fetch code are NOT reloaded.
            for name, stable in [('pricing', pricing), ('accounting', accounting),
                                 ('ownership', ownership), ('projection', projection)]:
                sys.modules[prefix + '.' + name] = stable
                generation['modules'].append(prefix + '.' + name)
            for name, source in sources.items():
                full = prefix + '.' + name
                module = types.ModuleType(full)
                module.__package__ = prefix
                module.__file__ = str(self.folder / (name + '.py'))
                sys.modules[full] = module
                generation['modules'].append(full)
                exec(compile(source, module.__file__, 'exec'), module.__dict__)
            storage = sys.modules[prefix + '.storage']
            if storage.SCHEMA != SCHEMA:
                raise RestartRequired('Database schema changes require a backend restart.')
            generation['store_type'] = storage.Store
            generation['decimal_sum'] = storage.DecimalSum
            self._validate(generation)
            if any((self.folder / (name + '.py')).read_bytes() != source for name, source in sources.items()):
                raise ValueError('Analytics files changed during validation; retry when editing finishes.')
            return generation
        except BaseException:
            self._discard(generation)
            raise

    @staticmethod
    def _reader(generation, root):
        # Bypass Store.__init__: a dashboard read must not create schemas, seed
        # pricing, start workers or change permissions. Main DB is opened read-only;
        # connection-local TEMP projections remain writable as required by reads.
        reader = object.__new__(generation['store_type'])
        reader.root = Path(root)
        reader.folder = reader.root / 'usage-ledger'
        reader.path = reader.folder / 'events.sqlite3'

        @contextmanager
        def db():
            connection = sqlite3.connect(reader.path.as_uri() + '?mode=ro', uri=True, timeout=2)
            connection.row_factory = sqlite3.Row
            connection.create_aggregate('decimal_sum', 1, generation['decimal_sum'])
            ownership.register_sql(connection)
            try:
                yield connection
            finally:
                connection.close()
        reader.db = db
        return reader

    def _validate(self, generation):
        with tempfile.TemporaryDirectory(prefix='analytics-check-') as temp:
            root = Path(temp)
            folder = root / 'usage-ledger'
            folder.mkdir()
            with sqlite3.connect(folder / 'events.sqlite3') as connection:
                connection.executescript(SCHEMA)
                for key, started, reads in [('baseline', 1, 0), ('later', 2, 100)]:
                    data = dict(id=key, started=started, ended=started + .1, provider='fixture',
                                model='fixture', session_id='fixture', source='main_hook', task='main',
                                status='completed', usage={'cache_read_tokens': reads, 'cache_write_tokens': 0})
                    connection.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                                       (key, started, started + .1, 'fixture', 'fixture', 'fixture', 'main', None, 'completed', json.dumps(data)))
            reader = self._reader(generation, root)
            result = reader.read(0, 3)
            if result['request_count'] != 2 or result['requests'][0]['id'] != 'later':
                raise ValueError('Analytics validation failed')
            if result['summary']['session_cache_writes']['tokens'] != 100:
                raise ValueError('Analytics calculation validation failed')
            json.dumps(result, allow_nan=False)

    @contextmanager
    def lease(self):
        with self.lock:
            generation = self.current
            generation['readers'] += 1
        try:
            yield generation
        finally:
            with self.lock:
                generation['readers'] -= 1
                if generation['retired'] and not generation['readers']:
                    self._discard(generation)

    def read(self, root, *args, test_id='', bucket_start=None, bucket_end=None, view=None, group=None):
        from .projection import selected_fields
        selected_fields(view, group)
        with self.lease() as generation:
            root = Path(root).resolve()
            if not (root / 'usage-ledger' / 'events.sqlite3').is_file():
                if test_id:
                    raise ValueError('Unknown test marker.')
                # Empty profiles get an empty response, not a newly created live
                # ledger or pricing worker merely because a dashboard was opened.
                with tempfile.TemporaryDirectory(prefix='analytics-empty-') as temp:
                    empty = Path(temp)
                    (empty / 'usage-ledger').mkdir()
                    with sqlite3.connect(empty / 'usage-ledger' / 'events.sqlite3') as connection:
                        connection.executescript(SCHEMA)
                    lo = max(args[0], bucket_start) if bucket_start is not None else args[0]
                    hi = min(args[1] if args[1] is not None else time.time(), bucket_end) if bucket_end is not None else args[1]
                    result = self._reader(generation, empty).read(lo, max(lo, hi) if hi is not None else None, *args[2:],view=view,group=group)
            else:
                reader = self._reader(generation, root)
                if test_id:
                    marker_start, marker_end = reader.test_window(test_id)
                    lo = max(args[0], marker_start)
                    hi = min(args[1] if args[1] is not None else time.time(),
                             marker_end if marker_end is not None else float('inf'))
                    args = (lo, max(lo, hi), *args[2:])
                if bucket_start is not None:
                    end = args[1] if args[1] is not None else time.time()
                    lo, hi = max(args[0], bucket_start), min(end, bucket_end)
                    args = (lo, max(lo, hi), *args[2:])
                result = reader.read(*args,view=view,group=group)
            result['analytics_revision'] = generation['revision']
            return result

    def info(self):
        try:
            restart_required = self._protected() != self.baseline
        except (SyntaxError, OSError):
            restart_required = True
        with self.lock:
            return {'revision': self.current['revision'], 'scope': 'analytics readers in this backend process',
                    'restart_required': restart_required}

    def reload(self):
        # Serialize builds; readers continue using the current generation meanwhile.
        with self.reload_lock:
            if self._protected() != self.baseline:
                raise RestartRequired('Recorder, schema, API, accounting, pricing or lifecycle code changed; backend restart required.')
            candidate = self._build()
            if self._protected() != self.baseline:
                self._discard(candidate)
                raise RestartRequired('Non-analytics files changed during reload; backend restart required.')
            with self.lock:
                previous = self.current
                self.current = candidate
                previous['retired'] = True
                if not previous['readers']:
                    self._discard(previous)
                return dict(self.info(), status='reloaded')
