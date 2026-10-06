# 제35회 TOPIK I AI 독립 감사 — 두 번째 검증 패스 (2026-10-05)

이 문서는 첫 독립성·복원력 검증 다음 단계로, 실제 사용자 워크플로에서 여러 감사 run을
연속 실행하고 중단·재개·실패·재시도·대상 재감사·과거 run 재열람·UI 영속성을 검증한
결과를 기록한다. 이 검증은 감사 인프라와 검수 UI의 동작을 확인한 것이며, AI 결과가
사람 검수 상태를 승인하거나 실제 시험 데이터의 correctness를 확정한다는 뜻은 아니다.

## 검증 목표

1. 소수 대표 대상(듣기 1번, 읽기 31번)을 고정해 여러 run을 연속 실행한다.
2. checkpoint 중단 뒤 프로세스를 끝내고 새 프로세스에서 안전하게 resume한다.
3. timeout·invalid 응답 뒤 같은 pass를 재시도해 과거 attempt가 덮어써지지 않는지 확인한다.
4. 특정 문항만 후속 run에서 재감사하고, 관련 없는 run이 다른 문항 이력을 오염시키지 않는지 확인한다.
5. 과거 완료 run의 verdict/finding/attempt/evidence를 새 run 뒤에도 다시 조회할 수 있는지 확인한다.
6. 최신 run이 미완료일 때 과거 finding을 성급히 "재현되지 않음" 또는 "해결"로 표시하지 않는지 확인한다.
7. 실제 browser에서 desktop과 약 390px 화면의 목록·필터·상세·긴 evidence·키보드/ARIA 흐름을 확인한다.
8. 발견한 결함은 상태를 숨기는 우회가 아니라 저장·조회·UI 경계의 원인을 수정한 뒤 같은 흐름을 다시 실행한다.

검증에는 운영 DB를 직접 수정하지 않고 별도 사본
`topik-past-papers/derived/ai-audit-validation-2026-10-05/second-pass-workflow.sqlite`와
UI 전용 사본 `topik-past-papers/derived/review-ui-validation-2026-10-05.sqlite`를 사용했다.
두 파일과 생성된 JSON/screenshot은 Git에서 제외되는 로컬 검증 자료다.

## 실제 다중-run 워크플로

### Run 1 — 중단·재개와 실패·재시도

- run: `audit35-097593461fdd45d0`
- label: `second-validation-r1`
- 대상: `035-I-L-001`, `035-I-R-031`
- pass 수: 2 / 완료 2

pass 1 `audit35-097593461fdd45d0-p1-347ed4a3`은 듣기 1번을 checkpoint sequence 1로
저장한 뒤 프로세스를 종료했다. checkpoint SHA-256은
`e304ea591aa2aedab57fd708fa0eb57acfe7bc863d08508bd95284b613e5cc70`이다. 새 프로세스의
`--resume` export는 이미 완료한 듣기 1번을 제외하고 읽기 31번만 반환했다. 남은 문항만
담은 final을 적재하자 importer가 immutable checkpoint와 병합해 pass를 정상 완료했다.

pass 2 `audit35-097593461fdd45d0-p2-bea84fbb`은 다음 attempt 순서를 실제로 남겼다.

1. `timed_out` — `deadline_exceeded`, `workflow_timeout`
2. `invalid` — malformed JSON 원시 응답 보존
3. `succeeded` — 수정한 구조화 응답

Run 1 최종 attempt 집계는 `succeeded=2`, `timed_out=1`, `invalid=1`, `failed=0`이다.
읽기 31번은 한 pass가 low `text_fidelity` finding, 다른 pass가 clear를 반환해
`disagreement=true`가 되었고, finding fingerprint는
`8ce64018fe949f10eb51017200bfae31dd441f204226b3b242a1f8fbc3b77f45`, consensus ratio는
`0.5`였다.

### Run 2 — 읽기 31번만 재감사

- run: `audit35-12679521ead246a6`
- label: `second-validation-r2-q31`
- 대상: `035-I-R-031`만
- 결과: clear, confidence 0.93

최신 run이 아직 pending일 때 읽기 31번의 과거 finding은
`latest_run_incomplete`로 표시되었다. Run 2가 완료된 뒤에는:

- run 수: 2
- 완료 audit 수: 3
- verdict 누적: clear 2 / finding 1 / uncertain 0
- disagreement가 있었던 run: 1
- 최신 run finding: 0
- 과거 finding: `not_reproduced_in_latest_run`

으로 조회됐다. `not_reproduced_in_latest_run`은 **해결 판정이 아니다**. 사람 검수 또는
source-backed correction 없이 AI가 과거 finding을 resolved로 승격하지 않는다.

### Run 3 — 듣기 1번만 재감사

- run: `audit35-42a79c4a66cb4350`
- 대상: `035-I-L-001`만
- perspective: `transcript_alignment`
- 결과: clear, confidence 0.96

듣기 1번 이력은 run 2의 Q31-only 재감사를 포함하지 않고, Run 1과 Run 3만 포함했다.
최종 누적은 run 2개 / audit 3회 / clear 3회다. 반대로 Run 3 이후 기본 Q31 summary/status는
관련 없는 최신 전역 run에 가려지지 않고 Q31을 실제 포함한 Run 2를 선택했다.

새 SQLite 연결로 다시 열어 확인한 최종 row 수는 run 3, pass 4, result 4, attempt 6,
checkpoint 1이다. Run 2/3 뒤에도 Run 1은 직접 재열람 가능했고, Q31의
finding 1 + clear 1 + disagreement 및 Run 1의 attempt 4개가 그대로 유지됐다.

## 두 번째 패스에서 발견·수정한 원인 결함

### 1. 특정 대상만 재감사하는 immutable run 범위가 없음

후속 재감사를 전체 70문항 run으로 만들면 실제 사용자 이력과 비용이 불필요하게 커지고,
어떤 대상이 그 run의 의도된 범위였는지 저장 계약으로 보장할 수 없었다.

`create_run(subject_ids=...)`와 반복 가능한 CLI `create-run --subject-id`를 추가했다. 빈 목록,
중복 ID, 존재하지 않는 ID는 거부하고, perspective 범위는 선택 대상과 교집합으로 제한한다.
선택 대상은 snapshot을 해시·저장하기 전에 고정되며 status에 `subject_ids`/`subject_count`가
노출된다.

### 2. 문항별 여러 run의 감사 이력을 한 번에 추적할 수 없음

최신 run summary만으로는 감사 횟수, 과거 finding, 불일치 run, 재감사 결과를 사용자가
연속해서 추적하기 어려웠다.

read-only `question_audit_history(db, qid)`와 CLI `history --subject-id <qid>`를 추가했다.
각 run의 subject-scoped execution/summary, 누적 verdict 수, disagreement run 수, 과거
finding의 재현 run을 반환한다. newest-first 조회 과정에서 최초 발견 run을 잘못 기록할 수
있던 의미 오류도 함께 수정해 `seen_run_ids`, `first_seen_run_id`, `latest_seen_run_id`,
`seen_run_count`를 실제 시간 순서에 맞게 계산한다.

최신 적용 run이 미완료이면 과거 finding은 `latest_run_incomplete`로 남고 최신 finding 수와
open finding 목록은 `null`이다. 완료되지 않은 감사를 근거로 "재현되지 않음"을 주장하지 않는다.

### 3. checkpoint 이후 남은 subject만 반환하는 final을 안전하게 완료할 수 없음

resume bundle은 남은 subject만 보여 주지만 final ingest는 원래 전체 pass 결과를 기대해 실제
중단→프로세스 재시작→resume 흐름이 맞지 않았다.

resume export에 checkpoint `sequence`와 `checkpoint_sha256`을 결합하고, resumed final은
`resume_sequence`와 `resume_checkpoint_sha256`을 반환하도록 했다. importer는 토큰이 최신
checkpoint와 정확히 일치할 때만 남은 결과를 immutable checkpoint와 병합한다. 오래된 토큰,
완료 subject 중복, checkpoint 변경은 fail closed한다.

### 4. 관련 없는 최신 targeted run이 다른 문항의 기본 조회를 가릴 수 있음

전역 최신 run만 선택하면 Q1-only 재감사 뒤 Q31의 최신 유효 summary가 사라질 수 있었다.
기본 문항 summary/status는 해당 문항을 실제 frozen input에 포함한 최신 적용 run을 선택하도록
수정했다. `summarize_all_questions()`도 문항마다 최신 완료 적용 run을 유지한다.

### 5. Windows CLI가 원시 Unicode 오류 응답 출력에서 실패할 수 있음

malformed 응답에 U+FEFF 같은 문자가 포함된 실제 retry 흐름에서 Windows cp949 stdout이
예외를 낼 수 있었다. JSON 출력 경로를 stream-safe하게 만들어 감사 evidence 자체 때문에
CLI 조회가 실패하지 않도록 수정했다.

### 6. 필터 목록과 상세 문항이 서로 다른 대상을 가리킬 수 있음

실제 Edge에서 `미해결 발견` 필터를 적용하자 목록에는 Q31만 남았지만 상세/AI 패널은 기존
Q1을 계속 보여 주는 상태 혼동을 재현했다.

필터가 현재 선택을 숨기고 이동이 안전하면 첫 visible 문항을 자동 선택한다. 저장하지 않은
편집이나 saving 상태가 이동을 막으면 자동으로 버리지 않고 상세가 필터 밖이라는 경고를
표시한다. 수정 후 unresolved filter는 Q31 목록·상세·`aria-current`를 동일 대상으로 맞춘다.

### 7. UI가 최신 run과 누적 다중-run 상태를 명확히 구분하지 못함

검수 상세에 backend `question_audit_history()`를 연결해 최신 run metrics와 전체 누적 history를
분리했다. 최신 영역은 verdict/attempt/pass/finding/risk를 `최근 실행`으로 명시하고, 이력 영역은
누적 verdict, run 수, 불일치 run 수, historical finding state를 표시한다. 각 과거 run은 native
`details/summary`로 다시 열어 전체 backend run JSON과 evidence를 읽을 수 있다.

실제 화면에서 `감사자 간 불일치`와 `수렴`이 동시에 보여 의미가 혼동될 수 있었으므로 run 내부
수렴은 `최근 신규 발견 수렴`/`최근 pass 추가 발견`으로, run 간 값은 `안정/변화` 용어로 구분했다.

## 실제 브라우저 UI 검증

Edge headless/CDP의 실제 렌더링을 사용했다. 별도 browser connector가 노출되지 않아 DOM 모의만
사용하지 않고 설치된 Edge를 직접 실행해 desktop과 모바일 폭을 확인했다.

Desktop 1440px:

- unresolved filter → Q31 하나만 표시되고 Q31 상세가 자동 선택됨
- active Q31 button `aria-current=true`
- 최신 상태: verdict 2, attempt 4, finding 1, disagreement, 2/3 pass 완료
- 누적 이력: verdict 4, run 2, disagreement run 1
- reload 후 검색 `31`로 완료 대상을 다시 열어도 두 run 이력 유지
- 긴 원시 응답/evidence는 내부 영역에 포함되고 page-level horizontal overflow 없음

약 390px:

- `innerWidth=390`, `document.scrollWidth=390`
- AI panel width 348px
- viewport 밖으로 나간 요소 0개
- 긴 `<pre>` overflow 요소 0개

키보드/접근성 확인:

- AI 필터가 자동 선택을 발생시켜도 필터 focus 유지
- AI 필터에서 Tab을 누르면 정렬 control로 이동
- 과거 run의 native `summary`는 keyboard focus 가능하고 Enter/Space로 열림
- active 문항은 `aria-current`, 문항 button의 `aria-label`은 최신 run verdict/attempt 수를 명시

로컬 screenshot은 `review-ui-validation-desktop-history2.png`와
`review-ui-validation-mobile-history2.png`로 남겼으며 Git에는 포함하지 않는다.

## 사람 검수 상태와 실제 데이터 finding

실제 E2E 시작 전후 human-state SHA-256은 모두
`63e934739d76b42539987130f35e8e0573d6ca0b6d95fb56eb8c156516429a3b`로 동일했다. AI 감사
흐름은 사람 review status/version/history 또는 검증된 음원 상태를 변경하지 않았다.

첫 검증 패스에서 Auditor B가 찾은 31~33 공통 지시문 띄어쓰기 fidelity finding도 이번 패스에서
자동 수정하지 않았다. 후속 Q31 targeted run이 이를 재현하지 않았다는 사실은 이력으로 남지만,
그 자체가 해결 근거가 되지 않는다. source-backed data correction은 별도 사람 검수 작업이다.

## 최종 검증

| 검증 | 결과 |
| --- | --- |
| backend AI audit 전용 | **14/14 OK** |
| review UI 전용 | **27/27 OK** |
| backend + UI focused | **41/41 OK** |
| 전체 프로젝트 | **83/83 OK** |
| Python compile | OK |
| `git diff --check` | OK (기존 Windows LF→CRLF warning만 존재) |
| 실제 DB 재오픈/영속성 | run 3 / pass 4 / result 4 / attempt 6 / checkpoint 1 유지 |
| 사람 검수 상태 | 전후 SHA-256 동일 |

두 번째 검증 패스에서 확인된 사용자 워크플로 결함은 모두 원인 수준에서 수정하고 동일 유형을
다시 실행했다. 변경은 아직 commit/push하지 않았으며, 원본 PDF/audio와 validation DB/JSON/
screenshot은 계속 로컬 Git-ignored 자료로 유지한다.
