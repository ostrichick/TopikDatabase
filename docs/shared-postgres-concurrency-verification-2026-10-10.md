# 공유 대본·음원 실제 PostgreSQL 17 동시성 검증 및 격리 실행 복구 (2026-10-10)

## 문제·범위

이전 `8ebf867` 개선에서는 35회 25/26·27/28·29/30의 공유 대본 공동 수정·개별 승인·버전 검사·409 충돌 복구를 SQLite 격리 사본과 가짜 PostgreSQL 잠금 SQL로 검증했다. **실제 PostgreSQL 두 세션의 락 경합·원자적 롤백·동시 음원 변경·F3 스냅샷 격리**는 확인하지 못했고, Windows `initdb.exe`가 일부 Python 하위 프로세스 실행에서 `Permission denied`로 실패했다.

이번 작업은 해당 **최우선 미검증 항목의 실제 실행**이다. 시작 `HEAD=8ebf867`, clean, 원격 대비 ahead 13, 단일 워크트리. 기존 F2~F5, F3 bulk/single-flight, 원본 탐색, AI 판단 패널, `8ebf867` 공동 대본 UI/저장 계약은 완료된 기준선으로 보존했다. **실제 PostgreSQL 버그 증거 없이 리뷰 서버/생산 저장 로직을 재작성하지 않았다.**

## 임시 클러스터 복구와 안전 경계

- `PostgreSQL 17.11` 설치 바이너리 `.stage9-runtime/PostgreSQL/17.11/pgsql/bin`을 활용. 최초 Python `subprocess` 내부의 `initdb`는 새 디렉터리 생성 시 `Permission denied`를 재현했으나, 동일 설치 바이너리를 Windows **PowerShell에서 직접 실행**하면 `initdb`·초기 부트스트랩·sync가 완료됐다. NTFS ACL에는 사용자 Modify/FullControl 권한이 있었고 Defender Controlled Folder Access는 꺼져 있었다. 이 차이는 **실행 방식별 관찰 사실**이며 구체적인 Windows 정책 차단 원인은 아직 확정하지 않았다. 기존 보안 정책을 전역으로 비활성화하거나 관리자 권한을 변경하지 않았다.
- 신규 `scripts/verify_shared_pg_concurrency.ps1`: 매 실행마다 새 UUID 디렉터리 `shared-pg-*`, 새 loopback 포트(50000–59499), 새 nonce 기반 DB `topik_shared_pg_test_<20자리 hex>`를 생성한다. PostgreSQL은 오직 `127.0.0.1`에만 바인딩한다. 검사·성공 여부와 무관하게 **정확히 그 실행에서 만든 서버만** `pg_ctl stop`하고 새 디렉터리를 삭제한다. 서버 종료를 확인하지 못하면 강제 삭제하지 않는다. PostgreSQL 바이너리/기존 클러스터·운영 DB·SSH 포워딩은 변경하지 않는다.
- Python 테스트는 `TOPIK_SHARED_PG_TEST_URL`과 `TOPIK_SHARED_PG_TEST_MARKER`를 **명시적으로 모두** 받아야 실제 DB에 접근한다. 읽기 전용 사전검사에서 127.0.0.1, 49152 이상 포트, 정확한 nonce DB 이름, PostgreSQL 버전 17, `COMMENT ON DATABASE = TOPIK_SHARED_PG_TEST::<nonce>`, `public` 스키마의 기존 객체가 없음을 확인한 경우에만 35회 SQLite 마이그레이션을 시작한다. 하나라도 다르면 **쓰기 없이 실패**한다. 이미 마이그레이션된 DB를 다시 재설정·재사용하지 않는다. 테스트 시 운영용 `TOPIK_DATABASE_URL`은 별도 테스트 URL로만 대체한다.
- `topik-past-papers/derived/035-I-B.sqlite`는 원본 SHA-256 `076826708a93718faa28c3c1ae3e87bbf28aac4bcfaed88359fd2a12b1c0b7e6`를 검사하며 **읽기 전용**으로 PostgreSQL에 복제한다. 35회 원본 PDF/MP3와 서버 설정·실제 운영 PostgreSQL은 수정하지 않았다. 실험 대상에는 오직 frozen 역사적 35회 자료 70문항이 포함되며, 현재 운영 35회 승인 상태(앞선 readonly 확인 시 공유 6문항 모두 verified)와 구별한다.

### 반복 실행 명령

Windows PowerShell 5.1, `C:\Projects\TopikDatabase`에서:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned -Force
& .\scripts\verify_shared_pg_concurrency.ps1
```

이 설정은 **현재 PowerShell 프로세스에만 적용**, 사용자·컴퓨터 정책을 영구 변경하지 않는다. 조직의 강제 실행 정책이 있으면 정책을 우회하지 않고 허가된 PowerShell 실행 경로를 사용해야 한다. 새 테스트 클러스터가 아닌 외부/운영 DB에서 명령을 직접 실행하지 않는다.

## 실제 PostgreSQL 17.11 통합 테스트 결과

`tests/test_shared_transcript_postgres_concurrency.py`: 테스트 자체는 실제 DB 접근 전에 안전 검증 3개를 포함하고, 그 뒤 새 DB에서만 35회 아카이브를 마이그레이션한다. PostgreSQL 연결에는 연결·statement·lock timeout을 둔다.

| 케이스 | 실제 PG 결과 |
| --- | --- |
| 25→26과 26→25 역방향 동시 대본 정정 | 두 세션 중 **정확히 하나만 커밋**, 나머지는 `Conflict`/409. 데드락·부분 승인은 없음 |
| 같은 상황의 localhost HTTP 동시 POST | **200 한 건 + 409 한 건**, 질문별 검수 이력 중복 증가 없음 |
| 27·28번 공유 대본 정정과 audio candidate 경계 저장 동시 수행 | **둘 다 성공**, 동일한 정렬 잠금 순서로 데드락 없음. 양쪽 구간 경계·version 일치 |
| 29·30번 대본·짝 이력 기록 후 의도적 예외 주입 | **원자적 롤백**: 두 문항 텍스트·상태·선택지·audio·이력·버전 모두 이전과 동일 |
| F3 문항 상세 `REPEATABLE READ` 도중 다른 세션 커밋 | 읽기 응답은 이전 일관된 스냅샷, 최신 조회는 새로운 버전; 오래된 저장은 충돌 |
| F3 70문항 bundle 조회 중 다른 세션 커밋 | 단일 버전의 본문/선택지/승인/이력 묶음 응답 보장 |
| 35/36 데이터 격리 | 35회 70문항만 존재, 36회 접근·관계 추론 불가 |
| 오접속/재사용 테스트 | 원격 주소·낮은 포트·잘못된 marker/comment/버전/schema·기존 DB 대상 변경을 사전에 차단 |

실제 신규 격리 DB 세 번째 시도에서 **9/9 PASS, 4.321초**. 앞선 두 테스트 전용 DB에서는 테스트 하네스 자체의 URL 공백 인코딩(`libpq options`)과 고장 주입 위치(`review_records` scope 파라미터 인덱스) 오류가 발견돼 수정했으며, 이미 마이그레이션된 DB 재사용 없이 새 nonce DB로 검증했다. 이는 애플리케이션 버그가 아니라 **테스트 하네스 결함**이었다.

추가로 **자체 수명 관리 PowerShell 러너**가 새로운 cluster+DB를 생성해 같은 테스트를 반복 실행했고 종료 후 서버·데이터 디렉터리가 모두 정리됐다. 두 번의 성공적인 반복 실행 중 마지막 결과: **9/9 PASS, 4.069초, 전체 준비→테스트→정리 17.692초**. `.stage9-runtime/shared_pg_isolated_run.json`, `.stage9-runtime/shared_pg_isolated_test.log`에 결과가 남아 있다. `pg_ctl` 출력을 PowerShell 리디렉션하면 백그라운드 서버의 상속된 파이프 때문에 호출자 대기가 생길 수 있어 **직접 호출**하도록 수정했으며, Windows PowerShell 5.1의 `unittest -v` stderr를 실패로 오인하는 부분도 처리했다.

## 실제 PostgreSQL을 사용한 Chromium 검수 동선

기설치 Playwright-core + Chromium headless shell을 이용해 **실제 PostgreSQL로 연결된 로컬 검수 서버**에서 다음을 수행했다: 원본 대본 PDF 열기 → 짝 문항으로 키보드 이동 → 현재 문항으로 돌아오기 → 텍스트 수정 및 근거 메모 → 짝 문항 확인·재검수 동의 → 한 번만 POST → 짝 대본 일치·재검수 상태 확인. 원본 PDF 페이지는 세 쌍 각각 10·11·12쪽, 음원 상태는 여전히 candidate다.

| 화면 | 쌍 | 저장 클릭~짝 문항 진입 | 전체 관측 GET/POST | 결과 |
| --- | --- | ---: | --- | --- |
| 320px | 25/26 | 217ms | GET 9, POST 1 | 공유 대본 정합, 짝 미검수, 초점·16px·가로 overflow 0·JS 오류 0 |
| 390px | 27/28 | 214ms | GET 8, POST 1 | 동일 |
| 1366px | 29/30 | 238ms | GET 8, POST 1 | 동일 |

각 화면의 전체 실험은 각각 914/706/669ms의 단일 샘플이다. 이 값은 **사용자 실제 검수 시간, 이전 SQLite 테스트 결과 또는 운영 PG 성능과 직접 비교할 수 없다.** 대본과 승인 경계의 정합성은 실제 PG에서 확인했으나 음원 후보 구간의 **실제 청각적 정확도는 아직 사람에 의해 검증되지 않았다.** PNG와 상세 JSON: `.stage9-runtime/shared_pg_chromium_{320,390,1366}.png`, `.stage9-runtime/shared_pg_chromium_metrics.json` (Git 제외).

## 변경·검증·정리

- 신규 `tests/test_shared_transcript_postgres_concurrency.py`: 안전 대상 계약 + 실제 PG17 두 세션 동시성/HTTP CAS/롤백/읽기 일관성/36회 격리.
- 신규 `scripts/verify_shared_pg_concurrency.ps1`: 매번 독립 클러스터와 nonce DB를 생성·검증 후 안전하게 정리하는 반복 실행 진입점. 실패한 Python 기반 `initdb` 진입점은 재사용하지 않고 삭제했다.
- `.stage9-runtime`의 일시적인 Chromium/Python 서버 러너는 실행 후 삭제했으며, 테스트 로그·스크린샷·JSON만 남긴다. 본 작업에 운영 DB 쓰기·36회 마이그레이션·원본 PDF/MP3 수정·원격 push·운영 배포·기존 서버 재시작은 없다.
- 최종 집중 `python -m unittest tests.test_shared_transcript_postgres_concurrency.TestTargetSafety tests.test_review_shared_transcript_integrity tests.test_review_ui_flow -q` **30개 PASS**, Python `py_compile` 및 PowerShell 구문 파서 PASS. 마지막 전체 `python -m unittest discover -s tests -q`는 **346개, OK, skipped=11, 337.121초, 종료 코드 0**. `skipped=11`에는 명시적 테스트 DB 환경이 없어 자동 생략한 PG 통합 6개가 포함되며, 해당 6개는 **실제 새 격리 PG17을 사용해 별도 9/9 PASS**했다. 이번 전체 실행 시간 증가를 UI/F3 속도 퇴행의 증거로 단정하지 않으며, 새 테스트 환경·일시적 시스템 부하를 구분한다. `git diff --check` 통과.

## 장기 목표·다음 우선순위

1. **UI/UX·가독성:** 실제 PG에서도 320/390/1366px 공동 대본 승인·재검수·원본 탐색·키보드 동선 이상 없음. 불필요한 UI 재구현은 하지 않았다.
2. **검수 의사결정 정보:** 같은 원본 대본이라도 선택 문항 승인과 짝 문항 재검수는 서로 다르며, 실제 동시성 시점에도 그 구분을 유지한다.
3. **구조·프로세스 간소화:** 외부 테스트 DB 의존성과 Python initdb 오류를 없애고, 항상 새 DB를 만드는 단일 PowerShell 검증 진입점과 자동 정리를 제공한다. 실패한 중간 DB를 초기화하거나 기존 워크트리를 늘리지 않았다.

**다음 최우선 사용자 가치 과제:** 운영 자료에서 확인된 공유 오디오 구간 `candidate` 상태 25/26·27/28·29/30의 원음·대본·원본 PDF를 실제로 청취하며 검증할 수 있는 근거/상태/승인 흐름. 현재 수치나 해시만으로 오디오 경계를 정확하다고 단정하지 말 것. 먼저 읽기 전용·격리 검수와 사람 확인 근거가 필요하다.
