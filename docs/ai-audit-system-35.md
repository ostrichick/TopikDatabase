# 제35회 TOPIK I 다중 독립 AI 감사 시스템

이 문서는 제35회 TOPIK I 파일럿에 추가한 **AI 사전 감사 계층**의 운영 원칙과
데이터 경계를 설명한다. AI 감사는 사람 검수를 대체하지 않는다. AI가 여러 번 같은
결론을 내리더라도 `questions.review_status`, `transcripts.review_status`, 검증된 음원
구간, 사람 `review_records`를 자동 승인하거나 변경할 수 없다.

## 목표

- 같은 문항을 서로 독립적인 관점의 AI 감사자가 반복 검토한다.
- blind pass에는 이전 AI 감사 결과를 넣지 않아 anchoring을 줄인다.
- 각 pass의 입력 snapshot, auditor, 관점, 결과, finding을 append-only로 남긴다.
- 같은 결함이 여러 번 발견되면 하나의 fingerprint로 묶고 재현 횟수를 보존한다.
- 사람 검수 화면에는 AI 감사 횟수, 미해결 finding, 감사자 불일치, 위험도, 수렴 상태를
  **읽기 전용**으로 보여 준다.

## 권장 5+1 감사 구성

1. `transcription` — 원본 문제지와 DB의 문항/보기/배점/이미지 연결 대조
2. `answer_consistency` — 정답표 번호/정답/배점 대응 검증
3. `transcript_alignment` — 듣기 문항/대본/공통 대화 대응 검증
4. `adversarial` — 데이터가 잘못됐다고 가정하고 누락·밀림·오연결 탐색
5. `independent` — 앞선 결과를 보지 않는 완전 독립 재검수
6. deterministic consensus — 앞선 결과를 코드가 비교해 재현 횟수·불일치·위험도를
   계산한다. 이 단계는 새 AI 판정을 만들지 않고 사람 승인 상태도 변경하지 않는다.

blind pass는 이전 `ai_audit_*` 결과를 입력 bundle에 포함하지 않는다. 같은 run에서는
`auditor_id`를 재사용하지 않아 독립 pass 수를 부풀리지 않는다.

## 데이터 경계

AI 감사 이력은 `ai_audit_*` 테이블에만 저장한다.

- `ai_audit_source_snapshots` — 감사 대상 원본/DB 상태의 canonical snapshot
- `ai_audit_runs` — 한 감사 run
- `ai_audit_passes` — 독립 auditor/perspective별 pass
- `ai_audit_checkpoints` — 중단/재개를 위한 append-only 진행 기록
- `ai_audit_results` — 완료된 pass의 구조화 결과
- `ai_audit_attempts` — provider 실패·시간 초과·잘못된 응답·성공을 재시도 번호와 함께 보존
- `ai_audit_findings` — 중복 제거된 결함 identity/fingerprint
- `ai_audit_finding_occurrences` — 어떤 pass가 같은 결함을 재현했는지 기록

모든 AI 감사 테이블은 UPDATE/DELETE를 거부한다. 과거 결과를 고치는 대신 새 run/pass를
추가한다. 감사 runner는 모델 출력 JSON을 SQL로 실행하지 않고 계약 검증 후 파라미터화된
INSERT만 수행한다.

## 구조화 결과 계약

각 완료 결과는 감사 대상 subject별 verdict를 포함한다.

```json
{
  "completed_subject_ids": ["035-I-L-001"],
  "verdicts": [
    {
      "subject_id": "035-I-L-001",
      "verdict": "clear",
      "confidence": 0.93,
      "rationale": "원본과 저장 값이 일치함"
    }
  ],
  "findings": [],
  "notes": []
}
```

`verdict`는 `clear`, `finding`, `uncertain` 중 하나다. finding이 있는 subject는
`finding`이어야 하고, `clear` 또는 `uncertain` subject에 finding을 붙일 수 없다.

## 실제 실행 절차

`src/ai_audit_35.py`는 AI 제공자를 직접 호출하지 않는다. 대신 각 감사자에게 전달할
고정된 blind bundle을 만들고, 에이전트가 반환한 구조화 JSON을 검증해서 로컬 DB에
append-only로 적재한다. 따라서 ChatGPT 에이전트, 별도 API runner, 수동 전달 등 어떤
오케스트레이션을 쓰더라도 동일한 감사 계약을 사용할 수 있다.

먼저 기존 35-I DB에 감사 스키마를 idempotent하게 설치한다.

```powershell
py -3 src/ai_audit_35.py init
```

그다음 run 하나를 만든다.

```powershell
py -3 src/ai_audit_35.py create-run --label audit-1
```

특정 문항만 재감사할 때는 `--subject-id`를 반복해서 run의 immutable 대상 집합을 고정한다.

```powershell
py -3 src/ai_audit_35.py create-run --label targeted-reread --subject-id 035-I-L-001 --subject-id 035-I-R-031
```

빈 대상, 중복 ID, 존재하지 않는 ID는 거부한다. `transcript_alignment` 같은 perspective의
자체 범위가 있으면 실제 pass 대상은 run 대상 집합과 perspective 범위의 교집합이다.

출력된 `run_id`를 사용해서 권장 5개 blind pass를 각각 만든다. 아래의 auditor/model 값은
실제 실행 주체를 식별할 수 있는 값으로 바꾼다.

```powershell
py -3 src/ai_audit_35.py export-pass --run <run-id> --pass-number 1 --auditor transcription-a --perspective transcription --model gpt-5.6-sol --output topik-past-papers/derived/ai-audit/pass-1.json
py -3 src/ai_audit_35.py export-pass --run <run-id> --pass-number 2 --auditor answer-b --perspective answer_consistency --model gpt-5.6-sol --output topik-past-papers/derived/ai-audit/pass-2.json
py -3 src/ai_audit_35.py export-pass --run <run-id> --pass-number 3 --auditor transcript-c --perspective transcript_alignment --model gpt-5.6-sol --output topik-past-papers/derived/ai-audit/pass-3.json
py -3 src/ai_audit_35.py export-pass --run <run-id> --pass-number 4 --auditor adversarial-d --perspective adversarial --model gpt-5.6-sol --output topik-past-papers/derived/ai-audit/pass-4.json
py -3 src/ai_audit_35.py export-pass --run <run-id> --pass-number 5 --auditor independent-e --perspective independent --model gpt-5.6-sol --output topik-past-papers/derived/ai-audit/pass-5.json
```

각 pass는 이전 `ai_audit_*` 결과를 포함하지 않는다. `transcript_alignment` pass만 듣기
문항 30개를 대상으로 하고, 나머지 pass는 전체 70문항을 대상으로 한다.

긴 작업은 checkpoint JSON을 적재한 뒤 동일 pass를 `--resume`으로 다시 export할 수 있다.
resume bundle은 이미 완료한 subject를 제외하며 checkpoint sequence/SHA에 결합된다. resumed
final은 남은 subject만 반환하고 `resume_sequence`와 `resume_checkpoint_sha256`을 포함한다.
importer는 최신 immutable checkpoint와 토큰이 정확히 맞을 때만 두 결과를 병합하며, stale
resume이나 완료 subject 중복은 거부한다. 완료 JSON은 다음처럼 적재한다.

```powershell
py -3 src/ai_audit_35.py import-result --input <agent-result.json>
py -3 src/ai_audit_35.py status --run-id <run-id>
py -3 src/ai_audit_35.py consensus --run-id <run-id>
py -3 src/ai_audit_35.py summary --run <run-id>
py -3 src/ai_audit_35.py history --subject-id 035-I-R-031
```

`import-result`/`import-pass`는 성공 응답뿐 아니라 구조가 잘못된 응답도 attempt 이력에
남긴다. 잘못된 응답은 pass를 종료하지 않고 `invalid`로 기록되며, 수정한 응답을 같은
pass에 다시 넣으면 다음 `attempt_number`로 재시도된다. provider 호출 자체가 실패하거나
시간 초과한 경우에는 다음처럼 명시적으로 기록한다.

```powershell
py -3 src/ai_audit_35.py record-outcome --pass-id <pass-id> --status failed --error-code provider_error --error-message "provider returned 503" --evidence-json '{"http_status":503}'
py -3 src/ai_audit_35.py record-outcome --pass-id <pass-id> --status timed_out --error-code deadline_exceeded --error-message "provider timeout" --evidence-json '{"timeout_seconds":120}'
py -3 src/ai_audit_35.py attempts --pass-id <pass-id>
```

attempt는 `succeeded`, `failed`, `timed_out`, `invalid` 중 하나이며 모두 append-only다.
실패 attempt가 있어도 다른 pass의 결과/consensus는 계속 사용할 수 있다. 성공 result와
그에 대응하는 `succeeded` attempt는 하나의 트랜잭션으로 저장되어 둘 중 하나만 남는
부분 저장을 허용하지 않는다.

사람 검수 화면 `py -3 src/review_ui.py --port 0`은 감사 데이터가 있으면 문항별 감사
횟수, clear/finding/uncertain 수, 미해결 finding, 감사자 불일치, 위험도, 수렴 상태와
auditor/model/prompt provenance를 읽기 전용으로 표시한다. 완료 verdict 수와 실제 실행
attempt 수는 별도로 표시하며, 실패/시간 초과/잘못된 응답/재시도와 각 attempt의 오류·근거,
finding의 구조화 evidence도 확인할 수 있다. 문항 상세에서는 그 문항을 frozen input에
실제로 포함한 pass만 표시한다. 여러 targeted run이 있으면 최신 실행 metrics와 전체 누적
run 이력을 분리해 보여 주며, 과거 완료 run도 다시 열어 verdict/finding/attempt/evidence를
확인할 수 있다. 과거 finding이 최신 완료 run에서 나타나지 않은 경우에도
`not_reproduced_in_latest_run`일 뿐 자동으로 resolved로 취급하지 않는다. 최신 적용 run이
미완료이면 `latest_run_incomplete`로 표시해 비재현 결론을 유보한다. AI 감사 테이블이 없는
기존 DB에서는 기존 화면이 그대로 동작한다.

## 위험도와 사람 검수

위험도는 사람 검수 순서를 정하기 위한 deterministic 지표일 뿐 정답 판정이 아니다.
특히 다음은 높은 우선순위로 취급한다.

- 동일 finding이 여러 독립 pass에서 재현됨
- `high`/`critical` finding
- auditor 간 `clear`/`finding`/`uncertain` 판정 불일치
- 정답, 문항 연결, 대본/음원 대응처럼 채점 신뢰성에 직접 영향을 주는 결함

사람 검수자는 AI 패널을 참고한 뒤 원본 PDF/정답표/대본/음원을 직접 확인하고 기존
검수 UI에서만 `verified` 또는 `rejected`를 저장한다.

## 반복과 수렴

무한 반복 자체를 품질로 간주하지 않는다. 여러 run에서 신규 finding fingerprint와
판정 변화가 더 이상 나타나지 않을 때만 수렴 신호로 사용한다. 수렴 표시가 있어도
사람 검수는 생략하지 않는다.

한 run 내부에서는 마지막 pass에 신규 finding이 없고 최근 pass 간 finding 집합이 안정되는
지를 계산한다. 여러 run에 대해서도 같은 문항의 판정/finding 변화를 비교한다. 이 값은
"더 검수할 가치가 줄어들고 있다"는 운영 신호일 뿐 자동 승인 조건이 아니다.

## 중단과 재개

각 pass는 `ai_audit_checkpoints`에 완료 subject와 state를 새 행으로 추가할 수 있다. 과거
checkpoint는 수정하지 않는다. resume bundle에는 이미 완료한 subject를 제외한 나머지만
담기므로 사용자가 중단을 요청한 시점까지의 작업을 보존하고 이후 같은 pass를 이어갈 수
있다. resumed final은 export 당시 최신 checkpoint의 sequence/SHA를 함께 반환해야 하며,
importer는 이 값을 확인한 뒤 checkpoint의 완료 결과와 남은 subject 결과를 원자적으로 최종
result로 구성한다. checkpoint가 바뀐 뒤의 오래된 resume 응답은 거부한다. 최종 result가
적재되면 동일한 result의 재적재는 idempotent하게 허용하되 내용이 다른 두 번째 최종 result는
거부한다.

provider 실패·시간 초과·형식 오류는 pass 자체를 삭제하거나 실패로 덮어쓰지 않는다.
대신 `ai_audit_attempts`에 실패 원인, error code/message, 구조화 evidence, 가능하면 원시
응답 hash/내용을 새 row로 보존한다. 이후 재시도는 같은 pass의 새 attempt 번호를 사용한다.

## 운영상 완료의 의미

이 시스템이 "완료"되었다는 것은 감사 인프라와 안전장치가 동작한다는 뜻이다. 실제 35회
문항의 AI 판정이 모두 끝났다는 뜻은 아니다. 실제 콘텐츠 감사는 5개 pass에 각각 독립
에이전트를 연결해 result를 적재해야 하며, 그 뒤에도 최종 `verified`는 사람이 원본을 보고
직접 결정한다.

## 안전 규칙

- 원본 PDF, MP3, 로컬 DB와 감사 결과 corpus는 계속 Git 제외 대상이다.
- source snapshot이 바뀌면 과거 결과를 현재 데이터의 근거로 승격하지 않는다.
- AI 결과는 증거/단서이며 NIIED 인증, 정답 공식성, 재배포 권리를 의미하지 않는다.
- 감사 입력에 포함된 시험 문구는 데이터로 취급하며 도구 실행 지시로 취급하지 않는다.

## 테스트 기준

완료 상태는 최소한 다음을 만족해야 한다.

- 기존 사람 검수 테스트 전부 통과
- AI audit append-only UPDATE/DELETE 거부
- blind input에 이전 감사 결과 미포함
- 같은 finding의 deterministic dedup
- 동일 결함 재현 횟수와 서로 다른 verdict의 disagreement 계산
- source snapshot/input/result SHA 검증
- checkpoint/resume가 기존 결과를 덮어쓰지 않음
- resumed final이 남은 subject만 반환해도 최신 checkpoint와 결합되어 완료되고 stale resume는 거부됨
- failed/timed_out/invalid attempt가 다른 pass/consensus를 중단시키지 않음
- 실패 후 retry가 단조 증가하는 attempt 번호와 전체 오류/evidence를 보존
- 성공 result와 succeeded attempt의 원자적 저장
- 구조화 finding evidence가 저장뿐 아니라 조회/UI에서도 손실 없이 왕복
- 문항별 실행 상태가 해당 문항을 실제 포함한 pass로만 제한됨
- targeted run의 immutable subject 집합과 perspective 교집합이 보존됨
- 여러 run 뒤에도 문항별 누적 감사 수·불일치·과거 finding·attempt가 재오픈/재조회됨
- 관련 없는 최신 targeted run이 다른 문항의 최신 적용 summary/status를 가리지 않음
- 미완료 최신 run을 근거로 과거 finding을 resolved 또는 비재현으로 오판하지 않음
- 검수 UI의 필터 목록과 상세 선택이 같은 문항을 가리키며 dirty 상태에서는 자동 이동하지 않음
- AI 감사 실행 전후 human review 상태/이력/음원 verified 상태 불변
- AI 테이블이 없는 기존 DB에서도 검수 UI 정상 동작

첫 독립성·복원력 검증은 `docs/ai-audit-first-validation-2026-10-05.md`, 다중-run/중단·재개/
재감사/UI 영속성 검증은 `docs/ai-audit-second-validation-2026-10-05.md`에 기록한다.
