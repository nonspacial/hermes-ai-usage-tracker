# DOX — AI usage + for Hermes Desktop

This repository's DOX hierarchy is adapted from agent0ai/dox, pinned to [`AGENTS.md` at commit `765ae4ac02cc884eefcd41a3d0f71941721adb89`](https://github.com/agent0ai/dox/blob/765ae4ac02cc884eefcd41a3d0f71941721adb89/AGENTS.md); [upstream bootstrap README](https://github.com/agent0ai/dox/blob/765ae4ac02cc884eefcd41a3d0f71941721adb89/README.md). Upstream supplies the AGENTS.md contract and indexing instructions, not a package or script. The project-specific contracts below are local adaptations, not upstream claims.

## Purpose

Develop the `ai-usage-tracker` Hermes Desktop plugin, displayed as **AI usage +**. Preserve recorded usage/accounting, subscription cards and profile boundaries while adding analytics UI. See [README.md](README.md) for user-facing behaviour and installation.

## Ownership

The root owns plugin integration (`bootstrap.py`, `install.py`, `build_preview.py`, `plugin.yaml`, `preview.html`, root technical references) and cross-area policy. Children own local implementation details; consult their indexes. `preview.html` is generated from `desktop/plugin.js` and `tests/ui/all_profiles_fixture.js`, not an independent UI implementation. Existing historical reference docs remain historical unless explicitly updated.

## Local Contracts

- Every `AGENTS.md` is a binding work contract for its subtree. Before editing: read this root, identify paths, read every `AGENTS.md` along each path and indexed scoped child; the nearest owns local details, but cannot weaken DOX or parent safety rules. Do not substitute remembered instructions.
- Every meaningful change needs a DOX pass: update nearest owning doc for changed purpose, ownership, structure, workflow, inputs/outputs, permissions, side effects or durable user preferences; update parents/child indexes and affected children; remove contradictions. Small non-contract edits may leave docs unchanged after an explicit check. Record current contracts rather than progress diaries.
- Use **AI usage +** for the displayed product and `ai-usage-tracker` for its installation identifier. Use Hermes UI theme tokens instead of hard-coded host colours; visual interaction details live in [Overview interaction contract](docs/OVERVIEW_INTERACTION_CONTRACT.md).
- Preserve per-profile storage/accounting and all-profile read-only aggregation: do not treat missing ledgers as zero; qualify cross-profile identities, preserve sentinel isolation, and never silently broaden a scoped query. Saved measurement markers retain their actual timestamps; display/input bounds may align to the half-hour grid, but server reads must intersect the marker's factual bounds. Do not fabricate test timestamps or mutate live account/ledger data to demonstrate a view.
- Isolate synthetic fixtures and tests from real Hermes homes/accounts, secrets and exports. No installation, enablement, restart, authenticated provider operation, reset spend, live ledger mutation, push or environment-wide dependency change is implied by a local code/doc edit. Explicit user approval and verification govern such delivery actions.
- Concurrent work must have exclusive file ownership. Documentation and implementation can proceed independently; reconcile DOX against the final implementation and test results before closeout. Do not present candidate behaviour as verified until exercised.

## Work Guidance

- Read relevant existing technical references and code before describing implemented behaviour. Label proposed contracts or pending investigations distinctly from proven runtime state.
- Scope edits and checks locally; run fixture-based behavioural verification, not source-text regex as the sole proof. Independently review candidate changes before delivery, then make a normal local commit for the accepted candidate.
- For this delivery, the user has authorised `install.py` installation to both default and `infra` Hermes profiles after acceptance. Preserve both backup/rollback receipts and verify installed bytes against the exact committed source hashes. This does **not** authorise a push, restart, authenticated provider operation or live ledger/account mutation; repository-only documentation work must not install anything.
- Read the nearest owner before changing source, fixture, catalogue or docs. Keep root references concise and link to local detail instead of copying it.

## Verification

- Isolated environment: `.venv/bin/python -m pytest -q -p no:cacheprovider tests --ignore=tests/ui` (the UI scripts are standalone and duplicate a backend test module name). `.venv/bin/python build_preview.py` regenerates preview **and writes it**; only run when preview edits are in scope. `node --check --input-type=module < desktop/plugin.js` checks module syntax.
- Standalone browser scripts accept `CHROMIUM_PATH` (locally `/home/nope/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome` when present). Tests require synthetic fixtures; success does not prove live Hermes/provider operation. `requirements-dev.txt` declares PyYAML for the activation-policy gate. Use existing test tools and report exact results rather than inventing checks.

## Child DOX Index

- [desktop/AGENTS.md](desktop/AGENTS.md): desktop pane, theme, chart, state and interactions.
- [dashboard/AGENTS.md](dashboard/AGENTS.md): host dashboard manifest and API/profile boundary.
- [ledger_runtime/AGENTS.md](ledger_runtime/AGENTS.md): recorder, storage, analytics, aggregation and read safety.
- [tests/AGENTS.md](tests/AGENTS.md): isolated fixtures, backend verification, test ownership; indexes `tests/ui`.
- [docs/AGENTS.md](docs/AGENTS.md): project-owned design contracts (including lower record paging), investigation evidence, historical technical plans.
- [catalog/AGENTS.md](catalog/AGENTS.md): plugin catalogue metadata and publication helpers.

Root owns remaining top-level files, including installation, preview generation, release references and licensing. No separate child instruction is needed for `tests/fixtures` or `docs/images`: their nearest parent owns them.
