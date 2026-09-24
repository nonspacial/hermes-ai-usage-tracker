# Offline browser tests DOX

## Purpose

Exercise the packaged pane embedded in generated `preview.html` against synthetic fixtures.

## Ownership

`all_profiles_fixture.js` supplies offline fixture data and API simulation; `test_*.py` scripts exercise browser behaviour. Root owns the generated preview file and build command, desktop owns packaged UI source.

## Local Contracts

- Read root and `tests/AGENTS.md`; read `desktop/AGENTS.md` for UI contracts and [Overview interaction contract](../../docs/OVERVIEW_INTERACTION_CONTRACT.md) for chart/identity tests.
- Preview uses an older React 16 shim without pointer-event support. Gesture tests need the existing native-event/batched adapter; never conclude production is broken solely because shim handlers are absent. Regenerate preview from source plus fixture when in scope, then assert parity.
- Keep tests offline and deterministic: assert actual query traffic and visible/accessible behaviour, not just source regex or CSS selector presence. Use synthetic marker timestamps only in fixtures, do not alter real tests/ledger; aligned viewing inputs must not rewrite the saved marker. Cover the five in-place Overview identity links, qualified Subagents, provider/effective-model event exclusions, half-hour selection versus fine-grained data, and profile/provider persistence. Inspect async query ordering.
- UI scripts are standalone and should not be collected into the backend pytest suite; browser success is not native host or provider-capture proof.

## Work Guidance

- `CHROMIUM_PATH=/home/nope/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome` is a locally observed candidate path, not an install requirement; verify it exists before use. Use `.venv/bin/python` when available.

## Verification

- Run relevant scripts individually, e.g. `CHROMIUM_PATH=... .venv/bin/python tests/ui/test_chart_interactions.py` and `... test_overview_identity_filtering.py`; report exact script exit/result and limitations.

## Child DOX Index

None. Browser scripts and synthetic fixture are owned here.
