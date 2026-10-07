# Stage 9 physical PC/Laptop cutover — 2026-10-07

**Status: complete.** Both physical devices run the central PostgreSQL reviewer
and their persisted user settings have been read back and verified.

This report records current execution separately from the earlier Laptop-only
handoff in `postgres-foundation.md`. Stage 10 is outside this work.

## Delivery and target

The Laptop's five migration commits and its updated remote-host/recovery handoff
were pushed and pulled onto the physical PC. Both machines now use the same
tracked migration/reviewer implementation. The configured central host is the
existing `wordpress-blog` PostgreSQL 17.11 server, not Supabase.

Clients connect through a loopback SSH forward with PostgreSQL TLS
`verify-full` and the non-superuser `topik_app` client-certificate identity.
The server's public self-signed `wordpress-blog` certificate, obtained over the
authenticated SSH connection, is the client trust anchor. It is separate from
the CA that authenticates the client certificate. No TLS verification was
disabled. Credentials and backups live outside Git with user/SYSTEM-only ACLs.

## Fresh evidence

- PC identity: `DUBUDESKTOP`; Laptop identity: `DUBUYOGA`.
- PC immutable SQLite SHA and all five source-media hashes/sizes match the
  approved Stage 9 source. The physical-check helper repeats this before and
  after each step on both devices.
- Operational `topik` deep parity from the physical PC: `status=ok`,
  `diff_count=0`, `postgres_structure_diff_count=0` before cutover.
- Fresh operational backup copied off-host to the PC:
  `topik-stage9-physical-20261007.dump`; SHA-256
  `f5dd431160fe1d9fd3325c657e039d841e639bb68d8f5b276f255ff15cde5d6a`.
  Server and PC checksums match. This fresh dump was restored into the separate
  `topik_stage9_restorecheck_20261007` database in the current cluster and deep
  parity again returned zero data/structure differences. That disposable restore
  database was then removed. Independent-cluster restore evidence for the
  preceding backup remains recorded in `postgres-foundation.md`.
- Fresh physical-device FFmpeg comparison: `PASS`, zero mismatched fields.
  Both generated 713,709 bytes with clip SHA-256
  `df265c839fb62249cc75a60844b15d3ee91e89383a73ecaa0a06d9be6a8c9d22`.
- PC whole-project tests: 137 run, no failures, one Windows symlink-privilege
  skip. Laptop whole-project tests: 137 run, all passed. The subsequently added
  two disposable-target/physical-identity guard tests passed on both machines.

## Physical workflow

Mutating checks use only `topik_stage9_physical_20261007`, cloned from the clean
operational DB. Each physical machine uses a separate local media root. Test
approval, audit and export history never enter the operational `topik` DB.

The helper starts the actual reviewer HTTP handler on loopback, obtains its
CSRF token, and sends its normal JSON requests. It records device identity and
requires 409 responses for stale writes. AI evidence is exercised through the
public append-only Python API because the reviewer intentionally offers no AI
write endpoint.

An initial helper error incorrectly expected the private canonical SHA in the
reviewer response. The helper now reads that SHA from PostgreSQL and compares
it with the MP3 bytes served by HTTP. Already-committed PC test writes were not
repeated: explicit `pc-finish` checked their exact version/history state before
resuming the remaining evidence checks.

The PC and Laptop steps have passed, including HTTP 409 for stale human and
shared-pair updates, exact clip bytes served by both devices, no duplicate
export history, and PC audit readback on Laptop. The clip SHA was
`ad7c5fe7178d66315c57220af9e641d02fc789cf4aa13f81da415608644820fa`.
Reverse PC readback also passed: the Laptop's second human-review version and
its append-only audit attempt were visible on PC. Central export history stayed
at exactly two rows for the shared pair. Both devices retained the approved
source SQLite and all five original-media hashes.

## Activated operational runtime

After all physical checks passed, `TOPIK_DATABASE_URL` and `TOPIK_MEDIA_ROOT`
were persisted on both devices. Their previous user values were saved outside
Git in `TopikDatabase-runtime/precutover-user-environment.json`. No password or
`PGPASSFILE` is needed for the certificate-authenticated connection.

The launched operational reviewers were independently probed on both physical
machines. Their actual HTTP list responses report PostgreSQL, 70 questions,
2 verified / 68 pending / 0 rejected. The original PDF and audio endpoints
both returned HTTP 206 for byte-range requests. Direct read-only SQL confirmed
database `topik`, role `topik_app`, TLS 1.3 and the client certificate identity.
The clean operational DB still has 124 review records, 30 candidate segments,
zero verified segments and zero AI runs. No physical-smoke state was promoted.

At verification time the PC reviewer was `http://127.0.0.1:56067/` and the Laptop
reviewer was `http://127.0.0.1:14074/`. These are device-local, session-specific
ports; the launcher selects an available port each time.

The disposable physical-check database was removed after its reports were
retained. The central operational `topik` database and off-host recovery backup
remain. Stage 10 SQLite archival/freeze was subsequently implemented; see
`stage10-sqlite-freeze-2026-10-07.md` for current freeze/verification evidence.

## Operational entry point

Use `scripts/start_postgres_review.ps1` on each device. It reads that device's
external runtime configuration, starts a hidden
SSH forward if necessary, and runs the PostgreSQL reviewer. Connection failure
does not select SQLite as a fallback. The preserved SQLite file and original
media remain unchanged.

```powershell
powershell -NoProfile -File scripts/start_postgres_review.ps1
```

Run this launcher after a restart, rather than assuming an old SSH tunnel is
still running. Existing terminals may retain old environment values; the
launcher loads the verified external configuration explicitly. After Stage 10,
rollback must not reactivate a writable SQLite workflow. Preserve the frozen
SQLite files unchanged and recover/reconcile PostgreSQL from retained
PostgreSQL backups and operational records instead of writing new state into
the archive.

## Regression discovered during the physical gate

The original remote list read timed out because it performed audit lookups for
all 70 questions, even those outside every frozen audit pass. The list now reads
the frozen subject scope once, skips unrelated per-question summaries/execution
lookups, and preserves pending/failed scoped passes. New regressions cover both
an empty audit history and a targeted pending run. The existing audit/history
tests passed after this change. No cached audit verdicts or automatic retries
were introduced.
