# Automatic provider pricing

Public-source review: 22 September 2026. OpenAI's live public pricing page and its official `.md` representation were fetched anonymously and parsed successfully in isolation (126 priced tier/band rows across 35 exact model IDs at review time). This does **not** establish a live refresh in an installed profile or actual account charges. Other providers remain as previously reviewed with mock transport coverage.

## Sources and supported coverage

| Route | Price source | This build's coverage |
|---|---|---|
| `openai-codex`, `openai-api`, `openai` | https://developers.openai.com/api/docs/pricing and https://developers.openai.com/api/docs/pricing.md | Exact published flagship model IDs discovered dynamically from the provider's per-million-token Standard, Batch, Flex and Fast tables. Only explicitly priced bands are used; short/long boundary is validated against the same pricing page's column tooltips. `openai-codex` is explicitly an **API-equivalent subscription estimate**, not a measured subscription debit. |
| `openrouter` | https://openrouter.ai/api/v1/models | Exact model IDs with prompt/completion pricing. Optional input-cache read/write fields and published minimum-prompt overrides. Routed endpoints and non-token costs can differ from list prices. |
| `nous` | https://inference-api.nousresearch.com/v1/models | Best-effort official model-catalog adapter. A usable response from this endpoint was **not verified** during source review. If authentication, schema or published rates are unavailable, the plugin reports unavailable. It does not reuse OpenRouter prices under the Nous name. |
| `ollama`, `ollama-cloud` | https://ollama.com/pricing | Published input/cached-input/output table. No invented cache-write price. Unsupported time-dependent pricing causes the parser to stop rather than assume a rate. |

The bundled fallback includes reviewed GPT-6 Sol rates for **new, empty catalogues only**. It does not replace an installed catalogue because `seed()` skips sources with any existing snapshot. A successful background/manual refresh inserts a new snapshot. Existing saved costs and rate snapshots are not rewritten; previously unpriced requests can gain a separately labelled, read-only retrospective valuation using the latest observed provider rate.

The OpenAI short/long boundary is **more than 272,000 input tokens** for the reviewed flagship table, read from its pricing-page column tooltips; no per-model threshold allowlist or model-page fetch is used. If tooltip or table schema changes, refresh fails closed and retains the last verified snapshot. The parser does not strip annotated display names into routable aliases, infer unsupported tiers or fill absent cache-write rates.

Both `priority` and `fast` select the published fast table. Explicit unknown service tiers stay unpriced. An unspecified/auto tier uses standard list rates **as an assumption**, recorded on the selected rate. Returned service tier takes precedence. Exact returned model also takes precedence; `-900k` is not blindly stripped. An unpublished alias remains unpriced unless the response identifies a supported published model.

## Refresh and network boundary

A background worker starts with the Python recorder or ledger API, independently of inference. It fetches the full public provider catalogue once per successful UTC calendar day (not once per request/model). Failed sources retry no more than once per fifteen minutes. A manual **Refresh provider prices** button queues a background refresh even after a successful daily fetch, with a one-minute minimum interval per source. In-process signals coalesce into one worker; a local file lock coordinates refreshes across processes using the same profile. This lock targets the Linux/macOS Hermes setup; Windows deployments require testing. No network fetch occurs in request hooks or ledger reads.

The fetcher uses HTTPS, rejects cross-host redirects, accepts at most 16 MiB, and does not forward OAuth credentials, cookies, prompts, usage, profile IDs or request headers to pricing sources. Environment HTTP proxies are disabled for these public fetches. No extra model inference occurs. The existing quota integration's own authenticated probes remain unchanged.

Each successful catalog is append-only, with source URL, timestamp, parser version and normalized-rate SHA-256. Parser/schema changes or network errors retain the last good catalog and are visible in Cache & costs. A stale rate is labelled in the request's provenance. A first-start request may remain unpriced if no matching catalog is available yet. A later verified catalogue can value it retrospectively but never claims the newly published rate was effective at request time.

## Retrospective valuation and accounting authority

New requests capture a matching rate snapshot from the provider catalogue observed **no later than their completion**. A request's saved rate and cost are fixed thereafter. Previously unpriced, completed requests with positive known token quantities and no saved rate/cost can receive a supplemental **read-side** API-equivalent valuation from the *latest* observed catalogue for their serving provider. An installed profile needs a successful refresh to discover new published Sol rates; editing the seed alone does not update it. This valuation uses exact returned (or requested) model, service tier and eligible context band; unknown routes, aliases, tiers and unsupported bands remain unpriced. No model-vendor fallback is used for routers.

The response's `supplemental_valuation` identifies `current_published_rate_for_past_usage`, original request end, source URL/ID, matched model/tier/band, observation timestamp, catalogue revision and hash. `cost.rate.retrospective` distinguishes it from a saved at-use rate. `stored_accounting` exposes the unchanged original usage and cost. The displayed cost and all full-window SQL/Python summaries, groups, trends, applied-rate breakdowns and paginated request exports use the same TEMP projection and Decimal component arithmetic; no ledger row or event is rewritten, and a saved rate/cost is never replaced. Missing quantities or rate fields remain unknown rather than zero; the known subtotal is not necessarily the complete charge. Reports are independent read snapshots: after a catalogue changes, the estimate may change, but saved history does not. A paginated CSV aborts if catalogue identity changes between pages.

There is currently **no verified provider-reported monetary charge ingestion** in this plugin; token pricing must not be presented as an actual billed charge. In particular, Copilot subscription usage has no verified public account API returning a per-request debit. Do not invent a credit-to-dollar conversion or call a model vendor's catalogue on behalf of a serving router. Adding genuine charge capture would require a separately verified, narrowly allowlisted provider response field and precedence rules before making actual-charge claims.


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

Completed requests keep their original pricing snapshot, including on duplicate notifications. Older manual rate metadata and already-recorded costs are retained for audit, but manual rates are no longer selected for new records. The manual-entry route returns HTTP 410, and the form is removed. There is no durable repricing of historical records.
