# Dashboard integration DOX

## Purpose

Expose the plugin manifest and Hermes-hosted quota/profile API alongside ledger routes.

## Ownership

`manifest.json` owns the host-visible **AI usage +** label and entry point. `plugin_api.py` owns authenticated host integration, target-profile resolution, quota collection and ledger route mounting; `ledger_runtime/` owns ledger calculation.

## Local Contracts

- Read root instructions and [ledger runtime](../ledger_runtime/AGENTS.md) for cross-boundary changes. The manifest name remains `ai-usage-tracker`; the label remains **AI usage +**.
- Resolve only known local profiles; bind provider probes to the selected profile rather than borrowing a different profile's credentials. All-profile analytics never combines subscription allowance or permits per-account actions.
- Preserve read-only analytics access and explicit action side effects. Dashboard opening should not create a live ledger; read routes must not start price workers. Authenticated quota, price refresh, marker or Codex reset operations must not be confused with offline read checks.
- `GET /ledger` and related analytics routes must preserve scope/qualification, marker intersection and read-only aggregate semantics. Its absent-`view` response stays complete; validated opt-in view/group returns a versioned included/omitted manifest and only the active payload while retaining common header data. The signed `POST /ledger/refresh` contract is new-ledger-only and selected Overview/time **or model**, with fixed bounds or a forward-moving 24h window; see [backend DTO and fallback rules](../docs/INCREMENTAL_ANALYTICS_CONTRACT.md). Unsupported scopes and unsafe deltas fall back to full reads. Verify both backend response and UI consumption before claiming an end-to-end fix.

## Work Guidance

- `ANALYTICS_RELOAD.md` describes partial backend reload; repository source edits alone do not activate installed code. Stale responses cannot supersede newer profile/view results.

## Verification

- Backend fixture tests live under `tests/` and run with `.venv/bin/python -m pytest -q --ignore=tests/ui`; no live credentials or ledgers in tests.

## Child DOX Index

None; both dashboard files belong here.
