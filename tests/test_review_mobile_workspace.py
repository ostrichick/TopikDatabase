"""Mobile reviewer acceptance: actual inline-JS interactions and semantic HTML.

The Node fixture runs the same script and DOM handlers as the review page, but
never opens a browser, starts a server, or modifies the review database.
"""

import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


HTML = Path(__file__).resolve().parents[1] / "src" / "review_ui.html"


class Outline(HTMLParser):
    VOID = {"input", "img", "meta", "link", "br", "hr", "source", "wbr"}

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.by_id = {}
        self.entries = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        entry = {"tag": tag, "attrs": attrs, "parents": tuple(self.stack), "text": ""}
        self.entries.append(entry)
        if attrs.get("id"):
            self.by_id.setdefault(attrs["id"], []).append(entry)
        if tag not in self.VOID:
            self.stack.append(entry)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        for entry in self.stack:
            entry["text"] += data


NODE = r"""
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(process.argv[1],'utf8');
const inline=html.match(/<script>([\s\S]*?)<\/script>/);
assert(inline,'Reviewer inline JS missing');
// Preserve all real declarations and handlers; only bypass the API init.
const code=inline[1].replace(/\n\s*init\(\);\s*\n\s*\}\)\(\);\s*$/,
  `\n      renderPdf = () => {};\n      renderAudioSegment = () => {};\n      renderAiAudit = () => {};\n      renderHistory = () => {};\n      renderIndependentComparison = () => {};\n      renderReviewDecisionCue = () => {};\n      refreshAudioActions = () => {};\n      updateNavigation = () => {};\n      globalThis.__reviewTest = { state, filterItems, selectQuestion, installHandlers, initCompactExplorer, renderDetail, dirty, draft };\n    })();`);
assert.notEqual(code,inline[1],'Unable to intercept init before testing');

const width=Number(process.argv[2]);
const events=[];
const nodes=new Map();
let created=0;
let replacements=0;
let confirmReturn=false;
let currentDocument=null;
class Element {
  constructor(tagName='div',id=''){
    this.tagName=tagName.toUpperCase(); this.id=id; this.dataset={}; this.listeners={};
    this.attributes={};this.children=[];this._text='';this.hidden=false;this.disabled=false;
    this.open=false;this.value='';this.style={};this.scrollLeft=0;this.scrollTop=0;
    this.scrollWidth=2200;this.clientWidth=width;this.scrollHeight=900;this.clientHeight=220;
    this.className='';this._classes=new Set();
    this.classList={
      add:(...tokens)=>tokens.forEach(x=>this._classes.add(x)),
      remove:(...tokens)=>tokens.forEach(x=>this._classes.delete(x)),
      contains:x=>this._classes.has(x),
      toggle:(x,force)=>{
        const on=force===undefined?!this._classes.has(x):!!force;
        if(on)this._classes.add(x);else this._classes.delete(x);
        return on;
      }
    };
  }
  get textContent(){return this._text+this.children.map(x=>x.textContent||'').join('');}
  set textContent(text){this._text=String(text);this.children=[];}
  get lastElementChild(){return this.children.at(-1)||null;}
  get isConnected(){return true;}
  setAttribute(name,value){this.attributes[name]=String(value);}
  getAttribute(name){return this.attributes[name]??null;}
  removeAttribute(name){delete this.attributes[name];}
  addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);}
  async click(){const results=(this.listeners.click||[]).map(fn=>fn({currentTarget:this,target:this,preventDefault(){},stopPropagation(){}}));await Promise.all(results);}
  append(...items){this.children.push(...items);}
  appendChild(item){this.children.push(item);return item;}
  replaceChildren(...items){replacements++;this.children=[...items];this._text='';}
  insertBefore(item,before){
    const previous=this.children.indexOf(item);if(previous>=0)this.children.splice(previous,1);
    const index=before?this.children.indexOf(before):-1;
    if(index<0)this.children.push(item);else this.children.splice(index,0,item);
    return item;
  }
  removeChild(item){const index=this.children.indexOf(item);assert(index>=0);this.children.splice(index,1);}
  querySelector(selector){
    if(selector==='.question-item.active')
      return this.children.find(x=>x.classList.contains('active'))||null;
    if(selector==='summary')return this.children.find(x=>x.tagName==='SUMMARY')||null;
    return null;
  }
  querySelectorAll(){return [];}
  getBoundingClientRect(){
    if(this.id==='questionList')return {top:50,bottom:250,left:0,right:width};
    return {top:70,bottom:125,left:40,right:125};
  }
  getClientRects(){return [{width:100,height:30}];}
  scrollIntoView(options){events.push(['scroll',this.id,options]);}
  focus(options){currentDocument.activeElement=this;events.push(['focus',this.id,options]);}
  pause(){}
  load(){}
}
const $=id=>{
  if(!nodes.has(id))nodes.set(id,new Element('div',id));
  return nodes.get(id);
};
for(const id of ['questionSearch','sectionFilter','statusFilter','imageFilter','aiFilter',
                  'sortOrder','indicatorFilter']){
  $(id).value=id==='questionSearch'?'':'all';
}
const document={
  title:'', activeElement:null, createElement(tag){created++;return new Element(tag);},
  getElementById:$, querySelectorAll:()=>[],addEventListener:()=>{},
  body:{classList:new Element().classList}
};
currentDocument=document;
const window={
  location:{search:'?exam_id=035-I-B',href:'http://127.0.0.1:8765/?exam_id=035-I-B',origin:'http://127.0.0.1:8765'},
  matchMedia:query=>({matches: query.includes('max-width: 760px')?width<=760:query.includes('max-width: 1250px')?width<=1250: false}),
  confirm:()=>confirmReturn,addEventListener:()=>{},
};
const ctx={window,document,URL,URLSearchParams,console,location:window.location,
  setTimeout:()=>1,clearTimeout:()=>{},AbortController,
  fetch:async()=>{throw Error('Unexpected network request without explicit fixture');}};
vm.runInNewContext(code,ctx);
const api=ctx.__reviewTest;
assert(api,'Test instrumentation did not expose reviewer functions');
const state=api.state;
const rows=Array.from({length:70},(_,i)=>({
  id:'035-I-L-'+String(i+1).padStart(3,'0'),number:i+1,section:'listening',
  status:'needs_manual_review',review_version:i+1,requires_image:false
}));
const details={};
for(let i=1;i<=4;i++){
  const id=rows[i-1].id;
  details[id]={id,number:i,exam_id:'035-I-B',section:'listening',review_status:'needs_manual_review',
    version:i,points:2,stem:'원문 '+i,choices:[1,2,3,4].map(n=>({number:n,text:'보기'+n})),
    transcript:{text:'대본 '+i,warnings:[{severity:'review',code:'transcript',message:'검토'}]},
    preview_flags:[{severity:'review',code:'question',message:'원본 대조'}],source_pdf_page:1};
}
state.items=rows;state.visible=rows.slice();state.selectedId=rows[0].id;
state.detail=details[rows[0].id];state.examId='035-I-B';state.detailsCache={...details};
state.items[0].status='verified';state.items[0].review_version=9;
state.committedReviews=new Map([[rows[0].id,{status:'verified',version:9}]]);
state.csrfToken='locked-csrf-fixture';
state.capabilities={reviewWrite:true};state.pendingReviews=new Map();
state.failedReviews=new Map();state.audio={saving:false,preview:null,heardSignature:null};
state.questionNodes=new Map();
state.independentAudits=null;state.loading=false;state.saving=false;
state.bundleProtectedIds=new Set();
// On both desktop and mobile the rail precedes secondary, optional controls.
$('advancedFilters').open=true;
api.initCompactExplorer();
assert.equal($('advancedFilters').open,false,'Secondary controls should not bury navigation');
$( 'stemInput' ).value=details[rows[0].id].stem;
for(let n=1;n<=4;n++)$('choice'+n).value='보기'+n;
$('transcriptInput').value=details[rows[0].id].transcript.text;
$('reviewNote').value='';
state.baseline=JSON.stringify(api.draft());
api.filterItems();
assert.equal($('questionList').children.length,70,'Initial rail contains 70 items');
assert.equal(state.questionNodes.size,70);
const originalButtons=new Map(state.questionNodes);
const beforeReplacements=replacements;
const button=state.questionNodes.get(rows[1].id);
assert.equal(button.tagName,'BUTTON','Rail items are native keyboard buttons');
assert.equal(button.getAttribute('aria-current'),'false');
await button.click();
assert.equal(state.selectedId,rows[1].id,'Question click switches to target');
assert.equal(state.detail.id,rows[1].id);
assert.equal(state.detail.version,2,'Use preserved detail review version');
assert.equal($('editorTitle').textContent,'2번 문항');
assert.equal(state.questionNodes.size,70,'Click must not evict cached question buttons');
for(const [id,node] of originalButtons)assert.equal(state.questionNodes.get(id),node);
assert.equal(state.questionNodes.get(rows[0].id).getAttribute('aria-current'),'false');
assert.equal(button.getAttribute('aria-current'),'true');
assert.equal(state.items[0].status,'verified','Changing questions must preserve prior ACK');
assert.equal(state.items[0].review_version,9,'Changing questions cannot revert ACK review version');
assert.equal(state.committedReviews.get(rows[0].id).version,9);
assert.equal(state.csrfToken,'locked-csrf-fixture','Navigation cannot replace CSRF context');
assert.equal(state.examId,'035-I-B','Navigation cannot cross exam scopes');
assert.ok(replacements-beforeReplacements<12,
  'Selection-only change must not reconstruct all 70 question buttons');
if(width<=760){
  assert.ok(events.some(x=>x[0]==='focus'&&x[1]==='editorTitle'),
    'User selection focuses editor heading on mobile');
  assert.ok(events.some(x=>x[0]==='scroll'&&x[1]==='editorTitle'),
    'User selection scrolls toward editor heading on mobile');
}
// Dirty editor refuses to discard an unsaved draft after rail clicks.
$('stemInput').value='미저장 수정 내용';
const initialRequestId=state.requestId;
await state.questionNodes.get(rows[2].id).click();
assert.equal(state.selectedId,rows[1].id,'Dirty draft blocks rail navigation');
assert.equal($('stemInput').value,'미저장 수정 내용');
assert.equal(state.requestId,initialRequestId,'Blocked click sends no detail request');

// Quick action buttons invoke real installed handlers and retain review state.
$('stemInput').value=details[rows[1].id].stem;
api.installHandlers();
for(const id of ['jumpSource','jumpAiEvidence','jumpWarnings']){
  assert.ok($(id).listeners.click?.length, id+' needs an actual click handler');
}
const keptVersion=state.detail.version;
const keptBaseline=state.baseline;
const keptSelected=state.selectedId;
for(const id of ['jumpSource','jumpAiEvidence','jumpWarnings']){
  events.length=0;
  await $(id).click();
  const expected={jumpSource:'sourceReference',jumpAiEvidence:'independentComparisonSection',
    jumpWarnings:'flagList'}[id];
  assert.ok(events.some(x=>x[0]==='scroll'&&x[1]===expected),
    id+' click must scroll to '+expected);
  assert.equal(state.selectedId,keptSelected,id+' cannot navigate to a different question');
  assert.equal(state.detail.version,keptVersion,id+' cannot alter review version');
  assert.equal(state.baseline,keptBaseline,id+' cannot change saved editor baseline');
  if(document.body.classList.contains('reference-open'))await $('closeReference').click();
}
assert.match($('jumpWarnings').textContent,/2/,
  'Quick warning shortcut exposes combined question + transcript warning count');
assert.equal($('approveQuestion').disabled,false,'Quick actions leave approval accessible');
// Existing keyboard/approval navigation invokes selectQuestion without
// focusEditor; it must not move focus away from an ongoing review operation.
events.length=0;
details[rows[2].id].preview_flags=[];
details[rows[2].id].transcript.warnings=[];
await api.selectQuestion(rows[2].id);
assert.equal(state.selectedId,rows[2].id);
assert.ok(!events.some(x=>x[0]==='focus'&&x[1]==='editorTitle'),
  'Programmatic next-question navigation must not steal editor-heading focus');
assert.equal($('jumpWarnings').hidden,true,
  'Question with no warnings must not show an irrelevant warning shortcut');
assert.ok($('flagList').textContent.includes('제공된 추출 주의사항이 없습니다'),
  'No-warning source panel still gives usable guidance');
assert.equal(state.csrfToken,'locked-csrf-fixture');
console.log('mobile workspace '+width+'px: 70 cached nodes, selected highlight, click focus, dirty guard, quick actions PASS');
"""


class MobileMarkupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML.read_text(encoding="utf-8")
        cls.doc = Outline(cls.html)

    def one(self, id_):
        entries = self.doc.by_id.get(id_, [])
        self.assertEqual(len(entries), 1, f"{id_} must occur exactly once")
        return entries[0]

    def test_core_filters_stay_outside_native_advanced_details(self):
        advanced = self.one("advancedFilters")
        self.assertEqual(advanced["tag"], "details", "Use keyboard-accessible native details")
        self.assertTrue(any(entry["tag"] == "summary" and advanced in entry["parents"]
                            for entry in self.doc.entries), "Advanced filters need a native summary")
        for id_ in ("questionSearch", "statusFilter"):
            self.assertNotIn(advanced, self.one(id_)["parents"],
                             f"{id_} must remain visible while advanced options are closed")
        for id_ in ("imageFilter", "aiFilter", "sortOrder"):
            self.assertIn(advanced, self.one(id_)["parents"], f"{id_} belongs in advanced filters")
        self.assertEqual(self.one("questionList")["tag"], "nav")
        self.assertEqual(self.one("editorTitle")["attrs"].get("tabindex"), "-1",
                         "Focusable heading supports keyboard/assistive navigation")

    def test_jump_actions_are_keyboard_buttons_with_valid_targets(self):
        for id_ in ("jumpSource", "jumpAiEvidence", "jumpWarnings"):
            item = self.one(id_)
            self.assertEqual(item["tag"], "button", id_)
            self.assertEqual(item["attrs"].get("type"), "button", id_)
            self.assertTrue(item["attrs"].get("aria-label") or item["text"].strip(),
                            f"{id_} needs a readable accessible name")
            controlled = item["attrs"].get("aria-controls")
            if controlled:
                for name in controlled.split():
                    self.one(name)
        # Count can be exposed in the accessible button label itself.
        self.assertTrue(self.one("jumpWarnings")["text"].strip())

    def test_responsive_css_has_compact_horizontal_rail(self):
        css = self.html.split("<style>", 1)[1].split("</style>", 1)[0]
        self.assertRegex(css, r"@media\s*\(max-width:\s*760px\)")
        self.assertRegex(css, r"@media\s*\(max-width:\s*420px\)")
        self.assertRegex(css, r"\.question-list\s*\{[^}]*overflow-x:\s*auto",
                         "At 320/390px the question rail must scroll horizontally")
        self.assertRegex(css, r"\.advanced-filters\s*\{",
                         "Native advanced details need visible disclosure styling")
        self.assertIn("questionSearch", self.html)
        self.assertIn("statusFilter", self.html)


@unittest.skipUnless(shutil.which("node"), "Node.js is required for real frontend DOM tests")
class MobileScriptTests(unittest.TestCase):
    def test_desktop_and_small_mobile_navigation(self):
        for width in (1280, 390, 320):
            with self.subTest(viewport=width):
                result = subprocess.run(
                    [shutil.which("node"), "-e",
                     "(async()=>{"+NODE+"})().catch(error=>{console.error(error);process.exitCode=1});",
                     str(HTML), str(width)],
                    capture_output=True, text=True, encoding="utf-8", timeout=15,
                    check=False,
                )
                self.assertEqual(result.returncode, 0,
                                 f"viewport {width}: {result.stdout}\n{result.stderr}")
                self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
