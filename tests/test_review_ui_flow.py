"""Exercise the actual approval handler without editing the user's SQLite database.

Node executes only the sendReview function extracted from the local HTML. All
requests, fields and navigation are mocked; no browser or server is launched.
"""

import shutil
import subprocess
import unittest
from pathlib import Path


HTML = Path(__file__).resolve().parents[1] / "src" / "review_ui.html"

NODE_CHECK = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(process.argv[1], 'utf8');
const start = html.indexOf('      async function sendReview(status) {');
const end = html.indexOf('      function neighbor(delta) {', start);
assert.ok(start >= 0 && end > start, 'Could not locate the real approval handler');
const handler = '(' + html.slice(start, end).trim() + ')';

async function scenario(status, { fail = false, last = false } = {}) {
  const rows = last ? [{ id: 'q70', number: 70, status: 'needs_manual_review' }]
                    : [{ id: 'q1', number: 1, status: 'needs_manual_review' },
                       { id: 'q2', number: 2, status: 'needs_manual_review' }];
  const first = rows[0];
  const state = {
    detail: { id: first.id, number: first.number, version: 0 },
    selectedId: first.id, loading: false, saving: false, items: rows, csrfToken: 'test',
  };
  const events = [];
  const context = {
    state,
    draft: () => ({ stem: 'draft stays intact', choices: ['', '', '', ''],
                    transcript_text: null, note: status === 'rejected' ? 'reason' : '' }),
    $: () => ({ focus: () => events.push('focus') }),
    notice: (message, type) => events.push(['notice', type, message]),
    window: { confirm: () => {
      events.push('confirm');
      if (status === 'verified') throw Error('Approval must not ask for confirmation');
      return true;
    } },
    setLoading: () => {},
    request: async (url) => {
      events.push('post');
      if (fail) throw new Error('Simulated failed save');
      return { ...state.detail, review_status: status, choices: [], requires_image: false };
    },
    renderDetail: () => events.push('render'),
    loadList: async () => events.push('load'),
    selectQuestion: async (id) => {
      assert.equal(state.saving, false, 'Navigate only after the save has finished');
      events.push('navigate');
      state.selectedId = id;
      state.detail = { id, number: 2, version: 0 };
    },
  };
  const sendReview = vm.runInNewContext(handler, context);
  await sendReview(status);
  return { state, events };
}

(async () => {
  let result = await scenario('verified');
  assert.equal(result.state.selectedId, 'q2');
  assert.equal(result.state.items[0].status, 'verified');
  assert.equal(result.events.filter(item => item === 'post').length, 1);
  assert.equal(result.events.includes('confirm'), false);
  assert.ok(result.events.indexOf('load') < result.events.indexOf('navigate'));

  result = await scenario('verified', { fail: true });
  assert.equal(result.state.selectedId, 'q1');
  assert.equal(result.state.detail.id, 'q1');
  assert.equal(result.events.includes('navigate'), false);
  assert.equal(result.events.some(item => Array.isArray(item) && item[1] === 'error'), true);

  result = await scenario('verified', { last: true });
  assert.equal(result.state.selectedId, 'q70');
  assert.equal(result.events.includes('navigate'), false);

  result = await scenario('needs_manual_review');
  assert.equal(result.state.selectedId, 'q1');
  assert.equal(result.events.includes('navigate'), false);

  result = await scenario('rejected');
  assert.equal(result.state.selectedId, 'q1');
  assert.equal(result.events.filter(item => item === 'confirm').length, 1);
  assert.equal(result.events.includes('navigate'), false);
  console.log('Approval flow: success, failed save, final question, draft, rejection PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


class TestReviewUIFlow(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_approval_automatically_advances_only_after_success(self):
        result = subprocess.run(
            [shutil.which("node"), "-e", NODE_CHECK, str(HTML)],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
