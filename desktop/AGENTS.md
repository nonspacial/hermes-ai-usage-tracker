# Desktop pane DOX

## Purpose

Own the `desktop/plugin.js` Hermes pane, chart, navigation, query state and theme integration.

## Ownership

This subtree owns packaged UI source. Root owns the generated `preview.html` and its build script; tests/ui owns offline browser fixtures. Change source first, regenerate preview when authorised, then test parity.

## Local Contracts

- Read root instructions and [the Overview interaction contract](../docs/OVERVIEW_INTERACTION_CONTRACT.md) before modifying graph/filters. That document distinguishes requirements from verified behaviour; validate implementation, not just wording.
- Keep display name **AI usage +** and use host theme tokens throughout. Plot focus/selection styling, graph-only border and data-versus-selection resolution are specified in the linked contract.
- Filter graph, totals and records with one semantic scope, retaining selected profile/provider and outer period. Keep saved per-profile view choices separate (including provider choice and Overview filters); prevent stale asynchronous responses from overwriting a newer profile/view. All profiles remains read-only with qualified IDs and backend support checks.
- Keep identity-link activation separate from narrow-record disclosure. The five Overview breakdown identities filter in place; Requests retains its drill navigation. A Subagents identity selects the qualified subagent plus subagent agent scope, not merely the generic Subagents totals shortcut. Simple hover must not shade the whole cell. Do not fetch for a single graph click, hover or keyboard inspection; fetch only on a committed valid scope change.
- Include the committed tab and Overview group in the `/ledger` projection query and coordinator identity. Every newly committed selection (including a rapid return to a previously visited view) starts with an explicit pending body; never stamp an old response with its new key or show previous group rows under the new label. Retain a body only during a same-view background refresh; bridge only shared header data under the same semantic filter/window scope. Skills has a separate history query; CSV removes `view`/`group` to fetch frozen-window full pages with sequence, coverage and catalog guards. The scoped Refresh/busy owner remains unchanged; no hover prefetch or cross-read result cache.
- The shared Codex reset header on Subscriptions and the provider card orders `Auto use` text, checkbox, then count badge before the card close button. An eligible positive badge opens the existing guarded confirmation directly; zero, unknown and ineligible counts open nothing. No intermediate balance popover or Refresh balance control. Keep account-bound backend redemption checks, opt-in and cancel/focus behavior intact.

## Work Guidance

- `preview.html` embeds this source and a synthetic fixture; it cannot independently establish native host behaviour. The old preview React 16 shim lacks pointer events; use the existing native-event/batched adapter for gesture tests rather than treating unavailable shim handlers as production bugs.
- Preserve tooltip reachability by keyboard and during drag, click-away focus clearing, correct selection highlights and host-token live updates.

## Verification

- `node --check --input-type=module < desktop/plugin.js`.
- When preview regeneration is in scope: `.venv/bin/python build_preview.py`, then run relevant standalone `tests/ui/test_chart_interactions.py`, `test_overview_identity_filtering.py`, and view-persistence browser scripts with `CHROMIUM_PATH` set. These exercise synthetic data; they do not prove live host capture or native clipboard.

## Child DOX Index

None. Root owns preview generation; [tests/ui/AGENTS.md](../tests/ui/AGENTS.md) owns browser tests.
