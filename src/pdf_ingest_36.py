"""PDF-first, read-only-source staging extraction of TOPIK 36 I B.

Run from the repository root: python -m src.pdf_ingest_36
Only an ignored derived JSON artifact is written. No database is opened.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "topik-past-papers" / "36th"
MANIFEST = ROOT / "topik-past-papers" / "manifest_early.json"
STAGING = ROOT / "topik-past-papers" / "derived" / "036-I-B" / "staging-v2.json"
EXTRACTION_VERSION = "pdf-first-36-v2"

FILENAMES = {
    "test_paper": "36th-TOPIK-I-Combined-Test-Paper.pdf",
    "listening_paper": "36th-TOPIK-I-Listening-Test-Paper.pdf",
    "reading_paper": "36th-TOPIK-I-Reading-Test-Paper.pdf",
    "answer_key": "36th-TOPIK-I-Answer-Keys.pdf",
    "listening_transcript": "36th-TOPIK-I-Listening-Transcript.pdf",
    "listening_audio": "36th-TOPIK-I-Listening-Audio.mp3",
}
EXPECTED_PAGES = {"test_paper": 27, "listening_paper": 8, "reading_paper": 17,
                  "answer_key": 2, "listening_transcript": 12}
SECTIONS = {"listening": (1, 30), "reading": (31, 70)}

_GROUP = re.compile(r"※\s*\[\s*(\d+)\s*[～~\-]\s*(\d+)\s*\]")
_QUESTION = re.compile(r"(?m)^\s*(\d{1,2})\s*\.\s*")
_CHOICE = re.compile(r"[①②③④]")
_MARKER = {"①": 1, "②": 2, "③": 3, "④": 4}
_SPEAKER = re.compile(r"^(?:남자|여자)\s*:")


def dependencies():
    try:
        from pypdf import PdfReader
    except ImportError:
        sys.path.insert(0, str(ROOT / "topik-past-papers" / ".verification_deps"))
        from pypdf import PdfReader
    try:
        import pymupdf
    except ImportError:
        sys.path.insert(0, str(ROOT / "topik-past-papers" / ".verification_deps"))
        import pymupdf
    return PdfReader, pymupdf


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def clean(text: str) -> str:
    """Preserve pypdf's Korean word spacing, only collapse PDF line wraps."""
    return re.sub(r"\s+", " ", text).strip()


def source_documents():
    """Check immutable manifest assertions before trusting any extracted text."""
    entries = json.loads(MANIFEST.read_text(encoding="utf-8"))["entries"]
    expected = {("test_paper", "combined"), ("answer_key", "combined"),
                ("listening_audio", "combined"), ("listening_transcript", "combined")}
    eligible = [x for x in entries if x.get("session") == 36 and x.get("level") == "I"]
    mapped = {(x["asset"], x["section"]): x for x in eligible}
    if len(eligible) != len(expected) or set(mapped) != expected:
        raise ValueError("36th TOPIK I manifest has missing/extra/duplicate source entries")
    sources, paths = [], {}
    for kind, name in FILENAMES.items():
        path = SOURCE_DIR / name
        relative = path.relative_to(ROOT).as_posix()
        if not path.is_file():
            raise FileNotFoundError(f"Source missing: {relative}")
        sha, size = digest(path), path.stat().st_size
        entry = mapped.get((kind, "combined"))
        if entry and (entry["path"].replace("\\", "/") != relative or
                      entry["sha256"] != sha or entry["size_bytes"] != size):
            raise ValueError(f"Source manifest SHA-256/size/path mismatch: {relative}")
        paths[kind] = path
        sources.append({"relative_path": relative, "kind": kind,
                        "sha256": sha, "byte_size": size,
                        "manifest_verified": entry is not None})
    return paths, sources


def load_pdfs(paths):
    PdfReader, pymupdf = dependencies()
    readers = {k: PdfReader(str(paths[k])) for k in EXPECTED_PAGES}
    for kind, expected in EXPECTED_PAGES.items():
        if len(readers[kind].pages) != expected:
            raise ValueError(f"{kind} page count changed: {len(readers[kind].pages)}")
    # Two cover pages, then eight listening and 17 reading pages in the booklet.
    combined = readers["test_paper"]
    for kind, offset in (("listening_paper", 2), ("reading_paper", 10)):
        for index, page in enumerate(readers[kind].pages):
            if compact(page.extract_text()) != compact(combined.pages[index + offset].extract_text()):
                raise ValueError(f"{kind} page {index + 1} disagrees with combined booklet")
    return readers, pymupdf


def answer_table(reader):
    """Both 36th sheets print global exam numbers: listening 1-30, reading 31-70."""
    result = {}
    for page_index, (section, (first, last)) in enumerate(SECTIONS.items()):
        text = reader.pages[page_index].extract_text()
        if "[B형]" not in text or ("듣기" if section == "listening" else "읽기") not in text:
            raise ValueError(f"Answer page {page_index + 1} is not TOPIK I B {section}")
        rows = {}
        for line in text.splitlines():
            values = re.findall(r"\d+|[①②③④]", line)
            if not re.match(r"^\s*\d+\s+", line) or len(values) != 6:
                continue
            for index in (0, 3):
                nr = int(values[index])
                symbol = values[index + 1]
                choice = _MARKER[symbol] if symbol in _MARKER else (int(symbol) if symbol in "1234" else 0)
                points = int(values[index + 2])
                if nr in rows or nr < first or nr > last or choice not in range(1, 5) or points not in (2, 3, 4):
                    raise ValueError(f"Invalid/duplicate answer row {section} {nr}")
                rows[nr] = {"choice_number": choice, "points": points,
                            "source_pdf_page": page_index + 1}
        if set(rows) != set(range(first, last + 1)) or sum(v["points"] for v in rows.values()) != 100:
            raise ValueError(f"Incomplete {section} answer sheet or point total")
        result.update(rows)
    return result


def strip_headers(text):
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if (not line or line.startswith("제36회 한국어능력시험") or
                line.startswith("TOPIKⅠ 듣기") or line.startswith("TOPIKⅠ 읽기") or
                line.startswith("듣기 통합 (") or re.fullmatch(r"\d{1,2}", line)):
            continue
        lines.append(line)
    return "\n".join(lines) + "\n"


def group_intro(text, *, number):
    """Split heading from source text preceding the group's first question."""
    text = text.strip()
    match = re.search(r"고르\s*십시오\.|답하십시오\.", text)
    if not match:
        raise ValueError(f"Group {number}: cannot extract printed instruction")
    instruction = clean(text[:match.end()]).replace("고르 십시오.", "고르십시오.")
    remainder = text[match.end():].strip()
    each = re.match(r"^\s*\(\s*각\s*(\d)점\s*\)", remainder)
    points_each = int(each.group(1)) if each else None
    if each:
        remainder = remainder[each.end():].strip()
    # Example box is explicitly not the passage to be solved.
    if "<보 기>" in remainder or "<보\n기>" in remainder or "<보기>" in remainder:
        remainder = ""
    return instruction, clean(remainder), points_each


def tokenize_paper(reader, section, source_relative):
    first, last = SECTIONS[section]
    groups, questions = [], []
    group = None
    for page_no, page in enumerate(reader.pages, 1):
        text = strip_headers(page.extract_text())
        events = sorted([(m.start(), "group", m) for m in _GROUP.finditer(text)] +
                        [(m.start(), "question", m) for m in _QUESTION.finditer(text)],
                        key=lambda item: item[0])
        for index, (start, kind, match) in enumerate(events):
            end = events[index + 1][0] if index + 1 < len(events) else len(text)
            body = text[match.end():end].strip()
            if kind == "group":
                lo, hi = int(match.group(1)), int(match.group(2))
                if not first <= lo <= hi <= last or (groups and lo <= groups[-1]["last_exam_number"]):
                    raise ValueError(f"Invalid group range {lo}-{hi} on {section} page {page_no}")
                instr, passage, points_each = group_intro(body, number=lo)
                group = {"id": f"036-I-{section[0].upper()}-{lo:02d}-{hi:02d}",
                         "section": section, "first_exam_number": lo,
                         "last_exam_number": hi, "instruction": instr,
                         "passage_text": passage, "points_each": points_each,
                         "source_pdf_page": page_no,
                         "source_relative_path": source_relative}
                groups.append(group)
                continue
            number = int(match.group(1))
            if number < first or number > last or group is None or not group["first_exam_number"] <= number <= group["last_exam_number"]:
                raise ValueError(f"Orphan/invalid question {number} on {section} page {page_no}")
            markers = list(_CHOICE.finditer(body))
            if len(markers) != 4 or [m.group() for m in markers] != list(_MARKER):
                raise ValueError(f"Question {number}: require four ordered, distinct printed choice slots")
            stem = clean(body[:markers[0].start()])
            stem = re.sub(r"\(\s*[234]점\s*\)", "", stem).strip()
            choices = [{"number": i + 1, "text": clean(body[markers[i].end():markers[i + 1].start() if i < 3 else len(body)])}
                       for i in range(4)]
            questions.append({"id": f"036-I-{section[0].upper()}-{number:03d}",
                              "section": section, "exam_number": number,
                              "answer_key_number": number, "group_id": group["id"],
                              "source_relative_path": source_relative,
                              "source_pdf_page": page_no,
                              "printed_page": page_no if section == "listening" else page_no + 8,
                              "stem": stem, "raw_question_text": body, "requires_image": False,
                              "choices": choices, "images": []})
    if [q["exam_number"] for q in questions] != list(range(first, last + 1)):
        raise ValueError(f"{section} questions not exactly sequential {first}-{last}")
    if {q["exam_number"] for q in questions} != {
            number for grp in groups for number in range(grp["first_exam_number"], grp["last_exam_number"] + 1)}:
        raise ValueError(f"{section} groups do not cover all questions exactly")
    return groups, questions


def transcript_entries(reader):
    """Extract source dialogue before the choices; paired 25-30 reuse one source passage."""
    entries = {}
    for page_index, page in enumerate(reader.pages, 1):
        content = strip_headers(page.extract_text())
        events = sorted([(m.start(), "group", m) for m in _GROUP.finditer(content)] +
                        [(m.start(), "question", m) for m in _QUESTION.finditer(content)],
                        key=lambda event: event[0])
        shared = None
        for index, (start, kind, match) in enumerate(events):
            stop = events[index + 1][0] if index + 1 < len(events) else len(content)
            body = content[match.end():stop].strip()
            if kind == "group":
                first = int(match.group(1))
                if first in (25, 27, 29):
                    # These shared paragraphs precede the first numbered question.
                    _, passage, _ = group_intro(body, number=first)
                    shared = clean(passage)
                    if not _SPEAKER.search(shared):
                        raise ValueError(f"Shared listening transcript {first} missing")
                continue
            number = int(match.group(1))
            if number not in range(1, 31) or number in entries:
                raise ValueError(f"Invalid/duplicate transcript entry {number}")
            portion = body.split("①", 1)[0]
            portion = re.sub(r"\(\s*[234]점\s*\)", "", portion)
            lines = [line.strip() for line in portion.splitlines()]
            pos = next((i for i, line in enumerate(lines) if _SPEAKER.match(line)), None)
            dialogue = shared if number in (25, 26, 27, 28, 29, 30) else (
                clean("\n".join(lines[pos:])) if pos is not None else "")
            if not dialogue:
                raise ValueError(f"Transcript for question {number} missing on page {page_index}")
            # Preserve each explicitly printed speaker turn, without inventing a speaker.
            dialogue = re.sub(r"\s*(?=(?:남자|여자)\s*:)", "\n", dialogue).strip()
            entries[number] = {"dialogue_text": dialogue, "source_pdf_page": page_index,
                               "warnings": [{"severity": "review", "code": "audio_playback_unchecked",
                                            "message": "Source transcript PDF was not compared to full MP3 playback."}]}
    if set(entries) != set(range(1, 31)):
        raise ValueError(f"Missing transcript subjects: {sorted(set(range(1,31)) - set(entries))}")
    return entries


def add_image_crops(questions, paths, pymupdf):
    """Crop only actual visual question material using PyMuPDF's image block bboxes."""
    per_number = {q["exam_number"]: q for q in questions}
    expected = {15: ("listening_paper", 4, (1, 2, 3, 4)),
                16: ("listening_paper", 4, (5, 6, 7, 8)),
                40: ("reading_paper", 3, (0,)),
                41: ("reading_paper", 4, (1,)),
                42: ("reading_paper", 4, (2,)),
                63: ("reading_paper", 14, (1,)),
                64: ("reading_paper", 14, (1,))}
    with pymupdf.open(str(paths["listening_paper"])) as listen, pymupdf.open(str(paths["reading_paper"])) as read:
        docs = {"listening_paper": listen, "reading_paper": read}
        for number, (kind, page_no, indexes) in expected.items():
            doc = docs[kind]
            page = doc[page_no - 1]
            blocks = [b for b in page.get_text("dict")["blocks"] if b["type"] == 1]
            if max(indexes) >= len(blocks):
                raise ValueError(f"Visual element positions changed for question {number}")
            bounds = [pymupdf.Rect(blocks[i]["bbox"]) for i in indexes]
            rect = pymupdf.Rect(min(r.x0 for r in bounds), min(r.y0 for r in bounds),
                                max(r.x1 for r in bounds), max(r.y1 for r in bounds))
            rect += (-4, -4, 4, 4)
            rect &= page.rect
            if rect.is_empty or rect.width < 50 or rect.height < 50:
                raise ValueError(f"Invalid source image clip for question {number}")
            png = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=rect, alpha=False).tobytes("png")
            q = per_number[number]
            if q["source_pdf_page"] != page_no:
                raise ValueError(f"Incorrect source image page for question {number}")
            q["requires_image"] = True
            q["images"] = [{"key": f"assets/036-I-{q['section'][0].upper()}-{number:03d}-pdf.png",
                             "mime_type": "image/png", "sha256": hashlib.sha256(png).hexdigest(),
                             "bytes_base64": base64.b64encode(png).decode("ascii"),
                             "source_relative_path": paths[kind].relative_to(ROOT).as_posix(),
                             "source_pdf_page": page_no,
                             "crop_rect": [round(v, 2) for v in rect]}]
    if {q["exam_number"] for q in questions if q["requires_image"]} != set(expected):
        raise ValueError("Unexpected source image count")


def extract():
    paths, sources = source_documents()
    readers, pymupdf = load_pdfs(paths)
    answers = answer_table(readers["answer_key"])
    groups, questions = [], []
    for section in SECTIONS:
        kind = f"{section}_paper"
        group_records, question_records = tokenize_paper(
            readers[kind], section, paths[kind].relative_to(ROOT).as_posix())
        groups.extend(group_records)
        questions.extend(question_records)
    transcripts = transcript_entries(readers["listening_transcript"])
    for q in questions:
        number = q["exam_number"]
        if number not in answers:
            raise ValueError(f"No source answer row {number}")
        points = answers[number]["points"]
        q["points"] = points
        q["answer"] = {"choice_number": answers[number]["choice_number"],
                       "source_pdf_page": answers[number]["source_pdf_page"]}
        group = next(g for g in groups if g["id"] == q["group_id"])
        if group["points_each"] is not None and group["points_each"] != points:
            raise ValueError(f"Group points disagree with answer key for {number}")
        if q["section"] == "listening":
            q["transcript"] = {**transcripts[number],
                               "source_relative_path": paths["listening_transcript"].relative_to(ROOT).as_posix()}
    add_image_crops(questions, paths, pymupdf)
    for q in questions:
        if any(not choice["text"] for choice in q["choices"]) and not q["requires_image"]:
            raise ValueError(f"Non-image question has an empty choice: {q['id']}")
    if len(groups) < 20 or len(questions) != 70 or len({q["id"] for q in questions}) != 70:
        raise ValueError("Staging completeness check failed")
    for source in sources:
        if digest(ROOT / source["relative_path"]) != source["sha256"]:
            raise ValueError(f"Source changed during extraction: {source['relative_path']}")
    return {"extraction_version": EXTRACTION_VERSION,
            "exam": {"id": "036-I-B", "session": 36, "level": "I", "booklet": "B"},
            "sources": sources, "groups": groups, "questions": questions,
            "warnings": [
                {"severity": "review", "code": "source_paper_crosscheck",
                 "message": "Standalone paper PDFs were compared page by page with the manifest-backed combined booklet."},
                {"severity": "review", "code": "audio_playback_unchecked",
                 "message": "The listening transcript PDF was not validated by complete MP3 playback."},
                {"severity": "review", "code": "korean_word_spacing_review",
                 "message": "Line breaks were collapsed, and Korean word boundaries still need visual checking."},
                {"severity": "review", "code": "image_crop_visual_review",
                 "message": "Mechanical PDF image crops require visual approval against original pages."},
                {"severity": "review", "code": "human_review_pending",
                 "message": "Source-derived content is not designated human-verified by this extractor."}
            ]}


def write_staging(data, dest=STAGING):
    dest.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if dest.exists():
        if dest.read_bytes() == encoded:
            return dest
        raise FileExistsError(f"Existing staging output differs; manual reconciliation required: {dest}")
    fd, name = tempfile.mkstemp(prefix=".stage36-", suffix=".json.part", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
        # Atomic fail-if-exists publication. Never clobber another extractor run.
        os.link(name, dest)
    finally:
        os.unlink(name)
    return dest


def main():
    data = extract()
    written = write_staging(data)
    return {"staging": str(written.relative_to(ROOT)), "questions": len(data["questions"]),
            "listening": sum(q["section"] == "listening" for q in data["questions"]),
            "reading": sum(q["section"] == "reading" for q in data["questions"]),
            "groups": len(data["groups"]), "transcripts": sum("transcript" in q for q in data["questions"]),
            "image_questions": [q["exam_number"] for q in data["questions"] if q["requires_image"]],
            "warnings": data["warnings"]}


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, OSError, KeyError, ImportError) as exc:
        print(f"TOPIK 36 PDF extraction failed closed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
