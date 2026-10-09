# 검수 상세 조회 snapshot 수정 결과 — 2026-10-09

## 문제와 수정

35회 PostgreSQL 상세 조회는 본문을 읽은 뒤 다른 검수자가 수정·커밋하면,
이전 본문과 새 선택지·이력·검수 버전이 섞인 응답을 반환할 수 있었다.
이 응답을 저장하면 최신 버전 검사를 통과하면서 먼저 저장된 수정 일부를 되돌렸다.

`src/review_ui.py`의 `get_question()`에서 PostgreSQL REPEATABLE READ 조건의
36회 제한을 제거했다. 모든 회차의 상세 조회가 첫 데이터 SELECT 전에
`SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY`를 실행한다.
본문·선택지·대본·검수 상태·이력·버전은 하나의 트랜잭션 snapshot에서 읽힌다.
기존 저장의 행 잠금·버전 검사 및 SQLite BEGIN 계약은 유지했다.
공용 연결 기본값이나 DB 스키마를 변경하지 않았다.

## 수정 전 실패 재현

기존 테스트 double과 달리 snapshot 명령을 실제 SQLite WAL 트랜잭션으로
매핑하는 독립 fixture를 추가했다. 이어 프로젝트 로컬 PostgreSQL 17.11
실행 파일로 loopback 전용 임시 클러스터와 테스트 DB를 생성했다.
운영 URL이나 운영 DB를 테스트 대상으로 사용하지 않았다.

두 환경 모두 수정 전 같은 실패를 재현했다.

- 35회 상세 조회의 16개 경합 subcase에서 이전 본문과 새 선택지/버전이 섞였다.
- 실제 HTTP 검수 endpoint에 오래된 응답을 제출하자 예상한 409 대신 200으로 저장됐다.
- 각각 4개 테스트 메서드 실행 중 실패 17개(16개 subcase + HTTP 1개), 실행 오류 0개였다.
- 같은 회차·영역 조건을 기존 native SQLite 경로로 실행하면 통과했다.

재현은 별도 연결의 실제 `save_review()`가 읽기 SQL 사이에 커밋하도록
동기적으로 제어했다. 임의의 sleep이나 경쟁 타이밍 운에 의존하지 않는다.
독립 PostgreSQL에서도 실제 연결·트랜잭션·행 잠금·HTTP 요청을 사용했다.

## 회귀 테스트 범위

추가 파일: `tests/test_review_snapshot.py`.
기존 `tests/test_review_ui_postgres_clip.py`의 SQLite 기반 PostgreSQL 모형도
새 snapshot SQL을 SQLite BEGIN으로 처리하도록 보강했다. 최초 전체 회귀에서
이 모형이 해당 SQL을 지원하지 않아 음원 테스트 5개가 오류를 냈으며,
보강 후 음원 모듈은 12개 중 11개 통과·기존 권한 제한 skip 1개로 확인했다.

| 축 | 검증 조건 |
| --- | --- |
| backend | PostgreSQL 분기를 모사한 SQLite WAL, native SQLite, 독립 PostgreSQL 17.11 |
| 회차 | 35회, 36회 |
| 영역 | 듣기, 읽기 |
| 조회 | 일반 상세, fast 상세, bundle |
| 기존 검수 이력 | 0, 29, 30, 31건; LIMIT 30 전후 버전 계산 경로 |
| source revision | 36회 구두점 revision을 포함한 버전 유지 |
| 충돌 | 오래된 저장은 Conflict/HTTP 409, 추가 이력·내용 변경 없음 |
| 정상 저장 | 최신 데이터 재조회 후 저장 성공, 버전 1 증가 |
| 보존 | 먼저 저장된 본문·선택지·대본·검수 상태·이력 및 raw 원문 |
| 자원 | 성공·예외 모두 읽기 연결 종료, 임시 HTTP 서버 종료 |

상세 경합 matrix는 backend별 32개 subcase이며, bundle은 backend별 4개
subcase다. 이 subcase 수와 unittest의 테스트 메서드 수는 구분한다.

테스트 PostgreSQL 연결은 별도 `TOPIK_SNAPSHOT_TEST_DATABASE_URL`이 명시된
경우에만 사용한다. loopback `127.0.0.1` 및 DB 이름
`topik_snapshot_test_<영숫자 suffix>`를 강제하며, host/dbname/service query
override도 차단한다. 각 fixture는 UUID schema를 만들고 자신이 만든 schema만
정리한다. 이 URL이 없으면 live PostgreSQL 테스트 5개는 skip된다.
따라서 기본 테스트만 통과한 결과를 실제 PostgreSQL 통과로 해석해서는 안 된다.

## 검증 결과

- 수정 후 새 focused 테스트: **16개 통과, 실패 0, 오류 0, skip 0**.
- 이 16개에는 독립 PostgreSQL 테스트 5개가 포함된다.
- 실제 테스트 서버의 기본 격리 수준은 `read committed`였다. snapshot
  테스트 종료 후 남은 임시 schema 0개·다른 테스트 DB 연결 0개를 확인했다.
- 최종 전체 회귀: **280개 실행, 279개 통과, 실패 0, 오류 0, skip 1**,
  135.199초. 이 실행에는 live PostgreSQL 테스트 5개도 포함된다.
- skip은 기존 음원 디렉터리 symlink 테스트 1개이며 Windows 생성 권한 부족
  (`WinError 1314`) 때문이다. snapshot 테스트의 skip은 없다.
- `pip check`: `No broken requirements found`.
- Stage 10 보관본 재검증: `status=ok`, 원본 4개·archive copy 4개,
  `sqlite_operational_writes=false`.
- tracked diff 검사와 변경/추가 파일의 trailing whitespace 검사 통과.
- 최종 회귀 후 남은 테스트 schema 0개·다른 테스트 DB 연결 0개를 재확인했다.
  임시 PostgreSQL은 정상 종료(exit 0)됐으며, 포트 listener와 postmaster PID 파일이
  없는 것을 확인했다. 로그와 중지된 테스트 클러스터는 Git-ignored 런타임에 보존했다.

이번 실행 증거는 다음 로컬 폴더에 있다. Git에 올리는 운영 자료는 아니다.

```text
.stage9-runtime/snapshot-test-27669dab4cd447fb857e1f402d965b56/
  before-fix.txt                 # 실제 PostgreSQL 수정 전 17개 실패
  after-fix-focused.txt          # 새 focused 테스트 16개 통과
  full-regression-final.txt      # 최종 전체 회귀 280개 결과
  full-regression-summary.json   # 전체 회귀 구조화 결과
  cleanup-summary.json           # schema/connection/서버 종료 확인
```

기본 독립 fixture 테스트 재실행:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_review_snapshot -v
```

실제 PostgreSQL도 재실행하려면 준비된 폐기 가능한 로컬 테스트 DB URL을
`TOPIK_SNAPSHOT_TEST_DATABASE_URL`에 설정한 뒤 같은 명령을 실행한다.
운영 `TOPIK_DATABASE_URL`은 이 테스트의 live 대상 선택에 사용하지 않는다.

## 운영 반영 및 작업 경계

- 이번 변경은 Python 상세 조회 코드와 테스트·문서에 한정된다.
- 운영 PostgreSQL 데이터·스키마, 원본 PDF/MP3, SQLite 보관본을 수정하지 않았다.
- 검수 UI의 기존 사용자 탭과 서버를 재시작하거나 새로고침하지 않았다.
  실행 중인 기존 Python 서버는 재시작 전까지 이전 상세 조회 코드를 사용할 수 있다.
- 운영 반영 시 진행 중 저장과 열린 검수 초안을 정리한 뒤 기존 launcher로
  검수 서버를 재시작하고 35/36회 읽기 API를 확인한다. 운영 프로세스 반영은
  이 테스트 결과만으로 완료됐다고 주장하지 않는다.
- 브라우저 전체 E2E, 원본 기출 내용의 추가 사람 검수, 36회 v6 마이그레이션,
  나머지 F2–F5 수정은 수행하지 않았다.
- 이 수정·검증을 마친 시점에는 Git 커밋·push를 수행하지 않았다.
  이후 사용자 요청에 따른 코드 이동과 커밋 구성은
  `docs/audit-followup-handoff-2026-10-09.md`에 별도로 기록한다.
