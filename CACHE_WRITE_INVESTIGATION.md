> Capture reference retained from test.14. The dashboard definition of Cache writes
> changed in test.17; see SESSION_CACHE_WRITES.md. Raw capture described below remains.

# Cache-write investigation — 2.0.0-test.14

This is a **passive diagnostic build**, not a claim that positive cache writes have
been recovered from the user's Codex subscription. The visual layout is unchanged.
Explicit zero, positive, absent, invalid and contradictory fields stay distinct.

## Why go one layer earlier?

The user's test.13 record contains `native_terminal_usage`, a cache-write zero,
and `field_provenance: response_field`. That establishes the contents of the
SDK event captured by the plugin. It does not independently establish the JSON
before SDK model construction.

In the inspected OpenAI Python SDK, streaming JSON is decoded and passed to
`BaseClient._process_response_data` before typed models are created. This revision
observes that earlier boundary. It also associates the finalized SDK HTTP request
with the exact existing main/auxiliary ledger record at `BaseClient._build_request`.

Neither the inspected Hermes consumer nor the SDK sources reviewed here established
a specific bug that injects this user's zero. The purpose is to test that remaining
local uncertainty, not to claim that a zero must be wrong.

## New recorded evidence

Every successfully correlated Responses request can gain:

- `cache_request`: whether cache options were sent, sent mode/retention/TTL,
  explicit-breakpoint count, whether a comparison response was requested,
  input-message counts/roles, and sent service tier/reasoning effort.
- `cache_evidence`: decoded terminal usage before SDK models, field presence and
  state, endpoint family, response ID, and returned cache-diagnostic metadata.
- `usage_source: wire_terminal_usage`: numeric usage at this earlier boundary;
  later SDK/assembled callbacks cannot overwrite it with weaker evidence.

The existing per-request JSON inspector and Copy JSON button expose these fields.
The pre-SDK hook observes sync and async SDK clients because both use the inspected
BaseClient methods. Non-streaming Responses objects are supported too. No messages,
responses, tokens of generated text, tools, credentials or raw cache keys are saved.
Instructions/tools/cache keys have per-process HMAC fingerprints only; fingerprints
cannot be compared across recorder restarts because the key is not persisted.
Request bodies above 16 MiB or unread streaming request bodies are not inspected.

No request arguments are changed. No diagnostic parameter or cache breakpoint is
inserted. No request is replayed, no cache is intentionally invalidated, no extra
inference is performed, and no TLS/proxy configuration is changed. These hooks add
local metadata work and database writes, not provider requests. Existing quota and
public-pricing refreshes continue their prior network behavior.

This does **not** instrument the separate Codex app-server runtime, a custom
transport bypassing these SDK methods, or uncorrelated external clients. A changed
SDK signature is reported as unavailable instead of guessing how to wrap it. A
producer's Online badge is not proof that this particular boundary was exercised:
check `cache_evidence` or the report below. Multiple terminal response IDs inside
one logical Hermes attempt are flagged, not claimed as separate fully captured
SDK-internal retry accounting. At most 16 such IDs are retained in the row.

## Read-only report

After installing/restarting the same producer, use Hermes normally. From the
extracted package folder run:

```bash
python3 cache_write_report.py \
  --home "$HOME/.hermes/profiles/infra" \
  --hours 24 \
  --out cache-report.json
```

Use the selected profile's actual HOME; a shared plugin symlink does not necessarily
mean the profiles share a ledger. For the default profile omit `--home`. A custom
ledger can be supplied with `--db /path/to/usage-ledger/events.sqlite3` instead.
The report reads only that ledger, not `state.db`, and uses no model/network calls.
An existing output file is not overwritten. `--hours 0` selects all recorded history.
The report snapshots one row per recorded request; it does not sum observation events.

Report classifications are intentionally separate:

| Evidence | Meaning |
|---|---|
| `pre_sdk_http_json:positive` | An observed upstream Responses JSON field is positive. |
| `pre_sdk_http_json:explicit_zero` | That field was explicitly zero before SDK model construction. |
| `pre_sdk_http_json:field_absent` | No supported write field occurred in that observed JSON. |
| `pre_sdk_http_json:invalid_or_null` | An observed write field was null or not a valid count. |
| `pre_sdk_http_json:conflicting_fields` | Supported write fields disagree. No exact write total is selected. |
| `sdk_or_adapter_usage:*` | Original test.13-style captured usage, without the new earlier evidence. |
| `normalized_only:*` | Only Hermes-normalized counters were retained. |

The report includes a few safe examples, producer adapter-registration states and
an AST-only SDK signature check. The signature check refers to the Python environment
running the report, which may differ from Hermes's environment; the producer's
recorded adapter state is therefore also included. Review identifiers before sharing.

Old requests do not acquire new evidence retrospectively. They and their saved
prices are preserved. Explicit zeros are not transformed into positive values.
The field is a **provider-reported accounting quantity**, not a measurement of
all physical cache memory activity.

## What the research supports

1. Official OpenAI docs support both implicit and explicit caching; lack of an
   explicit breakpoint alone does not establish that caching/writing was disabled.
2. Reusing an existing cached prefix can refresh its lifetime without a further
   cache-write charge. One high-read, zero-write request is not self-contradictory.
3. Supported Responses API models offer `prompt_cache_options.comparison_response_id`
   and returned `prompt_cache_diagnostics` for cache-hit/miss investigation. These
   diagnostics describe reuse, not an independent physical-write counter.
4. Public Responses API capability is not a promise about the ChatGPT Codex OAuth
   backend. An August 2026 issue report documents that backend rejecting
   `prompt_cache_options` with HTTP 400. This is a report about that configuration
   at that date, not proof that every current subscription route rejects it.
5. This revision saves returned diagnostics when present; it deliberately does not
   send an unverified parameter to the user's running agent. A supported diagnostic
   comparison can be a follow-on step once endpoint capability is established.

Do not compute writes as `input - reads`, count the next request's increased read
quantity as writes, or force cache churn just to make the card nonzero. Those
operations cannot establish the original per-request provider-reported write count.

### Primary sources reviewed

- OpenAI prompt caching:
  https://developers.openai.com/api/docs/guides/prompt-caching
- Official prompt-cache diagnostics (indexed German/French documentation retrieved):
  https://developers.openai.com/de-DE/api/docs/guides/prompt-caching/diagnostics
- OpenAI Python SDK streaming:
  https://raw.githubusercontent.com/openai/openai-python/main/src/openai/_streaming.py
- OpenAI Python SDK request/model-construction boundaries:
  https://raw.githubusercontent.com/openai/openai-python/main/src/openai/_base_client.py
- Hermes Codex runtime:
  https://raw.githubusercontent.com/NousResearch/hermes-agent/main/agent/codex_runtime.py
- Subscription endpoint incompatibility report, August 12, 2026:
  https://github.com/router-for-me/CLIProxyAPI/issues/4915

## Install / remove

Use the existing installer against the same real HOME as the working installation:

```bash
python3 install.py --home /your/existing/hermes/home
python3 install.py --home /your/existing/hermes/home --apply
```

Keep the backup receipt. Restart the relevant Hermes producer and serving backend,
and reload Desktop. Do not install over a shared profile symlink: update its real
existing target as before. No need for the earlier cumulative-counter watcher.

Rollback uses the receipt printed by the installer and preserves recorded ledger data:

```bash
python3 install.py --rollback /path/to/receipt.json --apply
```

Restart the same processes after rollback. No Hermes core source files are edited.
