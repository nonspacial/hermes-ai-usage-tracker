# 2.0.0-test.17 — session-derived Cache writes

## Dashboard definition

The main **Cache writes** number now uses the requested formula:

```
write_delta = max(0, current cache reads - previous cache reads)
window_writes = sum(write_delta for requests in the selected window)
```

There is no separate “Read growth (proxy)” line. The card caption is
**Calculated from session reads**. The same values appear in the request table,
Models & tasks, the Cache & costs token card, JSON and request CSV exports.

## Session scope and ranges

Each profile database is independent. Comparisons use the actual session, provider,
model, API route, recorded account and subagent identity. Main requests and helper
task streams are separate. Parent-session grouping never merges child cache chains.
Process restarts, service-tier changes, cache-option changes and compression events
no longer suppress an otherwise available positive difference.

Rows are ordered by request start and ID. A first read establishes the baseline;
it does not create an invented preceding read of zero. Falling or unchanged reads
contribute zero; a later rise contributes its positive difference. A pending row
without usage is not a read observation. Missing readings remain missing.

The preceding observation is found before time/page/project/agent filtering. Changing
a date range or page therefore does not restart the baseline. Only the later request's
delta contributes to its selected window. Refreshes do not count the same pair twice.

## Data contract

- `calculated_cache_writes` in each response record supplies `tokens`, `basis`,
  `method: session_read_delta`, the previous request ID and both read counts.
- `session_cache_writes` in all summary/group/trend objects supplies the sum,
  number of comparisons, baseline records and missing comparisons.
- The UI's Cache writes and CSV `cache_write_tokens` use that calculated quantity.
  CSV also includes `provider_cache_write_tokens` and the calculation method.
- Original `usage`, `raw_usage`, provider write counts, captured evidence, saved
  prices and cost accounting remain untouched. Calculated writes are not additional
  processed tokens. Dollar accounting continues to use the provider usage and
  saved rates; the writes card labels that separate amount **Provider write cost**.
- Read-time TEMP views add the calculation without rewriting stored events. Older
  recorded sequences can therefore populate the calculated metric after updating.
- CSV now takes the union of row fields, so a pending first row cannot omit later
  rows' input/output/read fields.

## Updating

Use the normal terminal, from this extracted package directory, with the same
real Hermes home as the working installation:

```bash
python3 install.py --home /path/to/your/hermes/home
python3 install.py --home /path/to/your/hermes/home --apply
```

The first command previews changes; the second applies them with a rollback receipt.
Restart the backend/gateway and relevant producer processes, then reload Desktop.
For symlinked profile plugins update the real shared installation, not the symlink.
Existing records are preserved. This package includes all earlier capture changes;
no separate test.14 installation is needed.

## Checks

209 Python tests and 12 offline Chromium suites passed. Checks cover rising,
falling and repeated read counts, separate sessions/subagents/helpers/providers,
restart/tier continuity, historical predecessors, filters, pagination, live UI
refresh, copy/export and unchanged raw ledger rows and prices. The preview uses
synthetic data. This revision has not been exercised in the user's authenticated
Hermes environment.
