# Test suite DOX

## Purpose

Verify ledger/backend behaviour and offline preview interactions without touching live Hermes profiles.

## Ownership

Root owns test dependencies and preview generation; this directory owns backend fixture tests and `conftest.py`. [ui/AGENTS.md](ui/AGENTS.md) owns standalone browser scripts and its fixture. `fixtures/` contains test assets under this owner.

## Local Contracts

- Read root and nearest child instructions. Use synthetic isolated homes/SQLite, offline pricing and no account credentials or live exports. Do not turn marker scenarios into fabricated production timestamps.
- Prefer executable behaviour tests over source-text regex. Assert provider/profile scope, response-model semantics, qualified aggregate identities, marker/bucket intersection, report/pagination parity and stale-response protection where applicable. `test_read_performance.py` compares complete selected/all-profile responses with the original unmaterialised SQL, counts fresh owner evaluations and benchmarks disposable requests; it does not read live ledgers or impose a flaky wall-clock threshold. `test_ledger_projection.py` compares every included projected field with the full fixture response, verifies omitted-field manifests, partial coverage, markers, empty/unknown data and hidden-stage nonexecution. `test_incremental_refresh.py` exercises transactional new-ledger revisions, signed resume/file identity, bounded journal bytes, canonical inventory/unknown-trigger or schema-object fallbacks, malformed JSON/shape/nested-price/catalogue preflight refusal with unchanged target/backup and no DDL (including negative saved, selected and unselected catalogue rates), finite/nonfinite/unknown/zero boundary parity with the accounting price validator, old rows beyond the first detail page/outside 24h, owned-open liveness refusal without process inspection, disposable catalog valuation and sparse legacy acceptance, mixed effective models across unchanged/changed buckets, omitted-end rolling route parity, token size/decompression, WAL/source sentinels, offline migration schema/backup/lock refusal and forced full fallbacks. `benchmark_incremental_refresh.py` is a separate executable synthetic timing fixture, not a pytest case. A test name or partial run is not completion evidence.
- Test commands must not implicitly install or restart Hermes; do not alter live ledger or account reset state.
- The old-selected-marker cap test uses isolated FastAPI requests with 101 newer persisted test rows per synthetic profile; it checks bounded prefix order, factual window, missing/recent dedup, qualified all-profile selection and no cross-profile marker fact.

## Work Guidance

- Backend pytest and UI scripts need separate invocations: `tests/ui/test_table_ordering.py` collides by module name with backend `tests/test_table_ordering.py` under unscoped collection.
- `requirements-dev.txt` declares PyYAML for dashboard activation-policy tests; use existing isolated `.venv` rather than installing into active Hermes.

## Verification

- `.venv/bin/python -m pytest -q -p no:cacheprovider tests --ignore=tests/ui`; targeted `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_overview_identity_filters.py`. Both use synthetic fixtures and need independent review before acceptance.

## Child DOX Index

- [ui/AGENTS.md](ui/AGENTS.md): preview fixture and standalone browser verification.
