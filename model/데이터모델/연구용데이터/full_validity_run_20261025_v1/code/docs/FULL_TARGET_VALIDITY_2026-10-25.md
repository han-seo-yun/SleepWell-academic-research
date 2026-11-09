# SleepWell target-validity 확장: 전체 1–4 적용 기록

작성일: 2026-10-25  
구현 버전: 연구자용 PC 0.4.3, 사용자용 Android 0.4.2  
상태: 기존 추천·서비스 동작은 유지한 연구 비교 기능의 전향적 확장

## 범위와 주장 경계

이번 확장은 passive-sensing-target-validity에서 참고한 네 원칙을 SleepWell의 다음날 입면·최종 기상 예측에 맞게 적용한다. (1) 강한 시간의존 기준선, (2) 개인 간 수준과 개인 내 변화의 분리, (3) 목표·측정·보고 지연의 타당성, (4) 개인화가 실제로 이득을 내는 관측량과 조건의 검증이다. 외부 저장소의 계수나 성능을 SleepWell 성능으로 전용하지 않는다.

Android 0.4.2는 기존 `robust-next-night-research-v2`의 물리·데이터·융합 출력 및 권장 정책을 바꾸지 않는다. 새 기준선은 연구용 comparator이며 추천 시각을 결정하지 않는다. PC 0.4.3은 동결된 휴대폰 예측과 최초 자기보고 결과를 재검증·집계한다. 이번 작업은 이미 배포된 백엔드 서비스나 Google Drive를 변경하지 않는다.

## 1. 강한 기준선과 동일 조건 비교

매일 한국시간 20:00을 기준시각으로 삼고 다음 주수면의 입면과 최종 기상을 분 단위 offset으로 예측한다. 기준선은 다음과 같다.

- 사용자 프로필의 평소 취침·기상 시각. 이는 `user_profile_assumption_not_cohort_train_population_prior`로 표시하며 TRAIN 집단 평균과 구분한다.
- 최근 28일 안의 직전 관측.
- 달력 기준 최근 7일·14일·28일 산술평균.
- 자정 경계를 보존한 최근 7일 원형평균.
- 최근 28일 관측의 반감기 14일 최근성 가중평균.
- 목표 기상일의 평일/주말과 같은 최근 28일 관측 평균.
- 다음날 첫 일정에서 준비·이동시간을 뺀 기상시각과 사용자 프로필의 고정 희망 수면시간만 이용한 일정 단독 기준선.

수면 이력 기준선에는 수동 일지(`manual`, `diary`, `user_entry`)만 들어간다. 기상과 수정시각이 모두 cutoff보다 엄격히 이른 기록만 허용하고, 한국시간 기상일별 최신 버전 하나만 사용한다. 미래 기록이나 cutoff와 같은 시각에 보고된 기록도 제외한다. 일정 단독 기준선은 수면 이력을 전혀 사용하지 않는다. 일정이 없거나, 일정 정보가 cutoff 뒤 입력되었거나, 다음날 범위가 아니거나, 고정 프로필 수면시간을 보존하면 사전 지정 예측창 밖이 되는 경우 `available=false`로 남긴다. 결측 일정을 프로필이나 집단 평균으로 대체하지 않는다.

각 기준선은 `research_baselines`에 schema/version/model identifier/cutoff/history count와 함께 저장된다. 각 arm은 `available`, `reason`, `method`, `fallback`, `contributing_records`, onset/wake/interval을 가진다. 기준선별 관측 밤 수가 다르면 MAE를 직접 비교하지 않고, 주 비교인 14일 평균과 같은 밤의 짝지은 차이와 skill을 함께 본다.

## 2. 개인 간 수준과 개인 내 변화

집단 Test 성능, 참가자별 평균오차, 개인 평균을 제거한 centered 지표를 같은 의미로 취급하지 않는다. 최근 평균이 절대 시각을 잘 맞혀도 개인 내 일별 변화까지 잘 추적한다는 뜻은 아니다. 현재 후향 결과에서는 개인 내 변화 추적이 약하며, 물리 특성 추가가 데이터 단독보다 유의하게 낫다는 근거도 확인되지 않았다.

휴대폰 전향 자료에서는 한 참여자의 동일 밤에 저장된 여러 arm을 비교한다. 기록이 누적되어도 과거 스냅샷은 다시 계산하지 않는다. 개인화 활성과 충분한 개인화 검증을 구분하며, 1·3·7·14·28일 또는 기록 공백별 분석은 같은 미래 밤에서 짝지어 수행해야 한다. 신규 참여자와 기존 참여자의 미래기간 검증도 분리한다.

## 3. 목표·측정·보고 타당성

주 목표는 최초 수동 자기보고의 주수면 입면과 최종 기상이다. 삼성헬스 착용자료는 장치·착용·알고리즘 차이가 큰 보조 측정이며 주 목표를 대체하지 않는다. 예측 오차는 수면의 질, 총수면시간, 수면압력, 추천 효과가 아니다.

20:00–20:15에 생성된 `forecast_status=prospective_20_window`만 전향 예측으로 보존한다. 등록 뒤 늦게 만든 재구성, cutoff 뒤 입력된 생활 맥락, 미리보기는 제외한다. 결과는 해당 예측 뒤 처음 입력된 수동 일지를 `matched` 또는 `excluded`로 한 번만 동결한다. 후속 수정이나 더 좋은 기록이 최초 결과를 바꾸지 않는다. PC 집계기는 cutoff 날짜·시각, 생성창, 결과 chronology, offset 범위, 동결된 baseline 객체의 forecast–outcome 동일성을 다시 확인하고 앱이 보낸 잔차를 신뢰하지 않고 오차를 재계산한다.

보고 지연은 최초 동결 결과의 `available_at - outcome_at`으로 별도 집계한다. 이는 백업 수신 지연이나 일반 기록의 마지막 수정 지연과 다르다. 휴대폰 시계가 정확하거나 앱 바이너리가 수정되지 않았음을 이 자료만으로 인증할 수는 없다.

## 4. 개인화 조건과 새 arm의 전향 검증

현재 기준선은 기존 모델과 개인화의 추가가치를 과장하지 않기 위한 연구 대조군이다. 프로필 기반 fallback은 집단 TRAIN prior가 아니며, 일정 단독 기준선도 모집단 학습 모델이 아니다. `robust-next-night-research-v2`의 실제 feature contract는 `sleepwell_next_night_robust_v2`이다. Android 자산의 SHA-256은 CMI `add8ba4e23f9c4c9337b821406386664a054580d86c6e53c9b5e6f189bdfa2c7`, MESA `757b939d756c9174eab9304e0d8e872f4ed9eeae34e99a3d921f29284207c620`이다.

새 기준선 arm은 Android 0.4.2 설치 뒤 실제로 20시 스냅샷이 생성된 밤부터만 평가한다. 과거 백업에 소급 생성하거나 비어 있는 일정을 보간하지 않는다. 서로 다른 model version, feature contract, artifact identity는 섞지 않는다. 비교의 최소 단위는 같은 참여자·같은 밤이다. 관측 밤이 적을 때의 skill은 표시하지 않으며 현재 구현은 공통 밤이 4개 이상이고 기준 MAE가 1분 이상일 때만 계산한다.

추천 효과는 별도 문제다. 예측이 정확해도 권장이 규칙성·회복감·다음날 기능을 개선한다는 뜻은 아니다. 추천 노출과 대조 조건을 전향적으로 비교하고, 추천 생성·표시·수락·이행을 구분하며, 규칙성과 다음날 기능을 독립 결과로 측정해야 한다.

## 동결 JSON 계약 요약

`research_baselines` 최상위 필드는 `schema_version=1`, `version=android-research-baselines-v1`, cutoff Instant, `model_identifier=research_comparator_only_no_recommendation`, `history_count`, `history_policy`, `profile_fallback_role`, `baselines`이다. 과거 전향 스냅샷은 새 필드를 소급 추가하지 않는다. 오래된 스냅샷은 `research_baselines`가 없을 수 있으며 PC에서 unavailable로 남겨야 한다.

실현 결과는 `status=matched|excluded`이다. matched에는 실제 onset/wake offset과 기존 모델 잔차, 기준선 잔차를 저장한다. PC는 이 잔차를 평가값으로 신뢰하지 않고 동결 예측과 실제 offset에서 다시 계산한다. excluded에는 사전 지정 사유가 남고 이후 기록이 대체하지 않는다.

## 현재 검증과 남은 위험

- Android 0.4.2: 오프라인 `assembleDebug`와 `lintDebug` 성공. 새 코드 관련 lint 오류는 없다. 기존 프로젝트 경고는 남아 있다.
- 기준선 경계 테스트: 미래/정시 보고 누수 차단, 자정 원형평균, 결측·늦은·예측창 밖 일정 unavailable, 유효 일정, 과거 스냅샷 비변경, 백업 보존을 확인했다.
- 기존 회귀: 일별 잔차 개인화 parity 및 전향 계약 15개 검사를 통과했다.
- APK는 기존 0.4.1과 같은 Android debug 인증서(SHA-256 `72a3b8e32ecae55a73c840bc4d6bb4e74049f312b60b9bde0b782fb26e595333`)를 사용한다.
- 물리 Android 단말에서 설치·20시 예약 실행·날짜 전환·백업 송수신을 수행한 것은 아니다. 실제 단말 검증 전에는 동작 완료로 주장하지 않는다.
- 연구자 집계기는 forecast와 outcome의 artifact metadata 동일성뿐 아니라 CMI/MESA별 SHA-256, model version, feature contract, physical provider, 원본 model SHA를 설치된 정본 자산과 대조한다. 정본이 없거나 일치하지 않는 코호트의 학습 모델 arm만 제외하고 기준선 평가는 유지한다. 다만 휴대폰 원격측정 자체와 휴대폰 시계는 여전히 신뢰 경계 밖이다.

## 다음 실험

1. 신규 참여자에서 최소 4–8주 동안 20시 생성 성공·실패를 모두 기록하고 미생성 밤을 소급 채우지 않는다.
2. 수동 일지 주 목표와 웨어러블 보조 목표를 분리하고 최초 보고 지연에 따른 민감도 분석을 한다.
3. 기존 데이터/융합 모델과 모든 기준선을 같은 밤에서 비교하며, participant-equal MAE와 paired uncertainty를 보고한다.
4. 개인화 반감기·수축강도·일정 feature 선택은 훈련 참여자·시간 내부에서만 결정하고 새 미래기간에 고정한다.
5. 추천의 규칙성·다음날 기능 효과는 예측 성능과 분리된 대조시험으로 검증한다.

