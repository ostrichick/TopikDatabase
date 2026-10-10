# 로컬 검수 서버 접근 보호: 실행별 operator 비밀번호 (2026-10-10)

## 원인과 적용 범위

기준 `1650fbb`의 `make_handler()`는 `127.0.0.1` Host와 POST Origin·CSRF를 검사했지만 **GET `/api/questions-fast`에서 동일한 POST용 CSRF 비밀값을 인증 없이 제공**했다. 따라서 다른 동일 PC 프로세스가 GET으로 토큰을 받은 후 자신이 작성한 Host/Origin 헤더로 검수 POST를 호출할 수 있었다. 웹 브라우저 교차 출처 공격이 입증된 것은 아니며, 원격 외부 공격/운영 PostgreSQL 침해가 확인됐다는 의미도 아니다. 로컬 포트 접근 권한과 특정 사람의 검수자 신원 인증은 별개의 문제다.

## 수정 후 사용자 동선

`scripts/start_postgres_review.ps1` 또는 `py -3 src/review_ui.py --port 0`으로 로컬 서버를 **향후 새로 실행할 때** 터미널은 정확한 `http://127.0.0.1:<port>/` 주소와 아래 내용을 출력한다.

```
Operator access: username operator
Operator access: password <random-per-launch-secret>
```

비밀번호 값은 `secrets.token_urlsafe(32)`로 각 실행마다 새로 생성되며 Git/DB/브라우저 URL에 저장되지 않는다. 브라우저에서 주소를 열면 HTTP Basic 인증창에 사용자 이름 **operator**, 터미널의 비밀번호를 입력한다. 인증 성공 시 기존 HTML, F3 fast-list, 묶음, 상세, AI 감사, PDF, 원본 MP3/이미지/클립, GET·POST 흐름이 그대로 작동한다. 실행 중 같은 브라우저 세션의 미디어 요청에는 표준 브라우저 HTTP 인증 캐시를 사용한다. **서버 재시작 후 새 비밀번호로 다시 인증**해야 한다. 비밀번호를 URL에 넣거나 화면 캡처/공유/원격 로그·Git에 기록하지 않는다.

`make_handler(store, *, access_key=...)`는 **접근키 지정이 필수**이므로 빠뜨리면 예외가 발생한다. 실제 CLI는 비밀번호를 명시적으로 생성·전달해 사람에게 보여 준다. 로컬 관리자 전용 `scripts/stage9_physical_check.py`도 디스포저블 검증 서버에 별도 임시 비밀번호를 전달하고 Authorization 헤더를 붙인다. 오래된 오프라인 통합 테스트 중 이미 격리된 서버만 `access_key=None, allow_unauthenticated_test_fixture=True`를 **동시에 명시적으로** 사용해 기존 테스트 흐름을 보존한다. 이 우회는 운영 실행 경로에서 사용하지 않으며, 운영에 사용해서는 안 된다.

## 검증 모델과 제한

- 모든 서버 요청은 기존 loopback Host 확인 후 별도의 `Authorization: Basic ...` 값을 일정시간 비교 함수로 검증한다. 키가 없거나 다른 키이면 **HTTP 401 + `WWW-Authenticate`**, 요청마다 인증 재확인. 실패한 요청의 본문/CSRF 토큰/메타데이터/PDF/MP3는 제공되지 않고 저장 API도 실행되지 않는다.
- 올바른 접근키여도 POST는 기존 **Origin, `X-Review-Token`, Content-Type, optimistic CAS/409, 원자적 공유 대본·음원 계약**을 별도로 요구한다. 키 하나로 POST의 다른 서버 검사들을 우회할 수 없다.
- HTTP Basic은 이 장치의 **실행 시 제공된 비밀번호 소지 여부만** 확인한다. 임시 키가 여러 사람에게 공유되면 **사람을 개별 식별할 수 없다.** `review_records.reviewer='local_reviewer'`, `human_review_evidence.identity_verified=false`, AI 근거 및 사람 청취 선언 표시는 그대로 유지한다. 신원·역할을 추측/승격하거나 과거 감사 로그를 인증된 신원으로 소급 변경하지 않는다.
- 루프백 HTTP는 TLS를 제공하지 않고 브라우저는 동일 호스트 내에 Basic 자격증명을 캐싱할 수 있다. 악성 확장 프로그램, 동일 OS 사용자의 권한 탈취, 관리자/프로세스 메모리 접근 및 실행 터미널/화면 비밀번호 유출은 이 설계의 보호 범위가 아니다. 개인정보·다중 계정·LAN 공개 서버용 신원 인증으로 쓰면 안 된다. 운영 접속 경계를 더 확대하기 전에 실제 사용자 인증 및 역할 모델의 별도 설계/승인이 필요하다.
- 원본 SQLite/PDF/MP3, 중앙 운영 PostgreSQL, Git 원격, 서버 서비스는 이 작업으로 변경·재시작하지 않는다. 모든 승인·동시성 쓰기는 **격리 복제 DB**로만 실행한다. 테스트 fixture의 합성된 `verified` 상태/감사 기록은 실제 사람 검수 사실을 나타내지 않는다.

## 검증 명령 / 보고

`python -m unittest tests.test_review_http_operator_auth -v`는 새로운 엄격한 접근키 경계를 **별도 복제 SQLite 및 localhost 포트**에서 검증한다. 기존 HTTP/UI/AI/공유 구간/35·36 격리 회귀와 `python -m unittest discover -s tests -q`를 실행해 이전 사용 동선을 유지한다. 이미 설치된 Chromium을 320/390/1366px에서 단일 브라우저 프로세스로 사용해 인증 성공·실패, 표시, PDF·오디오 및 가로 넘침/JS 오류를 확인한다. 실제 PostgreSQL 17.11은 새 격리 클러스터에서만 CAS·롤백·동시성 회귀를 실행하고 자동 종료·제거한다.

**재현 가능한 실행 결과 (2026-10-10):** 신규 `tests.test_review_http_operator_auth` **10/10 PASS** (기본/잘못된 인증 HTTP 401, HTML·F3·미디어 게이트, POST 별도 CSRF/Origin 403, 정당한 저장 200, 충돌 409, Range PDF/MP3 206, 다음 실행 키·토큰 재사용 차단, 35/36 격리, CLI 비밀번호 새로 생성). 기존 집중 HTTP/UI **74/74 PASS**. 실제 자동 정리되는 **PostgreSQL 17.11 11/11 PASS**, `server_stopped=true` 및 `disposable_cluster_removed=true`. **최종 전체 `python -m unittest discover -s tests -q` 378개 OK (skipped=14), 231.678초, 종료 코드 0**. 외부 서비스가 잠시 실패하는 AI 요약 fallback은 의도된 테스트 시나리오. 원본 frozen SQLite SHA-256 `076826708a93718faa28c3c1ae3e87bbf28aac4bcfaed88359fd2a12b1c0b7e6`, 원본 35회 MP3 SHA-256 `314af506159e24b7bd4b20e95d248683673e799e8e8af5ac4b77946879e4dae2` 변경 없음. 운영 PostgreSQL·원본·실사용 서버 접근 없이 테스트 전부 폐기 가능한 데이터에서 수행했다.

**실제 Chromium 렌더링/접근성:** Playwright-core 및 설치된 Chromium headless shell **1개 프로세스**를 재사용해 320·390·1366px 각각 실행. 실제 동결 35회 SQLite는 읽기 전용 백업 사본만 사용하고 HTTP Basic 키를 `httpCredentials`로 설정했다. 모든 폭에서 F3 목록 **70/70**, 승인 선언 확인 **2**, 근거 미확정 **0**, 실제 1번 상세 및 원본 링크를 렌더링했다. 각 화면의 문항 본문·보기·메모 글씨 **16px**, 가로 넘침 **0**, 브라우저 JS/콘솔 오류 **0**, POST **0**. 원본 PDF·MP3 Range GET은 각 HTTP **206**, 문항 15 이미지 원본은 HTTP **200**. 모바일 원본 대조 dialog의 Tab/Shift+Tab/Escape 초점 복귀 및 데스크톱 원본 패널 조작 통과. 인증 없는 브라우저 문서/목록/PDF 요청은 **401 Basic challenge**. 첫 문항 표시 **345/349/393ms** (320/390/1366 단발 실행), 각각 F3부터 이미지 선택까지 API 6회; 브라우저 전체 약 4.13초. HTTP 전체 경로 53 GET/0 POST, 200 29회·206 9회·401 15회, 핸들러 응답시간 중앙값 **3.87ms**/최대 72.39ms. 이는 한 번의 작은 로컬 스모크이며 지속 성능/운영 PostgreSQL 지연을 대표하지 않는다. 테스트 서버 정상 종료, 스레드 정지, Temp DB 폴더 삭제 완료; 민감 키 값은 JSON에 기록하지 않았다. 재현 지표는 Git 제외 `.stage9-runtime/operator_auth_playwright_metrics.json`.

### 다음 개선 후보

가장 큰 남은 판단 신뢰 한계는 **개별 검수자 인증과 권한/감사 주체 분리**다. 그 다음에는 35/36 F3 신규 근거 필드 70문항 응답의 크기(+6.9% 측정)의 필요성/압축·최적화와 네트워크 성능을 확인하되, 지금까지 확인된 첫 로드 동작과 스냅샷 일관성을 희생하지 않는다. 인증·접근키 소지는 실제 청취·승인의 증거가 아니며, 필요한 사람의 검수 작업은 자동화하지 않는다.
