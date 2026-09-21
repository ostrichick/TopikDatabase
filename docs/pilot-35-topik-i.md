# 35th TOPIK I local database pilot

This is a **local prototype**, not a certified or publishable exam database.
Only the 35th TOPIK **I**, booklet B, is imported in this stage. TOPIK II is not
imported, although the schema can accommodate another exam.

## Reproduce

Run from the repository root on the computer containing `topik-past-papers/`:

```powershell
py -3 -m pip install --target topik-past-papers/.verification_deps pymupdf pypdf
py -3 src/pilot_35.py
py -3 -B -m unittest discover -s tests -p 'test_*.py' -v
```

The importer reads only the existing local HTML preview, 35th TOPIK I combined
paper, answer PDF, listening transcript PDF, MP3 and source manifest. It parses
the embedded JSON as data **without executing HTML/JavaScript or fetching the
external script**. It checks the source hashes and independently reads the PDF
answer table. Missing or changed input causes a nonzero exit; it does not
silently replace an existing DB.

Local, Git-ignored outputs:

* `topik-past-papers/derived/035-I-B.sqlite` — normalized exam, source file,
  section, group, question, choices, answer, transcript, image BLOB and audio
  reference data, with original input hashes.
* `topik-past-papers/derived/035-I-B-report.json` — count and review summary.

The script imports into a temporary SQLite file and moves it into place only
after passing quality checks. Running it again with unchanged inputs leaves
the existing DB intact. If the preview or source files change, reconcile the
previous pilot database first instead of overwriting approved edits.

## Confirmed mechanical checks

* 70 unique booklet question numbers: listening 1–30, reading 31–70.
* Four choices and one PDF-matched answer per question, 280 choices and 70
  matched answers; the reading answer PDF restarts numbering at 1, so its local
  numbers are converted with `booklet_number - 30`.
* Two sections, 100 answer-key points per section; paper/answer PDF hashes
  match those recorded by the existing preview and manifest.
* 30 question-to-transcript references, including shared dialogue for 25–26,
  27–28 and 29–30; audio file is attached to the listening section **without
  invented per-question timestamps**.
* Six embedded PNG image payloads, linked to seven questions. Image-choice
  questions 15 and 16 have blank textual options and must be visually reviewed.
  With `punctuation-space-v1,image-pure-v1`, the DB retains all 6 original
  images alongside 5 new cropped images (11 blobs, 6 actively linked images).
  The five corrected images remove printed exam numbers/point labels; Q15/16
  keep all four original option-number glyphs in their image crops.
* Foreign keys, required counts and run-again safety are checked automatically.

## Not approved yet

Fresh imports leave all 70 questions `needs_manual_review`. The live database
may have user-approved questions; never reset their status on reimport. Extracted
text may still have ambiguous spaces or incorrectly delimited passages; 7 image
questions need visual review. The listening transcript uses pypdf to retain
word spacing and independently compares all non-whitespace characters against
PyMuPDF for all 12 original PDF pages. Previously saved databases can be updated
with `py -3 src/upgrade_transcripts_35.py` after stopping the reviewer: it makes
a full SQLite backup and skips user-reviewed or user-edited transcripts. The
preview's old `missing_source_parts` entries are retained as
**historical preview flags**. The actual audio and transcript sources are now
linked in the DB; the old flags do not mean that the files are still absent.
Punctuation spacing is now normalized conservatively when importing structured
fields. The original `raw_question_text` and the historical HTML preview are
deliberately left unchanged as extraction evidence. See
`docs/extraction-corrections-35.md` for the non-destructive existing-DB update.
The current PDF answers match the preview values, but this is not independent
semantic proof that every question and correct choice is accurate. Source
availability does not establish redistribution or commercial-use permission.

## Read-only spot checks

```sql
SELECT s.name, q.exam_number, q.answer_key_number, a.choice_number,
       q.source_pdf_page, q.review_status
FROM questions AS q
JOIN sections AS s ON s.id = q.section_id
JOIN answers AS a ON a.question_id = q.id
ORDER BY q.exam_number;

SELECT q.exam_number, t.source_pdf_page, t.review_status
FROM transcripts AS t
JOIN questions AS q ON q.id = t.question_id
ORDER BY q.exam_number;
```

Do not upload the SQLite output, PDFs, extracted text or images to GitHub or a
public service without separately establishing the applicable content rights.
