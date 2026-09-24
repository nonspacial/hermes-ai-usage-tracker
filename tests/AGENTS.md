# Test suite DOX

## Purpose

Verify ledger/backend behaviour and offline preview interactions without touching live Hermes profiles.

## Ownership

Root owns test dependencies and preview generation; this directory owns backend fixture tests and `conftest.py`. [ui/AGENTS.md](ui/AGENTS.md) owns standalone browser scripts and its fixture. `fixtures/` contains test assets under this owner.

## Local Contracts

- Read root and nearest child instructions. Use synthetic isolated homes/SQLite, offline pricing and no account credentials or live exports. Do not turn marker scenarios into fabricated production timestamps.
- Prefer executable behaviour tests over source-text regex. Assert provider/profile scope, response-model semantics, qualified aggregate identities, marker/bucket intersection, report/pagination parity and stale-response protection where applicable. A test name or partial run is not completion evidence.
- Test commands must not implicitly install or restart Hermes; do not alter live ledger or account reset state.

## Work Guidance

- Backend pytest and UI scripts need separate invocations: `tests/ui/test_table_ordering.py` collides by module name with backend `tests/test_table_ordering.py` under unscoped collection.
- `requirements-dev.txt` declares PyYAML for dashboard activation-policy tests; use existing isolated `.venv` rather than installing into active Hermes.

## Verification

- `.venv/bin/python -m pytest -q -p no:cacheprovider tests --ignore=tests/ui`; targeted `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_overview_identity_filters.py`. Both use synthetic fixtures and need independent review before acceptance.

## Child DOX Index

- [ui/AGENTS.md](ui/AGENTS.md): preview fixture and standalone browser verification.
