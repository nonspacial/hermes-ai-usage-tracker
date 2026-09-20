# AI Usage Tracker — native request ledger

This fork extends [lvabarajithan/hermes-ai-usage-tracker](https://github.com/lvabarajithan/hermes-ai-usage-tracker) with the **2.0.0-test.17** request ledger. The original MIT licence and quota components are retained.

## Behaviour

- **Subscriptions** opens first with the original quota cards, profile picker, refresh, status-bar provider selection and hide/unhide controls.
- All providers and individual providers have nested Overview, Requests, Cache & costs, Compressions and Models & tasks pages. Individual provider pages retain their quota card above analytics.
- Native request hooks and guarded runtime adapters record usage in each producing Hermes home's `usage-ledger/events.sqlite3`. The UI refreshes persisted events; it does not reconstruct usage from cumulative session counters.
- Displayed **Cache writes** is the sum of positive consecutive cache-read differences within each session stream. Its caption is **Calculated from session reads**. Provider counters and saved costs remain separate and unchanged; calculated writes are not additional processed tokens.
- Request JSON, CSV, attribution, compression correlations and saved price snapshots preserve missing-versus-zero distinctions.

Read [SESSION_CACHE_WRITES.md](SESSION_CACHE_WRITES.md) for the current calculation contract and [COMPATIBILITY.md](COMPATIBILITY.md) for capture limitations. Historical sections in imported documents describe earlier releases, not fresh validation. [UPSTREAM_README.md](UPSTREAM_README.md) describes the original quota-only plugin.

## Repository layout

The plugin now lives at the repository root, matching the upstream installation layout:

```text
plugin.yaml                 Plugin identity and hook declarations
__init__.py / bootstrap.py   Recorder registration and runtime loading
desktop/plugin.js           Desktop quota and analytics UI
dashboard/plugin_api.py     Original quota routes plus ledger API
ledger_runtime/             Capture, accounting, attribution, storage and pricing
tests/                      Offline Python and browser checks
install.py                  Explicit-home installer with receipts and rollback
doctor.py                   Offline Hermes source-signature inspection
cache_write_report.py       Explicit-home cache-evidence report
build_preview.py            Refresh the synthetic preview from desktop/plugin.js
preview.html                Synthetic offline SDK/React harness
```

The installer copies only named plugin files and runtime directories, not repository history, tests or private archives. The retained `catalog/` files describe the original upstream release and are **not a release declaration for this fork**. Do not run the upstream publication script to publish this build.

## Development checks

Use an isolated environment; do not install test dependencies into a running Hermes environment:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q --ignore=tests/ui
node --check --input-type=module < desktop/plugin.js
```

Browser suites are executable scripts, not pytest test functions. Install Chromium with Playwright, set `CHROMIUM_PATH` to its executable, regenerate `preview.html` with `build_preview.py`, then run the individual `tests/ui/test_*.py` scripts. They use synthetic records and an offline SDK; passing them does not establish live capture coverage or native clipboard behaviour.

Tests set `HERMES_USAGE_PRICING_OFFLINE=1` and use temporary fixture databases. Keep `HOME`, `HERMES_HOME` and temporary output isolated from real Hermes data when running verification.

## Installation is separate from source changes

Against an explicitly chosen, existing real Hermes home:

```bash
python3 install.py --home /actual/hermes/home
# Only when installation is intended:
python3 install.py --home /actual/hermes/home --apply
```

The first command is plan-only. Applying creates file backups and a rollback receipt, but does not enable the plugin, restart producers, alter credentials or remove ledger data. Loading changed Python code requires a separately arranged producer/backend reload. Do not disrupt active workloads. Symlink destinations are refused; identify the real installation before applying.

Quotas and background pricing can make network calls. The recorder does not add inference calls, collect prompt/response text or export credentials. Local paths and session IDs are private metadata; review exports before sharing.

## Local import provenance

The fork and upstream `main` both resolved to `77bdf112117d6d8811477d8837d3cb9a3a9d99d9` during import. All original-source entries in `UPSTREAM_SOURCE_MANIFEST.json` matched that checkout. `PRESERVED_UPSTREAM.json` retains component/probe regression fingerprints.

On the originating workstation, all pre-existing project contents were preserved under Git-ignored `.local-history/`, including the complete handoff, original test.17 package, archives and historical usage exports. They are not part of the publishable repository. Runtime/UI source is imported unchanged; development paths and installer packaging were adapted to this root layout. No installation or publication is implied by this import.
