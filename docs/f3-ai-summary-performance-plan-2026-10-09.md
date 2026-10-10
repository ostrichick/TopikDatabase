# F3 AI 감사 목록 실데이터 지연: 원인 해결·최적화 실행 계획 (2026-10-09)

## 목표와 현재 확인된 근거

기존 F3 구현은 빠른 문항 목록(`/api/questions-fast`)과 감사 요약(`/api/questions-ai-summary`)을 분리했지만, **요약 서버 경로의 N+1 DB 조회는 해결하지 않았다.** 운영 중앙 PostgreSQL에는 읽기 전용으로만 접근한다.

- 현행 `ReviewStore.list_ai_audit_summary()` → `list_questions()` → `_ai_audit_summaries()` → `summarize_all_questions()` → 문항별 `summarize_question()`의 연쇄 호출.
- `summarize_all_questions()`는 감사 범위에 든 문항 70개를 순회하며 매번 실행·패스 입력 및 결과 JSON, 수렴 상태를 재조회한다.
- `list_questions()`는 요약 생성 후에도 각 문항마다 `_ai_audit_execution(..., subject_id=qid)`/`status_report()`를 호출한다. F2의 문항별 격리는 **그대로 유지**해야 한다.
- 2026-10-09 읽기 전용 계측: 단순 SQL 왕복 평균 0.209초, 요약 문항 1개당 SQL 15회·3.53~4.95초, 실행 상태 문항 1개당 SQL 23회·6.33초; 감사 대상 70개, 총 run 6개 / pass 6개 / result 5개. `input_json` 평균 약 413KB (최대 약 1.46MB). fast 목록 70문항은 1.44초. 35회 요약은 관측한 122초 동안 `loading`에서 벗어나지 못했다.
- 프런트엔드 `loadAiSummary()`는 `loading`을 최대 18회 재시도(~44초)한 뒤 오류로 표시한다. 서버가 뒤늦게 완료되어도 자동 갱신이 보장되지 않는다.
- `_ai_summaries_checked_at`의 60초 TTL, `_list_questions_cache`, `_ai_summaries_loading`, `_ai_summaries_error` 갱신 순서 및 예외 처리의 정확성도 검증이 필요하다.

## 작업 원칙

1. 기존 감사 **판정(verdict)·불일치·위험도·미해결 발견·최신 run 선택·문항별 대상 scope·convergence·attempt/retry 집계 규칙**을 보존한다. 기능 삭제 또는 무조건 0 반환으로 빠르게 만들지 않는다.
2. 문항 목록의 사람 검수 상태, 버전, 409 충돌 방지, 승인 후 다음 문제 이동, 미저장 초안, 기존 F1 snapshot과 F2 subject-scoped status는 유지한다.
3. 실제 운영 `topik` PostgreSQL에서는 `READ ONLY` 트랜잭션으로 질의 및 계측만 실행. **부하 높은 전체 집계 반복 실행, `EXPLAIN ANALYZE`/DDL·승인 POST·감사 쓰기 금지.** 쓰기·경합·rollback 검증은 동일 서버의 별도 폐기용 테스트 DB(또는 로컬 fixture)에서만 수행한다.
4. 원본 SQLite/PDF/MP3, 35·36회 검수/승인 이력, 운영 DB schema, 운영 서버/기존 브라우저 탭을 임의로 변경하지 않는다. 배포/재시작/원격 push는 구현 검증과 별도 단계다.

## 단계 A — 성능·정확성 기준선 및 실패 재현

1. `git status` 확인. 기존 코드·테스트·문서와 작업 트리 변경을 보존한다.
2. 독립 SQLite 감사 fixture와 폐기용 PostgreSQL에서 기존 `summarize_question`, `summarize_all_questions`, `status_report` 출력을 **golden baseline**으로 수집한다. 승인·감사 원본 기록은 바꾸지 않는다. JSON 키/값 정규화는 필요한 경우만, 의미 있는 차이(특히 최신 run의 partial/failed 상태)를 감추지 않는다.
3. run이 여러 개인 35회, 한 문항만 포함한 신규 run, 다른 perspective의 listening/reading 범위 차이, 빈/미완료 pass, 실패·timeout·retry, findings recurrence, cross-run convergence, 감사 기록 없는 문항, 36회 미지원 상태 등 반례를 fixture에 만든다.
4. 읽기 전용 프로파일러로 **SQL 횟수, 쿼리 유형별 왕복 시간, JSON 전송/파싱 시간, 캐시 cold/warm, fast/API 로딩 단계**를 분리 측정한다. payload·원문·개인정보·접속 문자열을 로그에 기록하지 않는다. 비싼 운영 집계는 반복 호출하지 않는다.
5. 성능 회귀 테스트에 원격 지연 시뮬레이션(예: SQL 호출마다 200ms 지연/쿼리 수 상한)을 추가해 현재 N+1 구조에서는 실패하도록 한다.

## 단계 B — 감사 요약용 일괄 집계 엔진 (핵심 수정)

**대상:** `src/ai_audit_35.py`, 필요 시 `src/database.py`, 관련 감사 테스트.

1. `ai_audit_runs`, `ai_audit_passes`, `ai_audit_results`, `ai_audit_findings`/`occurrences`, `ai_audit_attempts`, `checkpoints` 등 필요한 행을 **run/회차별 일괄 SELECT**로 가져온다. 동일 `input_json`, `raw_json`은 pass/result별로 한 번만 가져와 파싱한다.
2. 메모리에 `run_id → pass_id → subject_id` 인덱스를 구성한다. 각 문항의 최신 적격 run은 기존의 `require_result` 및 frozen pass scope 규칙대로 선택하며, 실패/미완료 pass의 execution 상태는 별도로 포함한다.
3. 기존 공통 계산 규칙을 재사용/순수 함수로 분리한다. 문항별 `summarize_question()` 70회 호출과 `status_report()` 70회 호출을 목록 경로에서 **금지**한다. 순환 호출을 줄이기 위해 최신 run 탐색·수렴 판정·위험도 계산에 이미 로드한 인덱스를 전달한다.
4. DB 읽기는 하나의 명시적 **REPEATABLE READ, READ ONLY** snapshot으로 감싸 서로 다른 시점의 pass/result/attempt 집계가 섞이지 않게 한다. SQLite fixture와 PostgreSQL adapter의 일관성을 검증한다.
5. 우선 **UI 목록에 필요한 경량 필드**만 직렬화한다(`run_id`, verdict counts, risk, unresolved, disagreement, convergence, latest time, scoped attempt/retry counts 등). history/detail 증거 JSON과 전체 pass log는 문항 선택 시 기존 상세 API에서 별도 조회한다.
6. `summarize_all_questions()`의 공개 분석용 기존 계약은 유지한다. 새 경량 API/헬퍼는 의미상 완전히 동등한 필드를 정확하게 계산하고, 기존 API를 변경해야 한다면 모든 호출자와 회귀 테스트를 함께 갱신한다. 새 DB 테이블/마이그레이션은 우선 도입하지 않는다.

## 단계 C — API·캐시·동시 실행 통제

**대상:** `src/review_ui.py`의 `list_ai_audit_summary`, `list_questions`, preload/cache 경로, `tests/test_review_ai_summary_*`.

1. `/api/questions-ai-summary`가 전체 `list_questions()`를 거쳐 다시 70문항을 조회하지 않고, B단계의 경량 엔진 결과만 반환하도록 분리한다. fast 목록과 사람 검수 상태·버전은 항상 별도 경로가 권위 있다.
2. 캐시를 감사 자료 버전과 회차 기준으로 다룬다. DB 감사 append-only 데이터의 lightweight watermark(run/pass/result/attempt/checkpoint 변화)를 확인하는 전략을 우선 설계하고, TTL은 장애 복구용 보조 수단으로 사용한다. 60초 TTL 경계에서 긴 작업이 중복 시작되거나 아직 완료되지 않은 결과가 바로 만료되지 않도록 한다.
3. 한 회차에 여러 브라우저 요청이 동시에 오더라도 집계 작업은 **single-flight 1개**만 실행한다. lock/condition, 완료/오류 처리와 generation을 분리하고 성공 시 원자적으로 최신 snapshot만 publish한다. 백그라운드 예외를 무조건 삼키지 말고 개인정보 없는 진단코드·소요시간을 남긴다.
4. 응답 `state`는 `loading`(진행 중), `ready`(정상), `unavailable`(해당 회차/데이터 미지원), `error`(실패)를 엄격히 구분한다. 응답에는 `exam_id`, 필요한 경우 summary version/etag 정도만 추가한다. 실패 또는 부분 조회를 `ready`와 빈 목록으로 위장하지 않는다.
5. 선택적으로 컴퓨팅 예산과 부하 제어(명시적 query timeout, 백오프, worker 수 상한)를 둬 느린 작업이 반복으로 쌓이지 않게 한다. 운영 DB schema 변경 없이 구현 가능한 범위에서 처리한다.

## 단계 D — UI 로딩/재시도 및 상태 보존

**대상:** `src/review_ui.html`, Node DOM 테스트 및 CDP 브라우저 검증.

1. 44초 고정 재시도를 무조건 늘리는 대신 백엔드의 실제 job 상태와 cache version에 맞춰 점진적 backoff, 명시적 `다시 시도`, 회차별 요청 취소/세대 검증을 설계한다. 성공 시 필터·정렬을 활성화한다. 오래된 `error` 표시가 `ready` 회복을 막지 않게 한다.
2. 먼저 표시된 fast 목록, 사람 승인 ACK 및 review_version, 선택 문항, 입력 초안, 검수 대기큐·충돌(409)을 AI 요약 응답으로 덮어쓰지 않는다.
3. `unavailable`과 일시적인 `loading/error`를 구분하고, 일시적 실패 중 기존 정상 AI 요약이 있으면 필터 상태를 보존한다. 실제 미지원 36회에 35회 결과가 노출되지 않게 한다.

## 단계 E — 테스트와 목표 성능 검증

1. **동등성 테스트:** 새 집계와 기존 분석 계약의 verdict/risk/findings/latest-run/scope/attempt/retry/convergence 결과를 골든 fixture로 비교한다. 35회 실제 DB는 읽기 전용 샘플 spot-check만 수행한다.
2. **쿼리 수 회귀:** 70문항에 대한 경량 요약의 SQL 호출 수가 문항 수에 선형 비례하지 않도록 상한을 명시한다. **목표 ≤20 SQL 호출/70문항**, 부가 상세 history는 별도 요청만 허용. 기준 변경 시 근거 기록.
3. **지연 목표:** 같은 노트북 → SSH 터널 → 중앙 PostgreSQL 환경에서 cold AI 요약 **목표 ≤10초**, warm **≤1초**(서버 처리 기준, 가능하면 HTTP 왕복도 별도 측정). 기존 122초 미완료 대비 유의한 개선을 보이되 네트워크 변동은 최소 3회 측정 시 분리 보고한다. 일반 목록은 기존 1.44초 기준보다 악화시키지 않는 것을 목표로 한다.
4. **장애/경합:** 감사 업데이트 중 읽기 snapshot, concurrent GET 10건의 single-flight 확인, 캐시 만료·버전 변경, 연결 실패, SQL timeout, 브라우저 회차 전환, 늦은 결과, 승인 중/직후 응답 테스트.
5. 전체 `unittest discover`, 기존 `test_ai_audit_35`, `test_ai_audit_postgres`, `test_audit_scope_queries`, `test_review_audit_list_scope`, `test_review_ai_summary_flow/http`, `test_review_ui_flow`, `test_review_snapshot` 회귀. 실제 DB에 대해 성능 측정과 read-only 검증만 한다.
6. 기존 브라우저의 CDP(`127.0.0.1:9222`)가 있을 경우 **임시 탭**에서 320/390px, 필터/정렬, 에러→복구, 회차 전환을 검증하고 탭을 종료한다. 운영 중인 검수 창을 강제 새로고침하거나 승인 POST하지 않는다.

## 결과 인계·커밋/적용 순서

- 실행 순서: **A 기준선 → B 일괄 집계 → C API/캐시 → D UI → E 통합 검증**. 각 단계의 수정 전 실패/수정 후 통과 근거를 남기고 논리적으로 나눠 커밋한다.
- 사용자 승인/운영 영향이 없는 코드·로컬 fixture 수정까지만 수행하고, 운영 서버 재시작·배포·`git push`는 별도 확인 절차로 둔다.
- 최종 보고: 변경 파일/커밋, before·after SQL 수와 시간, 동등성, 통과·skip 테스트, 실제 PostgreSQL 및 브라우저 확인, 남은 병목·위험, 운영 반영 조건.
- **완료 판정:** 전체 70문항 정확성·subject scope가 유지되고, 실데이터 AI 요약이 완료되며, 위 쿼리 수/속도 목표를 검증하고, 기존 승인·F1/F2 보호 회귀에 실패가 없어야 한다. 목표 미달 시 최적화를 끝냈다고 선언하지 않고 미달 원인을 기록한다.
