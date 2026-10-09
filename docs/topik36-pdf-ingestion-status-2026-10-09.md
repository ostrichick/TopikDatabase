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

## Source-fidelity corrections and final independent audit

The initial independent blind report (ignored blind-audit-initial.json)
identified 60 clear, 10 finding, 0 uncertain: missing PDF ① and ③
option labels in L15 and L16 PNG crops and word-split artifacts in
L22, L25/26, R48, R51/52 and R61/62. Exact original PDF page references
were used to correct these rather than broad Korean text replacements.

The historical intermediate staging-v3.json is preserved (raw SHA-256
7687800bbbf5d0e1900b6d249ebeb4b535c7c3395e1cebf14c553f405ab94a2e).
Its separate source audit was 69 clear / 1 finding for an erroneously
appended first-choice glyph in R48. That error was fixed in v4 by applying
the PDF word-wrap correction to the pre-choice text only.

The accepted immutable output is
topik-past-papers/derived/036-I-B/staging-v4.json (ignored locally):

- extraction_version: pdf-first-36-v4
- raw file SHA-256:
  4c02c999b0fcbdaad48aaf9af6d0f5c754ca424f8a37885466a6704d90403684
- canonical data SHA-256 in PostgreSQL import_metadata:
  508713a63b5e005c3ea44f85631d797d67b776f827e6a4d30de1f08f91f8e2aa
- v1/v2/v3 source evidence remains preserved; only the intended source
  text, option-label crop bytes and extraction version differ in v4.

The v4 independent source comparison, at ignored
topik-past-papers/derived/036-I-B/blind-audit-v4.json, returned
**70 clear / 0 finding / 0 uncertain** over 70 unique subject IDs.
Its auditor was familiar with the preceding v3 R48 finding, so this is
not represented as blind to that historical issue, but 70 records were
rechecked against the original PDFs. Evidence includes 70 official
answer/point mappings, 30 listening transcript links, 381 field
checks, seven pixel-exact PNG crops, all visible ①②③④ for L15/16,
six source SHA-256 matches and a fixed R48 first-choice boundary.
No full MP3 playback or segment timing audit was conducted.

## Production PostgreSQL append: completed 2026-10-09

Immediately before the live insert a second, newer custom-format backup
was saved outside Git:

- C:\Projects\TopikDatabase-runtime\backups\topik-pre36-final-20261009-125825.dump
- 4,718,964 bytes
- SHA-256: 0e8ff93a66d239234f0d2103b58cdf5831f140072c1d498896c192df06440469
- pg_restore archive listing succeeded.

The backup was also restored into a fresh disposable database named
topik_stage36_v4_20261009, where final staging-v4 was inserted successfully.
Readback confirmed 35th 70 verified, 36th 70 pending, 36th 280 choices,
30 pending transcripts and 7 image links; the frozen 35th v4 source
hash remained unchanged.

The exact immutable staging-v4 raw SHA and independent audit 70 clear
were explicitly rechecked immediately before the live PostgreSQL
--apply operation. The single atomic transaction committed successfully
and returned status=applied, 35th_unchanged=true, 36th_pending_review=70.

Independent production readback after commit:

| Exam | Questions | Choices | Answers | Transcripts |
| --- | --- | --- | --- | --- |
| 035-I-B | 70 verified | 280 | 70 | 30 verified |
| 036-I-B | 70 needs_manual_review | 280 | 70 | 30 needs_manual_review |

The 36th has 7 image links, 0 audio segments, and all 70 answers have
preview_and_pdf_agree=0 because this PDF-first run had no HTML preview.
The old 35th source snapshot SHA-256 was recomputed from the **live**
database after the new exam was added and matched exactly:
631c4fb71784439961956b582d0e66847eb862e2c2370f49c89d5ca520057c35.

Regression suite after source corrections: **200 tests, 0 failures,
1 skipped**. The source extractor's 13 tests all passed.

## Separately outstanding, not implied by DB ingestion

1. The 36th questions and transcripts remain needs_manual_review.
   A fresh source-checked AI audit does **not** automatically grant
   human review approval.
2. The 36th blind evidence is preserved as a local ignored JSON report.
   Native append-only PostgreSQL AI audit persistence is still specific
   to the 35th run and needs a separately generalized contract.
3. Full MP3 listening, segment boundary approval and clip exports are a
   separate later step. The original 35th audio candidates are unchanged.

The user-authorized 36th PDF extraction and **database append** are done.
