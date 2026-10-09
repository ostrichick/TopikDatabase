"""36th punctuation migration: exact source delta, DB race, rollback and replay."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts import migrate_36_punctuation as m
from src.extraction_rules import (
    PUNCTUATION_RULE_VERSION,
    normalize_punctuation_spacing_v2 as normalize,
)


class Fixture:
    @classmethod
    def setUpClass(cls):
        cls.before, cls.v4_sha = m.read_staging(
            m.V4, frozen_sha=m.FROZEN_V4_FILE_SHA256
        )
        cls.after = copy.deepcopy(cls.before)
        cls.after["extraction_version"] = "pdf-first-36-v5"
        cls.after["punctuation_rule_version"] = PUNCTUATION_RULE_VERSION
        for question in cls.after["questions"]:
            question["stem"] = normalize(question["stem"])
            for choice in question["choices"]:
                choice["text"] = normalize(choice["text"])
            if question.get("transcript"):
                transcript = question["transcript"]
                transcript["dialogue_text"] = normalize(transcript["dialogue_text"])
        for group in cls.after["groups"]:
            for field in ("instruction", "passage_text"):
                group[field] = normalize(group[field])

    def test_exact_audited_corrections(self):
        changes = m.build_changes(self.before, self.after)
        self.assertEqual(len(changes), 54)
        self.assertEqual(sum(len(c.after) - len(c.before) for c in changes), 138)
        self.assertEqual(
            sorted(set(c.table for c in changes)),
            ["choices", "question_groups", "questions", "transcripts"],
        )

    def test_raw_provenance_answer_and_review_metadata_immutable(self):
        for edit in (
            lambda d: d["questions"][0].__setitem__("raw_question_text", "tampered"),
            lambda d: d["questions"][0]["answer"].__setitem__("choice_number", 4),
            lambda d: d["questions"][0].__setitem__("review_status", "verified"),
            lambda d: d["sources"][0].__setitem__("sha256", "0" * 64),
        ):
            with self.subTest(edit=edit):
                after = copy.deepcopy(self.after)
                edit(after)
                with self.assertRaisesRegex(m.MigrationBlocked, "outside permitted"):
                    m.build_changes(self.before, after)

    def test_manual_rewording_not_accepted_as_punctuation_correction(self):
        after = copy.deepcopy(self.after)
        after["questions"][0]["stem"] += "가"
        with self.assertRaisesRegex(m.MigrationBlocked, "other than"):
            m.build_changes(self.before, after)

    def test_missing_spaces_rejected(self):
        after = copy.deepcopy(self.after)
        after["questions"][0]["stem"] = self.before["questions"][0]["stem"]
        # Question 1 need not be an affected stem; choose a known changed cell.
        affected = next(c for c in m.build_changes(self.before, self.after)
                        if c.table == "questions")
        q = next(q for q in after["questions"] if q["id"] == affected.row_id)
        q["stem"] = affected.before
        with self.assertRaisesRegex(m.MigrationBlocked, "other than"):
            m.build_changes(self.before, after)


class SourceValidation(Fixture, unittest.TestCase):
    pass


class ReviewSafetyTests(unittest.TestCase):
    def _fake(self, *, reviewed_question=False, reviewed_transcript=False,
              record_count=0):
        question_rows = [{"id": f"036-I-L-{i:03d}", "review_status": "needs_manual_review"}
                         for i in range(1, 71)]
        transcript_rows = [{"question_id": f"036-I-L-{i:03d}",
                            "review_status": "needs_manual_review"} for i in range(1, 31)]
        if reviewed_question:
            question_rows[0]["review_status"] = "verified"
        if reviewed_transcript:
            transcript_rows[0]["review_status"] = "rejected"
        history_rows = ([{"id": 1, "subject_id": "036-I-L-001", "subject_type": "question",
                          "status": "verified", "reviewer": "reviewer", "scope": "manual",
                          "evidence": "{}", "reviewed_at": "2026-10-09"}]
                        if record_count else [])

        class ReadOnly:
            def execute(self, sql, params):
                if " FROM review_records r " in sql:
                    rows = history_rows
                elif "SELECT id,review_status FROM questions" in sql:
                    rows = question_rows
                elif "SELECT question_id,review_status FROM transcripts" in sql:
                    rows = transcript_rows
                else:
                    raise AssertionError(sql)
                return SimpleNamespace(fetchall=lambda: rows)
        return ReadOnly()

    def test_only_fully_pending_36_is_eligible(self):
        self.assertEqual(len(m._review_state(self._fake())), 64)
        for kwargs in ({"reviewed_question": True}, {"reviewed_transcript": True},
                       {"record_count": 1}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(m.MigrationBlocked):
                    m._review_state(self._fake(**kwargs))


class FakePG:
    def __init__(self, rows):
        self.rows = dict(rows)
        self.marker = None
        self.calls = []
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.fail_on_update = None
        self.updates = 0
        self._saved = (dict(self.rows), self.marker)

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if sql.startswith("SET TRANSACTION") or sql.startswith("LOCK TABLE"):
            return SimpleNamespace(rowcount=0)
        if sql.startswith("SELECT id FROM exams"):
            return SimpleNamespace(fetchone=lambda: {"id": m.EXAM_ID})
        if sql.startswith("SELECT value FROM import_metadata"):
            return SimpleNamespace(fetchone=lambda: (
                {"value": self.marker} if self.marker is not None else None
            ))
        if sql.startswith("INSERT INTO import_metadata"):
            self.marker = params[1]
            return SimpleNamespace(rowcount=1)
        if sql.startswith("UPDATE "):
            self.updates += 1
            if self.updates == self.fail_on_update:
                raise RuntimeError("injected write failure")
            table = sql.split()[1]
            field = sql.split("SET ")[1].split("=")[0]
            row_id = params[1]
            previous = params[2]
            number = params[3] if table == "choices" else None
            key = (table, row_id, number, field)
            if self.rows.get(key) != previous:
                return SimpleNamespace(rowcount=0)
            self.rows[key] = params[0]
            return SimpleNamespace(rowcount=1)
        raise AssertionError(f"Unexpected SQL: {sql}")

    def commit(self):
        self.committed = True
        self._saved = (dict(self.rows), self.marker)

    def rollback(self):
        self.rolled_back = True
        self.rows, self.marker = dict(self._saved[0]), self._saved[1]

    def close(self):
        self.closed = True


class DatabasePreflight(Fixture, unittest.TestCase):
    def setUp(self):
        self.db = FakePG(m._staging_rows(self.before))
        self.original = dict(self.db.rows)
        self.hash = m.FROZEN_35_SHA
        self.connect_patch = patch.object(m, "connect_postgres", return_value=self.db)
        self.snapshot_patch = patch.object(m, "_snapshot_35", return_value=self.hash)
        self.review_patch = patch.object(m, "_review_state", return_value="review-hash")
        self.row_patch = patch.object(m, "_text_rows", side_effect=lambda conn, lock: dict(conn.rows))
        for p in (self.connect_patch, self.snapshot_patch, self.review_patch,
                  self.row_patch):
            p.start()
            self.addCleanup(p.stop)

    def run_it(self, *, apply=False):
        return m.run(self.before, self.after, self.v4_sha, "f" * 64,
                     apply=apply, url="postgresql://testing/fixture")

    def test_default_dry_run_does_not_write_or_record_history(self):
        result = self.run_it()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["changed_cells"], 54)
        self.assertFalse(self.db.committed)
        self.assertEqual(self.db.rows, self.original)
        self.assertIsNone(self.db.marker)
        self.assertFalse(any(sql.startswith("UPDATE") for sql, _ in self.db.calls))
        self.assertTrue(self.db.closed)
        self.assertTrue(self.db.rolled_back)

    def test_apply_exact_match_then_idempotent_replay(self):
        result = self.run_it(apply=True)
        self.assertEqual(result["status"], "applied")
        self.assertTrue(self.db.committed)
        self.assertEqual(self.db.rows, m._staging_rows(self.after))
        marker = self.db.marker
        again = self.run_it(apply=True)
        self.assertEqual(again["status"], "already_applied")
        self.assertEqual(self.db.marker, marker)
        self.assertEqual(self.db.updates, 54)
        self.assertTrue(any(sql.startswith("LOCK TABLE") for sql, _ in self.db.calls))

    def test_changed_live_text_blocks_before_first_write(self):
        self.db.rows[("questions", self.before["questions"][0]["id"], None, "stem")] = "human edit"
        with self.assertRaisesRegex(m.MigrationBlocked, "manual edits"):
            self.run_it(apply=True)
        self.assertEqual(self.db.updates, 0)
        self.assertIsNone(self.db.marker)
        self.assertTrue(self.db.rolled_back)

    def test_missing_rows_block_even_when_changed_fields_would_match(self):
        self.db.rows.pop(next(k for k in self.db.rows if k[0] == "choices"))
        with self.assertRaisesRegex(m.MigrationBlocked, "mapping"):
            self.run_it(apply=True)
        self.assertEqual(self.db.updates, 0)

    def test_bogus_marker_and_partial_reapply_block(self):
        self.db.marker = "different evidence"
        with self.assertRaisesRegex(m.MigrationBlocked, "marker differs"):
            self.run_it(apply=True)
        self.db.marker = None
        self.db.rows = m._staging_rows(self.after)
        with self.assertRaisesRegex(m.MigrationBlocked, "manual edits"):
            self.run_it(apply=True)

    def test_stale_row_write_fails_with_rollback(self):
        # The fixture mirrors a PostgreSQL transaction's all-or-nothing rollback.
        self.db.fail_on_update = 4
        with self.assertRaisesRegex(RuntimeError, "injected write failure"):
            self.run_it(apply=True)
        self.assertFalse(self.db.committed)
        self.assertIsNone(self.db.marker)
        self.assertTrue(self.db.rolled_back)
        self.assertEqual(self.db.rows, self.original)

    def test_review_state_and_35_source_invariants_rechecked(self):
        mock_review = self.review_patch
        mock_review.stop()
        changed = [0]
        def review(_):
            changed[0] += 1
            return f"review-{changed[0]}"
        with patch.object(m, "_review_state", side_effect=review):
            with self.assertRaisesRegex(m.MigrationBlocked, "review statuses"):
                self.run_it(apply=True)
        # Prevent addCleanup from re-stopping an already stopped patcher.
        mock_review.start()
        self.assertFalse(self.db.committed)
        self.assertTrue(self.db.rolled_back)


if __name__ == "__main__":
    unittest.main()
