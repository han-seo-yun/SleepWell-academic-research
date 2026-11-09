# SleepWell 문서 안내

최종 정리: 2026-11-01

이 폴더에는 현재 계약 문서와 날짜별 연구 기록이 함께 있습니다. 날짜가 붙은 파일은 해당 시점의 데이터·코드·판단을 보존한 snapshot이며, 이후 구현을 자동으로 반영하지 않습니다.

## 현재 기준으로 먼저 읽을 문서

1. [저장소 전체 안내](../README.md)
2. [현재 모델·개인화·전향 평가 계약](CURRENT_MODEL_STATUS_2026-11-01.md)
3. [웹앱 실행·기능·제한](../web-app/README.md)
4. [모델 입력·출력 계약](interface-schema-모델-입출력.md)
5. [Android 사용자 앱](../android-app/README_소빈.md)
6. [데스크톱 패키징](../desktop/README.md)과 [Windows 사용 안내](../desktop/USER_GUIDE.md)

## 인터페이스와 통합 계약

- [전처리–프론트 인터페이스](interface-schema-전처리-프론트.md)
- [모델 입력·출력 인터페이스](interface-schema-모델-입출력.md)
- [CSV 파일 소스 연동 구조](analysis-csvfilesource-연동구조.md)
- [채윤–소빈 통합 합의안](agreement-채윤소빈-통합합의안.md)
- epoch 전환 기록: [변경 사항](epoch-30s-1min/01_epoch-30s-1min-변경사항.md), [30초 대 1분 검증](epoch-30s-1min/02_검증결과-30s-vs-1min.md), [후속 확인 사항](epoch-30s-1min/03_다음주-확인필요사항-정채윤.md)

## 최신 연구 묶음

- [2026-11-01 현재 모델·평가 계약](CURRENT_MODEL_STATUS_2026-11-01.md)
- [2026-10-25 연구 업데이트](RESEARCH_UPDATE_2026-10-25.md)
- [전체 타당성 검증](FULL_TARGET_VALIDITY_2026-10-25.md)
- [대상 타당성 검증](TARGET_VALIDITY_2026-10-25.md)
- [매일 개인화 검증](DAILY_PERSONALIZATION_2026-10-25.md)
- [추가 데이터·실험](ADDITIONAL_DATA_EXPERIMENTS_2026-10-25.md)
- [팀 모델 인계](TEAM_MODEL_HANDOFF_2026-10-25.md)

## 목적별 연구 기록

- 사전 고정 연구 설계: [RESEARCH_PROTOCOL_2026-10-10.md](RESEARCH_PROTOCOL_2026-10-10.md)
- MESA ACT 파생 라벨 및 공통 연구: [RESEARCH_RESULTS_2026-10-10.md](RESEARCH_RESULTS_2026-10-10.md)
- MESA PSG 수면/각성 분류: [RESEARCH_PSG_RESULTS_2026-10-20.md](RESEARCH_PSG_RESULTS_2026-10-20.md)
- 다음날 입면·기상 행동 예측: [NEXT_NIGHT_RESULTS_2026-10-20.md](NEXT_NIGHT_RESULTS_2026-10-20.md), [NEXT_NIGHT_IMPROVEMENT_2026-10-20.md](NEXT_NIGHT_IMPROVEMENT_2026-10-20.md)
- 앱의 전향적 저장·평가 계약: [PROSPECTIVE_APP_2026-10-20.md](PROSPECTIVE_APP_2026-10-20.md)
- Samsung 개인 기록 감사: [SAMSUNG_NEXT_NIGHT_AUDIT.md](SAMSUNG_NEXT_NIGHT_AUDIT.md)
- 문헌 근거 메모: [LITERATURE_EVIDENCE_MEMO_2026-10-20.md](LITERATURE_EVIDENCE_MEMO_2026-10-20.md)
- 2026-10-25 연구 계획: [RESEARCH_PLAN_2026-10-25.md](RESEARCH_PLAN_2026-10-25.md)

이 결과들은 서로 다른 목적과 정답을 사용합니다. PSG 분류 성능을 다음날 행동 예측 성능으로, 행동 예측 성능을 권장 정책의 수면 개선 효과로 바꾸어 표현하지 않습니다.

## 과거 계획과 인계 문서

`PROJECT_REVIEW_2026-10-08.md`, `MESA_WEB_PLAN_2026-10-09.md`, `WEB_MESA_DELIVERY_2026-10-09.md`, `TEAM_2WEEK_EXECUTION_PLAN_2026-10-10.md`, `SOBIN_SOFTWARE_HANDOFF_2026-10-10.md`는 당시 계획과 인계 내용을 보존합니다. 현재 경로가 필요한 명령은 2026-11-01 분류 구조에 맞게 고쳤지만, 당시의 상태·수치·판단 자체는 소급 변경하지 않았습니다.

## 저장 위치

- 코드와 공유 가능한 집계 문서: 이 저장소
- 새 연구 실행 결과: `E:\학술제\데이터모델\연구용데이터\새_버전_폴더`
- 원자료: `E:\학술제\데이터셋` 또는 `E:\학술제\보조데이터셋`
- 사용자용 배포: `E:\학술제\어플리케이션(사용자용)\배포`
- 연구자용 배포: `E:\학술제\소프트웨어(연구자용)\배포`

원자료와 기존 결과를 덮어쓰지 않고, 새 실험은 새 이름의 폴더에 저장합니다.
