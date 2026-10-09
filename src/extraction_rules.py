"""Conservative, reproducible text rules shared by TOPIK I extractions.

The historical HTML preview and the source exam PDFs are immutable inputs.
Only derived display text and derived image bytes are corrected here.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path


# Deliberately narrow: a Hangul sentence or a closing parenthesis/bracket,
# followed by a period/comma and immediately by Hangul or an opening parenthesis.
# This fixes the actual pilot errors without changing decimal numbers, 1,000,
# email addresses, URLs, filenames, ellipses or existing whitespace/newlines.
_PUNCTUATION = re.compile(r"(?<=[가-힣)\]])([.,])(?=[가-힣(])")


def normalize_punctuation_spacing(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Expected extracted text")
    return _PUNCTUATION.sub(r"\1 ", value)


# Crops are pixel coordinates (left, top, right, bottom) in the six original
# embedded-preview PNGs, visually checked against the source PDF. Listening
# 15/16 retain all four choice numerals; reading 40/41/42 remove number/points.
# The 63/64 shared email illustration already excludes both and is unchanged.
_IMAGE_RULES = {
    "assets/035-I-L-15-stimulus.png": (
        "2cede69c53ecafe4345c72f838c957ef7f328bf5f5d4721810b424b781fb472a",
        (934, 568), (76, 0, 888, 486),
    ),
    "assets/035-I-L-16-stimulus.png": (
        "30a3b3c2047d83cd7f60ce56032173bf691c9060f4ad11b380d25af85f45bf55",
        (934, 652), (76, 0, 888, 478),
    ),
    "assets/035-I-R-40-stimulus.png": (
        "d5c96e81c5a5751cd4684f1052280ae78ded3ac27b95ca2eb69cf3ef1c28b329",
        (900, 394), (184, 47, 756, 389),
    ),
    "assets/035-I-R-41-stimulus.png": (
        "f43b0d13afb8df5017104443284e1c0a5823ec3e3f998a5f9b5c7bbfa3b74e89",
        (902, 450), (246, 40, 690, 442),
    ),
    "assets/035-I-R-42-stimulus.png": (
        "02d9bd2abf95f5f73d157ab0a6ca8e0c468679b005f839ac0a12e3ab8f1d5126",
        (902, 426), (59, 33, 878, 423),
    ),
    "assets/035-I-R-63-64-stimulus.png": (
        "60f62999ae629a8d1d5c9c43d015ade25b50a50afc54789ba229895c133f94e8",
        (900, 452), None,
    ),
}

# The 35th pilot's correction metadata is a historical DB contract.  Keep its
# compound version stable; the punctuation sub-version can also be applied to
# later PDF-first staging without requiring or changing any 35th DB records.
PUNCTUATION_RULE_VERSION = "punctuation-space-v2"
RULE_VERSION = "punctuation-space-v1,image-pure-v1"
PUNCTUATION_RULE_VERSION_V3 = "punctuation-space-v3"

# TOPIK 36 R51-52 PDF page 8 has an explicitly printed sentence boundary:
# "... 있습니다. 120전화는 ...".  pypdf loses that gap; v1 intentionally
# excludes digit-start sentences to protect decimal notation.  Version 2
# conservatively extends v1 only for a complete formal Hangul ending and a
# digit-led Korean sentence, outside URL/email/filename spans. v1 and the frozen 35th
# correction metadata remain unchanged.
_DIGIT_SENTENCE = re.compile(r"(?:(?<=습니다)|(?<=합니다)|(?<=입니다)|(?<=됩니다))\.(?=\d+[가-힣])")
_PROTECTED_TOKEN = re.compile(r"(?i)(?:https?://|www\.|[^\s@]+@[^\s@]+)")
# A conventional filename has an ASCII extension, but its basename and
# intermediate suffixes may contain Hangul (e.g. 자료.설명.pdf).
_FILENAME_TOKEN = re.compile(
    r"(?i)(?<![\w.-])[\w-]+(?:\.[\w-]+)*\.[a-z][a-z0-9]{0,9}(?![A-Za-z0-9_.-])"
)
_TOKEN_OR_WHITESPACE = re.compile(r"\S+|\s+")


def normalize_punctuation_spacing_v2(value: str) -> str:
    """Apply both spacing rules outside original URL/email/filename tokens."""
    if not isinstance(value, str):
        raise TypeError("Expected extracted text")

    def transform(match: re.Match[str]) -> str:
        token = match.group()
        if (_PROTECTED_TOKEN.search(token) or _FILENAME_TOKEN.search(token)):
            return token
        return _DIGIT_SENTENCE.sub(". ", normalize_punctuation_spacing(token))

    return _TOKEN_OR_WHITESPACE.sub(transform, value)


# PDF-first 36th v6 only. Preserve v1/v2 as immutable 35th/v5 evidence.
# Spaces can be lost after a sentence question/exclamation mark or a speaker
# label's colon, including e.g. "그렇습니까?<보기>" and "여자 :________".
# Avoid punctuation chains ("?!"), pre-existing whitespace, numeric clock
# notation (12:30), colon-delimited paths and unverified URL/email/file tokens.
_FOLLOWING_SENTENCE_TEXT = re.compile(r"([?!:])(?=[가-힣A-Za-z_<\[(\"“‘])")
_PUNCTUATION_BEFORE_NUMBER = re.compile(r"(?<=[가-힣])([?!:])(?=\d+[가-힣])")


def normalize_punctuation_spacing_v3(value: str) -> str:
    """Normalize v2 plus ? ! : word boundaries without changing protected data."""
    if not isinstance(value, str):
        raise TypeError("Expected extracted text")

    def transform(match: re.Match[str]) -> str:
        token = match.group()
        if _PROTECTED_TOKEN.search(token) or _FILENAME_TOKEN.search(token):
            return token
        spaced = normalize_punctuation_spacing_v2(token)
        spaced = _FOLLOWING_SENTENCE_TEXT.sub(r"\1 ", spaced)
        return _PUNCTUATION_BEFORE_NUMBER.sub(r"\1 ", spaced)

    return _TOKEN_OR_WHITESPACE.sub(transform, value)


def image_keys() -> set[str]:
    return set(_IMAGE_RULES)


def derived_image_key(key: str) -> str:
    """Give corrected images new IDs so the six originals remain immutable."""
    if key not in _IMAGE_RULES:
        raise ValueError(f"Unreviewed image asset: {key}")
    return key[:-4] + "-clean-v1.png" if _IMAGE_RULES[key][2] else key


def clean_question_image(key: str, original_png: bytes) -> bytes:
    """Return a PNG without surrounding printed question number or points.

    Verify the known original hash/dimensions before cropping. The crop is a
    pixel copy, not OCR or new content; no choice numeral is synthesized.
    """
    if key not in _IMAGE_RULES:
        raise ValueError(f"Unreviewed image asset: {key}")
    expected_hash, dimensions, crop = _IMAGE_RULES[key]
    if hashlib.sha256(original_png).hexdigest() != expected_hash:
        raise ValueError(f"Source image changed, manually inspect before cropping: {key}")
    if not original_png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"Not a PNG: {key}")
    if crop is None:
        return original_png

    try:
        import pymupdf
    except ImportError:
        deps = Path(__file__).resolve().parents[1] / "topik-past-papers" / ".verification_deps"
        sys.path.insert(0, str(deps))
        import pymupdf

    pixmap = pymupdf.Pixmap(original_png)
    if (pixmap.width, pixmap.height) != dimensions or pixmap.alpha or pixmap.n != 3:
        raise ValueError(f"Unexpected original pixel format/dimensions: {key}")
    left, top, right, bottom = crop
    if not (0 <= left < right <= pixmap.width and 0 <= top < bottom <= pixmap.height):
        raise ValueError(f"Crop escapes original image: {key}")
    original = pixmap.samples
    rows = (original[(y * pixmap.width + left) * 3:(y * pixmap.width + right) * 3]
            for y in range(top, bottom))
    cleaned = pymupdf.Pixmap(pixmap.colorspace, right - left, bottom - top,
                             b"".join(rows), 0)
    return cleaned.tobytes("png")
