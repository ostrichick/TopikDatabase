# F2–F5 구현 결과 — 2026-10-09

## 구현 범위

- 브랜치: `feat/topik36-pdf-ingestion-20261009` (기준 `94dd4ec`)
- 코드 커밋: F4 `1464a62` (`fix(collector): preserve PDF URL and hash provenance`), F2/F3/F5 `9c8e37c` (`fix(review): isolate audit scope and restore review summaries and warnings`). 코드 변경은 로컬 커밋했고 원격 push와 운영 서버 반영은 별개다.
- 근거: `docs/audit-followup-handoff-2026-10-09.md`, `docs/f2-f5-implementation-plan-2026-10-09.md`.
- 기존 F1의 35회 상세 snapshot/409 충돌 보호, 승인·검수 이력, PDF/MP3 원본, 36회 v6 운영 migration 상태를 변경하지 않았다.

| 항목 | 수정 파일 | 확인된 재현 → 변경 |
| --- | --- | --- |
| F2 문항별 AI 감사 캐시 | `src/review_ui.py`, `tests/test_review_audit_list_scope.py` | 동일 실행의 듣기 timed_out attempt가 읽기에 오귀속(읽기 1 vs 실제 0) → `(run_id, qid)` 분리 후 일치, 원본 인간 승인·이력 불변 |
| F3 AI 필터/정렬 복구 | `src/review_ui.py`, `src/review_ui.html`, `tests/test_review_ai_summary_flow.py`, `tests/test_review_ai_summary_http.py` | fast 응답 뒤 감사 요약 요청 없음 → `/api/questions-ai-summary`를 비동기로 호출하고 `ai_audit`만 병합. `ready/loading/unavailable/error`, 세대 번호, 승인 ACK 버전, 기존 감사 필터, 회차 불일치, 요청 순서 역전, 미저장 초안, 실패를 테스트 |
| F4 PDF URL provenance | `collect_topik_pdfs.py`, `tests/test_collector.py` | A/B 링크 순서 변경 시 이전 PDF가 다른 URL에 연결 → canonical URL 해시 기반 경로, `catalog/pdf_provenance.json`에 URL·파일·SHA 매핑, 기존 파일 재사용/무결성 검사, 출처 미상 legacy fail-closed, 원자적 파일·inventory 기록과 오류 종료 코드 |
| F5 구조화 warning 표시 | `src/review_ui.html`, `tests/test_review_warning_render.py` | 구조화 warning의 `[object Object]` 및 듣기 대본 warning 누락 → 문항/대본 출처, 중요도, 코드, 메시지 구분. 안전한 textContent, role=list/listitem, 긴 메시지 줄바꿈/좁은 화면 스타일 |

## 새 검증 및 결과

- **F2:** `tests.test_review_audit_list_scope` — 수정 전 `AssertionError: 1 != 0`; 수정 후 통과.
- **F3:** 실제 `loadList()` 기반 Node VM 회귀는 수정 전 요약 요청이 없어 실패; 수정 후 통과. SQLite fixture API 계약 테스트는 human-review status/version을 요약 응답에 넣지 않음을 검증. 재시도·에러·회차 불일치·지연/구세대 응답도 확인.
- **F4:** 최초 15개 시나리오에서 수정 전 9 FAIL + 1 ERROR; 구현 후 확장된 19개 테스트 통과. 네트워크/파일 변경은 폐기 가능한 temporary root와 mock 사용.
- **F5:** 실제 `renderReferences()` 기반 Node DOM 테스트에서 대본 경고 누락과 객체 렌더링 문제 재현; 수정 후 안전 렌더 테스트 통과.
- 관련 집중 회귀: **35개 테스트 모두 통과** (`tests.test_review_audit_list_scope`, `tests.test_review_ai_summary_flow`, `tests.test_collector`, `tests.test_review_warning_render`, `tests.test_review_ui_flow`, `tests.test_review_ui_media_urls`).
- 이후 추가한 `tests.test_review_ai_summary_http` 포함 F2/F3 관련 5개 테스트도 **모두 통과**. 새 테스트는 실제 폐기용 localhost HTTP GET API 응답을 확인한다.
- 전체 회귀: `python -m unittest discover -s tests -q` — **298개 실행, 실패/오류 0, skipped 5** (추가 마지막 F3 상태 테스트 작성 전 실행; 해당 신규 테스트는 집중 회귀에서 별도 통과).
- F1 snapshot: `python -m unittest tests.test_review_snapshot -v` — **16개 실행, 11개 통과, 5개 skip**. Skip 원인은 `TOPIK_SNAPSHOT_TEST_DATABASE_URL`로 별도 폐기용 실제 PostgreSQL을 지정하지 않은 것. 기존 SQLite 및 시뮬레이션 경합 회귀 통과.
- `.venv/Scripts/python.exe -m pip check`: **No broken requirements found**.
- `node --check`로 `src/review_ui.html`의 전체 inline JavaScript 구문 검사 **통과**.
- `git diff --check`: **통과** (Windows CRLF 변환 알림만 있음).

## 실제 로컬 HTTP·브라우저 검증 경계

폐기 가능한 SQLite fixture에 2개 문항과 문항/대본 경고를 넣고 **별도의 `127.0.0.1` 임시 서버**에서 HTTP GET을 실행했다. `/api/questions-fast`의 2개 행, `/api/questions-ai-summary`의 `ready` 상태 및 인간 검수 status 필드가 없는 계약, `/api/questions/035-I-L-001`의 문항·대본 warning JSON을 확인했다. 이 경로는 `tests.test_review_ai_summary_http`에 재실행 가능한 회귀로 남겼다. 운영 서버·운영 PostgreSQL에는 연결하지 않았다.

최초 headless Edge `--dump-dom` 방식에서는 프로세스 종료 코드 0에도 표준 출력이 비었다. **이후 기존 브라우저의 CDP 세션으로 실제 렌더링을 검증했으며, 아래 추가 검증 기록이 이 최초 한계를 갱신한다.**

## 적용 범위

실행 중인 기존 검수 서버에는 아직 변경이 반영됐다고 확인하지 않았다. 서버를 강제로 재시작하지 않았다. F2–F5 수정에는 운영 DB 쓰기, `v6 --apply`, 승인 POST, PDF/MP3 원본 변경이 없었다. 신규 collector의 URL provenance 파일은 앞으로 실제 `--download`를 실행할 때 해당 수집 root의 `catalog/`에 기록된다.

## 추가 검증 — 기존 브라우저와 중앙 PostgreSQL (2026-10-09)

사용자 요청에 따라 최초 누락된 검증 경로를 다시 점검했다.

1. **중앙 DB 접근 경로 확인:** 기존 환경변수의 `wordpress-blog:55432`는 SSH 로컬 포워드를 전제로 한 `sslmode=verify-full` 연결이다. 최초의 DNS 실패는 SSH 터널을 열지 않은 검증 절차 문제였다. 저장소의 `scripts/start_postgres_review.ps1`와 `docs/postgres-foundation.md`에서 `ssh -L 127.0.0.1:55432:127.0.0.1:5432 bloguito` 경로를 확인했다. 별도 테스트용 SSH 터널을 띄운 후 사용 중인 중앙 PostgreSQL에 정상 연결했다. 계정·인증서·설정 파일은 수정하지 않았다.
2. **운영 DB 읽기 전용:** `transaction_read_only=on`, `REPEATABLE READ, READ ONLY`에서 35회 `verified=70`, 36회 `verified=9`, `needs_manual_review=61`을 조회했다. `ReviewStore.list_questions_fast()`의 35회/36회 각각 70행, AI 요약 응답의 스키마 및 `035-I-L-001`과 `035-I-R-031`의 `latest_run.subject_id`가 각각 원 문항과 일치함을 확인했다. 모두 읽기 전용이며 승인 POST와 데이터 변경은 없다.
3. **실제 브라우저 검증 성공:** 이미 열려 있던 로컬 CDP 디버깅 포트 `127.0.0.1:9222`의 Edge 브라우저에 붙어, 별도 폐기 가능한 SQLite fixture를 제공하는 임시 로컬 검수 서버용 **새 테스트 탭 1개만 만들었다가 닫았다**. 실제 브라우저 DOM에서 390px 화면의 `2 / 2` 문항, AI 필터·정렬 표시, 문항/대본 경고 2건과 필터 선택을 확인했다. 320px 화면에서는 `clientWidth=scrollWidth=320`, 원본 대조 모바일 `role=dialog`, `aria-expanded=true`, 경고 2건 확인 및 스크린샷 육안 점검까지 완료했다. 테스트 fixture에 미디어 파일이 없어 원본 PDF iframe은 오류를 표시했으므로 PDF 뷰어 자체의 정상 렌더링 검증은 아니다. 현재 열린 사용자의 원래 탭은 조작하지 않았다.
4. **새로 발견된 실제 성능 제약:** 동일 중앙 PostgreSQL에 접속한 35회 AI 요약의 백그라운드 조회가 **122초 관측 종료 시점까지 `loading`**이었다. F3 프런트엔드의 현재 재시도 예산(~44초) 안에 실데이터 요약이 완성되는지는 **검증 실패**이며, 실제 AI 필터 활성화까지의 지연이 남아 있다. 이는 초기 fast 목록 70행 조회와 분리된 경로다. 먼저 감사 집계 SQL/호출 횟수와 원격 왕복 시간을 계측하여 전용 요약 경로를 최적화해야 한다. 122초 후에도 항상 완료되지 않는다고 단정한 것은 아니며, 이번 계측에서 완료를 관측하지 못한 것이다.

**결론:** 최초의 "브라우저 불가" 판단은 잘못된 검증 방법을 충분히 대체하지 못한 결과였다. 로컬 CDP를 통해 실제 브라우저 검증에 성공했다. 중앙 DB도 별도의 물리 서버 없이 읽기 전용으로 검증할 수 있었다. 단, 이전에 건너뛴 5개의 *실제 동시 쓰기·rollback* 테스트는 운영 DB에 적용하지 않는다. F3의 중앙 DB AI 요약 응답 지연은 후속 성능 개선 대상으로 남는다.
