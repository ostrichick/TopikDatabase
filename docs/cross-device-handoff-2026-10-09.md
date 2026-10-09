# TOPIK Database — 다른 PC 작업 인수인계 (2026-10-09)

## 프로젝트 점검 후속 작업

F1(35회 상세 조회 snapshot)은 커밋 `7b29a25`에서 수정·검증했다.
**다른 컴퓨터에서 F2–F5를 이어서 수정할 때는
`docs/audit-followup-handoff-2026-10-09.md`부터 읽는다.**
이 문서에는 pull 절차, 재현 조건, 구현 순서, 검증 기준과 작업 경계가 있다.
기존 검수 서버는 재시작하지 않았으므로 Git pull과 실행 중 서버 반영은 구분한다.

## Git 코드 이동

2026-10-09 TOPIK 35/36 다회차 리뷰, 36회 PDF-first 추출과 구두점 v6,
승인/다음 문제 이동 UI 개선은 `feat/topik36-pdf-ingestion-20261009` 브랜치에 있다.
이 브랜치를 GitHub에 올린 뒤 다른 PC에서 다음 순서로 가져온다.

```powershell
cd C:\Projects\TopikDatabase
git status --short --branch
git fetch origin
git switch --track origin/feat/topik36-pdf-ingestion-20261009
```

이미 같은 이름의 로컬 브랜치가 있으면 `git switch feat/topik36-pdf-ingestion-20261009`로
전환하고 `git pull --ff-only`를 실행한다. **기존 PC에서 커밋되지 않은 변경이
있으면 먼저 확인**하고, 강제 checkout/reset을 실행하지 않는다. `main` 브랜치는
별도 통합 전까지 위 기능 브랜치와 다를 수 있다. 현재 버전 확인은
`git log -1 --oneline` 명령을 사용한다.

## GitHub에 포함되지 않는 기기별 파일

`.gitignore`는 `topik-past-papers/` 전체와 PDF, MP3, 로컬 DB 백업,
개인별 접속 설정을 제외한다. 따라서 Git fetch/pull만으로 **기출 미디어나
`derived/036-I-B/staging-v5.json`, `staging-v6.json`이 이동하지 않는다**.
이미 PC에 있는 기출 자료는 그대로 보존하고, 누락된 파생 파일은 그 PC의
검증된 PDF 자료 및 설치된 추출 의존성으로 다음 명령을 실행해 생성한다.

```powershell
py -3 -B -m src.pdf_ingest_36
py -3 -B -m unittest tests.test_punctuation_pipeline_v6 tests.test_audit_35_punctuation_v3 tests.test_migrate_36_punctuation_v6 -q
py -3 -B -m unittest discover -s tests -q
```

36회 `staging-v5.json`은 기존 기준선으로서 덮어쓰거나 교체하지 않는다.
위 추출 명령은 기존 v5 파일이 있으면 바이트 단위로 대조하고,
없으면 검증된 원본 PDF에서 동일한 동결 규칙으로 생성한다. 원본 PDF나
파생 데이터가 고정된 SHA 검사에 실패하면 진행하지 않는다. 기기별
`..\TopikDatabase-runtime\operational.json`, SSH 키, TLS 인증서와
`TOPIK_MEDIA_ROOT`는 Git으로 동기화하지 않는다. PostgreSQL은 기존 중앙
DB를 사용하며 원본 PDF/MP3는 각 기기에 유지한다.

## 현재 기능·운영 적용 경계

- 35회 기존 원본, SQLite 동결 보관본, 검수/승인 이력과 운영 DB는 보존한다.
  화면 미리보기에만 35회 **3개 표시 필드/4개 공백** 교정 후보를 제공한다.
- 36회 v6는 동결 v5 기준선과 비교해 **33개 필드/109개 공백**을 교정한
  파생본이다. 화면은 교정값을 미리보기로 제공하고, 저장 입력은 기존
  DB 원문을 사용한다.
- 2026-10-09 마지막 읽기 전용 검사에서 운영 36회는 v5 **502/502 필드
  동일**, 승인된 문제 9개(L001–L009)와 검수 이력 9건이었다. v6 운영 DB
  적용 상태는 `pending`이었다. 이 수치는 이후 검수에 따라 달라질 수 있다.
- `scripts/migrate_36_punctuation_v6.py`는 기본 실행 시 **읽기 전용**.
  실제 `--apply`를 실행하려면 **현재 검수 이후에 생성한 완전한 DB 백업**,
  SHA 일치, **별도 DB 독립 복원 검증과 manifest**, 검수 상태 스냅샷의
  일치, 기존 승인 텍스트 변경 검토가 선행되어야 한다. 마이그레이션은
  원자적 잠금·CAS·롤백·버전 증가와 승인 이력 불변 검사를 포함한다.
- 35회 운영 DB의 문장부호를 배치 수정하지 않는다. 36회도 위 조건을
  충족하기 전에는 운영 `--apply`를 실행하지 않는다.
- 앞선 문서 기준 PC의 Stage 10 물리적 SQLite `freeze`/`verify`는
  미완료였다. 해당 PC에서 실제 보관 파일/검사 상태를 다시 확인한다.

다른 PC에서 검수 UI를 시작할 때는 준비된 기기별 런타임을 사용한다.

```powershell
powershell -NoProfile -File scripts/start_postgres_review.ps1
```

참조: `docs/topik35-36-punctuation-v3-results-20261009.md`,
`docs/text-extraction-punctuation-policy.md`,
`docs/stage10-sqlite-freeze-2026-10-07.md`.
