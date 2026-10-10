"""Execute the actual review page JavaScript against browser-state regressions.

Read-only: no application server, production database, media or browser profile.
Node's VM executes function bodies extracted from the shipped inline script.
The small DOM/request doubles cover F3 provenance, accessibility, cache, and
save races that are otherwise easy to miss in isolated backend tests.
"""

from pathlib import Path
import shutil
import subprocess
import unittest


PAGE = Path(__file__).resolve().parents[1] / "src" / "review_ui.html"


NODE = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
const scenario = process.argv[2];

function actual(start, next) {
  const begin = html.indexOf('      '+start);
  const end = html.indexOf('      '+next, begin + 1);
  assert.ok(begin >= 0 && end > begin, `Actual page function missing: ${start}`);
  return html.slice(begin, end);
}
class Node {
  constructor(id = '', tag = 'div') {
    this.id=id;this.tagName=tag.toUpperCase();this.value='all';this.children=[];
    this.dataset={};this.attributes={};this.style={};this.hidden=false;
    this.className='';this.textContent='';this.isConnected=true;
    const marks=new Set();
    this.classList={add:s=>marks.add(s),remove:s=>marks.delete(s),
      contains:s=>marks.has(s),toggle:(s,v)=>v?marks.add(s):marks.delete(s)};
  }
  setAttribute(k,v){this.attributes[k]=String(v)}
  getAttribute(k){return this.attributes[k]}
  addEventListener(){}
  replaceChildren(...n){this.children=[...n]}
  append(...n){this.children.push(...n)}
  appendChild(n){this.children.push(n);return n}
  insertBefore(n,next){const old=this.children.indexOf(n);if(old>=0)this.children.splice(old,1);
    const i=next==null?this.children.length:this.children.indexOf(next);
    this.children.splice(i<0?this.children.length:i,0,n);return n}
  removeChild(n){const i=this.children.indexOf(n);if(i>=0)this.children.splice(i,1);return n}
  get lastElementChild(){return this.children[this.children.length-1]}
  focus(){}
}
function dom() {
  const nodes=new Map();
  const get=id=>{if(!nodes.has(id))nodes.set(id,new Node(id));return nodes.get(id)};
  for(const id of ['questionSearch','sectionFilter','statusFilter','imageFilter',
    'aiFilter','indicatorFilter','sortOrder']) get(id).value=id==='questionSearch'?'':'all';
  get('sortOrder').value='number';
  return {nodes,get,element:(tag,cls='',content='')=>{
    const item=new Node('',tag);item.className=cls;item.textContent=String(content);return item;
  }};
}
const make=id=>({id,number:Number(id.slice(-3)),section:'reading',exam_id:id.slice(0,3)+'-I-B',
  status:'verified',review_status:'verified',review_version:1,requires_image:false,version:1,
  stem:'unchanged',choices:['a','b','c','d'].map((text,i)=>({number:i+1,text})),
  transcript:null,history:[{status:'needs_manual_review',scope:'original',note:'old entry'}]});

async function provenance() {
  const {get,element}=dom();
  const good=make('035-I-R-001');good.last_human_review={approved:true,is_current:true,status:'verified',
    reviewed_at:'2026-10-10T03:00:00Z',reviewer:'local_reviewer'};
  const superseded=make('035-I-R-002');superseded.last_human_review={approved:false,status:'verified',
    is_current:false,reviewed_at:'2026-10-10T01:00:00Z',reviewer:'local_reviewer'};
  const absent=make('035-I-R-003');absent.last_human_review=null;
  const pending=make('035-I-R-004');pending.status=pending.review_status='needs_manual_review';
  const conflicting=make('035-I-R-005');conflicting.last_human_review={approved:true,
    is_current:false,status:'verified',reviewed_at:'2026-10-10T02:00:00Z'};
  const contradicted=make('035-I-R-006');contradicted.last_human_review={approved:true,
    is_current:true,status:'verified'};contradicted.human_review_evidence={approved:false};
  const state={items:[good,superseded,absent,pending],selectedId:good.id,
    independentAudits:null,questionNodes:new Map(),pendingReviews:new Map(),
    failedReviews:new Map(),visible:[]};
  const context=vm.createContext({state,$:get,element,document:{activeElement:null},
    aiAudit:()=>null,aiNumber:x=>Number(x)||0,aiRisk:()=>({score:0,level:'none'}),
    aiTimestamp:()=>0,STATUS_NAMES:{verified:'승인',needs_manual_review:'검토 필요',rejected:'반려'},
    SECTION_NAMES:{reading:'읽기'},AI_RISK_NAMES:{none:'없음'},statusName:s=>s,
    selectQuestion:()=>{}, Date});
  vm.runInContext(actual('function reviewEvidence(', 'function aiConvergenceText(')
    +actual('function counts()', 'function markSelectedQuestion()'),context);
  const indicator=context.reviewIndicator;
  assert.equal(indicator(good).kind,'verified','Recent manual human approval is verified');
  for(const item of [superseded,absent]) {
    const verdict=indicator(item);
    assert.notEqual(verdict.kind,'verified',`${item.id}: DB status alone cannot be green`);
    assert.doesNotMatch(verdict.reason,/인간 검수 승인 완료|사람 승인 완료/,
      `${item.id}: screenreader must not claim human approval`);
    assert.match(verdict.reason,/근거|이력|기록|확인|미입증|미확정/);
  }
  assert.notEqual(indicator(pending).kind,'verified');
  assert.notEqual(indicator(conflicting).kind,'verified',
    'Stale approval cannot be revived by an old true flag');
  assert.notEqual(indicator(contradicted).kind,'verified',
    'Conflicting explicit backend provenance must fail closed');

  get('indicatorFilter').value='verified';
  context.filterItems();
  assert.deepEqual(state.visible.map(i=>i.id),[good.id],
    'Green filter must return only evidence-backed manual approvals');
  assert.match(state.questionNodes.get(good.id).attributes['aria-label'],/승인/);
  assert.equal(state.questionNodes.get(good.id).children.at(-1).children.at(-1).className.includes('verified'),true);

  get('indicatorFilter').value='all';context.filterItems();
  for(const item of [superseded,absent]) {
    const button=state.questionNodes.get(item.id);
    assert(button,'Unproven DB-verified question must remain navigable');
    assert.doesNotMatch(button.attributes['aria-label'],/인간 검수 승인 완료|사람 승인 완료/);
    assert.notEqual(button.children.at(-1).children.at(-1).className,'state-dot verified');
  }
  context.counts();
  assert.equal(get('countVerified').textContent,1);
  assert.equal(get('countUnknown').textContent,2);
  assert.equal(get('countPending').textContent,1);
  const progress=get('progressLabel').textContent;
  // A DB status count may still be displayed, but the human-approved count
  // must be explicitly identified: status-only 3 and evidence-backed 1 differ.
  assert.match(progress,/1\s*(?:\/|개|문항|건|명|승인|확정|근거)/,
    `Progress must disclose the one current manual approval: ${progress}`);
  assert.match(progress,/승인|근거|검수|이력/);
  assert.match(get('progressTrack').attributes['aria-valuenow'],/^\d+$/);
  assert.match(get('progressTrack').attributes['aria-valuetext'],/미확정 2/);
  get('indicatorFilter').value='unknown';context.filterItems();
  assert.deepEqual(state.visible.map(i=>i.id),[superseded.id,absent.id],
    'Unproven rows must be explicitly discoverable, not silently counted as approved');
  console.log('PROVENANCE/filter/progress/screenreader PASS');
}

async function save({conflict=false}={}) {
  const {get,element}=dom();
  const id='035-I-R-001', before=make(id);
  before.status=before.review_status='needs_manual_review';
  before.last_human_review=null;
  const row={id,number:1,status:before.review_status,review_version:1,last_human_review:null};
  const detail={...before};
  const state={detail,items:[row],visible:[row],selectedId:id,loading:false,saving:false,
    audio:{saving:false,available:false},capabilities:{reviewWrite:true},csrfToken:'local',
    pendingReviews:new Map(),failedReviews:new Map(),detailsCache:{[id]:detail},
    committedReviews:new Map(),bundleProtectedIds:new Set(),aiHistoryLoadedIds:new Set(),
    baseline:'',examId:'035-I-B'};
  const values={stem:'original',choices:['a','b','c','d'],transcript_text:null,
    note:'Reviewed paper and answer key against the original source'};
  const liveInput={...values};
  state.baseline=JSON.stringify(liveInput);
  const notices=[],requests=[];
  const record={status:'verified',scope:'manual_question_review',note:values.note,
    reviewer:'local_reviewer',reviewed_at:'2026-10-10T03:30:00+00:00',id:101};
  const approved={...detail,version:2,status:'verified',review_status:'verified',
    history:[record,...detail.history]};
  const ack={id,number:1,request_version:1,version:2,saved:true,
    review_status:'verified',requires_image:false,saved_review_id:101,
    saved_reviewed_at:record.reviewed_at,
    saved_review_event:record,history:approved.history,
    human_review_evidence:{approved:true,is_current:true},
    last_human_review:{id:101,status:'verified',approved:true,is_current:true,
      reviewer:'local_reviewer',reviewed_at:record.reviewed_at}};
  let changedDuringPost=false;
  const context=vm.createContext({state,$:get,element,Number,JSON,
    statusName:s=>({verified:'승인',needs_manual_review:'검토 필요',rejected:'반려'}[s]||'상태 미확인'),
    draft:()=>({...liveInput}),
    dirty:()=>JSON.stringify(liveInput)!==state.baseline,
    audioDirty:()=>false,canNavigate:()=>!changedDuringPost,
    window:{confirm:()=>true},
    notice:(msg,type,conflict)=>notices.push({msg,type,conflict}),
    reviewWritesBlocked:()=>false,sharedTranscriptChanged:()=>false,
    prepareSharedTranscriptCorrection:()=>{throw Error('Unexpected shared edit')},
    invalidateSharedTranscriptCache:()=>{},
    setLoading:val=>{state.loading=val},
    counts:()=>{},filterItems:()=>{},renderReviewSync:()=>{},
    renderDetail:()=>{},loadList:async()=>{},
    selectQuestion:async()=>{throw Error('Unexpected next navigation')},
    verifyReviewAck:(res,questionId,status,submitted)=>{
      assert.equal(questionId,id);assert.equal(res.id,id);assert.equal(res.review_status,status);
      assert.equal(res.request_version,submitted.version);return res;
    },
    request:async(url,opts)=>{
      requests.push([url,opts?.method||'GET']);
      if(opts?.method==='POST') {
        assert.equal(url,`/api/questions/${id}/review?fast=1`);
        changedDuringPost=true; // New typing after submission must not vanish.
        liveInput.stem='typed after clicking save';
        if(conflict) {const e=new Error('stale review version');e.status=409;throw e;}
        return ack;
      }
      if(url.startsWith('/api/questions/')) return approved;
      throw Error('Unexpected request '+url);
    },
  });
  vm.runInContext(actual('function applyReviewAck(', 'function updateNavigation()')
    +actual('async function sendReview(status)', 'function neighbor(')
    +actual('function renderHistory()', 'function renderAiAudit()'),context);
  await context.sendReview('verified');
  // Allow a queued read (if used) to complete; no arbitrary sleep/network.
  for(let i=0;i<4;i++)await Promise.resolve();
  if(conflict) {
    assert.equal(state.items[0].status,'needs_manual_review');
    assert.equal(state.detail.review_status,'needs_manual_review');
    assert.equal(state.detail.history[0].note,'old entry');
    assert.equal(state.detailsCache[id].version,1);
    assert.equal(state.committedReviews.size,0);
    assert.equal(state.baseline,JSON.stringify(values),
      '409 must not mark the stale draft as saved');
    assert.equal(liveInput.stem,'typed after clicking save');
    assert.equal(context.dirty(),true);
    assert.equal(values.note,'Reviewed paper and answer key against the original source');
    assert(notices.some(n=>n.type==='error'&&n.conflict===true),
      '409 must surface an accessible reload action without losing draft');
    console.log('CONFLICT_409/draft_protection PASS');
    return;
  }
  assert.equal(state.items[0].status,'verified','Only a 200 ACK updates local status');
  assert.equal(state.items[0].last_human_review?.approved,true);
  assert.equal(changedDuringPost,true);
  assert.equal(state.baseline,JSON.stringify(values));
  assert.equal(liveInput.stem,'typed after clicking save');
  assert.equal(context.dirty(),true,'Edits made during POST must still be dirty');
  const latest=state.detail?.history?.[0];
  assert.equal(latest?.status,'verified',
    'Immediate detail timeline must contain the just-committed review');
  assert.equal(latest?.scope,'manual_question_review');
  assert.equal(latest?.note,values.note);
  assert.equal(state.detailsCache[id]?.history?.[0]?.note,values.note,
    'Revisit from cache must not resurrect old history');
  context.renderHistory();
  const shown=get('historyList').children[0].children.map(x=>x.textContent).join(' ');
  assert.match(shown,/manual_question_review/);
  assert.match(shown,/Reviewed paper and answer key/);
  assert.equal(state.failedReviews.size,0);
  assert.equal(notices.some(n=>n.type==='error'),false);

  // Every fast save remains scoped to its own exam; no 36th ID in this cache.
  assert.deepEqual(Object.keys(state.detailsCache).filter(x=>x.startsWith('036-')),[]);
  console.log('FAST_ACK/immediate_history/dirty_draft PASS',requests.length);
}

async function optimistic() {
  const {get}=dom(),one=make('035-I-R-001'),two=make('035-I-R-002');
  one.status=one.review_status='needs_manual_review';
  two.status=two.review_status='needs_manual_review';
  const row1={id:one.id,number:1,status:one.status,review_version:1,last_human_review:null};
  const row2={id:two.id,number:2,status:two.status,review_version:1,last_human_review:null};
  const state={detail:one,items:[row1,row2],visible:[row1,row2],
    selectedId:one.id,detailsCache:{[one.id]:one,[two.id]:two},
    csrfToken:'local',audio:{saving:false},saving:false,loading:false,
    pendingReviews:new Map(),failedReviews:new Map(),committedReviews:new Map(),
    bundleProtectedIds:new Set(),aiHistoryLoadedIds:new Set(),baseline:''};
  const input={stem:'approved text',choices:['a','b','c','d'],transcript_text:null,
    note:'Human source comparison note for optimistic approval'};
  const audit={id:200,status:'verified',scope:'manual_question_review',note:input.note,
    reviewed_at:'2026-10-10T04:05:00Z'};
  let accept,decline;
  const promise=new Promise((res,rej)=>{accept=res;decline=rej});
  const notifications=[];
  const context=vm.createContext({state,$:get,JSON,Number,
    request:()=>promise,
    verifyReviewAck:(result,id,status,submitted)=>{
      assert.equal(result.id,id);assert.equal(status,'verified');
      assert.equal(result.version,submitted.version+1);return result;
    },
    applyReviewAck:null,
    notice:(text,type)=>notifications.push([type,text]),
    selectQuestion:async id=>{state.selectedId=id;state.detail=state.detailsCache[id]},
    renderDetail:()=>{},renderReviewSync:()=>{},filterItems:()=>{},counts:()=>{},
    setLoading:()=>{},
  });
  vm.runInContext(actual('function applyReviewAck(', 'function updateNavigation()')
    +actual('async function sendOptimisticApproval(', 'async function sendReview(status)'),context);
  const underway=context.sendOptimisticApproval(one.id,input,two.id);
  await Promise.resolve();
  assert.equal(state.pendingReviews.has(one.id),true);
  assert.equal(row1.status,'needs_manual_review',
    'Navigating to the next cached question cannot pretend the previous save committed');
  assert.equal(state.detail.id,two.id);
  accept({id:one.id,number:1,review_status:'verified',version:2,request_version:1,
    saved:true,history:[audit,...one.history],saved_review_event:audit,
    last_human_review:{approved:true,status:'verified',is_current:true,id:200},
    human_review_evidence:{approved:true}});
  await underway;
  assert.equal(row1.status,'verified');
  assert.equal(state.detailsCache[one.id].history[0].note,input.note,
    'The fast navigation path must refresh the original question history');
  assert.equal(state.detailsCache[two.id],two,'Next-question data must remain independent');
  assert.equal(state.pendingReviews.size,0);
  assert.equal(state.failedReviews.size,0);
  assert.equal(notifications.some(([type])=>type==='error'),false);
  console.log('OPTIMISTIC_NAVIGATION/immediate_original_history PASS');
}

async function race() {
  const {get}=dom();
  const id='035-I-R-001', q=make(id), otherId='036-I-R-001';
  let resolveList;
  let fakeRows=[];
  let getCount=0;
  const state={examId:'035-I-B',items:[],selectedId:null,detail:null,
    committedReviews:new Map([[id,{status:'verified',version:4,
      last_human_review:{approved:true,is_current:true,id:88}}]]),
    listGeneration:0,detailsCache:{[id]:{...q,version:2,status:'needs_manual_review',
      review_status:'needs_manual_review',history:[{note:'obsolete'}]}},
    bundleProtectedIds:new Set(),audio:{saving:false,preview:null,heardSignature:null},
    pendingReviews:new Map(),failedReviews:new Map(),csrfToken:null,readOnly:false,
    saving:false,loading:false,requestId:0,pdfTab:'question',
    aiAuditAvailable:true,aiHistoryLoadedIds:new Set(),capabilities:{reviewWrite:true}};
  const recent=()=>({id,number:1,section:'reading',status:'verified',review_version:4,
    last_human_review:{approved:true,is_current:true,id:88}});
  const stale=()=>({id,number:1,section:'reading',status:'needs_manual_review',
    review_version:2,last_human_review:null});
  const notifications=[];
  const context=vm.createContext({state,$:get,Number,JSON,
    VALID_EXAMS:['035-I-B','036-I-B'],
    stopAiSummaryPoll:()=>{},syncExamHeading:()=>{},
    setAiSummaryStatus:()=>{},startAiSummaryPoll:()=>{},
    counts:()=>{},filterItems:()=>{},
    clearNotice:()=>{},setLoading:v=>{state.loading=v},
    canNavigate:()=>true,renderDetail:()=>{},
    keepSelectedQuestionVisible:()=>{},focusEditorOnMobile:()=>{},
    retainedSourceTab:()=> 'question',
    notice:(message,type)=>notifications.push([type,message]),
    request:(url)=>{
      if(url==='/api/questions-fast') return new Promise(resolve=>{resolveList=resolve});
      if(url.startsWith('/api/questions/')) {getCount++;return Promise.resolve({
        ...q,version:5,review_status:'verified',history:[{note:'fresh on server',status:'verified'}]});}
      throw Error('Unexpected URL '+url);
    }
  });
  get('audioPlayer').pause=()=>{};
  vm.runInContext(actual('async function loadList()', 'function audioSegment()')
    +actual('async function selectQuestion(', 'function badge('),context);
  const stalePromise=context.loadList();
  resolveList({exam_id:'035-I-B',items:[stale()],csrf_token:'local',database_backend:'postgres',
    capabilities:{review_write:true}});
  await stalePromise;
  assert.equal(state.items[0].status,'verified','Delayed F3 may not overwrite newer ACK');
  assert.equal(state.items[0].last_human_review?.approved,true,
    'Delayed F3 may not discard ACK provenance');
  assert.equal(state.items[0].review_version,4);

  const refresh=context.loadList();
  resolveList({exam_id:'035-I-B',items:[recent()],csrf_token:'local',database_backend:'postgres',
    capabilities:{review_write:true}});
  await refresh;
  assert.equal(state.committedReviews.size,0,'Server catch-up releases ACK shadow');
  // The list version may be current while the cached editor/history is older.
  await context.selectQuestion(id);
  assert.ok(getCount>0,
    'A fresh F3 version must invalidate/refetch a stale cached detail before display');
  assert.equal(state.detail.version,5);
  assert.equal(state.detail.history[0].note,'fresh on server');

  const beforeExam=state.items.map(item=>item.id).join();
  const wrong=context.loadList();
  resolveList({exam_id:'036-I-B',items:[{id:otherId,number:1,section:'reading'}],
    csrf_token:'other',capabilities:{review_write:true}});
  await assert.rejects(wrong,/회차|시험|mismatch/i);
  assert.equal(state.items.map(item=>item.id).join(),beforeExam,
    '35th state and cache must not be replaced by a 36th response');
  console.log('STALE_LIST/DETAIL_CACHE/CROSS_EXAM PASS');
}

(async()=>{
  assert.match(html,/id="questionList" aria-label=/,
    'Question list must remain a named navigation region');
  assert.match(html,/id="notification"[^>]*role="status"[^>]*aria-live="polite"/,
    'Async save/conflict notices must be screenreader announced');
  if(scenario==='provenance')await provenance();
  else if(scenario==='save')await save();
  else if(scenario==='conflict')await save({conflict:true});
  else if(scenario==='optimistic')await optimistic();
  else if(scenario==='race')await race();
  else throw Error('Unknown test scenario '+scenario);
})().catch(err=>{console.error(err.stack||err);process.exitCode=1});
"""


@unittest.skipUnless(shutil.which("node"), "Node.js required to execute real inline UI code")
class ProvenanceUIContractTests(unittest.TestCase):
    def _execute(self, scenario):
        process = subprocess.run(
            [shutil.which("node"), "-e", NODE, str(PAGE), scenario],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertIn("PASS", process.stdout)

    def test_provenance_filter_progress_and_accessibility(self):
        self._execute("provenance")

    def test_fast_ack_refreshes_history_without_discarding_draft(self):
        self._execute("save")

    def test_stale_f3_race_cache_refresh_and_exam_isolation(self):
        self._execute("race")

    def test_conflict_409_preserves_unsubmitted_review_and_history(self):
        self._execute("conflict")

    def test_optimistic_navigation_history_commits_only_after_ack(self):
        self._execute("optimistic")


if __name__ == "__main__":
    unittest.main()
