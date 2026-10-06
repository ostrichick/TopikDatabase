# 제35회 TOPIK I AI 독립 감사 — 첫 검증 패스 (2026-10-05)

이 문서는 다중 독립 AI 감사 시스템을 구현한 직후 실시한 첫 독립성·복원력 검증의
재현 가능한 증거를 기록한다. 이 검증은 **감사 인프라의 동작**을 검증하며, 35회 전체
문항이 사람 검수를 통과했다거나 원본의 재배포 권리가 확인되었다는 뜻이 아니다.

## 검증 목표

1. 같은 frozen source를 여러 감사 주체가 받고도 이전 감사 결론을 보지 않는지 확인한다.
2. 같은 결함은 하나의 fingerprint로 중복 제거하면서 재현 횟수는 잃지 않는지 확인한다.
3. 서로 다른 verdict는 disagreement로 표시되는지 확인한다.
4. 감사 횟수, finding, 구조화 evidence, pass/run 상태가 저장과 조회 사이에서 누락되지
   않는지 확인한다.
5. failed/timed_out/invalid 응답이 다른 pass와 consensus를 무너뜨리지 않고, 실패 원인이
   append-only로 남으며 같은 pass를 안전하게 재시도할 수 있는지 확인한다.
6. 첫 검증에서 발견한 결함은 우회하지 않고 계약·저장 모델·조회 경계의 원인을 수정한 뒤
   같은 시나리오를 다시 실행한다.

## 독립 입력 검증

운영 `topik-past-papers/derived/035-I-B.sqlite`는 직접 수정하지 않고 사본을 만들었다.
검증 run `audit35-613d392de4ac4002`에서 독립 pass 3개를 만들었으며 세 pass 모두:

- frozen source snapshot SHA-256:
  `e52c1efd8b7a20ab732e3379a61090d0c18cc48d5ce4faeee1c5061a51e37f78`
- subject 수: 70
- `blind=true`
- 직전 AI result, `review_records`, `review_status`를 입력에 포함하지 않음
- `source` payload의 canonical SHA-256이 위 snapshot SHA-256과 동일

이었다. pass별 `input_sha256`은 pass id/auditor provenance가 다르므로 서로 다르지만,
실제로 감사하는 frozen source와 subject 내용은 동일하다.

## 실제 독립 감사자 검증

Auditor A는 별도 대화에서 `pass-1.json`만 받고 다른 pass/result/audit DB/사람 검수 이력을
읽지 않도록 제한했다. 대상은 `035-I-R-031` 한 문항으로 고정했다.

- 확인 원본: `35th-TOPIK-I-Papers.pdf` 11쪽
- 확인 답안: `35th-TOPIK-I-Answer-Sheet.pdf` 2쪽
- verdict: `clear`
- confidence: `0.99`
- 근거: 문제 본문/보기/2점 배점이 문제지와 일치하고, 읽기 답안지의 section-local 1번이
  정답 3/2점으로 global 31번의 `answer_key_number=1`, `choice_number=3`과 일치
- 출력: ignored local evidence `auditor-a.json`

Auditor B도 별도 대화에서 `pass-2.json`만 받고 Auditor A의 결론/파일을 보지 않는 동일
제약으로 실행했다.

- 확인 원본: `35th-TOPIK-I-Papers.pdf` 11쪽
- 확인 답안: `35th-TOPIK-I-Answer-Sheet.pdf` 2쪽
- verdict: `finding`
- confidence: `0.99`
- 본문/보기/2점 배점/정답 ③은 원본과 일치
- low-severity `text_fidelity` finding:
  - frozen: `무엇에 대한 이야기입니까?<보기>와 같이 알맞은 것을 고르 십시오.`
  - PDF: `무엇에 대한 이야기입니까? <보기>와 같이 알맞은 것을 고르십시오.`
- 출력: ignored local evidence `auditor-b.json`

따라서 같은 frozen source와 같은 문항을 본 두 독립 감사자가 각각 `clear`와 `finding`을
냈다. 두 감사자는 서로의 결과를 입력으로 받지 않았으므로 실제 독립 감사에서도 이전
결론에 맞춰지는 anchoring이 강제되지 않았음을 확인했다. 두 checkpoint는 원본 그대로
validation DB에 적재되어 pass 1/2가 각각 `checkpointed`, 완료 1/대기 0으로 조회된다.

Auditor B가 찾은 띄어쓰기 차이는 AI 감사 인프라 오류가 아니라 기존 derived text의 실제
충실도 finding이다. `src/extraction_rules.py`의 기존 자동 보정은 의도적으로 `.`/`,` 뒤의
좁은 공백 오류만 수정하고, 문서화된 정책상 PDF의 낱말 내부 띄어쓰기 같은 모호한 변경은
사람 대조 대상으로 남긴다. 따라서 첫 감사 검증 과정에서 AI가 이 내용을 자동 수정하거나
사람 `verified` 상태를 바꾸지 않았다. 이 finding은 사람 검수/별도 source-backed data
correction 단계에서 처리해야 하며, **감사 시스템의 실패로 숨기지 않고 실제 발견으로
보존**한다.

## 첫 실행에서 발견한 결함과 원인 수정

### 1. 실패·타임아웃·형식 오류 이력이 사라짐

**수정 전 재현:** 잘못된 `input_sha256` 응답은 거부됐지만 pass 상태는 단순 `pending`으로
남았고, 실패 attempt row는 0개였다. 즉 실패 사실 자체를 나중에 감사할 수 없었다.

**원인:** pass/result/checkpoint 모델만 있고 provider 실행 시도 자체를 표현하는 데이터
모델이 없었다.

**수정:** append-only `ai_audit_attempts`를 추가했다. 각 attempt에 단조 증가 번호와
`succeeded|failed|timed_out|invalid`, 오류 code/message, evidence, 원시 응답/hash,
연결 result를 기록한다. 실패는 pass를 종료하지 않으며 다음 attempt로 재시도한다.

### 2. finding evidence는 저장되지만 조회에서 유실됨

**수정 전 재현:** `ai_audit_results.raw_json`에는 evidence가 있었지만
`summarize_question()`의 finding에는 `fingerprint/category/severity/summary/detail`만 남고
`identity/evidence`가 사라졌다.

**원인:** read projection이 저장 계약보다 좁았다.

**수정:** 구조화 `identity`와 `evidence`를 read summary와 검수 UI까지 그대로 전달한다.

### 3. 문서화된 CLI가 복원력 경로를 우회함

**원인:** Python의 새 `ingest_response()`는 attempt를 기록하지만 CLI `import-result`는
구형 `ingest_result()`/`import_result()` 경로를 사용했다.

**수정:** CLI `import-result`/`import-pass`도 attempt-aware ingest를 사용한다. malformed
응답은 `invalid` attempt로 기록하고 exit code 2를 반환하며, 수정한 응답은 같은 pass의
다음 attempt로 성공할 수 있다. provider 실패/시간 초과는 `record-outcome`, 이력 조회는
`attempts` 명령으로 지원한다.

### 4. 생성 계약과 수신 계약의 `contract_version` 불일치

**실제 재현:** Auditor A가 export bundle의 결과 계약을 그대로 따라 `contract_version`을
포함한 checkpoint를 반환했지만 첫 ingest는 `Audit result has unknown top-level fields`로
거부됐다.

**원인:** producer는 `contract_version`을 요구했지만 schema-version checkpoint/final
receiver의 allowed field set에는 그 필드가 없었다.

**수정 및 동일 시나리오 재실행:** receiver가 `contract_version`을 허용·검증하도록 계약을
통일한 뒤 **Auditor A의 JSON을 수정하지 않고 그대로 다시 적재**했다. 결과는 checkpoint
sequence 1, 완료 subject 1, 미완료 69로 정상 저장됐다.

### 5. run 전체 실패 이력이 문항별 화면을 오염시킬 수 있음

**원인:** UI가 run 전체 status를 모든 문항에 동일하게 붙이면 듣기 전용
`transcript_alignment` pass의 실패가 읽기 문항에도 표시될 수 있었다.

**수정:** backend status 조회에 `subject_id` 범위를 추가해 frozen pass input에 실제로 해당
문항이 포함된 pass만 반환하도록 한다. UI는 이 backend 범위를 사용하고 JS에서 임의로
적용 가능성을 추론하지 않는다.

회귀 테스트에서는 듣기 전용 `transcript_alignment` pass에 `timed_out`을 기록하고 전체
문항용 pass에는 `failed`를 기록했다. 읽기 문항 조회에서는 듣기 timeout이 0건으로 제외되고
전체 문항 pass의 failed만 1건 보이며, 듣기 문항 조회에서는 두 pass가 모두 보인다.

## 복원력·중복·충돌 재실행

실제 35회 DB 사본에 같은 frozen source를 사용하는 5개 독립 pass를 만들고 다음 순서로
고의 failure를 섞었다.

| pass | attempt 시나리오 | 최종 상태 |
| --- | --- | --- |
| 1 | 동일 finding A | succeeded |
| 2 | 동일 finding A (다른 auditor/표현) | succeeded |
| 3 | clear | succeeded |
| 4 | timed_out → retry | succeeded |
| 5 | failed → invalid → retry | succeeded |

재실행 결과:

- completed passes: **5 / 5**
- attempt counts: `succeeded=5`, `failed=1`, `timed_out=1`, `invalid=1`
- pass 4 attempt 순서: `timed_out → succeeded`
- pass 5 attempt 순서: `failed → invalid → succeeded`
- 논리적으로 동일한 finding 두 건: **unique fingerprint 1개 / occurrence 2개**
- `finding`과 `clear`가 공존한 문항: **`disagreement=true`**
- 해당 문항 완료 감사 수: **5**
- finding structured evidence: 저장 후 summary에서 원형 그대로 조회됨
- 사람 `questions/transcripts/audio_segments/review_records` 상태는 변경하지 않음

성공 result와 `succeeded` attempt는 하나의 트랜잭션에 저장하며, attempt INSERT를 의도적으로
실패시키는 회귀 테스트에서 result도 함께 rollback되는 것을 확인한다.

## 최종 검증 표

| 검증 항목 | 결과 |
| --- | --- |
| 동일 frozen source 3개 blind pass | 동일 source/snapshot SHA, 70 subjects, prior AI/human conclusion 비노출 확인 |
| 실제 독립 감사 A | Q31 `clear`, confidence 0.99 |
| 실제 독립 감사 B | Q31 `finding`, confidence 0.99, low text_fidelity finding |
| 실제 독립성의 서로 다른 결론 | **확인됨** (`clear` vs `finding`) |
| 동일 finding 중복 집계 | synthetic 2회 재현 → fingerprint 1개 / occurrence 2개 |
| 서로 다른 final verdict 충돌 표시 | synthetic finding + clear → `disagreement=true` |
| failure isolation | failed/timed_out/invalid가 있어도 다른 pass와 consensus 계속 동작 |
| retry | `timed_out→succeeded`, `failed→invalid→succeeded` 순서 보존 |
| attempt 이력 | status/error/evidence/raw response/hash/attempt number 조회 가능 |
| finding evidence 왕복 | 저장 → backend summary → 검수 UI까지 구조화 evidence 보존 |
| 문항별 실행 범위 | frozen input에 해당 문항을 포함한 pass만 노출 |
| 사람 검수 상태 보호 | AI 테스트 전후 human status/version/history 불변 |
| backend AI audit 전용 테스트 | **9/9 OK** |
| review UI 전용 테스트 | **25/25 OK** |
| 전체 프로젝트 테스트 | **76/76 OK** |
| Python compile | OK |
| `git diff --check` | OK (Windows LF→CRLF warning만 존재) |

첫 패스에서 발견된 인프라 결함은 모두 원인 수준에서 수정한 뒤 같은 유형의 시나리오를
재실행했다. 실제 시험 내용의 finding은 AI가 자동 수정하지 않고 사람 최종 검수용 evidence로
남기는 기존 안전 경계를 유지한다.
