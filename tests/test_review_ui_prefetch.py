"""P1 navigation cache and concurrent GET safety using the real frontend JS.

Node VM drives deferred mock responses. No HTTP listener or database writes.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "src" / "review_ui.html"

NODE_PREFETCH = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
function source(from, to) {
  const start = html.indexOf('      '+from);
  const end = html.indexOf('      '+to, start);
  assert.ok(start>=0 && end>start, 'Function missing: '+from);
  return html.slice(start,end);
}
const sourceText=source('function detailVersionFloor(', 'async function selectQuestion(');
const defer=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});
  return {promise,resolve,reject};};
const ticks=async()=>{for(let n=0;n<10;n++) await Promise.resolve();};
function harness() {
  const ids=[1,2,3,4,5].map(n=>'035-I-L-'+String(n).padStart(3,'0'));
  const rows=ids.map((id,index)=>({id,number:index+1,review_version:0,status:'needs_manual_review'}));
  const state={examId:'035-I-B',selectedId:ids[0],examSwitchConfirmed:false,items:rows,visible:rows,
    detail:{id:ids[0]},detailsCache:{},committedReviews:new Map(),bundleProtectedIds:new Set(),
    pendingReviews:new Map(),failedReviews:new Map(),detailFetches:new Map(),detailFetchEpochs:new Map(),
    prefetchQueue:[],prefetchActive:0};
  const requests=[];
  const context={state,request:(path)=>{
    const id=decodeURIComponent(path.match(/\/questions\/([^?]+)/)[1]);
    const d=defer();requests.push({id,...d});
    return d.promise;
  }};
  const api=vm.runInNewContext(sourceText+
    '\n({prefetchAhead,pumpQuestionPrefetch,requestQuestionDetail,cachedQuestion,detailVersionFloor,invalidateDetailFetch})',
    context);
  const result=(id,version=0)=>({id,exam_id:'035-I-B',version,choices:[]});
  const call=(id,index=0)=>requests.filter(req=>req.id===id)[index];
  return {state,ids,rows,requests,api,result,call};
}
(async()=>{
  {
    const h=harness(),{state,ids,api,requests,call,result}=h;
    api.prefetchAhead();
    await ticks();
    assert.equal(requests.length,2,'At most two background GETs concurrently');
    assert.deepEqual(requests.map(x=>x.id),ids.slice(1,3),'Prioritize immediate successors');
    assert.equal(state.prefetchActive,2);
    const same=api.requestQuestionDetail(ids[1]);
    assert.equal(same,api.requestQuestionDetail(ids[1]),'Same exam/id/version GET deduplicates');
    assert.equal(requests.length,2,'Foreground must reuse prefetch request');
    call(ids[1]).resolve(result(ids[1]));
    await same;await ticks();
    assert.equal(requests.length,3,'Third successor should start after a slot frees');
    assert.equal(call(ids[3]).id,ids[3]);
    call(ids[2]).resolve(result(ids[2])); call(ids[3]).resolve(result(ids[3]));
    await ticks();
    assert.equal(state.prefetchActive,0);
    assert.equal(api.cachedQuestion(ids[1]).id,ids[1]);
    assert.equal(api.cachedQuestion(ids[3]).id,ids[3]);
  }
  {
    const h=harness(),{state,ids,rows,api,requests,call,result}=h;
    const first=api.requestQuestionDetail(ids[1]);await ticks();
    rows[1].review_version=1; // Another PC advanced version during in-flight GET.
    call(ids[1]).resolve(result(ids[1],0));
    await assert.rejects(first,/이전 버전/);
    assert.equal(api.cachedQuestion(ids[1]),null,'Stale response never populates cache');
    const second=api.requestQuestionDetail(ids[1]);await ticks();
    assert.equal(requests.length,2,'New version must use a new request');
    call(ids[1],1).resolve(result(ids[1],1));await second;
    assert.equal(api.cachedQuestion(ids[1]).version,1);
    const older=api.requestQuestionDetail(ids[2]);await ticks();
    const newer=api.requestQuestionDetail(ids[2],{fresh:true});await ticks();
    call(ids[2],1).resolve(result(ids[2],0));await newer;
    call(ids[2],0).resolve(result(ids[2],0));
    await assert.rejects(older,/무효화/);
    assert.equal(api.cachedQuestion(ids[2]).version,0,'Old forced-reload response cannot overwrite');
  }
  {
    const h=harness(),{state,ids,api,call,result}=h;
    const pending=api.requestQuestionDetail(ids[1]);await ticks();
    state.pendingReviews.set(ids[1],{input:{stem:'unsaved'}});
    call(ids[1]).resolve(result(ids[1]));
    await assert.rejects(pending,/저장 결과/);
    assert.equal(api.cachedQuestion(ids[1]),null,'Unconfirmed approval cannot be overwritten by GET');
    state.pendingReviews.clear();
    const failed=api.requestQuestionDetail(ids[1]);await ticks();
    call(ids[1],1).reject(new Error('offline'));
    await assert.rejects(failed,/offline/);
    const recovered=api.requestQuestionDetail(ids[1]);await ticks();
    call(ids[1],2).resolve(result(ids[1]));await recovered;
    assert.equal(api.cachedQuestion(ids[1]).id,ids[1],'Offline GET can be retried explicitly');
  }
  {
    const h=harness(),{state,ids,api,call,result}=h;
    const old=api.requestQuestionDetail(ids[1]);await ticks();
    state.examSwitchConfirmed=true;state.examId='036-I-B';
    call(ids[1]).resolve(result(ids[1]));
    await assert.rejects(old,/무효화/);
    assert.equal(Object.keys(state.detailsCache).length,0,'Old exam response cannot enter new exam cache');
  }
  console.log('Prefetch: max2, next3, dedupe, stale/CAS, forced refresh, pending, offline, cross-exam PASS');
})().catch(error=>{console.error(error);process.exitCode=1});
"""

NODE_PARALLEL_APPROVAL = r"""
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(process.argv[1],'utf8');
function part(from,to){const a=html.indexOf('      '+from),b=html.indexOf('      '+to,a);
  assert.ok(a>=0&&b>a);return html.slice(a,b);}
const code=part('function applyReviewAck(', 'function updateNavigation()')+
  part('async function sendReview(status) {','function neighbor(');
const defer=()=>{let resolve,reject;const promise=new Promise((r,j)=>{resolve=r;reject=j;});
  return {promise,resolve,reject};};
const tick=async()=>{for(let n=0;n<8;n++) await Promise.resolve();};
async function scenario(failure='none'){
  const rows=[{id:'q1',number:1,status:'needs_manual_review',review_version:0},
              {id:'q2',number:2,status:'needs_manual_review',review_version:0}];
  const before={id:'q1',number:1,version:0,section:'reading',review_status:'needs_manual_review',
    stem:'old',choices:[1,2,3,4].map(number=>({number,text:'old'})),transcript:null};
  const state={detail:before,selectedId:'q1',items:rows,visible:rows,
    capabilities:{reviewWrite:true},audio:{saving:false},loading:false,saving:false,csrfToken:'token',
    detailsCache:{},committedReviews:new Map(),bundleProtectedIds:new Set(),
    aiHistoryLoadedIds:new Set(),pendingReviews:new Map(),failedReviews:new Map()};
  const post=defer(),get=defer(),events=[];
  const ctx={state,request:(url,opts)=>{assert.equal(opts.method,'POST');events.push('POST');return post.promise;},
    requestQuestionDetail:id=>{assert.equal(id,'q2');events.push('GET');return get.promise;},
    cachedQuestion:()=>null,invalidateDetailFetch:()=>{},reviewWritesBlocked:()=>false,
    sharedTranscriptChanged:()=>false,audioDirty:()=>false,dirty:()=>false,
    draft:()=>({stem:'submitted',choices:['a','b','c','d'],transcript_text:null,note:''}),
    verifyReviewAck:(res,id,status,submitted)=>{assert.equal(res.id,id);
      assert.equal(res.review_status,status);assert.equal(res.version,submitted.version+1);return res;},
    $:()=>({hidden:false,focus(){}}),setLoading:()=>{},notice:()=>{},
    counts:()=>{},filterItems:()=>{},loadList:async()=>{},renderDetail:()=>{},
    renderReviewSync:()=>{},
    selectQuestion:async id=>{events.push('navigate');state.selectedId=id;state.detail={id,number:2};},
    invalidateSharedTranscriptCache:()=>{}};
  const send=vm.runInNewContext(code+'\nsendReview;',ctx);
  const submission=send('verified');await tick();
  assert.equal(events[0],'GET','Independent next GET begins before POST acknowledgement');
  assert.equal(events[1],'POST');
  assert.equal(state.selectedId,'q1','No optimistic navigation on cache miss');
  get.resolve({id:'q2'});await tick();
  assert.equal(state.selectedId,'q1','Prefetch completion cannot approve or navigate');
  if(failure!=='none'){const e=new Error(failure==='conflict'?'409 conflict':'offline');
    if(failure==='conflict')e.status=409;post.reject(e);}
  else post.resolve({id:'q1',number:1,version:1,request_version:0,
    review_status:'verified',saved:true,requires_image:false});
  await submission;
  assert.equal(state.selectedId,failure!=='none'?'q1':'q2');
  assert.equal(rows[0].status,failure!=='none'?'needs_manual_review':'verified');
  assert.equal(events.includes('navigate'),failure==='none','Do not navigate after failed POST');
  assert.equal(state.failedReviews.size,failure==='network'?1:0);
  if(failure==='network') assert.equal(state.failedReviews.get('q1').input.stem,'submitted');
}
(async()=>{await scenario();await scenario('conflict');await scenario('network');
  console.log('Concurrent safe GET/POST, ACK gate, 409/offline fail-closed PASS');
})().catch(error=>{console.error(error);process.exitCode=1});
"""

NODE_FAST_NAVIGATION = r"""
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(process.argv[1],'utf8');
function part(from,to){const a=html.indexOf('      '+from),b=html.indexOf('      '+to,a);
  assert.ok(a>=0&&b>a);return html.slice(a,b);}
const defer=()=>{let resolve,reject;const promise=new Promise((r,j)=>{resolve=r;reject=j;});
  return {promise,resolve,reject};};
const ids=[1,2,3,4].map(n=>'035-I-L-'+String(n).padStart(3,'0'));
const item=(id,version=0)=>({id,exam_id:'035-I-B',version,number:Number(id.slice(-3)),
  choices:[],section:'listening',transcript:{text:'dialogue'}});
const state={examId:'035-I-B',selectedId:ids[0],detail:item(ids[0]),requestId:0,
  detailsCache:{},bundleProtectedIds:new Set(),committedReviews:new Map(),
  pendingReviews:new Map(),failedReviews:new Map(),items:ids.map((id,i)=>({
    id,number:i+1,review_version:0})),visible:[],audio:{preview:null,heardSignature:null},
  detailFetches:new Map(),detailFetchEpochs:new Map(),prefetchQueue:[],prefetchActive:0,pdfTab:'question',
  examSwitchConfirmed:false};
// Filtered list keeps q2 the only visible successor; this fixture isolates
// click-to-render timing from independently tested next-three prefetch.
state.visible=[state.items[1]];
const requests=[],renders=[],errors=[];
const ctx={state,
  request:url=>{const id=decodeURIComponent(url.match(/\/questions\/([^?]+)/)[1]);
    const d=defer();requests.push({id,...d});return d.promise;},
  $:id=>({pause(){},hidden:false,textContent:''}),
  canNavigate:()=>true,setLoading:value=>{state.loading=value;},
  renderDetail:()=>renders.push(state.detail.id),keepSelectedQuestionVisible:()=>{},
  focusEditorOnMobile:()=>{},filterItems:()=>{},clearNotice:()=>{},
  notice:message=>errors.push(message),
  retainedSourceTab:()=> 'question'};
const code=part('function detailVersionFloor(','async function selectQuestion(')+
  part('async function selectQuestion(','function badge(');
const nav=vm.runInNewContext(code+
  '\n({selectQuestion,requestQuestionDetail,cachedQuestion})',ctx);
const ticks=async()=>{for(let n=0;n<8;n++) await Promise.resolve();};
(async()=>{
  state.detailsCache[ids[1]]=item(ids[1]);
  const immediate=nav.selectQuestion(ids[1]);
  assert.equal(state.selectedId,ids[1],'Warm cache selects immediately in current event loop');
  assert.equal(state.detail.id,ids[1]);
  assert.deepEqual(renders,[ids[1]]);
  assert.equal(requests.length,0,'No GET on warm cache');
  await immediate;
  const slower=nav.selectQuestion(ids[2]);await ticks();
  assert.equal(state.detail,null,'Cold navigation shows loading rather than stale old detail');
  assert.equal(requests.length,1);
  const cachedLater=item(ids[3]);state.detailsCache[ids[3]]=cachedLater;
  await nav.selectQuestion(ids[3]);
  assert.equal(state.detail.id,ids[3]);
  requests[0].resolve(item(ids[2]));await slower;
  assert.equal(state.detail.id,ids[3],'Late old GET may cache, not steal current selection');
  assert.deepEqual(renders,[ids[1],ids[3]]);
  state.items[1].review_version=1;
  const stale=nav.selectQuestion(ids[1]);await ticks();
  assert.equal(requests.length,2,'Cache below list version must trigger current GET');
  requests[1].resolve(item(ids[1],1));await stale;
  assert.equal(state.detail.version,1);
  assert.equal(state.detailsCache[ids[1]].version,1);
  console.log('Fast navigation: synchronous cache hit, cold loading, late GET isolation, list-version refresh PASS');
})().catch(error=>{console.error(error);process.exitCode=1});
"""

NODE_STALE_SUCCESSOR_RACE = r"""
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(process.argv[1],'utf8');
const part=(a,b)=>{
  const start=html.indexOf('      '+a),end=html.indexOf('      '+b,start);
  assert.ok(start>=0&&end>start,'Missing '+a);
  return html.slice(start,end);
};
const code=part('function applyReviewAck(','function updateNavigation()')+
  part('function detailVersionFloor(','function invalidateDetailFetch(')+
  part('async function sendOptimisticApproval(','function neighbor(');
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});
  return {promise,resolve,reject};};
const ticks=async()=>{for(let n=0;n<12;n++)await Promise.resolve();};
const id='035-I-R-001',nextId='035-I-R-002';
let unhandled=0;
process.on('unhandledRejection',()=>{unhandled++;});

async function scenario(kind,failure) {
  const q={id,number:1,exam_id:'035-I-B',section:'reading',version:1,
    review_status:'needs_manual_review',stem:'original',
    choices:[1,2,3,4].map(number=>({number,text:'old'})),transcript:null};
  const successor={...q,id:nextId,number:2,version:kind==='already-stale'?1:2};
  const rows=[{id,number:1,status:'needs_manual_review',review_version:1},
    {id:nextId,number:2,status:'needs_manual_review',review_version:2}];
  const state={examId:'035-I-B',detail:q,selectedId:id,items:rows,visible:rows,
    detailsCache:{[nextId]:successor},committedReviews:new Map(),
    bundleProtectedIds:new Set(),detailFetches:new Map(),detailFetchEpochs:new Map(),
    prefetchQueue:[],pendingReviews:new Map(),failedReviews:new Map(),
    csrfToken:'test',saving:false,loading:false,audio:{saving:false},
    capabilities:{reviewWrite:true},baseline:'original baseline',requestId:0};
  const post=deferred(),get=deferred(),nav=deferred(),events=[],notices=[];
  const ctx=vm.createContext({
    state,
    draft:()=>({stem:'edited',choices:['a','b','c','d'],transcript_text:null,note:'human note'}),
    dirty:()=>false,audioDirty:()=>false,sharedTranscriptChanged:()=>false,
    reviewWritesBlocked:()=>false,renderReviewSync:()=>{},filterItems:()=>{},
    counts:()=>{},renderDetail:()=>{},loadList:()=>Promise.resolve(),
    invalidateDetailFetch:()=>{},invalidateSharedTranscriptCache:()=>{},
    notice:(message,type)=>notices.push({message,type}),
    $:()=>({hidden:false,focus(){}}),setLoading:()=>{},
    verifyReviewAck:(ack,questionId,wanted,submitted)=>{
      assert.equal(ack.id,questionId);assert.equal(ack.review_status,wanted);
      assert.equal(ack.version,submitted.version+Number(ack.saved));
      return ack;
    },
    request:(url,opts)=>{
      assert.equal(opts.method,'POST');events.push('POST');
      // The other PC's list refresh can invalidate a formerly valid successor
      // between optimistic eligibility and the navigation call.
      if(kind==='becomes-stale') rows[1].review_version=3;
      return post.promise;
    },
    requestQuestionDetail:()=>{
      events.push('GET-stalled');
      return get.promise;
    },
    selectQuestion:()=>{events.push('navigate-GET-stalled');
      assert.equal(vm.runInContext('cachedQuestion('+JSON.stringify(nextId)+')',ctx),null);
      return nav.promise;
    },
  });
  const api=vm.runInContext(code+'\n({sendReview,cachedQuestion})',ctx);
  const task=api.sendReview('verified');
  await ticks();
  if(kind==='already-stale') {
    assert.equal(events[0],'GET-stalled','Stale raw cache must take conservative branch');
    assert.equal(events[1],'POST');
    assert.equal(state.pendingReviews.size,0,'Must not start optimistic approval on stale cache');
    assert.equal(state.saving,true);
  } else {
    assert.equal(events[0],'POST');
    assert.equal(events[1],'navigate-GET-stalled');
    assert.equal(state.pendingReviews.size,1,'Optimistic request is pending, not approved');
    assert.equal(rows[0].status,'needs_manual_review');
  }
  // Do NOT resolve the successor GET. POST reconciliation must still finish.
  if(failure) {
    const error=new Error(failure==='conflict'?'stale 409':'ACK lost/offline');
    if(failure==='conflict')error.status=409;
    post.reject(error);
  } else {
    post.resolve({id,number:1,version:2,request_version:1,saved:true,
      review_status:'verified',requires_image:false});
  }
  if(kind==='already-stale' && !failure) {
    // Conservative success may subsequently wait for navigation. It must
    // commit/release the POST lock before any GET settles.
    await ticks();
    assert.equal(state.saving,false);
    assert.equal(rows[0].status,'verified');
    get.resolve({id:nextId}); // test's GET prefetch is non-blocking
    nav.resolve();
  }
  await task;
  assert.equal(state.pendingReviews.size,0,'POST completion must clear pending');
  assert.equal(state.saving,false,'POST completion must not retain save lock');
  assert.equal(rows[0].status,failure?'needs_manual_review':'verified');
  // An optimistic 409 already left its editor; preserve it as a failed-review
  // recovery item instead of reloading the unrelated successor. Conservative
  // 409 has never navigated and retains its existing editor input in place.
  const needsRecovery = failure==='offline' ||
    (kind==='becomes-stale' && failure==='conflict');
  assert.equal(state.failedReviews.size,needsRecovery?1:0);
  if(failure==='offline'){
    assert.equal(state.failedReviews.get(id).input.stem,'edited');
    assert.equal(state.failedReviews.get(id).input.note,'human note');
  }
  if(failure==='conflict')assert.ok(notices.some(x=>x.type==='error'));
  // Reject the STILL-unresolved navigation after the POST has completed:
  // the handler must consume it without causing an unhandled rejection.
  if(kind==='becomes-stale') nav.reject(new Error('successor GET failed'));
  await ticks();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(unhandled,0,'No unhandled POST or GET rejection');
}
(async()=>{
  await scenario('already-stale','conflict');
  await scenario('already-stale','offline');
  await scenario('becomes-stale',null);
  await scenario('becomes-stale','offline');
  await scenario('becomes-stale','conflict');
  console.log('Stale raw cache rejected, stalled navigation independent POST ACK, no unhandled rejection PASS');
})().catch(error=>{console.error(error);process.exitCode=1});
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class TestReviewUIPrefetch(unittest.TestCase):
    def run_node(self, script):
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(HTML)],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_detail_prefetch_dedupe_generation_pending_offline(self):
        self.run_node(NODE_PREFETCH)

    def test_safe_next_get_overlaps_post_without_approval_claim(self):
        self.run_node(NODE_PARALLEL_APPROVAL)

    def test_cached_selection_is_synchronous_and_late_get_cannot_steal_focus(self):
        self.run_node(NODE_FAST_NAVIGATION)

    def test_stale_raw_cache_and_stalled_navigation_do_not_block_approval_ack(self):
        self.run_node(NODE_STALE_SUCCESSOR_RACE)
