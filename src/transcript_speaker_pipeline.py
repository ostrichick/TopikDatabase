"""New source-derived 36th transcript speaker spacing, without editing frozen v4-v6.

The prior independently audited inputs stay byte-identical. Version v7 is a
separate, inspectable candidate and is NOT an automatic operational DB import
or a new human approval. This module is also reusable by later PDF extractors.
"""

from __future__ import annotations

import copy
import hashlib
import json

from src.extraction_rules import SPEAKER_TURN_RULE_VERSION, normalize_speaker_turn_spacing
from src.punctuation_pipeline import encoded_staging

SOURCE_VERSION = "pdf-first-36-v6"
OUTPUT_VERSION = "pdf-first-36-v7"
FROZEN_V6_SHA256 = "e1820e6db15f3911d32ae23d2bcaa7f401f42c0fc974b3b84d4eea3dd7759925"


def normalize_v6(source: dict) -> tuple[dict, dict]:
    """Derive exactly the speaker-label edits, with all source records fixed."""
    if source.get("extraction_version") != SOURCE_VERSION:
        raise ValueError("36th speaker spacing requires frozen v6 input")
    if source.get("exam") != {"id": "036-I-B", "session": 36, "level": "I", "booklet": "B"}:
        raise ValueError("Unexpected exam identity")
    if hashlib.sha256(encoded_staging(source)).hexdigest() != FROZEN_V6_SHA256:
        raise ValueError("Frozen 36th v6 checksum differs")

    output = copy.deepcopy(source)
    changes = []
    speaker_turns = 0
    for question in output.get("questions", []):
        transcript = question.get("transcript")
        if transcript is None:
            continue
        before = transcript["dialogue_text"]
        after = normalize_speaker_turn_spacing(before)
        speaker_turns += sum(1 for line in after.splitlines() if
                             line.startswith(("남자: ", "여자: ")))
        if before != after:
            transcript["dialogue_text"] = after
            changes.append({"id": question["id"], "before": before, "after": after})
    if len(output.get("questions", [])) != 70 or len(changes) != 30 or speaker_turns != 89:
        raise ValueError("Unexpected audited 36th speaker label distribution")
    for a, b in ((25, 26), (27, 28), (29, 30)):
        t = {q["exam_number"]: q.get("transcript") for q in output["questions"]}
        if t[a]["dialogue_text"] != t[b]["dialogue_text"]:
            raise ValueError("Shared-source speaker turns diverged")
    output["extraction_version"] = OUTPUT_VERSION
    output["speaker_turn_rule_version"] = SPEAKER_TURN_RULE_VERSION
    restored = copy.deepcopy(output)
    for item in restored["questions"]:
        if item.get("transcript") is not None:
            original = next(q for q in source["questions"] if q["id"] == item["id"])
            item["transcript"]["dialogue_text"] = original["transcript"]["dialogue_text"]
    del restored["speaker_turn_rule_version"]
    restored["extraction_version"] = SOURCE_VERSION
    if restored != source:
        raise ValueError("Non-dialogue or source provenance unexpectedly changed")
    return output, {
        "source_sha256": FROZEN_V6_SHA256,
        "output_sha256": hashlib.sha256(encoded_staging(output)).hexdigest(),
        "speaker_rule_version": SPEAKER_TURN_RULE_VERSION,
        "changed_transcripts": len(changes), "total_speaker_turns": speaker_turns,
        "changes": changes,
        "note": "Candidate extracted text only; does not update operational PostgreSQL or approve human review",
    }
