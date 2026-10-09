# TOPIK 36-I-B punctuation correction migration (2026-10-09, v4 -> v5)

The source PDF, transcript PDF, MP3, historical 35th data, and the original
36th `raw_question_text` remain frozen. This operation updates existing
36th PostgreSQL display text after checking an exact, reproducible v4 -> v5
delta. It never reimports the exam and does not change review decisions.

## Source and delta contract

* Immutable `staging-v4.json` raw SHA-256:
  `4c02c999b0fcbdaad48aaf9af6d0f5c754ca424f8a37885466a6704d90403684`.
* Validated `staging-v5.json` raw SHA-256:
  `af72d0c340fe7b833b63dc0eb07652538e3dd292668a6e4986b4247792e9e095`.
* `extraction_version=pdf-first-36-v5`,
  `punctuation_rule_version=punctuation-space-v2`.
* Exactly 54 changed cells and 138 inserted spaces: 15 question stems,
  8 choices, 9 group passages, 22 listening transcripts. The additional
  v2 space is the source-verified R51-52 `있습니다.120전화` boundary.

`scripts/migrate_36_punctuation.py` pins the v4 raw bytes; validates the
v4 shape/provenance with the *historical* punctuation exception; requires the
full modern v5 importer quality gate; and rejects differences to answer keys,
images, identifiers, question metadata, raw text, source references, or any
other field. Every v5 text change must match the exact shared versioned
punctuation normalizer.

## Operational procedure

The default command makes **no PostgreSQL writes**. It compares every
36th question stem, raw text, choice, group instruction/passage, and transcript
against exact frozen v4 values; even an unrelated human edit blocks migration:

```powershell
py -3 scripts/migrate_36_punctuation.py
```

It also checks the approved 35th source snapshot SHA-256
`631c4fb71784439961956b582d0e66847eb862e2c2370f49c89d5ca520057c35`,
36th review inventory (70 pending questions, 30 pending transcripts, no human
review history), and migration marker. The JSON report supplies the full
before/after field evidence. When complete, `--apply` is the separate
explicit write command:

```powershell
py -3 scripts/migrate_36_punctuation.py --apply
```

**Never apply to the live `topik` database without a fresh independent
backup, a verified restore, and a successful isolated rehearsal first.**
The script uses one PostgreSQL repeatable-read transaction, locks the
relevant source/review/metadata tables, and executes compare-and-swap updates
requiring the v4 value on every row. It checks all v5 values, all human review
statuses/history, and the 35th source snapshot before commit. Any error
rolls back the entire transaction. One versioned `import_metadata` row
records the v4/v5 hashes and canonical changes fingerprint, and an exact
repeat returns `already_applied` without duplicate updates/history.

## 2026-10-09 backup and isolated restore

A fresh `pg_dump -Fc` was captured using PostgreSQL 17.11 in custom format,
copied outside Git to:

```text
C:/Projects/TopikDatabase-runtime/backups/punctuation-20261009-w2/topik-pre36.dump
```

Remote and local SHA-256 match:
`7ad4c6bb80baaca55f5abe52af4dc9f819d5a01117ad377c3c6556fdb56eb7a3`.

The following commands captured the backup and loaded the isolated restore.
The live `topik` database was the source of a consistent read-only custom
dump, never the target of any restoration:

```powershell
ssh bloguito 'mkdir -m 700 /home/ubuntu/.topik36-punctuation-rehearsal-20261009-w2'
ssh bloguito 'sudo -n -u postgres pg_dump --format=custom --dbname=topik > /home/ubuntu/.topik36-punctuation-rehearsal-20261009-w2/topik-pre36.dump'
ssh bloguito 'sha256sum /home/ubuntu/.topik36-punctuation-rehearsal-20261009-w2/topik-pre36.dump'
scp bloguito:/home/ubuntu/.topik36-punctuation-rehearsal-20261009-w2/topik-pre36.dump C:/Projects/TopikDatabase-runtime/backups/punctuation-20261009-w2/topik-pre36.dump
Get-FileHash C:/Projects/TopikDatabase-runtime/backups/punctuation-20261009-w2/topik-pre36.dump -Algorithm SHA256
ssh bloguito 'sudo -n -u postgres createdb --owner=topik_app --template=template0 topik_punctuation_rehearsal_20261009_w2'
ssh bloguito 'sudo -n -u postgres pg_restore --role=topik_app --no-owner --no-acl --exit-on-error --dbname=topik_punctuation_rehearsal_20261009_w2 < /home/ubuntu/.topik36-punctuation-rehearsal-20261009-w2/topik-pre36.dump'
```

The isolated restore database is named
`topik_punctuation_rehearsal_20261009_w2`; it was created with the
`topik_app` owner and populated with `pg_restore --role=topik_app --no-owner
--no-acl --exit-on-error`, then queried over its own local SSH forward
(`127.0.0.1:55436`). This unique test DB must be removed after verification.
The central production `topik` database is never a write target for these
rehearsal tests.

The Windows backup file's inherited ACL was inspected: the only access
entries are `NT AUTHORITY\SYSTEM` and the logged-in machine owner, each
with FullControl. No credentials were printed or included in the report.

## Isolated PostgreSQL verification results

The clone was confirmed to be `topik_punctuation_rehearsal_20261009_w2`
under role `topik_app`. The test suite then exercised real PostgreSQL
transactions against that clone, with the following confirmed results:

1. **Dry run:** `status=pending`; all 36th editable and archival text
   exactly matched v4; 54 cells/138 spaces queued; frozen 35th source SHA
   unchanged.
2. **Forced fault:** raised an exception after three row-level updates, before
   the fourth; PostgreSQL rollback verified **every 36th source text field**
   still matched v4, with no migration marker left behind.
3. **Normal apply:** committed all 54 changes, exactly 138 spaces. The 35th
   source SHA remained the approved value.
4. **Repeat apply:** returned `already_applied`; no duplicate marker/history
   was generated.
5. **Independent final readback:** the complete 36th display text exactly
   matched v5, 36th human-review state hash was unchanged, and the migration
   metadata key had exactly one row.

The script enforces 70/70 `needs_manual_review` question statuses, 30/30
`needs_manual_review` transcript statuses, and zero prior 36th human-review
history. A SQL update never writes those status or review-record tables.
Rollback testing was performed only on the isolated clone. **The live
`topik` database had no correction transaction in this worker's run.**

The original 35th canonical snapshot function issued four SQL lookups per
question. The migration now has a scoped tuple-row cache that batches those
lookups into four SQL reads without altering the canonical snapshot serializer
or digest. An independent production **read-only** verification returned the
same frozen 35th SHA in **2.33 seconds**. This is necessary to avoid holding
the review table locks during hundreds of remote round trips.
The optimized code was also tested against the restored, already-migrated DB:
`already_applied` with no new marker/history, returning in **7.4 seconds**.

```powershell
py -3 -m unittest tests.test_migrate_36_punctuation -q
# 16 tests passed
```

After the complete test, `topik_punctuation_rehearsal_20261009_w2` was
dropped; PostgreSQL's database list again contained only `topik` and the
system databases. The single-purpose remote backup scratch directory and
its local SSH tunnel on port 55436 were removed, leaving the verified backup
copy outside Git on the client machine.

## Subsequent live verification and reviewer concurrency safeguard

On 2026-10-09, a fresh **read-only** preflight against the actual `topik`
database returned `already_applied`. The exact v4/v5 migration marker was
present and all 36th display-text rows matched frozen v5; the same query
verified 54 changed cells, 138 inserted spaces, the frozen 35th snapshot
`631c4fb71784439961956b582d0e66847eb862e2c2370f49c89d5ca520057c35`,
and unchanged 36th review-state fingerprint
`e1307ac0f3c65d8357d6b4195236773a54be9fa35db8a3c707143b2a57eaf0b1`.
This check **did not execute `--apply`**; it establishes present production
state, not the identity or time of the earlier successful writer.

An independent code review found that the migration's deliberate preservation
of human review history could allow a stale, pre-migration reviewer form to
submit the old v4 text. The reviewer now treats the exact 36th migration marker
as one additional optimistic version increment. This increment is reflected
consistently in detail, bundle, lightweight list, save verification and save
response. PostgreSQL detail reads begin in a repeatable-read, read-only
transaction, so the version token and text are drawn from one snapshot even
if the migration commits between queries. The 35th review version contract
remains unchanged. A previously opened v4 edit now receives HTTP 409 on
save instead of silently restoring old text.

The import validator now accepts only `pdf-first-36-v5` with the explicit v2
punctuation rule for normal imports. The read-only historical v4 exception
requires the canonical frozen v4 SHA-256. Synthetic alternate-version metadata
no longer passes validation.

**Operational UI rollout:** Existing Python reviewer processes load their
source code at startup and must be restarted to enforce the new version gate.
Any previously open editor tabs must be reloaded after that restart, preserving
the user's unsaved edits separately if needed. This rollout concerns the
local reviewer process, not any PostgreSQL re-migration.

The reviewer on this machine was restarted on its original loopback port
`127.0.0.1:18736` with the revised code. Live read-only HTTP smoke checks
returned 36th question version 1, 70 pending 36th questions, preserved
`raw_question_text` plus a separate normalized display value, and a
still-verified 35th question. The temporary smoke-test reviewer on port
`18737` was stopped. A separate physical machine running an older reviewer
must update/restart its own local process before accepting 36th review edits.

Verification after both race and importer fixes: 59 targeted tests passed
(1 skipped), complete suite 231 tests passed (1 skipped); `git diff --check`
passed. The full suite's initial PostgreSQL-clip fixture failures were
resolved by limiting the new repeatable-read detail transaction to exam
`036-I-B`, preserving the historical 35th PostgreSQL reviewer behavior.
