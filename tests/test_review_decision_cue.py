"""Real review UI logic must never assert that human approval reviewed AI findings."""

from pathlib import Path
import shutil
import subprocess
import unittest


HTML = Path(__file__).resolve().parents[1] / "src" / "review_ui.html"

NODE = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
function source(name, next) {
  const a = html.indexOf('      function '+name+'(');
  const b = html.indexOf('      function '+next+'(', a+1);
  assert.ok(a >= 0 && b > a, 'missing function '+name);
  return html.slice(a,b);
}
assert.match(html, /id="reviewDecisionCue"/,'selected question needs a visible decision cue');
const elements = Object.fromEntries(['reviewDecisionCue','reviewDecisionHeading',
  'reviewDecisionText','reviewDecisionEvidence'].map(id=>[id,{
  id,hidden:true,textContent:'',dataset:{},className:'',
  classList:{toggle(){}},
}]));
const details={
  independentComparisonSection:{open:false,querySelector:()=>({focus(){}}),scrollIntoView(){}},
  aiAuditSection:{open:false,querySelector:()=>({focus(){}}),scrollIntoView(){}},
};
let state={items:[],detail:null,independentAudits:{questions:{}}};
const context={state,$:id=>elements[id]||details[id],
  aiAudit:i=>i?.ai_audit||null,aiNumber:x=>Number(x)||0, Date, console};
vm.runInNewContext(source('reviewEvidence','aiConvergenceText')+
  source('renderReviewDecisionCue','renderDecisionEvidence'), context);
const decision=context.reviewIndicator;
const render=context.renderReviewDecisionCue;
const approved={id:'035-I-L-001',number:1,status:'verified',review_status:'verified',
  last_human_review:{reviewed_at:'2026-10-08T10:00:00Z',status:'verified',is_current:true,approved:true},version:3};
function expectCue(visible,term){
  render();
  assert.equal(elements.reviewDecisionCue.hidden,!visible);
  if(term) assert.match(elements.reviewDecisionText.textContent,term);
}
state.items=[approved];state.detail={...approved};
expectCue(false);
state.independentAudits.questions['1']={chatgpt:{verdict:'finding',created_at:'not-verified',
  timestamp_verified:false}};
const uncertain=decision(approved);
assert.equal(uncertain.kind,'verified','Unknown chronology cannot change human approval');
assert.doesNotMatch(uncertain.reason,/사람이 확인했습니다/);
assert.match(uncertain.reason,/확인|대조|미확정/);
expectCue(true,/확인|대조|미확정/);
assert.equal(elements.reviewDecisionEvidence.dataset.target,'independentComparisonSection');
assert.doesNotMatch(elements.reviewDecisionText.textContent,/대본/,'Reading items should not require listening transcripts');

state.independentAudits.questions['1']={gemini:{verdict:'finding',
  created_at:'2026-10-08T11:00:00Z',snapshot_created_at:'2026-10-08T10:30:00Z',
  timestamp_verified:true,snapshot_timestamp_verified:true}};
assert.equal(decision(approved).kind,'problem','Verified postapproval finding still requires attention');
expectCue(true,/승인 후|승인 이후/);

state.independentAudits.questions['1']={};
state.items=[{...approved,ai_audit:{unresolved_findings:2,total:3}}];
expectCue(true,/AI|감사/);
assert.equal(elements.reviewDecisionEvidence.dataset.target,'aiAuditSection');

state.items=[approved];state.detail={...approved};
expectCue(false);
state.detail={...approved,review_status:'needs_manual_review',status:'needs_manual_review'};
state.independentAudits.questions['1']={chatgpt:{verdict:'finding'}};
expectCue(false);
state.detail={...approved,section:'listening'};
state.independentAudits.questions['1']={chatgpt:{verdict:'finding'}};
expectCue(true,/대본/);
console.log('Human review status, unverified AI findings, verified postapproval and visible evidence cue PASS');
"""


@unittest.skipUnless(shutil.which("node"), "Node is required")
class ReviewDecisionCueTests(unittest.TestCase):
    def test_real_review_indicator_and_visible_evidence_cue(self):
        process = subprocess.run([shutil.which("node"), "-e", NODE, str(HTML)],
                                 capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertIn("PASS", process.stdout)


if __name__ == "__main__":
    unittest.main()
