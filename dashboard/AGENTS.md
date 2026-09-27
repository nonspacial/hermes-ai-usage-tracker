# Dashboard integration DOX

## Purpose

Expose the plugin manifest and Hermes-hosted quota/profile API alongside ledger routes.

## Ownership

`manifest.json` owns the host-visible **AI usage +** label and entry point. `plugin_api.py` owns authenticated host integration, target-profile resolution, quota collection and ledger route mounting; `ledger_runtime/` owns ledger calculation.

## Local Contracts

- Read root instructions and [ledger runtime](../ledger_runtime/AGENTS.md) for cross-boundary changes. The manifest name remains `ai-usage-tracker`; the label remains **AI usage +**.
- Resolve only known local profiles; bind provider probes to the selected profile rather than borrowing a different profile's credentials. All-profile analytics never combines subscription allowance or permits per-account actions. For standard host account-usage probes (Anthropic, Codex, OpenRouter), a `None` snapshot means subscription limits could not be read, not proof of absent credentials: show an unavailable status without asserting sign-in state. Preserve successful snapshots and explicit host-reported limitations, including OAuth-only quota access for API-key accounts. The historical upstream probe hash is retained as provenance, with this intentional exception covered by fixture tests.
- Every quota/reset worker binds the selected profile's Hermes home **and** host secret scope inside the worker (`_run_in_home`/`_run_reset_home`), reset in `finally`; never mutate `os.environ`. Like the host route, `GET /usage` prepares the selected scope **once per build** (`_prepare_home`: hydration + scope build) and passes that immutable `_PreparedHome` explicitly to discovery and every probe worker, which binds a private copy of it; workers never re-hydrate and never rely on inherited thread context. The prepared scope lives only for that build (a cached response re-serves the payload, not the scope; refresh re-prepares). Codex reset routes and the reset monitor are separate operations and prepare their own scope per call. The process's own profile uses host `launch_secret_scope`; any other profile uses only `build_profile_secret_scope(<home>)` stamped with that home. Nested executors carry the bound context. A host lacking `agent.secret_scope` keeps home-only binding; a partial scope API, or an own-profile read under multiplexing without launch policy, fails closed (`_ScopeUnavailable`) rather than probing unscoped. Source-only activity discovery (`state.db`) binds only the selected home (`_run_in_home_only`), never a secret scope or hydration. On `_ScopeUnavailable`, `GET /usage` performs no credential discovery, probe or quota-snapshot write: recently active providers render `configured: null` with `quota.source: scope_unavailable` plus top-level `credential_discovery.status: unavailable`; with no recorded activity it returns 503, never an empty provider list. Other exceptions still propagate. Covered by `tests/test_profile_secret_scope.py`.
- Quota reads are not write-free. Preparing a non-launch profile's scope calls the host `hydrate_profile_secret_sources(<home>)`, as host routed-profile scopes do. It can run that profile's configured external secret sources, such as 1Password, Bitwarden or `command` helpers (`/bin/sh -c`). It also fills process caches and 0600 on-disk fetch caches for those sources, without writing `os.environ`. Discovery and probes run host resolvers under the selected home. These can create home skeleton directories and seed `SOUL.md`, write `backups/config/config.yaml.good.*` on config load, and copy a corrupt `auth.json` to `auth.json.corrupt`. They can also take auth-store file locks, refresh and persist rotated OAuth tokens, and write `cache/oauth_heal_clean.json`. These are host side effects of an explicit quota request, not new plugin writes. This contract does not authorise deployment, installation, credential rotation or authenticated requests outside a user-initiated quota read.
- Hydration count matches host policy: at most one foreign-profile hydration attempt per `/usage` build (discovery plus up to 11 probe workers share it), plus one per separate reset operation. A fully successful source is cached once per home by the host; a failed source is retried by the host on the next build or operation, not within one build. The plugin keeps no secret snapshot beyond the build. The own profile uses `launch_secret_scope` without hydration and without a `profile_home` stamp; the host stamps the process home.
- Desktop consumer: the degraded payload needs no UI change. Cards show the per-provider `unavailable_reason`, a muted `no quota` badge and `used recently`; the summary counts 0 live. The 503 renders the existing `Could not load usage` error, never `No providers to show`. The pane ignores `configured` and does not render `credential_discovery.note`.
- Preserve read-only analytics access and explicit action side effects. Dashboard opening should not create a live ledger; read routes must not start price workers. Authenticated quota, price refresh, marker or Codex reset operations must not be confused with offline read checks.
- `GET /ledger` and related analytics routes must preserve scope/qualification, marker intersection and read-only aggregate semantics. Its absent-`view` response stays complete; validated opt-in view/group returns a versioned included/omitted manifest and only the active payload while retaining common header data. The signed `POST /ledger/refresh` contract is new-ledger-only and selected Overview/time **or model**, with fixed bounds or a forward-moving 24h window; see [backend DTO and fallback rules](../docs/INCREMENTAL_ANALYTICS_CONTRACT.md). Unsupported scopes and unsafe deltas fall back to full reads. Verify both backend response and UI consumption before claiming an end-to-end fix.
- The projected `GET /ledger` also accepts opt-in `list_mode=page|all` for lower record lists; absent mode and CSV keep legacy behaviour. View all is one complete response or explicit safety-bound failure, not a server-held snapshot session; see [record paging contract](../docs/RECORD_PAGING_CONTRACT.md).

## Work Guidance

- `ANALYTICS_RELOAD.md` describes partial backend reload; repository source edits alone do not activate installed code. Stale responses cannot supersede newer profile/view results.

## Verification

- Backend fixture tests live under `tests/` and run with `.venv/bin/python -m pytest -q --ignore=tests/ui`; no live credentials or ledgers in tests.

## Child DOX Index

None; both dashboard files belong here.
