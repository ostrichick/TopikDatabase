"""Render extraction warnings from the actual reviewer JavaScript without DB writes."""

import shutil
import subprocess
import unittest
from pathlib import Path


HTML = Path(__file__).resolve().parents[1] / "src" / "review_ui.html"

NODE_WARNING_CHECK = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
function functionSource(first, next) {
  const start = html.indexOf('      function ' + first + '(');
  const end = html.indexOf('      function ' + next + '(', start + 1);
  assert.ok(start >= 0 && end > start, `${first} not found in reviewer HTML`);
  return html.slice(start, end);
}
const js = functionSource('element', 'aiStructuredDetails') + '\n' +
           functionSource('renderReferences', 'renderHistory');

class FakeElement {
  constructor(tagName = 'div') {
    this.tagName = tagName;
    this.className = '';
    this.attributes = {};
    this.children = [];
    this._text = '';
    this.hidden = false;
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; this._text = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(_value) { throw Error('HTML injection through innerHTML'); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  pause() {}
  load() {}
}

function render(question) {
  const ids = ['flagList','imageList','imageSection','audioSection','audioPlayer','audioFullHint'];
  const nodes = Object.fromEntries(ids.map(id => [id, new FakeElement()]));
  const createdTags = [];
  const context = {
    document: { createElement: tag => { createdTags.push(tag); return new FakeElement(tag); } },
    state: { detail: question, audio: { preview: null } },
    $: id => { assert.ok(nodes[id], `Unexpected DOM id: ${id}`); return nodes[id]; },
    renderPdf: () => {},
    renderAudioSegment: () => {},
    refreshAudioActions: () => {},
    sourceWithSharedAudio: () => null,
    sameOriginURL: () => null,
  };
  vm.runInNewContext(js, context);
  context.renderReferences();
  return { rows: nodes.flagList.children, text: nodes.flagList.textContent, createdTags };
}

function item(scope, severity, code, message) {
  return { severity, code, message, scope };
}
const unsafe = '<img src=x onerror="globalThis.executed=true"><script>bad()</script>';
const longText = '원본 대비 오류 확인 '.repeat(1000);
const question = { section: 'listening', images: [], audio_url: null,
  preview_flags: [
    { severity: 'review', code: 'question_markup', message: '문항 출처 확인' },
    '기존 문자열 경고',
    { severity: 'high', code: 'html_candidate', message: unsafe },
    { severity: 'blocking', code: 'long_warning', message: longText },
    { severity: 'info', message: '' }, null, 55,
  ],
  transcript: { warnings: [
    { severity: 'medium', code: 'transcript_mismatch', message: '대본 대조 필요' },
  ] },
};
const output = render(question);
assert.equal(output.rows.length, 8, 'Render both question and listening transcript warnings');
assert.doesNotMatch(output.text, /\[object Object\]|undefined|null/);
assert.ok(output.text.includes('문항 출처 확인'));
assert.ok(output.text.includes('기존 문자열 경고'));
assert.ok(output.text.includes('대본 대조 필요'));
assert.ok(output.text.includes(unsafe), 'Show markup literally as text');
assert.ok(output.text.includes(longText), 'Do not truncate a long warning');
assert.ok(!output.createdTags.includes('img') && !output.createdTags.includes('script'));
assert.match(output.rows[0].textContent, /문항/);
assert.match(output.rows[0].textContent, /검토|review/);
assert.match(output.rows[0].textContent, /question_markup/);
assert.match(output.rows[7].textContent, /듣기 대본/);
assert.match(output.rows[7].textContent, /transcript_mismatch/);
assert.ok(output.rows.every(row => row.attributes.role === 'listitem'), 'Semantic warning list items');
assert.ok(output.rows[2].className.includes('flag--strong'), 'High severity is visually distinct');
assert.ok(output.rows[4].textContent.includes('확인할 수 없'), 'Malformed warning has safe fallback');

let empty = render({ section: 'listening', images: [], preview_flags: [], transcript: { warnings: [] } });
assert.equal(empty.rows.length, 1);
assert.match(empty.text, /제공된 추출 주의사항이 없습니다/);
empty = render({ section: 'listening', images: [], preview_flags: {},
  transcript: { warnings: ['대본 문자열 경고'] } });
assert.equal(empty.rows.length, 1);
assert.match(empty.rows[0].textContent, /듣기 대본/);
empty = render({ section: 'reading', images: [], preview_flags: [],
  transcript: { warnings: [item('transcript', 'high', 'wrong_scope', '읽기에 노출하면 안 됨')] } });
assert.equal(empty.rows.length, 1);
assert.doesNotMatch(empty.text, /wrong_scope/);

assert.match(html, /id="flagList"[^>]*role="list"/, 'Warning list must expose accessible list semantics');
assert.match(html, /\.flag-message[^}]*overflow-wrap:\s*anywhere/, 'Long warnings must wrap on mobile');
assert.match(html, /\.flag-message[^}]*white-space:\s*pre-wrap/, 'Warning line breaks must remain readable');
console.log('Structured/string warning rendering, transcript scope, malformed data, escaping, mobile and a11y PASS');
"""


@unittest.skipUnless(shutil.which("node"), "Node.js required for reviewer DOM tests")
class WarningRenderingTests(unittest.TestCase):
    def test_real_reference_warning_rendering(self):
        result = subprocess.run(
            [shutil.which("node"), "-e", NODE_WARNING_CHECK, str(HTML)],
            capture_output=True, text=True, encoding="utf-8", timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
