# 웹앱 우선 구현·검증·실행 안내

> 2026-10-09 당시 MESA inventory와 경량 모델 연결 단계의 기록입니다. 현재는 robust CMI/MESA 휴대형 모델, 점진 개인화, 전향 잠금, 연구 분석실이 추가됐습니다. 현행 계약은 [CURRENT_MODEL_STATUS_2026-11-01.md](CURRENT_MODEL_STATUS_2026-11-01.md)와 `../web-app/README.md`를 우선합니다.

2026-10-09. 분석은 [MESA 활용·전체 프로젝트 계획](MESA_WEB_PLAN_2026-10-09.md)을 참조한다.

## 이번에 구현한 것

| 파일 | 내용 |
|---|---|
| `tools/model/mesa_inventory.py` | 파일·고유 사람·모달리티 수, bounded 헤더/내용 QC, 업로드 변동 감지. 원본 변경/전송 없음 |
| `tools/model/test_mesa_inventory.py` | 수량·ID 정규화·링크 제외·부분/변동 파일·출력 경로 테스트 |
| `web-app/forecast_model.py` | 외부 학습 환경 없이 JSON 선형 행동 모델을 추론하는 계약 |
| `web-app/research.py` | 기존 연구 경로와 경량 모델 선택, 모델 거절 사유, 비식별 MESA 집계 로더 |
| `web-app/server.py` | `--forecast-model`, `--mesa-inventory`, 상태 API, Windows SQLite 연결 종료 수정 |
| `web-app/static/index.html`, `app.js` | 가입 전에도 열 수 있는 ‘연구 현황’, 모델 연결 상태, MESA snapshot/QC 표시 |
| `web-app/Start-SleepWell.ps1` | Windows 실행 진입점 |
| `web-app/tests/test_forecast_model.py`, `test_http.py` | 입력 계약·정보 시점·자정·대상군·손상 모델·HTTP 저장 경로 검증 |

기존 연구 모델의 동적 Python import/선택 의존성 경로는 호환을 위해 남아 있다. 새 경량 모델은 별도 계약이며 기존 Android Ridge JSON을 이름만 바꿔 넣을 수 없다. 이번에는 실제 MESA 학습 계수나 가짜 `trained` 모델을 만들지 않았다.

## 이번 MESA 스냅샷

파일: [집계 JSON](MESA_INVENTORY_2026-10-09.json).

- 2026-10-09 10:36:30–10:36:37 KST 점검.
- 파일 1,682개 / 파일명 기준 고유 참여자 1,682개 / 약 2.846 GB. 모두 ACT CSV.
- 헤더 읽기 1,682개 중 동일한 17열 1,681개, 읽을 수 없는 헤더 1개. 업로드 중 1개 파일의 크기/수정시각 변동 감지.
- 결정적 간격 표본 24개 중 안정적으로 읽은 23개, 610,930행만 내용 QC에 포함. 변동 파일 1개 제외.
- 표본 유효 인접 간격의 100%가 30초. 검사한 필드의 형식 위반 0.
- 표본의 offwrist=1 비율 26.48%, `wake` 결측 161,231행. 비착용/제외 구간을 수면·각성 또는 활동 0으로 대치하면 안 된다.
- PSG/단계 XML/HR/steps 파일 0. 이 집계는 헤더/표본 내용 검사이며 전체 파일 무결성·신호 정렬·학습 가능성 인증이 아니다.

## Windows에서 실행

현재 저장소 루트(`E:\학술제\기타\SleepWell-integrated`)에서:

```powershell
python -X utf8 web-app/server.py --mesa-inventory docs/MESA_INVENTORY_2026-10-09.json
```

브라우저: <http://127.0.0.1:8765>. **연구 현황**은 가입 없이 열 수 있다. 일지·추천은 시작 정보를 입력한 뒤 사용한다. 다른 PC/휴대폰에서 접속하는 공개 웹 서비스는 아니다.

PowerShell 실행 정책이 허용된 환경에서는 다음 런처도 사용할 수 있다. 정책을 변경할 필요는 없으며 위 Python 명령을 기본 대안으로 둔다.

```powershell
.\web-app\Start-SleepWell.ps1
```

서버를 종료하려면 실행한 터미널에서 Ctrl+C. 실제 프로필·일지 DB는 `web-app/.local/sleepwell.sqlite3`에 보존된다. 검증용 가입/일지 데이터는 단위 테스트의 임시 DB만 사용했다.

### 데이터 추가 후 점검 갱신

```powershell
python -X utf8 tools/model/mesa_inventory.py --root "../데이터셋/MESA" --output web-app/.local/mesa_inventory.json --sample-files 24
python -X utf8 web-app/server.py
```

서버 시작 때 집계 파일을 한 번 읽는다. 업로드마다 실시간 자동 재학습하지 않는다. 갱신 후 서버 재시작으로 새 집계가 반영된다. 기본 검사 범위는 모든 파일 목록, 최대 10,000 CSV 헤더, 선택 파일당 최대 100,000행이다. 행 제한을 초과하거나 읽는 동안 바뀐 파일은 내용 집계에서 제외한다.

## 모델 계약

모델 제공 시:

```powershell
python -X utf8 web-app/server.py --forecast-model "검증한_모델.json"
```

실제 학습 JSON은 아직 없다. 계약은 `forecast_model.PORTABLE_MODEL_CONTRACT`와 `FEATURE_ORDER/FEATURE_UNITS`가 코드 기준이다. **계약에 맞는 export trainer는 후속 학습 단계의 산출물**이며 기존 `tools/model/train_ridge_residual.py`는 이 새 계약의 exporter가 아니다.

필수 메타데이터:

- `schema_version=1`, `artifact_type=sleepwell_behavior_clock_linear`, `feature_contract=web_completed_history_pre20_v1`.
- `status=trained`, `model_version`, `decision_time=20:00`, `timezone=Asia/Seoul`.
- `release_gate.status=evaluation_passed`, `validation_protocol`. 이는 제공된 파일의 평가 선언을 확인하는 조건이지 로더가 평가 성능/임상 효과를 새로 증명하는 것은 아니다. 실제 승격은 보고서·holdout 결과 검토가 선행되어야 한다.
- `population.source`, `age_min/age_max`, `occupations`; 현재 프로필과 맞지 않으면 해당 모델 사용 거부.
- `minimum_history_records`(1–7), 정확한 특징 순서/단위, median/mean/양수 scale, 두 target의 유한 intercept/coefficients.

고정 특징 6개는 과거 취침 offset 중앙값, 과거 기상 offset 중앙값, 과거 수면구간 길이 중앙값, 직전 수면구간 길이, 유효 기록 수, 예측일 주말 여부다. 시각은 현지 20시 이후 offset **분**, 길이는 **분**이다. 여기서 길이는 `wake−bed`로 침대/기록 구간 길이이며 밤중 각성을 뺀 생리적 실제 수면시간과 다르다.

완료 기록의 기준:

- `source=diary/healthkit`, `wake < cutoff`, `observed_at < cutoff`.
- 최근 28일 기상 기록만, 기상 날짜별 마지막 관측 1개, 최대 7회.
- 취침 offset 0–960분, 기상 offset=취침 offset+구간 길이, 최대 1,440분인 지원 야간만.
- `observed_at` 없는 과거 archive나 나중에 입력한 기록은 이전 결정시각의 특징에 사용하지 않음.

JSON은 최대 1 MiB, UTF-8, 중복 키·NaN·Infinity 거부. 모델 없거나 기록 부족/대상군 불일치/지원 범위 밖 출력이면 사유를 반환하고 기존 일정 추천은 계속 제공한다. 새 모델 예측은 권장 시간을 덮어쓰지 않으며 보조 행동 예상으로 제공한다.

## 검증 명령

```powershell
python -X utf8 -m unittest discover -s web-app/tests -v
python -X utf8 -m unittest discover -s tools/model -p "test_*.py" -v
python -X utf8 -m compileall -q web-app tools/model
node --check web-app/static/app.js
git diff --check
```

최종 확인: **웹 20/20, MESA 점검 6/6, 기존 Ridge 3/3 = 총 29개 테스트 통과**. Python compileall, JavaScript 구문 검사, git diff 공백 검사 통과. 기존 Ridge에는 pandas `Timestamp.utcnow`의 향후 제거 경고가 있으나 테스트 실패는 아니다.

실제 서버 `/`·`/app.js` HTTP 200, 연구 현황 HTML/스크립트 제공을 확인했다. `/api/state`는 `model_status=unavailable`, `data_status=ingestion_active`, `file_count=1682`, 읽기 불가 헤더 1개, 안정 표본 23개/610,930행을 반환했다. 모델이 없는 상태를 학습 완료로 표시하지 않는 경로도 확인했다. 사용자의 기존 DB에는 시험용 프로필/일지를 추가하지 않았다.

자동 브라우저 제어에 연결된 브라우저가 없어 픽셀 렌더링·실제 클릭 검증은 수행하지 못했다. HTML 제공, JavaScript 구문, HTTP 워크플로 검증과 시각 검증을 구분한다. Android는 Java 17/Kotlin 도구, iOS는 Xcode/실기기가 필요하여 이번 Windows 검증 범위에 포함하지 않았다.

## GitHub 상태

origin은 요청한 `https://github.com/han-seo-yun/SleepWell-integrated`다. 작업 시작 전부터 `.gitignore/README` 수정, 웹/iOS/문서/워크플로 미추적 변경이 있었으며 그대로 보존했다. 새 작업은 해당 로컬 checkout에 구현했다.

원격 확인은 인증 요청으로 실패했다. **commit/push/공개 배포는 하지 않았다.** 인증이 갖춰진 후 코드·문서·집계 파일을 검토해 반영해야 한다. ACT 원본, 개별 식별자, 사용자 DB, 모델 학습용 개인자료는 반영 대상이 아니다.

## 남은 핵심 작업

1. 안정된 MESA snapshot과 라벨 코드북 기반 ACT 정규화.
2. PSG/단계 정답 확보·정렬 후 독립 평가.
3. 일지용/센서용 baseline 및 Hybrid 학습, 사람/시간 분리·개선 불확실성 검증.
4. 검증된 새 계약 모델 export 및 실제 앱 대상군 평가.
5. 추천 실행·다음날 피로/개운함을 연결하는 전향 파일럿.
