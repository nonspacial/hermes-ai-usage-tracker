# Analytics reload and recorder connection controls

## Separate operations

- **Refresh** / **Refresh data** refreshes quotas, reads, recorder status and the active analytics subscription. It does not reload Python code.
- **Reload analytics backend** loads a checked generation of analytics readers, reads back its revision, then refreshes data/status. It does not restart the gateway, agents or recorder.
- Clicking the **recorder badge** reconnects the analytics subscription where it is in use and obtains fresh status/read results. It does not refresh provider quota or prices.

One initial backend restart is necessary to mount the new reload routes. An old backend returns an explicit restart-required message instead of a fake success. No automatic restart is performed.

## Reload boundary

The reloadable set is `session_cache_writes.py`, `cache_progression.py` and the explicitly recognised read definitions in `storage.py` under this installed plugin's `ledger_runtime/`. Changes to storage writers, initialisation, schema, imports or other shared definitions require a restart. Shared `accounting.py` and `pricing.py` remain pinned to their original modules; changing them also requires a restart. Only the new analytics reader uses replacement module instances. Recorder hooks, their Store instances, request correlations, provider wrappers, heartbeat threads, pricing workers, API routing and the reload manager remain untouched.

Reload affects analytics served by **this backend process**, including profiles queried through that process. It does not broadcast to other producer/backend processes or copy repository edits into installed plugin directories. Copy the intended source files first. The profile parameter validates a known profile; it does not select arbitrary Python source paths.

Each candidate uses an isolated module namespace and frozen source bytes. Validation exercises a disposable fixture database and compares the schema contract before atomically publishing the candidate. Concurrent reads lease their original generation; old modules are removed only after those reads finish. Failed loading/validation keeps the previous generation. Main ledger connections in these readers use SQLite `mode=ro`; connection-local TEMP projections remain available. Schema changes are refused. Changes to non-reloadable plugin lifecycle, API, recorder or pricing files since manager startup require a backend restart rather than a partial reload.

Ledger reads, status checks and event subscriptions do not construct writable Stores or start pricing workers. A profile with no ledger gets an empty response from a disposable database; opening analytics does not create its live ledger. Event subscriptions may create their existing local notification socket, not ledger records. Explicit measurement-marker and price-refresh actions retain their documented write/network behaviour.

Endpoints are mounted under the existing authenticated host plugin namespace:

- `GET /ledger/analytics`: active revision, scope and non-reloadable-file change indication.
- `POST /ledger/analytics/reload`: validate and switch analytics generation; no arbitrary path or code payload.
- `GET /ledger`: includes `analytics_revision` for the generation that served that read.

This is a local development convenience, **not a sandbox for untrusted Python**. Validation catches loading/schema/basic query failures; it cannot prove arbitrary edited Python is safe or semantically correct. Review and test edits before loading them. Pricing refreshes and normal quota/read behaviour remain as documented separately.

## Badge semantics

- **Amber / Reconnecting**, with a spinner, reflects a pending manual check or an unacknowledged subscription. Duplicate clicks are disabled while pending.
- **Green / Online** requires fresh successful status (and current-view ledger data on analytics pages), a valid recorder heartbeat reported by the server, and an acknowledged native update subscription where used.
- **Amber / Limited** indicates backend capture warnings or an unavailable/degraded subscription with REST polling still active.
- **Red / Disconnected** indicates fresh check failure or expired status. **Not recording** indicates the server found no valid producer heartbeat and takes precedence even while an analytics read is pending.
- **Unverified** remains amber for legacy recorders without adequate heartbeat evidence.

Subscriptions does not need a ledger-event subscription, so its badge checks recorder health without pretending an inactive socket was connected. A subscription must acknowledge within eight seconds; missing later acknowledgements expire after 45 seconds. Status receipts expire after 30 seconds without fresh results. These are liveness deadlines, not a timed red/amber/green animation. Actual successful responses/frames recover the state. Reduced-motion users retain busy text and a static spinner shape.

Old in-flight results cannot overwrite a newer check or a different profile/view. Reload success is reported separately from recorder health and cannot force the badge green. Neither Online nor successful reload proves every provider route is recorded.

## Verification and preservation

Offline Python tests cover generation replacement, old-reader lifetime, failed syntax/behaviour validation, non-reloadable changes, schema rejection, read-only ledger access, scoped routes and revision readback. Browser tests hold actual mock SDK promises and subscription acknowledgements to verify busy/limited/error/recovery states, explicit dropdown actions, profile races and mobile menu containment. These are isolated fixtures, not live authenticated runtime proof.

The original PageHeader has an intentional extension for the split Refresh control. Its original source fingerprint remains in `PRESERVED_UPSTREAM.json` as historical evidence but is no longer asserted unchanged. Original quota controls and the new actions are covered by browser tests; the other preserved components/probes retain their fingerprint checks.
