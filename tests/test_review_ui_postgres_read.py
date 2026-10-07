"""Central PostgreSQL reviewer regressions shared by stages 5-8.

These tests exercise the PostgreSQL read branch without requiring a live server
by presenting the preserved SQLite snapshot through PostgreSQL-like mapping
rows. No source database writes are permitted.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from src import ai_audit_35, review_ui


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "topik-past-papers" / "derived" / "035-I-B.sqlite"
MEDIA_ROOT = ROOT / "topik-past-papers"
HTML = ROOT / "src" / "review_ui.html"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StaticCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.index = 0

    def fetchone(self):
        if self.index >= len(self.rows):
            return None
        row = self.rows[self.index]
        self.index += 1
        return row

    def fetchall(self):
        if self.index >= len(self.rows):
            return []
        rows = self.rows[self.index:]
        self.index = len(self.rows)
        return rows

    def __iter__(self):
        return iter(self.rows)


class SQLiteBackedPostgresRead:
    """Mapping-row read double for ReviewStore's PostgreSQL branch."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA query_only=ON")
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if sql.startswith("SET TRANSACTION"):
            return StaticCursor([])
        if "information_schema.tables" in sql:
            return StaticCursor([{"present": True}])
        return self.db.execute(sql, params)

    def close(self):
        self.db.close()


class SQLiteBackedPostgresAudit:
    """Tuple-row double for Stage-7 AI audit reads over the preserved source."""

    backend = "postgres"
    ai_audit_tuple_rows = True

    def __init__(self, _url, *, readonly=False):
        self.db = sqlite3.connect(SOURCE_DB.resolve().as_uri() + "?mode=ro", uri=True)
        self.db.execute("PRAGMA query_only=ON")

    def execute(self, sql, params=()):
        if sql.startswith("SET TRANSACTION"):
            return StaticCursor([])
        if "information_schema.tables" in sql:
            names = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            existing = {row[0] for row in names}
            existing.update(ai_audit_35.AI_AUDIT_TABLES)
            return StaticCursor([(name,) for name in sorted(existing)])
        if "ai_audit_" in sql:
            return StaticCursor([])
        return self.db.execute(sql, params)

    def rollback(self):
        if self.db.in_transaction:
            self.db.rollback()

    def close(self):
        self.db.close()


@unittest.skipUnless(SOURCE_DB.is_file(), "Private 35-I SQLite database is not installed")
class ReviewPostgresReadTests(unittest.TestCase):
    def setUp(self):
        self.before = digest(SOURCE_DB)
        patcher = patch.object(
            review_ui,
            "PostgresReadConnection",
            side_effect=lambda _url: SQLiteBackedPostgresRead(SOURCE_DB),
        )
        self.addCleanup(patcher.stop)
        patcher.start()
        audit_patcher = patch.object(
            ai_audit_35,
            "PostgresAuditConnection",
            side_effect=lambda url, readonly=False: SQLiteBackedPostgresAudit(url, readonly=readonly),
        )
        self.addCleanup(audit_patcher.stop)
        audit_patcher.start()
        self.store = review_ui.ReviewStore(
            root=ROOT,
            database_url="postgresql://fixture.invalid/topik",
            media_root=MEDIA_ROOT,
        )

    def tearDown(self):
        self.assertEqual(digest(SOURCE_DB), self.before)

    def test_central_listing_and_details_match_recovered_pilot(self):
        listing = self.store.list_questions()
        self.assertEqual(self.store.backend, "postgres")
        self.assertFalse(listing["read_only"])
        self.assertEqual(listing["database_backend"], "postgres")
        self.assertEqual(listing["capabilities"], {
            "review_write": True,
            "audio_segment_write": True,
            "clip_export": True,
            "ai_audit_write": False,
        })
        self.assertTrue(listing["ai_audit_available"])
        self.assertEqual(len(listing["items"]), 70)
        self.assertEqual(listing["counts"]["verified"], 2)
        self.assertEqual(listing["counts"]["needs_manual_review"], 68)

        listening = self.store.get_question("035-I-L-001")
        reading = self.store.get_question("035-I-R-031")
        self.assertEqual(listening["section"], "listening")
        self.assertIsNotNone(listening["transcript"])
        self.assertIsNotNone(listening["audio_segment"])
        self.assertEqual(reading["section"], "reading")
        self.assertIsNone(reading["transcript"])
        self.assertNotIn("ai_audit", listening)

    def test_bundle_uses_one_repeatable_read_snapshot_and_skips_per_question_audit_details(self):
        connections = []

        def factory(_url):
            connection = SQLiteBackedPostgresRead(SOURCE_DB)
            connections.append(connection)
            return connection

        with patch.object(review_ui, "PostgresReadConnection", side_effect=factory), \
                patch.object(self.store, "_get_ai_audit_for_question",
                             side_effect=AssertionError("N+1 audit detail read")):
            bundle = self.store.get_questions_bundle()
        self.assertEqual(bundle["total_questions"], 70)
        self.assertTrue(connections)
        self.assertEqual(
            connections[-1].statements[0][0],
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
        )

    def test_local_media_root_and_blob_images_remain_local(self):
        paper = self.store.media_path("035-I-L-001", "paper")
        audio = self.store.media_path("035-I-L-001", "audio")
        self.assertTrue(paper.is_file())
        self.assertTrue(audio.is_file())
        self.assertTrue(paper.is_relative_to(MEDIA_ROOT / "35th"))
        self.assertTrue(audio.is_relative_to(MEDIA_ROOT / "35th"))

        with sqlite3.connect(SOURCE_DB.resolve().as_uri() + "?mode=ro", uri=True) as db:
            qid = db.execute(
                "SELECT question_id FROM question_images ORDER BY question_id LIMIT 1"
            ).fetchone()[0]
        payload, mime = self.store.get_image(qid, 0)
        self.assertEqual(mime, "image/png")
        self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_postgres_clip_export_is_stage8_enabled_but_still_verified_only(self):
        with self.assertRaisesRegex(review_ui.ReviewError, "Verify every linked interval"):
            self.store.export_audio_clip("035-I-L-001")

    def test_environment_selects_postgres_but_explicit_sqlite_path_wins(self):
        with patch.dict(os.environ, {"TOPIK_DATABASE_URL": "postgresql://fixture.invalid/topik"}):
            central = review_ui.ReviewStore(root=ROOT, media_root=MEDIA_ROOT)
            local = review_ui.ReviewStore(SOURCE_DB, root=ROOT)
        self.assertEqual(central.backend, "postgres")
        self.assertEqual(local.backend, "sqlite")

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_browser_enables_review_controls_but_keeps_clip_export_capability_separate(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
function extractFunction(name) {
  const start = html.indexOf('      function ' + name + '(');
  assert.ok(start >= 0, 'Could not locate ' + name);
  const brace = html.indexOf('{', start);
  let depth = 0, quote = null, escaped = false;
  for (let i = brace; i < html.length; i++) {
    const ch = html[i];
    if (quote) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === quote) quote = null;
      continue;
    }
    if (ch === "'" || ch === '"' || ch.charCodeAt(0) === 96) { quote = ch; continue; }
    if (ch === '{') depth++;
    else if (ch === '}' && --depth === 0) return html.slice(start, i + 1);
  }
  throw new Error('Unterminated function ' + name);
}
const controls = {
  saveDraft: {disabled:false}, approveQuestion:{disabled:false}, rejectQuestion:{disabled:false},
};
const context = {
  state: {readOnly:false, capabilities:{reviewWrite:true,audioSegmentWrite:true,clipExport:false}, saving:false, loading:false, detail:{id:'q1'}, audio:{saving:false}},
  $: id => controls[id],
  updateNavigation: () => {},
  refreshAudioActions: () => {},
};
const source = extractFunction('setLoading') + '\nsetLoading;';
const setLoading = vm.runInNewContext(source, context);
setLoading(false);
assert.equal(controls.saveDraft.disabled, false);
assert.equal(controls.approveQuestion.disabled, false);
assert.equal(controls.rejectQuestion.disabled, false);
console.log('STAGE 6 REVIEW CONTROLS PASS');
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(HTML)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
