# Skills usage and context history

## What is recorded

The producer adds append-only `skill_events` rows to that profile's existing
`usage-ledger/events.sqlite3`. Opening the dashboard does **not** initialise or
migrate a database. Producers create the new table when their existing `Store`
is initialised. Installing these Python changes requires the usual producer and
backend reload/restart; the analytics-only reload deliberately cannot activate
them. This implementation performs no installation or restart.

- `post_tool_call` observes only `skill_view`. A successful main call contributes
  one load; the validated lookup name consistently identifies main/reference/failed
  attempts (distinct aliases remain distinct). Absolute/traversing skill names are
  rejected without persistence. Any supplied `file_path` is a separate reference attempt. Failed main
  and reference attempts contribute failures, not loads/references.
- Repeated successful main calls still count as loads. `repeat` marks an earlier
  successful view of the same skill/file in the **exact session**, including
  observations before the selected reporting period. Hermes unchanged-content
  stubs remain successful calls, with `deduplicated: true`,
  `content_returned: false` and unknown returned-content token estimate.
- Tool observations require session, turn, tool-call and exact registered request
  identity. Missing, ambiguous or mismatched identity is not guessed. The request
  pins the profile/provider/model even after `post_api_request` clears the current
  context or concurrent turns advance it. An uninstrumented request cannot be
  reconstructed from its tool result alone.
- Before model requests, the recorder estimates the **raw** `request_messages`
  and `system_prompt` hook fields. It does not use the potentially truncated
  `request.body` diagnostic envelope.
- The existing guarded compression wrapper records `compression_before` on entry.
  Only a matching native `commit_status == committed` plus a returned
  `(messages, system_prompt)` pair permits `compression_after`. Aborts and raised
  exceptions have no fabricated after-state. Both rows share `compression_id`;
  session rotation preserves the distinct before/after session IDs.
- `on_session_end` is a turn-completion boundary in the inspected Hermes runtime.
  It supplies no messages or occupancy. Its `turn_end` observation therefore has
  `context_used: null`, `context_max: null` and empty categories, rather than
  recycling the last request as a new measurement.

The skill tool is **not** monkey-patched. Hooks return no replacements and cannot
change model/tool results. Compression observers retain the original result and
exception behaviour. Native callback IDs make repeated notification insertion
idempotent within the immutable request identity; reused native tool IDs across
different requests remain distinct. SQLite transactions serialise concurrent duplicate checks and repeat
classification. Different profiles use separate databases; different turns use
separate event IDs. Existing billing/cost/counter calculations are untouched.

## Estimates and limits

`context_used` prefers the native request hook's `approx_input_tokens`, when
available, with source `native_preflight_estimate_categories_chars_v1`.
Otherwise `rough_chars_v1` estimates the directly visible messages/instructions
using characters divided by four, rounded upwards per component. Neither is a
provider-token measurement. Category sums need not equal the native preflight
estimate. Missing raw messages give unknown categories, not a fabricated split.

The categories are deliberately coarser than Hermes' live context inspector:

| ID | Meaning |
| --- | --- |
| `system_prompt` | Visible system/developer instructions, undivided |
| `skills_index` | Visible `<available_skills>` catalogue block, not loaded skill bodies |
| `skills` | Directly paired retained `skill_view` tool-result messages, including their envelope |
| `conversation` | Other visible conversation and tool-result messages |

The recorder never builds a fresh prompt, calls a provider, reads skill files,
or invokes the live context inspector just to record telemetry. Rules, memory,
MCP schemas and tool definitions cannot reliably be separated from these hook
fields. They are not invented as zero-valued categories. The raw-message fallback
excludes tool schemas and is a partial estimate, not total context-window
occupancy. `context_max` is unknown at the request/turn hooks; compression can
supply the compressor's already-resolved `_resolved_context_length`; telemetry
never invokes the lazy `context_length` property or resolves model metadata. Do not infer the window size
from output-token limits.

Retained skill attribution requires matching `skill_view` tool calls and full,
successful result payloads in the **same snapshot**. OpenAI tool messages and
Responses function-call/output pairs are recognised. Other representations,
pruning and compressed summaries remain unassigned: `attribution: unknown`
when no directly recognisable retained skill is present. Even
`direct_tool_pairs_only` means partial positive evidence, not proof of complete
attribution. Historical load counts are never substituted for retained tokens.
Main skill `estimated_tokens` measures returned content only, not current
retention, and is null if any counted main load has an unknown estimate.

Micro-compaction, pruning, providers bypassing these hooks, unavailable adapters,
missing identities and uninstrumented producers may leave gaps. This is not an
exact tokenizer, full historical reconstruction or uptime guarantee. Coverage
is conservatively `partial` after any observation and `not_recorded` before any;
it never asserts complete `recording` coverage from the mere presence of rows.
`coverage.since` is the earliest stored observation in the selected profile,
not a promise that every event since then was captured. Zero counts mean zero
**observed** matching events, not proof that no historical activity occurred.

## Read-only API

`GET /ledger/skills` is relative to the existing plugin namespace and uses the
host router's authentication/profile resolution. Parameters:

- `profile`, `start`, `end`, `provider`, `session`, `session_scope`, `agent`,
  `project`, `subagent`, `test_id`: existing ledger scope conventions.
- `start` is inclusive; `end` exclusive; timestamps are Unix seconds.
  A valid test marker replaces the time window, as in the existing ledger.
- `session_scope=exact|family` uses recorded `session_lineage`, never combines
  similarly named sessions. `agent` is empty, `primary`, `subagent` or `unknown`.
- `model`: exact match, applied to the whole response.
- `skill`: exact detail selection only. Pie aggregates, summary and model options
  remain scoped to the full global/model window. Context detail requires positive
  `retained_skills` membership; unknown/unattributed snapshots are not assigned
  to the selected skill.
- `offset >= 0`; `1 <= limit <= 200`. Pagination affects `events` only.

Response contract:

```text
{
  version: 1, generated_at: number, window: {start: number, end: number},
  coverage: {status: 'recording'|'not_recorded'|'partial', since: number|null, note: string},
  summary: {loads: int, references: int, failures: int, sessions: int},
  skills: [{name, loads, references, failures, sessions, repeat_loads, estimated_tokens: number|null}],
  model_options: [string], events: [Event], event_count: int, next_offset: int|null,
  snapshots: [Event], snapshot_count: int, snapshots_truncated: bool
}
```

`Event` has `id`, `ts`, `kind`, `session_id`, `provider`, `model`; optional
`skill`, `file_path`, `success`, `is_reference`, `repeat`, `estimated_tokens`,
`turn_id`, `request_id`, `project_id`, `project_label`, `agent_kind`,
`context_used`, `context_max`, `categories: [{id, label, tokens}]`, `source`,
`attribution`, `compression_id`. Kinds are `skill_load`, `context_snapshot`,
`compression_before`, `compression_after`, `turn_end`. Additional content-free
fields are `session_lineage`, `subagent_id`, `retained_skills`,
`content_returned`, `deduplicated`.

Aggregates cover the full filtered period, independent of pagination. Sessions
count distinct exact sessions with a skill attempt (including references or
failures), not snapshot-only sessions. Skills sort by main loads descending,
then name. Events and snapshots sort by `(ts DESC, id DESC)` deterministically.
`snapshots` contains all non-load kinds, separately bounded to 500 newest rows;
`snapshot_count` is the unbounded matching count and truncation is explicit.
All reads use one read-only SQLite transaction, including aggregates and pages.
A missing database/table returns `not_recorded` without creating either.
Invalid scope/time/pagination/test markers return 400; unknown profiles return
404; unavailable/corrupt storage returns a sanitised 503.

## Privacy and verification

Persisted observations contain timestamps, numbers, booleans, fixed categories,
safe identifiers and existing project display metadata. No prompts, skill text,
tool arguments, arbitrary results, error messages, absolute skill paths,
credentials or provider diagnostics are stored. Identifiers are bounded and
validated; reference paths cannot be absolute or traverse parents. Raw content
is inspected transiently to derive counts and immediately discarded.

`tests/test_skills_usage.py` uses synthetic profiles, SQLite databases, hook
payloads, compression functions and in-process API clients. It covers success,
reference/failure/repeat accounting, unchanged stubs, privacy, duplicate and
concurrent hooks, exact profile/turn identity, retained-context attribution,
compression commits/aborts/exceptions, unknown turn-end occupancy, global and
family filters, full-period aggregates, deterministic pagination, bounded
snapshots, test markers, missing/legacy read-only databases and analytics reload
protection. No live provider or database is accessed. The canonical isolated
verification command is:

```sh
python "$TMPDIR/verify_ai_usage_import.py" python
```

This exercises the Python suite in the existing network-isolated, real-home-
hidden bubblewrap environment. It is not evidence that already-running Hermes
processes have loaded the new hooks.
