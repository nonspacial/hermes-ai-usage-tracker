# AI usage + for Hermes Desktop

AI usage + shows your subscription allowances and the usage recorded by your Hermes agents. Use it to check how much quota remains, inspect individual requests, compare models and projects, or compare observed skill loads and estimated returned content over a period.

The plugin appears as **AI usage +** in Hermes Desktop. Its installation name is `ai-usage-tracker`.

This is a development fork of [hermes-ai-usage-tracker](https://github.com/lvabarajithan/hermes-ai-usage-tracker), released under the [MIT licence](LICENSE). It is not an official Hermes release.

## Contents

- [Install the plugin](#install-the-plugin)
- [Open the dashboard](#open-the-dashboard)
- [Inspect recorded usage](#inspect-recorded-usage)
- [Understand the numbers](#understand-the-numbers)
- [Use banked Codex resets](#use-banked-codex-resets)
- [Export a report](#export-a-report)
- [Update or roll back](#update-or-roll-back)
- [Troubleshooting](#troubleshooting)
- [Data and privacy](#data-and-privacy)
- [Technical reference](#technical-reference)

## Install the plugin

You need Hermes Desktop and Python 3. Install the plugin on the machine where Hermes runs, in each Hermes profile whose usage you want to record. Installing it in one profile does not start recording in the others.

The commands below use a local checkout of this repository. Run them from the repository directory.

### 1. Choose the Hermes home

The usual locations are `~/.hermes` for the default profile and `~/.hermes/profiles/<name>` for a named profile. If your installation uses another location, use that instead. The directory must already exist, and the installer refuses symlink destinations or symlinked parent directories.

Set the intended location once. This example selects a profile named `work`:

```bash
TARGET_HOME="$HOME/.hermes/profiles/work"
```

For the default profile, use `TARGET_HOME="$HOME/.hermes"` instead.

### 2. Preview and apply the installation

Check which files the installer will create or replace:

```bash
python3 install.py --home "$TARGET_HOME"
```

If the destination is correct, apply the installation:

```bash
python3 install.py --home "$TARGET_HOME" --apply
```

The installer copies the plugin files, backs up any files it replaces and prints the path to a rollback receipt. Keep that path if you may need to undo the update. It does not enable the plugin, change credentials, delete recorded usage or restart Hermes.

### 3. Enable it for the same profile

For the `work` profile used above:

```bash
hermes --profile work plugins enable ai-usage-tracker --no-allow-tool-override
```

Replace `work` with your profile name. For the default profile, omit `--profile work`. The plugin does not need permission to replace Hermes' built-in tools.

After active work has finished, restart the Hermes backend and the agent processes that should record usage. Copying the files alone does not load the recorder into processes that are already running.

## Open the dashboard

Open **AI usage +** in Hermes Desktop and select a profile. On first use, the pane opens on **Subscriptions**.

Each subscription card shows the allowance and reset windows reported by that provider. Click the provider's name to open its usage pages. The hide control only changes whether the card is shown; it does not disable recording. Use the hidden-provider control or **Unhide all** to restore cards.

Choose **All providers** to inspect recorded usage across providers in the selected profile.

Choose **All profiles** in the profile picker to combine recorded usage from discovered local profiles. This view is read-only. It does not combine subscription allowances, so select an individual profile to see quota cards or use account controls. Check the coverage message before treating the totals as complete. A missing or unreadable profile ledger is not the same as zero usage.

The pane remembers your page, display choices and filters separately for each profile and for All profiles. Reopening a rolling period such as Past 24h keeps it rolling rather than restoring an old fixed date range.

## Inspect recorded usage

Start by selecting a provider or All providers. Choose a time window, then narrow the results by project, session or agent as needed. **All recorded** includes the history available in the selected scope. **Custom** lets you enter start and end times.

The usage pages answer different questions:

| Page | What to do here |
| --- | --- |
| Overview | Read the totals and usage chart, then change the grouping to compare where usage went. |
| Requests | Inspect individual recorded calls and their token counts. Open a record's JSON details when you need the underlying fields. |
| Cache & costs | Compare cache reads, calculated cache writes and estimated costs. Inspect the rates used and refresh provider prices. |
| Compressions | Review recorded context compression and micro-compaction events alongside their linked usage. |
| Models & tasks | Compare usage by model and task. |
| Skills usage | Compare observed skill-load frequency, estimated returned content and recorded catalogue-description inclusion over a period. |

On Skills usage, select a slice of the frequency pie or its legend to inspect that skill's loads. Use the model filter to narrow the results. **Context footprint** totals estimated returned main-skill content over the selected period (not retained context or provider input tokens); missing sizes remain unavailable. **Catalogue overhead** compares future observed description inclusion in the request preflight with actual main-skill loads. It does not measure model attention or reconstruct old descriptions. These reports update on opening, filter changes and manual Refresh, not on every request.

In narrow panes, some tables become collapsed records. Expand a record to see its fields. Widen the pane if you prefer the table layout.

Usage refreshes automatically while the pane is open. Click **Refresh** for an immediate update. A previously opened view may appear straight away from a bounded, temporary Desktop-memory snapshot: its actual generation time, window and age are shown with **Updating** until a new complete response replaces it. On a newly instrumented ledger, eligible selected-profile Overview **Hour** or **Model** refreshes can request that complete response using a signed resume token and narrow backend changed-bucket reads, including moving 24h windows; the pane never adds totals itself. A backend snapshot fallback, missing/rejected token, old ledger, changed scope, other views and All profiles use full reads. It is not current accounting or a replacement for a read. First use of a view still loads normally; changing profiles, connections or filters cannot reuse another scope's body. Memory is discarded on Desktop quit/plugin reload. If a read fails, the last successful result remains visible with an explicit update-failed warning; do not mistake it for a fresh result. Long-window first reads can still be slow. The filesystem change check is a hint, not a ledger revision; periodic reconciliation remains necessary. CSV export always makes fresh full paginated reads. This is repository-candidate behaviour, **not installed yet**; existing ledgers remain on full reads unless an operator separately approves and performs the offline migration described in the [incremental contract](docs/INCREMENTAL_ANALYTICS_CONTRACT.md). No automatic schema upgrade occurs.

## Understand the numbers

### Quota, tokens and cost are different measurements

Subscription cards show what the provider reports about your account allowance. The usage pages show requests captured by the local recorder. Their dollar values are API-equivalent estimates, not subscription charges or a conversion of quota into money.

A provider can report tokens without offering a quota API or a matching price catalogue. Automatic pricing currently has adapters for OpenAI, OpenRouter, Nous and Ollama. A usable catalogue and an exact model match are still required. Direct Anthropic and Gemini requests can have token counts but no price.

A missing value means unknown, not zero. A subtotal with missing fields may be less than the complete cost.

### Cache writes are calculated

The displayed **Cache writes** value is labelled **Calculated from session reads**. It adds the positive increases between consecutive cache-read counts in a session. It is not a provider-reported write counter and should not be added to the processed-token total.

Provider-reported cache counters remain separate. See [the cache-write calculation](SESSION_CACHE_WRITES.md) if you need to reconcile a report.

### Prices can be saved or retrospective

New requests save a matching price snapshot when one is available. Those saved rates and costs do not change when the catalogue updates.

Previously unpriced requests may receive a separately labelled retrospective estimate using a newer published rate. That estimate can change after a price refresh; it does not rewrite the original record or claim that the newer rate applied at the time.

Use **Refresh provider prices** on Cache & costs to request a catalogue update. If a refresh fails, the plugin keeps the last successful catalogue. [Pricing sources and calculations](PRICING_SOURCES.md) explains rate matching, missing prices and cache savings.

### Compression and context values are estimates

On a compression record, **Start** is the configured threshold, not a measured before-compression token count. **End** is the first subsequent reported input and may include new content. Do not treat the difference as an exact saving.

Skills and context observations only cover events the recorder saw. The plugin does not reconstruct earlier skill loads or recover usage that a provider never reported.

## Use banked Codex resets

The Codex card shows **Auto use**, its checkbox, then a **Resets** badge with the banked balance. The same controls appear on the Codex provider page. They require the Codex CLI on the backend's executable path, a supported app-server response and a ChatGPT subscription credential that the plugin can verify against the selected profile's account. If the balance or account identity cannot be confirmed, spending is disabled.

To spend a reset manually:

1. Select the individual profile and open its Codex card on Subscriptions or the Codex provider page.
2. Read the **Resets** count. Its hint explains why a reset is not available when the count is zero, unknown or not eligible; clicking in those states does nothing.
3. When the count is positive and Codex confirms an eligible exhausted limit, click the **Resets** badge to open the confirmation directly.
4. Read the warning and choose **Confirm use** to spend one reset, or **Cancel** to leave the account unchanged.

A rounded 0% allowance is not enough to enable redemption. The backend checks the account, balance and limit again before spending.

**Auto use spends resets without a confirmation each time.** It is off until you opt in for that profile and account. While the backend is running and the plugin remains enabled, its monitor can redeem a reset when Codex confirms eligible exhaustion, even if the pane is closed. Clear Auto use to turn it off.

On Plus accounts, automatic use can trigger at the five-hour limit, not only the weekly limit. A reset refreshes eligible five-hour and weekly allowances and can change the weekly reset date. Using one early can waste the weekly reset, so leave Auto use off if you want to decide when to spend each reset.

If redemption returns an uncertain result, further attempts are blocked. Check your Codex account before taking further action; the plugin does not automatically retry an uncertain spend.

## Export a report

Select the profile, provider, time window and filters you want, then click **Export request CSV**. The export includes matching request rows across pages, not just the rows currently visible.

All profiles exports include profile provenance. They read profiles and pages separately, so they are not a single simultaneous snapshot. If the plugin detects a change during export, it stops rather than downloading a report with inconsistent pages. Refresh and try again.

Review the file before sharing it. Session identifiers and local paths can identify private work even though the recorder does not store prompt or response bodies.

## Update or roll back

To update an installed copy, run the same preview and apply commands from the source revision you want to install. You do not need to enable the plugin again.

Desktop UI files may hot-reload, but recorder, pricing, API and other backend changes require a restart after active work has finished. The Refresh menu's **Reload analytics backend** option only reloads a limited set of analytics readers. It does not install repository changes or update recorder hooks, and it will report when a restart is required.

To undo an installation, use the receipt path printed by the installer. Preview the rollback first:

```bash
python3 install.py --rollback /path/to/receipt.json
```

Then apply it:

```bash
python3 install.py --rollback /path/to/receipt.json --apply
```

Rollback restores replaced plugin files and removes files introduced by that installation. It leaves the usage ledger intact. It refuses to overwrite files changed since installation. Arrange the required backend and producer restarts separately.

## Troubleshooting

### The dashboard opens, but no usage appears

Check the selected profile and time window first. Then confirm the plugin is enabled in the profile running the agent and that the producing process restarted after installation. Opening analytics does not itself start recording or create missing history.

Normal Hermes request hooks cover OpenAI-compatible Chat Completions, Responses and Anthropic Messages. Additional observers retain native Anthropic and Gemini counts where supported. Custom transports, separate app-server or ACP runtimes, some auxiliary streaming calls and SDK-internal retries may not be fully captured.

### The recorder badge is not green

Click the badge to check recorder status and reconnect the analytics subscription. **Not recording** means no valid producer heartbeat was found. **Limited** means capture or subscription support is degraded; polling may still work. **Disconnected** means a check failed or its status expired.

**Online** confirms recent recorder and connection checks, not complete capture of every request path. See [recorder status and reload controls](ANALYTICS_RELOAD.md) for the detailed meanings.

### A cost or quota is missing

Check whether the provider reports that value. Token recording, subscription quota and pricing have separate coverage. Refresh prices for an unpriced model, but do not expect an unsupported model or provider to gain a price. Missing values are intentionally left unknown.

### A large report is slow

Narrow the time window or select one profile. All profiles reads copy ledger snapshots into temporary storage, so large histories require more disk I/O. There is no automatic retention policy; monitor the size of your ledger if you keep long histories.

## Data and privacy

Each producing Hermes home stores recorded usage in `usage-ledger/events.sqlite3`. Records include token counts and metadata such as session IDs, project attribution, skill observations and pricing evidence. The recorder does not store prompt or response bodies and does not add model inference calls.

Quota checks contact provider services using the existing account integration. Price refreshes contact public catalogue sources without forwarding your prompts, usage records or account credentials. Codex reset controls use the selected account's credential for authenticated app-server operations; redemption changes that account's allowance.

All profiles combines local profiles only. It does not collect ledgers from other machines. Keep backups of any usage history you need to retain.

## Technical reference

For details beyond day-to-day use, read [provider capture limitations](COMPATIBILITY.md), [skills recording](SKILLS_USAGE.md), [project attribution](PROJECT_ATTRIBUTION.md) and [recorder lifecycle](RECORDER_LIFECYCLE.md). Some reference documents retain historical release notes; those sections are not descriptions of the current interface.

The accepted repository candidate is **not installed**. Its gated two-profile handoff (offline database migration, pinned installation, readback and rollback) is in [offline delivery](docs/OFFLINE_DELIVERY.md); it must not be applied while producers are active.

The original plugin documentation is in [UPSTREAM_README.md](UPSTREAM_README.md). [IMPORT_NOTES.md](IMPORT_NOTES.md) records this fork's import history.

### Development checks

Create an isolated test environment rather than installing dependencies into a running Hermes environment:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q --ignore=tests/ui
node --check --input-type=module < desktop/plugin.js
```

Keep test homes and temporary output separate from real Hermes data. The Python tests use fixture databases and disable online pricing. Optional native-provider contract tests need `HERMES_CAPTURE_CORE` set to a read-only Hermes source checkout; otherwise they skip.

Browser tests are standalone scripts under `tests/ui`, not pytest test functions. They use synthetic records in an offline preview. Regenerate `preview.html` with `build_preview.py`, install Chromium through Playwright, set `CHROMIUM_PATH` to its executable and run the relevant test script. These checks do not prove live provider capture or native clipboard behaviour.
