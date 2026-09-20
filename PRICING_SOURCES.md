# Automatic provider pricing — test.3

Public-source review: 20 September 2026. Sources were inspected through web retrieval. Direct HTTP downloads from this build container failed. The runtime HTTP adapters have therefore been tested with source-derived schema fixtures and a mock transport, **not a successful end-to-end live refresh**. The installed plugin reports refresh failures, stale data and missing models instead of silently guessing.

## Sources and supported coverage

| Route | Price source | This build's coverage |
|---|---|---|
| `openai-codex`, `openai-api`, `openai` | https://developers.openai.com/api/docs/pricing | Reviewed flagship tables for exact `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`; standard, batch, flex and fast; short and long context. |
| `openrouter` | https://openrouter.ai/api/v1/models | Exact model IDs with prompt/completion pricing. Optional input-cache read/write fields and published minimum-prompt overrides. Routed endpoints and non-token costs can differ from list prices. |
| `nous` | https://inference-api.nousresearch.com/v1/models | Best-effort official model-catalog adapter. A usable response from this endpoint was **not verified** during source review. If authentication, schema or published rates are unavailable, the plugin reports unavailable. It does not reuse OpenRouter prices under the Nous name. |
| `ollama`, `ollama-cloud` | https://ollama.com/pricing | Published input/cached-input/output table. No invented cache-write price. Unsupported time-dependent pricing causes the parser to stop rather than assume a rate. |

The shipped fallback data contains the reviewed OpenAI flagship rates and Ollama text-token rates. Its origin explicitly says **bundled provider snapshot**; it is not presented as a live API fetch. OpenRouter and Nous have no fabricated offline rate seed.

The OpenAI long-context threshold is **more than 272,000 prompt tokens**, not the session's configured context capacity. Sources:

- https://developers.openai.com/api/docs/models/gpt-6-astra
- https://developers.openai.com/api/docs/models/gpt-5.6-sol
- https://developers.openai.com/api/docs/models/gpt-5.6-terra
- https://developers.openai.com/api/docs/models/gpt-5.6-luna

Both `priority` and `fast` select the published fast table. Explicit unknown service tiers stay unpriced. An unspecified/auto tier uses standard list rates **as an assumption**, recorded on the selected rate. Returned service tier takes precedence. Exact returned model also takes precedence; `-900k` is not blindly stripped. An unpublished alias remains unpriced unless the response identifies a supported published model.

## Refresh and network boundary

A background worker starts with the Python recorder or ledger API, independently of inference. It attempts each allowlisted public source; successful sources refresh every six hours, failed sources retry after fifteen minutes. A manual **Refresh provider prices** button requests a background refresh with a one-minute minimum interval per source. A local file lock coordinates refreshes across processes using the same profile. This lock targets the Linux/macOS Hermes setup; Windows deployments require testing.

The fetcher uses HTTPS, rejects cross-host redirects, accepts at most 16 MiB, and does not forward OAuth credentials, cookies, prompts, usage, profile IDs or request headers to pricing sources. Environment HTTP proxies are disabled for these public fetches. No extra model inference occurs. The existing quota integration's own authenticated probes remain unchanged.

Each successful catalog is append-only, with source URL, timestamp, parser version and normalized-rate SHA-256. Parser/schema changes or network errors retain the last good catalog and are visible in Cache & costs. A stale rate is labelled in the request's provenance. A first-start request may remain unpriced if no matching catalog is available yet; refresh does not retrospectively invent its rate.

## Cost and savings definitions

All amounts are Decimal USD text-token estimates. The provider's returned usage and the chosen price snapshot are separate evidence.

- Uncached input cost = uncached input tokens × uncached-input rate.
- Cache-read cost = cache-read tokens × cache-read rate.
- Cache-write cost = cache-write tokens × cache-write rate; mixed one-hour cache writes require a separately published one-hour rate and a complete TTL breakdown.
- Output cost = output tokens × output rate.
- Cache-read savings = cost of those read tokens at the same request's uncached-input rate − cache-read cost.
- Cache-write premium = cache-write cost − cost of those write tokens at that uncached-input rate.
- **Net cache savings = read savings − write premium. It can be negative.**

Rates are stored per million; the calculations divide by 1,000,000. Reasoning is not charged a second time. Unknown quantity or rate is not zero. Known zero quantities cost zero, even without a rate. Partial subtotals and missing-field counts remain visible.

These estimates exclude tools, images/audio-specific charges, taxes, regional-processing uplifts, route-specific surcharges and actual subscription debits. They are not a Codex-quota-dollar conversion. OpenRouter list prices are not guaranteed to match the actual endpoint used. See https://openrouter.zendesk.com/hc/en-us/articles/51691717731483-Why-was-I-charged-more-per-token-than-the-price-shown-on-the-model-page .

Completed requests keep their original pricing snapshot, including on duplicate notifications. Older manual rate metadata and already-recorded costs are retained for audit, but manual rates are no longer selected for new records. The manual-entry route returns HTTP 410, and the form is removed. There is no automatic repricing of historical records.
