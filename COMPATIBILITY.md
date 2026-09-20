# Test.14 SDK boundary extension

Guarded OpenAI BaseClient._build_request(self, options, ...) and
_process_response_data(self, *, data, cast_to, response, ...) wrappers are new.
No guarantee of their availability in custom transports or future SDK versions.
The per-record cache_evidence object establishes actual exercise of the new
boundary; adapter registration/Online alone does not. Existing limitations below
still apply. No authenticated Hermes runtime was tested for this revision.

# test.13 compatibility additions

The recorder additionally signature-checks `agent.codex_runtime.run_codex_stream(agent, api_kwargs, ...)`. The `_usage_summary_for_api_request_hook` wrapper adds a numeric-only `_ai_usage_raw_usage` field to the plugin hook summary. Raw capture uses the exact `_current_api_request_id` and session ID. Older or different runtimes without those boundaries keep the normalized-only path explicitly unverified; no transport arguments are rewritten.

The UI uses documented `ctx.os.writeClipboard`, with browser/legacy fallbacks. User gesture is required and denied capability is not reported as success. Live Hermes compatibility and the actual host clipboard have not been exercised here.

# Compatibility, capture boundaries and source basis

## Target

Personal test build 2.0.0-test.11; source basis is the user's uploaded quota plugin and the Hermes `main` interfaces retrieved during this build. The online main files were not obtained as a complete, commit-pinned checkout. Therefore **no exact upstream commit is claimed as tested**. `doctor.py` records the local checkout commit on the user's machine.

This build extends **Hermes Desktop's plugin** and the associated FastAPI backend. It is not a separately built Hermes Web Dashboard frontend or standalone remote collector. The supplied HTML is only a synthetic preview.

## Instrumentation

Documented plugin hooks: `pre_api_request`, `post_api_request`, `api_request_error`, `on_session_end`, `on_session_start`, `subagent_start`, `subagent_stop`.

Guarded **in-memory wrappers (monkey patches)** supplement the hooks; no Hermes core file is changed on disk:

| Location | Wrapped boundary | Purpose |
|---|---|---|
| `run_agent.AIAgent` | `_usage_summary_for_api_request_hook` | Capture allowlisted raw counts before normalization strips original fields. |
| `agent.auxiliary_client` | `_relay_sync_completion`, `_relay_async_completion` | Persist native helper-call start/success/error. |
| `agent.auxiliary_client._CodexCompletionsAdapter` | `create` | Observe the direct Codex helper special case; guard against duplicate Relay recording. |
| `agent.codex_runtime` | `_consume_codex_event_stream` | Preserve terminal reported usage before later conversion or errors. |
| `agent.anthropic_adapter` | `create_anthropic_message` | Preserve native Anthropic usage before adapter conversion. |
| `agent.conversation_compression` | `compress_context`, `_emit_compression_attempt_telemetry` | Correlate attempt, linked helper use and native compression metadata. |
| `agent.context_compressor.ContextCompressor` | `_micro_compact`, `_emit_micro_compaction_telemetry` | Observe native idle micro-compaction events. |

Only recognized signatures are wrapped. Exceptions/returns from the original functions are preserved in unit tests. Adapter installation retries when later-loaded modules become available. A future signature or implementation change can still evade a structural check: runtime verification remains necessary.

## Explicit limits

1. **Direct-provider Hermes path is the primary target.** The separate `codex_app_server` runtime is not covered as a complete request ledger by this build.
2. Main and nested agent calls are captured when those producers load the plugin and emit the observed hooks. Helpers are captured at the listed adapter boundaries. Plugin-originated requests that bypass those boundaries are not guaranteed coverage.
3. Generic **non-Codex direct streaming auxiliary routes** are not intercepted. This gap remains documented here and in the internal health record. This does not mean ordinary main-loop streaming calls are all excluded.
4. Hermes-level observed attempts are not necessarily individual HTTP wire attempts: SDK-internal retries/batches can occur beneath a boundary. No claim of one ledger row for every physical upstream request is made.
5. Cancellation or interrupted streams can end without authoritative usage. A previously observed terminal usage is preserved, but absent upstream fields cannot be reconstructed.
6. Provider-specific shapes outside the implemented OpenAI-style, Anthropic-style and Hermes-canonical mappings may retain incomplete counters. Supported raw details are numeric allowlisted fields, not a full unredacted response archive.
7. Each process/profile must load the recorder. Local profiles remain separate. Hosts reached over SSH/Tailscale do not get a central cross-host merger from this package; install at the actual producing host and inspect its ledger.
8. Native compaction context estimates are not exact billable tokens. The next call's reported input includes any newly appended content. Session/ContextVar correlation needs live verification for concurrent/custom routing paths.
9. Socket delivery is an accelerator; the 15-second display fallback is necessary on unavailable sockets/OAuth remotes. It does not replace event-level recording.
10. Not load-tested on the user's billions-of-tokens workload. There is no automatic retention, external queue, multi-host replay collector or audited comprehensive coverage monitor. A lightweight profile-pinned producer heartbeat is now present. Watch recorder errors and database size.
11. Quota snapshots reflect the original tracker probes while the UI/status feature is using them. This build does not promise an exhaustive server reset ledger, automatically classify every allowance refresh, or redeem resets.
12. Models absent from the matched public-provider catalog remain unpriced. Costs are API-equivalent token estimates, not actual subscription charges.
13. Subagent counts require observed lifecycle or explicit source metadata. A parent link alone is not treated as delegation. Project grouping uses recorded owning repository roots or labelled directory fallbacks, not inferred conversation content. Old rows may remain unattributed.
14. Every child-producing process must load this plugin. Missing child requests cannot be reconstructed from the parent's totals; unknown ancestry is retained rather than invented.

## Attribution in this revision

At native events, the recorder reads only the selected profile's session ID/source/parent/repository/CWD columns using SQLite read-only mode. No Hermes token counters are read. Safe lifecycle IDs/roles are stored in the new local `session_context` table. The request schema remains JSON-compatible with earlier ledgers. Ancestry traversal is bounded to 64 records; missing roots/cycles are not silently resolved.

New hook registration is guarded for unavailable subagent lifecycle hooks. The connection badge warns on unavailable adapters; underlying health remains in the ledger API. Current-main `delegate_tool.py` emits parent/child session IDs around delegated work and identifies children as `platform="subagent"`; runtime coverage of the user's exact installation is still untested.

The original 27 quota backend function ASTs (excluding only the optional snapshot addition in get_usage) and seven original Desktop component/helper sources were compared with the uploaded installed plugin and are regression-tested. Ledger backend import/registration failure is caught so original `/usage`, `/profiles` and `/health` routes survive.

## Relevant primary sources inspected

- Delegated lifecycle / child identities: https://raw.githubusercontent.com/NousResearch/hermes-agent/main/tools/delegate_tool.py
- Native hook contracts: https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks
- Desktop plugin API / ctx.rest / ctx.socket / profile scope: https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk
- Main loop and normalized usage dispatch: https://raw.githubusercontent.com/NousResearch/hermes-agent/main/run_agent.py
- Token normalization and local price snapshots: https://raw.githubusercontent.com/NousResearch/hermes-agent/main/agent/usage_pricing.py
- Helper routing: https://raw.githubusercontent.com/NousResearch/hermes-agent/main/agent/auxiliary_client.py
- Codex final usage: https://raw.githubusercontent.com/NousResearch/hermes-agent/main/agent/codex_runtime.py
- Anthropic response adapter: https://raw.githubusercontent.com/NousResearch/hermes-agent/main/agent/anthropic_adapter.py
- Compression commit/telemetry: https://raw.githubusercontent.com/NousResearch/hermes-agent/main/agent/conversation_compression.py
- Micro-compaction telemetry: https://raw.githubusercontent.com/NousResearch/hermes-agent/main/agent/context_compressor.py

The uploaded quota code is retained with its original MIT licence. This is a personal modification, not an official release or endorsed upstream update.

## test.7 status limitations

This UI/health revision reuses the test.6 instrumentation; no newly pinned full Hermes checkout or live authenticated test is claimed. The new badge proves a recently reporting, hook-registered producer in the selected profile, not live connectivity to every model provider and not complete capture of every gateway/process. Leases can remain visible for up to 60 seconds after process death. Normal process shutdown lets the lease expire; there is no process-kill or configuration mutation route. WebSocket connection state alone never establishes recorder health. The SDK backoff and OAuth REST-fallback contract was checked at https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/developer-guide/desktop-plugin-sdk.md.
