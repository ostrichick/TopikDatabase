"""Exercise real frontend approval and navigation functions without DB writes.

Node extracts functions from the local HTML and runs them with mocked requests,
editor elements and browser controls. No browser or server is launched.
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

async function scenario(status, { fail = false, last = false, slowList = false } = {}) {
  const rows = last ? [{ id: 'q70', number: 70, status: 'needs_manual_review' }]
                    : [{ id: 'q1', number: 1, status: 'needs_manual_review' },
                       { id: 'q2', number: 2, status: 'needs_manual_review' }];
  const first = rows[0];
  const state = {
    detail: { id: first.id, number: first.number, version: 0 },
    selectedId: first.id, loading: false, saving: false, items: rows, csrfToken: 'test',
    detailsCache: {}, bundleProtectedIds: new Set(),
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
    verifyReviewAck: (result,id,wanted,submitted) => {
      assert.equal(result?.id,id);
      assert.equal(result.review_status,wanted);
      assert.equal(result.request_version,submitted.version);
      assert.equal(result.version,submitted.version + Number(result.saved));
      return result;
    },
    reviewWritesBlocked: () => false,
    sharedTranscriptChanged: () => false,
    invalidateSharedTranscriptCache: () => {},
    audioDirty: () => false,
    counts: () => events.push('counts'),
    filterItems: () => events.push('filter'),
    request: async (url) => {
      events.push('post');
      if (fail) throw new Error('Simulated failed save');
      return { id: state.detail.id, number: state.detail.number, review_status: status,
               request_version:state.detail.version, version: state.detail.version + 1, requires_image: false, saved: true };
    },
    renderDetail: () => events.push('render'),
    loadList: () => {
      events.push('load');
      return slowList ? new Promise(() => {}) : Promise.resolve();
    },
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
  assert.equal(result.state.detailsCache.q1.version, 1);
  assert.equal(result.state.detailsCache.q1.stem, 'draft stays intact');

  result = await scenario('verified', { slowList: true });
  assert.equal(result.state.selectedId, 'q2', 'Background list refresh must not delay navigation');

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

NODE_REAL_NAVIGATION_CHECK = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
function source(from, until) {
  const start = html.indexOf('      ' + from);
  const end = html.indexOf('      ' + until, start);
  assert.ok(start >= 0 && end > start, 'Missing real function: ' + from);
  return html.slice(start, end);
}
const functions = [
  source('function verifyReviewAck(', 'function matchesReviewInput('),
  source('function sharedTranscriptChanged(', 'function aiAudit('),
  source('function draft() {', 'function audioSegment() {'),
  source('function audioSegment() {', 'function seconds('),
  source('function audioDirty() {', 'function audioFeedback('),
  source('function canNavigate() {', 'function renderReviewSync() {'),
  source('async function selectQuestion(', 'function badge('),
  source('async function sendReview(status) {', 'function neighbor(')
].join('\n');

async function scenario({ listening = false, edited = true, post = 'ok',
                          audioUnsaved = false, changeDuringPost = false,
                          confirm = false, filtered = false, last = false } = {}) {
  const q = (id, number) => ({
    id, number, version:0, review_status:'needs_manual_review',
    section:listening ? 'listening' : 'reading',
    stem:'old', choices:['a','b','c','d'].map((text, index) => ({ number:index+1, text })),
    transcript:listening ? { text:'transcript' } : null,
    audio_segment:null
  });
  const rows = (last ? [1] : [1,2,3]).map(n => ({
    id:'q'+n,number:n,status:'needs_manual_review',requires_image:false
  }));
  const detail = q('q1', 1);
  const fields = Object.fromEntries([
    'stemInput','choice1','choice2','choice3','choice4','transcriptInput',
    'reviewNote','audioStart','audioEnd','audioPlayer','editorContent','editorEmpty'
  ].map(id => [id, { value:'', hidden:false, pause() {} }]));
  function setFields(item) {
    fields.stemInput.value = item.stem;
    for(let n=1;n<=4;n++) fields['choice'+n].value = item.choices[n-1].text;
    fields.transcriptInput.value = item.transcript?.text || '';
    fields.reviewNote.value = '';
  }
  setFields(detail);
  const state = {
    detail,selectedId:'q1',items:rows,visible:filtered ? [rows[0],rows[2]] : [...rows],
    loading:false,saving:false,csrfToken:'test',baseline:'',
    audio:{saving:false,available:listening},capabilities:{reviewWrite:true},
    pendingReviews:new Map(),failedReviews:new Map(),detailsCache:{},
    committedReviews:new Map(),bundleProtectedIds:new Set(),
    aiHistoryLoadedIds:new Set(),requestId:0,pdfTab:'question'
  };
  const events = [];
  const ctx = vm.createContext({
    state,JSON,Number,
    $:id=>fields[id] || { focus(){ events.push('focus'); } },
    window:{confirm:()=>{events.push('confirm');return confirm;}},
    notice:(text,type,conflict)=>events.push({notice:type,text,conflict}),
    clearNotice:()=>{},
    reviewWritesBlocked:()=>false,
    aiAudit:()=>null,
    setLoading:value=>{state.loading=value;events.push('loading:'+value);},
    counts:()=>{},
    filterItems:()=>{
      if (filtered) state.visible=rows.filter(row =>
        row.number !== 2 && row.status === 'needs_manual_review');
    },
    loadList:async()=>{},
    renderDetail:()=>{
      events.push('render:'+state.detail.id);
      setFields(state.detail);
      state.baseline=JSON.stringify(vm.runInContext('draft()',ctx));
    },
    keepSelectedQuestionVisible:()=>{},
    request:async(url, options)=>{
      if(options?.method === 'POST') {
        events.push('post');
        if(changeDuringPost) fields.stemInput.value = 'changed-again';
        if(post !== 'ok') {
          if(post === 'unknown') return {id:'wrong',review_status:'verified',saved:true,version:1};
          const error=new Error('Mock '+post);
          if(post === 'conflict') error.status=409;
          throw error;
        }
        events.push('ack');
        return {id:'q1',number:1,review_status:'verified',saved:true,
          version:1,request_version:0,requires_image:false};
      }
      events.push('get:'+url);
      const next = /q(\d+)/.exec(url);
      assert.ok(next, 'Expected question-detail GET');
      return q('q'+next[1],Number(next[1]));
    }
  });
  vm.runInContext(functions,ctx);
  state.baseline=JSON.stringify(vm.runInContext('draft()',ctx));
  if(edited) fields.stemInput.value='edited';
  if(audioUnsaved) {
    fields.audioStart.value='1.000';
    fields.audioEnd.value='3.000';
  }
  const before=state.baseline;
  await vm.runInContext("sendReview('verified')",ctx);
  return {state,events,fields,before,ctx};
}

(async()=>{
  // No cached successor: submitted edits are committed, baseline must be
  // clean before the REAL canNavigate()/selectQuestion() navigation guard.
  let r=await scenario();
  assert.equal(r.state.selectedId,'q2');
  assert.equal(r.state.items[0].status,'verified');
  assert.equal(r.events.includes('confirm'),false,'Committed text must not prompt');
  assert.ok(r.events.indexOf('ack')<r.events.findIndex(e=>typeof e==='string'&&e.startsWith('get:')));

  r=await scenario({filtered:true});
  assert.equal(r.state.selectedId,'q3','Follow filtered visible order, skipping hidden q2');
  assert.equal(r.events.includes('confirm'),false);

  r=await scenario({listening:true,audioUnsaved:true,confirm:false});
  assert.equal(r.state.items[0].status,'verified','Text approval committed');
  assert.equal(r.state.selectedId,'q1','Unsaved audio must block auto-navigation');
  assert.equal(r.events.includes('confirm'),true,'Real audioDirty gate must prompt');
  assert.equal(r.fields.audioStart.value,'1.000','Keep unsaved audio boundaries');
  assert.equal(vm.runInContext('dirty()',r.ctx),false,'Submitted text is clean');
  assert.equal(vm.runInContext('audioDirty()',r.ctx),true);

  r=await scenario({listening:true,audioUnsaved:true,confirm:true});
  assert.equal(r.state.selectedId,'q2','Explicit consent may discard audio edits');

  r=await scenario({changeDuringPost:true,confirm:false});
  assert.equal(r.state.items[0].status,'verified');
  assert.equal(r.state.selectedId,'q1','Unsubmitted edits must block auto-navigation');
  assert.equal(r.fields.stemInput.value,'changed-again','Keep new unsaved draft');
  assert.equal(vm.runInContext('dirty()',r.ctx),true);
  assert.equal(r.events.includes('confirm'),true);

  r=await scenario({last:true,listening:true,audioUnsaved:true});
  assert.equal(r.state.selectedId,'q1');
  assert.equal(r.fields.audioStart.value,'1.000','Final item must keep unsaved audio edits');
  assert.equal(r.events.includes('render:q1'),false,'Do not rerender over unsaved audio');

  r=await scenario({last:true,changeDuringPost:true});
  assert.equal(r.state.selectedId,'q1');
  assert.equal(r.fields.stemInput.value,'changed-again','Final item must keep new text edits');
  assert.equal(r.events.includes('render:q1'),false,'Do not rerender over unsaved text');

  for (const post of ['error','conflict','unknown']) {
    r=await scenario({post});
    assert.equal(r.state.selectedId,'q1',post+' must never advance');
    assert.equal(r.state.items[0].status,'needs_manual_review',post+' must not claim approval');
    assert.equal(r.state.baseline,r.before,post+' must preserve unsaved baseline');
    assert.equal(r.fields.stemInput.value,'edited',post+' must preserve draft');
    assert.equal(r.events.includes('confirm'),false);
    assert.ok(r.events.some(e=>e.notice==='error'),post+' must explain error');
    if (post==='conflict') assert.ok(r.events.some(e=>e.conflict===true),'409 recovery offered');
  }
  console.log('Real dirty/canNavigate/selectQuestion approval safety PASS');
})().catch(error=>{console.error(error);process.exitCode=1});
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
    children: [], className: '', textContent: '', hidden: false, value: '', dataset: {},
    classList: { toggle() {} },
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = [...nodes]; },
    insertBefore(node, next) { this.children = this.children.filter(child => child !== node); const i=this.children.indexOf(next); this.children.splice(i<0?this.children.length:i,0,node); },
    removeChild(node) { this.children = this.children.filter(child => child !== node); },
    get lastElementChild() { return this.children[this.children.length-1]; },
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
  reviewIndicator: item => ({kind:item.status === 'verified'?'verified':'pending',reason:'test'}),
  selectQuestion: () => {},
};
const names = ['aiAudit', 'aiNumber', 'aiRisk', 'aiTimestamp', 'element', 'filterItems'];
const source = names.map(extractFunction).join('\n') + '\nfilterItems;';
const filterItems = vm.runInNewContext(source, context);

filterItems();
assert.deepEqual(Array.from(state.visible, item => item.id), ['q1', 'q2', 'q3']);
const focusedButton = controls.questionList.children[0];
filterItems();
assert.equal(controls.questionList.children[0],focusedButton,'refresh must preserve button identity and focus');
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
    children: [], className: '', textContent: '', value: '', hidden: false, dataset: {},
    classList: { toggle() {} },
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = [...nodes]; },
    insertBefore(node, next) { this.children = this.children.filter(child => child !== node); const i=this.children.indexOf(next); this.children.splice(i<0?this.children.length:i,0,node); },
    removeChild(node) { this.children = this.children.filter(child => child !== node); },
    get lastElementChild() { return this.children[this.children.length-1]; },
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
  reviewIndicator:item=>({kind:item.status==='verified'?'verified':'pending',reason:'test'}),
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

NODE_BUNDLE_CACHE_CHECK = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(process.argv[1], 'utf8');
function extractFunction(name) {
  let start = html.indexOf('      function ' + name + '(');
  if (start < 0) start = html.indexOf('      async function ' + name + '(');
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

const state = {
  items: [
    { id: 'q25', number: 25, section: 'listening' },
    { id: 'q26', number: 26, section: 'listening' },
    { id: 'q2', number: 2, section: 'listening' },
    { id: 'q3', number: 3, section: 'reading' },
  ],
  detailsCache: { q25: { version: 5 }, q26: { version: 5 } },
  bundleProtectedIds: new Set(['q2']),
};
const context = {
  state,
  request: async () => ({ questions: {
    q25: { version: 1 }, q2: { version: 1 }, q3: { version: 1 },
  } }),
};
const source = extractFunction('invalidateSharedAudioCache') + '\n' +
  extractFunction('loadBundle') + '\n({ invalidateSharedAudioCache, loadBundle });';
const functions = vm.runInNewContext(source, context);

(async () => {
  await functions.loadBundle();
  assert.equal(state.detailsCache.q25.version, 5, 'late bundle must not replace a fresher cached detail');
  assert.equal(state.detailsCache.q2, undefined, 'protected individual fetch must not be reinserted by bundle');
  assert.equal(state.detailsCache.q3.version, 1, 'missing safe entries should still be prefetched');

  functions.invalidateSharedAudioCache({ audio_segment: { shared_questions: [25, 26] } }, 'q25');
  assert.equal(state.detailsCache.q25.version, 5, 'current question cache remains authoritative');
  assert.equal(state.detailsCache.q26, undefined, 'shared-pair sibling cache must be invalidated');
  assert.equal(state.bundleProtectedIds.has('q26'), true, 'late bundle must not resurrect invalidated sibling');
  console.log('Bundle cache race and shared-pair invalidation PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


class TestReviewUIFlow(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_pdf_repeated_render_keeps_existing_source(self):
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html=fs.readFileSync(process.argv[1],'utf8');
const start=html.indexOf('      function renderPdf() {');
const end=html.indexOf('      function renderReferences() {',start);
assert.ok(start>0 && end>start);
let writes=0,removals=0;
const frame={hidden:true,_src:'',get src(){return this._src;},set src(value){writes++;this._src=value;},
  removeAttribute(name){assert.equal(name,'src');removals++;this._src='';}};
const node=()=>({hidden:false,textContent:'',removeAttribute(){},setAttribute(){},classList:{toggle(){}}});
const controls={sourceFrame:frame,pdfFallback:node(),sourceLink:node(),sourcePage:node()};
const state={pdfTab:'question',detail:{source_pdf_url:'/media/q1/paper',source_pdf_page:1,
  answer:{source_pdf_page:2},section:'reading'}};
const ctx={state,$:id=>controls[id],sourceWithPage:(url,page)=>url?'http://127.0.0.1:8577'+url+'#page='+page:null,
  document:{querySelectorAll:()=>[]}};
const render=vm.runInNewContext('('+html.slice(start,end).trim()+')',ctx);
render();render();
assert.equal(writes,1,'same PDF must be assigned only once');
assert.equal(removals,0,'same PDF must never clear iframe src');
state.detail.source_pdf_page=3;
render();
assert.equal(writes,2,'different original PDF page should update hash');
state.detail.source_pdf_url=null;
render();
assert.equal(removals,1,'unavailable PDF should clear iframe');
console.log('PDF source lifecycle PASS');
'''
        result = subprocess.run([shutil.which("node"), "-e", script, str(HTML)],
                                capture_output=True, text=True, encoding="utf-8", timeout=15,
                                check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_review_indicator_ack_and_media_contract(self):
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
function getFn(name, next) {
  let start = html.indexOf('      function ' + name + '(');
  assert.ok(start>=0,name);
  let end = html.indexOf('      function ' + next + '(',start+1);
  assert.ok(end>start,next);
  return html.slice(start,end);
}
const approved = {id:'q1',number:1,status:'verified',last_human_review:{status:'verified',
  reviewed_at:'2026-10-08T10:00:00+00:00',is_current:true}};
const pending = {...approved,status:'needs_manual_review'};
const state = {independentAudits:{questions:{'1':{gemini:{verdict:'finding',
  created_at:'2026-10-08T09:00:00+00:00', snapshot_created_at:'2026-10-08T08:00:00+00:00',
  timestamp_verified:true,snapshot_timestamp_verified:true}}}}};
const context = {state,aiNumber:x=>Number(x)||0,aiAudit:i=>i.ai_audit||null,Date};
const reviewIndicator = vm.runInNewContext('('+getFn('reviewIndicator','aiConvergenceText').trim()+')',context);
assert.equal(reviewIndicator(pending).kind,'problem','AI finding without human approval must be red');
assert.equal(reviewIndicator(approved).kind,'verified','human approval after AI finding must be green');
const report = state.independentAudits.questions['1'].gemini;
report.created_at='2026-10-08T11:00:00+00:00';
assert.equal(reviewIndicator(approved).kind,'verified','late audit of pre-approval snapshot must not reopen');
report.snapshot_created_at='2026-10-08T10:30:00+00:00';
assert.equal(reviewIndicator(approved).kind,'problem','verified new finding and postapproval snapshot must reopen');
report.snapshot_timestamp_verified=false;
assert.equal(reviewIndicator(approved).kind,'verified','unverified snapshot time cannot reopen');
assert.equal(reviewIndicator({...pending,status:'rejected'}).kind,'problem');
state.independentAudits.questions['1']={chatgpt:{verdict:'finding',created_at:'unknown',timestamp_verified:false}};
assert.equal(reviewIndicator(approved).kind,'verified','unknown ChatGPT timestamp cannot reopen');
assert.equal(reviewIndicator(pending).kind,'problem');
state.independentAudits.questions['1']={};
assert.equal(reviewIndicator(pending).kind,'pending');
const ack = vm.runInNewContext('('+getFn('verifyReviewAck','matchesReviewInput').trim()+')');
const input = {version:2};
const result = {id:'q1',review_status:'verified',request_version:2,version:3,saved:true};
assert.equal(ack(result,'q1','verified',input),result);
for (const invalid of [{...result,id:'q9'},{...result,version:2},{...result,request_version:0},
   {...result,saved:false},{...result,review_status:'rejected'}]) {
  assert.throws(()=>ack(invalid,'q1','verified',input));
}
const matches = vm.runInNewContext('('+getFn('matchesReviewInput','sharedTranscriptChanged').trim()+')');
assert.equal(matches({stem:'S',section:'listening',choices:[{number:1,text:'a'}],transcript:{text:'t'}},
  {stem:'S',choices:['a'],transcript_text:'t'}),true);
assert.equal(matches({stem:'S',section:'listening',choices:[{number:1,text:'a'}],transcript:{text:'old'}},
  {stem:'S',choices:['a'],transcript_text:'t'}),false);
const sharedAudio = vm.runInNewContext('('+getFn('sourceWithSharedAudio','statusName').trim()+')',{
  sameOriginURL:x=>new URL(x,'http://127.0.0.1:8577/')});
assert.equal(sharedAudio('/media/035-I-L-025/audio').href,sharedAudio('/media/035-I-L-026/audio').href);
assert.equal(sharedAudio('/media/035-I-L-027/audio').pathname,'/media/035-I-L-001/audio');
console.log('Review indicator, strict ack, draft comparison, shared-media URL PASS');
'''
        result = subprocess.run([shutil.which("node"), "-e", script, str(HTML)],
                                capture_output=True, text=True, encoding="utf-8", timeout=15,
                                check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_question_list_follows_selected_item_without_scrolling_page(self):
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
const start = html.indexOf('      function keepSelectedQuestionVisible() {');
const end = html.indexOf('      function filterChanged() {', start);
assert.ok(start > 0 && end > start, 'Missing selected-question scroll helper');
const helper = '(' + html.slice(start,end).trim() + ')';
const selected = { left:40, right:250, top:140, bottom:180 };
const active = { getBoundingClientRect: () => selected };
const nav = { scrollTop:100, scrollLeft:50, scrollWidth:260, clientWidth:260,
  scrollHeight:700, clientHeight:200, querySelector:() => active,
  getBoundingClientRect:() => ({left:20,right:280,top:100,bottom:300}) };
let mobile=false;
const context = { $:id=>{assert.equal(id,'questionList');return nav;},
  window:{matchMedia:()=>({matches:mobile})} };
const run = vm.runInNewContext(helper,context);
run();
assert.equal(nav.scrollTop,100,'visible question must not jump');
assert.equal(nav.scrollLeft,50);
selected.top=310; selected.bottom=340;
run();
assert.equal(nav.scrollTop,148,'below viewport should scroll down only within question list');
selected.top=80; selected.bottom=118;
run();
assert.equal(nav.scrollTop,120,'above viewport should scroll up');
nav.scrollWidth=700; nav.clientWidth=260; nav.scrollHeight=200; nav.clientHeight=200;
mobile=true;
selected.top=110; selected.bottom=170; selected.left=290; selected.right=380;
run();
assert.equal(nav.scrollLeft,158,'mobile question strip should scroll horizontally');
assert.equal(nav.scrollTop,120,'mobile should not scroll vertically');
nav.querySelector=()=>null;
run();
assert.equal(nav.scrollLeft,158,'filtered-out selection must not move list');
const navFn = html.slice(html.indexOf('      async function selectQuestion('),
                         html.indexOf('      function badge(',html.indexOf('      async function selectQuestion(')));
assert.equal((navFn.match(/keepSelectedQuestionVisible\(\);/g)||[]).length, 2,
  'both cached and fetched selection paths should follow the active question');
console.log('Selected-question scroll: vertical, horizontal, no-op, navigation paths PASS');
'''
        result = subprocess.run([shutil.which("node"), "-e", script, str(HTML)],
                                capture_output=True, text=True, encoding="utf-8", timeout=15,
                                check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_optimistic_approval_navigates_before_ack_and_fails_closed(self):
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
const start = html.indexOf('      async function sendOptimisticApproval(');
const end = html.indexOf('      async function sendReview(status) {', start);
assert.ok(start >= 0 && end > start);
const source = '(' + html.slice(start, end).trim() + ')';
async function scenario(fail) {
  const deferred = {};
  deferred.promise = new Promise((resolve, reject) => { deferred.resolve = resolve; deferred.reject = reject; });
  const question = { id:'q1', number:1, version:2, review_status:'needs_manual_review',
    section:'reading', choices: [{number:1,text:'original'}], transcript:null };
  const next = { id:'q2', number:2, version:0, section:'reading' };
  const state = { detail:question, selectedId:'q1', baseline:'', items:[
      {id:'q1',number:1,status:'needs_manual_review'}, {id:'q2',number:2,status:'needs_manual_review'}],
    pendingReviews:new Map(), failedReviews:new Map(), detailsCache:{q2:next},
    bundleProtectedIds:new Set(), aiHistoryLoadedIds:new Set(), csrfToken:'token', loading:false };
  const events = [];
  const ctx = {state, request:()=>{events.push('send');return deferred.promise;},
    verifyReviewAck:(result,id,status,submitted)=>{
      assert.equal(result.id,id);assert.equal(result.review_status,status);
      assert.equal(result.request_version,submitted.version);return result;
    },
    selectQuestion:async id=>{events.push('navigate');state.selectedId=id;state.detail=next;},
    renderReviewSync:()=>{}, filterItems:()=>{}, counts:()=>{}, setLoading:()=>{}, renderDetail:()=>{},
    notice:(m,t)=>events.push(['notice',t,m]), $:()=>({hidden:false})};
  const submit = vm.runInNewContext(source, ctx);
  const input = {stem:'edited',choices:['a','b','c','d'],transcript_text:null,note:'my evidence'};
  const task = submit('q1',input,'q2');
  assert.equal(state.pendingReviews.has('q1'),true);
  await Promise.resolve();
  assert.equal(state.selectedId,'q2','must display next question before acknowledgement');
  assert.equal(state.items[0].status,'needs_manual_review','unconfirmed approval is not committed');
  assert.equal(state.detailsCache.q1.stem,'edited','submitted draft survives navigation');
  if (fail) deferred.reject(new Error('network uncertain'));
  else deferred.resolve({id:'q1',request_version:2,version:3,review_status:'verified',saved:true});
  await task;
  assert.equal(state.pendingReviews.size,0);
  if (fail) {
    assert.equal(state.items[0].status,'needs_manual_review');
    assert.equal(state.failedReviews.size,1);
    assert.equal(state.failedReviews.get('q1').input.note,'my evidence');
    assert.ok(events.some(e=>Array.isArray(e)&&e[1]==='error'));
  } else {
    assert.equal(state.items[0].status,'verified');
    assert.equal(state.detailsCache.q1.version,3);
    assert.equal(state.failedReviews.size,0);
  }
}
(async()=>{await scenario(false);await scenario(true);console.log('Optimistic navigation and fail-closed reconciliation PASS')})()
  .catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run([shutil.which("node"), "-e", script, str(HTML)],
                                capture_output=True, text=True, encoding="utf-8", timeout=15,
                                check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

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
    def test_real_approval_navigation_preserves_audio_drafts_and_failure_guards(self):
        result = subprocess.run(
            [shutil.which("node"), "-e", NODE_REAL_NAVIGATION_CHECK, str(HTML)],
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

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_bundle_cache_preserves_fresher_entries_and_invalidates_shared_pairs(self):
        result = subprocess.run(
            [shutil.which("node"), "-e", NODE_BUNDLE_CACHE_CHECK, str(HTML)],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
