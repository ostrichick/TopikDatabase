# F2–F5 구현 및 검증 계획 — 2026-10-09

## 범위와 기준선

- 대상: `feat/topik36-pdf-ingestion-20261009` 브랜치, 기준 커밋 `94dd4ec`.
- 근거: `docs/audit-followup-handoff-2026-10-09.md`, `docs/project-audit-2026-10-09.md`와 현재 코드.
- 계획 작성 당시 작업 트리는 깨끗함. `.venv` Python과 Node 사용 가능.
- 기준 회귀: `python -m unittest tests.test_audit_scope_queries tests.test_collector tests.test_review_ui_flow tests.test_review_ui_media_urls -q` → **18 tests, OK**. 현재 결함을 검출하는 새 테스트는 아직 없다.
- F1 상세 조회의 PostgreSQL 일관된 snapshot 및 409 보호는 유지한다.
- 운영 PostgreSQL·실제 PDF/MP3·35/36회 원본·검수 승인/이력은 건드리지 않는다. 운영 DB에는 테스트 쓰기를 하지 않고, 36회 v6 `--apply`와 운영 서버 재시작도 포함하지 않는다.

## 0. 변경 전 확인

1. `git status --short --branch`, 브랜치/커밋, 기존 작업 트리 변경을 재확인한다. 미커밋 변경은 덮어쓰지 않는다.
2. F1 관련 `tests.test_review_snapshot`을 실행하고 결과를 기록한다. 원본 corpus 없이 실행 가능한 회귀와 환경 의존 테스트를 분리한다.
3. 각 F2–F5 결함의 **수정 전 실패 재현 테스트**를 확보한다. 이 단계에서는 수정에 필요한 독립 fixture와 Node DOM mock만 사용한다.

## 1. F2 — AI 감사 실행 상태를 문항별로 격리 (F3의 선행 작업)

**대상:** `src/review_ui.py`의 `ReviewStore.list_questions()`, `tests/test_audit_scope_queries.py` 또는 별도 관련 테스트.

**재현:** 동일 run에 듣기 문항과 읽기 문항을 만들고, 듣기 전용 pass에만 `timed_out` attempt를 만든다. 실제 subject-scoped `status_report()`와 달리 읽기 문항의 목록 응답에도 듣기 attempt가 나타나는 실패를 테스트로 고정한다. `latest_run.subject_id`, `run_id=None`인 미완료 실행도 검사한다.

**수정:** `exec_cache`를 `(run_id, qid)`로 분리한다. run 공통 데이터를 최적화하더라도 eligible pass/attempt 집계는 subject별로 유지한다. 기존 원시 감사 기록/검수 데이터에 쓰지 않는다.

**통과 기준:** 모든 문항의 `list_questions().items[].ai_audit`가 해당 subject 상태 보고와 일치하고, 감사 대상이 아닌 문항에 실패·재시도가 붙지 않는다. 기존 감사 관련 테스트도 통과한다.

## 2. F3 — 빠른 목록 표시 후 AI 요약을 비동기로 병합

**대상:** `src/review_ui.html`의 `loadList`·`init`·필터/정렬 처리, `src/review_ui.py`의 목록·요약 API, `tests/test_review_ui_flow.py`와 필요 시 새 API 테스트.

**설계:** `/api/questions-fast`로 현재 인간 검수 목록을 즉시 표시하는 흐름을 유지한다. 이어 **감사 정보만 제공하는 목록 요약 API**를 조회하는 방향을 우선 검토한다(후보: `GET /api/questions-ai-summary?exam_id=...`). `GET /api/questions` 전체 응답을 재사용하는 방안과 조회 비용/캐시 동작을 비교한 후 결정한다. API 결과에는 `exam_id`, `ai_audit_available`, `state`(`ready`/`loading`/`unavailable`/`error`) 및 `question_id`별 요약만 담는다. 실제 설계가 이 후보와 달라지면 테스트에서 동등한 계약을 명시한다.

**병합 규칙:** ID와 회차를 검증하고 기존 문항 객체에 `ai_audit`만 덧붙인다. `status`, `review_version`, `last_human_review`, 선택 문항, 미저장 초안, `pendingReviews`·`failedReviews`·`committedReviews`, CSRF 토큰은 요약 응답으로 변경하지 않는다. 더 오래된 요청/다른 회차의 응답은 버린다. 필요 시 요청 세대 번호와 bounded retry로 `loading`을 처리한다. 감사가 없거나 조회에 실패해도 일반 검수는 즉시 가능해야 한다.

**캐시/성능:** PostgreSQL `_ai_summaries_loading`, `_ai_summaries_cache`, `_list_questions_cache`의 시점과 갱신 조건을 검토한다. `loading`을 `unavailable`로 잘못 확정하거나, 감사가 새로 생성됐을 때 빈 응답이 무기한 유지되지 않도록 한다. audit scope 밖 문항의 불필요한 조회를 피한다. 현재 감사 요약 지원 범위(35회)를 기준으로 36회에는 없는 기능을 잘못 노출하지 않는다.

**통과 기준:** fast 목록이 먼저 보이고, 유효한 감사 요약이 도착한 뒤 AI 필터와 위험도/미해결 발견/최근 감사 정렬이 작동한다. 0건, 지연, 일시적 실패, 잘못된 회차, 연속 요청 역전, 승인 ACK 이후 늦은 응답, 회차 변경, 저장 중 편집, 409 충돌에서 UI 상태가 보존된다. 실제 `loadList()` 호출 경로를 실행하는 Node 테스트와 로컬 브라우저 스모크를 포함한다.

## 3. F4 — PDF 수집 provenance와 기존 파일 재사용 보호

**대상:** `collect_topik_pdfs.py`, `tests/test_collector.py`.

**재현:** 네트워크 mock과 `TemporaryDirectory`로 A/B PDF를 모은 뒤 링크 순서를 B/A로 뒤집는다. 현재 코드가 서로 다른 URL의 바이트를 `already_present`로 기록하는 실패를 고정한다.

**수정 방향:** 링크 순번 대신 정규화된 **원본 URL의 안정적인 해시 식별자**를 파일명에 포함시키고, inventory의 URL→경로→SHA-256 관계를 검증한다. 정확히 검증된 기존 매핑만 재사용한다. 출처를 확인할 수 없는 legacy 파일은 자동 매칭하거나 덮어쓰지 않고 명시적으로 실패 또는 분리된 새 파일로 처리한다. 저장/재사용 양쪽에서 PDF 헤더·trailer·크기·해시를 검사하고 충돌을 fail-closed로 기록한다. 다운로드는 임시 파일/원자적 rename 등의 방법으로 부분 저장을 방지한다. 오류가 있을 때 CLI 종료 코드와 CSV/summary 모두에서 실패를 확인할 수 있도록 정리한다.

**통과 기준:** 같은 입력 반복, 링크 순서 변경, 추가/삭제/교체, URL 중복, 기존 파일 변조, 헤더만 있는 불완전 PDF, 다운로드 실패와 재실행에서 URL과 실제 바이트/해시가 일치한다. 기존 corpus/운영 DB의 source metadata는 일괄 수정하지 않는다.

## 4. F5 — 구조화 warning을 안전하고 명확하게 표시

**대상:** `src/review_ui.html`의 `element()`/`renderReferences()`, 필요 시 warning 관련 UI CSS 및 DOM 테스트. `scripts/import_exam_staging.py`의 `{severity, code, message}` 검증 계약을 보존한다.

**재현:** `preview_flags`에 정상 구조화 warning을 전달하면 `[object Object]`가 보이는 상황을 실제 렌더 함수 테스트로 고정한다.

**수정:** 기존 문자열 warning과 객체 warning을 모두 처리하는 표시 함수를 만든다. 객체의 `message`를 본문으로, `severity`와 `code`를 보조 정보로 구분한다. 예상하지 못한 자료형은 안전한 대체 문구로 처리하며, 내용은 `textContent`로 삽입한다. 듣기 문항의 `transcript.warnings`도 빠뜨리지 않고 출처를 구분하여 노출할지 실제 배치를 확인해 구현한다.

**통과 기준:** 문자열/객체/빈 배열/불완전 객체/HTML처럼 생긴 메시지/긴 텍스트에서 의미 없는 객체 문자열과 실행 가능한 HTML이 발생하지 않는다. 좁은 화면 및 키보드/스크린리더 가독성을 확인한다. DB와 검수 상태는 불변이다.

## 5. 통합 검증 및 인계

1. 각 단계별 **수정 전 실패 → 수정 후 통과** 기록을 남긴다. F2를 먼저 완료한 뒤 F3를 통합하고, F4/F5는 코드 충돌이 없으면 독립적으로 진행한다.
2. 변경 범위에 맞는 테스트를 먼저 실행한 뒤 `python -m unittest discover -s tests -q`와 `git diff --check`를 실행한다. corpus/의존성 누락 skip, Windows symlink 제약은 사유를 분리해 보고한다.
3. 실제 브라우저 검증 시 fast 첫 표시, AI 필터·정렬, 승인 후 다음 문제 이동, 35↔36회 전환, 경고 렌더링, 모바일 폭을 확인한다. 기존 사용 중인 검수 서버/탭을 재시작하거나 강제 새로고침하지 않고 별도 로컬 검증 인스턴스를 사용한다.
4. 필요 시 **폐기 가능한 PostgreSQL**에서 read/list/API 경합을 재현한다. 운영 DB는 읽기 전용 상태 확인까지만 허용한다.
5. 목적별로 변경을 검토하고 커밋한다. push·운영 반영은 명시적 요청과 상태 확인에 맞춰 별도로 결정한다. 최종 보고에는 변경 파일, 검증 수, skip, 실제 브라우저/PostgreSQL 검증 여부, 커밋/push, 미검증 범위를 구분한다.

## 선행 관계와 완료 판정

`기준선 → F2 → F3 → 통합 검증`이 필수 순서다. F4/F5는 F2/F3와 독립이며 별도 테스트 후 통합할 수 있다. **F2~F5의 재현 테스트·수정·회귀 검증이 모두 통과하고 기존 F1/승인 흐름이 유지**될 때 구현 완료로 판정한다. 계획 작성 시점에는 어느 F2~F5 수정도 적용되지 않았다.
