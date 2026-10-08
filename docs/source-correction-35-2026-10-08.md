# 35회 TOPIK I source-backed correction — 2026-10-08

## Scope

The user manually confirmed the source-fidelity findings from the independent
GPT-6 third audit. The correction is applied only to the operational PostgreSQL
database. The Stage-10 canonical SQLite file remains frozen and unchanged.

Correction version:

`audit35-gpt6-third-pass-source-correction-v1`

## Applied corrections

- Listening 25–30: restore each per-question prompt from the listening
  transcript PDF into `questions.stem`. The dialogue text itself is unchanged.
- Reading 31–33: restore the shared instruction spacing and `고르십시오`.
- Reading 34–39: restore the shared instruction spacing and `고르십시오`.
- Reading 34, 35, 38, 39, 45, 57, 58: restore the audited source spacing in
  `questions.stem`.
- Reading 67–68: restore `괜찮습니다` in the shared passage.

The correction helper records the exact before/after value, source filename and
source PDF page in append-only `review_records` with scope
`source_backed_correction_35`. All 20 affected questions are returned to
`needs_manual_review`; already verified listening transcript text remains
`verified`.

## Safety and invariants

`scripts/correct_audit_findings_35.py`:

- requires operational PostgreSQL through `TOPIK_DATABASE_URL`;
- verifies the local paper/transcript SHA-256 against PostgreSQL source metadata;
- locks target question/group rows and requires the exact audited before value;
- fails closed on a stale or partially applied target set;
- records one version marker in `import_metadata` and is idempotent after success;
- preserves `raw_question_text` as historical extraction evidence;
- does not modify choices, answers, image links, transcript text/status, or audio
  segments;
- hashes those protected tables before and after the transaction and rolls back
  if any protected state changes.

Immediately after application:

- affected questions: 20;
- source-backed correction review records: 20;
- question state: 25 `verified`, 45 `needs_manual_review`;
- transcripts: 30/30 `verified`;
- audio segments: 30 `candidate`;
- paper SHA-256:
  `3b5e8fca1ec324508e0ed56d40578c6da2f7797475ac1882915d7b732b98318c`;
- listening transcript SHA-256:
  `b7a0aeb6d96b1b99facd1bd050a6d1c09c54509f52ae58e8e1284e9bfd35852f`.

## Regression validation

After the correction implementation and live application:

- focused correction/PostgreSQL audit/write tests: pass;
- full project suite: **164 tests, OK**;
- Python compile: pass;
- `git diff --check`: pass (existing Windows LF→CRLF warnings only).

## Final blind re-audit

A new frozen 70-question run was created only after the correction committed:

- run: `audit35-b4cd5bea76c249f9`;
- label: `final-blind-full70-after-source-correction-20261008`;
- snapshot SHA-256:
  `e15433d62843cb06b616d93fd09d6d7c297ffbfaaeb0cb8e1a50e769830a1c07`;
- pass: `audit35-b4cd5bea76c249f9-p1-79685d46`;
- input SHA-256:
  `869419a867b652d90336bf438a1c1c357693382ce231d8a8df2fc3d6243065a4`;
- perspective: `independent`;
- subject count: 70.

The fresh auditor receives this pass bundle and cited originals only. It is not
given previous audit results, correction findings, or human review history.
Final verdict/import status is recorded after that blind pass completes.

### Source provenance correction and replacement audit

An initial run `audit35-b4cd5bea76c249f9` was exported before the
per-field `stem` provenance was explicit. It is retained as historical
evidence and is not the final acceptance run.

`src/ai_audit_35.py` now emits `field_sources` for every question. For
listening questions 25–30, `field_sources.stem` points to the **official
listening transcript PDF**; `field_sources.choices` continues to point to the
question paper. The source snapshot includes this mapping so it is covered by
the immutable SHA-256.

Replacement final blind run:

- run: `audit35-974a4ad069554d18`
- label: `final-blind-full70-after-source-correction-20261008-v2`
- pass: `audit35-974a4ad069554d18-p1-61067843`
- snapshot SHA-256:
  `b833fe28729b07baf63faafe15dd8cdf30608cbca7f5e87c3388ed05a62276fc`
- input SHA-256:
  `2950c8078639db35e590c859b2d73940a8006214efdb34e704877acfb71e77d8`
- pass bundle:
  `topik-past-papers/derived/ai-audit-final-blind-20261008/pass-v2.json`

The v2 audit identified eight provenance-only findings in references to shared
instructions that are printed on the group's first PDF page. The question text,
choices, answers and points agreed with the cited sources; the page references
for question numbers 9, 10, 20, 21, 38, 39, 41 and 42 were incorrect.
The valid v2 result was imported into append-only AI audit history with 62
`clear`, 8 `finding`, and 0 `uncertain`.

The group instruction/passage `field_sources` now uses the first question
page of the group, while `field_sources.stem` and `choices` retain their own
per-question source. The exact active image bytes are also included as base64
in the snapshot under `source.image_assets`, linked by `images[].key`, with
the stored SHA-256 for source-level fidelity verification.

The intermediate v3 run added frozen image payloads before the group page
reference fix and has no final result. It is retained for traceability.

The final candidate is v4:

- run: `audit35-f05e8a9febf348b2`
- label: `final-blind-full70-after-source-correction-20261008-v4`
- pass: `audit35-f05e8a9febf348b2-p1-ee94537a`
- snapshot SHA-256:
  `631c4fb71784439961956b582d0e66847eb862e2c2370f49c89d5ca520057c35`
- input SHA-256:
  `d509e54faea7982fd01cabb3b5dbd1cc4e8c7b5b9ada39841a2f562b48767249`
- pass bundle:
  `topik-past-papers/derived/ai-audit-final-blind-20261008/pass-v4.json`

The independent v4 auditor receives only the immutable pass-v4 bundle and
original cited exam files. Final status and approval must refer to v4 and
require 70 `clear`, 0 findings and 0 uncertain verdicts.

## Final approval gate

`scripts/finalize_blind_audit_35.py check` validates the exact v4 run/pass
identity, 70 unique completed subjects and 70 `clear` verdicts, zero findings,
and a live PostgreSQL content snapshot equal to the frozen v4 SHA-256.
Until a valid result is imported it fails closed with
`final v4 independent blind audit result not imported`.
Only after the check passes may `apply` use the existing versioned
`ReviewStore.save_review` path to approve pending question records, preserving
the original answer, choice, image, and audio data and noting the user's
earlier manual inspection. The audio segment candidates remain outside this
text-extraction approval.

Latest code validation: **169 tests passed**, `git diff --check` passed.
The operational PostgreSQL source snapshot has been read again and equals
`631c4fb71784439961956b582d0e66847eb862e2c2370f49c89d5ca520057c35`.
v4 independent verdict import and the final 45 pending approvals remain
incomplete while `result-v4.json` is absent.
