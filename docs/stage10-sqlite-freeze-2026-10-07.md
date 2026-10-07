# Stage 10 SQLite archive/freeze — 2026-10-07

## Status

Stage 10 implementation and DUBUYOGA physical freeze are complete. The same
physical `freeze`/`verify` still has to be executed on `DUBUDESKTOP` before the
two-device Stage 10 rollout can be called physically complete.

PostgreSQL remains the only operational writable database. The legacy SQLite
databases are retained as read-only recovery/reference artifacts and are not an
operational fallback.

## Final contract

- Central `wordpress-blog` PostgreSQL database `topik` remains the operational
  source of truth after the completed Stage 9 PC/Laptop cutover.
- `topik-past-papers/derived/035-I-B.sqlite` is preserved in place. It is not
  renamed, deleted, rebuilt, or modified by Stage 10.
- Its approved SHA-256 remains
  `076826708a93718faa28c3c1ae3e87bbf28aac4bcfaed88359fd2a12b1c0b7e6`.
- The three historical `035-I-B.before-*.sqlite` snapshots are preserved with
  the canonical database as the Stage 10 rollback/reference set.
- No dual-write path exists. PostgreSQL failures fail closed and do not select
  SQLite automatically.
- A rollback must not write new operational state into the frozen SQLite files.
  PostgreSQL recovery/reconciliation uses retained PostgreSQL backups and
  operational records instead.
- Explicit non-canonical SQLite databases remain supported only for tests and
  offline legacy fixtures.

## DUBUYOGA archive evidence

The archive is stored outside Git under:

`C:\Projects\TopikDatabase-runtime\sqlite-archive`

The runtime tree is ACL-restricted to `SYSTEM` and `DUBUYOGA\gip4k`. Stage 10
stores exact copies under `files\` plus `stage10-manifest.json`. The manifest
records SHA-256, byte size, SQLite `PRAGMA integrity_check`, policy state and
the operational database identity. The Windows read-only attribute is an
accidental-write guard, not a cryptographic or administrator-proof immutability
boundary; SHA verification and code-level guards provide the fail-closed check.

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `035-I-B.sqlite` | 1,994,752 | `076826708a93718faa28c3c1ae3e87bbf28aac4bcfaed88359fd2a12b1c0b7e6` |
| `035-I-B.before-audio-20260921T031335Z-22e739.sqlite` | 1,966,080 | `5c5fb115dce37f9521ae4130627a446c8e1279f33cf2e240e3b5dae98912b10c` |
| `035-I-B.before-extraction-correction-20260921T020017Z-c551a4.sqlite` | 1,110,016 | `f2afe62b5cb407b0f361fb34367008d1f0541beda196ad98a106f6d3dbc295ce` |
| `035-I-B.before-word-spacing-20260921T022415Z-8ee035.sqlite` | 1,933,312 | `98d2e186814a67b9ef48c5a0fa66bb7f40a223a4ca8ffd0084af3eba5c857251` |

All four originals and all four archive copies pass `integrity_check`, and the
source/copy hashes and byte sizes match. `review-ui-validation-2026-10-05.sqlite`
is a validation fixture rather than part of the historical rollback chain and
is intentionally not promoted into the Stage 10 archive.

## Operational write-path changes

Stage 10 removes both remaining implicit fallbacks:

- `ReviewStore()` requires `TOPIK_DATABASE_URL` unless the caller explicitly
  supplies a non-canonical SQLite fixture path.
- `ai_audit_35` requires `TOPIK_DATABASE_URL` unless `--db` or an API argument
  explicitly names an offline legacy SQLite fixture.

The shared SQLite archive guard rejects the canonical DB before any project-side
mutation. It also checks resolved aliases/file identity so hard-link/symlink
aliases cannot bypass the canonical path check, and writable raw
`sqlite3.Connection` objects are checked through `PRAGMA database_list`.
Covered legacy mutation paths include reviewer writes, AI-audit writes,
extraction correction, audio candidate registration, transcript spacing
upgrade, and recreation of a missing canonical pilot DB.

## Recovery boundary

The primary database recovery artifact remains the Stage 9 PostgreSQL dump:

- `topik-stage9-physical-20261007.dump`
- SHA-256 `f5dd431160fe1d9fd3325c657e039d841e639bb68d8f5b276f255ff15cde5d6a`

That dump was already restored and parity-checked during Stage 9. The frozen
SQLite set is secondary historical source evidence, not a live rollback DB. If
PostgreSQL recovery is required, restore/reconcile PostgreSQL and resume only
after the central database is healthy. Do not unset `TOPIK_DATABASE_URL` and
resume writable SQLite review.

## Repeatable commands

On each physical device after the Stage 10 code is present:

```powershell
py -3 scripts/stage10_sqlite_freeze.py freeze
py -3 scripts/stage10_sqlite_freeze.py verify
```

Successful verification reports `stage=10`, `status=ok`,
`sqlite_operational_writes=false`, four frozen source files and four matching
archive copies. `freeze` publishes a `status=frozen` manifest only after all
source and archive SQLite files pass the final read-only check.

Immediately before the DUBUYOGA freeze, read-only SQL through the Stage 9
SSH/TLS path confirmed database `topik`, role `topik_app`, and 70 questions.
Stage 10 did not mutate production PostgreSQL rows.

## Remaining physical rollout gate

`DUBUDESKTOP` was not reachable from the DUBUYOGA CoS session over an executable
remote path or SMB share at the time of this Stage 10 run. Therefore repository
implementation and DUBUYOGA archival are complete, but physical two-device
rollout remains gated on running the same `freeze` then `verify` commands on
`DUBUDESKTOP` and recording its successful manifest/hash result.
