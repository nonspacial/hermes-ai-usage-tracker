# Recorder lifecycle and incomplete usage

## Request identity and finalisation

Hermes's `on_session_end` hook is emitted at turn completion. Cleanup therefore requires an exact session and turn ID, a producer-local registered profile, and the current process identity. A missing or ambiguous identity is not permission to sweep a session or another profile.

Cleanup selects open `pending`/`usage_received` requests. Every write rechecks the expected turn, process and open status inside its SQLite write transaction. A completion that arrives after the selection wins; cleanup cannot overwrite it.

A request ended without a post/error hook is `ended_without_usage` when no numeric usage exists, or `ended_with_usage` when numeric usage was captured. Neither label asserts successful completion. The independent `turn_outcome` retains supplied booleans and an allowlisted exit-reason category. Late authoritative usage can change availability to `ended_with_usage`, but cannot turn a failed request into a successful one or reopen a final request.

The Codex runtime wrapper snapshots request identity at entry and uses that identity for its returned response, even if the agent has advanced meanwhile. Storage rechecks usage-source priority inside the write transaction; a late weaker or empty usage update cannot erase stronger retained counters. Saved rate snapshots on final records remain pinned.

Native API error hooks supply a dictionary. The recorder retains its validated error class name, not the dictionary's Python class and not the error message. Exit reasons outside the reviewed vocabulary become `other`; arbitrary strings are not retained.

## What unavailable values mean

`missing_fields` remains the exact per-field unavailable count. Additive `missing_reasons` counts partition that count into:

- `awaiting_usage`: an open request has not supplied the field yet.
- `unverified_accounting`: normalized cache zeros/decomposition lack provider evidence, including historical read-time projections.
- `ended_without_usage`: the request ended and no numeric usage was retained.
- `unreported_field`: other absent fields in otherwise recorded requests.

The same classifications cover full-window summaries, trends and groups, not only the current Requests page. UI notes/tooltips explain these categories. Cost estimates may be incomplete because tokens are unknown, not solely because unit prices are absent.

An unavailable breakdown does not remove a known processed-token total. One request can lack several fields and cause several card warnings. These are not independent lost-request counts.

Historical normalized-only records without retained raw evidence cannot be honestly reconstructed. No migration or backfill is performed; absent values remain distinct from zero. Calculated session cache writes, provider counters and saved-dollar accounting remain separate.

## Activation and verification

These changes modify recorder/writer code and shared accounting. Analytics-only reload must refuse them; load the installed version through a separately arranged backend/producer restart. File deployment does not update already-imported Python objects.

Regression tests use synthetic ledgers and deterministic interleavings covering cleanup versus completion/capture, overlapping turns, profile recovery, late usage, immutable Codex identity, safe diagnostics, source priority and full-window reason counts. They do not prove every provider transport reports terminal usage, and do not eliminate genuine cancellation/transport gaps.
