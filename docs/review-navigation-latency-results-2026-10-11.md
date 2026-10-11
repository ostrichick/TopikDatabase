# TOPIK 35·36회 검수 문항 이동·승인 지연 개선 실행 보고 (2026-10-11)

## 1. 목적과 적용 범위

기준 커밋 `7bbcecc`, `docs/review-navigation-latency-plan-2026-10-11.md`의 P0–P4. 기존 운영 PostgreSQL의 질문/보기/대본/정답/승인과 독립 AI 감사 근거는 변경하지 않는다. 기기별 PDF·MP3, 공통 35회 대본 쌍·음원 검수 근거·HTTP Basic/Origin/CSRF/409 안전성도 유지한다.

### P0: 운영 DB READ ONLY 기준선

- 기존 서버를 종료/재시작하지 않고 Python API path 계측. 대상 35·36회 각 3개 fast 상세, 70문항 F3 bundle. 개별 fast 상세 **2.4–3.1초**, SQL 7–11회. 각 `ReviewStore` 시작 1.6~1.7초.
- 스크립트 `scripts/benchmark_review_navigation.py`: GET 전용, 35/36회 `fast_detail`, `bundle_70` p50/p95·초기 생성. 본문/자격증명/미디어 경로 미출력. 회귀 `tests/test_benchmark_review_navigation.py`.
- 별도 동일 실행환경 짧은 기준선 2회: fast 상세 35 p50 **2989.8ms**, 36 p50 **2643.8ms**; bundle 35 p50 **2874.2ms**, 36 **2867.4ms**. 표본 n=2, SSH/동시 요청 지터가 있어 확정적인 장기 p95는 아니다.

### P1: 프런트엔드 선행 로딩

- 화면 가시/필터 순서의 다음 3문항을 최대 동시 2 GET으로 미리 가져온다. 기존 70문항 bundle 유지.
- 동일 회차·문항·버전·mutation epoch GET은 합쳐서 중복 수신을 줄인다. 이미 캐시된 문항 이동은 네트워크 대기 없이 렌더, 없는 경우엔 오류 복구 가능한 GET fallback.
- 보통 승인에서 다음 문항 캐시가 비어 있으면 **다음 GET과 승인 POST를 병렬 시작**하고, DB ACK 확인 후에만 최종 승인 상태를 표시한다. 안전한 cached-next 낙관적 이동은 유지.
- 35회 공유 대본 편집, 미저장 음원 구간, 다른 PC 수정(409), 누락/불확실한 ACK, 회차 전환/필터/오래된 번들 응답에서는 과거 초안 보존과 실패 복구를 우선한다. 실패 POST는 자동 재시도하지 않는다.

### P2: 중앙 PostgreSQL 연결·왕복 비용

- `requirements-postgres.txt`에 선택적 `psycopg_pool>=3.2,<4`; 프로젝트별 `.venv` 설치. 시스템 Python에서 모듈이 없으면 기존 비풀 연결로 안전하게 fallback.
- reviewer 전용 최대 4개, 기본 3개의 읽기/쓰기 분리된 풀. 임대·반납에서 트랜잭션 롤백/읽기 전용 상태 분리, SSH/TLS 단절 재연결, 연결 상태 확인. 기존 AI 감사/SQLite 경로는 변경하지 않는다.
- `get_question(fast=True)`의 PostgreSQL 전용 문항/보기/대본/이력/버전 조회를 한 snapshot에서 집계하며 조회 왕복을 줄인다. 기존 공개 JSON 필드 parity, 공유 대본 쌍, 음원 승인 근거, 전체 역사적 검수 이력 및 409를 유지한다.
- `scripts/start_postgres_review.ps1`는 해당 장치 프로젝트 `.venv`가 존재하면 이를 우선 사용하고, 없으면 기존 `py -3`으로 유지한다. PC 외 다른 장치에는 각자 의존성 설치와 서버 재시작 필요.

### P2 초기 측정 결과

| 측정 대상 | 기존 빠른 상세 p50 | 풀/통합 상세 p50 | 변화 |
| --- | ---: | ---: | ---: |
| 35회 | 2938ms | 1234ms | -58% |
| 36회 | 2747ms | 903ms | -67% |

동일 환경 worker read-only 측정에서 70문항 bundle p50도 35회 3112→1927ms, 36회 2910→1827ms였다. 별도 짧은 재측정 `benchmark_review_navigation.py --repeat 3` 결과 35 fast p50 1172.8ms / bundle 1814.5ms, 36 fast 913.8ms / bundle 1464.9ms. 소규모 표본으로 브라우저 클릭→완전 렌더의 p95를 증명하지는 않는다.

### P3: 미디어 검증 결과/적용 판단

- 기존 `sourceWithPage`(35·36 동종 PDF canonical URL), `sourceWithSharedAudio`(원본 음원 1개당 일정 URL), 불필요한 `player.load()` 회피 코드가 이미 존재한다. 같은 파일을 캐시 없이 재읽는 비용의 대부분은 PG 연결/조회 왕복이었으므로 P2 우선.
- 실미디어 `media_path()`(원본 SHA 검증 포함) 35/36회 종목별 조회는 기존 약 1.7~2.1초 → 풀 적용 후 약 0.8~1.2초. 원본 MP3 35회 40.8MB, 36회 77.0MB도 무결성 검증 유지.
- 실제 Edge PDF iframe 네트워크 및 완전한 재렌더 p95를 아직 측정하지 못했다. **PDF/MP3 SHA 검증이나 `no-store` 보안을 근거 없이 제거하지 않는다.** 중복 전송 증거가 없으므로 P3에서 추가 파일 동작 변경은 하지 않는다.

### P4: 격리 PostgreSQL 실제 승인/409 검증

- SSH 운영 `topik`의 새로운 pg_dump **READ ONLY** 후 `topik_perf_20261011_afcef6b2` 폐기 가능한 별도 PostgreSQL 복원(140개 문항)으로만 테스트. 원본 라이브 승인 INSERT/UPDATE **0건**.
- `tests/test_review_latency_clone_e2e.py`는 DB URL이 `topik_perf_`로 시작하고 실제 `current_database()`와 일치하지 않으면 POST를 거부한다. 일반 테스트에서는 자동 skip.
- 36회 `L020` 실제 저장 ACK/검수 버전 +1/이력 추가→같은 구버전 재요청 Conflict(409)→다음 `L021` 상세 GET PASS. 풀 켠 상태 ACK 샘플 **2089.9ms**, 풀 없을 때 2862.0ms (단발·로그잼핑 변동 반영).
- 일회성 Basic/CSRF 보호 HTTP 실제 서버를 임의 localhost port에서 띄워 `L021` 승인 POST 200→`L022` GET→동일 stale POST 409 PASS, 풀 켠 상태 ACK 샘플 **2634.3ms**. 이 기록은 복제 DB에만 추가됐다.
- 35회 25/26·27/28·29/30 공유 대본 동일성 유지. 프런트 Node VM과 사용자 초안/실패/버전 테스트도 병행.
- 별도 두 `ReviewStore`에서 동시에 `036-I-L-023` 같은 버전으로 승인 제출: 정확히 한 요청만 `COMMIT`, 다른 요청은 `Conflict`; 신규 검수 이력 1건, 버전 정확히 +1 확인.
- P4 임시 DB **전용 삭제 완료**: `topik_perf_20261011_afcef6b2`만 `dropdb` 처리 후 운영 DB `topik` 문항 140개 유지 확인. 복제에 사용된 비공개 pg_dump(`/home/ubuntu/.topik_perf_20261011_afcef6b2/live-before.dump`, SHA-256 `18b150a164ebcd71297aa96152cdaa6d57c031bb18162710b59045b2e85738f7`)는 복구 근거로 남겨둔다.

## 2. 최종 통합 검증·운영 활성화

추가 P1 독립 감사에서 오래된 캐시 entry가 낙관적 승인 분기에 들어가면 후속 GET이 정지된 동안 POST ACK 처리도 정지하는 race를 발견했다. 최종 코드에는 **`cachedQuestion(nextId)`의 최신 버전 확인**을 승인 경로 선택에 사용하고 `selectQuestion(nextId)` 처리와 승인 ACK 처리를 분리했다. 중단된 GET·POST 성공/실패/409의 독립적 완료·오류 복구를 별도 Node VM 테스트로 확인한다. 이는 코드 수정 후 독립 리뷰에서 찾은 문제이므로 최초 전체 통과 수치를 고정 최종 결과로 오인하면 안 된다.

**최종 전체 회귀 테스트**: 프로젝트 `.venv`의 Python 3.12에서
`python -B -m unittest discover -s tests -q` **418개 실행 / 실패 0 / 오류 0 / 19 skip**, 약 167.1초.
별도로 Node VM P1 race/prefetch/원본/AI/다회차·미디어 관련 타깃 **32개 PASS**,
읽기 전용 운영 PG 7문항 payload parity PASS,
격리 PG 실제 POST/ACK/HTTP 409 및 2검수자 동시 CAS PASS,
PowerShell launcher 구문 검사 PASS, `git diff --check` PASS.
일부 `skip`은 외부 PostgreSQL 별도 환경을 명시적으로 요구하는 통합 테스트와 의존성 없는
실행 환경 예외로, 전체 suite PASS가 실제 브라우저 클릭 p95 보증을 뜻하지는 않는다.

최종 GitHub 커밋/푸시 식별자는 실제 push 검증 후 추가한다.

**운영 배포 주의:** 사용자 현재 검수 서버(18737/18738)는 실행 중이다. 서버 재시작은 열린 브라우저의 **미저장 입력이나 저장 중 요청을 먼저 확인**해야 하므로 강제 종료하지 않는다. 코드가 Git에 반영돼도 현재 Python 프로세스는 새 연결 풀을 즉시 사용하지 않는다. 다만 HTML은 새로고침하면 최신 프런트가 적용될 수 있다(미저장 초안 보존 후 새로고침).
