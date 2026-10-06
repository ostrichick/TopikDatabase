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

NODE_AI_FILTER_CHECK = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(process.argv[1], 'utf8');
function extractFunction(name) {
  const start = html.indexOf('      function ' + name + '(');
  assert.ok(start >= 0, 'Could not locate ' + name);
  const brace = html.indexOf('{', start);
  let depth = 0;
  let quote = null;
  let escaped = false;
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

function fakeNode() {
  return {
    children: [], className: '', textContent: '', hidden: false, value: '',
    classList: { toggle() {} },
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = [...nodes]; },
    setAttribute() {},
    addEventListener() {},
  };
}

const controls = {
  questionSearch: Object.assign(fakeNode(), { value: '' }),
  sectionFilter: Object.assign(fakeNode(), { value: 'all' }),
  statusFilter: Object.assign(fakeNode(), { value: 'all' }),
  imageFilter: Object.assign(fakeNode(), { value: 'all' }),
  aiFilter: Object.assign(fakeNode(), { value: 'all' }),
  sortOrder: Object.assign(fakeNode(), { value: 'risk' }),
  listCount: fakeNode(),
  questionList: fakeNode(),
};
const state = {
  selectedId: null,
  visible: [],
  items: [
    { id: 'q1', number: 1, section: 'listening', status: 'needs_manual_review', requires_image: false,
      ai_audit: { total: 3, finding: 2, uncertain: 0, unresolved_findings: 2, disagreement: true,
                  risk_score: 86, risk_level: 'high', latest_at: '2026-10-05T02:03:00Z' } },
    { id: 'q2', number: 2, section: 'reading', status: 'verified', requires_image: false,
      ai_audit: { total: 3, finding: 1, uncertain: 1, unresolved_findings: 0, disagreement: true,
                  risk_score: 45, risk_level: 'medium', latest_at: '2026-10-05T02:02:00Z' } },
    { id: 'q3', number: 3, section: 'reading', status: 'verified', requires_image: true },
  ],
};
const context = {
  state,
  $: id => controls[id],
  document: { createElement: () => fakeNode() },
  SECTION_NAMES: { listening: '듣기', reading: '읽기' },
  AI_RISK_NAMES: { low: '낮음', medium: '중간', high: '높음' },
  statusName: value => value,
  selectQuestion: () => {},
};
const names = ['aiAudit', 'aiNumber', 'aiRisk', 'aiTimestamp', 'element', 'filterItems'];
const source = names.map(extractFunction).join('\n') + '\nfilterItems;';
const filterItems = vm.runInNewContext(source, context);

filterItems();
assert.deepEqual(Array.from(state.visible, item => item.id), ['q1', 'q2', 'q3']);
controls.aiFilter.value = 'unresolved';
filterItems();
assert.deepEqual(Array.from(state.visible, item => item.id), ['q1']);
controls.aiFilter.value = 'uncertain';
filterItems();
assert.deepEqual(Array.from(state.visible, item => item.id), ['q2']);
controls.aiFilter.value = 'high';
filterItems();
assert.deepEqual(Array.from(state.visible, item => item.id), ['q1']);
controls.aiFilter.value = 'unaudited';
filterItems();
assert.deepEqual(Array.from(state.visible, item => item.id), ['q3']);
controls.aiFilter.value = 'all';
controls.sortOrder.value = 'findings';
filterItems();
assert.deepEqual(Array.from(state.visible, item => item.id), ['q1', 'q2', 'q3']);
console.log('AI audit filters and sorting PASS');
"""

NODE_AI_RENDER_CHECK = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(process.argv[1], 'utf8');
function extractFunction(name) {
  const start = html.indexOf('      function ' + name + '(');
  assert.ok(start >= 0, 'Could not locate ' + name);
  const brace = html.indexOf('{', start);
  let depth = 0;
  let quote = null;
  let escaped = false;
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

function fakeNode(tag = 'div') {
  return {
    tagName: tag.toUpperCase(), children: [], className: '', textContent: '', hidden: false,
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = [...nodes]; },
  };
}
function collect(node) {
  return [node.textContent || '', ...node.children.map(collect)].filter(Boolean).join('\n');
}
function findTags(node, tagName, found = []) {
  if (node.tagName === tagName.toUpperCase()) found.push(node);
  for (const child of node.children) findTags(child, tagName, found);
  return found;
}

const controls = {
  aiAuditSection: fakeNode('section'),
  aiAuditSummary: fakeNode('div'),
  aiHistorySection: fakeNode('section'),
  aiHistorySummary: fakeNode('div'),
  aiRunHistory: fakeNode('div'),
  aiAttemptLog: fakeNode('ol'),
  aiAuditLog: fakeNode('ol'),
};
const malicious = '<script>alert("not html")</script>';
const state = { detail: { ai_audit: {
  total: 1, clear: 0, finding: 1, uncertain: 0, unresolved_findings: 1,
  disagreement: false, risk_score: 65, risk_level: 'medium',
  convergence: { completed_passes: 1, new_findings_latest_pass: 1, converged: false },
  latest_run: { id: 'run-1', pass_total: 1, completed_passes: 1, created_at: '2026-10-05T03:00:00Z' },
  attempt_total: 4,
  attempt_status_counts: { succeeded: 1, failed: 1, timed_out: 1, invalid: 1 },
  retry_count: 3, incomplete_passes: 0,
  history: {
    run_count: 2, audit_count: 3,
    verdict_counts: { clear: 2, finding: 1, uncertain: 0 },
    disagreement_run_count: 1, latest_run_finding_count: 1,
    historical_findings: [
      { summary: 'mismatch', severity: 'high', latest_state: 'present_in_latest_run' },
      { summary: 'old mismatch', severity: 'medium', latest_state: 'not_reproduced_in_latest_run' },
      { summary: 'returning mismatch', severity: 'medium', latest_state: 'reappeared_in_latest_run' },
    ],
    runs: [
      { run_id:'run-1', label:'latest run', created_at:'2026-10-05T03:00:00Z',
        summary:{ total:1, totals:{clear:0,finding:1,uncertain:0}, disagreement:false,
          entries:[{findings:[{evidence:{unsafe_text:malicious}}]}] },
        execution:{ latest_run:{pass_total:1,completed_passes:1,attempt_total:4}, passes:[] } },
      { run_id:'run-0', label:'older completed run', created_at:'2026-10-04T03:00:00Z',
        summary:{ total:2, totals:{clear:2,finding:0,uncertain:0}, disagreement:false },
        execution:{ latest_run:{pass_total:2,completed_passes:2,attempt_total:2}, passes:[] } },
    ],
  },
  execution: { passes: [{
    id: 'pass-a', pass_number: 1, state: 'complete', auditor_id: 'auditor-a', model_id: 'model-a',
    prompt_version: 'v1', perspective: 'independent', completed_subject_count: 70, pending_subject_count: 0,
    attempts: [
      { attempt_number: 1, status: 'failed', error_code: 'provider_error', error_message: '503',
        evidence: { http_status: 503, unsafe_text: malicious }, raw_response: { body: malicious } },
      { attempt_number: 2, status: 'timed_out', error_code: 'deadline', error_message: '120s', evidence: { timeout_seconds: 120 } },
      { attempt_number: 3, status: 'invalid', error_code: 'invalid_structured_response', error_message: 'bad shape', evidence: { parser: 'json' } },
      { attempt_number: 4, status: 'succeeded', result_id: 9, evidence: { retry_reason: 'corrected' } },
    ],
  }] },
  entries: [{ pass_id: 'pass-a', pass_number: 1, auditor_id: 'auditor-a', verdict: 'finding', confidence: 0.9,
    rationale: 'check answer', findings: [{ category: 'answer', severity: 'high', summary: 'mismatch', detail: 'detail',
      identity: { field: 'answer.choice_number' }, evidence: { observed: 1, expected: 2, unsafe_text: malicious } }] }],
} } };
const context = {
  state,
  $: id => controls[id],
  document: { createElement: tag => fakeNode(tag) },
  AI_ATTEMPT_NAMES: { succeeded: '성공', failed: '실패', timed_out: '시간 초과', invalid: '잘못된 응답' },
  AI_VERDICT_NAMES: { clear: '이상 없음', finding: '결함 발견', uncertain: '불확실' },
  AI_RISK_NAMES: { none: '없음', low: '낮음', medium: '중간', high: '높음', critical: '매우 높음' },
};
const names = ['aiAudit', 'aiNumber', 'aiRisk', 'aiConvergenceText', 'element', 'aiStructuredDetails', 'renderAiAudit'];
const source = names.map(extractFunction).join('\n') + '\nrenderAiAudit;';
const renderAiAudit = vm.runInNewContext(source, context);
renderAiAudit();

assert.equal(controls.aiAuditSection.hidden, false);
const summaryText = collect(controls.aiAuditSummary);
assert.match(summaryText, /최근 실행 verdict/);
assert.match(summaryText, /1회 · 이상 없음 0 · 발견 1 · 불확실 0/);
assert.match(summaryText, /최근 실행 attempt/);
assert.match(summaryText, /4회 · 성공 1 · 실패 1 · 시간초과 1 · 잘못된 응답 1 · 재시도 3/);
assert.equal(controls.aiHistorySection.hidden, false);
const historySummaryText = collect(controls.aiHistorySummary);
assert.match(historySummaryText, /전체 누적 verdict/);
assert.match(historySummaryText, /3회 · 이상 없음 2 · 발견 1 · 불확실 0/);
const historyText = collect(controls.aiRunHistory);
assert.match(historyText, /older completed run/);
assert.match(historyText, /최신 실행에서 재현되지 않음 · 해결 판정 아님/);
assert.match(historyText, /최신 실행에서 다시 확인됨 · 중간 비재현은 해결 판정 아님/);
assert.match(historyText, /이 회차 전체 감사 기록 보기/);
const attemptText = collect(controls.aiAttemptLog);
assert.match(attemptText, /시도 1 · 실패/);
assert.match(attemptText, /시도 2 · 시간 초과 · 재시도/);
assert.match(attemptText, /시도 3 · 잘못된 응답 · 재시도/);
assert.match(attemptText, /시도 4 · 성공 · 재시도/);
assert.match(attemptText, /"timeout_seconds": 120/);
assert.match(attemptText, /"retry_reason": "corrected"/);
const findingText = collect(controls.aiAuditLog);
assert.match(findingText, /finding 식별 근거 전체 보기/);
assert.match(findingText, /finding 증거 전체 보기/);
assert.match(findingText, /"observed": 1/);
assert.match(findingText, /"expected": 2/);
const preText = [...findTags(controls.aiAttemptLog, 'pre'), ...findTags(controls.aiAuditLog, 'pre'), ...findTags(controls.aiRunHistory, 'pre')]
  .map(node => node.textContent).join('\n');
assert.ok(preText.includes('<script>alert(') && preText.includes('not html') && preText.includes('</script>'),
  'Structured evidence must remain literal textContent');
console.log('AI audit execution and evidence rendering PASS');
"""

NODE_FILTER_SELECTION_CHECK = r"""
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
function fakeNode() {
  return {
    children: [], className: '', textContent: '', value: '', hidden: false,
    classList: { toggle() {} },
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = [...nodes]; },
    setAttribute() {}, addEventListener() {},
  };
}
const controls = {
  questionSearch: Object.assign(fakeNode(), { value: '' }),
  sectionFilter: Object.assign(fakeNode(), { value: 'all' }),
  statusFilter: Object.assign(fakeNode(), { value: 'all' }),
  imageFilter: Object.assign(fakeNode(), { value: 'all' }),
  aiFilter: Object.assign(fakeNode(), { value: 'unresolved' }),
  sortOrder: Object.assign(fakeNode(), { value: 'number' }),
  listCount: fakeNode(), questionList: fakeNode(),
};
const state = {
  selectedId: 'q1', visible: [], saving: false, loading: false,
  audio: { saving: false },
  items: [
    { id:'q1', number:1, section:'reading', status:'verified', requires_image:false,
      ai_audit:{ total:2, unresolved_findings:0, risk_score:0, risk_level:'none' } },
    { id:'q31', number:31, section:'reading', status:'needs_manual_review', requires_image:false,
      ai_audit:{ total:2, finding:1, unresolved_findings:1, disagreement:true, risk_score:80, risk_level:'high' } },
  ],
};
const events=[];
const context = {
  state,
  $: id => controls[id],
  document: { createElement: () => fakeNode() },
  SECTION_NAMES:{reading:'읽기'}, AI_RISK_NAMES:{none:'없음',high:'높음'},
  statusName:v=>v, aiAudit:item=>item?.ai_audit||null, aiNumber:v=>Number(v)||0,
  aiRisk:a=>({score:Number(a?.risk_score)||0,level:a?.risk_level||'none'}), aiTimestamp:()=>0,
  element:(tag,className,text)=>Object.assign(fakeNode(),{className,textContent:text===undefined?'':String(text)}),
  canNavigate:()=>true,
  notice:(msg,type)=>events.push(['notice',type,msg]),
  selectQuestion:async(id,opts)=>{events.push(['select',id,opts?.force]);state.selectedId=id;},
};
const source = extractFunction('filterItems') + '\n' + extractFunction('filterChanged') + '\nfilterChanged;';
const filterChanged = vm.runInNewContext(source, context);
(async()=>{
  await filterChanged();
  assert.deepEqual(Array.from(state.visible, x=>x.id), ['q31']);
  assert.equal(state.selectedId, 'q31');
  assert.deepEqual(events, [['select','q31',true]]);

  state.selectedId='q1'; events.length=0; context.canNavigate=()=>false;
  // Re-evaluate so the function closes over the updated canNavigate binding.
  const blocked = vm.runInNewContext(source, context);
  await blocked();
  assert.equal(state.selectedId, 'q1');
  assert.equal(events[0][0], 'notice');
  assert.match(events[0][2], /자동 이동하지 않았습니다/);
  console.log('Filter/detail selection synchronization PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
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

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_ai_audit_filters_and_sorting_prioritize_review_risk(self):
        result = subprocess.run(
            [shutil.which("node"), "-e", NODE_AI_FILTER_CHECK, str(HTML)],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_ai_audit_renders_attempt_failures_retries_and_full_structured_evidence(self):
        result = subprocess.run(
            [shutil.which("node"), "-e", NODE_AI_RENDER_CHECK, str(HTML)],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_filter_change_keeps_visible_list_and_detail_selection_in_sync(self):
        result = subprocess.run(
            [shutil.which("node"), "-e", NODE_FILTER_SELECTION_CHECK, str(HTML)],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
