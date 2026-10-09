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

로컬 Microsoft Edge headless의 `--dump-dom` 검증은 프로세스 종료 코드 0에도 표준 출력에 DOM이 없어 실제 브라우저 화면·모바일 외관을 확정하지 못했다. 실제 브라우저 UX와 실제 PostgreSQL의 동시 요청/처리 시간은 미검증으로 남긴다. Node DOM 회귀와 임시 HTTP 테스트는 이 제한을 대체하는 테스트 결과로 구분한다.

## 적용 범위

실행 중인 기존 검수 서버에는 아직 변경이 반영됐다고 확인하지 않았다. 서버를 강제로 재시작하지 않았다. F2–F5 수정에는 운영 DB 쓰기, `v6 --apply`, 승인 POST, PDF/MP3 원본 변경이 없었다. 신규 collector의 URL provenance 파일은 앞으로 실제 `--download`를 실행할 때 해당 수집 root의 `catalog/`에 기록된다.
