# 프로젝트 점검 F2–F5 후속 작업 인수인계 — 2026-10-09

> **후속 구현 완료 안내 (2026-10-09):** 아래 문서는 최초 인수인계 당시의 결함 재현과 구현 지침을 보존한 역사적 기준선이다. 이후 F2–F5를 수정하고 테스트했다. 현재 변경 내용과 실제 검증/미검증 경계는 `docs/f2-f5-implementation-results-2026-10-09.md`를 우선 참고한다. 아래의 "미수정" 상태표는 구현 이전 관측이다.

## 이 문서부터 읽기

사용자는 **F2–F5를 현재 컴퓨터에서 구현하지 않고 다른 컴퓨터에서 이어서
수정·테스트**하기로 했다. 이 문서는 해당 작업의 시작점이다.
이동 브랜치는 `feat/topik36-pdf-ingestion-20261009`, 원격 저장소는
`https://github.com/ostrichick/TopikDatabase.git`이다. `main`과 혼동하지 않는다.

| 항목 | 인수인계 상태 |
| --- | --- |
| F1: 35회 상세 snapshot | 수정·회귀 검증 완료, 커밋 `7b29a2533d894437775c0c62c9ae1e4eff5e5b28` |
| F2: 문항별 감사 실행 cache | 미수정, 다른 컴퓨터에서 첫 작업 |
| F3: AI 필터·정렬 미표시 | 미수정, F2 다음 작업 |
| F4: collector 출처 혼합 | 미수정 |
| F5: warning 객체 표시 | 미수정 |
| 기존 검수 서버에 F1 적용 | 기존 사용자 서버를 재시작하지 않아 확인되지 않음 |
| 36회 v6 운영 migration | 이번 작업에서 적용하지 않음 |

전체 발견 내용과 최초 관측은 `docs/project-audit-2026-10-09.md`,
F1의 수정 전·후 검증은 `docs/review-snapshot-fix-2026-10-09.md`에 있다.
문서의 운영 승인 수·marker·기기 상태는 2026-10-09 관측이며 현재 상태로
단정하지 않는다. 작업 대상 파일/라인도 pull 후 실제 코드에서 다시 확인한다.

## 다른 컴퓨터에서 가져오기

실제 저장소 위치와 Git/Python/Node 설치부터 확인한다. 아래 `C:\Projects`는
현재 컴퓨터의 경로 예시이며 다른 컴퓨터에 같은 경로가 있다고 가정하지 않는다.

```powershell
cd C:\Projects\TopikDatabase
git status --short --branch
git remote -v
git fetch origin
git branch --list feat/topik36-pdf-ingestion-20261009
```

대상 브랜치가 이미 있으면:

```powershell
git switch feat/topik36-pdf-ingestion-20261009
git pull --ff-only origin feat/topik36-pdf-ingestion-20261009
```

로컬 브랜치가 없으면:

```powershell
git switch --track origin/feat/topik36-pdf-ingestion-20261009
```

저장소 자체가 없으면 실제 작업 부모 폴더에서:

```powershell
git clone --branch feat/topik36-pdf-ingestion-20261009 --single-branch https://github.com/ostrichick/TopikDatabase.git TopikDatabase
cd TopikDatabase
```

변경 파일이 있거나 fast-forward가 거절되면 그 변경과 양쪽 커밋을 확인한다.
강제 reset/checkout, 자동 stash/pop, 기존 변경 덮어쓰기로 진행하지 않는다.
서로 다른 브랜치에서의 단순 `git pull`만으로 이번 수정이 들어왔다고 판단하지 않는다.

```powershell
git log -3 --oneline
git merge-base --is-ancestor 7b29a2533d894437775c0c62c9ae1e4eff5e5b28 HEAD
if ($LASTEXITCODE -ne 0) { throw 'F1 snapshot fix is not in this checkout' }
Test-Path docs\audit-followup-handoff-2026-10-09.md
git status --short --branch
```

## 자료·환경 경계 및 초기 검증

- Git으로 전달되는 것은 코드·테스트·문서다. `topik-past-papers/`,
  `.verification_deps`, `.venv`, `.stage9-runtime`, 원본 PDF/MP3, staging JSON,
  실제 DB·백업·인증서·SSH 키와 `..\TopikDatabase-runtime\operational.json`은
  Git으로 준비되지 않는다. 민감하거나 원본인 파일을 강제 stage하지 않는다.
- 기존 기기 자료를 보존한다. corpus/의존성/staging 준비는 기존
  `docs/cross-device-handoff-2026-10-09.md`를 참고한다. v5/v6 동결 자료는
  무조건 덮어쓰지 않고 원본 SHA와 기존 bytes를 확인한다.
- F2–F5 회귀는 가능한 한 synthetic DB, mock 네트워크, Node DOM 모형으로
  작성한다. 원본 corpus를 테스트 입력으로 강제할 필요가 없는 결함들이다.
- 중앙 운영 DB에 테스트용 쓰기를 하지 않는다. 실제 DB 쓰기·HTTP POST
  경합 검증은 별도 폐기 가능한 PostgreSQL을 사용한다.
- 사용자의 열린 검수 초안·저장 중 요청이 있는 서버를 임의로 재시작하거나
  브라우저를 강제로 새로고침하지 않는다. 새 코드의 서버 반영과 Git pull은 구분한다.

Python 실행기는 해당 기기의 준비된 환경으로 선택한다. 아래는 `.venv` 예시다.
없으면 `py -3 -m venv .venv`로 생성한 뒤 실제 필요한 의존성을 설치한다.
PostgreSQL 모듈은 `requirements-postgres.txt`, canonical 음원 도구는
`scripts/setup_media_tools.py`를 따른다. 전체 PDF 추출 의존성이 모두
두 requirements 파일에 있다고 가정하지 않는다.

```powershell
.\.venv\Scripts\python.exe --version
node --version
.\.venv\Scripts\python.exe -m unittest tests.test_review_snapshot -v
.\.venv\Scripts\python.exe -m unittest tests.test_audit_scope_queries tests.test_collector tests.test_review_ui_flow tests.test_review_ui_media_urls -v
```

F1 snapshot 모듈은 원본 corpus 없이 실행 가능하다. 이번 컴퓨터에서
live URL 없이 재실행했을 때 16개 중 11개 통과·live PostgreSQL 5개 skip이었다.
`TOPIK_SNAPSHOT_TEST_DATABASE_URL`에 명시적인 폐기용 로컬 DB를 설정하면
그 5개도 실행한다. 운영 `TOPIK_DATABASE_URL`을 여기에 복사하지 않는다.

이전 F1 최종 검증은 실제 PostgreSQL 17.11을 포함해 전체 280개 중 279개 통과,
Windows symlink 권한 제한 1개 skip, 실패·오류 0개였다. 이는 이전 기기의
검증 결과다. 다른 컴퓨터에서 재실행하지 않은 결과를 새 검증으로 보고하지 않는다.
전체 테스트 일부는 Git에 없는 corpus/staging을 요구하므로 환경 준비 후 실행한다.
이번 컴퓨터의 원시 테스트 로그와 중지된 PostgreSQL 클러스터는
Git-ignored `.stage9-runtime`에만 있으며 pull로 이동하지 않는다.

## F2 — 먼저 문항별 감사 실행 cache 수정

위치: `src/review_ui.py`, `ReviewStore.list_questions()`의 `exec_cache` 및
`cache_key = run_id`; 실제 범위를 `rg -n 'exec_cache|cache_key|_ai_audit_execution' src/review_ui.py`로 찾는다.

**확인된 원인:** `_ai_audit_execution(..., subject_id=qid)`가 문항별 결과인데
run ID만 키로 캐시한다. 동일 run 내 다른 문항은 첫 문항의 실행 상태를 재사용한다.

**수정 전 재현:** 임시 DB에 듣기 전용 `transcript_alignment` pass와 모든 문항
`independent` pass를 같은 run에 만든다. 듣기 pass에만 timed_out attempt를
기록한다. 읽기 문항은 `status_report(..., subject_id=reading_id)`에서 attempt 0개,
`ReviewStore.list_questions()`에서는 attempt 1개이고 latest_run.subject_id도 듣기다.
독립 fixture는 `tests/test_ai_audit_35.py`의 `AIAudit35Tests`, `Q1`, `Q2`를 참고한다.

**구현 방향:** 가장 작은 수정은 `(run_id, qid)` 캐시다. 성능상 run 공통 자료를
묶더라도 subject별 eligible pass/attempt 집계와 식별자는 분리한다.

**완료 기준:** 같은 run의 듣기/읽기에 서로 다른 attempt·실패·재시도 상태를
넣고 list API가 subject-scoped status와 일치하는 테스트를 추가한다. pass를
전달받지 않은 문항에 실패를 귀속하지 않아야 한다. `run_id=None`의 아직
완료되지 않은 run도 검사한다. 저장된 감사 원본과 인간 승인 상태는 바뀌면 안 된다.

## F3 — 빠른 첫 표시 후 AI 필터·정렬 복구

위치: `src/review_ui.html`의 `loadList`, `init`, 상세 감사 toggle,
`src/review_ui.py`의 `list_questions_fast`, `list_questions`.

**확인된 원인:** 기본 프런트엔드는 `/api/questions-fast`만 요청하고,
이 응답은 `ai_audit_available=false`로 고정돼 AI 필터·정렬이 계속 숨겨진다.
상세 감사 펼치기는 해당 detail만 갱신하므로 전체 목록 기능을 복구하지 않는다.

**수정 전 재현:** 실제 `loadList()`를 추출해 fast 응답과 최소 DOM 모형을
주는 Node 테스트에서 `aiFilterWrap.hidden`과 `sortOrderWrap.hidden`은 둘 다 true다.
현재 전체 UI에는 후속 목록 summary 요청이 없다. 기존
`tests/test_review_ui_flow.py`의 필터 함수 테스트 통과만으로 실제 초기화
흐름을 검증했다고 판단하지 않는다.

**구현 방향:** 최초 fast 목록을 유지하고 AI 요약을 비동기로 보강한다.
기존 full 목록을 재사용할지 summary 전용 API를 만들지는 코드/성능을 확인해 결정한다.
기존 `list_questions()`/AI 요약 캐시의 갱신 방식도 확인한다. 오래된 전체 목록을
그대로 덮어써 최신 인간 승인·버전·현재 초안·선택 문항을 되돌리지 않는다.

**완료 기준:** fast 응답 후 실제 요약이 들어오면 필터·정렬이 활성화되고
미해결 발견/위험도/최근 감사 순서가 정확해야 한다. 감사 지연·실패·없는 DB에서
일반 검수는 계속 가능해야 하며, 감사 없음과 조회 실패를 혼동하지 않는다.
늦은 응답이 승인 ACK·다른 회차·더 최신 요청을 덮어쓰지 않는 회귀를 추가한다.
Node 테스트와 실제 브라우저에서 초기 로딩·필터·승인 후 이동·회차 전환을 확인한다.

## F4 — collector 파일과 URL의 provenance 보존

위치: `collect_topik_pdfs.py`의 `unique_filename`, 링크 enumerate,
`destination.exists()`/`already_present` 처리. 기본 테스트는 `tests/test_collector.py`.

**확인된 원인:** 파일 이름을 링크 순번으로 정하고, 같은 이름의 파일을
URL 대조 없이 PDF 헤더만 검사해 재사용한다.

**수정 전 재현:** TemporaryDirectory에 source_pages.csv 한 행을 준비한다.
`inspect_page`를 같은 종류의 PDF 링크 A/B로 mock하고 `read_bytes`는 서로 다른
`%PDF-1.4 ... %%EOF` bytes를 반환하게 한다. `--download`로 실행한 뒤 같은
폴더에서 링크를 B/A로 바꿔 다시 실행한다. 현재 두 번째 CSV는 URL B→PDF A,
URL A→PDF B를 각각 already_present로 기록한다. 네트워크와 sleep은 mock한다.

**구현 방향:** URL 식별자를 반영한 안정적인 파일 이름 또는 지속적인
URL→파일→hash mapping을 사용한다. 기존 파일과 이전 inventory의 관계를
안전하게 대조하고 모호한 legacy 파일은 무조건 재사용하지 않는다. 기존 파일
덮어쓰기·삭제 또는 corpus/운영 DB의 source metadata 배치 수정으로 해결하지 않는다.

**완료 기준:** 동일 입력 재실행, 순서 변경, 링크 추가·삭제·교체, 파일 변조,
헤더만 있고 trailer가 없는 파일을 검사한다. 수집 실패가 CSV/summary에
명확히 남고, URL과 다른 파일 bytes/hash를 정상 출처로 기록하지 않아야 한다.
현재 운영 corpus가 이미 잘못됐다고 단정할 증거는 없다.

## F5 — 구조화 warning을 사람이 읽을 수 있게 표시

위치: `src/review_ui.html`의 `element`, `renderReferences` 및
`scripts/import_exam_staging.py`의 warning 검증·저장 계약.

**확인된 원인/재현:** 정상 warning `{severity:'review', code:'source_check',
message:'Review original PDF'}`를 실제 `element('div','flag',flag)`에 전달하면
textContent는 `[object Object]`다. importer는 해당 구조를 정상으로 저장한다.
warning이 비어 있는 문항에는 이 문제가 드러나지 않는다.

**구현 방향:** 기존 문자열 flag와 객체 warning을 모두 지원한다. message를
읽을 수 있게 표시하고 code/severity를 구분한다. 사용자 입력은 textContent로
렌더링한다. transcript warning의 표시 여부는 기존 UX를 확인해 명시적으로 결정한다.

**완료 기준:** 문자열, 정상 객체, 빈 배열, 불완전/예상 밖 객체, HTML처럼 생긴
message를 실제 렌더 함수에서 검사한다. `[object Object]`나 실행 가능한 HTML이
나오지 않아야 하며, DB 내용·검수 상태를 바꾸지 않는다. 브라우저에서 긴 경고와
좁은 화면의 가독성도 확인한다.

## 작업 순서·보존 조건·최종 보고

1. 다른 컴퓨터의 기존 변경과 실행 환경을 확인하고 위 baseline 검증을 실행한다.
2. F2를 재현하는 실패 테스트를 작성하고 최소 수정 후 통과시킨다.
3. F3의 실제 초기화/비동기 병합을 검증하고 복구한다. F2의 잘못된 요약을
   다시 노출하지 않도록 F2를 먼저 완료한다.
4. F4, F5 각각 실패 재현→수정→회귀 검증을 수행한다.
5. 준비된 환경에서 전체 회귀와 실제 브라우저 검증, diff 검사를 수행한다.
   필요 커밋은 목적별로 나누며, 기존 기기 변경을 섞거나 임의로 삭제하지 않는다.

F1의 일관된 읽기 snapshot·오래된 저장 409 보호, 35회 원본과 검수 이력,
36회 기존 검수, 회차별 media URL, 음원 로컬 경로·해시 검증을 보존한다.
36회 v6 `--apply`, 운영 스키마 변경, DB reset, 원본 배치 수정, 인증/SSH/TLS
변경은 이 후속 버그 수정에 포함하지 않는다. v6의 백업·독립 복원·review
snapshot gate는 기존 문서대로 별도 충족해야 한다.

최종 보고에는 F2–F5별 수정 위치와 이유, 수정 전 실패/수정 후 통과,
새로 실행한 테스트 수·skip 사유, 실제 PostgreSQL/브라우저 검증 여부,
운영 서버 반영 여부, 커밋·push 여부와 미검증 영역을 구분해 적는다.

## 다음 컴퓨터의 Codex에 전달할 요청

```text
현재 저장소의 docs/audit-followup-handoff-2026-10-09.md부터 읽고,
이전 점검의 F2–F5를 순서대로 수정하고 테스트 후 결과를 보고해줘.
먼저 현재 브랜치·기존 변경·환경·F1 회귀 상태를 확인하고,
F2의 감사 scope cache를 고친 다음 F3의 비동기 AI 필터/정렬을 복구해줘.
이어 F4 collector provenance, F5 warning 표시를 수정해줘.
각 문제는 수정 전 실패 재현과 수정 후 통과를 보여줘.
F1 snapshot/409 보호와 기존 승인·원본을 보존하고,
운영 DB에 테스트 쓰기, v6 migration apply, 서버/사용자 탭 강제 재시작은 하지 마.
실제 PostgreSQL/브라우저 검증과 mock 테스트를 구분해서 보고해줘.
```
