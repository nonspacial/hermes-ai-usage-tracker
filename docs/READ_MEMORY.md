# Read-side memory: key selection before detail hydration

All-profiles request pagination now merges `(started, original request ID, profile ID)`
keys in descending order, then hydrates and qualifies only the selected page.
The HTTP offset API and its 2000-row maximum are unchanged. Key selection and
hydration share one analytics generation and the same copied source roots.
A late profile failure removes that profile and recomputes/refills the page from
surviving copies, including corrected totals and coverage.

The shared predicate covers both queries; private detail-ID membership cannot
escape it. `None` retains normal Store pagination; an empty selection returns no
request details but still computes the full report. Summaries, groups, trends,
cache history and predecessor calculations remain full-window/per-profile.
Cache-write and legacy JSON projections now stream into SQLite TEMP tables,
rather than retaining full-payload `fetchall()` lists. Decimal/NULL accounting,
ownership classification and source databases are unchanged.

## Synthetic measurements

Original fixtures: 10,000 requests per profile, 8192-byte stored JSON per request,
200-row response; four profiles for All profiles. Each candidate/control case
ran once in a fresh process, sequentially, without tracemalloc. Values below are
sampled process RSS (MiB), not whole-host memory or a promised application cap.

| Read | Historical `0ce21955` peak / iteration seconds | Matched parent `a430e1f` peak / iteration seconds | Candidate peak / iteration seconds |
|---|---:|---:|---:|
| Selected, offset 0 | 120.41 / 10.26 | 119.93 / 13.95 | 46.11 / 13.72 |
| All profiles, offset 0 | 129.93 / 39.30 | 130.48 / 56.03 | 51.76 / 55.73 |
| All profiles, offset 30000 | 697.95 / 46.41–46.66 | 704.82 / 66.91 | 57.73 / 56.62 |

The deep-page peak is 91.81% below the matched parent (91.73% below the historical
baseline). Historical runtime is not an immediate-parent control: lifecycle and
accounting changes intervened. No latency-distribution claim follows from these
single candidate/control samples. The historical deep case completed two reads
before its cumulative CPU limit killed a later iteration; its peak covers that
campaign, unlike the fresh single-read control.

Iteration timings include response deletion and garbage collection, not solely
the read call.

| Candidate read | Imported RSS | Peak increment | Response held | After deletion + GC |
|---|---:|---:|---:|---:|
| Selected | 33.71 | 12.40 | 42.07 | 42.07 |
| All, first page | 33.50 | 18.26 | 47.79 | 43.79 |
| All, deep page | 33.66 | 24.07 | 51.70 | 46.78 |

Scratch is separate: All profiles still copied **335.36 MiB** of source snapshots;
deleted SQLite TEMP descriptors peaked at **250.85 MiB** (matched parent
250.16 MiB). Selected-read TEMP descriptors peaked at 250.16 MiB, with about
0.10 MiB visible temporary validation files. These separate maxima must not be
summed as an observed simultaneous peak. Fixture file hashes were unchanged and
scratch temporary directories were empty after each campaign.

All benchmark children used a read-only source mount, isolated HOME/HERMES_HOME,
a network namespace, 1400 MiB RLIMIT_AS, 110 CPU seconds, a 120-second alarm and
a 1 GiB RSS supervisor stop. No stop fired. These are verifier safeguards, not
product truncation or memory limits.

Evidence under `/home/nope/.hermes/profiles/infra/cache/scratch/`:

- `plugin-memory-0ce21955/verified-summary.json`: historical evidence.
- `plugin-memory-a430e1f-control/`: committed-parent source and matched measurements.
- `plugin-memory-a430e1f-candidate/`: frozen committed-parent archive plus explicit
  worktree overlay, original copied fixtures, raw JSONL/results, fixture checks
  and `verified-summary.json`. No commit was created. Source-manifest SHA-256:
  `96df4f6d1859b7780de9f70883bcf26a39d4229933d8cdb25f520402d10f62ee`.
  Runtime source hashes were checked against the worktree after execution;
  additional tests and this document were added after the benchmark freeze.

## Verification and remaining limits

Isolated Python backend suite: **379 passed, 18 skipped, 4 deselected** in 201.62s.
Native-provider contract cases were skipped because `HERMES_CAPTURE_CORE` was
not supplied to this isolated runner; installer/rollback cases were excluded to
respect the no-install scope. UI suites
were not run by the implementer. Subsequent coordinator checks passed all four
excluded installer/rollback tests in disposable storage and four browser suites:
All profiles, export, sentinel identities, and abandoned-request lifecycle.
Independent review passed 43 hydration/reload tests with no actionable findings.
Two existing Starlette/AnyIO deprecation warnings remain.

Command: `python /home/nope/.hermes/profiles/infra/cache/scratch/run_abandoned_checks.py tests --ignore=tests/ui -k 'not installer and not rollback_refuses_changed_file'`.
Log: `abandoned-check-julxkqzm/output.txt` beneath that scratch directory.

New tests cover the structural hydration bound, equal-time/colliding IDs, deep
and empty pages, all scope predicates, 2000-row membership, full summary parity,
late-failure refill, source changes between selection and hydration, and the
protected key-method reload boundary. Existing lifecycle, accounting, legacy,
cache, aggregate, read-only, API and export-pagination tests also passed.

This bounds retained request payloads, not total memory: scalar keys still grow
with per-profile prefixes; summary/group cardinality, cache calculations,
compression auxiliary details and SQLite scratch remain data-dependent. Late
failures may repeat survivor reports. There is no hard memory cap, truncation,
new live-data cache, schema change or UI change. Changes to the aggregate/runtime
or protected Store key method require the normal backend activation/restart
boundary; nothing was installed, restarted or committed by this work.
