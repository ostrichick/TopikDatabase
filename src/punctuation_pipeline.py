"""Versioned, lossless punctuation spacing stage for 36th TOPIK I B.

Only display text changes.  The original v4 extraction and raw PDF text are
archival evidence and remain untouched.  Call the import validator before
publishing v5; it performs independent source and content checks.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

from src.extraction_rules import PUNCTUATION_RULE_VERSION, normalize_punctuation_spacing_v2


SOURCE_VERSION = "pdf-first-36-v4"
OUTPUT_VERSION = "pdf-first-36-v5"
SOURCE_V4_SHA256 = "4c02c999b0fcbdaad48aaf9af6d0f5c754ca424f8a37885466a6704d90403684"


def encoded_staging(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def verified_v4(path: Path) -> dict:
    """Read the historical v4 artifact only if its exact bytes are approved."""
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != SOURCE_V4_SHA256:
        raise ValueError(f"Unrecognized 36th v4 staging SHA-256: {path}")
    data = json.loads(source)
    if data.get("extraction_version") != SOURCE_VERSION:
        raise ValueError("Unexpected 36th source extraction version")
    return data


def text_fields(data: dict):
    """Yield editable text paths; never visit source text or provenance."""
    for index, group in enumerate(data["groups"]):
        for field in ("instruction", "passage_text"):
            yield (f"/groups/{index}/{field}", "group", group["id"], group, field)
    for index, question in enumerate(data["questions"]):
        yield (f"/questions/{index}/stem", "stem", question["id"], question, "stem")
        for choice_index, choice in enumerate(question["choices"]):
            yield (f"/questions/{index}/choices/{choice_index}/text", "choice",
                   f"{question['id']}/choice-{choice['number']}", choice, "text")
        if "transcript" in question:
            yield (f"/questions/{index}/transcript/dialogue_text", "transcript",
                   question["id"], question["transcript"], "dialogue_text")


def counts(data: dict) -> dict:
    questions = data["questions"]
    return {
        "questions": len(questions),
        "choices": sum(len(q["choices"]) for q in questions),
        "answers": sum(isinstance(q.get("answer"), dict) for q in questions),
        "groups": len(data["groups"]),
        "transcripts": sum("transcript" in q for q in questions),
        "image_links": sum(len(q["images"]) for q in questions),
    }


EXPECTED_COUNTS = {"questions": 70, "choices": 280, "answers": 70,
                   "groups": 26, "transcripts": 30, "image_links": 7}


def normalize_v4(data: dict) -> dict:
    """Produce a v5 snapshot without modifying any input data or other field."""
    if not isinstance(data, dict) or data.get("extraction_version") not in (SOURCE_VERSION, OUTPUT_VERSION):
        raise ValueError("Punctuation stage requires 36th TOPIK I B v4/v5 staging")
    if data.get("exam") != {"id": "036-I-B", "session": 36, "level": "I", "booklet": "B"}:
        raise ValueError("Punctuation stage received another exam")
    if counts(data) != EXPECTED_COUNTS:
        raise ValueError(f"Punctuation stage source counts changed: {counts(data)}")
    if (data["extraction_version"] == SOURCE_VERSION and
            hashlib.sha256(encoded_staging(data)).hexdigest() != SOURCE_V4_SHA256):
        raise ValueError("Punctuation stage v4 source SHA-256 mismatch")
    if data["extraction_version"] == OUTPUT_VERSION:
        if data.get("punctuation_rule_version") != PUNCTUATION_RULE_VERSION:
            raise ValueError("Unknown v5 punctuation version")
    output = copy.deepcopy(data)
    for _, _, _, obj, field in text_fields(output):
        obj[field] = normalize_punctuation_spacing_v2(obj[field])
    output["extraction_version"] = OUTPUT_VERSION
    output["punctuation_rule_version"] = PUNCTUATION_RULE_VERSION
    if counts(output) != EXPECTED_COUNTS:
        raise ValueError("Punctuation stage invariants failed")
    # Compare the whole documents with the only permitted text fields restored.
    restored = copy.deepcopy(output)
    original = {path: obj[field] for path, _, _, obj, field in text_fields(data)}
    for path, _, _, obj, field in text_fields(restored):
        obj[field] = original[path]
    restored["extraction_version"] = data["extraction_version"]
    if "punctuation_rule_version" in data:
        restored["punctuation_rule_version"] = data["punctuation_rule_version"]
    else:
        del restored["punctuation_rule_version"]
    if restored != data:
        raise ValueError("Punctuation stage unexpectedly changed non-text data")
    if any(normalize_punctuation_spacing_v2(obj[field]) != obj[field]
           for _, _, _, obj, field in text_fields(output)):
        raise ValueError("Punctuation stage not idempotent")
    return output


def validate_for_import(data: dict) -> dict:
    """Reuse the central importer quality gate; require no blocking warnings."""
    from scripts.import_exam_staging import validate

    gate = validate(data)
    if gate["status"] != "validated":
        raise ValueError(f"36th staging quality gate blocked publication: {gate['blocking_warnings']}")
    required = {"questions": 70, "choices": 280, "answers": 70,
                "groups": 26, "transcripts": 30, "image_links": 7}
    if any(gate.get(key) != value for key, value in required.items()):
        raise ValueError(f"36th staging quality gate wrong record counts: {gate}")
    return gate


def diff_report(source: dict, output: dict) -> dict:
    """An exact per-field textual before/after report for independent review."""
    if source.get("extraction_version") != SOURCE_VERSION or normalize_v4(source) != output:
        raise ValueError("Diff report requires matching v4 source and derived v5")
    old = {path: (category, identity, obj[field])
           for path, category, identity, obj, field in text_fields(source)}
    changes = []
    for path, category, identity, obj, field in text_fields(output):
        _, _, original = old[path]
        normalized = obj[field]
        if original != normalized:
            changes.append({"path": path, "type": category, "record_id": identity,
                            "before": original, "after": normalized,
                            "spaces_added": len(normalized) - len(original)})
    by_type = Counter(change["type"] for change in changes)
    return {
        "source_extraction_version": SOURCE_VERSION,
        "output_extraction_version": OUTPUT_VERSION,
        "punctuation_rule_version": PUNCTUATION_RULE_VERSION,
        "source_v4_sha256": SOURCE_V4_SHA256,
        "output_v5_sha256": hashlib.sha256(encoded_staging(output)).hexdigest(),
        "counts_before": counts(source), "counts_after": counts(output),
        "fields_checked": len(old), "fields_changed": len(changes),
        "changes_by_type": dict(sorted(by_type.items())),
        "spaces_added": sum(change["spaces_added"] for change in changes),
        "changes": changes,
    }
