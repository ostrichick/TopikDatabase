"""Extract dialogue for the 35th TOPIK I listening questions from its transcript PDF.

The result is deliberately marked for human review: pypdf recovers authored
Korean word spaces from this PDF where PyMuPDF drops them. Neither extractor
can certify correspondence with the recording.
"""

from pathlib import Path
import re
import sys


def _format_dialogue(value: str) -> str:
    """Add the explicit speaker and punctuation separators lost in extraction."""
    from src.extraction_rules import normalize_punctuation_spacing

    value = normalize_punctuation_spacing(value)
    value = re.sub(r"(?m)^(남자|여자)\s*:\s*", r"\1: ", value)
    return re.sub(r"(?<=[?!])(?=[가-힣])", " ", value)

_QUESTION = re.compile(r"(?m)^([1-9]|[12][0-9]|30)\.\s*")
_SPEAKER = re.compile(r"^(?:남자|여자)\s*:")
_SHARED = {25: (25, 26), 27: (27, 28), 29: (29, 30)}


def _dialogue(source: str) -> str:
    """Keep speaker lines and their continuations; drop stems and choices."""
    lines = [line.strip() for line in source.splitlines()]
    start = next((index for index, line in enumerate(lines) if _SPEAKER.match(line)), None)
    if start is None:
        return ""
    return "\n".join(line for line in lines[start:] if line)


def extract_transcripts(pdf_path: Path, *, restore_visual_spacing: bool = True) -> dict[int, dict]:
    """Return 30 entries with dialogue, PDF page, review status and warnings.

    Questions 25-26, 27-28 and 29-30 each use one shared passage placed before
    the first question. Both members receive the same passage, with a warning.
    No timing or recording-to-text correspondence is inferred.
    """
    try:
        import pymupdf
    except ModuleNotFoundError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "topik-past-papers" / ".verification_deps"))
        try:
            import pymupdf
        except ModuleNotFoundError as exc:
            raise RuntimeError("Install pymupdf to extract the 35th transcript") from exc

    results: dict[int, dict] = {}
    with pymupdf.open(str(pdf_path)) as document:
        cover = document[0].get_text("text") if len(document) else ""
        if "제35회" not in cover or "한국어능력시험I" not in cover or "듣기 통합" not in cover:
            raise ValueError("Expected the 35th TOPIK I listening transcript")
        if restore_visual_spacing:
            try:
                from pypdf import PdfReader
            except ModuleNotFoundError:
                sys.path.insert(0, str(Path(__file__).resolve().parents[1] /
                                       "topik-past-papers" / ".verification_deps"))
                from pypdf import PdfReader
            spaced_document = PdfReader(str(pdf_path))
            if len(spaced_document.pages) != len(document):
                raise ValueError("Transcript PDF parsers disagree on page count")
        for page_no, page in enumerate(document, 1):
            original = page.get_text("text")
            text = (spaced_document.pages[page_no - 1].extract_text()
                    if restore_visual_spacing else original)
            if restore_visual_spacing and re.sub(r"\s+", "", text) != re.sub(r"\s+", "", original):
                raise ValueError(f"Transcript PDF parsers disagree on page {page_no} content")
            matches = list(_QUESTION.finditer(text))
            shared = {}
            if matches and int(matches[0].group(1)) in _SHARED:
                first = int(matches[0].group(1))
                passage = _dialogue(text[: matches[0].start()])
                if not passage:
                    raise ValueError(f"Shared dialogue {first}-{first + 1} missing on PDF page {page_no}")
                shared = {question: passage for question in _SHARED[first]}

            for index, match in enumerate(matches):
                number = int(match.group(1))
                if number in results:
                    raise ValueError(f"Duplicate question {number} on PDF page {page_no}")
                end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
                question_body = text[match.end() : end].split("①", 1)[0]
                dialogue = shared.get(number) or _dialogue(question_body)
                if not dialogue:
                    raise ValueError(f"Dialogue missing for question {number} on PDF page {page_no}")
                if restore_visual_spacing:
                    dialogue = _format_dialogue(dialogue)
                warnings = ["Word spacing extracted independently by pypdf; human and audio review still required"]
                if number in shared:
                    first = number if number % 2 else number - 1
                    warnings.append(f"Dialogue shared with question {first + 1 if number == first else first}")
                results[number] = {
                    "text": dialogue,
                    "pdf_page": page_no,
                    "review_status": "needs_manual_review",
                    "warnings": warnings,
                }

    expected = set(range(1, 31))
    if set(results) != expected:
        raise ValueError(f"Expected questions 1-30; missing={sorted(expected - results.keys())}; "
                         f"unexpected={sorted(results.keys() - expected)}")
    return dict(sorted(results.items()))
