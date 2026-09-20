# Test.17 fork import

## Git baseline and ownership

- Origin: `https://github.com/nonspacial/hermes-ai-usage-tracker.git`
- Upstream: `https://github.com/lvabarajithan/hermes-ai-usage-tracker.git`
- Both fetched `main` refs and local `HEAD`: `77bdf112117d6d8811477d8837d3cb9a3a9d99d9`.
- All eight original backend-package source entries in `UPSTREAM_SOURCE_MANIFEST.json` match that baseline, including the original Desktop entry. No intervening fork/upstream source divergence required reconciliation.
- At completion of import verification, the changes were unstaged and uncommitted on local `main`. That import operation performed no commits, pushes, release/catalog publication, installation or service reload. Subsequent local commits are recorded in Git history.

## Layout conversion

The plugin's contents moved from the delivery's `ai-usage-tracker/` wrapper to the repository root. All 19 runtime/source/manifest files (`.py`, `.js`, `.json`, `.yaml`) are byte-identical to test.17. The MIT licence is unchanged from upstream.

Development-only adaptations:

- Test bootstrap/backend paths and preview builder now target the root layout.
- Installer uses explicit root plugin files plus `dashboard/`, `desktop/` and `ledger_runtime/`; it does not copy the entire repository.
- Added a regression test for installer exclusions.
- Added isolated development dependency pins and current root README.
- Preserved upstream `catalog/` unchanged as historical upstream publication material, not current fork metadata.

All 184 pre-existing files were relocated under `.local-history/` and verified byte-for-byte immediately after relocation. This directory is Git-ignored and retains the complete handoff, original test.17 package, ZIPs, historical exports and original export-kit README. Do not stage or publish it. The live installed plugins and usage databases remain separate and untouched.

## Executed verification

Checks used a separate Python 3.13.13 virtual environment and disposable fixture state. Bubblewrap blocked external networking, hid the real home, hid `.local-history/` and `.git/`, and provided an isolated HOME/HERMES_HOME/TMPDIR. No live ledger or provider credentials were available to the checks.

| Check | Result |
|---|---|
| Python suite, excluding executable UI scripts | 210 passed (209 imported + one installer-layout regression) |
| JavaScript ESM syntax | Passed |
| Preview regeneration from imported Desktop source | Passed |
| Offline Chromium scripts | All 12 passed |
| Runtime/manifest byte comparison against test.17 | All 19 identical |
| Original licence comparison | Identical |
| `git diff --check` | Passed |
| Private archive, environment and generated result exclusions | Confirmed with `git check-ignore` |

The Python suite emitted two dependency deprecation warnings: Starlette's httpx TestClient integration and the AnyIO BlockingPortal alias. No runtime dependency upgrade or application fix was made for these warnings.

Local, ignored evidence: `tests/results/import-verification.json`, per-check text logs in `tests/results/`, and screenshots in `tests/ui/artifacts/`.

Browser tests exercise a synthetic SDK and records, not the authenticated Hermes shell. This does not establish live producer reload, every provider transport, natural compression capture, public price freshness, native OS clipboard behaviour or production-scale performance. The inherited recorder heartbeat version string still says `2.0.0-test.14`; it was intentionally not changed during this source import.
