# TopikDatabase 프로젝트 점검 — 2026-10-09

후속 작업: F1의 상세 조회 snapshot 조건을 수정하고 독립 PostgreSQL에서
수정 전·후 경합을 검증했다. 아래 본문은 최초 점검 시점의 기록이며,
후속 수정 결과와 운영 반영 경계는
`docs/review-snapshot-fix-2026-10-09.md`를 참조한다. F2–F5는 이번 수정 범위에 포함하지 않았다.

## 범위와 판정 기준

- 점검 기준 커밋: `7c4962c`, 브랜치 `feat/topik36-pdf-ingestion-20261009`.
- PDF 수집, 35/36회 추출·staging, 검수 UI/API, PostgreSQL 어댑터, AI 감사, 음원, SQLite 동결 및 마이그레이션 보호 장치를 검토했다. 핵심 경로의 정적 검토와 전체 회귀 테스트, 별도 임시 fixture 재현, 운영 DB 읽기 전용 확인을 조합했다. 모든 파일의 모든 분기를 검증했다는 의미는 아니다.
- P1은 데이터 유실 가능성이 있어 우선 수정, P2는 기능 또는 데이터 신뢰성 결함, P3는 제한적인 표시 문제다.
- 코드상 확인, 임시 데이터 재현, 운영 DB 관측을 구분한다. 재현된 결함이 실제 운영 데이터에서 이미 발생했다고 판단하지 않는다.
- 구현 수정, 운영 승인 POST, 마이그레이션 `--apply`, 서버 재시작, Git 커밋·push는 수행하지 않았다. 이 보고서만 추가했다.

## 확인한 현재 상태

중앙 PostgreSQL은 `transaction_read_only=on` 및 repeatable-read 트랜잭션으로 조회했다. 다음 값은 이 점검 시점의 관측이며 이후 검수에 따라 바뀔 수 있다.

| 항목 | 관측 |
| --- | --- |
| 35회 문항 | 70개 verified |
| 35회 대본 | 30개 verified |
| 36회 문항 | 9개 verified, 61개 needs_manual_review |
| 36회 대본 | 9개 verified, 21개 needs_manual_review |
| question 검수 이력 | 35회 191건, 36회 9건 |
| 36회 구두점 marker | v4-to-v5 있음, v5-to-v6 없음 |

v6 마이그레이션의 실제 읽기 전용 preflight도 통과했다. 결과는 `status=pending`, `mode=dry_run`, v5 기준 502개 DB 필드, 변경 대상 33개 필드·109개 공백이다. 운영 적용은 하지 않았다. 35회 source fingerprint는 고정 기준과 일치했다.

현재 PC에서 `scripts/stage10_sqlite_freeze.py verify`는 `status=ok`, source 4개·archive copy 4개, `sqlite_operational_writes=false`를 반환했다. 다른 물리적 PC의 현재 freeze 상태까지 검증한 것은 아니다.

## F1 — P1: 35회 상세 조회에서 본문과 버전의 snapshot이 달라질 수 있음

위치: `src/review_ui.py:1328`, 특히 1333–1336의 snapshot 조건, 후속 history/version 조회 및 `save_review`.

`get_question()`은 PostgreSQL에서 **36회에만** REPEATABLE READ를 설정한다. 35회는 본문, 선택지, 이력/버전을 여러 SQL로 읽는다. READ COMMITTED 환경에서 본문을 읽은 직후 다른 검수자가 수정·커밋하면, 기존 본문에 새 이력/버전이 붙는 응답이 가능하다. 그 응답으로 저장하면 버전 검사를 통과하면서 먼저 저장된 수정 내용을 덮어쓸 수 있다. 저장 시 행 잠금만으로는 이미 잘못 조합된 읽기 응답을 해결하지 못한다.

재현: 별도 임시 SQLite fixture를 이용해 PostgreSQL READ COMMITTED의 SQL 사이 커밋 가시성을 모사했다. `_question` 조회 직후 별도 연결이 본문을 `new concurrent text`로 수정하고 검수 이력을 추가했다. PostgreSQL용 분기로 실행한 상세 조회는 기존 본문 `035 question`과 새 버전 `2`를 반환했다. 이 응답을 실제 `save_review()`의 임시 SQLite 쓰기 경로에 제출하자 저장이 승인되고 본문이 `035 question`으로 되돌아갔다.

확인 경계: 읽기 분기 및 버전 검증 실패를 로컬 fixture에서 재현했다. 실제 운영 PostgreSQL 동시 쓰기는 수행하지 않았으며, 운영 데이터 유실 이력도 확인하지 않았다.

수정 방향: 모든 PostgreSQL 상세 조회에 일관된 snapshot을 적용한다. 35회에서도 본문 조회와 이력 조회 사이에 다른 검수 커밋이 발생하는 테스트를 추가하고, 폐기 가능한 PostgreSQL에서 같은 경합을 검증한다.

## F2 — P2: 감사 목록의 캐시가 문항별 실행 상태를 섞음

위치: `src/review_ui.py:431–445`, 특히 `cache_key = run_id`.

`_ai_audit_execution(..., subject_id=qid, run_id=run_id)`는 문항별 결과를 반환하지만, 목록 내부 캐시는 run ID만 키로 쓴다. 동일 run의 서로 다른 문항은 첫 번째 문항의 pass/attempt 상태를 재사용한다.

재현: 임시 감사 DB에 듣기 전용 pass와 전체 문항 pass를 만들고, 듣기 전용 pass에만 timed_out attempt를 기록했다. 읽기 문항의 실제 `status_report(..., subject_id=reading_id)`는 attempt 0개지만 목록에는 1개로 표시됐다. 목록의 `latest_run.subject_id`도 읽기 문항이 아닌 `035-I-L-001`이었다.

영향: 실제로 받지 않은 감사의 실패·재시도를 다른 문항에 귀속한다. 저장된 원본 감사 이력 자체가 바뀌는 문제는 아니다. 현재 기본 UI는 F3 때문에 이 목록 API를 이용하지 않지만, 해당 API의 반환값은 잘못된다.

수정 방향: `(run_id, qid)`로 캐시하거나, run 공통 자료만 재사용하고 subject별 집계를 분리한다. 감사 모듈 자체의 scope 테스트 외에 `ReviewStore.list_questions()` 반환값을 검증하는 통합 회귀가 필요하다.

## F3 — P2: 빠른 목록 도입 후 AI 필터·정렬이 항상 숨겨짐

위치: `src/review_ui.html:722–746`, `src/review_ui.py:523` 및 상세 감사 toggle 처리.

프런트엔드 `loadList()`는 `/api/questions-fast`만 호출한다. 이 API는 `ai_audit_available=false`를 고정 반환하며 AI 요약도 제공하지 않는다. UI는 이 응답에 따라 AI 필터·정렬을 숨긴다. 이후 요약 목록을 받아 보강하는 경로가 없고, 상세 감사 펼치기는 선택한 detail만 갱신한다.

재현: HTML에서 실제 `loadList()` 함수를 추출해 Node VM과 최소 DOM 모형으로 실행했다. fast API 형태의 정상 응답 이후 `aiFilterWrap.hidden=true`, `sortOrderWrap.hidden=true`가 됐다. 백엔드의 감사 없는 빠른 응답 계약과 프런트엔드의 후속 요청 부재도 소스로 확인했다.

영향: 기존 감사 DB가 있어도 위험도/미해결 발견 기준으로 전체 목록을 필터·정렬할 수 없다. 독립 감사 비교 및 문항별 상세 감사 조회가 전부 사라진다는 뜻은 아니다.

수정 방향: 빠른 최초 표시를 유지하면서 비동기로 요약을 보강한다. summary를 병합할 때 회차·문항 ID와 최신 검수 버전을 보존한다. 초기 fast 응답 이후 실제 요약이 들어오면 필터·정렬이 활성화되는 테스트가 필요하다.

## F4 — P2: 수집 링크 순서가 바뀌면 기존 파일의 출처가 뒤바뀜

위치: `collect_topik_pdfs.py:140`, `collect_topik_pdfs.py:178–194`.

파일 이름은 페이지 링크의 순번으로 정해진다. 같은 이름의 파일이 있으면 URL이나 이전 provenance와 대조하지 않고 PDF 헤더만 검사한 뒤 `already_present`로 사용한다. 같은 종류의 링크가 재정렬·교체되면 현재 URL에 다른 PDF의 bytes/hash를 연결할 수 있다.

재현: 네트워크를 mock한 임시 폴더에서 동일한 종류의 링크 A/B를 수집한 후 B/A 순서로 다시 실행했다. 두 번째 inventory는 `url=B → PDF A`, `url=A → PDF B`이며 둘 다 `already_present`였다.

영향: 다운로드 로그의 출처 신뢰성이 깨진다. 현재 35/36회 운영 corpus에 이 오류가 발생했다는 증거는 없다. README대로 collector의 `local_sources/`와 별도 corpus는 자동 합쳐지지 않는다.

수정 방향: URL 식별자를 반영한 안정적인 파일 이름 또는 URL→파일→hash의 지속 provenance mapping을 사용한다. 기존 파일은 해당 출처 계약과 PDF trailer까지 검사하고, 불일치 시 중단한다. 링크 순서 변경·추가·삭제·교체 테스트를 추가한다.

## F5 — P3: 구조화된 추출 warning이 `[object Object]`로 표시됨

위치: `src/review_ui.html:542–546`, `src/review_ui.html:1307–1311`; 입력 계약은 `scripts/import_exam_staging.py`의 warning 검증·저장 부분.

staging importer는 warning을 `{severity, code, message}` 객체로 받고 `preview_flags_json`에 저장한다. 프런트엔드는 해당 객체를 그대로 `element(..., flag)`에 넘기며 helper는 `String(flag)`로 변환한다.

재현: 실제 HTML의 `element()`를 추출해 정상 구조의 warning 객체를 전달하면 textContent가 `[object Object]`가 된다. 실제 브라우저 화면 전체의 재현은 하지 않았다. warning 배열이 비어 있는 문항에는 영향이 없다.

수정 방향: 문자열인 기존 flag와 객체 warning을 모두 지원하고 message를 표시하며 code/severity도 읽을 수 있게 한다. transcript warning을 어느 영역에 표시할지도 명시적으로 정리한다.

## 검증 결과와 한계

- `.venv/Scripts/python.exe -m unittest discover -s tests -v`: 264개 실행, 실패·오류 0개, skipped 1개, 154.259초. 즉 263개 통과.
- 건너뛴 항목은 `Stage8PostgresClipTests.test_clip_output_symlink_escape_is_rejected_before_outside_directory_creation`. 별도 재실행에서도 Windows symlink 권한 부족(`WinError 1314`)으로 skip됐다.
- `.venv/Scripts/python.exe -m pip check`: `No broken requirements found`.
- Stage 10 로컬 보관본 검증 성공, 중앙 PostgreSQL 읽기 전용 상태 확인 성공, v6 읽기 전용 migration preflight 성공.
- F1/F2/F4는 임시 데이터로 재현했고 F3/F5는 실제 HTML 함수를 Node에서 실행했다. 원본 문제지·음원 및 운영 DB에 쓰지 않았다.
- PostgreSQL 관련 테스트의 상당수는 SQLite 기반 어댑터 또는 mock을 사용한다. 전체 테스트 통과는 실제 PostgreSQL의 row lock·isolation·동시 쓰기·복구 검증을 대체하지 않는다.
- Chrome 실제 사용자 동작, 운영 승인 POST, 서버에 떠 있는 Python 프로세스의 코드 revision, 다른 PC의 현재 상태는 검증하지 않았다.
- 70문항 전체를 원본 PDF 및 전체 MP3와 사람이 다시 대조한 내용 검수는 수행하지 않았다. 추출 재현성·해시·정답 파싱 테스트가 전체 교육 콘텐츠의 최종 정확성을 보증하지는 않는다.
- CI는 현재 secret scan workflow만 확인됐다. 전체 회귀의 일부는 Git에 없는 corpus/staging을 직접 요구하므로 새 checkout만으로 같은 검증을 재현하기 어렵다. 공개 가능한 synthetic fixture와 corpus 필요 검증을 분리하고 설치 의존성을 명문화할 필요가 있다.

## 권장 작업 순서

1. F1의 35회 읽기 snapshot과 동시 저장 회귀를 수정하고 폐기 가능한 PostgreSQL로 확인한다.
2. F2의 문항별 scope 캐시를 수정한 후 F3의 비동기 AI 요약 목록을 복구한다.
3. F4의 안정적인 수집 provenance와 F5의 warning 표시를 수정한다.
4. 독립 fixture 기반 CI와 실제 브라우저 검증을 보강한다.
5. v6 운영 마이그레이션은 현재 검수 이후 백업·독립 복원·review snapshot 검증이라는 기존 조건을 충족한 별도 작업으로 수행한다. 이 점검은 적용 승인이 아니다.
