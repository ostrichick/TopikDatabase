"""Shared PDF/MP3 URL regression tests for 35th and 36th reviewer navigation."""

from pathlib import Path
import shutil
import subprocess
import unittest


HTML = Path(__file__).resolve().parents[1] / "src" / "review_ui.html"


@unittest.skipUnless(shutil.which("node"), "Node.js required for browser helper tests")
class ReviewMediaURLTests(unittest.TestCase):
    def test_source_urls_are_stable_within_actual_shared_pdf_and_audio(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
function source(start, end) {
  const a = html.indexOf('      function ' + start + '(');
  const b = html.indexOf('      function ' + end + '(', a + 1);
  assert.ok(a !== -1 && b > a, start + ' missing');
  return html.slice(a, b);
}
const origin = 'http://127.0.0.1:18736';
const window = { location: { href: origin + '/?exam_id=036-I-B', origin } };
const context = { window, URL };
vm.runInNewContext(source('sameOriginURL','sourceWithPage') +
  source('sourceWithPage','sourceWithSharedAudio') +
  source('sourceWithSharedAudio','statusName'), context);
const pdf = context.sourceWithPage;
const audio = context.sourceWithSharedAudio;
function p(q,kind,page) {
  return pdf('/media/' + q + '/' + kind + '?exam_id=036-I-B', page);
}
function base(value) { return value.split('#')[0]; }
assert.equal(base(p('036-I-L-001','paper',1)),base(p('036-I-L-030','paper',8)));
assert.equal(base(p('036-I-R-031','paper',1)),base(p('036-I-R-070','paper',17)));
assert.notEqual(base(p('036-I-L-001','paper',1)),base(p('036-I-R-031','paper',1)),
  'Listening and reading question papers must never be conflated');
assert.equal(base(p('036-I-L-001','answer',1)),base(p('036-I-R-070','answer',2)));
assert.equal(base(p('036-I-L-001','transcript',1)),base(p('036-I-L-030','transcript',12)));
assert.ok(p('036-I-R-070','paper',17).endsWith('?exam_id=036-I-B#page=17'));
assert.equal(audio('/media/036-I-L-001/audio?exam_id=036-I-B').href,
  audio('/media/036-I-L-030/audio?exam_id=036-I-B').href);
assert.equal(audio('/media/035-I-L-025/audio').href,
  audio('/media/035-I-L-026/audio').href);
assert.notEqual(audio('/media/035-I-L-025/audio').href,
  audio('/media/036-I-L-025/audio?exam_id=036-I-B').href);
assert.equal(audio('/media/036-I-L-030/audio?exam_id=036-I-B').pathname,
  '/media/036-I-L-001/audio');
assert.ok(base(pdf('/media/035-I-R-032/paper',4)).endsWith('/media/035-I-L-001/paper'));
assert.equal(audio('http://malicious.example/asset'),null,'No off-origin media');
assert.equal(pdf('http://malicious.example/asset',1),null,'No off-origin PDF');
assert.equal(audio('//malicious.example/media'),null);
assert.equal(audio('/media/036-I-R-031/audio?exam_id=036-I-B').pathname,
  '/media/036-I-R-031/audio', 'Do not canonicalize a non-listening audio path');
const first = audio('/media/036-I-L-001/audio?exam_id=036-I-B').href;
const second = audio('/media/036-I-L-002/audio?exam_id=036-I-B').href;
assert.equal(first,second,'Browser player should not load again when URL is unchanged');
assert.match(html,/if \(shouldReload\)\s*\{[\s\S]*?player\.load\(\)/,
  'Player should only load after media URL changes');
console.log('Shared PDF + MP3 URL scope and stable player source PASS');
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(HTML)],
            capture_output=True, text=True, encoding="utf-8", timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
