"""Exercise the actual fast-list initialization and delayed audit enrichment."""

import shutil
import subprocess
import unittest
from pathlib import Path

from tests import test_ai_audit_35 as fixtures
from src import ai_audit_35
from src.review_ui import ReviewStore


HTML = Path(__file__).resolve().parents[1] / "src" / "review_ui.html"

NODE = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
const a = html.indexOf('      function setAiSummaryStatus(status) {');
const b = html.indexOf('      function draft() {', a);
assert.ok(a !== -1 && b > a);
const src = html.slice(a, b);
const controls = Object.fromEntries(['aiFilterWrap','sortOrderWrap','aiFilter','sortOrder','aiLoadStatus',
  'connectionState'].map(id => [id,{hidden:true,value:id==='sortOrder'?'number':'all',textContent:''}]));
const state = {examId:'035-I-B',items:[],selectedId:'q1',baseline:'unsaved draft',
  committedReviews:new Map(),csrfToken:null,aiAuditAvailable:false,detailsCache:{},
  pendingReviews:new Map(),failedReviews:new Map()};
const requests=[];
const context={state,$:id=>controls[id],VALID_EXAMS:['035-I-B','036-I-B'],
  syncExamHeading:()=>{},counts:()=>{},filterItems:()=>{},
  request:url=>new Promise((resolve,reject)=>requests.push({url,resolve,reject})),
  setTimeout:(fn)=>{setImmediate(fn);return 1;},
  clearTimeout:()=>{},console};
vm.runInNewContext(src,context);
const flush=async()=>{for(let n=0;n<5;n++) await new Promise(resolve=>setImmediate(resolve));};
const fast=(status='needs_manual_review',version=0)=>({exam_id:'035-I-B',csrf_token:'csrf',
  database_backend:'sqlite',capabilities:{review_write:true},items:[
    {id:'q1',number:1,section:'listening',status,review_version:version,last_human_review:null},
    {id:'q2',number:2,section:'reading',status:'needs_manual_review',review_version:0}]});
const audit=(n,exam_id='035-I-B')=>({exam_id,state:'ready',ai_audit_available:true,items:[
  {id:'q1',ai_audit:{total:n,unresolved_findings:n,risk_score:n*10}},
  {id:'q2',ai_audit:{total:0,risk_score:0}}]});
(async()=>{
  const first=context.loadList();
  assert.equal(requests[0].url,'/api/questions-fast');
  requests[0].resolve(fast());
  await first;
  assert.equal(state.items.length,2,'Fast review rows must render first');
  assert.equal(requests[1]?.url,'/api/questions-ai-summary','Audit summary must follow fast render');
  state.committedReviews.set('q1',{status:'verified',version:4,last_human_review:{approved:true}});
  requests[1].resolve(audit(3));
  await flush();
  assert.equal(state.items[0].ai_audit.total,3);
  assert.equal(state.items[0].status,'needs_manual_review',
    'Audit response must never rewrite a human status');
  assert.equal(state.selectedId,'q1');
  assert.equal(state.baseline,'unsaved draft');
  assert.equal(controls.aiFilterWrap.hidden,false);
  assert.equal(controls.sortOrderWrap.hidden,false);
  controls.aiFilter.value='unresolved';
  const second=context.loadList();
  requests[2].resolve(fast('needs_manual_review',1));
  await second;
  assert.equal(state.items[0].status,'verified','Committed ACK must win over old fast response');
  assert.equal(state.items[0].review_version,4);
  assert.equal(state.items[0].ai_audit.total,3,'Audit stays visible across a refresh');
  assert.equal(controls.aiFilter.value,'unresolved','Preserve active AI filter after approval');
  assert.equal(requests[3]?.url,'/api/questions-ai-summary');
  const third=context.loadList();
  requests[4].resolve(fast('verified',4));
  await third;
  assert.equal(requests[5]?.url,'/api/questions-ai-summary');
  requests[5].resolve(audit(5));
  await flush();
  requests[3].resolve(audit(99));
  await flush();
  assert.equal(state.items[0].ai_audit.total,5,'Older audit result must be discarded');
  assert.equal(state.items[0].status,'verified');
  assert.equal(state.baseline,'unsaved draft');
  const fourth=context.loadList();
  requests[6].resolve(fast('verified',4));
  await fourth;
  requests[7].reject(new Error('audit unavailable'));
  await flush();
  assert.equal(state.items[0].status,'verified','Audit failure must not affect human review');
  assert.equal(state.items[0].ai_audit.total,5,'Prior successful audit display survives outage');
  const fifth=context.loadList();
  requests[8].resolve(fast('verified',4));
  await fifth;
  requests[9].resolve(audit(100,'036-I-B'));
  await flush();
  assert.equal(state.items[0].ai_audit.total,5,'Wrong-exam summary must be ignored');
  const sixth=context.loadList();
  requests[10].resolve(fast('verified',4));
  await sixth;
  requests[11].resolve({exam_id:'035-I-B',state:'loading',items:[]});
  await flush();
  assert.equal(requests[12]?.url,'/api/questions-ai-summary','Loading summary should retry');
  requests[12].resolve(audit(7));
  await flush();
  assert.equal(state.items[0].ai_audit.total,7);
  console.log('Fast list, async audit, ACK, out-of-order, error, draft preservation PASS');
})().catch(err=>{console.error(err);process.exitCode=1;});
"""


class ReviewAiSummaryFlowTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for HTML flow")
    def test_actual_fast_list_then_async_audit_update(self):
        done = subprocess.run([shutil.which("node"), "-e", NODE, str(HTML)],
                              capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("PASS", done.stdout)

    def test_audit_endpoint_contract_does_not_include_human_review_status(self):
        fixture = fixtures.AIAudit35Tests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        store = ReviewStore(db_path=fixture.db_path)
        initial = store.list_ai_audit_summary()
        self.assertEqual(initial["exam_id"], "035-I-B")
        self.assertEqual(initial["state"], "ready")
        self.assertEqual(initial["items"], [])
        run = ai_audit_35.create_run(fixture.db_path, label="audit-list")
        result = ai_audit_35.export_pass(
            fixture.db_path, run_id=run["run_id"], pass_number=1,
            auditor_id="fixture", perspective="transcript_alignment",
            model_id="fixture", prompt_version="test",
        )
        ai_audit_35.record_attempt_outcome(fixture.db_path, result["pass_id"], "timed_out",
                                           error_code="timeout", error_message="test")
        store = ReviewStore(db_path=fixture.db_path)
        result = store.list_ai_audit_summary()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["items"][0]["id"], fixtures.Q1)
        self.assertTrue(result["items"][0]["ai_audit"]["has_partial_failures"])
        self.assertTrue(all(set(item) == {"id", "ai_audit"} for item in result["items"]))

    def test_audit_endpoint_reports_loading_error_and_other_exam_separately(self):
        fixture = fixtures.AIAudit35Tests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        store = ReviewStore(db_path=fixture.db_path)
        store._list_questions_cache = {"ai_audit_available": True, "items": []}
        store._ai_summaries_loading = True
        self.assertEqual(store.list_ai_audit_summary()["state"], "loading")
        store._ai_summaries_loading = False
        store._ai_summaries_error = True
        self.assertEqual(store.list_ai_audit_summary()["state"], "error")
        store.exam_id = "036-I-B"
        self.assertEqual(store.list_ai_audit_summary()["state"], "unavailable")


if __name__ == "__main__":
    unittest.main()
