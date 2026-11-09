# gradual_v1 재평가와 보고서

바탕 화면의 **SleepWell Academic Workbench** 바로가기는 이 설치 폴더의 `launch.cmd`를 실행합니다. 앱을 새로 실행한 뒤 **TRIPOD·PROBAST 점검** 메뉴를 열면 성능 재평가, 기준별 점검·보고서, 추가로 필요한 근거 탭을 사용할 수 있습니다.

## 저장된 결과 보기

1. **현재 모델 평가 불러오기**는 설치본에 포함된 2026-11-03 재평가 집계 결과를 엽니다. 기존 CMI/MESA 자료를 다시 사용한 후향 평가입니다.
2. **근거를 연결해 점검 만들기**는 현재 SleepWell 모델에 대한 근거 기반 초안을 만듭니다. 새 프로젝트의 일반 모델을 평가할 때는 **새 점검 만들기**로 빈 체크리스트부터 시작합니다.
3. TRIPOD+AI 52개 항목과 PROBAST+AI 34개 질문을 선택해 판정과 근거를 기록하고, 판정자 이름을 입력한 뒤 **새 버전으로 저장**합니다. 기존 버전은 보존됩니다.
4. **근거 문서 연결**로 방법 보고서나 기관 확인 문서를 첨부할 수 있습니다. 선택한 항목과 문서의 해시가 연결됩니다.
5. **근거·기록 검증**은 파일·해시·허용 응답·판정 기록의 일관성을 확인합니다. **보고서 패키지 내보내기**는 Markdown, DOCX, PDF, JSON, CSV와 해시 목록을 생성합니다.

자동 검사 통과는 TRIPOD 충족 인증, PROBAST 저위험 판정, 임상 성능 인증을 뜻하지 않습니다. 근거가 없는 사항은 미보고·확인 불가·정보 없음으로 남겨야 합니다. 독립 평가자의 판단은 별도입니다.

## 동일 자료 재계산

GUI의 **평가 입력 CSV**에서 정규화된 입력을 선택한 다음 **gradual_v1 재평가 실행**을 누릅니다. 새 결과는 해당 프로젝트의 `research/evaluations/gradual_<고유번호>/`에 기록됩니다. 계산 중 화면 탐색과 취소가 가능하며, 완료되지 않은 결과를 보고서 근거로 가져오지 않습니다.

입력 필수 열:

`cohort,participant_id,split,row_id,cutoff,bed,wake,observed_at,bed_offset,wake_offset`

- `cohort`: `cmi` 또는 `mesa`. `split`: 기존 `train`, `validation`, `test`.
- 시각은 오프셋을 포함한 ISO-8601 형식이며 cutoff는 현지 20:00입니다. 목표값은 cutoff로부터의 분입니다.
- 한 참여자는 한 분할에만 속해야 합니다. 참여자의 첫 cutoff가 가입 기준 시점이며, 과거 수면과 관측 시각이 cutoff보다 이른 이력만 예측에 쓰입니다.
- 기존 Test 예측 재현을 확인하려면 `legacy_{data,hybrid,physical}_{bed,wake}` 여섯 열에 저장된 참조값을 제공해야 합니다. 임의의 새 외부 자료를 이 재현 전용 기능에 넣어서는 안 됩니다.
- `age`, `sex`, `label_source`, `rare_by_train_mad`는 실제로 확인된 값만 사용합니다. 인구통계 변수를 추정해 채우지 않습니다.
- 입력 CSV와 `private_predictions.csv`는 개인별 연구 자료입니다. 공유용 집계 JSON·CSV·보고서와 분리해 로컬에 보관합니다.

CLI에서 원 parquet를 읽어 CSV를 준비할 때는 pandas와 parquet reader가 있는 기존 Python 환경을 사용합니다. 이 컴퓨터의 `C:\Python312\python.exe`에 필요한 라이브러리가 있습니다. GUI 평가에는 parquet reader가 필요하지 않습니다. 기존 파일을 덮어쓰지 않는 새 경로를 지정합니다.

```powershell
C:\Python312\python.exe -X utf8 -B scripts/reevaluate_gradual.py --prepare `
  --legacy-root 'E:/학술제/데이터모델/연구용데이터/next_night_run_20261020_v1' `
  --robust-root 'E:/학술제/데이터모델/연구용데이터/robust_next_night_run_20261020_v2' `
  --input-csv '<새 폴더>/normalized_input.csv'

.\.venv\Scripts\python.exe -X utf8 -B scripts/reevaluate_gradual.py `
  --input-csv '<새 폴더>/normalized_input.csv' `
  --output-dir '<새 결과 폴더>' --bootstrap-replicates 1000 --seed 20261103
```

모델을 재학습하거나 Test 결과로 선택하지 않습니다. 원 모델·분할·행을 고정하고 `legacy14` 재현을 먼저 확인한 뒤 `gradual_v1`을 계산합니다. 참여자 균등 가중치와 밤 균등 가중치를 별도 보고합니다. 95% 신뢰구간은 참여자 군집 부트스트랩이며 고정된 모델 조건에서의 불확실성입니다. Validation 잔차로 만든 탐색적 구간은 Test에서 경험적 포함률만 확인하며, 개인별 95% 보장 구간으로 배포하지 않습니다.

## 새 근거가 필요한 범위

- 새 참여자 또는 아직 열어보지 않은 기간의 동결 평가와 실제 사용환경의 기록 도착 시각.
- 목표 사용자 집단의 대표성, 현재 없는 인구·임상 특성, 충분한 하위집단 표본.
- 생리학적 수면을 주장하려는 경우 적절한 기준 측정과 ACT/event·일지 라벨 비교.
- 수면 개선 효과를 주장하려는 경우 별도의 비교·중재 연구.
- IRB/면제·동의·이차 이용 허가, 연구비·이해관계·등록·이용자 참여 등 연구자와 기관이 확인해야 하는 사실.

기존 Test를 다시 사용했다는 사실은 문서나 소프트웨어 변경으로 해소되지 않습니다.
