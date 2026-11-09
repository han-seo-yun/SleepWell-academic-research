# CMI·MESA 통합 DB — 2026-11-04

Samsung을 제외한 **2,408명, 19,388개 수면 에피소드**를 하나의 SQLite DB에 저장했다. 원본의 QC 완료 다음날 예측용 자료를 사용했고 원본 파일과 기존 모델은 변경하지 않았다.

|출처|참여자|Train|Validation|Test|전체 에피소드|
|---|---:|---:|---:|---:|---:|
|CMI|269|3,263|691|673|4,627|
|MESA|2,139|10,524|2,062|2,175|14,761|
|합계|2,408|13,787|2,753|2,848|19,388|

## 결과 위치

- [SQLite DB](</E:/학술제/데이터모델/연구용데이터/cmi_mesa_unified_20261104_v1/cmi_mesa.sqlite3>)
- [같은 내용의 CSV](</E:/학술제/데이터모델/연구용데이터/cmi_mesa_unified_20261104_v1/cmi_mesa_episodes.csv>)
- [출처·해시·분할 manifest](</E:/학술제/데이터모델/연구용데이터/cmi_mesa_unified_20261104_v1/manifest.json>)
- [전체 원본 대조 검증](../../reports/cmi_mesa_unified_20261104/VERIFICATION.json)

개인별 행은 원본과 같은 E: 로컬 데이터 저장소에만 두었다. 이 문서와 검증 보고서는 집계 정보만 담는다. 원자료의 이용 조건을 따르며 DB·CSV를 공개 저장소나 외부 서비스에 업로드하지 않는다.

## 구조와 사용 범위

- `datasets`: CMI/MESA별 QC 및 라벨 의미 메타데이터.
- `participants`: `(cohort, subject_id)` 복합 키와 기존 split. 서로 다른 출처의 ID를 연결하지 않는다.
- `sleep_episodes`: 수면 시각, cutoff, observed_at, 시간 차이, 인구 특성, 라벨 출처, 원본 CSV 행 위치와 행 해시.
- `build_info`: 입력 해시, 시간 규칙, 변환 범위, 빌더 해시, 출처·분할별 집계.
- `model_data`: 기존 CSV와 동일한 13개 열을 제공하는 뷰.

```sql
SELECT cohort, split, COUNT(*) AS episodes
FROM model_data
GROUP BY cohort, split;

SELECT * FROM model_data WHERE split = 'train';
```

DB는 다음날 수면 예측용 에피소드 계층이다. 원시 가속도·조도 스트림, PSG 수면단계, 다른 연구의 설문, 실험 예측 결과는 이번에 적재하지 않았다. 기존 앱의 프로젝트 저장 방식을 바꾸거나 모델을 재학습하지 않았다. 기존 앱은 SQLite 직접 입력을 지원하지 않으므로 DB 조회 또는 필요한 CSV 변환을 통해 연결한다. 동봉 CSV에는 원본 13개 열을 그대로 유지했으며 앱의 각 입력 모드와의 호환성은 별도 계약을 확인해야 한다.

## 보존한 의미와 검증

CMI는 같은 밤 onset/wakeup 사건쌍이고 MESA는 ACT 기반 약한 라벨이다. 같은 DB에 있어도 같은 측정 방법이 되지는 않는다. CMI의 결측 나이·성별과 MESA 일부 결측은 NULL로 보존했다.

`+09:00`은 원본의 벽시계 직렬화 표현이며 한국에서 측정했다는 의미가 아니다. MESA 날짜는 요일을 보존한 재구성 날짜로 실제 계절·측정일 변수로 사용하지 않는다. `observed_at = wake`는 원 파이프라인의 후향적 가용시각 가정이다. 모델 입력 이력은 해당 예측 cutoff 전에 이용 가능했던 기록만 사용해야 한다.

원본 CMI/MESA 19,388행의 모든 값을 DB와 CSV에 대조했다. Samsung은 0행이며 기존 참여자별 split을 모두 유지했다. SQLite integrity/foreign-key 검사, 전체 행 해시, 원본 및 출력 파일 해시를 확인했다. 기존 Test는 이미 사용된 자료이므로 통합으로 독립 외부 검증 자료가 되는 것은 아니다.

## 재생성

추가 설치 없이 Python 표준 라이브러리로 생성한다. 아래 명령은 `research_workbench_academic`에서 실행하며, 기존 결과를 덮어쓰지 않도록 **존재하지 않는 새 출력 폴더**를 지정한다.

```powershell
& .\.venv\Scripts\python.exe -X utf8 -B scripts\build_cmi_mesa_db.py --output 'E:/학술제/데이터모델/연구용데이터/cmi_mesa_unified_new_version'
& .\.venv\Scripts\python.exe -X utf8 -B -m unittest tests.test_cmi_mesa_db -v
```

빌더는 참가자 분할 충돌, 중복 밤, 시각·분 단위 값의 불일치, 비유한 수치, 메타데이터와 행수·참여자수의 불일치를 거부한다. 검증을 통과한 결과만 출력 폴더로 발행한다.
