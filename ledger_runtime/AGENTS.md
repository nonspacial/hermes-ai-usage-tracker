# Ledger runtime DOX

## Purpose

Own event recording, SQLite storage, request accounting, pricing, analytics reads and multi-profile aggregation.

## Ownership

`recorder.py` and adapters observe producer events; `storage.py` owns durable records and report reads; `aggregate.py` combines read-only profile reports; `api.py` exposes scoped ledger routes; `skills.py` owns skill/context observations. Other sibling modules own the named accounting, pricing, reset and analytics-reload boundaries. Root references provide technical detail, not replacement runtime authority.

## Local Contracts

- Read root instructions and [Overview interaction contract](../docs/OVERVIEW_INTERACTION_CONTRACT.md) for chart/identity-filter changes. A selected request model is its provider plus effective response model; skill/compression rows use their own recorded model and unknown rows are excluded, not inferred from their session.
- Preserve profile-local writes and read-only all-profile fan-in. Qualify cross-profile project/session/subagent identities and guard the all-profile sentinel against a real profile name. Preserve partial coverage as partial, not zero.
- Requested period, saved test-marker bounds and selected bucket intersect; marker timestamps stay factual even when viewing inputs are aligned. Selectable half-hour grid does not imply half-hour aggregation buckets (30-minute reads use one-minute buckets; one-hour reads use two-minute buckets). Preserve half-open interval boundaries and pagination/report parity.
- Analytics reads must not create a live ledger, mutate account data or contact pricing services as a side effect. Keep read-only snapshots/TEMP projection, lifecycle and reload limitations from `ANALYTICS_RELOAD.md`; do not claim an Online badge proves analytics latency.
- Retain unknown values as unknown. Do not coerce missing usage/cost into measured zero, rewrite recorded pricing snapshots or falsify request metadata to satisfy a UI filter.

## Work Guidance

- Diagnose slow period changes with isolated repeated fixtures and separate server read timings from frontend waiting. [Pending performance investigation](../docs/PERFORMANCE_INVESTIGATION.md) is not an established live root cause.
- Do not run authenticated reset, provider refresh, fixture migration or install work merely to exercise a read path.

## Verification

- `.venv/bin/python -m pytest -q -p no:cacheprovider tests --ignore=tests/ui` uses fixture homes; standalone `tests/ui` scripts verify consumer semantics. `tests/test_overview_identity_filters.py` exercises effective-model, marker/bucket and all-profile read semantics; its presence or an implementer run does not mark acceptance complete.

## Child DOX Index

None; all `ledger_runtime` modules remain under this owner.
