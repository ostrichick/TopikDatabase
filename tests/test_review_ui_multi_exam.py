"""Read/query isolation for 35th and 36th TOPIK in a private SQLite fixture."""

from __future__ import annotations

import hashlib
import http.client
import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from src import review_ui


ROOT = Path(__file__).resolve().parents[1]


class MultiExamReviewTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory(prefix="topik-multi-exam-")
        self.addCleanup(self.sandbox.cleanup)
        root = Path(self.sandbox.name)
        self.db_path = root / "two-exams.sqlite"
        self.media_root = root / "topik-past-papers"
        with closing(sqlite3.connect(self.db_path)) as db:
            db.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
            for session in (35, 36):
                number = f"{session:03d}"
                exam = f"{number}-I-B"
                section = f"{number}-I-reading"
                group = f"{number}-I-R-01"
                qid = f"{number}-I-R-001"
                folder = self.media_root / f"{session}th"
                folder.mkdir(parents=True)
                source = folder / f"{number}-paper.pdf"
                source.write_bytes(f"%PDF-1.4\nexam {number}\n%%EOF\n".encode())
                logical = f"topik-past-papers/{session}th/{number}-paper.pdf"
                db.execute(
                    "INSERT INTO source_files(relative_path,kind,sha256,byte_size) VALUES(?,?,?,?)",
                    (logical, "test_paper", hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_size),
                )
                file_id = db.execute(
                    "SELECT id FROM source_files WHERE relative_path=?", (logical,)
                ).fetchone()[0]
                db.execute("INSERT INTO exams(id,session,level,booklet) VALUES(?,?,?,?)",
                           (exam, session, "I", "B"))
                db.execute(
                    "INSERT INTO sections(id,exam_id,name,first_exam_number,last_exam_number) "
                    "VALUES(?,?,?,1,1)", (section, exam, "reading"),
                )
                db.execute(
                    "INSERT INTO question_groups(id,section_id,first_exam_number,last_exam_number,instruction) "
                    "VALUES(?,?,1,1,?)", (group, section, f"{number} instructions"),
                )
                db.execute(
                    "INSERT INTO questions(id,section_id,group_id,source_file_id,exam_number,"
                    "answer_key_number,source_pdf_page,points,stem,raw_question_text,extraction_origin) "
                    "VALUES(?,?,?,?,1,1,1,2,?,?,?)",
                    (qid, section, group, file_id, f"{number} question", f"{number} raw", "fixture"),
                )
                db.executemany(
                    "INSERT INTO choices(question_id,number,text) VALUES(?,?,?)",
                    [(qid, i, f"{number} choice {i}") for i in range(1, 5)],
                )
                db.execute(
                    "INSERT INTO answers(question_id,choice_number,source_file_id,source_pdf_page,"
                    "preview_and_pdf_agree) VALUES(?,1,?,1,1)", (qid, file_id),
                )
                db.execute(
                    "INSERT INTO review_records(subject_type,subject_id,status,scope,evidence) "
                    "VALUES('question',?,'needs_manual_review','fixture','{}')", (qid,),
                )
            db.commit()
        self.default = review_ui.ReviewStore(self.db_path, root=root, media_root=self.media_root)
        self.other = self.default.for_exam("036-I-B")

    def test_store_defaults_to_35_and_filters_list_fast_bundle_and_history(self):
        for store, number in ((self.default, "035"), (self.other, "036")):
            qid = f"{number}-I-R-001"
            self.assertEqual(store.exam_id, f"{number}-I-B")
            for listing in (store.list_questions(), store.list_questions_fast()):
                self.assertEqual(listing["exam_id"], store.exam_id)
                self.assertEqual(listing["counts"]["total"], 1)
                self.assertEqual([item["id"] for item in listing["items"]], [qid])
            bundle = store.get_questions_bundle()
            self.assertEqual(bundle["exam_id"], store.exam_id)
            self.assertEqual(bundle["total_questions"], 1)
            self.assertEqual(set(bundle["questions"]), {qid})
            self.assertEqual(bundle["questions"][qid]["version"], 1)
            detail = store.get_question(qid, fast=True)
            self.assertEqual(detail["exam_id"], store.exam_id)
            self.assertEqual(detail["stem"], f"{number} question")
            self.assertEqual(len(detail["history"]), 1)
            self.assertEqual(len(detail["choices"]), 4)
            if number == "036":
                self.assertIn(f"?exam_id={store.exam_id}", detail["source_pdf_url"])
            else:
                self.assertNotIn("?exam_id=", detail["source_pdf_url"])
        self.assertFalse(self.other.list_questions()["ai_audit_available"])
        comparison = self.other.get_independent_audit_comparison()
        self.assertEqual(comparison["exam_id"], "036-I-B")
        self.assertFalse(comparison["available"])
        self.assertEqual(len(comparison["questions"]), 70)
        self.assertTrue(all(not item["audits"] for item in comparison["questions"].values()))

    def test_cross_exam_access_rejected_and_registered_media_isolated(self):
        with self.assertRaises(review_ui.NotFound):
            self.default.get_question("036-I-R-001", fast=True)
        with self.assertRaises(review_ui.NotFound):
            self.other.get_question("035-I-R-001", fast=True)
        with self.assertRaises(review_ui.NotFound):
            self.default.media_path("036-I-R-001", "paper")
        self.assertEqual(self.other.media_path("036-I-R-001", "paper").parent.name, "36th")
        self.assertEqual(self.default.media_path("035-I-R-001", "paper").parent.name, "35th")
        with self.assertRaises(review_ui.ReviewError):
            self.default.for_exam("036-I-B;DROP TABLE exams")
        with self.assertRaises(review_ui.ReviewError):
            self.other.save_audio_segment("036-I-R-001", {})

    def test_36_raw_source_is_immutable_while_view_text_is_normalized(self):
        qid = "036-I-R-001"
        raw = "네,공책이에요.친구입니다.( ㉠ ) 있습니다.120전화는 무료입니다."
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE questions SET raw_question_text=? WHERE id=?", (raw, qid))
            db.commit()
        expected = "네, 공책이에요. 친구입니다. ( ㉠ ) 있습니다. 120전화는 무료입니다."
        detail = self.other.get_question(qid, fast=True)
        bundle = self.other.get_questions_bundle()["questions"][qid]
        for result in (detail, bundle):
            self.assertEqual(result["raw_question_text"], raw)
            self.assertEqual(result["raw_question_text_display"], expected)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT raw_question_text FROM questions WHERE id=?", (qid,)).fetchone()[0], raw)

    def test_both_35_and_36_preview_new_punctuation_without_mutating_verified_db(self):
        # Historical 35th approvals and 36th v5 rows remain canonical until
        # a deliberate reviewed save or a separately gated migration.
        with closing(sqlite3.connect(self.db_path)) as db:
            for exam in ("035", "036"):
                qid = f"{exam}-I-R-001"
                db.execute("UPDATE questions SET stem=? WHERE id=?",
                           ("그렇습니까?그럼", qid))
                db.execute("UPDATE choices SET text=? WHERE question_id=? AND number=1",
                           ("아!우리", qid))
                db.execute("UPDATE question_groups SET instruction=? WHERE id=?",
                           ("어디입니까?<보기>", f"{exam}-I-R-01"))
            db.commit()
        for store, exam in ((self.default, "035"), (self.other, "036")):
            qid = f"{exam}-I-R-001"
            for item in (store.get_question(qid, fast=True),
                         store.get_questions_bundle()["questions"][qid]):
                self.assertEqual(item["stem"], "그렇습니까?그럼")
                self.assertEqual(item["stem_display"], "그렇습니까? 그럼")
                self.assertEqual(item["choices"][0]["text"], "아!우리")
                self.assertEqual(item["choices"][0]["display_text"], "아! 우리")
                self.assertEqual(item["group"]["instruction"], "어디입니까?<보기>")
                self.assertEqual(item["group"]["instruction_display"], "어디입니까? <보기>")
            with closing(sqlite3.connect(self.db_path)) as db:
                self.assertEqual(db.execute(
                    "SELECT stem FROM questions WHERE id=?", (qid,)).fetchone()[0],
                    "그렇습니까?그럼")

    def test_punctuation_marker_invalidates_open_36_review_form(self):
        qid = "036-I-R-001"
        stale = self.other.get_question(qid, fast=True)
        self.assertEqual(stale["version"], 1)
        baseline_35 = self.default.get_question("035-I-R-001", fast=True)["version"]
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE questions SET stem=? WHERE id=?",
                       ("corrected 36th stem", qid))
            db.execute("INSERT INTO import_metadata(key,value) VALUES(?,?)",
                       ("036-I-B:punctuation:v4-to-v5", "verified fixture"))
            db.commit()
        current = self.other.get_question(qid, fast=True)
        self.assertEqual(current["version"], stale["version"] + 1)
        self.assertEqual(self.other.get_questions_bundle()["questions"][qid]["version"],
                         current["version"])
        fast_item = self.other.list_questions_fast()["items"][0]
        self.assertEqual(fast_item["review_version"], current["version"])
        self.assertEqual(self.default.get_question("035-I-R-001", fast=True)["version"], baseline_35)
        payload = {
            "version": stale["version"], "status": "verified", "stem": stale["stem"],
            "choices": [item["text"] for item in stale["choices"]],
            "transcript_text": None, "note": "opened before correction",
        }
        with self.assertRaises(review_ui.Conflict):
            self.other.save_review(qid, payload, fast_response=True)
        self.assertEqual(self.other.get_question(qid, fast=True)["stem"], "corrected 36th stem")
        payload["version"] = current["version"]
        payload["stem"] = current["stem"]
        result = self.other.save_review(qid, payload, fast_response=True)
        self.assertTrue(result["saved"])
        self.assertEqual(result["version"], current["version"] + 1)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("INSERT INTO import_metadata(key,value) VALUES(?,?)",
                       ("036-I-B:punctuation:v5-to-v6", "verified fixture"))
            db.commit()
        revised = self.other.get_question(qid, fast=True)
        self.assertEqual(revised["version"], result["version"] + 1)
        self.assertEqual(self.other.get_questions_bundle()["questions"][qid]["version"],
                         revised["version"])
        self.assertEqual(self.other.list_questions_fast()["items"][0]["review_version"],
                         revised["version"])
        self.assertEqual(self.default.get_question("035-I-R-001", fast=True)["version"], baseline_35)

    def test_postgres_detail_starts_repeatable_read_before_fetching_text(self):
        # If a migration commits between the detail's SQL statements, a
        # READ COMMITTED GET could send v4 text with the v5 version token.
        class StopAfterSnapshot(Exception):
            pass

        proxy = Mock()
        with patch.object(self.other, "_connect", return_value=proxy), \
             patch.object(self.other, "_question", side_effect=StopAfterSnapshot):
            self.other.backend = "postgres"
            with self.assertRaises(StopAfterSnapshot):
                self.other.get_question("036-I-R-001", fast=True)
        self.assertEqual(
            proxy.execute.call_args_list[0].args[0],
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
        )

    def test_review_write_changes_only_selected_exam(self):
        before_35 = self.default.get_question("035-I-R-001", fast=True)
        before_36 = self.other.get_question("036-I-R-001", fast=True)
        result = self.other.save_review("036-I-R-001", {
            "version": before_36["version"],
            "status": "verified",
            "stem": before_36["stem"],
            "choices": [item["text"] for item in before_36["choices"]],
            "transcript_text": None,
            "note": "Checked against the registered source",
        }, fast_response=True)
        self.assertTrue(result["saved"])
        self.assertEqual(result["review_status"], "verified")
        self.assertEqual(self.other.get_question("036-I-R-001", fast=True)["version"], 2)
        self.assertEqual(self.default.get_question("035-I-R-001", fast=True)["version"], before_35["version"])
        self.assertEqual(self.default.get_question("035-I-R-001", fast=True)["review_status"],
                         before_35["review_status"])
        with self.assertRaises(review_ui.NotFound):
            self.other.save_review("035-I-R-001", {})

    def test_http_explicit_exam_selection_and_media(self):
        server = review_ui.ThreadingHTTPServer(("127.0.0.1", 0), review_ui.make_handler(self.default, access_key=None, allow_unauthenticated_test_fixture=True))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop_server():
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        self.addCleanup(stop_server)
        def request(path):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                conn.request("GET", path)
                response = conn.getresponse()
                return response.status, response.read()
            finally:
                conn.close()

        status, payload = request("/api/questions-fast?exam_id=036-I-B")
        self.assertEqual(status, 200)
        data = json.loads(payload)
        self.assertEqual([item["id"] for item in data["items"]], ["036-I-R-001"])
        status, payload = request("/api/questions-bundle?exam_id=036-I-B")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["exam_id"], "036-I-B")
        status, payload = request("/api/questions/036-I-R-001?exam_id=036-I-B&fast=1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["stem"], "036 question")
        status, payload = request("/media/036-I-R-001/paper?exam_id=036-I-B")
        self.assertEqual(status, 200)
        self.assertIn(b"exam 036", payload)
        status, _ = request("/api/questions/036-I-R-001?fast=1")
        self.assertEqual(status, 404)
        status, _ = request("/media/035-I-R-001/paper?exam_id=036-I-B")
        self.assertEqual(status, 404)
        status, html = request("/")
        self.assertEqual(status, 200)
        self.assertIn(b'id="examSelector"', html)
        self.assertIn(b'value="035-I-B"', html)
        self.assertIn(b'value="036-I-B"', html)


class ExamSelectorScriptTests(unittest.TestCase):
    """Execute the actual inline browser script with a small DOM/fetch mock."""

    @unittest.skipUnless(shutil.which("node"), "Node.js needed for inline JavaScript test")
    def test_selector_reload_and_api_query_scope_with_inflight_fetches(self):
        harness = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
const code = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function openPage(query) {
  const requests = [];
  const navigations = [];
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {
      value: '', listeners: {}, hidden: false, style: {},
      classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
      addEventListener(event, callback) { this.listeners[event] = callback; },
      setAttribute(){}, removeAttribute(){}, replaceChildren(){}, focus(){},
      pause(){}, load(){}, getAttribute(){return null;},
    });
    return nodes.get(id);
  }
  const location = {
    href: 'http://127.0.0.1:8765/' + query,
    search: query,
    hostname: '127.0.0.1',
    protocol: 'http:',
    origin: 'http://127.0.0.1:8765',
    assign(url) { navigations.push(url); },
  };
  const document = {
    title: '', getElementById: node,
    querySelectorAll(){ return []; }, addEventListener(){},
    body: {classList: {contains(){return false;}, toggle(){}}},
  };
  const window = {
    location, addEventListener(){}, confirm(){ return true; },
    matchMedia(){return {matches: false};},
  };
  const context = {
    document, window, location, URL, URLSearchParams, console,
    fetch(url) {requests.push(url); return new Promise(() => {});},
  };
  vm.runInNewContext(code, context, {filename:'review_ui_inline.js'});
  return { requests, navigations, nodes };
}

const defaultPage = openPage('');
assert.equal(defaultPage.nodes.get('examSelector').value, '035-I-B');
assert.equal(defaultPage.requests.length, 3);
assert(defaultPage.requests.every(url => !url.includes('exam_id=')));
const selectDefault = defaultPage.nodes.get('examSelector');
selectDefault.value = '036-I-B';
selectDefault.listeners.change({target: selectDefault});
assert.equal(defaultPage.navigations.length, 1);
assert.equal(new URL(defaultPage.navigations[0]).searchParams.get('exam_id'), '036-I-B');

// Fresh document = brand-new JS state and fetches, not reused bundle promises.
const page36 = openPage('?exam_id=036-I-B');
assert.equal(page36.nodes.get('examSelector').value, '036-I-B');
assert.match(page36.nodes.get('pageHeading').textContent, /36회/);
assert.equal(page36.requests.length, 3);
for (const request of page36.requests) {
  assert.equal(new URL(request, 'http://127.0.0.1:8765').searchParams.get('exam_id'), '036-I-B');
}
const select36 = page36.nodes.get('examSelector');
select36.value = '035-I-B';
select36.listeners.change({target: select36});
assert.equal(new URL(page36.navigations[0]).searchParams.get('exam_id'), '035-I-B');
"""
        completed = subprocess.run(
            ["node", "-e", harness, str(ROOT / "src" / "review_ui.html")],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)


if __name__ == "__main__":
    unittest.main()
