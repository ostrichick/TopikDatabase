"""Original-source reviewer contract, with immutable 35th corpus as provenance.

SQLite is opened with mode=ro plus PRAGMA query_only. All Node browser behavior
uses the actual inline review_ui.html script and fake DOM; no local server, live
PostgreSQL or original media is ever written.
"""

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "review_ui.html"
FROZEN_DB = ROOT / "topik-past-papers" / "derived" / "035-I-B.sqlite"
PAIRS = ((25, 26), (27, 28), (29, 30))


def source_rows():
    """Return only six 35th rows using an explicitly read-only SQLite handle."""
    if not FROZEN_DB.is_file():
        raise AssertionError(f"Frozen SQLite source not found: {FROZEN_DB}")
    db = sqlite3.connect(FROZEN_DB.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        rows = db.execute(
            """
            SELECT q.id, q.exam_number, q.source_pdf_page, paper.relative_path,
                   paper.sha256, paper.byte_size,
                   answer.source_pdf_page, answer_file.relative_path,
                   answer_file.sha256, answer_file.byte_size,
                   transcript.source_pdf_page, transcript_file.relative_path,
                   transcript_file.sha256, transcript_file.byte_size,
                   seg.start_ms, seg.end_ms, seg.status, seg.version,
                   audio_file.relative_path, audio_file.sha256, audio_file.byte_size,
                   audio_asset.duration_seconds
            FROM questions q
            JOIN sections sec ON sec.id=q.section_id AND sec.exam_id='035-I-B'
            JOIN source_files paper ON paper.id=q.source_file_id
            JOIN answers answer ON answer.question_id=q.id
            JOIN source_files answer_file ON answer_file.id=answer.source_file_id
            JOIN transcripts transcript ON transcript.question_id=q.id
            JOIN source_files transcript_file ON transcript_file.id=transcript.source_file_id
            JOIN audio_segments seg ON seg.question_id=q.id
            JOIN audio_assets audio_asset ON audio_asset.id=seg.audio_asset_id
                 AND audio_asset.section_id=q.section_id
            JOIN source_files audio_file ON audio_file.id=audio_asset.source_file_id
            WHERE q.exam_number BETWEEN 25 AND 30
            ORDER BY q.exam_number
            """
        ).fetchall()
        keys = (
            "id", "number", "paper_page", "paper_path", "paper_sha", "paper_bytes",
            "answer_page", "answer_path", "answer_sha", "answer_bytes",
            "transcript_page", "transcript_path", "transcript_sha", "transcript_bytes",
            "start_ms", "end_ms", "segment_status", "segment_version",
            "audio_path", "audio_sha", "audio_bytes", "audio_duration_seconds",
        )
        return {row[1]: dict(zip(keys, row)) for row in rows}
    finally:
        db.close()


def hash_file(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


class FrozenSources(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = source_rows()

    def test_35th_three_pair_sources_are_real_and_sha_verified(self):
        rows = self.rows
        self.assertEqual(set(rows), set(range(25, 31)))
        used_files = {}
        intervals = []
        for a, b in PAIRS:
            first, second = rows[a], rows[b]
            for row in (first, second):
                self.assertEqual(row["id"], f"035-I-L-{row['number']:03d}")
                self.assertLess(row["start_ms"], row["end_ms"])
                self.assertLess(row["end_ms"], row["audio_duration_seconds"] * 1000)
                self.assertIn(row["segment_status"], ("candidate", "verified"))
                for prefix, suffix in (
                    ("paper", ".pdf"), ("answer", ".pdf"),
                    ("transcript", ".pdf"), ("audio", ".mp3"),
                ):
                    relative = Path(row[prefix + "_path"])
                    path = (ROOT / relative).resolve()
                    self.assertTrue(path.is_relative_to(ROOT.resolve()), relative)
                    self.assertTrue(path.is_file(), path)
                    self.assertEqual(path.suffix.lower(), suffix)
                    self.assertEqual(path.stat().st_size, row[prefix + "_bytes"])
                    prior = used_files.setdefault(path, row[prefix + "_sha"])
                    self.assertEqual(prior, row[prefix + "_sha"],
                                     "Same immutable original cannot have different DB digest")
            for field in ("paper_path", "paper_page", "answer_path", "answer_page",
                          "transcript_path", "transcript_page", "audio_path", "audio_sha",
                          "start_ms", "end_ms"):
                self.assertEqual(first[field], second[field], f"{a}/{b}: {field}")
            intervals.append((first["start_ms"], first["end_ms"]))
        self.assertEqual(len(set(intervals)), 3,
                         "Each of the three shared dialogues must have a distinct interval")
        for path, expected in used_files.items():
            with self.subTest(source=path.name):
                with path.open("rb") as stream:
                    magic = stream.read(5)
                self.assertTrue(magic.startswith(b"%PDF") if path.suffix == ".pdf"
                                else magic.startswith(b"ID3") or magic[:1] == b"\xff",
                                f"Unexpected media signature: {path}")
                self.assertEqual(hash_file(path), expected, f"Original SHA mismatch: {path}")


NODE = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const fixtures = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const inline = html.match(/<script>([\s\S]*?)<\/script>/);
assert(inline, 'Missing real reviewer inline JS');
const tail = '\n      renderAiAudit=()=>{}; renderHistory=()=>{}; '+
  'renderIndependentComparison=()=>{}; renderReviewDecisionCue=()=>{}; '+
  'renderAiDecisionPanel=()=>{}; refreshDirty=()=>{}; updateNavigation=()=>{}; '+
  'filterItems=()=>{}; renderDetail=()=>renderReferences(); '+
  // This fixture proves original-source navigation and zero foreground GET
  // for warm cache. Proactive background reads have separate bounded VM tests.
  'prefetchAhead=()=>{}; '+
  'request=async url=>globalThis.__getDetail(url); '+
  'globalThis.testApi={state,selectQuestion,retainedSourceTab,jumpToOriginal,'+
  'renderPdf,renderReferences,renderAudioSegment,audioSharedNumbers,'+
  'installHandlers,sourceWithPage,sourceWithSharedAudio,setReferenceOpen};\n    })();';
const code = inline[1].replace(/\n\s*init\(\);\s*\n\s*\}\)\(\);\s*$/, tail);
assert.notEqual(code, inline[1], 'Unable to safely bypass network init');
const events = [], network=[];
class FakeElement {
  constructor(id='',tag='div') {
    this.id=id;this.tagName=tag.toUpperCase();this.attrs={};this.children=[];
    this.hidden=false;this.disabled=false;this.value='';this._text='';this.listeners={};
    this.style={};this.dataset={};this.open=false;this.className='';this.playCount=0;
    const classes=new Set();
    this.classList={contains:x=>classes.has(x),add:x=>classes.add(x),
      remove:x=>classes.delete(x),toggle:(x,v)=>{
        const on=v===undefined?!classes.has(x):!!v;
        if(on)classes.add(x);else classes.delete(x);return on;}};
  }
  get textContent(){return this._text+this.children.map(x=>x.textContent||'').join('');}
  set textContent(v){this._text=String(v);this.children=[];}
  get src(){return this.attrs.src||'';}
  set src(v){this.attrs.src=String(v);}
  getAttribute(k){return this.attrs[k]??null;}
  setAttribute(k,v){this.attrs[k]=String(v);}
  removeAttribute(k){delete this.attrs[k];}
  append(...values){this.children.push(...values);}
  appendChild(value){this.children.push(value);return value;}
  replaceChildren(...values){this._text='';this.children=[...values];}
  addEventListener(k,cb){(this.listeners[k]??=[]).push(cb);}
  async click(){assert(!this.disabled,'Disabled control clicked: '+this.id);
    for(const cb of this.listeners.click||[])await cb({target:this,currentTarget:this,
      preventDefault(){},stopPropagation(){}});}
  focus(){events.push(['focus',this.id]);document.activeElement=this;}
  scrollIntoView(){events.push(['scroll',this.id]);}
  pause(){events.push(['pause',this.id]);}
  load(){events.push(['load',this.id]);}
  getClientRects(){return [{width:40}];}
  querySelector(selector){return selector==='summary'?get(this.id+'-summary'):null;}
  querySelectorAll(){return [];}
}
const nodes=new Map();
function get(id) {if(!nodes.has(id)) nodes.set(id,new FakeElement(id)); return nodes.get(id);}
const tabs=['question','answer','transcript'].map(kind=>{
  const node=new FakeElement('source-tab-'+kind,'button');
  node.dataset.source=kind;nodes.set(node.id,node);return node;
});
const document={activeElement:null,title:'',getElementById:get,
  createElement:tag=>new FakeElement('',tag),
  querySelectorAll:s=>s==='.source-tab'?tabs:[],
  querySelector:s=>{
    const match=/^\.source-tab\[data-source="(question|answer|transcript)"\]$/.exec(s);
    return match?tabs.find(x=>x.dataset.source===match[1]):null;},
  addEventListener(){},body:{classList:new FakeElement().classList}};
const origin='http://127.0.0.1:8765';
let viewport=390;
const window={location:{href:origin+'/?exam_id=035-I-B',origin,search:'?exam_id=035-I-B',
    hostname:'127.0.0.1',protocol:'http:'},
  matchMedia:q=>({matches:q.includes('1250px')?viewport<=1250:viewport<=760}),
  confirm:()=>true,addEventListener(){}};
const ctx={window,location:window.location,document,URL,URLSearchParams,AbortController,
  console,setTimeout:()=>1,clearTimeout:()=>{},
  fetch:async()=>{throw Error('Unexpected native network request');}};
vm.runInNewContext(code,ctx);
const api=ctx.testApi;
assert(api && typeof api.selectQuestion==='function');
const state=api.state;
function makeQ(row, exam='035-I-B', section='listening') {
  const number=row.number,id=exam.slice(0,3)+'-I-'+(section==='listening'?'L':'R')+'-'+
    String(number).padStart(3,'0');
  const examParam=exam==='035-I-B'?'':'?exam_id='+exam;
  const route=kind=>'/media/'+id+'/'+kind+examParam;
  return {id,exam_id:exam,number,section,
    review_status:'needs_manual_review',version:7,
    source_pdf_page:row.paper_page,source_pdf_url:route('paper'),
    answer:{source_pdf_page:row.answer_page,choice_number:1},
    answer_pdf_url:route('answer'),
    transcript:section==='listening'?{source_pdf_page:row.transcript_page,
      text:'frozen audio transcript',warnings:[]}:null,
    transcript_pdf_url:section==='listening'?route('transcript'):null,
    audio_url:section==='listening'?route('audio'):null,
    audio_segment:section==='listening'?{start_ms:row.start_ms,end_ms:row.end_ms,
      status:row.segment_status,version:row.segment_version,
      source_duration_ms:Math.round(row.audio_duration_seconds*1000),
      shared_questions:exam==='035-I-B'?[Math.floor((number-25)/2)*2+25,
        Math.floor((number-25)/2)*2+26]:undefined}:null,
    choices:[1,2,3,4].map(number=>({number,text:'choice'})),images:[],
    preview_flags:[],history:[]};
}
const qs={};for(const key of Object.keys(fixtures))qs[key]=makeQ(fixtures[key]);
function reset(q){
  state.items=Object.values(qs).map(q=>({id:q.id,number:q.number,status:q.review_status,
    section:q.section}));state.examId=q.exam_id;state.selectedId=q.id;state.detail=q;
  state.detailsCache={};state.detailsCache[q.id]=q;state.pdfTab='question';
  state.saving=false;state.loading=false;state.requestId=0;state.examSwitchConfirmed=false;
  state.capabilities={reviewWrite:true,audioSegmentWrite:q.exam_id==='035-I-B',
    clipExport:q.exam_id==='035-I-B'};
  state.audio={available:false,saving:false,error:false,durationMs:null,
    preview:null,heardSignature:null};
  state.pendingReviews=new Map();state.failedReviews=new Map();
  state.committedReviews=new Map();state.bundleProtectedIds=new Set();
  state.baseline='FROZEN BASELINE';state.csrfToken='UNCHANGED TOKEN';
  get('stemInput').value='unsaved draft';get('reviewNote').value='unsaved note';
  get('audioPlayer').removeAttribute('src');document.body.classList.remove('reference-open');
  api.renderReferences();
}
function snapshot(q){return {id:q.id,version:q.version,status:q.review_status,
    baseline:state.baseline,csrf:state.csrfToken,
    stem:get('stemInput').value,note:get('reviewNote').value};}
function preserved(q,s){
  assert.equal(q.version,s.version);assert.equal(q.review_status,s.status);
  assert.equal(state.selectedId,s.id);assert.equal(state.baseline,s.baseline);
  assert.equal(state.csrfToken,s.csrf);
  assert.equal(get('stemInput').value,s.stem);
  assert.equal(get('reviewNote').value,s.note);
}
let requests=[];
// The actual wrapper is overridden only for selecting an uncached question:
// assert that exactly one read-only GET happens, never a review/audio POST.
ctx.__getDetail=async url=>{requests.push(url);assert.match(url,/^\/api\/questions\/035-I-L-026\?fast=1$/);
  return qs['26'];};

for(const [a,b] of [[25,26],[27,28],[29,30]]){
  const first=qs[a],next=qs[b];reset(first);
  assert.deepEqual(Array.from(api.audioSharedNumbers(first)),[a,b]);
  assert.equal(api.sourceWithSharedAudio(first.audio_url).href,
    api.sourceWithSharedAudio(next.audio_url).href);
  for(const tab of ['transcript','answer']){
    reset(first);state.pdfTab=tab;api.renderPdf();
    const prevUrl=get('sourceFrame').src, oldRequests=requests.length;
    state.detailsCache[next.id]=next;
    await api.selectQuestion(next.id);
    assert.equal(state.detail,next);assert.equal(state.pdfTab,tab);
    assert.equal(get('sourceFrame').src,prevUrl,
      'Same original and page must not trigger a new PDF iframe URL');
    assert.equal(requests.length,oldRequests,'Cached transition must not call API');
  }
}

// Exercise the uncached async detail-fetch path rather than just the helper.
reset(qs[25]);state.pdfTab='transcript';api.renderPdf();
const oldPage=get('sourceFrame').src;delete state.detailsCache[qs[26].id];
requests=[];await api.selectQuestion(qs[26].id);
assert.equal(state.pdfTab,'transcript');
assert.equal(get('sourceFrame').src,oldPage);
assert.equal(requests.length,1,'Uncached navigation should issue one GET');

reset(qs[25]);state.pdfTab='answer';api.renderPdf();
delete state.detailsCache[qs[26].id];requests=[];
await api.selectQuestion(qs[26].id);
assert.equal(state.pdfTab,'answer');assert.equal(requests.length,1);
assert.match(get('sourceFrame').src,/\/answer#page=1$/);

// Scopes: different exam/section or unavailable transcript must reset safely.
const l36=makeQ({...fixtures[25],number:25},'036-I-B','listening');
const r36=makeQ({...fixtures[25],number:31},'036-I-B','reading');
assert.equal(api.retainedSourceTab(l36,r36,'answer'),'question');
assert.equal(api.retainedSourceTab(l36,r36,'transcript'),'question');
assert.equal(api.retainedSourceTab(qs[25],l36,'answer'),'question');
assert.equal(api.retainedSourceTab(qs[25],{...qs[26],transcript_pdf_url:null},'transcript'),
  'question');
assert.equal(api.retainedSourceTab(qs[25],qs[26],'answer'),'answer');
assert.equal(api.retainedSourceTab(qs[25],qs[26],'transcript'),'transcript');
assert.notEqual(api.sourceWithPage(l36.source_pdf_url,9).split('#')[0],
  api.sourceWithPage(r36.source_pdf_url,9).split('#')[0]);
assert.match(api.sourceWithPage(r36.source_pdf_url,9),/036-I-R-031\/paper\?exam_id=036-I-B#page=9/);
for(const bad of ['https://other.example/x','//other.example/file',
  'javascript:alert(1)','http://127.0.0.1:8765.evil.test/file']){
  assert.equal(api.sourceWithPage(bad,9),null);
  assert.equal(api.sourceWithSharedAudio(bad),null);
}

// Actual direct button listeners must open the right mobile source panel,
// focus/scroll the evidence and preserve unsaved text and review version.
api.installHandlers();
for(const px of [320,390,1280]){
  viewport=px;reset(qs[25]);const initial=snapshot(qs[25]);
  for(const [id,tab,target] of [
    ['jumpAnswer','answer','source-tab-answer'],
    ['jumpTranscript','transcript','source-tab-transcript'],
    ['jumpAudio',null,'audioSection']]){
    events.length=0;const callsBefore=requests.length;
    assert.equal(get(id).disabled,false);
    await get(id).click();
    if(tab) assert.equal(state.pdfTab,tab);
    assert.ok(events.some(e=>e[0]==='scroll'&&e[1]===target),
      id+' must scroll correct target at '+px);
    assert.ok(events.some(e=>e[0]==='focus'&&e[1]===target),
      id+' must focus correct target at '+px);
    assert.equal(document.body.classList.contains('reference-open'),px<=1250);
    preserved(qs[25],initial);
    assert.equal(requests.length,callsBefore,'Source jump must not call API');
  }
}
// If an audio shortcut is not a naturally focusable element, tabindex=-1
// makes the programmatic focus meaningful in a real keyboard browser.
assert.match(html,/<[^>]+id="audioSection"[^>]*tabindex="-1"/,
  'Audio jump destination must be genuinely programmatically focusable');

// 36th full-track original is visible, while no speculative shared pairs or
// editable boundary controls are offered; a stale 35 segment must not bypass.
viewport=390;reset(l36);l36.audio_segment={...qs[25].audio_segment,
  shared_questions:[25,26]};api.renderReferences();
assert.equal(api.audioSharedNumbers(l36).length,0);
assert.equal(get('audioSection').hidden,false);
assert.equal(get('audioSegmentPanel').hidden,true);
assert.equal(state.audio.available,false);
assert.equal(get('jumpAudio').hidden,false);
assert.equal(get('jumpAudio').disabled,false);
assert.equal(get('audioPlayer').hidden,false);
assert.match(get('audioPlayer').src,/\/media\/036-I-L-001\/audio\?exam_id=036-I-B$/);
assert.equal(get('audioSaveCandidate').disabled,true);
assert.equal(get('audioVerify').disabled,true);
assert.equal(get('audioExport').disabled,true);
assert.equal(get('audioStart').disabled,true);
assert.equal(get('audioEnd').disabled,true);
assert.equal(get('audioSharedNote').hidden,true);
const before36=snapshot(l36),req36=requests.length;
events.length=0;await get('jumpAudio').click();
assert.ok(events.some(e=>e[0]==='scroll'&&e[1]==='audioSection'));
assert.ok(events.some(e=>e[0]==='focus'&&e[1]==='audioSection'));
preserved(l36,before36);assert.equal(requests.length,req36);
console.log('PASS original 35 pair workflow / cached+fetched / 320+390+desktop / 36 read-only audio');
"""


@unittest.skipUnless(shutil.which("node"), "Node.js required for actual inline-JS tests")
class OriginalWorkflowNodeTests(unittest.TestCase):
    def test_real_review_js_source_navigation_and_read_only_audio(self):
        rows = source_rows()
        with tempfile.TemporaryDirectory(prefix="topik-original-workflow-") as temp:
            temp = Path(temp)
            script = temp / "browser_fixture.cjs"
            fixture = temp / "read_only_35.json"
            script.write_text(
                "(async()=>{" + NODE +
                "})().catch(error=>{console.error(error);process.exitCode=1});",
                encoding="utf-8",
            )
            fixture.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [shutil.which("node"), str(script), str(HTML), str(fixture)],
                capture_output=True, text=True, encoding="utf-8",
                timeout=20, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
