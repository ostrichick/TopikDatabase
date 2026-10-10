# F3 AI 감사 요약 성능 개선 결과 — 2026-10-10

## 변경 범위

- 기존 사용자 검수 승인·검수 이력·원본 PDF/MP3·운영 DB 스키마 변경 없음. 운영 PostgreSQL은 read-only 측정만 수행.
- `src/ai_audit_list_summary.py` 신설: 35회 감사 run/pass/result/occurrence/attempts를 한 번씩 조회·파싱하고, 문항별 판정·risk·수렴·최신 적격 run·subject scope/F2 재시도 상태를 메모리에서 계산한다. 문항마다 기존 `summarize_question()`이나 `status_report()`를 재실행하지 않는다. 한 스냅샷(REPEATABLE READ READ ONLY)에서 조회.
- `src/review_ui.py`: 독립적인 `/api/questions-ai-summary` 조회가 `list_questions()`의 N+1 로직을 호출하지 않는다. 같은 회차에서 동시 요청은 스레드 lock 기반 single-flight 1개로 제한한다. 완료 시점을 기준으로 60초 캐시를 유지하고 실패 시 기존 유효 값을 보호하며 10초 후 재시도한다. 사용자 조회 오류는 일반 검수를 막지 않는다. 오류 로그에는 예외 유형/경과 시간만 기재한다.
- `src/review_ui.html`: 44초 고정 종료 대신 점진적 backoff(초기 750ms, 최대 12초 간격, 180초 상한), 타이머 취소·요청 abort, `다시 시도` 버튼, 회차 변경·구세대 응답 무효화, 승인 ACK/초안/필터 상태 보존.
- 새 회귀 테스트: `tests/test_ai_audit_list_summary.py`, `tests/test_review_ai_summary_cache.py`, `tests/test_review_ai_summary_recovery.py`.

## 성능 전후

| 측정/목표 | 기존 방식 | 개선 후 |
|---|---|---|
| 중앙 PostgreSQL 35회 AI 감사 요약 70문항 | 관측 122초 동안 `loading` 지속 | 일괄 집계 실측 5.797초, 5.250초 / 임시 로컬 HTTP 서버에서 GET→`ready` 5.625초 |
| 목록 집계 SQL | 문항당 15~23회, 문항 70개 | 신규 집계 약 6 SQL SELECT, 테스트 상한 ≤20 |
| warm HTTP 응답 | 검증되지 않음 | 같은 서버에서 3회 0.001초 미만(표시 정밀도 기준) |
| fast 문항 목록 | 기존 기준 1.44초 | GET 70문항 실측 2.265초 (네트워크/서버 상태에 따라 변동하므로 안정적 비교 확인 필요) |

위 값은 서로 다른 측정 시점의 관측치다. SQL 호출 수 감소와 cold 집계 10초 이내는 확인했으며, 네트워크 변동을 감안한 동일 조건의 반복 측정이 바람직하다. 다른 전체 테스트와 레거시 비교를 동시에 실행했을 때 한 차례 임시 HTTP 20초 제한에 걸렸지만, 단독 재측정에서는 5.625초 `ready`가 확인되었다. 운영 DB 자체의 읽기 불능 또는 영구 실패로 해석하지 않는다.

## 정확성 및 통합 검증

- 신규 70문항 SQLite fixture의 모든 문항 필드와 기존 `ReviewStore.list_questions()`의 golden 결과 동등성 확인. 여러 run·대상 scope·실패/timeout·재시도·finding recurrence 조건 검사.
- 중앙 PostgreSQL에서 35회 듣기 `035-I-L-001`, 읽기 `035-I-R-031`, `035-I-R-070`의 기존 상세 감사 계산과 신규 경량 필드 비교 **일치**. 해당 비교는 read-only 질의.
- 전체 테스트: **310개 실행, 실패/오류 0, skipped 6**. 기존 F1 실제 PostgreSQL 경합 테스트는 별도 disposable PG 설정 없어서 skip 유지(운영 DB에 쓰기 테스트하지 않음).
- 집중 테스트: 단일 flight/캐시 TTL 및 실패/복구/10 동시 조회, UI generation·회차·수동 재시도·미저장 상태, F2 범위/감사 동등성/DB snapshot 회귀 통과.
- 중앙 PostgreSQL을 사용하는 별도 localhost GET-only HTTP 서버: concurrent 10개 첫 상태 `loading`, 이후 70문항 `ready`, warm 3회 즉시 응답, 36회 `unavailable` 분리 **PASS**.
- 기존 Edge CDP 연결을 활용한 **임시 테스트 탭**에서 실제 70/70 문항, AI 감사 필터/정렬 표시 및 변경, 390px와 320px 가로 넘침 없는 화면 확인 **PASS**. 원래 브라우저 탭과 운영 검수 서버는 변경하지 않았음.

## 남은 운영 고려사항

- 캐시 갱신 기준은 완료 후 60초 TTL이며, 운영 외부 감사 작업의 append-only 업데이트를 즉시 감지하는 watermark는 이번 개선에서 별도로 구현하지 않았다. 따라서 감사 직후 최대 약 60초간 이전 요약이 표시될 수 있다. 정확한 실시간 반영이 필요해지면 별도 watermark/notify invalidation을 추가한다.
- 실제 중앙 PostgreSQL 동시 *쓰기*/ROLLBACK 검증은 운영 데이터 보호를 위해 수행하지 않았다. 기존 F1/승인 충돌 회귀는 fixture/모의 경합 테스트로 확인했다.
- 코드는 로컬에서 검사/커밋할 수 있으나, 서버 재시작이나 배포 및 GitHub push는 이 단계에서 실행하지 않았다. 기존 실행 중인 검수 서버에는 재실행 전까지 새 코드가 반영되지 않는다.
