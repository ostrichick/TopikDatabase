# TOPIK 검수실 AI 감사 정보의 의사결정 유용성 개선 — 2026-10-10

## 범위와 기준선

시작 HEAD `3b15a01`, 브랜치 `feat/topik36-pdf-ingestion-20261009`, 작업 트리 clean. 기존 F2–F5, F3 집계·캐시·비동기 복구, 사람 검수/AI 구분, 모바일·데스크톱 문항 레일·근거 버튼 개선(`bc427a0`,`1de0288`)은 유지한다. 사용자 요청의 세 목표 중 **AI 감사 내용이 검수 결정을 직접 돕지 못하는 문제** 하나를 선정했다.

35회 실제 코드 계약: 경량 목록 응답에는 최근 run의 `total/clear/finding/uncertain/unresolved_findings/risk_score`, 실행 attempt·수렴 등이 있고, 구체적인 문항별 verdict·finding의 요약/원시 근거/패스 이력은 상세 조회 후에만 얻을 수 있다. 36회는 현재 기존 35회 AI 감사와 독립 비교 결과를 제공하지 않는다. **AI 미지원·미수집은 ‘이상 없음’이나 ‘사람 확인 완료’가 아니다.** 원본 위치는 문항 상세의 `source_pdf_page`, `answer.source_pdf_page`, 듣기 `transcript.source_pdf_page`만 사용하며, 이 페이지는 대조 시작 위치이지 AI가 지목한 결함 위치가 아니다.

## 문제 원인과 사용자가 겪던 흐름

- 기존 `#aiAuditSection`을 펼치면 verdict/attempt/pass/run/수렴/위험도 **기술 지표 8~9개가 먼저 보이고**, 그 아래 과거 run 이력·attempt와 원시 JSON을 거쳐야 문항별 상세 판단의 핵심인 **finding summary/detail/evidence**가 나왔다.
- 위험 점수나 발견 횟수만으로는 **무엇이 문제인지**, 어떤 자료를 어디서 대조할지, 사람의 승인 후 새 AI 지적이 생긴 것인지 파악하기 어렵다. 수치가 0이거나 기록이 없을 때 ‘미감사’를 ‘이상 없음’으로 혼동할 위험도 있다.
- 기존 35회 AI 감사가 새 run으로 바뀐 뒤 브라우저가 오래된 상세 결과를 갖고 있으면 새 요약과 섞어 보여줄 수 있다. UI는 검수자가 새 근거를 명시적으로 요청할 때만 전체 상세를 읽어야 하며 F3의 빠른 목록 로딩을 느리게 해서는 안 된다.

## 변경 후 정보 구조

1. **문항 상단 판단 패널:** `#aiDecisionPanel`을 **문항 제목 바로 다음**에 배치하여 필터/기술 기록을 탐색하지 않고 첫 화면에서 판단 단서를 찾게 했다. 표시 내용은 `검수 전에 확인할 근거`(AI finding/불확실/실패·미완료/추출 경고) 또는 `AI 감사 및 원본 확인 상태`. 승인·반려·미검수는 DB의 사람 상태 그대로 표현하고 **AI 지적을 사람이 확인했다고 주장하지 않는다**.
2. **확인할 문제·이유:** 이미 로드된 독립 Gemini/ChatGPT 보고의 실제 `summary/detail` 및 완료된 문항별 AI `finding.summary` 등 **저장된 텍스트만** 최대 3개 미리 보여준다. F3 경량 집계는 지적 건수만 반환하므로 요약이 없을 때는 **구체 내용 미제공**을 명시하고 근거 버튼으로 상세 데이터를 가져온다. `high/critical/blocking` 추출 경고는 실제 저장된 `message`를 최대 2개 우선 노출하며, 나머지 경고도 기존 바로가기에서 확인 가능하다. 점수만으로 오류 종류나 수정 지시를 생성하지 않는다.
3. **원본 대조 시작점:** 문항 PDF, 정답표 PDF, 듣기 대본의 실제 페이지 번호가 있으면 명시하고 없으면 `페이지 미기록`. 듣기 음원은 구간을 추정하지 않고 별도 청취 검증으로 안내. 제공 페이지는 **AI의 오류 지목 위치 아님**을 바로 옆에 표시한다.
4. **시점·검토 여부:** 저장된 AI run 생성 시각은 존재할 때만 보이며 승인 대비 순서·최신성은 근거가 없어 **미확정**이라고 표시. 사람의 승인 상태와 AI 지적 검토 여부를 분리한다. 36회는 추출 경고가 있더라도 **기존 35회 AI 감사 미지원**을 함께 표시해 `clear`로 오인하지 않게 한다.
5. **상세의 순서 역전:** `#aiAuditSection` 안에서 **문항별 verdict·finding 근거가 먼저 보이고**, run/pass/attempt/이력·통계는 키보드 접근 가능한 네이티브 `<details id="aiTechnicalDetails">` 아래 보존. 사용자가 원할 때 상세 근거/원시 JSON까지 접근할 수 있다.
6. **비동기·범위·중복 방지:** 패널 렌더는 기존 state에서 동작하는 순수 UI 읽기이며 API 요청을 추가하지 않는다. F3 async summary·독립 비교·상세 클릭 이후에만 패널의 정보가 갱신된다. 실제 상세 API는 사용자가 감사 상세를 펼칠 때만 사용. list 최신 run과 상세 run이 다르면 과거 finding을 최신 결과에 병합하지 않고 상세를 다시 요청하게 한다. 36회에는 35회 목록 데이터가 잘못 남아 있더라도 표시하지 않는다.

## 실 브라우저 수치 및 사용 시나리오

설치된 Playwright-core + Chromium headless shell에서 **격리된 localhost SQLite fixture**로 동일한 기존 HTML(`HEAD:src/review_ui.html`)과 새 HTML을 각각 320·390·1366px로 렌더링했다. 기존 화면과 같은 문항 탐색 시작점과 키보드/편집 필드를 유지하고 판단 패널만 추가했다.

| 관찰 항목 | 변경 전 | 변경 후 |
| --- | --- | --- |
| 320/390/1366px 첫 편집 제목 Y | 612 / 588 / 335px | 동일 612 / 588 / 335px |
| 문항 편집 중 판단 요약 | 없음; 기존 AI 기술 상세로 이동해야 함 | **320px Y=690px, 390px Y=666px, 1366px Y=375px**에서 바로 시작 (문항 제목 다음) |
| 발견 근거/원본 페이지 | 기존 실행 수치·위험 지표 8~9개 뒤에서 해석 | 실제 finding 우선, 대조 시작 위치 표기; 상세 1회 클릭 |
| 상세 실행 통계 | 위쪽에 기본 펼쳐 표시 | 기술 내역 접기(원하면 그대로 열람) |
| 목록 API 최초 요청 | 화면 진입 시 fast list와 기존 필요한 GET | 변경 후 동일 API 경로, 렌더만으로 신규 요청 없음 |

구체 검증: 35회 fixture에 실제 구조의 AI finding 2건과 원본 자료 페이지 1, 추출 경고를 삽입하여 기존 요약만 있는 상태→상세 근거 클릭→on-demand 상세 GET→문항별 finding 확인→기술 `<details>` 별도 펼치기. 320/390 모바일에서 원본 panel `role=dialog`, summary 키보드 포커스, 1366 데스크톱에서 정보 순서 및 가로 넘침 없음. 36회는 추출 경고를 별도로 표시하면서 35회 AI 미지원 상태 유지. 비운영 읽기 fixture로만 검증했고 승인 POST·DB 변경은 없음. 스크린샷: `.stage9-runtime/decision_{before,after}_{320,390,1366}_editor.png`, `.stage9-runtime/decision_after_*_evidence.png`; 정량 로그는 `.stage9-runtime/ai_decision_metrics.json`.

## 테스트·제약 및 다음 우선순위

- 신규 `tests/test_review_ai_decision_panel.py`: 패널 의미론/접근성, 원본 페이지 있는/없는 경우, 승인≠AI 확인, 경량 요약→비동기 갱신, F3 요청 수, 초안·ACK·version·CSRF 보존, 36회 데이터 격리, 신·구 run 상세 혼합 방지. 기존 `tests/test_review_ai_summary_recovery.py` Node fixture에 신규 렌더러 스텁 추가.
- 전체 회귀 `python -m unittest discover -s tests -q`: **318 tests, failure/error 0, skip 5**, 최종 PowerShell `FULL_SUITE_EXIT_CODE=0` 확인 (145.874초). 마지막 UI·경고 표시 이후 집중 회귀 **48 tests, failure/error 0, skip 5** (12.228초). 전체 inline JavaScript `node --check` 및 `git diff --check` 통과. F3 backend SQL, 스키마, 사람 검수 저장 API, PDF/MP3 원본을 수정하지 않았다. 전체 테스트에서 skip한 5개는 실제 격리 PostgreSQL 경합 환경 등 별도 준비가 필요한 테스트이며 운영 DB 쓰기를 대신 수행하지 않았다.
- 한계: 현재 독립 보고의 일부 `summary/detail`과 기존 AI finding에는 **결함의 정확한 PDF 좌표나 청취 구간이 없다**. 임의로 조치·페이지·시간을 생성하지 않았다. 36회 AI 감사 미지원. 브라우저 시험은 35·36 각 1문항의 격리 fixture이며 중앙 PostgreSQL 실제 70문항의 장시간 사용과 PDF/MP3 실미디어는 이번 검증 범위 밖이다.
- 장기 목표 재평가: (1) **UI/UX:** 첫 동선·모바일 레이아웃 유지, 감사 정보의 시각적 우선순위 개선; (2) **의사결정:** 발견 단서·원본 출처·불확실성·사람 상태를 분리하여 우선 목표 해결; (3) **구조 간소화:** 기존 F3 fast path 유지, on-demand 상세 원칙과 과거 run 구분, 단순 렌더링. **다음 우선순위 후보**는 35회 원본 PDF/듣기 음원·공유 구간과 36회 실제 파일을 로컬에서 연 장시간 사용자 흐름 검증, 그 다음 bundle/개별 상세 API 비용 계측·중복 제거다.
