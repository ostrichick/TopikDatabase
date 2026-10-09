"""Additive, reproducible 036-I-B ?/!/: spacing correction from frozen v5.

Never rewrite the original PDFs or the historically validated v4/v5 artifacts.
Only derived, human-readable text is changed. Raw extraction and all source,
answer, image, status and provenance fields are carried through verbatim.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

from src.extraction_rules import PUNCTUATION_RULE_VERSION_V3, normalize_punctuation_spacing_v3
from src.punctuation_pipeline import EXPECTED_COUNTS, counts, encoded_staging, text_fields

SOURCE_VERSION = "pdf-first-36-v5"
OUTPUT_VERSION = "pdf-first-36-v6"
SOURCE_V5_SHA256 = "af72d0c340fe7b833b63dc0eb07652538e3dd292668a6e4986b4247792e9e095"
EXPECTED_EXAM = {"id": "036-I-B", "session": 36, "level": "I", "booklet": "B"}


def verified_v5(path: Path) -> dict:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_V5_SHA256:
        raise ValueError("Frozen 36th v5 SHA-256 mismatch")
    data = json.loads(raw.decode("utf-8"))
    if data.get("extraction_version") != SOURCE_VERSION or data.get("exam") != EXPECTED_EXAM:
        raise ValueError("Unexpected v5 source identity")
    return data


def normalize_v5(data: dict) -> dict:
    if not isinstance(data, dict) or data.get("exam") != EXPECTED_EXAM:
        raise ValueError("Punctuation v6 requires 36-I-B source")
    if counts(data) != EXPECTED_COUNTS:
        raise ValueError("Punctuation v6 source counts changed")
    version = data.get("extraction_version")
    if version not in (SOURCE_VERSION, OUTPUT_VERSION):
        raise ValueError("Punctuation v6 requires versioned v5/v6 source")
    if version == SOURCE_VERSION:
        if hashlib.sha256(encoded_staging(data)).hexdigest() != SOURCE_V5_SHA256:
            raise ValueError("Punctuation v6 requires frozen original v5 SHA-256")
        if data.get("punctuation_rule_version") != "punctuation-space-v2":
            raise ValueError("Source v5 normalization version changed")
    elif data.get("punctuation_rule_version") != PUNCTUATION_RULE_VERSION_V3:
        raise ValueError("Unknown v6 punctuation rule")
    output = copy.deepcopy(data)
    originals = {path: obj[field] for path, _, _, obj, field in text_fields(data)}
    for _, _, _, obj, field in text_fields(output):
        obj[field] = normalize_punctuation_spacing_v3(obj[field])
    output["extraction_version"] = OUTPUT_VERSION
    output["punctuation_rule_version"] = PUNCTUATION_RULE_VERSION_V3
    if counts(output) != EXPECTED_COUNTS:
        raise ValueError("Unexpected v6 record counts")
    restored = copy.deepcopy(output)
    for path, _, _, obj, field in text_fields(restored):
        obj[field] = originals[path]
    restored["extraction_version"] = data["extraction_version"]
    restored["punctuation_rule_version"] = data["punctuation_rule_version"]
    if restored != data:
        raise ValueError("Punctuation v6 changed non-text or source evidence")
    if any(normalize_punctuation_spacing_v3(obj[field]) != obj[field]
           for _, _, _, obj, field in text_fields(output)):
        raise ValueError("Punctuation v6 rule is not idempotent")
    return output


def diff_report(source: dict, output: dict) -> dict:
    if source.get("extraction_version") != SOURCE_VERSION or normalize_v5(source) != output:
        raise ValueError("Punctuation v6 diff must derive exclusively from frozen v5")
    original = {path: (kind, identity, obj[field])
                for path, kind, identity, obj, field in text_fields(source)}
    changes = []
    for path, kind, identity, obj, field in text_fields(output):
        before = original[path][2]
        after = obj[field]
        if before != after:
            changes.append({
                "path": path, "type": kind, "record_id": identity,
                "before": before, "after": after, "spaces_added": len(after) - len(before),
            })
    return {
        "source_extraction_version": SOURCE_VERSION,
        "output_extraction_version": OUTPUT_VERSION,
        "punctuation_rule_version": PUNCTUATION_RULE_VERSION_V3,
        "source_v5_sha256": SOURCE_V5_SHA256,
        "output_v6_sha256": hashlib.sha256(encoded_staging(output)).hexdigest(),
        "counts_before": counts(source), "counts_after": counts(output),
        "fields_checked": len(original), "fields_changed": len(changes),
        "changes_by_type": dict(sorted(Counter(c["type"] for c in changes).items())),
        "spaces_added": sum(c["spaces_added"] for c in changes),
        "changes": changes,
    }


def validate_for_import(data: dict) -> dict:
    from scripts.import_exam_staging import validate

    gate = validate(data)
    if gate["status"] != "validated":
        raise ValueError(f"36th v6 quality gate blocked: {gate['blocking_warnings']}")
    for field, expected in EXPECTED_COUNTS.items():
        if gate.get(field) != expected:
            raise ValueError(f"36th v6 quality gate count mismatch: {field}")
    return gate
