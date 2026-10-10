# TOPIK F3 전송 비용·회차별 감사 이력 정렬 최적화 (2026-10-10)

## 우선순위·기준선

기준은 `c7db454`, 단일 clean worktree. 기존 HTTP Basic operator 접근키, `1650fbb` 사람 승인 *선언*의 최신성 계약, 음원 근거·클립 차단·공유 대본 CAS/409·F2~F5·F3 bundle/AI 분리·승인 후 이동을 유지했다. **개인 신원·역할**의 더 강한 인증은 사용자별 계정 정책과 운영 설정 결정을 필요로 하므로 임의로 만들지 않았다. 공용 operator 접근키는 *접근 허용*만 증명하며 기존 `identity_verified=false`와 미인증 `local_reviewer`는 그대로 유지한다. AI 검토·음원 청취/검증·실제 사람 승인도 자동으로 추정하지 않는다.

이번에 독립 수행 가능한 최우선 문제는 **F3의 모든 문항 JSON을 무압축 전송하는 비용**과 **선택한 시험 회차 외의 질문 검수 이력까지 F3 목록의 SQL 윈도우 함수에서 정렬하는 비용**이다. 데이터·권한 상태를 임의로 변경하거나 운영 PostgreSQL을 수정할 필요 없이 읽기/전송 경로만으로 개선할 수 있다.

## 실제 관찰한 원인

원본 35회 동결 SQLite를 `mode=ro, PRAGMA query_only=ON`으로 열고 별도의 폐기 가능한 SQLite 백업 + `127.0.0.1` Basic 인증 HTTP 서버에서 측정했다.

- 70문항 F3 묶음 `/api/questions-bundle`: 무압축 **205,865B**, 선택 화면에서 최초부터 별도 GET이 발생하고 SQLite **9개 SQL**, 로컬 중간값 약 18–30ms. 상세 텍스트·보기·이력·출처/AI 보조 필드를 70문항 일괄 전송해 이동 중 개별 GET을 줄이는 설계다. 데이터 압축 없이 송신돼 네트워크 전송 비용이 컸다.
- F3 빠른 목록 `/api/questions-fast`: 무압축 약 **21,931B**, SQL 1개, 원본 실측 약 2–4ms. `ranked_reviews`가 `WHERE subject_type='question'`인 **전체 회차**의 `review_records`를 대상으로 `ROW_NUMBER`, `COUNT(*) OVER`와 정렬을 수행하고, 결과의 질문/회차는 **밖에서만** 필터링했다. 다른 회차 이력이 커질수록 35회만 보는 사용자도 불필요한 윈도우 정렬을 떠안는다.
- 별도 실험에서 **원본이 아닌 격리 사본**에 다른 36회 ID의 **합성** review record 20,000개를 추가했다. 실제 35회 표시·근거 JSON은 그대로인데 변경 전 35회 F3 목록 시간은 **2.7–3.7ms → 62–66ms**로 증가했다(동일 로컬 환경에서 각각 소수 회 실행). 이는 운영 PostgreSQL의 36회 실제 규모·성능을 뜻하지 않는다.
- 초기 화면은 F3 fast + F3 bundle + AI 요약 + 독립 감사 및 상황에 따른 첫 문항 detail을 병렬 요청하는 기존 계약을 사용한다. 묶음 도착 전에 처음 문항을 열면 개별 detail GET이 추가될 수 있지만, 이 요청을 억지로 대기시키면 느린 PostgreSQL에서 첫 화면이 늦어질 수 있어 기존 non-blocking fallback을 유지했다.

## 수정: 전송 협상 + 시험 회차 범위 제한

1. `src/review_ui.py`의 HTTP `_json` 응답에서 요청의 `Accept-Encoding`에 지원 가능한 `gzip`이 있고 JSON 본문이 **2,048바이트 이상**이면 gzip level 5로 압축한다. 압축 후 충분한 절감이 있을 때만 `Content-Encoding: gzip`, 모든 JSON 응답에는 `Vary: Accept-Encoding`을 적용하고 **실제 바이트 길이**를 `Content-Length`에 기록한다. `gzip;q=0`, 잘못된 q 값, gzip 미지원 클라이언트, 짧거나 압축 효과가 없는 응답은 기존 plain JSON으로 전송한다. 일반 브라우저 `fetch()`는 자동 복호화하며 앱 JS/JSON 스키마는 변경하지 않는다.
2. `list_questions_fast()`의 `ranked_reviews` 입력을 `subject_id IN (SELECT q2.id FROM questions q2 JOIN sections s2 ... WHERE s2.exam_id=?)`로 제한했다. `COUNT(*) OVER`, 최신 수동 선언 판정, 순서·버전·35/36 격리와 `review_records` 자체는 변경하지 않는다. F3 묶음의 기록 조회는 이미 선택한 시험 회차로 제한하므로 추가 SQL을 만들지 않는다.
3. 저장 후 F3 목록 갱신, 409 CAS, 공유 대본/음원 원자성, 미저장 초안·비동기 버전 보호, AI 집계와 원본 PDF/MP3/image/clip은 변경하지 않았다. 비인가 401/권한 403, 인증된 POST 200/409의 JSON도 동일한 의미를 유지하며 압축 여부와 독립적이다.

## 수정 전후 실제 HTTP 전송량

**단위: 서버 `Content-Length`, 실제 35회 데이터.** 요청은 동일한 `Accept-Encoding: gzip`, 독립 TCP localhost 경로. `c7db454` 서버와 수정 서버를 **동일한 격리 SQLite 사본**에서 차례대로 실행하고 응답 본문을 gzip 복호화하여 동일 데이터인지 확인했다.

| 응답 | 전 (HTTP 무압축) | 후 (gzip 전송) | 절감 |
|---|---:|---:|---:|
| `/api/questions-fast` | 21,931B | 1,111B | 94.9% |
| `/api/questions-bundle` | 205,865B | 18,312B | 91.1% |
| `/api/independent-audits` | 23,270B | 1,727B | 92.6% |
| 첫 문항 `/api/questions/...?...fast=1` | 3,069B | 1,081B | 64.8% |

`Accept-Encoding: identity` 경로는 byte-for-byte 원래 JSON이 유지된다. AI 요약의 빈 응답 88B는 gzip을 사용하지 않는다. 이전/이후 HTTP 10회씩(첫 회 제외 9회) 로컬 F3 bundle 중앙값은 무압축 baseline **18.528ms**, 수정 후 `gzip` **19.220ms**로 관찰됐다. list baseline **2.729ms**, 수정 `gzip` **3.479ms**. 해당 측정은 서버·DB·네트워크 지터가 큰 짧은 localhost 표본이므로 **지속적인 실제 사용자 지연 개선 또는 저하를 증명하지 않는다**. 전달되는 바이트 절감과 데이터 정합성만 직접 입증했다. 재현 계측 결과 `.stage9-runtime/f3_gzip_http_benchmark.json` (Git 제외).

## 스케일 / 회귀 검증

- 격리 SQLite에 합성 다른 회차 이력 20,000개를 넣은 동일 실험에서 수정 후 35회 F3 목록은 원래와 같은 상태·버전·사람 승인 선언 근거를 반환했다. 독립 감사의 반복 측정 3회는 **9.70/7.20/32.04ms**(수정 전 62.14/62.04/66.38ms); 원본(합성 추가 전) 3.47/3.18/3.28ms. 원본 리뷰 테이블에는 해당 필터를 사용할 복합 인덱스가 없으므로 부가적인 **전체 이력 스캔 비용**은 남으며, 운영 PostgreSQL 인덱스 변경·마이그레이션은 금지 범위라 임의 실행하지 않았다.
- 신규 `tests/test_review_f3_transport_gzip.py`: Basic auth 보호, gzip 협상·Content-Length·Vary, 70문항 status/history/evidence JSON 의미 동일, AI 작은 응답 무압축, PDF·MP3 Range 206·미인증 401, 격리 저장 성공 200 / stale 409, 누락/손상 협상 및 **합성 다른 회차 20,000 이력 차단** 검증.
- 실제 **일회용 PostgreSQL 17.11**에서 신규 `test_55_f3_fast_list_and_bundle_match_on_real_postgres_snapshot`을 통해 수정 SQL 구문과 실제 70문항 F3 목록·묶음의 저장 상태/버전/수동 검수 근거 일치를 확인했다. 기존 35/36 격리, 공유 대본/음원 동시성·CAS·rollback 스냅샷도 보존했다.
- Playwright/Chromium 실제 화면 320/390/1366px, 단일 브라우저 프로세스 및 Basic 인증 유지. 브라우저의 자동 gzip 해제 뒤 문항 상세·상태·AI/원본 탐색, PDF/MP3 Range 및 PNG 원본 접근, keyboard/focus, 16px 본문, 가로 넘침·JS 오류·GET 수 확인. **테스트 브라우저는 읽기 전용**이고 실제 사람의 검수·승인을 자동으로 수행하지 않는다.
- **최종 실측:** 한 Chromium 프로세스에서 320/390/1366px 각각 **PASS**, F3 70문항과 근거상 현재 승인 선언 **2**, 미확정 **0**(원본 35회 데이터), 본문·보기·메모 **16px**, 가로 넘침 없음, JavaScript 예외/콘솔 오류 **0건**. 초기 1번 문항 표시 **212 / 172 / 238ms**(단일 로컬 실행, 지터 있음). 인증된 PDF·MP3 Range `206`, 그림 PNG `200`, 무인증 API/파일 `401`, 전체 세 뷰포트·반복 인증 확인을 합쳐 **GET 61 / POST 0**, `200` 33회·`206` 9회·`401` 19회. Chrome 자동 gzip 복호화 후 F3 **70개 ID·버전·승인 근거 완전 일치**, `Vary: Accept-Encoding` 유지. 프런트 GET 라우팅/캐시 로직은 수정하지 않았으므로 첫 진입 요청 개수를 임의로 늘리지 않았다. 브라우저 스모크/서버 응답 측정은 `.stage9-runtime/f3_gzip_browser_metrics_20261010.json` (Git 제외).
- 검증 중 실제 Chromium이 화면 전환으로 PNG 응답을 중간에 취소하면, 기존 서버가 이미 보낸 `200` 헤더 뒤에 `500` 오류를 다시 쓰려다 소켓 예외를 기록하는 **관련 결함**도 발견했다. `do_GET`에서 클라이언트 연결 종료 (`BrokenPipeError`, `ConnectionResetError`, `ConnectionAbortedError`)를 별도로 처리해 잘못된 두 번째 응답을 시도하지 않도록 수정하고 격리 unit fixture를 추가했다. 수정 전 스모크에서 수집된 가짜 `500` 1건과 Python traceback은 최종 재검증에서 **0건**; 정상 `200` 이미지·클립/PDF Range 경로, 숨겨진 검수 저장에는 영향이 없다.
- **최종 전체 회귀:** `.venv/Scripts/python.exe -m unittest discover -s tests -q` 결과 **385 tests, OK (skipped=15), 180.407초, exit=0**. 15건의 외부/명시적 격리 PG URL 필수 테스트는 일반 discover에서 생략됐으며 PG17은 **별도 새 클러스터에서 12/12 PASS**, `STOPPED=True`, `REMOVED=True`로 확인했다. 후속 이미지 중단 방어까지 포함한 집중 gzip/Basic/409 테스트 **16/16 PASS**. 원본 35회 SQLite SHA-256 `076826708a93718faa28c3c1ae3e87bbf28aac4bcfaed88359fd2a12b1c0b7e6`, 35회 MP3 `314af506159e24b7bd4b20e95d248683673e799e8e8af5ac4b77946879e4dae2` 모두 불변. 검수자의 사람 청취·판단·승인, 운영 PostgreSQL 쓰기, 운영 서버 재시작, 36회 마이그레이션·push·배포는 일절 수행하지 않았다.

## 여전히 필요한 사람/운영 결정

- 공용 Basic 키는 사용자별 신원·권한이 아니므로 개별 검수자 계정, 다중 장치 역할, 감사 주체 전환 및 이력 보존 정책은 별도로 사용자 결정을 받아야 한다. 과거 `local_reviewer` 값을 독립적으로 인증된 이름으로 소급 변경하지 않는다.
- 다른 회차의 이력이 매우 많아지면 데이터베이스의 index 설계 및 운영 PostgreSQL 실행계획을 **읽기 전용 EXPLAIN**으로 확인한 뒤 별도 마이그레이션 승인 필요성을 판단해야 한다. 지금은 스키마/인덱스 변경 없이 선택 회차만 윈도우 정렬에 들어가도록 제한했다.
- 현재 초기 bundle은 여러 문항을 순회하는 검수 속도를 위해 그대로 병렬 시작하며, 사용자 행태와 PostgreSQL 실제 지연을 계측하기 전에는 임의로 lazy-loading으로 바꾸지 않는다. 다음 사용자 가치 개선 후보는 **F3 첫 목록·bundle/detail 중복 GET과 대규모 감사 기록 증가에 따른 index/캐시 비용**, 또는 **개인 신원·역할 승인 정책 확보 뒤 정식 권한 모델**이다.
