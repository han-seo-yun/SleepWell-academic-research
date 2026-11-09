# Two-Process S-C 부호 교정 및 재검증 기준

## 결론

현재 구현이 다음 두 조건을 동시에 사용한다면 `S - C`는 내부 정의와 부호가 맞지 않는다.

1. `phase_ref`를 관측 수면 구간의 평균 **midsleep**으로 둔다.
2. `C = A cos(2pi(t - phase_ref)/24)`로 두어 midsleep에서 `C=+A`가 된다.

이때 `C`는 수면 중앙에서 가장 큰 **sleep-promoting circadian component**로 작동한다. 출력도 “높을수록 sleep propensity”라면 같은 방향의 항을 빼서는 안 되므로 다음처럼 고친다.

```python
C_sleep = 0.15 * cos(2 * pi * (t - phase_ref) / 24)
sleep_propensity = S + C_sleep
```

이 수정은 “Two-Process Model의 표준식이 언제나 S+C”라는 주장에 근거하지 않는다. `C`를 wake-promoting drive로 정의한 구현이라면 `S-C_wake`도 가능하다. 이번 교정의 근거는 **현재 구현에서 C의 양의 방향을 midsleep과 일치시켰다는 사실**이다. 변수명도 `C_sleep`로 바꾸어 같은 오류의 재발을 막았다.

## 변경 사항

| 항목 | 기존 | 교정 후 | 이유 |
|---|---|---|---|
| circadian 결합 | `pressure = S - C` | `sleep_propensity = S + C_sleep` | midsleep에서 양의 최댓값인 항과 점수 방향 통일 |
| 변수 의미 | `C`, `pressure` | `C_sleep`, `sleep_propensity` | 부호 규약을 이름에 노출 |
| epoch AUC 명칭 | 전체 epoch 예측 AUC | `epoch_auc_history_conditioned` | S가 직전 실제 sleep/wake label로 업데이트되므로 독립 예측 성능이 아님 |
| 주 전이 지표 | 없음/불명확 | `transition_auc_onset_vs_wakeup` | onset 점수가 wakeup 점수보다 큰지 직접 평가 |
| 보조 전이 지표 | 없음/불명확 | `transition_margin` | 평균 onset score - 평균 wakeup score의 방향과 크기 확인 |

## 지표 정의

### 1. History-conditioned epoch AUC

각 epoch의 실제 sleep/wake 이력을 이용해 S를 갱신한 후 같은 label에 대해 계산한 ROC-AUC이다. 파이프라인 진단에는 쓸 수 있지만, label을 보지 않고 미래를 맞히는 독립 예측 AUC로 보고하면 안 된다.

### 2. Transition AUC

- 양성: wake -> sleep으로 바뀌는 첫 epoch(onset)
- 음성: sleep -> wake로 바뀌는 첫 epoch(wakeup)
- 점수: 해당 epoch의 `sleep_propensity`

`transition_auc_onset_vs_wakeup > 0.5`이면 임의로 고른 onset이 wakeup보다 높은 점수를 가질 확률이 0.5보다 크다는 뜻이다. 단, 피험자별 전이 수가 적으면 불확실성이 크므로 전이 수와 함께 보고한다.

### 3. Transition margin

```text
mean(score at onset) - mean(score at wakeup)
```

양수여야 의도한 방향과 일치한다. 현재 파일은 전이 집합 전체의 평균 차이를 계산하며, “같은 수면 episode의 onset-wakeup 쌍”만 비교하는 paired margin은 아니다.

## 정당성 및 근거

- Two-Process Model은 수면-각성 이력에 의존하는 homeostatic Process S와 circadian Process C의 상호작용으로 수면 조절을 설명한다.
- 첨부 연구계획서는 Walch -> CMI -> MESA 순서로 데이터 검증을 확장하고, 물리모델 baseline과 잔차모델을 구축하는 계획을 제시한다. 따라서 baseline의 부호 규약과 평가 지표를 먼저 고정하는 것이 후속 데이터 비교의 전제다.
- 첨부 SLEEP 논문은 개인 수면 이력을 실시간으로 반영하는 수리모델과 웨어러블 기반 개입을 다루며, homeostatic/circadian drive의 정렬이 수면 권고의 핵심임을 설명한다. 이는 관측 이력 기반 진단과 독립적인 미래 예측을 구분해야 한다는 판단을 뒷받침한다.
- Dijk & Czeisler(1995)는 circadian pacemaker와 sleep homeostat가 sleep propensity에 각각 기여함을 실험적으로 분리해 제시했다.

## 재검증 절차

1. CMI를 `subject_id`, `timestamp`, `is_sleep`(0/1) 형태로 준비한다.
2. 교정 코드를 실행해 epoch 결과 CSV와 피험자별 JSON 요약을 생성한다.
3. 각 피험자에서 `transition_margin > 0`, `transition_auc_onset_vs_wakeup > 0.5`인지 확인한다.
4. macro 평균, 중앙값, 95% bootstrap CI와 유효 피험자 수를 함께 보고한다.
5. 교정 전 `S-C`와 교정 후 `S+C_sleep`을 같은 전처리·피험자 집합에서 비교한다.
6. phase_ref를 동일 관측 수면에서 추정한 결과는 calibration/diagnostic 결과라고 명시한다. 완전한 외부 검증은 phase_ref를 훈련 구간 또는 독립적 생체리듬 지표에서 정한 뒤 평가 구간에 고정해야 한다.

## 실행 예시

```bash
python two_process_model_demo_corrected.py cmi_epochs.csv \
  --epoch-output cmi_two_process_epochs.csv \
  --summary-output cmi_two_process_summary.json
```

## 제한사항

- 원본 `two_process_model_demo.py` 전체 파일이 제공되지 않아 기존 파일을 직접 덮어쓰지 않고 교정본을 별도로 작성했다.
- time constant와 진폭 기본값은 기존 데모의 규약을 확인해 다시 고정해야 한다. 부호 교정의 타당성은 이 수치 선택과 독립적이다.
- midsleep을 평가 대상 label에서 직접 추정하면 정보가 평가 점수에 들어가므로, 이를 외부 예측 성능으로 해석할 수 없다.
- CMI의 실제 컬럼명이 다르면 입력 매핑 단계가 필요하다.

## 참고문헌

1. Borbely AA et al. *The two-process model of sleep regulation: a reappraisal*. Journal of Sleep Research. 2016. https://doi.org/10.1111/jsr.12371
2. Dijk DJ, Czeisler CA. *Contribution of the circadian pacemaker and the sleep homeostat to sleep propensity, sleep structure, EEG slow waves, and sleep spindle activity in humans*. Journal of Neuroscience. 1995;15(5):3526-3538. https://doi.org/10.1523/JNEUROSCI.15-05-03526.1995
3. Song YM et al. *A real-time, personalized sleep intervention using mathematical modeling and wearable devices*. Sleep. 2023;46(9):zsad179. https://doi.org/10.1093/sleep/zsad179
4. 첨부 자료: `붙임2. 2026학년도 미래인재대학 학술제 연구계획서.pdf`, `수면 논문.pdf`, `수면 논문_해설.pdf`.
