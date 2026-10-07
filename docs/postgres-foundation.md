# PostgreSQL migration and central-state foundation (stages 1-7)

This foundation prepares the existing 35-I review data for a central standard
PostgreSQL database while leaving original PDF/audio files local on each PC.
Stages 5-7 add the reviewer read/write path and append-only AI audit runtime;
the preserved SQLite source remains untouched until an explicit later cutover.

## Safety contract

- `topik-past-papers/derived/035-I-B.sqlite` remains the preserved source of truth
  until a later explicit cutover.
- Migration/validation opens SQLite with `mode=ro` and `PRAGMA query_only=ON`.
- No dual-write path exists.
- `--apply` is required before the migration tool writes PostgreSQL.
- Schema creation and all row inserts use one PostgreSQL transaction. Any error
  rolls the whole target transaction back.
- The target TOPIK tables must be empty before migration.
- The migration target must use a fresh schema/database with no existing TOPIK
  tables. The tool owns schema creation so an unrelated stale empty table cannot
  silently satisfy `CREATE TABLE IF NOT EXISTS`.
- Original PDF/audio paths remain logical relative paths plus byte size/SHA-256;
  files themselves remain local under `TOPIK_MEDIA_ROOT`.

## Configuration

Install the optional PostgreSQL dependency only on machines that need migration
or central DB access:

```powershell
py -3 -m pip install -r requirements-postgres.txt
```

Set secrets outside Git:

```powershell
$env:TOPIK_DATABASE_URL = "postgresql://..."
$env:TOPIK_MEDIA_ROOT = "C:\Projects\TopikDatabase\topik-past-papers"
```

`TOPIK_MEDIA_ROOT` is optional and defaults to the repository's
`topik-past-papers` folder. `TOPIK_DATABASE_URL` is required only for actual
PostgreSQL operations.

## Stage 1: PostgreSQL schema

`db/schema_postgres.sql` mirrors the current SQLite tables, constraints and
indexes. SQLite integer primary keys that need generated IDs use PostgreSQL
identity columns but allow explicit historical IDs during migration. Image BLOBs
use `BYTEA`. Timestamp/JSON text stays `TEXT` in this first migration so migration
does not silently reinterpret existing values.

All eight `ai_audit_*` tables have PostgreSQL triggers that reject both UPDATE
and DELETE, preserving the append-only audit contract.

## Stage 2: additive DB/config layer

`src/database.py` provides:

- `TOPIK_DATABASE_URL` validation
- `TOPIK_MEDIA_ROOT` resolution
- lazy psycopg 3 PostgreSQL connections
- read-only SQLite connections for migration/validation

Existing reviewer/importer modules are intentionally not switched to this layer
until stage 5 or later.

## Stage 3: dry-run and migration

Dry-run is the default and does not require PostgreSQL:

```powershell
py -3 scripts/migrate_sqlite_to_postgres.py
```

For the recovered production pilot it requires these approved invariants:

- questions: 70
- review status: 2 verified / 68 needs_manual_review
- review records: 124
- candidate audio segments: 30
- SQLite integrity check: `ok`
- foreign-key violations: 0

Actual migration requires an empty target and an explicit flag:

```powershell
py -3 scripts/migrate_sqlite_to_postgres.py --apply
```

The PostgreSQL schema and copied rows are committed together. Existing explicit
integer IDs are retained and identity sequences are advanced afterward.
Immediately after schema creation the tool validates column types/nullability/
defaults/identity, PK/FK/UNIQUE/CHECK contracts, required indexes, and the exact
append-only trigger behavior before it inserts source rows.

## Stage 4: parity validation

Offline/source-only validation:

```powershell
py -3 scripts/validate_sqlite_postgres_parity.py --source-only
```

With `TOPIK_DATABASE_URL` configured, omit `--source-only` to compare PostgreSQL.
The validator compares every source table by canonical row digest and count,
including questions, choices, answers, transcripts, review history, audio
segments, source metadata, and image BLOB hashes. If the source SQLite predates
AI audit tables, the corresponding PostgreSQL tables must be empty. It also
revalidates PostgreSQL structure and checks that all eight append-only triggers
are enabled, schema-scoped, call the expected function, and fire BEFORE UPDATE
OR DELETE only.

Stage 5 must not begin until PostgreSQL validation reports `diff_count: 0`.

### Live PostgreSQL validation

The foundation and stage-5 read path were exercised against a real local
PostgreSQL 17.11 server on 2026-10-06. The recovered `035-I-B.sqlite` migrated
into a fresh database with `diff_count: 0` and
`postgres_structure_diff_count: 0`. The live checks also confirmed PostgreSQL
read-only sessions reject UPDATE, the AI audit append-only trigger rejects both
UPDATE and DELETE with SQLSTATE `55000`, and a second migration attempt rejects
the already-populated target. The temporary validation cluster was not adopted
as an operational central database.

## Stage 5 read-only reviewer preparation

Stage 5 introduced the central PostgreSQL read path when `TOPIK_DATABASE_URL`
is configured. At that stage it was intentionally read-only:

- question/list/detail, choices, answers, transcripts, review history, audio
  segment state, source metadata and image BLOBs can be read from PostgreSQL;
- PDF/audio bytes still resolve from each device's `TOPIK_MEDIA_ROOT` and retain
  the existing byte-size/SHA-256 checks;
- AI audit display remains on the legacy SQLite path until its dedicated
  PostgreSQL conversion stage;
- question review, audio-segment mutation and clip-export linking are blocked
  before side effects in PostgreSQL mode;
- the browser receives `read_only: true` and disables editor/write controls.

The SQLite reviewer remains the writable default whenever
`TOPIK_DATABASE_URL` is unset or a test/tool supplies an explicit SQLite path.

## Stage 6 human-review writes and concurrency

Stage 6 enables only the central human-review write paths:

- question stem/choices/status and listening transcript edits;
- append-only `review_records` for human review history;
- candidate/verified `audio_segments`, including shared-dialogue pairs;
- `/review` and `/audio-segment` HTTP POST routes after the existing Host,
  Origin, CSRF-token and JSON content-type checks.

Question optimistic versioning intentionally remains the count of
`review_records` for that question. PostgreSQL writers lock the question row
with `SELECT ... FOR UPDATE`, then re-read that count inside the same
transaction. A stale browser therefore receives `409 Conflict` without a
partial write. Question, choices, transcript and history are committed or
rolled back together.

Shared audio pairs (25/26, 27/28, 29/30) are sorted into one deterministic lock
order. PostgreSQL locks all participating question rows and any existing
`audio_segments` rows before checking the common segment version. One stale
member aborts the whole pair transaction.

There is no automatic retry for database failures, serialization failures or
deadlocks. The current transaction is rolled back and the caller must reload
before applying input again.

Still intentionally disabled/deferred after stage 6:

- MP3 clip export/linking (stage 8);
- operational central-database cutover (stage 9);
- SQLite archival/freeze (stage 10).

### Live Stage 6 PostgreSQL validation

Stage 6 was exercised on 2026-10-06 against a disposable PostgreSQL 17.11
server seeded from the preserved `035-I-B.sqlite`. Two independent
`ReviewStore` instances represented PC and Laptop clients. The live checks
confirmed:

- PC save is immediately visible to Laptop and a Laptop stale save returns
  `Conflict`; the reverse direction behaves the same;
- a true simultaneous same-version question race produces one commit and one
  conflict;
- note-only review appends history and increments the existing review-record
  count version without changing question/choice/transcript content;
- forced review-history insert failure rolls back question, choices,
  transcript and history together;
- invalid payloads leave zero partial writes;
- forced PostgreSQL SQLSTATE `40001` (serialization) and `40P01` (deadlock)
  fail closed and roll back; no automatic write retry is performed;
- shared audio pair 25/26 is immediately visible cross-client, rejects stale
  writes in both directions, and a simultaneous pair race produces one commit
  and one conflict while both pair rows remain identical;
- PostgreSQL HTTP `/review` is writable after the existing security gates,
  while `export-clip` stays `403` and no AI-audit write route is exposed.

The disposable database/server is validation-only and is removed after the
test run. The production SQLite source is never opened writable by these tests.

## Stage 7 append-only AI audit on PostgreSQL

Stage 7 moves the mutable `ai_audit_35.py` runtime to standard PostgreSQL when
`TOPIK_DATABASE_URL` is configured, while preserving explicit SQLite paths for
offline/local regression workflows. No Supabase-specific API is used.

The PostgreSQL audit adapter deliberately uses tuple rows because the mature AI
audit reader has a positional-row contract, while the stage-5/6 reviewer keeps
its mapping-row adapter. Those two connection surfaces are not interchangeable.
The reviewer therefore opens a dedicated audit read connection when it needs AI
summary/status/history data.

PostgreSQL audit writes preserve the existing append-only contract:

- creating a run freezes source data in one `REPEATABLE READ` transaction so a
  concurrent human-review edit cannot produce a mixed-time audit snapshot;
- audit timestamps come from the central PostgreSQL server clock rather than a
  PC/Laptop client clock;
- creating a pass locks its parent run before checking the unique pass number
  and auditor constraints;
- checkpoint sequence and attempt number allocation lock the parent pass with
  `SELECT ... FOR UPDATE` before `MAX(...)+1` is computed;
- final result, finding occurrences and succeeded attempt are inserted in one
  transaction, with PostgreSQL identity IDs obtained through `RETURNING`;
- immutable source-snapshot and finding dedup use `ON CONFLICT DO NOTHING`
  followed by content verification, never UPDATE/UPSERT mutation of evidence;
- all database, serialization (`40001`) and deadlock (`40P01`) failures roll the
  active transaction back. No automatic retry is performed.

The pre-existing PostgreSQL triggers on all eight `ai_audit_*` tables remain in
force and still reject UPDATE/DELETE. AI audit code does not write questions,
transcripts, audio segments or human `review_records`. The reviewer can display
PostgreSQL AI evidence, but `ai_audit_write` remains false because there is no
browser/HTTP AI-audit write route.

### Live Stage 7 PostgreSQL validation

Stage 7 was exercised on 2026-10-06 against a disposable PostgreSQL 17.11
database seeded once from the preserved SQLite source. Live checks confirmed:

- run/pass/checkpoint/result/attempt/finding/occurrence write and readback;
- invalid structured output records only its intended immutable `invalid`
  attempt and does not leave a partial final result;
- simultaneous attempts for one pass receive unique monotonic attempt numbers;
- simultaneous checkpoints for one pass receive unique monotonic sequences;
- two clients racing for the same `(run_id, pass_number)` slot produce one
  winner and one conflict;
- simultaneous runs over the same immutable source deduplicate to one snapshot;
- the same finding fingerprint committed by two independent passes produces one
  finding identity and two immutable occurrences;
- simultaneous identical final responses produce one immutable result while
  their attempt records remain uniquely numbered;
- forced generic database failure, SQLSTATE `40001` and SQLSTATE `40P01` all
  roll back final-result/attempt writes with no retry;
- direct UPDATE and DELETE attempts remain blocked by SQLSTATE `55000`;
- the central reviewer reads the AI audit summary while human-review writes stay
  enabled, AI-audit HTTP writes stay disabled, and PostgreSQL clip export stays
  disabled;
- question/transcript/audio/review-record state is byte-for-byte/value-for-value
  unchanged across the AI audit validation.

The validation cluster is disposable and is not the stage-9 operational central
database. Original PDF/MP3 files remain device-local under `TOPIK_MEDIA_ROOT`.

## Stage 8 device-local clip export with central canonical identity

Stage 8 enables the reviewer `export-clip` path on PostgreSQL without moving
PDFs, source MP3s, or derived MP3 clips into the database. PostgreSQL keeps only
`audio_segments.clip_relative_path` and `clip_sha256` as the canonical identity;
the actual file remains under each device's `TOPIK_MEDIA_ROOT/derived/audio-clips`.

The export flow is intentionally split around the database lock:

- before encoding, the reviewer verifies the local source exists and still
  matches the recorded byte size/SHA-256, every member of a shared dialogue is
  `verified`, and bounds/version/source identity are identical;
- encoding writes only a unique local temporary MP3 while no central row lock
  is held, and the source size/SHA is checked again after the encoder returns;
- PostgreSQL then reuses the stage-6 deterministic question/audio
  `SELECT ... FOR UPDATE` order and revalidates the complete segment/source
  snapshot before any canonical link is written;
- the first successful exporter publishes with no-overwrite hard-link semantics
  and writes every shared-pair clip link plus `audio_export_35` history in one
  transaction; rollback cleanup removes only a destination that invocation
  created and whose checksum still matches;
- a later device with central clip metadata but no local file may re-encode the
  same source/bounds and materialize the canonical relative path only when the
  generated SHA-256 exactly matches the central checksum. This local recovery
  does not append duplicate metadata or review history;
- a different generated checksum, path escape, different existing destination,
  encoder failure, stale segment/source snapshot, database failure,
  serialization failure or deadlock fails closed. No automatic retry or file
  overwrite is performed.

Changing an audio segment continues to clear both clip metadata columns in the
same stage-6 audio update, so an older derived file can never be advertised as
the current segment. The HTTP endpoint retains the existing loopback Host,
same-origin, CSRF-token and JSON content-type checks. `ai_audit_write` remains
disabled. Stage 9 operational cutover and stage 10 SQLite freeze are not part of
this stage.

### Live Stage 8 PostgreSQL validation

Stage 8 was exercised on 2026-10-06 against a disposable PostgreSQL 17.11
x86_64 Windows server seeded once from the preserved SQLite source. Two separate
temporary `pc-media` / `laptop-media` roots represented independent devices; the
normal path used the project's imageio FFmpeg 7.1 build. The live checks
confirmed:

- PostgreSQL advertises `clip_export=true` while `ai_audit_write=false`; an
  invalid Origin still returns 403 and a wrong content type returns 415 before
  encoding or DB writes;
- PC first export through the HTTP endpoint creates one canonical shared-pair
  path/SHA plus two atomic `audio_export_35` history rows, and the local GET
  endpoint serves the exact canonical bytes;
- Laptop sees the same central clip metadata with no local `clip_url`, then
  independently re-encodes the same source/bounds, matches the canonical SHA,
  materializes the local file, serves it through GET, and does not append any
  duplicate DB metadata/history;
- two device roots racing for the first shared-pair export both complete, but
  central metadata/history is written only once while both local artifacts end
  at the same canonical SHA;
- a deliberately different encoder output is rejected and cannot overwrite the
  central identity or publish a mismatching local canonical file;
- forced PostgreSQL `P0001`, serialization `40001`, and deadlock `40P01` errors
  after publication/linking begins roll back clip metadata/history with no
  automatic retry, and rollback cleanup removes the canonical file created by
  the failed invocation;
- question/transcript state and all non-export review history remain unchanged.

The exact-SHA rematerialization contract is deliberately fail-closed across
encoder differences. This validation proves two independent media roots using
the same FFmpeg 7.1/libmp3lame build; before stage-9 cutover, the real PC and
Laptop must either use a pinned/equivalent encoder that produces the same bytes
for the accepted source/bounds or demonstrate the same SHA equality in a
physical two-device preflight. A mismatch is not silently normalized or
uploaded through PostgreSQL.

### Pinned FFmpeg and physical PC/Laptop preflight

The default canonical export path is now stricter than the initial stage-8
validation: it no longer accepts whichever `ffmpeg.exe` happens to appear first
on `PATH`. On Windows it requires the project-local binary installed by
`imageio-ffmpeg==0.6.0`:

- FFmpeg build: `7.1-essentials_build-www.gyan.dev`
- approved binary SHA-256:
  `2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3`
- local ignored location:
  `topik-past-papers/.verification_deps/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe`

`src/audio_35.py` verifies both the binary SHA and reported build before using
the default encoder. An explicit `ffmpeg=` argument is still available for
standalone diagnostic/CLI use, but reviewer canonical clip export does not pass
an override and therefore always uses the pinned binary.

Run once on **both** physical devices:

```powershell
py -3 scripts/setup_media_tools.py
```

Then create reports using the same immutable source and fixed 84.408s-120.023s
interval:

```powershell
# Laptop
py -3 scripts/ffmpeg_cross_device_preflight.py --label Laptop `
  --output topik-past-papers/derived/ffmpeg-preflight-laptop.json

# PC
py -3 scripts/ffmpeg_cross_device_preflight.py --label PC `
  --output topik-past-papers/derived/ffmpeg-preflight-pc.json
```

After copying/syncing the PC report to the Laptop (or vice versa), compare it:

```powershell
py -3 scripts/ffmpeg_cross_device_preflight.py --label Laptop `
  --compare-report topik-past-papers/derived/ffmpeg-preflight-pc.json
```

The comparison is accepted only when `comparison.status` is `PASS`. It checks
the immutable source SHA, fixed bounds, FFmpeg version, exact FFmpeg binary SHA,
and generated clip SHA. Exit code 2 means the physical devices do not produce
the same canonical bytes and stage 9 should remain blocked. The temporary MP3
used by this check is automatically deleted; only the small JSON report is kept
when `--output` is supplied.

On 2026-10-06 the pinned build was executed on the physical Laptop and physical
PC using the same immutable source and bounds. Both devices produced exactly the
same preflight result:

- source SHA-256:
  `314af506159e24b7bd4b20e95d248683673e799e8e8af5ac4b77946879e4dae2`
- clip byte size: `713709`
- clip SHA-256:
  `df265c839fb62249cc75a60844b15d3ee91e89383a73ecaa0a06d9be6a8c9d22`

The Laptop then compared its fresh result against the PC's JSON report with
`--compare-report`; `comparison.status` was `PASS` and `mismatched_fields` was
empty. This clears the encoder-byte-parity blocker that remained after the
initial stage-8 same-machine two-media-root validation. Stage 9 still requires
its own operational cutover gates; this preflight does not perform cutover.

## Stage 9 operational PostgreSQL cutover preparation

Stage 9 provisions a persistent PostgreSQL 17.11 cluster outside the repository
under the current Windows user's local application-data directory. The cluster
listens on port `55432`; application access uses a non-superuser `topik_app`
role with SCRAM authentication. Passwords are stored only in local, untracked
credential files and are never embedded in repository configuration or printed
by the Stage 9 helpers. Source PDF/MP3 files remain device-local.

The cutover tooling added for this stage is intentionally fail-closed:

- `scripts/stage9_operational.py verify-source` rechecks the approved immutable
  SQLite SHA and every recorded source-media size/SHA before operational work;
- provisioning/reset helpers keep credentials out of shell arguments and only
  permit the approved Stage 9 database names;
- `scripts/stage9_smoke.py` mutates only an explicitly supplied PostgreSQL
  target and uses temporary hard-link media roots. It exercises human review,
  stale conflicts, shared audio, append-only AI audit, clip export and local
  rematerialization without writing the original SQLite or source media;
- application environment variables are not persisted until all physical
  PC/Laptop gates have passed. In particular, Stage 9 does not fall back to a
  dual-write scheme and does not write new operational state to SQLite.

### Migration and disaster-recovery evidence

On 2026-10-06 the immutable SQLite source was migrated into a fresh persistent
Stage 9 candidate database. Deep validation reported `diff_count: 0` and
`postgres_structure_diff_count: 0`, including all source row/BLOB digests and
the append-only PostgreSQL schema contract.

Before any application cutover, a custom-format logical backup was created:

- file: `topik-stage9-precutover-20261006-205501.dump`
- size: `1,897,584` bytes
- SHA-256:
  `1d606994572fedb37ff0fea8c3394d3fb4fe27f76956e0b963f7058dbbebec59`

That backup was restored into an entirely separate PostgreSQL 17.11 cluster on
port `55433`. The independent restore again passed deep parity with zero data or
structure differences. A disposable write smoke on the restored copy then
confirmed:

- a human-review write is readable and a stale second-client write conflicts;
- shared audio updates remain atomic and stale shared-pair writes conflict;
- Stage-7 AI audit evidence can be appended and read back through its public
  append-only API;
- a verified clip can be exported in one temporary media root and
  rematerialized in another root with the same canonical SHA without adding
  duplicate export history.

The independent restore cluster and temporary validation media were removed
after the checks. The pre-cutover backup and its checksum file are retained as
the Stage 9 rollback/recovery artifact.

### Current cutover gate: physical PC unavailable

The persistent host and clean `topik` database are prepared, and the same
disposable smoke also passed against the persistent candidate database. The
candidate and final `topik` databases were then rebuilt from the clean
pre-cutover backup; the final `topik` database again passed deep parity with
zero differences.

The actual two-device application cutover was **not** activated. At the final
gate the physical PC previously identified as `DUBUDESKTOP` (`192.168.0.5`)
was offline/unreachable from the database host: ICMP and the required network
ports did not respond. Both available Chat On Steroids terminal connectors were
confirmed to be the Laptop `DUBUYOGA`, so treating them as two independent
physical clients would have produced false evidence.

Accordingly, the Laptop's persistent user environment still has no
`TOPIK_DATABASE_URL`, `TOPIK_MEDIA_ROOT`, or `PGPASSFILE` cutover values. This
preserves the pre-cutover operating state and prevents a one-device-only switch.
When the physical PC is online, Stage 9 must resume at this gate: connect both
physical devices to the prepared clean `topik` database, repeat cross-device
human stale-conflict/shared-audio/AI-audit/clip-rematerialization smokes, and
only then persist both devices' PostgreSQL runtime configuration. Stage 10 must
not begin before that physical cutover succeeds.
