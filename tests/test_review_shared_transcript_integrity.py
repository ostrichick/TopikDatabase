"""Shared transcript review integrity against a disposable copy of 35th corpus.

The real SQLite archive is only opened with SQLite URI mode=ro and
PRAGMA query_only=ON, then copied via sqlite3.Connection.backup(). All test
writes, version assertions and HTTP requests target the private temp copy.
"""

from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch
import hashlib
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest

from src import review_ui


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "topik-past-papers" / "derived" / "035-I-B.sqlite"
SHARED_PAIRS = ((25, 26), (27, 28), (29, 30))


def qid(number: int) -> str:
    return f"035-I-L-{number:03d}"


class Disposable35ReviewTests(unittest.TestCase):
    """An independent source-verified disposable DB per individual test."""

    @classmethod
    def setUpClass(cls):
        if not SOURCE.is_file():
            raise unittest.SkipTest("Frozen 35th corpus SQLite unavailable")

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="topik-shared-transcript-test-")
        self.addCleanup(directory.cleanup)
        self.copy = Path(directory.name) / "private-35.sqlite"
        self.reset_copy()

    def reset_copy(self):
        """Restore immutable source snapshot between independent scenario branches."""
        with closing(sqlite3.connect(SOURCE.resolve().as_uri() + "?mode=ro", uri=True)) as original:
            original.execute("PRAGMA query_only=ON")
            self.assertEqual(original.execute("PRAGMA query_only").fetchone()[0], 1)
            with closing(sqlite3.connect(self.copy)) as destination:
                original.backup(destination)
        self.store = review_ui.ReviewStore(self.copy, root=ROOT)

    def payload(self, number, *, status="verified", note="Shared source checked"):
        detail = self.store.get_question(qid(number), fast=True)
        return {
            "version": detail["version"],
            "status": status,
            "stem": detail["stem"],
            "choices": [choice["text"] for choice in detail["choices"]],
            "transcript_text": detail["transcript"]["text"] if detail["transcript"] else None,
            "note": note,
        }

    def pair_correction(self, selected, sibling, *, status="verified"):
        payload = self.payload(selected, status=status,
                               note=f"Checked original shared transcript {selected}/{sibling}")
        payload["transcript_text"] += f" [explicit {selected}/{sibling} correction]"
        payload["shared_transcript"] = {
            "other_question_id": qid(sibling),
            "other_version": self.store.get_question(qid(sibling), fast=True)["version"],
            "confirm_shared_source": True,
        }
        return payload

    def approve_or_reject_sibling(self, number, *, status="verified"):
        """Model live-PG human decisions only inside the disposable 35th copy."""
        self.assertIn(status, ("verified", "rejected"))
        before = self.store.get_question(qid(number), fast=True)
        payload = self.payload(number, status=status,
                               note=f"Previous independent human decision: {status}")
        payload["stem"] += f" [historical human {status} evidence]"
        result = self.store.save_review(qid(number), payload, fast_response=True)
        self.assertTrue(result["saved"])
        self.assertEqual(result["review_status"], status)
        self.assertEqual(result["version"], before["version"] + 1)
        detail = self.store.get_question(qid(number), fast=True)
        self.assertEqual(detail["review_status"], status)
        self.assertEqual(detail["transcript"]["review_status"], status)
        self.assertEqual(detail["transcript"]["text"], before["transcript"]["text"])
        return detail

    def snapshot(self, ids):
        """Capture all mutable review fields, histories, versions and source rows."""
        with closing(sqlite3.connect(self.copy)) as conn:
            snapshot = {}
            for name in ids:
                snapshot[name] = {
                    "question": conn.execute("SELECT * FROM questions WHERE id=?", (name,)).fetchone(),
                    "choices": conn.execute(
                        "SELECT * FROM choices WHERE question_id=? ORDER BY number", (name,)
                    ).fetchall(),
                    "transcript": conn.execute(
                        "SELECT * FROM transcripts WHERE question_id=?", (name,)
                    ).fetchone(),
                    "answer": conn.execute("SELECT * FROM answers WHERE question_id=?", (name,)).fetchone(),
                    "audio_segment": conn.execute(
                        "SELECT * FROM audio_segments WHERE question_id=?", (name,)
                    ).fetchone(),
                    "history": conn.execute(
                        "SELECT * FROM review_records WHERE subject_id=? ORDER BY id", (name,)
                    ).fetchall(),
                    "version": self.store.get_question(name, fast=True)["version"],
                }
            snapshot["source_files"] = conn.execute(
                "SELECT * FROM source_files ORDER BY id"
            ).fetchall()
            snapshot["history_count_all"] = conn.execute(
                "SELECT COUNT(*) FROM review_records"
            ).fetchone()[0]
            return snapshot

    def assert_source_unchanged(self, baseline):
        with closing(sqlite3.connect(self.copy)) as conn:
            self.assertEqual(
                conn.execute("SELECT * FROM source_files ORDER BY id").fetchall(),
                baseline["source_files"],
                "Review must never alter archived PDF/MP3 metadata",
            )

    def test_shared_dialogue_pair_provenance_and_existing_audit_history(self):
        with closing(sqlite3.connect(self.copy)) as conn:
            pair_spans = []
            for left, right in SHARED_PAIRS:
                pair = []
                for number in (left, right):
                    name = qid(number)
                    item = conn.execute(
                        "SELECT q.exam_number,q.review_status,t.dialogue_text,t.source_file_id,"
                        "t.source_pdf_page,s.source_file_id,a.start_ms,a.end_ms,a.status,a.version "
                        "FROM questions q JOIN transcripts t ON t.question_id=q.id "
                        "JOIN audio_segments a ON a.question_id=q.id "
                        "JOIN answers s ON s.question_id=q.id "
                        "WHERE q.id=?", (name,),
                    ).fetchone()
                    self.assertIsNotNone(item)
                    self.assertEqual(item[0], number)
                    self.assertEqual(item[1], "needs_manual_review")
                    history = conn.execute(
                        "SELECT COUNT(*) FROM review_records WHERE subject_type='question' AND subject_id=?",
                        (name,),
                    ).fetchone()[0]
                    self.assertGreater(history, 0, "Frozen reviewer history must be preserved")
                    pair.append(item)
                for index in (3, 4, 5, 6, 7, 8, 9):
                    self.assertEqual(pair[0][index], pair[1][index],
                                     f"{left}/{right} paired source/bounds index {index}")
                pair_spans.append((pair[0][6], pair[0][7]))
            self.assertEqual(len(set(pair_spans)), 3)

    def test_ordinary_one_sided_shared_transcript_change_fails_closed_without_any_mutation(self):
        for left, right in SHARED_PAIRS:
            for edited, sibling in ((left, right), (right, left)):
                with self.subTest(edited=edited, sibling=sibling):
                    ids = (qid(edited), qid(sibling))
                    prior = self.snapshot(ids)
                    attempt = self.payload(edited)
                    attempt["stem"] += " [attempted unrelated stem edit]"
                    attempt["choices"][0] += " [attempted choice edit]"
                    attempt["transcript_text"] += " [unilateral transcript edit]"
                    with self.assertRaises(review_ui.Conflict):
                        self.store.save_review(qid(edited), attempt, fast_response=True)
                    self.assertEqual(self.snapshot(ids), prior,
                                     "Rejected ordinary edits must not persist partial writes")

    def test_unchanged_transcript_allows_selected_stem_and_approval_only(self):
        for left, right in SHARED_PAIRS:
            with self.subTest(pair=(left, right)):
                ids = (qid(left), qid(right))
                prior = self.snapshot(ids)
                attempt = self.payload(left, status="verified", note="Checked actual original PDF")
                attempt["stem"] += " [independent validated stem]"
                ack = self.store.save_review(qid(left), attempt, fast_response=True)
                self.assertTrue(ack["saved"])
                self.assertEqual(ack["review_status"], "verified")
                self.assertEqual(ack["version"], prior[qid(left)]["version"] + 1)
                after = self.snapshot(ids)
                self.assertEqual(after[qid(right)], prior[qid(right)],
                                 "Independent approval must not implicitly approve sibling")
                self.assertEqual(
                    self.store.get_question(qid(left), fast=True)["transcript"]["text"],
                    attempt["transcript_text"],
                )
                self.assertEqual(len(after[qid(left)]["history"]),
                                 len(prior[qid(left)]["history"]) + 1)
                self.assert_source_unchanged(prior)

    def test_explicit_pair_correction_updates_both_texts_but_not_sibling_human_approval(self):
        for left, right in SHARED_PAIRS:
            for selected, sibling in ((left, right), (right, left)):
                with self.subTest(selected=selected, sibling=sibling):
                    self.reset_copy()
                    ids = (qid(selected), qid(sibling))
                    prior = self.snapshot(ids)
                    correction = self.pair_correction(selected, sibling)
                    correction["stem"] += " [individually reviewed stem]"
                    ack = self.store.save_review(qid(selected), correction, fast_response=True)
                    self.assertTrue(ack["saved"])
                    self.assertEqual(ack["review_status"], "verified")
                    self.assertEqual(ack["version"], prior[qid(selected)]["version"] + 1)
                    self.assertEqual(ack["shared_transcript_updated"], {
                        "id": qid(sibling),
                        "version": prior[qid(sibling)]["version"] + 1,
                        "review_status": "needs_manual_review",
                    })
                    changed = self.snapshot(ids)
                    for number in (selected, sibling):
                        detail = self.store.get_question(qid(number), fast=True)
                        self.assertEqual(detail["transcript"]["text"], correction["transcript_text"])
                        self.assertEqual(detail["version"], prior[qid(number)]["version"] + 1)
                    self.assertEqual(self.store.get_question(qid(selected), fast=True)["transcript"]["review_status"],
                                     "verified")
                    self.assertEqual(self.store.get_question(qid(sibling), fast=True)["transcript"]["review_status"],
                                     "needs_manual_review")
                    self.assertEqual(self.store.get_question(qid(sibling), fast=True)["review_status"],
                                     "needs_manual_review")
                    self.assertEqual(changed[qid(sibling)]["question"],
                                     prior[qid(sibling)]["question"],
                                     "Sibling stem, status, and immutable question fields must not change")
                    self.assertEqual(changed[qid(sibling)]["choices"], prior[qid(sibling)]["choices"])
                    self.assertEqual(changed[qid(sibling)]["answer"], prior[qid(sibling)]["answer"])
                    self.assertEqual(changed[qid(sibling)]["audio_segment"],
                                     prior[qid(sibling)]["audio_segment"])
                    self.assertEqual(changed[qid(sibling)]["history"][:-1],
                                     prior[qid(sibling)]["history"])
                    sibling_evidence = changed[qid(sibling)]["history"][-1]
                    self.assertEqual(sibling_evidence[1], "question")
                    self.assertEqual(sibling_evidence[2], qid(sibling))
                    self.assertEqual(sibling_evidence[3], "needs_manual_review")
                    self.assertEqual(sibling_evidence[5], "shared_transcript_source_correction")
                    evidence = json.loads(sibling_evidence[6])
                    self.assertEqual(evidence["paired_question_id"], qid(selected))
                    self.assertEqual(evidence["after"], correction["transcript_text"])
                    self.assertEqual(evidence["human_approval_changed"], False)
                    self.assertEqual(changed[qid(selected)]["history"][:-1],
                                     prior[qid(selected)]["history"])
                    self.assertEqual(changed[qid(selected)]["history"][-1][5],
                                     "manual_question_review")
                    self.assertEqual(changed["history_count_all"], prior["history_count_all"] + 2)
                    self.assert_source_unchanged(prior)

    def test_explicit_pair_correction_with_stale_sibling_version_rolls_back(self):
        selected, sibling = SHARED_PAIRS[0]
        stale = self.pair_correction(selected, sibling)
        sibling_review = self.payload(sibling, status="needs_manual_review", note="Fresh sibling review")
        sibling_review["stem"] += " [newer sibling stem]"
        self.store.save_review(qid(sibling), sibling_review, fast_response=True)
        prior = self.snapshot((qid(selected), qid(sibling)))
        with self.assertRaises(review_ui.Conflict):
            self.store.save_review(qid(selected), stale, fast_response=True)
        self.assertEqual(self.snapshot((qid(selected), qid(sibling))), prior)

    def test_explicit_pair_correction_refuses_approved_sibling_and_approved_transcript(self):
        for change in ("question", "transcript"):
            with self.subTest(approved=change):
                self.reset_copy()
                a, b = SHARED_PAIRS[1]
                if change == "question":
                    review = self.payload(b, status="verified", note="Already approved")
                    self.store.save_review(qid(b), review, fast_response=True)
                else:
                    with closing(sqlite3.connect(self.copy)) as conn:
                        conn.execute("UPDATE transcripts SET review_status='verified' WHERE question_id=?",
                                     (qid(b),))
                        conn.commit()
                prior = self.snapshot((qid(a), qid(b)))
                correction = self.pair_correction(a, b)
                with self.assertRaises(review_ui.Conflict):
                    self.store.save_review(qid(a), correction, fast_response=True)
                self.assertEqual(self.snapshot((qid(a), qid(b))), prior)

    def test_live_like_verified_or_rejected_sibling_requires_separate_reset_confirmation(self):
        """Historical verified/rejected human decisions must be explicitly revoked.

        Live 35th PG differs from the frozen pilot: shared pair question and
        transcript review statuses are verified. Construct that state by an
        ordinary human review in the private DB; do NOT touch live PG.
        """
        for left, right in SHARED_PAIRS:
            for selected, sibling in ((left, right), (right, left)):
                for final_status in ("verified", "rejected"):
                    for selected_already_verified in (False, True):
                        with self.subTest(selected=selected, sibling=sibling,
                                          existing_sibling_status=final_status,
                                          selected_already_verified=selected_already_verified):
                            self._assert_live_like_reset(
                                selected, sibling, final_status,
                                selected_already_verified=selected_already_verified)

    def _assert_live_like_reset(self, selected, sibling, final_status,
                                *, selected_already_verified):
        """Separate branch so each scenario starts with a fresh immutable copy."""
        self.reset_copy()
        if selected_already_verified:
            self.approve_or_reject_sibling(selected, status="verified")
        self.approve_or_reject_sibling(sibling, status=final_status)
        ids = (qid(selected), qid(sibling))
        initial = self.snapshot(ids)
        correction = self.pair_correction(selected, sibling,
                                          status="verified")
        correction["stem"] += " [selected-only human stem change]"
        # The ordinary explicit-pair confirmation is not consent
        # to revoke a previous human verification or rejection.
        with self.assertRaises(review_ui.Conflict):
            self.store.save_review(qid(selected), correction,
                                   fast_response=True)
        self.assertEqual(self.snapshot(ids), initial,
                         "Without reset confirmation every field and "
                         "historical review must remain unchanged")
        correction["shared_transcript"]["confirm_reset_review"] = True
        ack = self.store.save_review(qid(selected), correction,
                                     fast_response=True)
        self.assertTrue(ack["saved"])
        self.assertEqual(ack["review_status"], "verified")
        self.assertEqual(ack["request_version"],
                         initial[qid(selected)]["version"])
        self.assertEqual(ack["version"],
                         initial[qid(selected)]["version"] + 1)
        self.assertEqual(ack["shared_transcript_updated"], {
            "id": qid(sibling),
            "version": initial[qid(sibling)]["version"] + 1,
            "review_status": "needs_manual_review",
        })
        self.assertEqual(ack["last_human_review"]["status"],
                         "verified")
        self.assertTrue(ack["last_human_review"]["approved"])
        after = self.snapshot(ids)
        selected_detail = self.store.get_question(qid(selected),
                                                  fast=True)
        sibling_detail = self.store.get_question(qid(sibling),
                                                 fast=True)
        # Both source transcripts change atomically, but human
        # approval is exclusively the selected question's.
        for detail in (selected_detail, sibling_detail):
            self.assertEqual(detail["transcript"]["text"],
                             correction["transcript_text"])
        self.assertEqual(selected_detail["review_status"], "verified")
        self.assertEqual(selected_detail["transcript"]["review_status"],
                         "verified")
        self.assertEqual(sibling_detail["review_status"],
                         "needs_manual_review")
        self.assertEqual(sibling_detail["transcript"]["review_status"],
                         "needs_manual_review")
        self.assertEqual(sibling_detail["version"],
                         initial[qid(sibling)]["version"] + 1)
        self.assertEqual(after[qid(sibling)]["choices"],
                         initial[qid(sibling)]["choices"])
        self.assertEqual(after[qid(sibling)]["answer"],
                         initial[qid(sibling)]["answer"])
        self.assertEqual(after[qid(sibling)]["audio_segment"],
                         initial[qid(sibling)]["audio_segment"])
        # Resetting q.review_status is intentional; its stem,
        # source_file_id, PDF metadata, and all other question
        # columns must remain byte-for-byte untouched.
        before_question = initial[qid(sibling)]["question"]
        after_question = after[qid(sibling)]["question"]
        self.assertEqual(len(before_question), len(after_question))
        with closing(sqlite3.connect(self.copy)) as metadata_db:
            changed_columns = [row[1] for row in metadata_db.execute(
                "PRAGMA table_info(questions)")]
        self.assertEqual(
            {column for column, before, now in
             zip(changed_columns, before_question, after_question)
             if before != now},
            {"review_status"})
        history_before = initial[qid(sibling)]["history"]
        history_after = after[qid(sibling)]["history"]
        self.assertEqual(history_after[:-1], history_before,
                         "Append-only verified/rejected history "
                         "must never be overwritten")
        old_human = [entry for entry in history_before
                     if entry[5] == "manual_question_review"]
        self.assertTrue(old_human,
                        "Original verified/rejected human evidence lost")
        self.assertEqual(old_human[-1][3], final_status)
        self.assertEqual(history_after[-1][3], "needs_manual_review")
        self.assertEqual(history_after[-1][5],
                         "shared_transcript_source_correction")
        record = json.loads(history_after[-1][6])
        self.assertEqual(record["paired_question_id"], qid(selected))
        self.assertEqual(record["after"], correction["transcript_text"])
        self.assertEqual(record["previous_question_status"], final_status)
        self.assertEqual(record["previous_transcript_status"], final_status)
        self.assertIs(record["human_approval_changed"], True)
        self.assertIs(record["historical_approvals_preserved"], True)
        self.assertIs(record["explicit_pair_recheck"], True)
        self.assertEqual(after[qid(selected)]["history"][:-1],
                         initial[qid(selected)]["history"])
        self.assertEqual(after[qid(selected)]["history"][-1][5],
                         "manual_question_review")
        self.assertEqual(after["history_count_all"],
                         initial["history_count_all"] + 2)
        # The latest provenance entry supersedes (without
        # deleting) the old verified human approval, so the
        # list/rail must not claim a current approval.
        sibling_row = next(
            item for item in self.store.list_questions_fast()["items"]
            if item["id"] == qid(sibling)
        )
        self.assertEqual(sibling_row["status"], "needs_manual_review")
        self.assertEqual(sibling_row["review_version"],
                         initial[qid(sibling)]["version"] + 1)
        self.assertEqual(sibling_row["last_human_review"]["status"],
                         final_status)
        self.assertIs(sibling_row["last_human_review"]["is_current"], False)
        self.assertIs(sibling_row["last_human_review"]["approved"], False)
        self.assert_source_unchanged(initial)

    def test_stale_reviewed_sibling_cannot_be_reset_even_with_explicit_confirmation(self):
        selected, sibling = SHARED_PAIRS[0]
        for status in ("verified", "rejected"):
            with self.subTest(status=status):
                self.reset_copy()
                correction = self.pair_correction(selected, sibling)
                correction["shared_transcript"]["confirm_reset_review"] = True
                self.approve_or_reject_sibling(sibling, status=status)
                initial = self.snapshot((qid(selected), qid(sibling)))
                with self.assertRaises(review_ui.Conflict):
                    self.store.save_review(qid(selected), correction,
                                           fast_response=True)
                self.assertEqual(self.snapshot((qid(selected), qid(sibling))),
                                 initial)

    def test_invalid_reset_confirmation_never_revokes_a_human_review(self):
        selected, sibling = SHARED_PAIRS[2]
        self.approve_or_reject_sibling(sibling, status="verified")
        for invalid in (False, None, "true", 1, 0):
            with self.subTest(confirm_reset_review=invalid):
                prior = self.snapshot((qid(selected), qid(sibling)))
                correction = self.pair_correction(selected, sibling)
                correction["shared_transcript"]["confirm_reset_review"] = invalid
                with self.assertRaises(review_ui.ReviewError):
                    self.store.save_review(qid(selected), correction,
                                           fast_response=True)
                self.assertEqual(self.snapshot((qid(selected), qid(sibling))),
                                 prior)

    def test_explicit_pair_requires_matching_source_text_and_page(self):
        a, b = SHARED_PAIRS[2]
        for column, replacement in (("dialogue_text", "already changed independently"),
                                    ("source_pdf_page", 999)):
            with self.subTest(provenance_field=column):
                with closing(sqlite3.connect(self.copy)) as conn:
                    original = conn.execute(f"SELECT {column} FROM transcripts WHERE question_id=?",
                                            (qid(b),)).fetchone()[0]
                    conn.execute(f"UPDATE transcripts SET {column}=? WHERE question_id=?",
                                 (replacement, qid(b)))
                    conn.commit()
                prior = self.snapshot((qid(a), qid(b)))
                with self.assertRaises(review_ui.Conflict):
                    self.store.save_review(qid(a), self.pair_correction(a, b), fast_response=True)
                self.assertEqual(self.snapshot((qid(a), qid(b))), prior)
                with closing(sqlite3.connect(self.copy)) as conn:
                    conn.execute(f"UPDATE transcripts SET {column}=? WHERE question_id=?",
                                 (original, qid(b)))
                    conn.commit()

    def test_explicit_pair_wrong_sibling_or_missing_confirmation_cannot_write(self):
        a, b = SHARED_PAIRS[0]
        for shared in (
            {"other_question_id": qid(28), "other_version": 2, "confirm_shared_source": True},
            {"other_question_id": qid(b), "other_version": 0, "confirm_shared_source": False},
            {"other_question_id": qid(b), "other_version": "0", "confirm_shared_source": True},
            {"other_question_id": qid(b), "other_version": 0},
        ):
            with self.subTest(shared=shared):
                prior = self.snapshot((qid(a), qid(b)))
                payload = self.pair_correction(a, b)
                payload["shared_transcript"] = shared
                with self.assertRaises(review_ui.ReviewError):
                    self.store.save_review(qid(a), payload, fast_response=True)
                self.assertEqual(self.snapshot((qid(a), qid(b))), prior)

    def test_postgres_fake_connection_locks_both_pair_questions_in_stable_order(self):
        """Replay actual save_review SQL under a fake PG lock adapter (no server)."""

        class PgSqlOnPrivateSqlite:
            def __init__(self, raw):
                self.raw = raw
                self.calls = []

            def execute(self, sql, args=()):
                self.calls.append((sql, tuple(args)))
                return self.raw.execute(sql.replace(" FOR UPDATE OF q", "")
                                        .replace(" FOR UPDATE", ""), args)

            def executemany(self, sql, rows):
                self.calls.append((sql, tuple(map(tuple, rows))))
                return self.raw.executemany(sql, rows)

        for selected, sibling in ((26, 25), (25, 26)):
            with self.subTest(selected=selected, sibling=sibling):
                self.reset_copy()
                payload = self.pair_correction(selected, sibling)
                with closing(sqlite3.connect(self.copy)) as raw:
                    raw.row_factory = sqlite3.Row
                    probe = PgSqlOnPrivateSqlite(raw)

                    @contextmanager
                    def private_transaction(_question_id):
                        raw.execute("BEGIN IMMEDIATE")
                        try:
                            yield probe
                        except BaseException:
                            raw.rollback()
                            raise
                        else:
                            raw.commit()

                    with patch.object(self.store, "backend", "postgres"), \
                         patch.object(self.store, "_review_write_transaction",
                                      side_effect=private_transaction):
                        result = self.store.save_review(qid(selected), payload,
                                                        fast_response=True)
                self.assertTrue(result["saved"])
                locks = [(sql, params) for sql, params in probe.calls if "FOR UPDATE" in sql]
                self.assertGreaterEqual(len(locks), 2)
                first_query, first_args = locks[0]
                self.assertIn("ORDER BY id FOR UPDATE", first_query)
                self.assertEqual(first_args, tuple(sorted((qid(25), qid(26)))))
                self.assertIn("FOR UPDATE OF q", locks[1][0])
                self.assertEqual(locks[1][1][0], qid(selected))
                first_mutation = next(index for index, (sql, _) in enumerate(probe.calls)
                                      if sql.lstrip().upper().startswith(("UPDATE", "INSERT")))
                self.assertGreaterEqual(first_mutation, 2,
                                        "Pair locks must precede all write statements")

    def test_36_listening_without_proven_pair_can_edit_only_own_transcript(self):
        """Build *only in the temp copy* a 36th listening pair-shaped fixture."""
        files = {
            "paper": "36th-TOPIK-I-Listening-Test-Paper.pdf",
            "answer": "36th-TOPIK-I-Answer-Keys.pdf",
            "transcript": "36th-TOPIK-I-Listening-Transcript.pdf",
        }
        file_ids = {}
        with closing(sqlite3.connect(self.copy)) as conn:
            for kind, filename in files.items():
                path = ROOT / "topik-past-papers" / "36th" / filename
                self.assertTrue(path.is_file(), f"Missing immutable 36th original {path}")
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                relative = f"topik-past-papers/36th/{filename}"
                file_ids[kind] = conn.execute(
                    "INSERT INTO source_files(relative_path,kind,sha256,byte_size) "
                    "VALUES(?,?,?,?) RETURNING id",
                    (relative, "fixture_" + kind, digest, path.stat().st_size),
                ).fetchone()[0]
            conn.execute("INSERT INTO exams(id,session,level,booklet) VALUES('036-I-B',36,'I','B')")
            conn.execute("INSERT INTO sections(id,exam_id,name,first_exam_number,last_exam_number) "
                         "VALUES('036-I-B-listening','036-I-B','listening',1,30)")
            conn.execute("INSERT INTO question_groups(id,section_id,first_exam_number,last_exam_number) "
                         "VALUES('036-I-L-25-26','036-I-B-listening',25,26)")
            for number in (25, 26):
                question_id = f"036-I-L-{number:03d}"
                conn.execute(
                    "INSERT INTO questions(id,section_id,group_id,source_file_id,exam_number,"
                    "answer_key_number,source_pdf_page,points,stem,raw_question_text,extraction_origin) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (question_id, "036-I-B-listening", "036-I-L-25-26", file_ids["paper"],
                     number, number, 9, 2, f"36th number {number}", f"36th raw {number}", "fixture"),
                )
                conn.executemany(
                    "INSERT INTO choices(question_id,number,text) VALUES(?,?,?)",
                    [(question_id, index, f"36th answer {index}") for index in range(1, 5)],
                )
                conn.execute(
                    "INSERT INTO answers(question_id,choice_number,source_file_id,"
                    "source_pdf_page,preview_and_pdf_agree) VALUES(?,?,?,?,?)",
                    (question_id, 1, file_ids["answer"], 1, 1),
                )
                conn.execute(
                    "INSERT INTO transcripts(question_id,source_file_id,source_pdf_page,dialogue_text,"
                    "review_status,warnings_json) VALUES(?,?,?,?,?,?)",
                    (question_id, file_ids["transcript"], 10,
                     "36th mock source dialogue; no verified common-pair contract",
                     "needs_manual_review", "[]"),
                )
            conn.commit()
        other = self.store.for_exam("036-I-B")
        current = other.get_question("036-I-L-025", fast=True)
        before_sibling = other.get_question("036-I-L-026", fast=True)
        payload = {
            "version": current["version"], "status": "verified",
            "stem": current["stem"],
            "choices": [choice["text"] for choice in current["choices"]],
            "transcript_text": current["transcript"]["text"] + " [individual 36th correction]",
            "note": "36th listening is independently reviewed",
        }
        result = other.save_review("036-I-L-025", payload, fast_response=True)
        self.assertTrue(result["saved"])
        self.assertIsNone(result["shared_transcript_updated"])
        self.assertEqual(other.get_question("036-I-L-025", fast=True)["transcript"]["text"],
                         payload["transcript_text"])
        self.assertEqual(other.get_question("036-I-L-026", fast=True)["transcript"],
                         before_sibling["transcript"])
        self.assertEqual(other.get_question("036-I-L-026", fast=True)["version"],
                         before_sibling["version"])
        # A 35th-only pair correction request must not be honored in 36th.
        latest = other.get_question("036-I-L-025", fast=True)
        invalid = dict(payload, version=latest["version"],
                       transcript_text=latest["transcript"]["text"] + " [unsafe pair]",
                       shared_transcript={"other_question_id": "036-I-L-026",
                                          "other_version": before_sibling["version"],
                                          "confirm_shared_source": True})
        with self.assertRaises(review_ui.ReviewError):
            other.save_review("036-I-L-025", invalid, fast_response=True)

    def test_stale_selected_question_version_after_accepted_sibling_review_is_conflict(self):
        a, b = SHARED_PAIRS[0]
        ids = (qid(a), qid(b))
        stale = self.payload(b)
        accepted = dict(stale, stem=stale["stem"] + " [independent accepted correction]",
                        note="Independent original check")
        first = self.store.save_review(qid(b), accepted, fast_response=True)
        self.assertTrue(first["saved"])
        baseline = self.snapshot(ids)
        stale["stem"] += " [stale second tab]"
        with self.assertRaises(review_ui.Conflict):
            self.store.save_review(qid(b), stale, fast_response=True)
        self.assertEqual(self.snapshot(ids), baseline)
        self.assertEqual(self.store.get_question(qid(a), fast=True)["review_status"],
                         "needs_manual_review")
        self.assertEqual(self.store.get_question(qid(a), fast=True)["version"],
                         baseline[qid(a)]["version"])

    def test_nonshared_35th_listening_can_still_edit_own_transcript(self):
        number = 24
        attempt = self.payload(number)
        attempt["transcript_text"] += " [separately reviewed nonshared dialogue]"
        result = self.store.save_review(qid(number), attempt, fast_response=True)
        self.assertTrue(result["saved"])
        self.assertEqual(self.store.get_question(qid(number), fast=True)["transcript"]["text"],
                         attempt["transcript_text"])

    def test_http_ordinary_shared_edit_returns_409_and_does_not_retry(self):
        number, sibling = SHARED_PAIRS[0]
        ids = (qid(number), qid(sibling))
        before = self.snapshot(ids)
        listener = review_ui.ThreadingHTTPServer(
            ("127.0.0.1", 0), review_ui.make_handler(self.store, access_key=None, allow_unauthenticated_test_fixture=True)
        )
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(listener.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(listener.shutdown)
        port = listener.server_port

        def request(method, path, content=None, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=7)
            try:
                conn.request(method, path, content, headers=headers or {})
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()

        status, listing = request("GET", "/api/questions-fast")
        self.assertEqual(status, 200)
        payload = self.payload(number)
        payload["stem"] += " [would be lost if partial commit]"
        payload["transcript_text"] += " [single-side unsafe change]"
        headers = {"Origin": f"http://127.0.0.1:{port}",
                   "X-Review-Token": listing["csrf_token"],
                   "Content-Type": "application/json"}
        with patch.object(self.store, "save_review", wraps=self.store.save_review) as tracked:
            status, body = request("POST", f"/api/questions/{qid(number)}/review?fast=1",
                                   json.dumps(payload).encode("utf-8"), headers)
        self.assertEqual(status, 409, body)
        self.assertEqual(tracked.call_count, 1, "Conflict must not be silently retried")
        self.assertIn("error", body)
        self.assertEqual(self.snapshot(ids), before,
                         "HTTP 409 must have the same rollback guarantees")

    def test_http_verified_sibling_requires_reset_and_acknowledges_explicit_revocation(self):
        """Actual endpoint: 409 without separate consent, then 200 with it."""
        selected, sibling = SHARED_PAIRS[0]
        self.approve_or_reject_sibling(selected, status="verified")
        self.approve_or_reject_sibling(sibling, status="verified")
        ids = (qid(selected), qid(sibling))
        initial = self.snapshot(ids)
        payload = self.pair_correction(selected, sibling, status="verified")
        listener = review_ui.ThreadingHTTPServer(
            ("127.0.0.1", 0), review_ui.make_handler(self.store, access_key=None, allow_unauthenticated_test_fixture=True)
        )
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(listener.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(listener.shutdown)
        port = listener.server_port
        base_path = f"/api/questions/{qid(selected)}/review?fast=1"
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=7)
        try:
            conn.request("GET", "/api/questions-fast")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            token = json.loads(response.read())["csrf_token"]
        finally:
            conn.close()
        headers = {
            "Origin": f"http://127.0.0.1:{port}",
            "X-Review-Token": token,
            "Content-Type": "application/json",
        }

        def post(data):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=7)
            try:
                connection.request("POST", base_path,
                                   json.dumps(data).encode("utf-8"), headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        with patch.object(self.store, "save_review",
                          wraps=self.store.save_review) as tracked:
            status, body = post(payload)
        self.assertEqual(status, 409, body)
        self.assertEqual(tracked.call_count, 1,
                         "A verified sibling conflict must not auto-retry")
        self.assertEqual(self.snapshot(ids), initial)
        payload["shared_transcript"]["confirm_reset_review"] = True
        status, ack = post(payload)
        self.assertEqual(status, 200, ack)
        self.assertTrue(ack["saved"])
        self.assertEqual(ack["review_status"], "verified")
        self.assertEqual(ack["version"], initial[qid(selected)]["version"] + 1)
        self.assertEqual(ack["shared_transcript_updated"], {
            "id": qid(sibling),
            "version": initial[qid(sibling)]["version"] + 1,
            "review_status": "needs_manual_review",
        })
        self.assertEqual(self.store.get_question(qid(sibling), fast=True)["review_status"],
                         "needs_manual_review")
        self.assertEqual(self.store.get_question(qid(sibling), fast=True)
                         ["transcript"]["review_status"], "needs_manual_review")
        self.assertEqual(self.snapshot(ids)[qid(sibling)]["history"][:-1],
                         initial[qid(sibling)]["history"])


if __name__ == "__main__":
    unittest.main()
