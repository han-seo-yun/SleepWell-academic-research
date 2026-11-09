# 베이지안 잔차 개인화 및 부분 계수 적합 비교

## 결론

현재 개인화 방식을 유지한다. Train에서만 추정한 경험적 베이지안 절편, 14일 가중 절편, 랜덤워크·평균회귀 Kalman, 정적 affine 보정, 개인별 Bayesian affine 보정 등 사전 고정한 6개 대안 가운데 Validation 최고 후보는 `kalman_random_walk_q01`이었다. 그러나 현재 방식 대비 4셀 평균 이득은 0.022분에 불과했고 MESA 두 셀에서 악화되어 사전 게이트(평균 1분 이상, 4셀 모두 관측 오차 비악화)를 통과하지 못했다.

선택 잠금 뒤 Test를 재생했을 때 이 후보는 현재 방식보다 4셀 평균 0.357분 나빴다. 10,000회 participant-paired bootstrap의 98.75% 구간도 4셀 모두 하한이 0보다 크지 않았다. 따라서 코드·운영 상수 변경 근거가 확인되지 않았다.

## 사전 고정한 비교

모든 후보와 선택·Test 게이트는 결과 산출 전에 `protocol.json`에 고정했다.

- 경험적 베이지안 절편: Train 집단 사전분포 `N(mu,T)`와 개인 잔차 관측분산 `R`을 결합한다. 정적 버전과 14일 반감기 fractional-likelihood 버전을 비교했다.
- 랜덤워크 Kalman: 첫 사전분포는 `N(mu,T)`, 관측분산은 `R`, 경과 일수당 과정분산은 `0.01R`이다.
- 평균회귀 Kalman: `F=2^(-dt/14)`, 과정분산 `T(1-F²)`로 집단평균 `mu`에 회귀한다.
- 정적 affine: Train 예측값을 중심화·표준화하고 절편·기울기를 participant-balanced ridge로 적합했다.
- 개인 Bayesian affine: 정적 affine 계수를 사전평균으로 두고 개인 기록으로 절편·기울기를 14일 가중 갱신했다. 이는 생리계수 적합이 아니라 예측 잔차의 탐색적 보정이다.

잔차는 ±180분 절단, 보정은 ±60분 제한, 최종 일정은 운영 `night_core.constrain`을 그대로 적용했다. Gaussian 및 fractional-likelihood 계산으로 얻은 분산은 미교정 모형 진단값이며 확률 보장이나 신뢰구간이 아니다.

## Validation 선택 결과

현재 방식 대비 4개 cohort-arm 셀의 participant-macro joint MAE 평균 이득은 다음과 같다. 양수일수록 좋다.

| 후보 | 평균 이득(분) | 4셀 무악화 | 게이트 |
|---|---:|---:|---:|
| 랜덤워크 Kalman `Q=0.01R/day` | 0.022 | 아니오 | 실패 |
| 개인 Bayesian affine | -0.085 | 아니오 | 실패 |
| 정적 EB 절편 | -0.091 | 아니오 | 실패 |
| 14일 EB 절편 | -0.129 | 아니오 | 실패 |
| 14일 평균회귀 Kalman | -0.130 | 아니오 | 실패 |
| Train 정적 affine | -0.284 | 아니오 | 실패 |

개인 Bayesian affine은 정적 affine보다 평균 0.199분 덜 나빴지만 현재 방식보다는 악화했다. 따라서 이는 “개인 갱신의 개선”을 입증하지 않는다. Train 집단 평균 보정과 정적 affine 모두 현재 방식보다 Validation에서 나빴으므로, 단순 집단 calibration 이득도 확인되지 않았다.

## 잠금 후 Test 결과

Test에는 프로토콜대로 현재 방식, 잠긴 랜덤워크 후보, 개인화 비활성화, Train 집단평균 보정만 열었다. 개인 affine이 선택되지 않았으므로 정적 affine Test는 새로 탐색하지 않았다.

| Cohort | Arm | 현재 MAE | 잠긴 후보 MAE | 이득(분) | 98.75% bootstrap 구간 | 셀 통과 |
|---|---|---:|---:|---:|---:|---:|
| CMI | data | 47.777 | 48.472 | -0.695 | [-1.232, 0.014] | 아니오 |
| CMI | hybrid | 49.474 | 49.757 | -0.283 | [-0.789, 0.330] | 아니오 |
| MESA | data | 67.981 | 68.211 | -0.231 | [-0.467, 0.001] | 아니오 |
| MESA | hybrid | 68.130 | 68.349 | -0.218 | [-0.458, 0.021] | 아니오 |

4셀 동일가중 이득은 -0.357분이고 98.75% 구간은 [-0.638, -0.020]분이다. 최종 게이트는 실패했다. Train 집단평균 보정의 Test MAE는 CMI data/hybrid 48.189/49.293분, MESA data/hybrid 68.401/68.527분으로, 잠긴 후보와 마찬가지로 현재 방식의 일관된 개선이 아니었다.

## 추정된 수축 강도

8개 cohort-arm-channel Train 그룹에서 `R/T`는 18.32~81.29였다. 이는 정적 절편 사전이 완전가중 개인 관측 약 18~81개에 해당하는 강도로 작동한다는 뜻이다. 첫 개인 관측이 posterior 평균에서 차지하는 비율은 약 1.22~5.18%였다. `R`, `T`, 예측값 scale의 하한은 어느 그룹에서도 발동하지 않았고, 모든 그룹에 `n>=2`인 참여자가 있어 무분산 fallback도 발동하지 않았다.

이 강한 수축은 Train에서 개인간 평균 차이 `T`가 개인내 밤별 변동 `R`보다 작게 추정된 결과다. 현재 자료의 짧은 Test 이력에서는 개인 정보를 빠르게 높이는 것보다 오히려 집단 사전 지배가 강했으며, 그 설정조차 현재 운영 방식보다 좋아지지 않았다. 이를 생리적 개인차가 작다는 증거로 해석해서는 안 된다.

## 데이터 계약과 적용 한계

- 입력의 고유 관측 4,627개(CMI)와 14,761개(MESA)에서 `observed_at`은 모두 `wake`와 같았다. 코드는 `available_at < cutoff`와 `outcome_at <= available_at`을 엄격히 적용하고 최신 revision만 사용하지만, 이 자료는 실제 보고·업로드·동기화 지연을 검증하지 않는다.
- MESA에 PSG stage annotation이 있더라도 이 입력에는 반복 EEG/SWA, PVT 또는 DLMO 관측이 없다. 따라서 `tau_w`, `tau_s`, circadian phase나 light sensitivity 같은 개인 생리계수는 이 비교로 적합할 수 없다.
- frozen 모델의 Train 잔차로 hyperparameter를 추정했으므로 낙관 가능성이 있다. Validation/Test는 기존 분석에서 재사용된 holdout이며 새 외부·전향 검증이 아니다.
- Test 이력이 짧아 개인 affine 기울기 식별이 제한된다. 보고된 posterior 분산은 미교정 진단값이며 생리적 불확실성이나 calibrated probability가 아니다.

## 재현성과 무결성

- 운영 현재 방식 재현 오차: Validation/Test 모두 최대 0분.
- 비활성화 기준선과 2026-11-03 동결 평가의 최대 차이: `1.42e-14`분.
- 입력 CSV, frozen prediction CSV, CMI/MESA frozen 모델, `adaptive_personalization.py`, `night_core.py`, 재사용한 2026-11-04 평가기 해시를 확인했다.
- Train 27,574개, Validation 5,506개 base-prediction row를 처리한 뒤 선택을 잠갔고, 그 후 Test 5,696개 row를 재생했다.
- 상세 수치와 provenance는 `comparison.json`, Train 추정량은 `hyperparameters.json`, 선택 잠금은 `selection_lock.json`, Validation 평면표는 `validation_candidates.csv`에 저장했다.

