# TopikDatabase continuation audit — 2026-09-22

This report distinguishes **file availability**, **mechanical checks**, and
**human verification**. Do not use the findings as permission to publish exam
materials or as proof that an answer key, recording, and transcript correspond.

## Existing work and reproducible checks

- Git baseline before this audit: `7cf7428` on `main`, with no pre-existing
  tracked or untracked changes. The local `topik-past-papers/` corpus and derived
  database are Git-ignored and must stay private pending an actual rights review.
- The requested sessions **35, 36, 37, 41, 47, 52, 60, 64, 83, 91, 96, 102**
  each have local TOPIK I and II materials. Running
  `py -3 topik-past-papers/verify_corpus.py` found **144 readable media files,
  zero invalid**, with all four asset categories present for **24 of 24**
  session/level combinations. This command rewrites the local ignored
  `topik-past-papers/verification_report.json`; it checks parsing and audio
  duration but does not certify content. Some PDFs emitted the parser warning
  `Advanced encoding /KSC-EUC-H not implemented yet`; scanned text still needs
  visual checks.
- `py -3 src/pilot_35.py` reused the existing 35-I-B SQLite database and
  regenerated only its ignored JSON report. `py -3 src/prepare_audio_35.py`,
  `py -3 src/export_audio_candidates_35.py`, and
  `py -3 src/upgrade_transcripts_35.py` all reported that their earlier work
  was already present. No approvals were created by these commands.
- Live DB checks: SQLite `integrity_check=ok`, no foreign key violations,
  **70 questions, 280 choices, 70 answers matching the supplied PDF, 30
  transcripts**, 11 stored image blobs and six active image blobs. Question
  **1 and 2 are verified; 68 need manual review**. All **30** listening
  question/audio links are `candidate`, representing **27** distinct candidate
  intervals because 25/26, 27/28 and 29/30 share conversations. **Zero**
  segments are verified and **zero** final clips are linked. The 27 locally
  exported candidate MP3s are explicitly unverified.

## Resolved implementation defects

1. The pilot JSON report now reads actual audio-segment counts/statuses and
   exported-clip counts instead of always reporting zero timestamps. Its
   review description reflects the actual DB rather than claiming every
   question is unreviewed. Freshly imported DBs still report zero segments.
2. The pilot importer now publishes a new SQLite DB using an atomic
   create-if-absent hard link. A concurrent DB created after the initial
   existence check cannot be overwritten. A regression test simulates this
   race and confirms the existing destination and review data are preserved.
3. The local review service checks stored byte size and SHA-256 before serving
   each original PDF/audio file. It also checks the paper and answer key (plus
   transcript for listening questions) before accepting a question-review
   write. A temporary-copy regression confirms a same-size modified PDF is
   rejected without changing the review DB.

All fixes operate on tracked code/tests. The original PDFs, MP3s, transcripts,
images, and previously recorded reviewer decisions were not edited.

## Work requiring independent source evidence or a person

| Item | Current evidence and required action |
| --- | --- |
| 35-I text | Review 68 remaining questions against original paper, answer key, and transcripts. Visually check image questions 15, 16, 40, 41, 42, 63, and 64. Matching the supplied answer PDF is only an internal cross-check; the source manifest also records an answer-key complaint. |
| 35-I audio | Listen to each of 27 proposed clips in the local review UI, confirm question numbers, shared conversations, start/end and transcript alignment, then explicitly verify 30 linked segments and export final clips as appropriate. A UI playback guard is not independently auditable proof of listening: a saved `verified` status represents the reviewer's attestation. |
| 52-I/II | Keep the six answer/audio/transcript entries marked `quarantined_suspected_mismatch` in the early manifest. Source complaints require item-by-item independent corroboration before scored use. Real 47-I, 47-II, 52-I and 52-II MP3 SHA-256 hashes differ, which rejects only **byte identity**, not possible speech reuse or mislabeling. |
| 83-I | Reading answer complaints, including questions 32/44/45 raised in earlier work, lack an authenticated item-level resolution in this repository; do not invent corrected answers. |
| 96-II | Source landing-page TOPIK II header reportedly says 91, while linked filenames identify 96. The late manifest checks only the first 70 seconds of an independent 96-II audio introduction; full audio/transcript/session correspondence remains unverified. |
| Other corpus and publication | Structural availability and hashes do not establish question/answer accuracy or redistribution/commercial-use permission. Establish rights and perform content reviews separately before publishing or using questionable material for scores. |

## Reproduction

Run `py -3 -B -m unittest discover -s tests -p 'test_*.py' -v` from the
project root. On 2026-09-22 the complete suite passed: **60 tests, OK**;
`git diff --check` also passed. The live SQLite database separately passed
`integrity_check` and had zero foreign-key violations after the changes.
Run `py -3 src/pilot_35.py` to regenerate a report from the
current existing DB without replacing the DB. The human-review interface is
`py -3 src/review_ui.py --port 0` and prints a loopback-only URL; see
`docs/review-ui-35.md`. The reviewer is a local workstation tool, not a public
learner API. Never label candidate audio as verified or release materials solely
because automated tests pass.

## Git delivery scope (2026-09-22)

The three implementation repairs and this audit are delivered together on
`main` to the configured `origin` (`ostrichick/TopikDatabase`). The delivery
contains `src/pilot_35.py`, `src/review_ui.py`, `tests/test_pilot_35.py`,
`tests/test_review_ui.py`, `README.md`, and this audit report. These changes
include the current 35-I progress and explicit handoff for manual verification.

Only those six named code/test/documentation paths belong in the delivery.
The ignored `topik-past-papers/` tree (including original exams, derived
audio, the live SQLite database, source manifests and generated reports),
downloaded files, and private review evidence are excluded. A successful Git
push transfers the tracked implementation and status documentation only; it
does not publish the corpus or imply any unverified review passed.
