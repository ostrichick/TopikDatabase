# 35회 음원: 레거시 검증 상태의 클립 추출·제공 근거 일치 (2026-10-10)

## 기준선, 문제, 정책

시작 Git `bf44f85` clean, 단일 기존 워크트리. 완료된 F2–F5/F3, AI 판단 패널, 원본 탐색, 공유 대본/오디오 CAS, 격리 PG17 테스트와 35/36회 격리를 유지했다. **실제 사람이 해야 할 MP3 청취·시각 판단·근거 입력은 수행하지 않았다.** frozen 35회 SQLite/MP3는 읽기 전용이고 운영 PostgreSQL에는 접속·쓰기·배포하지 않았다.

`bf44f85`는 명시적 `human_evidence` 선언을 새 검증 저장에 요구하고 기존 근거 없는 verified 기록은 UI에서 미확정으로 표시했지만, 다음 **파일 액세스 경로**는 `audio_segments.status='verified'`만 확인했다.

- `ReviewStore.export_audio_clip`: status-only 레거시 검증 행도 원본 MP3로 클립 인코딩, 새 파일/음원 이력 작성 가능.
- `ReviewStore.clip_path`: 존재하는 클립 파일의 직접 HTTP GET `/media/.../clip`이 status-only로 허용.
- 개별 `get_question` 및 F3 `get_questions_bundle`: 기존 파일의 URL을 청취 근거 없이 안내 가능.
- UI `audioExport`: `verified`이면 사람 선언 근거가 미확정이어도 추출 버튼 활성화 가능.

따라서 기존 DB에 레거시 verified 행이 있다면 사람의 음원 경계 검증 근거 없이도 **검증된 MP3라는 외관의 결과물**을 만들거나 다운로드할 수 있었다. 이 결함은 코드를 통한 **가능한 경로의 재현**이며, frozen 35회 실제 세 공유쌍은 여전히 자동 `candidate` 상태이므로 운영에서 이미 오용됐다고 주장하지 않는다.

## 조치

1. `_audio_attestation`과 `_require_audio_evidence`로 서버가 각 음원 구간의 **가장 최근 명시적 사람 검증 이력**을 확인한다. 선언의 필수 체크·20~2000자 메모, 구간 시작/종료 및 verified 전환, 원본 SHA-256, 등록된 공유 쌍 관계가 현재 구간과 일치해야 한다. `review_records.reviewer='local_reviewer'`는 인증된 신원이 아니므로 별도 개인 신원으로 주장하지 않는다. 자동 재생 완료는 사람 검증의 증거로 사용하지 않는다.
2. 공유 쌍에서는 양쪽 행의 status/시각/version/source/clip identity와 **양쪽 별도 선언**이 일치해야 파일 접근 가능. 한쪽의 근거가 사라지거나 모순되면 양쪽 모두 클립 링크를 숨기며 직접 GET도 거부한다.
3. `clip_path`(직접 파일 GET), `export_audio_clip`(인코딩 시작 전에 사전검사 + 원자적 링크 트랜잭션 내부 재검사), `_audio_info`(개별 상세 URL), F3 bundle(기존 조회된 audio audit batch 활용, 추가 per-question DB 요청 없음), 프런트 추출 버튼을 **같은 근거 정책**으로 정렬했다. 서버는 `clip_provenance_confirmed`라는 명시적 boolean을 반환한다. 한쪽의 본인 선언이 있더라도 짝 문항의 선언·version/구간이 불완전하면 두 화면 모두 false이며 추출 버튼이 비활성화되고 이유가 안내된다.
4. 기존 `audio_segments.status`나 승인/검수 이력을 자동으로 수정·삭제하지 않았다. status가 verified라도 명시적 근거가 없으면 **'기존 검증 표시 · 사람 청취 근거 미확정'**으로 남고 클립 추출·제공만 차단한다. 실제 사람이 새 구간을 검증하기 전에 기존 근거를 가짜로 채우거나 과거 승인 기록을 재작성하지 않는다.
5. DB 마이그레이션 및 새 인증 시스템 불필요. 과거 Stage-8 클립 테스트의 `UPDATE audio_segments SET status='verified'` 단독 설치는 이제 비정상으로 판정되므로, **격리 SQLite 복사본 테스트의 초기 준비에만** `SIMULATED EVIDENCE ONLY` 사람 선언 레코드를 추가했다. 이는 실제 청취 기록이 아니다.

## 검증과 성능

- SQLite/가상 PG 정상 클립 추출·재물질화·공유 일치·실패 롤백 **38개 집중 테스트 통과**. 레거시 status-only 행에 대해서는 재생 파일이 이미 존재하더라도 export·direct clip_path·UI URL·F3 URL 차단, 기존 파일/DB 무변경. 짝 문항 한쪽만 선언 누락해도 양쪽 클립 링크/다운로드 차단 테스트 추가.
- 실제 **격리 PostgreSQL 17.11**: 기존 동시 편집·409·트랜잭션·F3 스냅샷 및 status-only verified 차단을 포함 **11개 테스트 통과**. 매 실행 새 localhost 클러스터/nonce DB 사용, 종료 후 서비스 중지/디렉터리 삭제 자동 검증.
- Chromium 실제 35회 원본 SQLite의 **분리된 사본**에만 25·26번 `verified` 값을 인위적으로 부여해 레거시 상태를 재현. 320/390/1366px 각각 API GET 5회, 저장 POST 0회, 추출 버튼 비활성·링크 숨김·직접 GET HTTP 400, `clip_provenance_confirmed=false`, JavaScript 오류 0, 16px, 가로 넘침 없음. 마지막 자동 실행 시간은 각각 **665/456/353ms**의 짧은 화면 진입 샘플이며 사람의 실제 청취·판단 시간이나 운영 성능으로 일반화하지 않는다. `.stage9-runtime/clip_legacy_{320,390,1366}.png`, `clip_legacy_browser_metrics.json`은 Git 제외.
- 실제 frozen 35회 70문항 F3 묶음 조회를 구버전 `bf44f85` vs 새 코드로 각 9회 비교: **전 항목과 JSON 191,103 bytes 동일**, 마지막 8회 소표본 중앙값 **20.74ms → 19.65ms**. 노이즈가 있으므로 성능 개선을 주장하지 않으며 추가 API나 per-question DB 쿼리는 도입하지 않았다. `.stage9-runtime/clip_provenance_benchmark.json` 참조.
- 최종 전체 회귀 `python -m unittest discover -s tests -q`: **356개 실행, OK (skipped=13), 종료 코드 0, 177.680초**. `skipped`에는 별도 DB 환경이 없을 때 자동 생략되는 PostgreSQL 통합 케이스가 포함되어 있으며, 해당 격리 클러스터의 실제 **11/11** 결과는 별도로 확보했다. 일부 AI 집계 미가용/시간초과 로그는 테스트의 오류 주입·폴백 경로에서 발생했으며 테스트 실패는 아니었다. Python `py_compile`, 인라인 JS `node --check`, `git diff --check`도 통과.

## 제약, 세 장기 목표, 다음 과제

- **UI/UX:** 미확정인 레거시 verified 행에도 추출이 가능한 모순을 제거했다. 320/390/1366px에서 버튼 상태·링크·가로 스크롤을 확인했다.
- **판단 정보 신뢰성:** 실제 음원 검증 선언·원본 SHA·짝 문항 provenance가 완전한 경우에만 '검증된 클립'을 내보낸다. 기존 상태는 변경하거나 추정하지 않는다.
- **프로세스 단순화:** 동일한 typed audit 검사 함수를 파일 생성/직접 다운로드/UI 응답에 재사용. 별도 테이블·36회 마이그레이션 없이 검증 가능하다.

**다음 비인간 개선 후보:** 감사 기록의 `local_reviewer`는 고정 문자열로 실제 검수자를 식별·인증하지 않는다. 개인 신원 도용을 막는 인증 계약, 운영 권한과 접근 방법을 먼저 확정하고 세션·계정의 실제 구분을 갖춘 감사 주체 식별 체계를 설계해야 한다. 현재 독립적인 본인 확인 수단이 없는데도 로그인을 완료했다거나 실명 인증됐다고 기록하지 말 것. 사람의 청취 판단 작업은 여전히 별개이며 후보를 자동 승인하지 않았다.
