# Automatic provider pricing

Public-source review: 22 September 2026. OpenAI's live public pricing page and its official `.md` representation were fetched anonymously and parsed successfully in isolation (126 priced tier/band rows across 35 exact model IDs at review time). This does **not** establish a live refresh in an installed profile or actual account charges. Other providers remain as previously reviewed with mock transport coverage.

## Sources and supported coverage

| Route | Price source | This build's coverage |
|---|---|---|
| `openai-codex`, `openai-api`, `openai` | https://developers.openai.com/api/docs/pricing and https://developers.openai.com/api/docs/pricing.md | Exact published flagship model IDs discovered dynamically from the provider's per-million-token Standard, Batch, Flex and Fast tables. Only explicitly priced bands are used; short/long boundary is validated against the same pricing page's column tooltips. `openai-codex` is explicitly an **API-equivalent subscription estimate**, not a measured subscription debit. |
| `openrouter` | https://openrouter.ai/api/v1/models | Exact model IDs with prompt/completion pricing. Optional input-cache read/write fields and published minimum-prompt overrides. Routed endpoints and non-token costs can differ from list prices. |
| `nous` | https://inference-api.nousresearch.com/v1/models | Best-effort official model-catalog adapter. A usable response from this endpoint was **not verified** during source review. If authentication, schema or published rates are unavailable, the plugin reports unavailable. It does not reuse OpenRouter prices under the Nous name. |
| `ollama`, `ollama-cloud` | https://ollama.com/pricing | Published input/cached-input/output table. No invented cache-write price. Unsupported time-dependent pricing causes the parser to stop rather than assume a rate. |
| `anthropic` (direct Claude API only) | https://platform.claude.com/docs/en/about-claude/pricing.md, plus https://platform.claude.com/docs/en/models/overview.md and linked `models/<slug>/overview.md` pages | Prices come from the Model pricing, Fast mode, Prompt caching, Data residency and Long context sections. Exact Claude API IDs and aliases come only from each model page's `Model IDs` table, checked against the overview. Display names are not slugified. See [Direct Anthropic](#direct-anthropic). |

The bundled fallback includes reviewed GPT-6 Sol rates for **new, empty catalogues only**. It does not replace an installed catalogue because `seed()` skips sources with any existing snapshot. A successful background/manual refresh inserts a new snapshot. Existing saved costs and rate snapshots are not rewritten; previously unpriced requests can gain a separately labelled, read-only retrospective valuation using the latest observed provider rate.

The OpenAI short/long boundary is **more than 272,000 input tokens** for the reviewed flagship table, read from its pricing-page column tooltips; no per-model threshold allowlist or model-page fetch is used. If tooltip or table schema changes, refresh fails closed and retains the last verified snapshot. The parser does not strip annotated display names into routable aliases, infer unsupported tiers or fill absent cache-write rates.

Both `priority` and `fast` select the published fast table. An unknown service tier (neither documented nor an alias) is **fail-open**: the standard table is selected and flagged `tier_assumption: true`, `tier_basis: unknown_value_as_default`. Known premiums with no published row (e.g. `flex`/`batch` on a routed catalogue) stay unpriced (`endpoint_unpriced`). An unspecified/auto tier uses standard list rates **as an assumption**, recorded on the selected rate. Returned service tier takes precedence. Exact returned model also takes precedence; `-900k` is not blindly stripped. An unpublished alias remains unpriced unless the response identifies a supported published model.

## Direct Anthropic

Public-source review: 27 September 2026 (fetched 02:36 UTC). The bundled seed (`bundled_provider_snapshot_reviewed_2026-09-27`) was produced by this parser from those pages. This follow-up reused the saved review copies; it made no new public fetch.

**Upgrade behaviour** (verified offline against a disposable ledger created by the previous commit, `2b21437`): `Store()` construction calls `seed()`, which inserts every bundled source that has **no** snapshot yet — including into an existing catalogue. An installed ledger that previously had only OpenAI/Ollama rows therefore gains the reviewed Anthropic snapshot the first time new plugin code opens it. That requires the Hermes backend/recorder to be restarted onto the new code; **no manual refresh is needed for the initial seed**. Previously unpriced Anthropic requests then get the read-only retrospective estimate described below; saved rows and costs are unchanged. A later background or manual **Refresh provider prices** adds a live snapshot (`origin: live_public_provider`) alongside the seed; the seed never replaces a source that already has any snapshot.

USD per million tokens at review, direct Claude API, global routing:

| Claude API ID (alias) | Input | 5m write | 1h write | Cache hit | Output | Fast input / output |
|---|---|---|---|---|---|---|
| `claude-fable-5-1` | 10 | 12.50 | 20 | 0.25 | 50 | — |
| `claude-mythos-5-1` (limited availability) | 10 | 12.50 | 20 | 0.25 | 50 | — |
| `claude-fable-5` | 10 | 12.50 | 20 | 1 | 50 | — |
| `claude-mythos-5` (limited availability) | 10 | 12.50 | 20 | 1 | 50 | — |
| `claude-opus-5-5` | 4 | 5 | 8 | 0.20 | 20 | 8 / 40 |
| `claude-opus-5` | 5 | 6.25 | 10 | 0.50 | 25 | 10 / 50 |
| `claude-opus-4-8` | 5 | 6.25 | 10 | 0.50 | 25 | 10 / 50 |
| `claude-opus-4-7`, `claude-opus-4-6` | 5 | 6.25 | 10 | 0.50 | 25 | — |
| `claude-opus-4-5-20251101` (`claude-opus-4-5`) | 5 | 6.25 | 10 | 0.50 | 25 | — |
| `claude-sonnet-5` | 2 | 2.50 | 4 | 0.20 | 10 | — |
| `claude-sonnet-4-6` | 3 | 3.75 | 6 | 0.30 | 15 | — |
| `claude-sonnet-4-5-20250929` (`claude-sonnet-4-5`) | 3 | 3.75 | 6 | 0.30 | 15 | — |
| `claude-haiku-4-5-20251001` (`claude-haiku-4-5`) | 1 | 1.25 | 2 | 0.10 | 5 | — |

Rows are omitted when the pricing page lists a model but no model page documents its Claude API ID, and every row annotated *retired* is skipped before that check. At review this excluded all four retired rows: Claude Opus 4.1, Opus 4, Sonnet 4 and Haiku 3.5 (each "retired, except on Bedrock and/or Google Cloud"). The same applies to any future unlinked or retired row.

Rules:

- **API-equivalent estimate only.** An Anthropic rate is labelled `pricing_basis: provider_catalog_estimate`. It is not a Claude subscription debit, a provider-reported charge or a Claude Code plan cost.
- **Fast mode.** Fast input/output rates come from the Fast mode table. Cache rates use the published cache multipliers on the fast input price. For example, Opus 5.5 fast gives $10 for 5m writes, $16 for 1h writes and $0.40 for hits.
  - `usage.speed` from the response is authoritative.
  - A legacy record that only requested fast is priced as fast. The docs say completed fast requests report fast, and a standard fallback reports `standard`. Because the provider did not confirm it, the rate is flagged `speed_inferred: true` and shown as **fast inferred**; it is not a confirmed returned speed.
  - Fast on a model with no published fast rate stays unpriced. It is never charged at standard rates.
- **Other tiers.** Priority Tier and Batch (returned `usage.service_tier` or requested tier `priority`/`batch`) stay unpriced (`endpoint_unpriced`); their rates are not published here and are never discounted to standard.
  - An unspecified request is priced at standard speed **as a recorded assumption** (`tier_assumption`).
- **Fail-open on unknown enum values.** A `usage.speed`, `usage.inference_geo` or `usage.service_tier` value the catalogue does not know selects the documented default — standard speed, global geo, standard tier — and is always flagged:
  - speed: `speed_inferred: true`, `speed_basis: 'unknown usage.speed value; documented standard speed assumed'` (a fast request keeps the fast rate: `requested fast assumed`);
  - geo: `geo_assumption: true`, `geo_basis: unknown_value_as_default` (`pricing.UNKNOWN_AS_DEFAULT`);
  - tier (returned usage tier or unknown requested tier): `tier_assumption: true`, `tier_basis: unknown_value_as_default`.
  Values with a known premium keep exact handling: `us` 1.1×, `fast`, Priority/Batch (unpriced).
- **Data residency.**
  - `usage.inference_geo: us` uses the published 1.1× rate. This applies only to 4.6+ models, which accept `inference_geo`.
  - `usage.inference_geo: not_available` is recorded verbatim and priced at the global 1.0× rate. Two cases are kept apart:
    - *Documented:* Anthropic's Usage & Cost API returns `not_available` for models that do not support the `inference_geo` parameter (models that don't support it "return `not_available` for this dimension"; standard pricing applies). On a model with no published US rate (pre-4.6) the global rate is exact: no `geo_assumption`, no `geo_basis`.
    - *Observed, undocumented:* operator live captures on 28 September 2026 reported `not_available` for `claude-fable-5-1` and `claude-opus-5-5` requests that sent no geo parameter (not reproduced by the test suite). On a model with a published US rate (4.6+) the actual geo is unknown — a workspace `default_inference_geo` of `us` cannot be excluded — so global is an assumption: `geo_assumption: true`, `geo_basis: not_available_as_global` (`pricing.ANTHROPIC_NOT_AVAILABLE_GEO_AS_GLOBAL`).
  - Any other reported geo, including the recorder's `unrecognised` sentinel, takes the fail-open global default at completion, flagged `geo_basis: unknown_value_as_default`.
  - Rows recorded before `not_available` was admitted hold `unrecognised` (the raw value was not kept; the allow-list gap was closed in commit 0d5ade4). They gain a **labelled retrospective estimate at the global 1.0× rate** (`pricing.ANTHROPIC_UNRECOGNISED_GEO_AS_GLOBAL`) only when all hold: provider `anthropic`; `returned_inference_geo` exactly `unrecognised`; `returned_usage_service_tier` exactly `standard`; `returned_speed` absent or `standard` with no fast request; no recorded requested geo; a first-party or unrecorded endpoint. Basis: Anthropic documents only `global`/`us`/`not_available`, returns `us` only when the request asked for it, and Hermes never sends `inference_geo`; as with `not_available` on 4.6+, a workspace default geo cannot be excluded, so the flag is always set. The rate carries `geo_assumption: true` and `geo_basis: unrecognised_as_global` and is a read-time projection like any retrospective estimate; no ledger row is rewritten. Outside those bounds the sentinel takes the generic fail-open default (`geo_basis: unknown_value_as_default`); Priority/Batch stay unpriced and custom/unverified endpoints take the labelled OpenRouter list-price fallback.
  - Trade-off, accepted: the rule is not date-bounded. New rows record every bounded enum value verbatim, so the sentinel recurs only for non-string/oversized input. A date window was rejected because it would silently stop valuing late-arriving pre-fix rows and gives no better protection than the explicit flag.
  - When no geo is reported for a model that supports US-only routing, global pricing is used and flagged `geo_assumption` (no `geo_basis`).
- **Context window.**
  - 4.6+ models are priced flat over the full published window.
  - Earlier models have no long-context price, so prompts above their documented window stay unpriced, as do unknown prompt sizes.
- **Expiring prices.** A price footnote that describes a time-limited or introductory price, without saying it is now standard, fails the refresh closed.
  - The Sonnet 5 introductory price was stated at review to be "now the standard price".
  - Retired rows are never priced.
- **Cache TTL.** 5m and 1h writes are priced separately from `cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`.
  - Without an exact split that sums to the total write tokens, the write component stays unknown. The rest of the request stays known but incomplete.
  - Cache hits use the model's published hit rate. Opus 5.5 and Fable/Mythos 5.1 have footnoted 0.05×/0.025× multipliers, which the parser validates.
  - Writes are counted once.
- **Routes.**
  - Only the `anthropic` provider route uses these rates. Direct first-party Claude API never lends its rates to routers, Bedrock, Vertex, Foundry or custom hosts.
  - OpenRouter, Nous, Bedrock, Vertex, Copilot and custom providers never borrow them; those without a catalogue here take the OpenRouter list-price fallback below.
  - The endpoint class uses the exact host `api.anthropic.com`, accepting only Hermes' canonical single trailing-dot form (`api.anthropic.com.`). Subdomains, look-alikes, `/anthropic` gateway paths and scheme-less strings are `custom`.
  - Direct records with a non-`api.anthropic.com` base URL are classified `custom` and take the labelled OpenRouter list-price fallback (never the direct catalogue).
  - Main-hook records with no base URL are flagged `endpoint_assumption`.
  - Auxiliary calls (titling, compression, …) are classified from the client object Hermes passes to its relay funnel, with the same helper. When that client exposes no readable `base_url` the call is recorded `unverified` and takes the labelled list-price fallback, including retrospectively; an auxiliary Anthropic client may point at a configured gateway, so it is never assumed first-party.
- **Model names.** Hermes wire names are normalised only as Hermes does for direct Anthropic: strip an `anthropic/` prefix and change `.` to `-`. The result must then exactly match a documented ID or alias. Bedrock/Vertex IDs, `-latest` and undocumented snapshots stay unpriced.
- **Recorder metadata.** `usage.speed`, `usage.inference_geo` and `usage.service_tier` are kept **verbatim** when they are bounded, content-free enum strings (1–64 chars of `A-Za-z0-9_-`); documented or not. Non-string, empty, oversized or otherwise shaped values become the `unrecognised` sentinel. Also kept are a requested `speed` of `fast`/`standard` and a `first_party`/`custom`/`unverified` endpoint class. No URL or other raw field is stored.
- **Display.** Cache & costs keeps its existing Context column. For direct Anthropic rates it shows `Global` or `US-only 1.1×`, `fast inferred` when the speed was not returned, and `assumed geo/endpoint/standard speed` for recorded assumptions. Its shared tooltip gives the full basis. A rate with `geo_basis` (`not_available_as_global` or `unrecognised_as_global`) renders as `Global · assumed geo` (plus any other assumptions); a retrospective one also carries the request-cost `†`. The desktop tooltip does not yet distinguish these from an unreported geo — it reads "Global routing assumed: no usage.inference_geo was reported.", which is inaccurate for them — a known UI gap outside this pricing change. Other providers keep their previous labels.
- **Fail-closed refresh.** A refresh fails closed and keeps the last good catalogue if any of the following changes:
  - the currency statement, table columns or separators
  - the cache multipliers, fast/geo/long-context wording or model-page ID table
  - overview/model-page agreement
  - the model-page crawl: only `https://platform.claude.com/docs/en/models/<slug>/overview.md` pages linked from the overview (or from already-fetched model pages) are fetched, at most 40 distinct pages; any other host, path or query is refused

## Universal list-price fallback (providers without a catalogue)

A provider with no catalogue here is estimated from OpenRouter list prices, always labelled. Direct catalogues always win: a provider in `pricing.PROVIDERS` never consults the fallback, even when its own catalogue lacks the model (reason `model_missing`). Direct first-party Anthropic rates are still never lent to anyone.

**Who uses it** (`pricing.source_for`): every provider id not in `PROVIDERS` (e.g. `copilot`, `gemini`, `xai`, `deepseek`, `zai`, `kimi-coding`, `minimax`, `alibaba`, `mistral`, `groq`, `bedrock`, `vertex`, `custom`, pre-fix `auto`), and direct `anthropic` rows whose endpoint is `custom` or `unverified`.

**Source:** the latest stored `SOURCES['openrouter']` snapshot (`provider_catalog`); no extra network call.

**Match rules** (`pricing.fallback_candidates`; exact only, no fuzzy matching, no `:variant` or `~alias` derived):

1. The recorded model id exactly (`anthropic/claude-opus-5.5`, `qwen/qwen3.8-27b:free`).
2. Otherwise `vendor/name`, where `name` is the recorded id, its lower-case form, and — for `claude-*` — Anthropic's hyphenated minor version in OpenRouter's dotted form (`claude-opus-5-5` → `claude-opus-5.5`). Vendors come from the provider table, then the model-family table. A recorded `vendor/` prefix keeps its vendor.

More than one candidate in the first tier that matches gives no price, reason `ambiguous`.

| Serving provider id | OpenRouter vendor |
|---|---|
| `anthropic` (custom/unverified endpoint) | `anthropic` |
| `gemini`, `google`, `google-gemini` | `google` |
| `xai`, `xai-oauth`, `x-ai`, `grok` | `x-ai` |
| `deepseek` | `deepseek` |
| `zai`, `z-ai`, `glm` | `z-ai` |
| `kimi-coding`, `kimi-coding-cn`, `moonshot`, `moonshotai`, `kimi` | `moonshotai` |
| `minimax`, `minimax-cn`, `minimax-oauth` | `minimax` |
| `alibaba`, `alibaba-coding-plan`, `qwen-oauth`, `dashscope`, `qwen` | `qwen` |
| `mistral`, `mistralai` | `mistralai` |
| `xiaomi` / `stepfun` / `arcee` / `upstage` / `cohere` / `perplexity` | `xiaomi` / `stepfun` / `arcee-ai` / `upstage` / `cohere` / `perplexity` |
| any other id (Copilot, Bedrock, Vertex, Azure, Groq, custom, …) | model family only |

| Model id family (anchored, lower-case) | OpenRouter vendor |
|---|---|
| `claude-` | `anthropic` |
| `gpt-`, `chatgpt-`, `codex-`, `o1`…`o9` (alone or `-…`) | `openai` |
| `gemini-`, `gemma-` | `google` |
| `grok-` | `x-ai` |
| `deepseek-` | `deepseek` |
| `glm-` | `z-ai` |
| `kimi-` | `moonshotai` |
| `minimax-` | `minimax` |
| `qwen` | `qwen` |
| `mistral-`, `devstral-`, `codestral-`, `ministral-`, `magistral-`, `pixtral-` | `mistralai` |
| `llama-` | `meta-llama` |
| `command-` | `cohere` |
| `nova-` | `amazon` |
| `sonar` | `perplexity` |
| `mimo-` | `xiaomi` |
| `step-` | `stepfun` |
| `hermes-` | `nousresearch` |

**Premiums:** a request or response naming priority/fast/flex/batch/scale, a returned `fast` speed, `us` geo, or Priority/Batch usage tier is unpriced (`endpoint_unpriced`): the list carries standard prices only. An unknown tier takes the list default, flagged `tier_basis: unknown_value_as_default`.

**Flags on the selected rate:** `pricing_basis: 'list_price_fallback'`, `basis: 'OpenRouter list price for this model id; serving provider has no catalogue here, actual charge may differ'`, `source: 'OpenRouter list price (fallback)'`, `source_url` = the OpenRouter models API, `fallback_model` (matched id), `fallback_match` (`exact`|`vendor_prefixed`).

**Zero prices.** A catalogue row with $0 input and $0 output (local Ollama, `:free` tiers) is a known price: its unlisted cache prices are also $0, so cost is `0`, `complete: true`, no marker. A non-zero model with an unlisted component stays incomplete.

**Local endpoints.** For a provider with no catalogue, the recorder stores only the content-free class `endpoint_host: 'local'` when `base_url` resolves to loopback, private, link-local, `localhost`, `*.local` or `host.docker.internal` (never the URL or host). Such rows price at a known $0 (`pricing_basis: 'local_endpoint_zero'`, `source: 'Local endpoint'`); a local proxy forwarding to a paid API would be undercounted, as the basis states.

## Unpriced reasons

Every request without a complete cost carries exactly one read-time `unpriced_reason` (never stored):

| Reason | Meaning |
|---|---|
| `no_catalogue` | no catalogue snapshot yet for the source the provider uses (direct or OpenRouter fallback) |
| `model_missing` | the source has no row for the exact model id (or derived ids) |
| `ambiguous` | more than one candidate id / rate row matched |
| `usage_missing` | pending, no usage yet, a token field missing, or a prompt size needed for a band is unknown |
| `endpoint_unpriced` | tier, speed, geo, band or endpoint without a published rate, or a rate lacking a used component |

## Refresh and network boundary

A background worker starts with the Python recorder or ledger API, independently of inference. It fetches the full public provider catalogue once per successful UTC calendar day (not once per request/model). Failed sources retry no more than once per fifteen minutes. A manual **Refresh provider prices** button queues a background refresh even after a successful daily fetch, with a one-minute minimum interval per source. In-process signals coalesce into one worker; a local file lock coordinates refreshes across processes using the same profile. This lock targets the Linux/macOS Hermes setup; Windows deployments require testing. No network fetch occurs in request hooks or ledger reads.

The fetcher uses HTTPS, rejects cross-host redirects, accepts at most 16 MiB, and does not forward OAuth credentials, cookies, prompts, usage, profile IDs or request headers to pricing sources. Environment HTTP proxies are disabled for these public fetches. No extra model inference occurs. The existing quota integration's own authenticated probes remain unchanged.

Each successful catalog is append-only, with source URL, timestamp, parser version and normalized-rate SHA-256. Parser/schema changes or network errors retain the last good catalog and are visible in Cache & costs. A stale rate is labelled in the request's provenance. A first-start request may remain unpriced if no matching catalog is available yet. A later verified catalogue can value it retrospectively but never claims the newly published rate was effective at request time.

## Retrospective valuation and accounting authority

New requests capture a matching rate snapshot from the provider catalogue observed **no later than their completion**. A request's saved rate and cost are fixed thereafter. Previously unpriced, completed requests with positive known token quantities and no saved rate/cost can receive a supplemental **read-side** API-equivalent valuation from the *latest* observed catalogue for their serving provider. An installed profile needs a successful refresh to discover new published Sol rates; editing the seed alone does not update it. This valuation uses exact returned (or requested) model, service tier and eligible context band; unknown aliases and unsupported bands remain unpriced. Providers without a catalogue use the labelled OpenRouter list-price fallback above; a catalogued router never falls back to a model vendor's catalogue.

The response's `supplemental_valuation` identifies `current_published_rate_for_past_usage`, original request end, source URL/ID, matched model/tier/band, observation timestamp, catalogue revision and hash. `cost.rate.retrospective` distinguishes it from a saved at-use rate. `stored_accounting` exposes the unchanged original usage and cost. The displayed cost and all full-window SQL/Python summaries, groups, trends, applied-rate breakdowns and paginated request exports use the same TEMP projection and Decimal component arithmetic; no ledger row or event is rewritten, and a saved rate/cost is never replaced. Missing quantities or rate fields remain unknown rather than zero; the known subtotal is not necessarily the complete charge. Reports are independent read snapshots: after a catalogue changes, the estimate may change, but saved history does not. A paginated CSV aborts if catalogue identity changes between pages.

There is currently **no verified provider-reported monetary charge ingestion** in this plugin; token pricing must not be presented as an actual billed charge. In particular, Copilot subscription usage has no verified public account API returning a per-request debit. Do not invent a credit-to-dollar conversion or call a model vendor's catalogue on behalf of a serving router; the OpenRouter list-price fallback is always labelled an estimate that may differ from the actual charge. Adding genuine charge capture would require a separately verified, narrowly allowlisted provider response field and precedence rules before making actual-charge claims.


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

These estimates exclude tools, images/audio-specific charges, taxes, regional-processing uplifts (except Anthropic's published US-only 1.1× when reported), route-specific surcharges and actual subscription debits. They are not a Codex-quota-dollar conversion. OpenRouter list prices are not guaranteed to match the actual endpoint used. See https://openrouter.zendesk.com/hc/en-us/articles/51691717731483-Why-was-I-charged-more-per-token-than-the-price-shown-on-the-model-page .

Completed requests keep their original pricing snapshot, including on duplicate notifications. Older manual rate metadata and already-recorded costs are retained for audit, but manual rates are no longer selected for new records. The manual-entry route returns HTTP 410, and the form is removed. There is no durable repricing of historical records.
