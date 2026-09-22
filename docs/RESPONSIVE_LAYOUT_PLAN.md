# Responsive pane audit plan

## Scope

Work inline, without subagents. Use synthetic offline browser fixtures, not the user's screen or live usage data. First establish short-pane failures; do not change installed files or restart Hermes during the audit.

## Coordinate system

Layout must use the plugin pane's available CSS width and height, not monitor resolution. A 3840×2160 screen at a single effective 125% scale corresponds to 3072×1728 logical pixels before desktop/application chrome; at 150% it corresponds to 2560×1440. OS scaling and application zoom may compound. Device pixel ratio alone cannot establish usable pane height.

Include requested physical heights 1080, 1440, 1600 and 2560, plus 2160 (standard UHD/4K). Translate physical sizes through scale, then subtract measured or explicitly simulated shell chrome. Treat those monitor profiles as fixture inputs, not CSS breakpoint names.

## Existing code and evidence gap

- `desktop/plugin.js`, `PaneFrame` measurement near lines 754–803: dense lower views reserve ten normal lines and cap the upper area between half and two-thirds of actual pane height. Sparse views return unused space.
- Existing `tests/ui/test_pane_height.py` uses offline `preview.html`, fixed plugin-root dimensions and synthetic records. It checks containment, scrolling and reading-position retention.
- Its height samples are 260, 420, 900 and 2100 CSS pixels. Passing these checks does not demonstrate that the initial cards, charts and controls are comfortably visible on a 1080p monitor.
- Suspected design pressure, not yet a reproduced defect: retaining a half-height upper allocation on a short pane while metric cards wrap can leave both sections technically contained but unpleasant to use. Chart, legend and control heights need separate measurement.

## Bounded first pass

1. Reuse the existing isolated Playwright runner and preview; block all network access. Do not navigate the user's preview or capture their screen.
2. Start with pane heights 600, 720, 850, 1000, 1200 and 1600 CSS pixels crossed with widths 390, 600, 980 and 1500. These are synthetic available-pane dimensions, not claims about actual host chrome.
3. Exercise All providers, a single provider and Subscriptions. Cover Overview, Requests, Cache & costs, Compressions, Models & tasks and each Skills view. Start with dense data; repeat sparse data and expanded records at the worst short/narrow cases.
4. Record root/upper/lower rectangles, natural content height, chart/legend rectangles, metric wrapping, controls, visible normal row count and each scroll owner. Save JSON and contact-sheet screenshots in ignored test artefacts.
5. Inspect the short/wide and short/narrow screenshots first. Report what fails visually even when containment assertions pass. Select the smallest shared layout correction from that evidence.

## Correction principles

- Respond to actual container width AND height. A narrow split pane inside a wide window must behave like a narrow pane.
- Keep values readable; reduce spacing and chart/legend allocation before reducing text size. Do not shrink the entire UI or hide accounting data.
- Keep navigation and essential filters reachable. Tables/cards scroll internally; no document overflow or clipped inaccessible content.
- Use content-fit thresholds found by the audit, not arbitrary monitor-labelled breakpoints. Short-height chart caps must preserve axes, labels and legend access.
- Preserve natural sizing on sparse pages, desktop tables at adequate width, initially collapsed record cards, expanded inspector state, focus and keyed reading-position retention.
- Revisit the half-height minimum only with demonstrated short-pane evidence; do not blindly change the existing ten-line/two-thirds behaviour across every size.

## Acceptance

- No outer vertical or horizontal overflow; intentional table scrolling remains reachable.
- Initial cards, graphs and controls remain legible at short height, with explicit usable internal scroll regions where all content cannot physically fit.
- Dense lower pages retain useful visible records; sparse pages do not force blank filler.
- Switching tabs, resizing and synthetic arrivals do not jump the reader or close expanded records.
- Run existing pane-height, sparse-allocation, JSON-allocation and accordion browser tests plus new short-pane cases.
- Present representative offline screenshots for visual acceptance before committing/installing a layout change. Technical containment alone is not visual acceptance.

## Current status

Short-height density and allocation correction implemented as an uncommitted candidate; no installation or restart is part of this work.

- Compact spacing and chart heights interpolate with actual pane height, without height-triggered jumps at 700, 850 or 1100 CSS pixels. At wide widths the two-column provider summary remains structural across heights; this deliberately changes the previous tall wide provider grouping to avoid a height-induced reflow. The original tall spacing/chart caps return at 1400 pixels.
- Upper allocation eases in from 500–600 pixels and tapers back to the prior ten-row allocation over 1100–1400 pixels, rather than dropping abruptly at 1101. From 600–1100 the lower reader retains navigation plus at least 120 pixels usable area; tiny panes keep their balanced split. This is a maximum, not forced blank space on sparse upper content.
- `tests/ui/test_height_density.py` checks 69 synthetic width/actual-pane-height samples, including adjacent 500/501, 599/600/601, 699/700/701, 849/850/851, 1099/1100/1101 and 1399/1400/1401 pairs. DOM assertions cover chart visibility, navigation position, upper allocation, lower area and containment. At 980×1100/1101 the upper area measures 829/830 pixels, rather than losing height on growth; at 1500×850 the chart fits initially.
- Exact allocation and sparse tests cover the tapered exception. Offline screenshots and measured JSON reside in ignored `tests/ui/artifacts/`; they are synthetic review evidence, not live-app captures. Narrow panes still need internal upper scrolling. Browser geometry cannot establish live visual acceptance.
