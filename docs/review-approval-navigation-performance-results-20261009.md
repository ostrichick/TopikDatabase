# TOPIK 검수 승인·자동 이동 및 로딩 성능 개선 결과 (2026-10-09)

## 결과 요약

- 범위: 원래 계획 `docs/review-approval-navigation-performance-plan-20261009.md`의 P0–P3.
- 코드 커밋: `e73bd44 fix(topik): stabilize review advancement and reduce remote approval overhead`.
- **P0/P1 기능 코드 및 회귀 테스트 완료.** P2 PostgreSQL 승인 저장 경로의 연결/SQL 왕복 최적화 코드 및 검증 완료.
- **최종 커밋 상태 전체 유닛 테스트 240 PASS / 0 실패** (`py -3 -B -m unittest discover -s tests -q`, 실행 시간 약 152초). 관련 35개 통합 타깃 테스트 추가 재실행 PASS. `git diff --check` PASS.
- **운영 승인 요청 POST 0회, 검수 기록·원본 PDF·MP3·DB 스키마 변경 0건.** SQLite 동작 및 35회/36회 검수 버전 안전장치 유지.

## 단계별 변경 및 확인

### P0: 승인 후 간헐적 미이동

파일: `src/review_ui.html`, `tests/test_review_ui_flow.py`.

- 기존 일반 승인 성공 경로에서 `state.baseline`을 갱신하지 않아 이미 저장된 텍스트·메모를 '미저장'으로 오인하던 버그를 수정했다.
- **확인된 저장 ACK 후에만** 제출한 스냅샷을 baseline으로 기록하므로 실제 미저장 음원 구간과 요청 중 새로 작성한 내용은 계속 보호한다.
- 다음 문항은 화면에 보이는 필터/정렬 목록을 우선해 선택한다. 목록에 없는 현재 문항에서는 전체 목록을 안전하게 사용한다.
- 정상 승인/실패/409/이상 ACK, 필터 순서, 실제 `dirty`·`canNavigate`·`selectQuestion`, 미저장 음원 구간의 수락/취소를 Node VM 통합 테스트에서 검증했다.

### P1: 36회 PDF·MP3 재로드

파일: `src/review_ui.html`, `tests/test_review_ui_media_urls.py`.

- 36회 공통 MP3, 정답 PDF, 듣기 대본 PDF의 문항별 URL을 동일 원본 기준으로 정규화했다.
- **36회 듣기 문제지 PDF와 읽기 문제지 PDF는 서로 다른 원본**이므로 구분한다. 35회의 기존 정규화, 회차별 `exam_id` 쿼리, same-origin 제한은 유지한다.
- 동일 MP3를 쓰는 듣기 문항 사이에서는 오디오 `src`가 바뀌지 않아 `player.load()`를 중복 호출하지 않는 경로를 테스트했다. 브라우저의 실제 네트워크 재전송 건수는 별도 실사용 계측 필요.

### P2: PostgreSQL 승인 저장 경로

파일: `src/review_ui.py`, `tests/test_review_ui_postgres_write.py`.

- 기존 '원본 증거 preflight READ 접속 + 잠금·저장 WRITE 접속'을 **하나의 writable PostgreSQL 접속**으로 통합했다. 사전 검증 후 `ROLLBACK`으로 읽기 트랜잭션을 종료하고 다시 쓰기 트랜잭션에서 `FOR UPDATE`로 잠근다.
- 원본 파일별 SHA-256 검증은 **모두 유지**하면서 원본 파일 메타데이터 2–3건을 한 SQL로 읽는다.
- PostgreSQL 질문 `FOR UPDATE OF q`와 본문 조회를 하나의 SQL로 결합하고, 36회 검수 버전 COUNT와 punctuation revision marker 확인을 하나의 SQL로 합쳤다.
- `TOPIK_REVIEW_TIMING=1`을 설정하면 승인 저장의 연결/사전 검증/쓰기/총 시간을 **자격증명·입력 본문 없이 밀리초**로 출력한다. 기본값은 비활성.
- 새 풀 라이브러리가 없어 전역 수제 연결 풀을 만들지 않았다. 읽기 GET 요청은 여전히 연결을 새로 만든다.

## 재현 및 테스트 근거

| 검증 | 결과 |
| --- | --- |
| 최종 커밋 상태 전체 테스트 (`discover -s tests -q`) | **240 PASS**, 0 실패 |
| 관련 PostgreSQL 쓰기/읽기·멀티회차·UI·미디어 35개 테스트 | **35 PASS**, 0 실패 |
| 무결성/SQL 직접 확인 | 36회 read-only `_version` = 1, 원본 미디어 SHA 확인, joined `FOR UPDATE OF q`의 PostgreSQL `EXPLAIN` 14행 정상 |
| 수정 전 실서버 GET `18736` | 36회 목록 약 2.18초, 상세 약 3.60초 (이번 검증 회차의 개별 표본) |
| 새 코드 별도 테스트 서버 GET `18737` | 36회 목록 약 2.67초, 상세 약 3.67초, 각각 HTTP 200, 70문항·버전 일치 |
| 보존 상태 | 기존 `18736` 서버는 유지, 임시 `18737` 서버는 테스트 후 종료 |

두 버전의 **GET 속도는 이번 변경으로 개선됐다고 주장할 수 없다.** P2의 주요 최적화는 **승인 POST 저장 경로**에 적용했고, 운영 DB에서 실제 승인 POST를 수행하지 않았으므로 승인 시간 감소량의 정량화는 아직 불가능하다. SSH 왕복 편차가 있어 단발 GET 표본으로도 우열을 판단하지 않는다.

## 운영 적용/잔여 검증

1. 기존 `18736` Python 검수 서버는 **이 문서 작성 시 재시작하지 않았다**. 따라서 이미 떠 있는 Python 프로세스는 P2 서버 코드의 이전 버전을 사용한다. HTML은 HTTP 요청 때 파일에서 읽으므로 브라우저 새로고침 시 P0/P1 프런트엔드 코드가 적용될 수 있지만 **열린 검수 초안은 저장 전 새로고침하지 말 것**.
2. 사용자가 현재 브라우저의 검수 초안·미완료 승인·음원 입력을 정리한 것을 확인한 후, 서버를 안전하게 재시작해야 P2도 실제 적용된다. 재시작 후 신규 연결 및 읽기 API GET·회차 전환을 확인한다.
3. **폐기 가능한 PostgreSQL**에서 실제 승인 POST와 롤백/409/경합/성능 전후 비교를 실행하고, Chrome에서 필터·수정 후 승인·음원·그림 등 E2E 시험을 추가할 필요가 있다. 이 노트북에는 안전한 일회용 PG 서버가 준비되지 않아 실제 POST 시험은 하지 않았다.
4. 위험 징후(잘못된 승인 성공 표시, 데이터 유실, 원본 PDF/MP3 잘못 연결, 35회 동작 변화, 트랜잭션 누수)가 보이면 재시작/적용을 중단하고 `e73bd44`를 `git revert`로 안전하게 되돌릴 수 있다. 되돌리기 전에 저장 중 요청을 완료하고 열린 초안을 확인해야 한다.

## 변경 금지 확인

- 원본 PDF·MP3 및 `035-I-B` 원본 SQLite와 review history, `036-I-B` 실제 승인 기록·추출 결과 파일, SSH/TLS 설정과 운영 DB 스키마는 수정하지 않았다.
- 외부 Git push, 운영 데이터 변경, 라이브 승인 POST, 기존 사용자 검수 탭 재시작/강제 새로고침 없음.
