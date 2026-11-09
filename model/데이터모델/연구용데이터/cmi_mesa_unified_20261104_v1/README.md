# CMI·MESA 통합 데이터베이스

Samsung 기록을 제외한 다음날 수면 예측용 QC 완료 에피소드입니다. 원시 센서·PSG·설문 전체를 합친 자료가 아니며 모델을 재학습하지 않았습니다.

- `cmi_mesa.sqlite3`: datasets, participants, sleep_episodes, build_info 테이블
- `model_data` 뷰: 기존 13개 열을 제공하는 통합 분석 입력
- `cmi_mesa_episodes.csv`: model_data의 전체 CSV 내보내기
- `manifest.json`: 출처 해시, 출처별 인원·행수·분할, 검증과 산출물 해시

참여자 키는 (cohort, subject_id)입니다. 출처가 다른 동일 ID를 같은 사람으로 연결하지 않습니다. 기존 train/validation/test를 유지했고 결측 인구 정보는 NULL입니다.

CMI 사건쌍과 MESA ACT 유래 라벨의 의미는 datasets에 보존했습니다. +09:00은 원본의 벽시계 표현이며 한국에서 측정했다는 의미가 아닙니다. MESA 재구성 날짜는 실제 계절·측정일로 해석하지 않습니다. observed_at은 기상 시각이라는 후향 가정입니다.

기존 Test는 재사용된 자료입니다. 통합 후에도 독립 검증으로 바뀌지 않습니다. 이용 제한 파생자료로 원본과 같은 로컬 저장소에 보관하며 외부에 공개하지 않습니다.

```sql
SELECT cohort, split, COUNT(*) FROM model_data GROUP BY cohort, split;
SELECT * FROM model_data WHERE split = 'train';
```
