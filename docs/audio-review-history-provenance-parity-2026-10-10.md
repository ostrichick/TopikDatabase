# 35회 음원 사람 검증 선언: 상세 화면과 F3의 이력 제한 불일치 (2026-10-10)

## 범위와 재현

기준 `5f80abf`, 변경 전 clean 단일 워크트리. 완료된 음원 검증 계약·클립 접근 차단·35회 공유 쌍·질문/대본 별도 승인·409/CAS·F3 일괄 조회를 유지했다. 원본 SQLite/MP3와 운영 PostgreSQL은 쓰지 않았다. 아래 검증 선언과 추출 이력은 모두 **격리 테스트 사본에만 삽입한 합성 기록**이다. 실제 사람이 청취·확인·승인했다는 뜻이 아니다.

`ReviewStore._audio_info`의 개별 상세 조회는 최근 `review_records` 음원 이력 **8건만 조회**한 뒤, 해당 결과 내에서 가장 최근 `manual_audio_boundary_35/verified` 선언을 검증했다. 같은 문항에서 클립 추출 이력이 아홉 번 이상 후속 저장되면 유효한 원래 사람 검증 선언은 최근 8건에서 밀려났다. 반면 F3 `get_questions_bundle()`과 MP3 export/serve 경로는 **이력 전체에서 최신 해당 선언**을 찾았다.

따라서 동일한 25/26 공유 구간에 대해 상세 `human_evidence=null`, `clip_provenance_confirmed=false`, 링크 없음/추출 버튼 비활성인 반면 F3는 명시적 선언/공유 근거/클립 링크를 유지하는 **화면·API 간 오판**이 가능했다. 변경 전 실제 격리 SQLite 복제본에 동일한 테스트를 추가해 `None != {note: 'SIMULATED EVIDENCE ONLY: …', ...}`로 실패함을 확인했다. 이는 상태·판단 근거 노출의 불일치이며 실제 운영에서 이러한 이력 누적이 이미 일어났다는 주장은 아니다.

## 수정

- 상세 화면은 최근 8건을 기존처럼 **표시용 타임라인**으로만 유지한다. 해당 8건 내에 검증 선언이 없고 저장된 segment status가 `verified`라면, 기존 `_require_audio_evidence`를 재사용하여 감사 로그 전체에서 최신 해당 선언을 한 번 더 조회해 현재 경계·원본 SHA·공유 관계와 대조한다.
- 최근 검증 선언이 **이미 8건 안에 있는 일반 경로**에서는 추가 SQL 호출이 없다. 전체 이력 보충 조회는 검증 상태이지만 최근 타임라인에 해당 선언이 없을 때만 발생한다. F3 일괄 조회의 기존 쿼리와 페이로드는 변경하지 않는다.
- 최신 선언이 손상·불일치한 경우 **더 오래된 유효 선언으로 되돌아가지 않는다.** 공유 쌍 한쪽이 불일치하면 양쪽 링크·추출이 계속 차단된다. 자동 청취 텔레메트리/고정 reviewer 문자열을 인증된 사람 증거로 간주하지 않는다.
- 저장/스키마/원본/기존 승인 이력과 36회 관계는 변경하지 않았다. 구현 변경은 `src/review_ui.py`의 상세 조회 코드, 회귀는 `tests/test_review_ui_postgres_clip.py`에 한정했다.

## 검증

- 집중 회귀: **39/39 PASS** (`tests.test_review_ui_postgres_clip`, `tests.test_audio_segments_api`, `tests.test_audio_evidence_review`, `tests.test_review_ui_flow`). 신규 테스트는 25/26 공유 MP3 클립을 **격리 DB + 합성 사람 선언 + fake encoder**로 준비하고, 아홉 건씩 더한 `audio_export_35` 이후에도 상세/F3의 선언·공유 provenance·링크가 같고 표시용 이력은 8건임을 확인한다. 이후 26의 더 최신 선언을 일부러 잘못 기록하고 추출 이벤트로 밀어내도 최신 손상 선언을 우회하지 않고 두 문항 링크가 차단되는 것도 확인한다.
- 새로 프로비저닝한 **localhost disposable PostgreSQL 17.11**: 동시 쓰기/공유 쌍/CAS/스냅샷/35·36 격리·기존 클립 근거 차단 등 **11/11 PASS**; runner가 `STOPPED=True`, `REMOVED=True`를 보고했다. 변경 코드를 대상으로 하는 신규 이력 누적 회귀는 **PG 매핑행 더블 + SQLite 테스트 사본**에서 수행했다. 해당 추가 사례를 실제 PG17의 새 통합 테스트로 실행한 것은 아니며 실제 운영 PostgreSQL에도 접근하지 않았다.
- 실제 Chromium headless shell, 분리된 원본 35회 SQLite **백업 사본**에만 두 문항의 합성 검증 선언과 추출 이력을 넣어 UI를 확인: **320 / 390 / 1366px 모두 PASS**, 각 GET 6, POST 0, 콘솔 JS 오류 0, horizontal overflow 없음, 본문 16px. 개별 25번 화면에 `사람이 입력한 검증 선언 (청취 사실은 독립 검증되지 않음)` 표시 및 서버의 `explicit_declaration=true`, `clip_provenance_confirmed=true`, 이력 8건 확인. 별도 MP3 실제 청취 테스트가 아니며 사용자의 사람 검증을 대체하지 않는다. 결과 `.stage9-runtime/audio_history_{320,390,1366}.png`, `audio_history_ui_smoke.json` (Git 제외).
- F3 원본 **70문항**의 불변성: 이전 `5f80abf`와 변경 소스 직접 비교, compact JSON 모두 **183,798 bytes** 및 SHA-256 `e25b08b40509822b3879ea0827c57ea553eba6706c7329c20a11b0afd5636b83` 동일. 각 9회, 끝 8회 중앙값 **13.944ms → 14.340ms**; 소표본 로컬 지터로 성능 차이를 일반화하지 않는다. 결과 `.stage9-runtime/audio_history_f3_parity.json` (Git 제외).
- 최종 전체 `python -m unittest discover -s tests -q`: **357개 OK (skipped=13), 164.179초, exit 0**. 별도 실제 PG17 검증으로 환경 의존 테스트 범위를 보완. Python compile 및 Git diff 체크 통과.

## 세 장기 목표 및 잔여 위험

1. **UI/UX**: 실제 사람이 선언한 검증 근거의 타임라인 표시 제한이 일관성 오류로 변하지 않도록 수정했다. 모든 viewport에서 모바일 가로 넘침과 브라우저 오류를 검사했다.
2. **판단 신뢰성**: F3와 개별 상세의 근거 의미·클립 승인 상태가 같으며, 더 최신 손상 선언의 과거 근거 fallback을 금지했다. 이 선언도 **독립 인증된 검수자 신원·실제 청취 사실의 증명은 아니다**.
3. **구조/프로세스**: 별도 테이블·마이그레이션 없이 기존 감사 계약 재사용, 타임라인은 8건 상한을 유지하고 예외적인 상세 조회에만 1회 보충 query 사용. F3 70문항 대량 조회 비용 변화 없음.

**다음 독립 개선 후보:** `src/review_ui.html`의 F3 검수 표시가 `review_status='verified'`만으로 초록색 ‘인간 검수 승인 완료’를 표시하고 서버의 `last_human_review.approved=false/null`을 무시할 수 있음. 별도 읽기 전용 코드 감사에서 확인됐지만 이 커밋에는 손대지 않았다. 또한 최근 저장 후 상세 검수 이력이 오래된 캐시에 남을 수 있으며, 모든 기록의 `reviewer='local_reviewer'`는 인증된 개인 신원이 아니다. 인증·권한 경계는 사용 방식과 계정 정책 확정 없이 임의 구현하지 않았다. 운영 DB/서비스·원본 수정·원격 push·배포 없음.
