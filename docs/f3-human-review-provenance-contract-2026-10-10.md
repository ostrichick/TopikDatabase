# TOPIK F3 승인 표시·사람 검수 이력 현재성 통합 (2026-10-10)

## 범위 / 변경하지 않은 기준선

작업 시작 `feat/topik36-pdf-ingestion-20261009`, HEAD `6ab74d6`, 변경 없는 단일 워크트리. F2–F5, F3 fast-list/bundle/AI 분리 조회, AI 판단 패널, PDF·MP3 원본 탐색, 공유 대본의 버전/CAS·원자성, 음원 검증 선언과 클립 provenance 제한을 유지했다. 원본 35회 SQLite/MP3는 mode=ro/불변, 운영 PostgreSQL 쓰기·배포·Push·서버 재시작·36회 마이그레이션 없음. 모든 쓰기·승인·상충 이력은 **격리 복제본/합성 테스트**에 한정한다.

## 실제 관찰한 문제와 계약

기존 서버 `/api/questions-fast`는 `status='verified'`와 `last_human_review.approved=false/null`을 이미 구분하지만, UI `reviewIndicator`, 진행률·승인 개수·필터·배지에서 `status === 'verified'`만으로 **'인간 검수 승인 완료'**를 표시했다. 상세 응답은 fast-list와 같은 근거 계약이 없었고, fast 저장 ACK에는 최신 이력 배열이 없어 정상 200 이후에도 오래된 메모/판정이 캐시에 남았다. 이전/새er 버전의 비동기 F3 또는 상세/번들 응답이 현재 편집/이력과 섞일 위험도 있었다.

서버는 저장된 `review_status`를 변경하지 않고 다음 별도 근거를 **fast-list, 개별 fast/full 상세, F3 70문항 bundle, fast 저장 ACK**에 동일하게 제공한다.

- `last_human_review`: null 또는 `{id, record_id, status, reviewer, reviewed_at, is_current, approved, identity_verified:false}`.
- `human_review_evidence`: `{approval_state, reason, approved, identity_verified:false}`; approval_state는 `current_manual_approval`, `verified_without_current_manual_approval`, `not_verified` 중 하나. reason은 `latest_manual_review`, `missing_manual_review`, `superseded_manual_review`, `manual_status_mismatch`, `incomplete_manual_review`, `status_not_verified` 중 하나.
- **현재 승인 근거 확인** 조건은 DB 상태 verified **AND** 가장 최근 문항 변경 기록이 최신 `manual_question_review` **AND** 그 기록의 상태가 verified **AND** reviewer 문자열이 비어 있지 않고 timezone-aware 검수 시각이 유효한 경우. `local_reviewer`/합성 reviewer는 **실명·로그인·독립 신원 인증이 아니므로** 항상 identity_verified=false. AI 지적사항 검토와 음원 후보·사람 검증 선언은 이 문항 승인 근거와 독립이다.
- 검증 선언의 부재·과거 이력에 의한 대체·상태 불일치·타임스탬프/서술 필드 결손은 `verified_without_current_manual_approval`로 표현하고 **저장된 verified를 취소하거나 실제 사람이 승인하지 않았다고 단정하지 않는다.** `rejected`도 현재 로컬 반려 선언과 저장 상태만 있는 경우를 분리한다.
- 상세/F3는 기존 검수 이력 30건 표시 상한을 보존하되 `id`, `reviewer`, `scope`, `note`, 시각을 안전하게 표출한다. 최근 30건 밖의 최신 manual 기록은 필요할 때만 별도로 조회한다. F3는 **기존 일괄 review query**를 재사용하고 per-question 추가 SQL 없이 같은 판정을 계산한다.
- `POST .../review?fast=1`은 잠금 트랜잭션에서 판정한 `last_human_review`/`human_review_evidence`와 최신 history 30건, `saved_review_event`를 함께 반환한다. 프런트는 추가 상세 GET 없이 **확정된 ACK만** 캐시·이력에 반영한다. 이전 CAS 버전, shared sibling 버전, 저장·rollback 시멘틱스는 그대로 유지한다.

## UI·동선

- 한 함수 `reviewEvidence()`를 목록, 상세 배지, 건수, 진행률, 승인/반려/미확정 필터와 키보드/스크린리더 설명에 재사용한다. DB 저장 상태는 중립적으로 보이고, 유효한 현재 선언에만 **'승인 선언 확인'**, 미확정 상태는 **'검수 근거 미확정'**, 반려·검토 필요도 별도로 표시한다. 추적 가능한 개인 인증이 없음을 명시한다.
- 목록 상태는 색상뿐 아니라 **문자 라벨**을 동반한다. 새 `unknown` 필터 및 건수, 접근성 `aria-label`, `aria-valuetext`, 모바일 가독성/44px 기존 터치 영역, 필터·키보드 조작을 확인한다. 미검수 다음 이동은 근거 미확정 문항도 포함한다.
- AI 발견은 별도 근거로 남긴다. 현재 승인이 확인된 문항의 **후발·검증된 AI 지적**은 재확인을 요구하되, 인간 승인을 자동 철회하거나 AI 지적을 사람이 실제 확인했다고 주장하지 않는다. 현재 승인 근거 미확정 문항에서도 AI 발견은 근거 미확정과 구분해 안내한다.
- 제출 후 200의 최신 history/근거를 현재 화면·재방문 캐시·F3 목록에 반영한다. 입력 도중 생긴 **미저장 추가 수정은 보존**한다. ACK가 오기 전 승인으로 표시하지 않으며, 응답 누락·HTTP 409 시 보류/복구 안내와 초안 보존을 유지한다. 승인 저장 성공 시 기존 다음 문항 자동 이동을 유지한다.
- 새 list의 `review_version`이 상세 캐시보다 최신이면 해당 캐시만 무효화하며, 열린 입력은 조용히 덮어쓰지 않는다. 이전 F3 요청이 최신 ACK를 되돌리지 못하며, 늦게 도착한 상세/번들로 버전·시험 회차가 섞이지 않게 검사한다. 초기 병렬 F3 bundle은 첫 목록보다 먼저 도착해도 올바른 회차이면 재사용한다. 정상 첫 화면에 불필요한 새 API 요청은 도입하지 않았다.

## 실제 자료와 격리 테스트의 구분

읽기 전용 frozen `035-I-B.sqlite`: **70문항 중 저장 verified 2, needs_manual_review 68**. 두 verified에는 최신 manual 검수 기록이 있고, 이 로컬 자료에서 기존 UI의 잘못된 실제 표시 건수가 관찰되지는 않았다. 같은 파일의 `exams`에는 **035-I-B만** 있어 `036-I-B`는 이 파일에서 열 수 없다. 별도의 36회 staging-v6 데이터(70문항/6개 원본 파일)를 읽기 전용 확인했고, 백엔드 테스트는 실제 36회 PDF를 참조하는 **별도 임시 SQLite**로 35/36 상태·AI·공유구간 누수를 검증했다. **운영 PostgreSQL의 현재 행 상태를 조회한 결과로 해석해서는 안 된다.**

재현을 위한 임시 35회 SQLite 백업에는 원본의 verified 2건을 그대로 두고, 3·4번의 저장 상태를 verified로 가정하며 3번은 사람 기록 없이, 4번은 합성 manual 후 다른 변경 이력으로 무효화했다. 5번은 근거 없는 rejected, 6번은 가상의 현재 반려 이력으로 만들었다. **이 기록은 테스트 데이터이며 사람이 청취·검수·승인한 사실이 아니다.**

## 검증

- 새 서버 계약 테스트: status-only/누락/오래된 기록/손상된 reviewer·시각, 나중에 덮인 manual, 30건 밖의 이력, 반려, no-op ACK, 최신 이력 저장 직후, 강제 rollback, 409, 35·36 격리·가상 검수 등을 **격리 DB**에서 검증. `tests/test_review_ui.py` 등 백엔드 5개 모듈 89개 OK (5 skipped).
- 새 실제 인라인 JS Node VM 통합 테스트 5개: verified와 last_human_review true/false/null, 필터·ARIA·카운트, 200 ACK 즉시 기록·캐시, 요청 중 입력 보존, HTTP 409 회복, 낙관적 다음 문항 이동, 응답 역전·stale cache·35/36 분리. 기존 UI 통합 테스트 더블도 새 계약을 갖추도록 정렬했고 관련 16개 테스트 통과.
- 실제 **격리 PostgreSQL 17.11**, 자동 생성된 새 localhost 클러스터: 공유 대본/음원 트랜잭션, CAS/409, 롤백, F3 스냅샷/36 격리, 음원 provenance 경계 **11/11 PASS**. 매 실행 종료 후 stopped/removed. 새 모든 human_review_evidence 경계 사례를 실제 PG 엔진별로 각각 재실행한 것은 아니며, 개별 계약 테스트는 별도 격리 SQLite로 수행했다.
- Playwright/Chromium 실제 브라우저 **320 / 390 / 1366px**에서 기존 커밋의 HTML과 새 HTML을 동일한 합성 35회 데이터를 대상으로 비교했다. 기존 UI는 **저장 verified 4건을 모두 승인**으로 표시, 수정 UI는 **현재 승인 선언 2건 + 검수 근거 미확정 3건**(verified 2건과 rejected 1건), 다른 반려 1건·pending 64건으로 구분했다. 새 화면의 상세 배지·스크린리더 라벨·미확정 필터(3건)·현재 승인 필터(2건)를 확인. 각 viewport 가로 overflow 없음, 본문 16px, JS error 0, 저장 POST 0. 저장 뒤 렌더링은 Node VM/백엔드 격리 케이스로 별도 검증했다.
- 비교 브라우저 짧은 샘플 첫 문항 표시(각 1회): 기존 **427 / 246 / 276ms**, 수정 **259 / 230 / 361ms** (320/390/1366 순서); 다른 단발 재실행에서는 각각 **297 / 447 / 291ms**, **302 / 296 / 369ms**로 변동했다. 기본 초기 동선은 양쪽 모두 bundle/독립감사/F3 fast/AI 요약/필요시 첫 상세이며, 응답 순서에 따라 **API GET 4–5회**. 이후 수정 UI에서 3번 상세를 열고 새로운 미확정·승인 필터까지 조작하면 총 GET 7회였다(늦은 bundle에 따른 3번 상세 GET 1회, 필터에 따른 1번 자동 이동 GET 1회). 이들은 **추가 검수 동선**의 요청이므로 같은 첫 화면의 API 증가로 해석하지 않는다. `questionSearch`에서 Tab으로 `statusFilter`에 도달하는 키보드 순서도 모든 폭에서 확인했다. 단발 샘플은 체감 속도 개선/악화의 근거가 아니다. `.stage9-runtime/provenance_{before,after}_{320,390,1366}.png`, 브라우저 metrics JSON은 Git 제외.
- 실제 35회 read-only F3 70문항 응답은 새 근거 계약 때문에 **183,798B → 196,494B**(+12,696B, 약 6.9%). 각각 9회 실행 뒤 끝 8회 중앙값 **24.865ms → 30.721ms**. 로컬 비동시 소표본으로 지속 성능 악화를 단정하지 않지만, 근거 필드 비용을 공개한다. 기존 F3 API 1회/70문항 집계·per-question SQL 없음은 유지. `.stage9-runtime/review_provenance_f3_benchmark.json` (Git 제외).
- 수정 후 최종 전체 `python -m unittest discover -s tests -q`: **368 tests, OK (skipped=13), exit 0, 171.544초**. 일차 전체 검증의 실패 2개는 과거 UI 테스트 더블이 새 `reviewEvidence` 및 시험 회차 보호 계약을 포함하지 않은 것이 원인이었으며, 테스트를 수정하고 집중 16/16 및 전체 368개 재검증으로 해결했다. AI 요약 오류 로그는 의도적으로 만든 fallback 경로이다. 마지막 코드·브라우저 수정 뒤 관련 64개 집중 회귀 재실행도 모두 통과했다.

## 한계와 다음 우선순위

`identity_verified:false`는 **현재 시스템에서 개인 신원을 검증할 수 없다는 뜻**이지, 과거 기록이 거짓이거나 사람이 검수하지 않았다는 뜻이 아니다. CSRF/loopback Host·Origin 검사는 인증이 아니며 고정 `local_reviewer`로부터 개인·장치를 식별할 수 없다. 사람이 청취해야 할 35회 음원 후보도 여전히 별도 작업이며 자동 승격하지 않았다. 운영 DB/원본/서버/원격 push는 변경하지 않았다.

세 장기 목표 관점에서 이번 개선은 (1) 색상에 의존하지 않는 근거 라벨·필터·모바일/키보드 동선, (2) 실제 사람 승인 **선언**과 최신 감사 기록·AI·음원 상태 구분, (3) 하나의 서버 판정과 하나의 UI 헬퍼, 불필요한 저장 후 상세 GET 없는 ACK·버전 동기화를 달성했다. **다음 영향도 높은 독립 개선 후보**는 로컬 HTTP 서버의 인증·권한 분리와 검수 주체 식별이다. 현 상태에서는 어떤 로컬 프로세스도 loopback GET에서 CSRF 토큰을 받을 수 있으므로 장치 간 중앙 DB의 `local_reviewer`가 실제 한 사람임을 증명할 수 없다. 먼저 실제 운영자의 인증 요구·사용자 동선·신뢰경계를 확인하고, 독립 인증이 없는 경우는 절대 인증된 검수라고 표시하지 않는 fail-closed 설계를 적용해야 한다. 사람의 판단/승인과 운영 인증 설정을 자동으로 가장하지 말 것.
