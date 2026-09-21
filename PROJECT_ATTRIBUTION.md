# Named projects and historical reconciliation

New native records resolve ownership from **the producing profile's** `projects.db`:
active `projects` joined to explicit `project_folders`. Longest canonical folder
prefix wins, with a path-component boundary. IDs and names are the actual project
ID/name; `project_path` is its canonical primary path. Discovered repositories,
archived projects and the process working directory are not ownership evidence.

Existing POSIX symlink prefixes are resolved, including paths below missing leaves.
Absolute Windows drive/UNC metadata is normalised case-insensitively without
resolving it against a POSIX host's working directory. Relative paths are ignored.
Equal longest claims by different projects are ambiguous; no arbitrary project
is selected. Basenames are labels, never keys.
Register each unrelated worktree root explicitly against its owning project.
Within the nearest owning session, cwd precedes repository for named ownership.
Delegates inherit the nearest primary owner's metadata, skipping intermediate
delegates' launcher paths. An explicit Home or other path fallback is resolved
before consulting a more distant ancestor or the child's own named project.
A primary session's explicit workspace beats its ancestor's project; missing
paths can inherit. Without named
ownership, repository then cwd provide a canonical-path hash, independent of
which of those two metadata fields supplied it. Explicit `/` is labelled **Home**;
missing metadata remains unattributed. This does not create a sidebar project.

Ordinary capture preserves historical project snapshots. Current metadata may
clear cached paths/parent links for *future* requests; that is not a historical
migration. Renames, archive changes and corrected session ownership need the
explicit operation below to update previous records.

## Operator-only maintenance

First back up `usage-ledger/events.sqlite3` using SQLite's backup API (including
committed WAL contents), and finish any intended same-profile session/project
repairs. Backups and any deployment/reload are separate operator actions. Do not
copy a live SQLite database without accounting for its WAL.

From this repository, inspect **only infra**, with no writes:

```bash
.venv/bin/python -m ledger_runtime.reconcile_projects \
  --profile-root /home/nope/.hermes/profiles/infra
```

After reviewing the counts and securing the backup, explicitly apply:

```bash
.venv/bin/python -m ledger_runtime.reconcile_projects \
  --profile-root /home/nope/.hermes/profiles/infra --apply
```

Callable: `ledger_runtime.reconcile_projects.reconcile_projects(profile_root,
apply=False)`. The installed bootstrap namespace exposes the same function under
`_hermes_ai_usage_ledger_v2.reconcile_projects`. It requires an explicit absolute
profile root and existing databases; no default-profile or environment fallback.
Symlinked database/ledger targets outside that resolved root are refused. It does
not instantiate `Store`, seed prices, install anything, notify the UI or restart
producers. The source-tree CLI can therefore repair a ledger independently of
installation, but new producer attribution still requires separately authorised
installation/reload of this code.

The operation takes read-only snapshots of current state/project metadata and a
single ledger transaction. Keep metadata administration quiescent while applying:
the separate databases do not form a globally atomic snapshot. Missing/corrupt
metadata or incompatible schemas fail closed. Current state paths and lineage
(including NULLs) override cached metadata. A missing session may follow a cached
parent link to current state, but cached paths are never historical-repair evidence.
Rows without a reachable current session are reported as skipped and retained.

Only these JSON fields change:

- `requests` and `compressions`: `project_id`, `project_label`, `project_path`,
  `project_source`.
- `skill_events`, when present: `project_id` (keeps existing skill filters aligned).

All other fields and SQL columns remain unchanged, including saved lineage,
request IDs, counts, tokens, raw usage, costs/rates and timestamps. Append-only
ledger `events` and cached `session_context` are untouched. No historical usage is
reconstructed or repriced. Any error rolls back all ledger changes; rerunning with
the same metadata is idempotent. Reports give scanned/changed/skipped counts per
table. Dry-run returns the same proposed changes without updating any rows.
