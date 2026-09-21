"""Same-profile named folder ownership; never infer ownership from basenames."""
from __future__ import annotations

import hashlib
import ntpath
import posixpath
import sqlite3
from pathlib import Path, PureWindowsPath

PROJECT_FIELDS = ('project_id', 'project_label', 'project_path', 'project_source')


def canonical_path(value):
    """Resolve existing symlink prefixes, including paths whose leaf is gone.

    Relative metadata is unusable: resolving it would invent process-cwd context.
    Normalise after realpath resolution so symlink/.. retains filesystem meaning.
    """
    if not isinstance(value, str) or '\x00' in value:
        return None
    # Remote Windows metadata must not pass through this host's POSIX realpath.
    if PureWindowsPath(value).is_absolute():
        return ntpath.normpath(value).replace('\\', '/').casefold()
    if not value.startswith('/'):
        return None
    try:
        return str(Path(value).resolve())
    except (OSError, RuntimeError, ValueError):
        return None


def read_projects(root, *, strict=False):
    """Read active explicit folder claims only, without opening another profile."""
    con = None
    try:
        path = Path(root) / 'projects.db'
        con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.05)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA query_only=ON')
        con.execute('BEGIN')
        rows = con.execute('''SELECT p.id, p.name, p.primary_path, f.path
            FROM projects p JOIN project_folders f ON f.project_id=p.id
            WHERE p.archived=0''').fetchall()
        claims = []
        for row in rows:
            folder = canonical_path(row['path'])
            if folder:
                claims.append(dict(id=row['id'], name=row['name'], folder=folder,
                                   primary=canonical_path(row['primary_path'])))
        return claims
    except (sqlite3.Error, OSError):
        if strict:
            raise
        return []
    finally:
        if con is not None:
            con.close()


def owner(path, claims):
    matches = [c for c in claims if path == c['folder'] or
               path.startswith(c['folder'].rstrip('/') + '/')]
    if not matches:
        return None
    longest = max(len(c['folder']) for c in matches)
    matches = [c for c in matches if len(c['folder']) == longest]
    # Duplicate physical claims by different projects are ambiguous, not an
    # invitation to choose a SQL row or a broad ancestor arbitrarily.
    if len({c['id'] for c in matches}) != 1:
        return None
    return matches[0]


def project_for(chain, sid, claims):
    """Nearest owning session wins; its cwd is authoritative for named projects.

    Explicit folder mappings handle worktrees without guessing Git common roots.
    Repository remains the preferred *fallback* when no named owner is available.
    """
    for session, meta in chain:
        for key in ('cwd', 'git_repo_root'):
            path = canonical_path(meta.get(key))
            match = owner(path, claims) if path else None
            if match:
                return dict(project_id=match['id'], project_label=match['name'],
                            project_path=match['primary'] or match['folder'],
                            project_source=('parent_' if session != sid else '') + 'named_project')
        # Resolve this session completely before consulting a more distant one.
        # Explicit Home is an owner, not absent evidence for a child to override.
        for key in ('git_repo_root', 'cwd'):
            path = canonical_path(meta.get(key))
            if path:
                return dict(project_id=hashlib.sha256(('path:' + path).encode()).hexdigest()[:24],
                            project_label='Home' if path == '/' else (posixpath.basename(path) or path), project_path=path,
                            project_source=('parent_' if session != sid else '') +
                            ('repository' if key == 'git_repo_root' else 'working_directory'))
    return dict(project_id=None, project_label=None, project_path=None, project_source='unavailable')
