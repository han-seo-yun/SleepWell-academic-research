# A52s용 TabDPT 모바일 변환 방법

이 폴더의 모델은 TabDPT-v1.3 가중치를 줄인 모델이 아니다. 가중치는 FP32 약 252 MB로 유지하고, 검색 context와 ensemble 수를 제한해 연산량을 낮춘 후보이다. 참여자 단위 Validation에서 두 코호트에 공통으로 선택된 최종 설정은 `K=1024`, ensemble `E=2`이다. FP16·INT8 양자화는 아직 정확도와 Android 실행을 검증하지 않았으므로 이 후보를 양자화 모델로 부르면 안 된다.

## 고정된 모바일 계산 계약

- 검색: `mobile_l2_f32store_f64accum_index_tie_v1`
  - Train과 query 특징을 먼저 IEEE float32로 저장한다.
  - 각 값을 float64로 변환하고 특징 인덱스 순서대로 제곱 L2 거리를 누적한다.
  - `(거리, Train 행 인덱스)` 오름차순으로 context를 고른다.
- 모델 입력 전처리: `tabdpt13_mobile_fp32_external_preprocess_think64_v2`
  - 각 특징을 context 행만 이용해 두 번 clipping하고, 그 사이 sample standard deviation(`ddof=1`)으로 표준화한다.
  - 평균과 분산 합산은 float64, 특징 순서와 행 순서는 오름차순, 표준편차에는 `1e-6`을 더한다.
  - clipping은 8 sigma이며, 첫 평균을 중심으로 두 번째 inlier 표준편차를 적용한다.
  - 이 결과를 float32로 변환한 뒤 ONNX encoder에 넣는다.
- context target 표준화:
  - 검색하기 전에 전체 Train target을 float32로 저장하고, 전체 Train의 평균과 sample standard deviation(`ddof=1`)으로 한 번만 표준화한다.
  - 표준편차에 float32 `1e-6`을 더한다. 검색된 K개 행은 이미 표준화된 전체 Train target에서 선택하고, 출력 border도 같은 전체 Train 평균·표준편차를 사용한다.
- 네트워크 구조:
  - 체크포인트의 `n_thinking_rows=64`와 학습된 `thinking_embed[64,512]`를 ONNX 안에 그대로 포함한다.
  - 각 layer의 target embedding 앞에는 native forward와 같은 64개 zero row를 붙이고, attention context 및 최종 head index도 `K+64`를 사용한다.
- ensemble: 1개이면 원래 seed를 바로 사용한다. 2개이면 NumPy `SeedSequence(seed).generate_state(2)`로 내부 seed를 만든다. 특징 순열은 PyTorch `randperm` 결과를 bundle에 고정 저장한다.
- 회귀: 앞 16개 classification logit을 버리고 2,048개 회귀 logit을 ensemble 평균한다. `[-10, 10]` bin을 전체 Train target 평균·표준편차로 원래 단위에 돌린 뒤 piecewise-uniform distribution median을 계산한다.

## 원래 구현과 구분해야 하는 이유

TabDPT 내부 정규화를 그대로 ONNX로 변환하면 검색 context에서 거의 상수인 특징의 float32 분산이 런타임마다 다르게 계산됐다. 대표 MESA 입력에서 sample standard deviation이 `2.39e-7`인 특징의 정규화 값이 PyTorch `-0.263`과 ONNX Runtime `+0.704`로 갈렸고, 최종 median은 최대 4.86분 차이가 났다.

초기 v1 변환은 64개 thinking row를 누락하고 검색된 K개 target만 다시 표준화하는 두 가지 의미 오류가 있었다. v1 graph, 골든 및 Validation 출력은 `invalid_missing_thinking_rows/`에 격리했으며 정확도나 배포 근거로 사용할 수 없다. 과거 v1 수치(0.009003분 및 K1024 parity 수치)도 최종 모델 수치가 아니다.

수정된 v2 architecture gate는 설치된 native `model.forward`에서 feature 전처리만 identity로 바꾸어 직접 호출했다. 수정 wrapper는 attention patch 전에 native 출력과 bit-exact였고, exportable attention 적용 후 최대 logit 차이는 `3.8147e-6`이었다. 별도 진단으로 `TabDPTRegressor.predict(output_type="full")`에 결정적 모바일 검색과 동일한 외부 float64 feature 전처리를 연결해 코호트별 첫·마지막 Validation 행 4개(두 target씩)를 독립 실행했으며, v2 portable PyTorch median과 최대 0.002380분 차이였다. 따라서 이는 수정된 모바일 계산 경로의 독립 배선 진단이지, 전처리를 바꾸지 않은 native 모델과의 end-to-end 동일성 주장이 아니다. 이 4행 검사는 채택 기준도 아니다.

최종 v2 32개 Validation/경계 골든에서 CPU ONNX Runtime 1.31.1과 portable PyTorch의 최대 차이는 0.001724분(0.103초)이었다. CUDA batch1과 CPU batch1의 최대 차이는 0.000549분(0.033초), 실제 평가용 CUDA batch16과 CPU batch1의 최대 차이는 CMI 0.001160분, MESA 0.000488분(최대 0.070초)이었다. Android는 같은 v2 골든으로 별도 검증한다.

float64 평균·분산의 행 순서를 유지하면서 Python scalar loop를 NumPy float64 `cumsum`으로 바꾼 계산 최적화는 원본 런타임과 40개 scalar 배열 및 최종 선택 입력 tensor 128개가 byte 단위로 같음을 확인한 뒤 적용했다. 원본은 `archive_pre_optimization/portable_runtime.py`, 검증은 상위 폴더의 `fast_preprocess_check.json`, 최종 batch16 CUDA 검증은 bundle의 `cuda_batch16_parity_validation32.json`에 보존한다.

## Android 인터페이스

- ONNX 입력 `x_src`: float32 `[1, K+1, 128]`, 외부 clip-normalize-clip 완료 상태
- ONNX 입력 `y_src`: float32 `[1, K]`, 전체 Train으로 정규화한 target에서 선택한 K개 값
- ONNX 출력 `logits`: float32 `[1, 1, 2064]`
- context 파일 `SWCTX01`: magic 8 bytes, little-endian int32 `rows/features/targets`, float32 row-major `X`, float32 row-major `y`
- bundle에는 CMI/MESA 전처리 JSON, context, 특징 순열, 모델 계약과 각 파일 SHA-256을 함께 저장한다.

Android 런타임은 공식 ONNX Runtime Android AAR을 사용할 수 있다. CPU 골든은 ONNX Runtime 1.31.1, 실제 전체 정확도 산출은 CUDA ONNX Runtime 1.20.2, 독립 Android harness는 Maven의 1.22.0을 사용한다. 세 런타임의 출력이 같다고 가정하지 않고 각각 수정된 v2 골든과 비교한다. 초기 Android probe 결과는 의미 오류가 있던 v1 graph의 실행성만 확인한 것이므로 v2 정확도나 수치 일치 근거로 재사용하지 않는다.

- ONNX Runtime Android: https://onnxruntime.ai/docs/install/
- ONNX Runtime mobile flow: https://onnxruntime.ai/docs/tutorials/mobile/
- ExecuTorch Android/XNNPACK 대안: https://docs.pytorch.org/executorch/stable/using-executorch-android.html

## 검증 파일

- `generate_goldens.py`: Test를 사용하지 않고 Validation 특징과 합성 경계 입력으로 단계별 골든을 만든다.
- `verify_end_to_end.py`: raw39 전처리, portable PyTorch, Python ONNX 결과를 비교한다.
- `verify_retrieval.py`: FAISS와 결정적 모바일 검색이 서로 다른 방법임을 확인한다.
- `test_mobile_retrieval.py`: 저장 dtype, 동률 처리, 입력 검사를 고정한다.

