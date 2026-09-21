# Recorder lifecycle and incomplete usage

## Request identity and finalisation

Hermes's `on_session_end` hook is emitted at turn completion. Cleanup therefore requires an exact session and turn ID, a producer-local registered profile, and the current process identity. A missing or ambiguous identity is not permission to sweep a session or another profile.

Cleanup selects open `pending`/`usage_received` requests. Every write rechecks the expected turn, process and open status inside its SQLite write transaction. A completion that arrives after the selection wins; cleanup cannot overwrite it.

A request ended without a post/error hook is `ended_without_usage` when no numeric usage exists, or `ended_with_usage` when numeric usage was captured. Neither label asserts successful completion. The independent `turn_outcome` retains supplied booleans and an allowlisted exit-reason category. Late authoritative usage can change availability to `ended_with_usage`, but cannot turn a failed request into a successful one or reopen a final request.

The Codex runtime wrapper snapshots request identity at entry and uses that identity for its returned response, even if the agent has advanced meanwhile. Storage rechecks usage-source priority inside the write transaction; a late weaker or empty usage update cannot erase stronger retained counters. Saved rate snapshots on final records remain pinned.

Native API error hooks supply a dictionary. The recorder retains its validated error class name, not the dictionary's Python class and not the error message. Exit reasons outside the reviewed vocabulary become `other`; arbitrary strings are not retained.

## What unavailable values mean

`missing_fields` remains the exact per-field unavailable count. Additive `missing_reasons` counts partition that count into:

- `awaiting_usage`: an open request with a locally observed matching owner process has not supplied the field yet. This is not proof that a provider call is still executing.
- `unresolved_execution`: an open request cannot be proved live here (including legacy ownership, foreign hosts/namespaces, denied inspection and dead owners awaiting reconciliation).
- `abandoned_execution`: its owner was proved dead and reconciliation terminalised the execution; the field remains unknown.
- `unverified_accounting`: normalized cache zeros/decomposition lack provider evidence, including historical read-time projections.
- `ended_without_usage`: the request ended and no numeric usage was retained.
- `unreported_field`: other absent fields in otherwise recorded requests.

The same classifications cover full-window summaries, trends and groups, not only the current Requests page. UI notes/tooltips explain these categories. Cost estimates may be incomplete because tokens are unknown, not solely because unit prices are absent.

An unavailable breakdown does not remove a known processed-token total. One request can lack several fields and cause several card warnings. These are not independent lost-request counts.

Historical normalized-only records without retained raw evidence cannot be honestly reconstructed. No migration or backfill is performed; absent values remain distinct from zero. Calculated session cache writes, provider counters and saved-dollar accounting remain separate.

## Abandoned-owner reconciliation

New main and auxiliary/compression-helper requests retain an immutable `owner` tuple: Linux machine identity plus hostname, boot ID, PID namespace, PID, `/proc` process start ticks and a random producer generation. Forked children receive a fresh generation. This is local cooperative recorder evidence, not authenticated remote-host identity; cloned machine identities or hostile kernel/filesystem substitution are outside the contract.

A matching host with an earlier boot, a reused PID with different start ticks, or kernel-confirmed PID absence proves that exact recorded owner cannot continue. A terminal thread-group leader state (`Z`, `X` or `x`) alone does **not** prove whole-process death: `pthread_exit` can leave a zombie leader while worker threads continue and complete requests. These states remain unresolved, without abandonment, until independent death evidence is available. No pidfd-based whole-process inspection is implemented; even a genuinely dead but unreaped zombie remains unresolved until absence or identity replacement can be proved. A hidden `/proc` entry alone does not: absence also requires `kill(pid, 0)` to return `ESRCH` in the matching namespace (no signal is delivered). Missing/inaccessible/unsupported identities, foreign hosts/namespaces and legacy records remain unresolved. Heartbeat age and later same-session reports never authorise closure. A live process also cannot prove an old in-process generation is still servicing a particular call; the UI says **open (owner observed live)**, not globally active inference.

Reconciliation runs at producer registration, session start, exact turn end and heartbeat boundaries. Each invocation reads at most 256 open rows using a partial index and a rotating ID cursor, with one observation per owner in that batch. There is no full-history scan on every request. The heartbeat interval is a scheduling cadence, not a death timeout. Repeated boundaries drain larger backlogs; restarting repeatedly can delay entries beyond the first batch. Dashboard and All profiles reads never reconcile or write source records.

Each transition rechecks exact owner, open status and absent end time inside the SQLite transaction. It appends `request_abandoned` and retains the request and every prior event. `execution_outcome=abandoned` and `reconciliation.reason/observed_at/actual_end_known=false` are separate from `ended`, which stays absent: the observation time is **not** the actual end time. Status is `abandoned_without_usage` or `abandoned_with_usage`; late authoritative usage upgrades availability idempotently without reopening the request or adding another attempt. Known counters and saved rates are preserved; no usage is fabricated. Completion that wins the write race prevents abandonment.

Read-time `execution_state` is `owner_live`, `unresolved`, `abandoned` or `closed`. The compatible summary key `pending` now counts only open `pending` **and** `usage_received` rows with matching local owner-process evidence; additive `unresolved` and `abandoned` counts expose the rest. An ended row never contributes to the open count. SQL, Python, groups, trends and All profiles use these semantics; All profiles remains independent profile snapshots, not a globally simultaneous liveness proof.

## Activation and verification

These changes modify recorder/writer code and shared accounting. Analytics-only reload must refuse them. Activation requires a separately approved deployment of the complete plugin (including `ledger_runtime/ownership.py`) and Desktop source to every intended profile, followed by restarting the Python backend **and each already-running producer** (CLI, gateway and relevant workers). A backend-only restart does not reload another CLI process. Reload the Desktop plugin if its installed JavaScript is not picked up automatically. File deployment does not update already-imported Python objects. The existing installer recursively includes the new module; no installation or restart is part of source verification.

Newly recorded attempts can carry sufficient evidence for automatic reconciliation. Historical requests without that identity remain visible as unresolved; restarting does not manufacture their missing ownership or usage. Reads alone do not terminalise requests. Actual provider execution and installed/live activation remain unverified by the synthetic tests.

Regression tests use synthetic ledgers and deterministic interleavings covering cleanup versus completion/capture, overlapping turns, profile recovery, late usage, immutable Codex identity, safe diagnostics, source priority and full-window reason counts. They do not prove every provider transport reports terminal usage, and do not eliminate genuine cancellation/transport gaps.
