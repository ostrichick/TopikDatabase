# 회차 단위 일괄 번들 로딩(Session-Scoped Bundle) 최적화 명세서

- **작성 일자**: 2026-10-07
- **적용 대상**: `src/review_ui.py`, `src/review_ui.html`, `tests/test_review_ui.py`
- **목표**: 원격 PostgreSQL SSH 터널 RTT 지연으로 인한 문항 전환 지연(2~3초)을 완전 제거하고, 브라우저 인메모리 전환을 통한 **문항 이동 0.00초(즉각 전환)** 달성

---

## 1. 최적화 배경 및 문제 분석

### 1.1 물리적 제약 (Network RTT Bottleneck)
- 중앙 데이터베이스(`wordpress-blog` PostgreSQL)는 SSH 터널(`127.0.0.1:55432`)을 통해 통신합니다.
- 터널 왕복 1회에 최소 **0.46초**의 물리적 RTT가 발생합니다.
- 기존 단일 문항 조회(`GET /api/questions/<id>`) 방식은 문항을 클릭할 때마다 원격 DB를 쿼리하므로, 문항 전환마다 2~3초의 멈춤 현상(대기 스피너)이 발생하여 작업 피로도가 누적되었습니다.

### 1.2 확장성 고려 및 설계 선택
- 전체 TOPIK 데이터베이스는 향후 12개 회차(약 2,100문항)로 확장될 예정입니다.
- **Option A (회차 단위 일괄 로드)** 채택:
  - 전체 DB(12개 회차)를 한꺼번에 받지 않고, 검수자가 현재 작업 중인 **해당 시험 회차(제35회 TOPIK I B형 70문항, 약 124KB)**만 선별 번들링합니다.
  - 124KB는 웹 환경에서 일반 이미지 1장보다 작은 경량 페이로드로 네트워크 부담이 없습니다.

---

## 2. 백엔드 아키텍처 및 구현

### 2.1 단일 배치 추출 (Batch Bulk Extraction)
70개 문항을 각각 N번 쿼리(70 × 6 = 420회 쿼리)하지 않고, 단 6개의 일괄 SELECT 쿼리로 데이터를 조립합니다:

1. `questions`: 전 문항 기본 메타데이터 및 지문·정답 조인 (1회)
2. `choices`: 전체 선택지 일괄 조회 후 `question_id`별 그룹화 (1회)
3. `transcripts`: 전체 듣기 대본 및 경고 JSON 파싱 (1회)
4. `question_images`: 전체 문항 이미지 키 매핑 (1회)
5. `review_records`: 검수 기록 및 버전 카운트 일괄 집계 (1회)
6. `audio_segments`: 전체 음원 구간 및 에셋 메타데이터 일괄 로드 (1회)

### 2.2 신규 엔드포인트: `GET /api/questions-bundle`
- **반환 구조**:
  ```json
  {
    "exam_id": "035-I-B",
    "total_questions": 70,
    "questions": {
      "035-I-L-001": { ... },
      ...
      "035-I-R-040": { ... }
    }
  }
  ```
- **하위 호환성**: 기존 단일 문항 API(`GET /api/questions/<id>`)는 100% 원형 유지.

---

## 3. 프론트엔드 인메모리 캐싱 및 무지연 렌더링

### 3.1 백그라운드 프리페치 (`review_ui.html`)
- 초기 화면 로드 시 `loadList()` 완료 직후 백그라운드로 `loadBundle()`을 비동기 호출합니다.
- `bundle.questions`를 수신하여 브라우저 `state.detailsCache`에 보관합니다.

### 3.2 문항 전환 로직
- `selectQuestion(id, { force = false })`:
  - `state.detailsCache[id]`가 존재하면 네트워크 요청(`fetch`)을 일체 발생시키지 않고 **동기적으로 즉시 렌더링 (소요 시간 0.00ms)**.
  - 캐시가 아직 도착하지 않은 첫 문항 또는 `force: true`(충돌 후 강제 새로고침) 시에만 개별 API 호출로 폴백.

### 3.3 정합성 및 캐시 동기화
- `saveReview`: 검수 승인/반려 시 반환된 최신 문항 상태를 `state.detailsCache[id]`에 즉시 반영.
- `saveAudioSegment` / `exportAudioClip`: 음원 구간 변경 시에도 `state.detailsCache[id]`의 `audio_segment` 및 `version` 갱신.

---

## 4. 검증 결과 (Proof of Execution)

### 4.1 자동화 테스트 하네스
- 테스트 파일: `tests/test_review_ui.py`
  - `test_questions_bundle_covers_all_seventy_questions`: 번들 70문항 필드 일치성 검증 (OK)
  - `test_get_questions_bundle_endpoint`: HTTP 엔드포인트 응답 및 캐시 헤더 검증 (OK)
- 전체 스위트 실행:
  ```text
  Ran 142 tests in 68.457s
  OK (Exit Code 0)
  ```

### 4.2 프로덕션 실측 벤치마크
- **번들 페이로드 크기**: `126,980 bytes` (**124 KB**)
- **번들 수신 소요 시간**: **3.50초** (원격 PostgreSQL SSH 터널 왕복)
- **문항 전환 소요 시간**: **0.00초** (브라우저 인메모리 전환)
