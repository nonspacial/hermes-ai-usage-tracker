# Ledger runtime DOX

## Purpose

Own event recording, SQLite storage, request accounting, pricing, analytics reads and multi-profile aggregation.

## Ownership

`recorder.py` and adapters observe producer events; `storage.py` owns durable records and report reads; `aggregate.py` combines read-only profile reports; `api.py` exposes scoped ledger routes; `skills.py` owns skill/context observations. Other sibling modules own the named accounting, pricing, reset and analytics-reload boundaries. Root references provide technical detail, not replacement runtime authority.

## Local Contracts

- Read root instructions and [Overview interaction contract](../docs/OVERVIEW_INTERACTION_CONTRACT.md) for chart/identity-filter changes. A selected request model is its provider plus effective response model; skill/compression rows use their own recorded model and unknown rows are excluded, not inferred from their session.
- Preserve profile-local writes and read-only all-profile fan-in. Qualify cross-profile project/session/subagent identities and guard the all-profile sentinel against a real profile name. Preserve partial coverage as partial, not zero.
- Requested period, saved test-marker bounds and selected bucket intersect; marker timestamps stay factual even when viewing inputs are aligned. Selectable half-hour grid does not imply half-hour aggregation buckets (30-minute reads use one-minute buckets; one-hour reads use two-minute buckets). Preserve half-open interval boundaries and pagination/report parity.
- Selected marker reads use the same copied-ledger point lookup for factual bounds and marker data. Return the normal newest 100 tests in their existing order, then append the selected fact only when outside that prefix (at most 101, no duplicate); absent markers still fail. All-profile reads decode qualified test identity first and can append only that profile's qualified fact after the global newest-100 merge. Unfiltered reads, including absent-`view`, keep their existing recent-100 contract.
- Analytics reads must not create a live ledger, mutate account/usage rows or contact pricing services as a side effect. Selected GET and refresh POST use a `mode=ro` SQLite source connection and one SQLite-managed backup into disposable storage; normal SQLite WAL/SHM source sidecar effects are permitted for selected profiles only. Close connections and remove scratch on all paths; bound lock retries and report busy/unavailable snapshots as retryable 503. All-profile reads retain the strict raw DB/WAL copy strategy with **no source SQLite connection or sidecar effects**. Keep TEMP projection, lifecycle and reload limitations from `ANALYTICS_RELOAD.md`; do not claim an Online badge proves analytics latency. Project owner execution state and missing-field reason once per selected request per read; keep closed-history short-circuit and fresh owner inspection on the next read. The absent-`view` response and all-profile fan-in remain complete. Opt-in `view`/Overview `group` selection is validated in startup-pinned, restart-required `projection.py` across API, selected and all-profile readers; it names omitted fields in a version-1 manifest, computes only requested section data alongside one complete shared header, and never converts an omitted or unreadable value into zero. The new-ledger-only bounded transactional journal and signed Overview/time-or-model fixed/forward-24h refresh are specified in [the incremental contract](../docs/INCREMENTAL_ANALYTICS_CONTRACT.md); old ledgers are not silently migrated, and unsupported revisions/windows return full reads. Eligible reads verify the closed canonical main-schema table/index/trigger inventory (including journal and every trigger body), never trusting trigger names as authority; unknown objects force full fallback. The effective response-model expression must appear explicitly in every grouping rather than as the ambiguous `model` alias. Omitted GET end is resolved once and signed at the reported exact bound; POST forwards that bound. `incremental_migration.py` is strictly opt-in/offline with backup proof, exact target, writer-exclusion, tracked JSON/rate/catalogue validation and disposable-copy production-reader preflight across full history before DDL (open owned rows fail closed without process inspection); validate every saved and catalogue price against `accounting.decimal_value` (finite/nonnegative, preserving unknown and zero), including unselected models, while signed savings amounts retain their separate contract. It never runs from Store startup/read and future schema versions require new explicit approval.
- Retain unknown values as unknown. Do not coerce missing usage/cost into measured zero, rewrite recorded pricing snapshots or falsify request metadata to satisfy a UI filter.
- Skills period aggregates reduce successful main loads and estimated returned text in SQL, keeping reference/failure and missing-size counts separate. `aggregate_only` skips historical detail reads but never deletes rows. Future catalogue-description inclusion is recorded only from a parseable `<available_skills>` block in the supported pre-request hook, with validated names, estimated description sizes and recorded request provider/model; missing blocks, old history and effective provider-model attribution remain unavailable. No raw prompt or description text is persisted. See [skills recording](../SKILLS_USAGE.md).

## Work Guidance

- Diagnose slow period changes with isolated repeated fixtures and separate server read timings from frontend waiting. [Performance investigation](../docs/PERFORMANCE_INVESTIGATION.md) has local read-only timing and a remaining end-to-end request-stage gap; it is not a blanket guarantee for all profiles.
- Do not run authenticated reset, provider refresh, fixture migration or install work merely to exercise a read path.

## Verification

- `.venv/bin/python -m pytest -q -p no:cacheprovider tests --ignore=tests/ui` uses fixture homes; standalone `tests/ui` scripts verify consumer semantics. `tests/test_overview_identity_filters.py` exercises effective-model, marker/bucket and all-profile read semantics; its presence or an implementer run does not mark acceptance complete.

## Child DOX Index

None; all `ledger_runtime` modules remain under this owner.
