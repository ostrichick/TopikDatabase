"""Exercise actual F3 frontend polling and recovery using a deterministic Node DOM/clock."""

from pathlib import Path
import shutil
import subprocess
import unittest


HTML = Path(__file__).resolve().parents[1] / "src" / "review_ui.html"

NODE_CHECK = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
function source(first, next) {
  const a = html.indexOf('      function ' + first + '(');
  const b = html.indexOf('      function ' + next + '(', a + 1);
  assert.ok(a >= 0 && b > a, `Missing actual frontend function ${first}`);
  return html.slice(a,b);
}
const script = source('setAiSummaryStatus','draft') + '\n' + source('switchExam','sameOriginURL');

function createHarness(examId = '035-I-B') {
  let clock = 0;
  let nextTimer = 0;
  const timers = new Map();
  const delays = [];
  const requests = [];
  const navigations = [];
  const controls = Object.fromEntries([
    'aiFilterWrap','sortOrderWrap','aiFilter','sortOrder','aiLoadStatus','aiRetry','connectionState','examSelector'
  ].map(id => [id,{ hidden:true, textContent:'', value:id==='sortOrder'?'number':'all',
                    listeners:{},addEventListener(kind,cb){ this.listeners[kind]=cb; }}]));
  const state = {
    examId, items:[], selectedId:'q1', detail:{id:'q1',version:7},
    baseline:'unsaved text editor', pendingReviews:new Map([['pending',{version:7}]]),
    failedReviews:new Map(),committedReviews:new Map([['q1',{version:7,status:'verified'}]]),
    csrfToken:'old-token', aiAuditAvailable:false,detailsCache:{q1:{stem:'draft'}},
    saving:false,audio:{saving:false},examSwitchConfirmed:false,
  };
  const context = {
    state, $:id=>{assert.ok(controls[id],id);return controls[id]},
    VALID_EXAMS:['035-I-B','036-I-B'],
    counts:()=>{}, filterItems:()=>{},syncExamHeading:()=>{},renderReviewDecisionCue:()=>{},
    Date:{now:()=>clock},
    request:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject})),
    setTimeout:(fn,ms)=>{const id=++nextTimer;timers.set(id,{when:clock+ms,fn});delays.push(ms);return id;},
    clearTimeout:id=>timers.delete(id),
    dirty:()=>false,audioDirty:()=>false,notice:()=>{},
    window:{confirm:()=>true,location:{href:'http://127.0.0.1:8123/?exam_id='+examId,
      assign:url=>navigations.push(url)}}, URL,
    AbortController,console,
  };
  vm.runInNewContext(script,context);
  const flush=async()=>{for(let n=0;n<6;n++) await Promise.resolve();};
  const fast=(status='needs_manual_review', version=5)=>({
    exam_id:examId,csrf_token:'csrf-'+examId,capabilities:{review_write:true},
    database_backend:'sqlite',items:[
      {id:'q1',number:1,section:'listening',status,review_version:version},
      {id:'q2',number:2,section:'reading',status:'needs_manual_review',review_version:1},
    ]
  });
  const ready=(v,summaryVersion)=>({exam_id:examId,state:'ready',ai_audit_available:true,
    summary_version:summaryVersion,items:[
      {id:'q1',ai_audit:{total:v,unresolved_findings:v,risk_score:v}},
      {id:'q2',ai_audit:{total:0}}]});
  const loading=()=>({exam_id:examId,state:'loading'});
  async function tick() {
    assert.ok(timers.size,'Expected a scheduled summary retry');
    const [id,task] = [...timers].sort((a,b)=>a[1].when-b[1].when)[0];
    timers.delete(id);
    clock = task.when;
    task.fn();
    await flush();
  }
  async function beginList() {
    const index = requests.length;
    const promise = context.loadList();
    assert.equal(requests[index].url,'/api/questions-fast');
    requests[index].resolve(fast());
    await promise;
    assert.equal(requests[index+1].url,'/api/questions-ai-summary');
    return index+1;
  }
  return {context,state,controls,timers,delays,requests,navigations,
    flush,fast,ready,loading,tick,beginList,now(){return clock;},advance(ms){clock+=ms;}};
}

(async()=>{
  const h=createHarness();
  let index=await h.beginList();
  assert.equal(h.state.items[0].status,'verified','Preserve higher local ACK version');
  assert.equal(h.state.items[0].review_version,7);
  assert.equal(h.state.baseline,'unsaved text editor');
  h.requests[index].resolve(h.loading());
  await h.flush();
  assert.equal(h.timers.size,1);
  assert.equal(h.controls.aiRetry.hidden,true,'Do not show retry in normal loading state');
  const firstDelay=h.delays.at(-1);
  assert.ok(firstDelay>=500 && firstDelay<3000, 'Begin polling with a short backoff');
  await h.tick();
  assert.equal(h.requests.at(-1).url,'/api/questions-ai-summary');
  h.requests.at(-1).resolve(h.loading());
  await h.flush();
  assert.ok(h.delays.at(-1)>firstDelay, 'Progressively slow repeated loading requests');
  await h.tick();
  h.requests.at(-1).resolve(h.ready(3,10));
  await h.flush();
  assert.equal(h.state.items[0].ai_audit.total,3);
  assert.equal(h.state.items[0].status,'verified');
  assert.equal(h.state.items[0].review_version,7);
  assert.equal(h.state.detailsCache.q1.stem,'draft');
  assert.equal(h.state.csrfToken,'csrf-035-I-B');
  assert.equal(h.state.pendingReviews.size,1);
  assert.equal(h.controls.aiFilterWrap.hidden,false);
  assert.equal(h.timers.size,0);
  h.controls.aiFilter.value='unresolved';

  // A stale server cache response must not overwrite a newer summary version.
  h.context.retryAiSummary();
  h.requests.at(-1).resolve(h.ready(99,9));
  await h.flush();
  assert.equal(h.state.items[0].ai_audit.total,3,'Reject older summary version');
  assert.equal(h.timers.size,1,'Retry stale cache after backoff');
  await h.tick();
  h.requests.at(-1).resolve(h.ready(4,11));
  await h.flush();
  assert.equal(h.state.items[0].ai_audit.total,4);
  assert.equal(h.controls.aiFilter.value,'unresolved');

  // Outages keep the human list, previous AI view and filter; retry is explicit.
  h.context.retryAiSummary();
  h.requests.at(-1).reject(new Error('network offline'));
  await h.flush();
  assert.equal(h.controls.aiRetry.hidden,false,'Offer a manual retry on request failure');
  assert.match(h.controls.aiLoadStatus.textContent,/실패|못했습니다/);
  assert.equal(h.state.items[0].ai_audit.total,4);
  assert.equal(h.controls.aiFilter.value,'unresolved');
  const before=h.requests.length;
  h.context.retryAiSummary();
  assert.equal(h.requests.length,before+1,'Manual retry does not reload the fast list');
  assert.equal(h.controls.aiRetry.hidden,true);
  h.requests.at(-1).resolve(h.ready(5,12));
  await h.flush();
  assert.equal(h.state.items[0].ai_audit.total,5);

  // Replacing a polling job cancels scheduled retries and invalidates old fetches.
  h.context.retryAiSummary();
  const old=h.requests.at(-1);
  old.resolve(h.loading());
  await h.flush();
  assert.equal(h.timers.size,1);
  const staleIndex=h.requests.length;
  const refresh=h.context.loadList();
  assert.equal(h.timers.size,0,'New fast load must dispose old polling timer');
  h.requests[staleIndex].resolve(h.fast('needs_manual_review',6));
  await refresh;
  assert.equal(h.requests[staleIndex+1].url,'/api/questions-ai-summary');
  h.requests[staleIndex+1].resolve(h.ready(6,13));
  await h.flush();
  assert.equal(h.state.items[0].status,'verified');
  assert.equal(h.state.items[0].review_version,7);
  assert.equal(h.state.items[0].ai_audit.total,6);
  assert.equal(h.state.selectedId,'q1','Background audit must not change current selection');
  assert.equal(h.state.baseline,'unsaved text editor','Background audit must not reset draft');
  assert.equal(h.state.pendingReviews.size,1,'Background audit must not alter pending writes');

  // Version and generation checks both protect against late responses.
  h.context.retryAiSummary();
  const oldRequest=h.requests.at(-1);
  h.context.retryAiSummary();
  h.requests.at(-1).resolve(h.ready(7,14));
  await h.flush();
  oldRequest.resolve(h.ready(999,99));
  await h.flush();
  assert.equal(h.state.items[0].ai_audit.total,7,'Discard superseded result after explicit retry');

  // The retry budget must exceed the old 44-second polling cutoff, then stop.
  h.context.retryAiSummary();
  h.requests.at(-1).resolve(h.loading());
  await h.flush();
  const pollingStartedAt=h.now();
  let turns=0;
  while(h.timers.size && turns<80) {
    await h.tick();
    if(h.requests.at(-1).url==='/api/questions-ai-summary' && h.requests.at(-1).resolve) {
      const pending=h.requests.at(-1);
      if(!pending.handled) {pending.handled=true; pending.resolve(h.loading());await h.flush();}
    }
    turns++;
  }
  assert.ok(turns<80,'Retry job must eventually finish');
  assert.ok(h.now()-pollingStartedAt>44000,'Polling must no longer stop at ~44s');
  assert.equal(h.timers.size,0,'Expired poll must leave no orphan timer');
  assert.equal(h.controls.aiRetry.hidden,false,'Expired poll offers explicit retry');
  assert.equal(h.state.items[0].ai_audit.total,7,'Do not erase prior AI data when polling expires');
  assert.equal(h.controls.aiFilter.value,'unresolved');

  // An actual exam-selector navigation clears timers and ignores old results.
  h.context.retryAiSummary();
  h.requests.at(-1).resolve(h.loading());
  await h.flush();
  assert.equal(h.timers.size,1);
  h.state.pendingReviews.clear();
  h.context.switchExam('036-I-B');
  assert.equal(h.navigations.length,1);
  assert.equal(h.timers.size,0,'Switching exams must cancel old polling timers');
  assert.equal(h.state.examSwitchConfirmed,true);

  // New 36th document reports unavailable, never reusing the 35th audit.
  const h36=createHarness('036-I-B');
  index=await h36.beginList();
  h36.requests[index].resolve({exam_id:'036-I-B',state:'unavailable',ai_audit_available:false,items:[]});
  await h36.flush();
  assert.equal(h36.controls.aiFilterWrap.hidden,true);
  assert.equal(h36.controls.aiRetry.hidden,true);
  assert.equal(h36.state.items[0].ai_audit,undefined);
  assert.equal(h36.timers.size,0);
  assert.match(html,/\$\('aiRetry'\)\.addEventListener\('click',\s*retryAiSummary\)/,
    'The real Retry button must be wired to the recovery handler');
  console.log('AI summary recovery: progressive backoff, delayed-ready, timeout, manual retry, versions, navigation and ACK PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class AiSummaryRecoveryTests(unittest.TestCase):
    def test_real_frontend_loading_recovery_and_switch(self):
        result = subprocess.run(
            [shutil.which("node"), "-e", NODE_CHECK, str(HTML)],
            capture_output=True, text=True, encoding="utf-8", timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
