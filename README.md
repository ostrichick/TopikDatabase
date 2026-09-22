# TopikDatabase - PDF collector starter

**Repository status:** Source index and collector code are Git-tracked. A separate, Git-ignored local corpus is now present in `topik-past-papers/` for 12 exam sessions; its PDFs, audio, transcripts, manifests, and verification report are **not** pushed to GitHub. See `topik-past-papers/README.md` on this computer for local corpus scope and verification limits.

This tool scans **12 verified TOPIK GUIDE landing pages** (35/36/37/41/47/52/60/64/83/91/96/102), identifies candidate PDF links, optionally saves actual PDF bytes, checks PDF signature/trailer, and writes a CSV provenance log with SHA-256. It performs no Git or GitHub operations.

## Windows usage

1. Unzip these files into `C:\Users\gip4k\OneDrive\Documents\Projects\TopikDatabase` (merge with existing files only after comparing names).
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
