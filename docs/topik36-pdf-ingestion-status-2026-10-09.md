# TOPIK 36 I B — PDF-first ingestion implementation

Date: 2026-10-09. This report distinguishes completed staging/disposable
database work from production insertion and human review.

## Completed code

- PDF-first extraction (src/pdf_ingest_36.py) uses 36th TOPIK I B original
  question papers, answer PDF, listening transcript PDF and source MP3.
- Six source SHA-256/size checks, manifest checks for four sources and combined
  versus standalone paper text comparison are enforced.
- Output: 70 questions (30 listening/40 reading), 280 choice slots,
  70 official answer rows, 26 instruction/passage groups, 30 transcript rows,
  7 source-backed visual question crops (15/16/40/41/42/63/64). Official 36th
  reading answer numbers are 31–70; each section contributes 100 points.
- Local ignored outputs:
  topik-past-papers/derived/036-I-B/staging.json (first extraction)
  and topik-past-papers/derived/036-I-B/staging-v2.json (typed warnings).
  The original was not overwritten; source/answer/question data are unchanged
  between them. Canonical staging-v2 SHA-256:
  e214588ea6cb7b44f5c9d67964d6d57def37cf610327ebddb2ee7fc63eb46ed1
- scripts/import_exam_staging.py defaults to read-only validation. Explicit
  --apply checks source hashes, namespaced IDs, 70/280/70/30 counts, image
  hashes, section totals and structured/nested warnings. It refuses duplicate
  sessions and wraps all inserts in one PostgreSQL transaction.
- 36th question/transcript state stays needs_manual_review. Audio timing
  remains unsegmented; no MP3 playback or clip timing is claimed.
- The 36th PDF-first answer does not have a historic HTML preview, so
  answers.preview_and_pdf_agree is 0 (the earlier false attestation was fixed).
- ReviewStore/API and HTML have an explicit 35/36 selector with 35th as default;
  list/detail/media/cache queries use exam_id. Changing exam reloads the page
  to clear stale cache and in-flight operations. 36 audio-edit actions are
  intentionally disabled until their hardcoded 35th paths are generalized.
- src/ai_audit_35.py now scopes historical 35th source files/groups/images
  to the 35th exam. The historical final v4 snapshot remains exactly
  631c4fb71784439961956b582d0e66847eb862e2c2370f49c89d5ca520057c35
  on live PostgreSQL, corroborated by disposable SQLite regression tests.

## PostgreSQL backup and disposable rehearsal

Before new-exam production writes a fresh custom-format backup was saved
outside the repository:

- C:\Projects\TopikDatabase-runtime\backups\topik-pre36-20261009-114201.dump
- 4,718,964 bytes
- SHA-256: 3c1f5f8884ab1672311f27814c6be6c31372ca0bb96b02761b00b6a7c9bf5b89

pg_restore archive listing succeeded, and the backup was actually restored
to a separate database named topik_stage36_20261009. The initial staging
was inserted there with an intact 35th audit snapshot. Readback:

- 035-I-B: 70 questions verified / 30 transcripts
- 036-I-B: 70 questions needs_manual_review / 30 transcripts
- 036 official answers/choices/groups/images were inserted
- A second 36th insert was rejected, not overwritten

The later staging-v2 warnings contract and preview bit fix were tested through
the validator, but the original disposable rehearsal was performed with the
earlier staging payload. Do not call it v2 insertion evidence.

## Remaining before live release

1. Initial independent 70-question original-PDF/source review is complete.
   The report is in the ignored local file
   topik-past-papers/derived/036-I-B/blind-audit-initial.json.
   It records 60 clear / 10 finding / 0 uncertain, with 70 distinct subject IDs:
   listening Q15 and Q16 image crops omit printed option labels ① and ③;
   transcript L22 has an artificial word break (근처 에도);
   shared transcript L25/26 has 방법 들이;
   R48 stem has 좋아 했습니다;
   R51/52 shared passage has 예약 하고;
   R61/62 shared passage has 있습 니다.
   All 70 official answer/point mappings and 30 source transcript contents
   passed the auditor's comparison, but these whitespace/image findings
   **block an unconditional content-accuracy release**.
   Source-backed targeted corrections must create a new staging-v3,
   preserving v1 and v2, and a new independent verification.
2. Rehearse the accepted final staging revision, verify 35th baseline and
   backups, then append only the 36th exam to production. Existing 35th
   review records/AI audits must remain unchanged.
3. Finish 36th append-only AI-audit integration if parity with the 35th
   persisted audit workflow is required. Current audit runner is 35th-specific.
4. Human review of 36th extracted content and image crops; MP3 audio segments
   remain a separate future task.

The production topik database has NOT been modified by this 36th work as of
the latest operational query; only the disposable test database was changed.
