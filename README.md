# TopikDatabase - PDF collector starter

**Repository status:** Source index and collector code are Git-tracked. A separate, Git-ignored local corpus is now present in `topik-past-papers/` for 12 exam sessions; its PDFs, audio, transcripts, manifests, and verification report are **not** pushed to GitHub. See `topik-past-papers/README.md` on this computer for local corpus scope and verification limits.

This tool scans **12 verified TOPIK GUIDE landing pages** (35/36/37/41/47/52/60/64/83/91/96/102), identifies candidate PDF links, optionally saves actual PDF bytes, checks PDF signature/trailer, and writes a CSV provenance log with SHA-256. It performs no Git or GitHub operations.

## Windows usage

1. Unzip these files into `C:\Projects\TopikDatabase` (merge with existing files only after comparing names).
2. Open PowerShell in that folder.
3. `py -3 collect_topik_pdfs.py` — source scan only, populates `catalog/pdf_inventory.csv`.
4. `py -3 collect_topik_pdfs.py --download` — when you have confirmed applicable source use terms, save PDF files to `local_sources/035/TOPIK_I/` etc.
5. Inspect `catalog/collection_summary.json` and any `file_error` rows. Verify content/answer keys manually; a PDF signature alone does not guarantee that the file matches the session or correct answer sheet.

The collector above writes to `local_sources/` and does not automatically merge with the independently gathered `topik-past-papers/` corpus. To recheck that corpus, run `py -3 topik-past-papers/verify_corpus.py` on the computer with its local verification dependencies installed. This confirms file parsing and asset-category coverage, not full exam-content correctness.

To test on a small sample: `py -3 collect_topik_pdfs.py --download --limit 5`.

**Never stage or push originals without establishing appropriate rights and confirming visibility.** The `.gitignore` excludes source PDFs and the `local_sources/` directory. A private GitHub repo restricts access, but does not automatically establish lawful reproduction or uploading. Metadata, original code, and independently written material can be kept separately in Git. Never run `git add -f local_sources` by default.

## Naming

`local_sources/{session:03d}/TOPIK_{I|II}/TOPIK_{session:03d}_{I|II}_{READING|LISTENING|WRITING|COMBINED_OR_UNKNOWN}_{QUESTIONS|ANSWER_KEY|TRANSCRIPT|UNCLASSIFIED}_{NN}.pdf`

When ambiguous, names deliberately use `COMBINED_OR_UNKNOWN` and `UNCLASSIFIED` rather than asserting a false document type. Confirm and rename based on PDF contents after ingestion.

## Notes

- TOPIK GUIDE is an independent index, not an explicit license from the TOPIK test operator.
- Not every exam session was released. Do not fabricate missing sessions.
- The 96th TOPIK GUIDE page incorrectly labels its TOPIK II section header “91st” even though its links say 96th; anchor text takes priority.
- Comments on the 52nd download page report potential answer/audio mismatches in old files. Check against authoritative copies.
- The page may change or disallow scripted downloading. Respect its terms, robots rules, and traffic limits; if scanning fails, use authorized manual downloads and enter provenance into the CSV.
- PDF links may include third-party hosting or redirected pages: invalid/broken files are reported instead of being saved.
- OneDrive can sync your local files to Microsoft's cloud; it is not purely local storage.

Primary sources:
- https://www.topikguide.com/previous-papers/
- https://www.niied.go.kr/web/niied/contents/niied_topik
- https://docs.github.com/en/repositories/creating-and-managing-repositories/about-repositories

Official TOPIK copyright/use enquiry: topik@korea.kr (PBT, per NIIED).


## Git repository

The collector, local pilot/review code, tests, documentation, and source-page metadata are version-controlled. Downloaded exam PDFs, extracted previews, archives, caches, and local corpora remain local and are excluded by .gitignore. For the verified implementation status and outstanding human checks as of 2026-09-22, see `docs/project-audit-2026-09-22.md`.

For the current 35/36-session work, Git branch transfer and the remaining
production punctuation migration gate, see
`docs/cross-device-handoff-2026-10-09.md`.

The 35th TOPIK I pilot also includes an append-only **multi-agent independent AI audit** layer. It creates frozen blind audit bundles, records checkpoints/results/findings separately from human review state, calculates deterministic consensus/risk/convergence, and exposes those results read-only in the local reviewer. See `docs/ai-audit-system-35.md` and run `py -3 src/ai_audit_35.py --help` for the local workflow. The audit runner does not call an AI provider by itself and can never promote a question, transcript, or audio segment to human `verified` status.

The PostgreSQL migration foundation and central reviewer path are documented in
`docs/postgres-foundation.md`. Human review and audio-segment state can use
central PostgreSQL while original PDF/audio stays local. The append-only AI
audit runtime can also use the same standard PostgreSQL database as of stage 7;
its evidence remains separate from human review state and is read-only in the
reviewer. As of stage 8, verified audio clips also use central PostgreSQL only
for their canonical logical path/checksum; each PC keeps or rematerializes the
actual MP3 under its own `TOPIK_MEDIA_ROOT`. Stage 9 operational cutover was
completed on both physical devices on 2026-10-07 against the central
`wordpress-blog` PostgreSQL server. See `docs/stage9-cutover-2026-10-07.md` for
cutover evidence. Stage 10 is also implemented: the canonical SQLite database
and its three historical `before-*.sqlite` snapshots are preserved byte-for-byte
as read-only rollback/reference archives, implicit operational SQLite fallback
is disabled, and operational writes remain PostgreSQL-only. See
`docs/stage10-sqlite-freeze-2026-10-07.md` for the archive contract and current
physical-device completion status.

Start the central reviewer on either device with
`powershell -NoProfile -File scripts/start_postgres_review.ps1`. The launcher
loads device-local external configuration, starts the SSH forward if needed,
and prints an available loopback URL. It never silently selects SQLite after
a PostgreSQL connection failure.

Recheck a Stage 10 archive with
`py -3 scripts/stage10_sqlite_freeze.py verify`. The preserved SQLite files are
read-only recovery/reference evidence only; do not re-enable them as a writable
operational database. Explicit non-canonical SQLite paths remain available only
for tests and offline legacy fixtures.

Canonical clip export does **not** use an arbitrary `ffmpeg.exe` from `PATH`.
On Windows it is pinned to the exact FFmpeg 7.1 binary shipped by
`imageio-ffmpeg==0.6.0`, and `src/audio_35.py` verifies that binary's SHA-256
before use. Prepare each PC/Laptop with `py -3 scripts/setup_media_tools.py`.
Before stage 9, create one report on each physical device with
`py -3 scripts/ffmpeg_cross_device_preflight.py --label <PC-or-Laptop> --output <report.json>`
and compare one device against the other's report using `--compare-report`.
Only a `comparison.status` of `PASS` proves that source, bounds, FFmpeg build and
the generated MP3 bytes are identical across those two machines.
