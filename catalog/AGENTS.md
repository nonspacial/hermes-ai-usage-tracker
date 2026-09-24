# Plugin catalogue DOX

## Purpose

Own the publication metadata and helper materials for the plugin catalogue.

## Ownership

`ai-usage-tracker.yaml` describes catalogue installation/metadata, `PR-body.md` is publication copy, and `open-pr.sh` is a submission helper. Root owns local `install.py` and all deployment/rollback policy; this subtree does not install the local plugin by itself.

## Local Contracts

- Read root instructions. Keep plugin installation identifier `ai-usage-tracker` distinct from display label **AI usage +**; verify metadata against the actual manifest/revision before publication.
- Do not execute publication helpers, push, submit a PR or install a profile as a side effect of documenting changes. Review external-facing copy for unsupported feature claims and private paths/credentials.

## Work Guidance

- Treat catalogue packaging and local developer installations as separate delivery routes. Changes to release copy should follow verified implementation, not candidate contracts.

## Verification

- Inspect YAML/manifest consistency and run non-mutating checks only. No automated catalogue publication validation is asserted here.

## Child DOX Index

None; the three catalogue files remain under this owner.
