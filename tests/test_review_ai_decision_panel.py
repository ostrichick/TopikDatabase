"""Acceptance tests for the AI decision panel in the real review workspace.

Only the browser's inline JS runs, with initialization suppressed and a small
deterministic DOM. There are no database accesses, review writes, or requests
to a running review server.
"""

from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


HTML = Path(__file__).resolve().parents[1] / "src" / "review_ui.html"
IDS = (
    "aiDecisionPanel", "aiDecisionHeading", "aiDecisionSummary",
    "aiDecisionChecks", "aiDecisionSource", "aiDecisionStatus",
    "aiDecisionOpenEvidence",
)


class Elements(HTMLParser):
    VOID = {"br", "hr", "img", "input", "link", "meta", "source", "wbr"}

    def __init__(self, text):
        self.nodes = {}
        self.stack = []
        self.entries = []
        super().__init__(convert_charrefs=True)
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        item = {"tag": tag, "attrs": dict(attrs), "ancestors": tuple(self.stack), "text": ""}
        self.entries.append(item)
        if item["attrs"].get("id"):
            self.nodes.setdefault(item["attrs"]["id"], []).append(item)
        if tag not in self.VOID:
            self.stack.append(item)

    def handle_endtag(self, tag):
        for n in range(len(self.stack) - 1, -1, -1):
            if self.stack[n]["tag"] == tag:
                del self.stack[n:]
                break

    def handle_data(self, data):
        for node in self.stack:
            node["text"] += data


class DecisionPanelStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML.read_text(encoding="utf-8")
        cls.dom = Elements(cls.html)

    def unique(self, id_):
        matches = self.dom.nodes.get(id_, [])
        self.assertEqual(len(matches), 1, f"#{id_} must exist once")
        return matches[0]

    def test_semantic_panel_and_accessible_evidence_action(self):
        for id_ in IDS:
            self.unique(id_)
        panel = self.unique("aiDecisionPanel")
        heading = self.unique("aiDecisionHeading")
        self.assertIn(panel["tag"], {"section", "aside", "article"})
        self.assertIn(heading["tag"], {"h2", "h3", "h4"})
        self.assertIn(panel, heading["ancestors"])
        self.assertEqual(panel["attrs"].get("aria-labelledby"), "aiDecisionHeading")
        for id_ in ("aiDecisionSummary", "aiDecisionChecks", "aiDecisionSource", "aiDecisionStatus"):
            self.assertIn(panel, self.unique(id_)["ancestors"])
        checks = self.unique("aiDecisionChecks")
        self.assertTrue(checks["tag"] in {"ul", "ol"} or checks["attrs"].get("role") == "list",
                        "Follow-up checks should be navigable as a list")
        evidence = self.unique("aiDecisionOpenEvidence")
        self.assertEqual(evidence["tag"], "button")
        self.assertEqual(evidence["attrs"].get("type"), "button")
        self.assertIn(panel, evidence["ancestors"])
        self.assertTrue(evidence["attrs"].get("aria-label") or evidence["text"].strip(),
                        "Evidence button needs a keyboard/AT-accessible name")
        for target in evidence["attrs"].get("aria-controls", "").split():
            self.unique(target)

    def test_decision_panel_is_wired_to_detail_and_async_ai_summary(self):
        html = self.html
        self.assertRegex(html, r"function\s+renderAiDecisionPanel\s*\(")
        detail = html.split("function renderDetail()", 1)
        self.assertEqual(len(detail), 2, "Actual detail-render function is required")
        # Scope to the function body up to the next declaration, rather than
        # simply accepting a call somewhere unrelated in the file.
        self.assertIn("renderAiDecisionPanel()", detail[1].split("function renderPdf()", 1)[0])
        async_source = html.split("async function loadAiSummary(", 1)
        self.assertEqual(len(async_source), 2)
        self.assertIn("renderAiDecisionPanel()",
                      async_source[1].split("async function loadList()", 1)[0],
                      "A late bulk result must refresh the open decision panel")


NODE = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const inline = html.match(/<script>([\s\S]*?)<\/script>/);
assert(inline, 'Review UI inline script was not found');

// Execute the actual IIFE and handlers, not a reimplementation. Skip only
// network initialization and noisy, unrelated media/audit-detail renderers.
const replacement = `
      renderPdf = () => {};
      renderReferences = () => {};
      renderAiAudit = () => {};
      renderHistory = () => {};
      renderIndependentComparison = () => {};
      renderReviewDecisionCue = () => {};
      refreshAudioActions = () => {};
      refreshDirty = () => {};
      updateNavigation = () => {};
      filterItems = () => {};
      globalThis.__test = { state, renderAiDecisionPanel, renderDetail,
        installHandlers, loadAiSummary, draft };
    })();`;
const code = inline[1].replace(/\n\s*init\(\);\s*\n\s*\}\)\(\);\s*$/, replacement);
assert.notEqual(code, inline[1], 'Cannot intercept initialization safely');

const elements = new Map(), events = [];
let networkCalls = [];
class Element {
  constructor(tag='div', id='') {
    this.tagName=tag.toUpperCase(); this.id=id; this.dataset={};
    this.listeners={}; this.hidden=false; this.disabled=false;
    this._text=''; this.children=[]; this.value=''; this.style={};
    this.open=false; this.attributes={}; this.className='';
    const classes = new Set();
    this.classList={contains:x=>classes.has(x), add:(...xs)=>xs.forEach(x=>classes.add(x)),
      remove:(...xs)=>xs.forEach(x=>classes.delete(x)),
      toggle:(x,force)=>{const set=force===undefined?!classes.has(x):Boolean(force);
        set?classes.add(x):classes.delete(x);return set;}};
  }
  set textContent(text){this._text=String(text);this.children=[];}
  get textContent(){return this._text+this.children.map(x=>x.textContent||'').join('');}
  get lastElementChild(){return this.children.at(-1)||null;}
  setAttribute(name,value){this.attributes[name]=String(value);}
  getAttribute(name){return this.attributes[name]??null;}
  removeAttribute(name){delete this.attributes[name];}
  addEventListener(kind,cb){(this.listeners[kind]??=[]).push(cb);}
  async click(){for(const cb of this.listeners.click||[])await cb({target:this,currentTarget:this,
    preventDefault(){},stopPropagation(){}});}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this._text='';this.children=[...nodes];}
  querySelector(selector){if(selector==='summary')return get(this.id+'-summary');return null;}
  querySelectorAll(){return [];}
  focus(opts){events.push(['focus',this.id]);}
  scrollIntoView(opts){events.push(['scroll',this.id]);}
  pause(){}
  load(){}
}
function get(id) {if(!elements.has(id))elements.set(id,new Element('div',id));return elements.get(id);}
const location = {href:'http://127.0.0.1:8888/?exam_id=035-I-B',
  search:'?exam_id=035-I-B',hostname:'127.0.0.1',protocol:'http:',origin:'http://127.0.0.1:8888'};
const document={title:'',getElementById:get, createElement:tag=>new Element(tag),
  activeElement:null,querySelectorAll:()=>[],addEventListener:()=>{},body:{classList:new Element().classList}};
const window={location,confirm:()=>true,addEventListener:()=>{},
  matchMedia:()=>({matches:false})};
const ctx={window,document,location,URL,URLSearchParams,AbortController,
  setTimeout:()=>1,clearTimeout:()=>{},console,
  fetch:async(url,options)=>{
    networkCalls.push({url,options});
    if(!nextResponse)throw Error('Unexpected network call: '+url);
    const response=nextResponse;nextResponse=null;
    return {ok:true,json:async()=>response};
  }};
let nextResponse=null;
vm.runInNewContext(code,ctx);
const api=ctx.__test;
assert(api && typeof api.renderAiDecisionPanel==='function');
const state=api.state;

const BULK={total:2,clear:1,finding:1,uncertain:0,unresolved_findings:1,
  risk_score:83,risk_level:'high',disagreement:false,attempt_total:2,
  latest_run:{run_id:'run-35-evidence',pass_total:2,completed_passes:2}};
const qid='035-I-L-012';
const first={id:qid,number:12,section:'listening',status:'verified',review_version:8,
  ai_audit:{...BULK}};
const detail={id:qid,exam_id:'035-I-B',number:12,section:'listening',
  review_status:'verified',version:8,source_pdf_page:11,points:2,stem:'원문',
  choices:[1,2,3,4].map(number=>({number,text:'선택지'+number})),
  transcript:{text:'대본',source_pdf_page:41},answer:{choice_number:2,source_pdf_page:18},
  preview_flags:[],group:{},history:[]};
state.items=[first];state.detail=detail;state.selectedId=qid;
state.examId='035-I-B';state.aiAuditAvailable=true;
state.aiSummaryStatus='ready';state.independentAudits=null;
state.capabilities={reviewWrite:true};state.aiHistoryLoadedIds=new Set();
state.pendingReviews=new Map();state.failedReviews=new Map();
state.detailsCache={[qid]:detail}; state.bundleProtectedIds=new Set();
state.questionNodes=new Map();state.audio={available:false,saving:false,preview:null,heardSignature:null};
state.committedReviews=new Map([[qid,{status:'verified',version:8}]]);
state.baseline='PRESERVE BASELINE';state.csrfToken='PRESERVE CSRF';
get('stemInput').value='unsaved draft';
for(let n=1;n<=4;n++)get('choice'+n).value='unsaved '+n;
get('transcriptInput').value='unsaved transcript';
get('reviewNote').value='unsaved note';
let draftBefore=['stemInput','choice1','choice2','transcriptInput','reviewNote'].map(id=>get(id).value);
const previous={id:state.selectedId,version:detail.version,status:detail.review_status,
  rowStatus:first.status,rowVersion:first.review_version,baseline:state.baseline,
  csrf:state.csrfToken,pending:state.pendingReviews.size};
function invariant(){
  assert.equal(state.detail,detail,'Panel must not replace selected detail');
  assert.equal(state.selectedId,previous.id);
  assert.equal(detail.version,previous.version);
  assert.equal(detail.review_status,previous.status);
  assert.equal(first.status,previous.rowStatus);
  assert.equal(first.review_version,previous.rowVersion);
  assert.equal(state.baseline,previous.baseline);
  assert.equal(state.csrfToken,previous.csrf);
  assert.equal(state.pendingReviews.size,previous.pending);
  assert.deepEqual(['stemInput','choice1','choice2','transcriptInput','reviewNote'].map(id=>get(id).value),draftBefore);
}
function panelText(){return ['aiDecisionHeading','aiDecisionSummary','aiDecisionChecks','aiDecisionSource','aiDecisionStatus']
  .map(id=>get(id).textContent).join(' ');}

// A 35th list row has useful bulk evidence even before opening detailed history.
api.renderAiDecisionPanel();
assert.equal(get('aiDecisionPanel').hidden,false,'The decision panel must be visible');
assert.match(get('aiDecisionSummary').textContent,/AI|감사|발견|위험|대조|확인/);
assert.match(panelText(),/확인|대조|검수|근거/);
assert.ok(get('aiDecisionChecks').children.length || get('aiDecisionChecks').textContent.trim(),
  'Decision panel should provide actionable checks');
assert.match(get('aiDecisionSource').textContent,/11\s*쪽|11\s*페이지|PDF\s*11/,
  'Only the detail supplied paper page 11 may be presented as source location');
assert.doesNotMatch(get('aiDecisionSource').textContent,/86\s*쪽|86\s*페이지/,
  'No AI-generated or ungrounded page number may appear');
// Other source-document page values are permitted only when explicitly labeled
// and grounded by the matching answer/transcript fields in the selected detail.
assert.match(get('aiDecisionSource').textContent,/정답표\s*18\s*쪽/);
assert.match(get('aiDecisionSource').textContent,/대본\s*41\s*쪽/);
assert.match(panelText(),/승인만으로 판단 불가|원본.*확인|사람.*별개|확정.*아니/,
  'Human verification cannot silently certify AI evidence was addressed');
assert.doesNotMatch(panelText(),/AI\s*(?:지적|발견|결함)\s*(?:모두\s*)?(?:검토 완료\.|해결 완료\.|확인 완료\.)/,
  'Never affirm that AI findings were reviewed or resolved');
assert.equal(networkCalls.length,0,'Rendering must never request extra audit detail or write');
invariant();

// Specific independent AI explanations must be carried forward verbatim, not
// replaced with a generic risk score or invented edit instruction.
state.independentAudits={questions:{'12':{gemini:{verdict:'finding',summary:'근거 있는 AI 발견 설명'}}}};
api.renderAiDecisionPanel();
assert.match(panelText(),/근거 있는 AI 발견 설명/);
assert.equal(networkCalls.length,0);
state.independentAudits=null;
invariant();

// Critical extraction warnings are more useful than a bare count, but are
// displayed verbatim rather than converted into invented fix instructions.
detail.preview_flags=[{severity:'critical',message:'CRITICAL_SOURCE_TEXT_RECHECK'}];
api.renderAiDecisionPanel();
assert.match(panelText(),/CRITICAL_SOURCE_TEXT_RECHECK/);
assert.match(panelText(),/추출|주의|경고/);
detail.preview_flags=[];
invariant();

// The actual review-detail route must refresh this panel without async fetches.
state.baseline='PRESERVE BASELINE';
api.renderDetail();
assert.equal(get('aiDecisionPanel').hidden,false);
assert.equal(networkCalls.length,0,'Opening a cached detail must not request AI evidence');
assert.equal(detail.version,8);assert.equal(detail.review_status,'verified');
assert.equal(state.selectedId,qid);
// renderDetail intentionally initializes form inputs/baseline; subsequent AI
// rendering must never replace new unsaved changes to that initialized form.
previous.baseline=state.baseline;
get('stemInput').value='new unsaved edit after renderDetail';
draftBefore=['stemInput','choice1','choice2','transcriptInput','reviewNote'].map(id=>get(id).value);

// Missing locations should be stated as unknown rather than guessed from audit
// findings, answer keys, question numbers, or generic default page 1.
detail.source_pdf_page=null;
detail.answer.source_pdf_page=null;
detail.transcript.source_pdf_page=null;
api.renderAiDecisionPanel();
assert.doesNotMatch(get('aiDecisionSource').textContent,/\b\d+\s*(?:쪽|페이지)|PDF\s*\d+/);
assert.match(get('aiDecisionSource').textContent,/원본|PDF|페이지|위치|미확인|확인/);
assert.equal(networkCalls.length,0);

// A null audit is unknown, never a proven "clear" verdict or AI approval.
first.ai_audit=null;
state.aiSummaryStatus='loading';state.aiAuditAvailable=false;
api.renderAiDecisionPanel();
assert.match(panelText(),/미평가|미실행|미확인|미수집|불러오는|로딩|확인 필요|자료 부족|정보 없음|결과 없음|아직|대기|확인 중/);
assert.doesNotMatch(panelText(),/AI.{0,12}(이상 없음|승인 완료|검수 완료|완료 검증)/);
invariant();
state.aiSummaryStatus='ready';state.aiAuditAvailable=true;
api.renderAiDecisionPanel();
assert.match(panelText(),/미평가|미감사|조회.*없|자료.*없|결과.*없|확인.*없|기록.*없/,
  'A completed, empty summary is not an endlessly loading AI evaluation');
assert.doesNotMatch(get('aiDecisionSummary').textContent,/이상 없음으로 확인|문제 없음으로 확정/);
invariant();

// Simulate the real asynchronous bulk-list endpoint. Its completed response
// should refresh the selected panel, not just the rail or background filters.
state.aiSummaryStatus='loading';state.aiAuditAvailable=false;
state.listGeneration=7;state.aiSummaryPoll={generation:7,examId:'035-I-B',
  startedAt:Date.now(),retries:0,timer:null,controller:null};
nextResponse={exam_id:'035-I-B',state:'ready',ai_audit_available:true,
  items:[{id:qid,ai_audit:{...BULK,unresolved_findings:3,risk_score:92}}]};
await api.loadAiSummary(state.aiSummaryPoll);
assert.equal(networkCalls.length,1,'Only async enrichment should issue GET');
assert.ok(networkCalls[0].url.includes('/api/questions-ai-summary'));
assert.equal(state.items[0].ai_audit.unresolved_findings,3);
assert.equal(get('aiDecisionPanel').hidden,false);
assert.match(panelText(),/3|발견|위험|재검토|확인/,
  'Panel must show available, updated evidence rather than a stale loading state');
invariant();

// The new F3 run must not inherit detailed finding text from an older cached
// run simply because the user has already opened that older detail once.
state.items[0].ai_audit.run_id='new-run';
detail.ai_audit={...BULK,run_id:'old-run',entries:[{verdict:'finding',summary:'STALE_DETAIL_MUST_NOT_APPEAR'}]};
api.renderAiDecisionPanel();
assert.doesNotMatch(panelText(),/STALE_DETAIL_MUST_NOT_APPEAR/,
  'Old detailed findings cannot be presented as the latest audit result');
assert.match(panelText(),/3/,'Updated summary must remain authoritative');
delete detail.ai_audit;
invariant();

// The evidence button uses the real event handler and opens/focuses details.
api.installHandlers();
assert.ok(get('aiDecisionOpenEvidence').listeners.click?.length,
  'Evidence action needs an actual click listener');
events.length=0;
await get('aiDecisionOpenEvidence').click();
assert.ok(['aiAuditSection','independentComparisonSection'].some(id=>get(id).open),
  'Evidence action must open a specific detail section');
assert.ok(events.some(x=>x[0]==='focus') && events.some(x=>x[0]==='scroll'),
  'Evidence action should place keyboard focus and scroll to evidence');
invariant();

// A different exam never inherits 35th AI findings, even if a stale row is
// accidentally present. 36th unsupported state must remain explicit.
state.examId='036-I-B';state.aiSummaryStatus='unavailable';state.aiAuditAvailable=false;
detail.exam_id='036-I-B';detail.id='036-I-L-012';
state.selectedId=detail.id;
state.items=[{...first,id:detail.id,number:12,ai_audit:null}];
const before36={status:detail.review_status,version:detail.version,baseline:state.baseline};
api.renderAiDecisionPanel();
assert.match(panelText(),/미지원|지원하지|이용할 수 없|사용할 수 없|제공되지|해당 회차|자료 없음|미평가/,
  '36th must explicitly distinguish unavailable audit coverage');
state.items[0].ai_audit={...BULK,latest_run:{run_id:'run-35-leak'}};
api.renderAiDecisionPanel();
assert.match(panelText(),/미지원|지원하지|이용할 수 없|사용할 수 없|제공되지|해당 회차|자료 없음|미평가/,
  'Stale data must not override the actual unsupported exam contract');
assert.doesNotMatch(panelText(),/run-35-leak|run-35-evidence/);
assert.equal(detail.review_status,before36.status);
assert.equal(detail.version,before36.version);
assert.equal(state.baseline,before36.baseline);
assert.equal(networkCalls.length,1,'Re-rendering unsupported exam must not fetch 35th data');
console.log('AI decision panel: structural evidence, grounded page, unknown/unavailable, async update, focus, no mutation PASS');
"""


@unittest.skipUnless(shutil.which("node"), "Node.js required for actual reviewer JS")
class DecisionPanelJsTests(unittest.TestCase):
    def test_real_panel_render_async_update_and_evidence_navigation(self):
        # On Windows `node -e` can mojibake Korean arguments via the process
        # code page. Use a temporary UTF-8 script to test actual Korean text.
        with tempfile.TemporaryDirectory(prefix="topik-ai-panel-test-") as temp:
            script = Path(temp) / "decision_panel.js"
            script.write_text(
                "(async()=>{" + NODE + "})().catch(error=>{console.error(error);process.exitCode=1})",
                encoding="utf-8",
            )
            result = subprocess.run(
                [shutil.which("node"), str(script), str(HTML)],
                capture_output=True, text=True, encoding="utf-8", timeout=20,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
