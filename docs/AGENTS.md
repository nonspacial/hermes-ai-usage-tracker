# Project reference DOX

## Purpose

Own project-specific design contracts, investigation notes and reference evidence under `docs/`.

## Ownership

Root owns user-facing README and top-level technical references. This directory owns [Overview interaction contract](OVERVIEW_INTERACTION_CONTRACT.md), [pending performance investigation](PERFORMANCE_INVESTIGATION.md), prior planning/evidence files and images. The Overview contract distinguishes the inspected implementation candidate and implementer-reported checks from independent acceptance. Existing historical evidence must not be relabelled as current run proof.

## Local Contracts

- Read root instructions. Distinguish desired behaviour, code observed in a changing candidate and checks actually completed. Do not present pending feature contracts or synthetic benchmarks as live runtime guarantees.
- Keep requirements durable, concise and source-linked. Do not dump task conversation, secrets, private exports, tokens, account IDs or unredacted ledger contents. Historical documents may retain their clearly labelled evidence; do not rewrite them merely to suggest current feature acceptance.
- When implementation changes, reconcile this directory with actual code and tests, distinguishing implementer-run checks from independent review and installation verification; update root/child indexes only when ownership or indexed documents change.

## Work Guidance

- Overview chart/identity details belong in the local contract; latency hypotheses belong in the investigation note. README remains authoritative for published user-facing behaviour until separately updated and verified.

## Verification

- Check relative links, instruction chain/index and `git diff --check` without running preview builds or live operations for docs-only edits.

## Child DOX Index

None; `images/` is evidence/assets under this owner rather than a separate instruction boundary.
