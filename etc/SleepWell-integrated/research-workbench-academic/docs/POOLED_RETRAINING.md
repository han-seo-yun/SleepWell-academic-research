# CMI·MESA 통합 재학습 — 2026-11-04

SleepWell Academic Workbench의 `AcademicQueue`에서 실제 적합·모델 선택·평가를 실행했다. 완료 실행 ID는 `31967b34-08ef-4a87-b655-e649832d34f2`이다. Samsung은 포함하지 않았다.

검증 자료에서 선택한 통합 데이터 모델의 시험 joint MAE는 CMI **48.04분**, MESA **68.84분**, 출처 균등 전체 **58.44분**이다. 기존 출처별 데이터 모델은 각각 47.49/68.11/57.80분이다. 통합 모델의 개선은 확인되지 않아 설치된 기존 모델을 교체하지 않았다.

- [상세 결과·그림·한계](../../reports/cmi_mesa_pooled_training_20261104/REPORT.md)
- [Workbench 결과 열기](../launch_cmi_mesa_results.cmd)
- [완료 프로젝트](</E:/학술제/데이터모델/연구용데이터/cmi_mesa_pooled_workbench_20261104.swa>)
- [평가 JSON](</E:/학술제/데이터모델/연구용데이터/cmi_mesa_pooled_training_20261104_v1/evaluation.json>)
- [선택된 통합 모델](</E:/학술제/데이터모델/연구용데이터/cmi_mesa_pooled_training_20261104_v1/pooled_data.joblib>)

## Workbench에서 보기·재실행

`launch_cmi_mesa_results.cmd`로 이번 프로젝트를 열고 **모델 성능 비교**에서 완료된 통합 재학습을 선택한다. `joint_mae_min`이 입면·기상 평균 절대오차이며 낮을수록 좋다. CMI/MESA는 각각 참여자 균등 평균, 전체는 두 출처에 각각 50%를 주는 평균이다. 상세 신뢰구간·짝지은 차이와 진단 그림은 위 결과 보고서에서 확인한다.

**데이터·실험 설정 → CMI·MESA 통합 재학습**을 누르면 SQLite 입력과 새 결과 폴더를 선택할 수 있다. 기존 수면 후향 재평가와 구분된 실제 재학습 기능이다. 결과는 현재 프로젝트의 불변 완료 실행으로 저장된다. 개인별 시험 예측은 지정한 로컬 출력 폴더에만 저장하고 프로젝트에는 집계 결과를 저장한다.

CLI도 같은 Workbench 대기열과 프로젝트 저장 API를 사용한다. `research_workbench_academic`에서 아래 명령을 실행한다. 재실행 시 기존 경로를 덮어쓰지 않도록 새 `--output`과 `--project`를 지정한다.

```powershell
& .\.venv\Scripts\python.exe -X utf8 -B scripts\retrain_cmi_mesa.py `
  --database 'E:/학술제/데이터모델/연구용데이터/cmi_mesa_unified_20261104_v1/cmi_mesa.sqlite3' `
  --output '<새 결과 폴더>' --project '<새 프로젝트.swa>' `
  --seed 20261104 --bootstrap-replicates 5000 --max-seconds 7200
```

실패·취소된 실행의 부분 산출물은 진단을 위해 남을 수 있다. 같은 폴더를 덮어쓰지 않고 새 폴더로 재실행한다. 완료 여부는 Workbench 실행 상태와 `evaluation.json`·`manifest.json`으로 확인한다.

## 방법과 사용 경계

- Train 1,711명·13,787밤만으로 공통 prior와 전처리·회귀기를 적합한다. 학습 가중치는 코호트마다 절반, 코호트 안에서는 참여자마다 동일하다.
- 공통 수면 이력 33개 특징의 데이터 모델과 여기에 수리 특징 6개를 더한 융합 모델을 만든다. cohort ID, 나이, 성별은 예측 입력으로 쓰지 않는다. 원 vendor의 `gradual_v1` 특징 계산을 재사용한다.
- 각 arm의 Ridge 2종, Huber, HGB 3종을 사전 고정한 뒤 기존 Validation 338명·2,753밤의 출처 균등 joint MAE로 후보와 최종 arm을 선택한다. 모델 파일과 선택 해시를 잠근 뒤 Test 359명·2,848밤을 평가한다. Train+Validation 재적합은 하지 않는다.
- 최종 후보는 두 arm 모두 `absolute15`: absolute-error HGB, 최대 15 leaves, 최소 leaf 40행, learning rate 0.05, 120회, L2=10, 내부 early stopping 없음. 입면·기상용 회귀기 두 개가 하나의 공통 모델 묶음이다.
- `pooled_data.joblib`, `pooled_hybrid.joblib`에는 fitted pipeline, 특징 순서, 공통 prior, 정책, 입력 DB 해시가 있다. 이 실행에서 생성하고 manifest로 검증한 로컬 파일만 사용한다. 기존 portable 모델과는 별도 연구 artifact다.
- 5,000회 참가자 군집 부트스트랩은 동결 모델 조건부 불확실성이다. Test는 이전 연구에서 사용된 자료이며 독립 외부·전향 검증이 아니다. 두 코호트의 측정 방식과 라벨 의미는 보존되며, 통합만의 효과를 분리한 실험으로 해석하지 않는다.

## 검증

핵심 학습·통합·대기열·화면·그림 테스트 36개를 통과했다. 18개 성능 집계를 개인별 예측에서 독립 재계산했고, 기존 모델/기준선 예측 11,392건은 지난 gradual_v1 평가와 최대 차이 0분이다. 새 모델은 저장 후 다시 불러와도 시험 예측 차이 0분이며 입력 DB와 모델·평가 해시를 확인했다. [검증 기록](../../reports/cmi_mesa_pooled_training_20261104/VERIFICATION.json)
