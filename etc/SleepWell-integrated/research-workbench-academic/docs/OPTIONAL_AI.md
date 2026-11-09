# 선택적 AI 및 로컬 임베딩

이 기능은 기존 실험·결과 열람·구조화 보고서에 필요하지 않은 선택 기능이다. 핵심 학습과 수치 계산은 외부 API 없이 동작한다.

## 외부 LLM 전송 계약

`researchbench.optional_ai.OptionalAI`는 다음 순서를 강제한다.

1. `create_preview(...)`가 허용된 집계형 실험 요약과 사용자가 선택한 근거 청크만 읽는다.
2. 화면은 반환된 `transmission_preview`와 정확한 직렬화 SHA-256을 사용자에게 보여준다.
3. 사용자가 전송을 명시적으로 확인한 경우에만 `approve_preview(..., user_confirmed=True)`를 호출한다.
4. UI의 별도 전송 동작만 `send_approved(...)`를 호출한다. 승인 토큰이 없거나 미리보기가 이미 사용됐으면 전송하지 않는다.

허용 목적은 `result_summary`, `experiment_plan`, `settings_explanation`, `follow_up_experiments`이다. 원시 행, 개별 예측값, 실제값, 참여자 식별자와 연락처 필드는 거부한다. 발췌문은 활성 상태이며 해시가 유효한 로컬 근거 청크에서 다시 읽는다. 미리보기 후 청크가 변경·폐기되면 전송을 차단한다.

문서 내용은 신뢰할 수 없는 데이터로 취급한다. 시스템 프롬프트는 문서 안의 지시를 실행하지 않도록 명시하며 도구 호출 인터페이스를 제공하지 않는다.

### 자격증명과 감사

API 키는 `keyring`을 통해 Windows 자격 증명 저장소에 보관한다. 키 값은 프로젝트 SQLite, 요청 감사 기록, 캐시, 로그 또는 내보내기 자료에 저장하지 않는다. `requirements-ai.txt`가 없거나 안전한 시스템 keyring backend를 사용할 수 없으면 외부 API 기능을 활성화하지 않는다.

- HTTPS endpoint만 허용하며 loopback HTTP만 로컬 서버용으로 허용한다.
- 승인 SHA와 캐시 키에는 전체 endpoint가 포함된다. Authorization을 가진 redirect는 동일 origin에서만 허용한다.
- 호출 수, 예상 비용 상한, timeout, 재시도 수, 출력 토큰과 응답 크기를 제한한다.
- 사용자 입력 가격으로 계산한 보수적 사전 비용이 예산을 넘으면 전송하지 않는다. 실제 공급자 비용을 검증할 수 없는 경우 추정치로 표시한다.
- 동일 입력의 캐시도 새 명시적 승인이 필요하다.
- 실제 입력·응답·provider·model·설정을 `research/optional_ai.sqlite3`에 보존한다.
- 전송 전에 `sending` 감사 행과 보수적 비용 예약을 커밋한다. 중단된 호출은 다음 시작에서 `interrupted_unknown_outcome`으로 표시하며 안전하게 취소됐다고 가정하지 않는다.
- 캐시와 요청 응답에는 SHA-256을 저장하고 재사용·감사·초안 저장 전에 검증한다.
- 생성 결과는 근거 태그가 붙은 별도 초안이다. 자동 검사 통과 보고서나 검증된 주장으로 자동 삽입하지 않는다.
- 실제 외부 API 호출은 API 키가 없어 검증하지 않았다. 테스트는 결정론적 transport만 사용한다.

## 로컬 다국어 임베딩

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-ai.txt
.venv\Scripts\python.exe tools\prepare_embeddings.py
```

스크립트는 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`의 고정 커밋 `e8f8c211226b894fcb81acc59f3b34ba3efd5f42`를 `.models/paraphrase-multilingual-MiniLM-L12-v2`로 내려받는다. 실행에 불필요한 ONNX, OpenVINO, TensorFlow와 중복 PyTorch 바이너리는 제외한다. 원격 코드를 신뢰하지 않으며 다운로드 후 `local_files_only=True`로 다시 로드하고 SHA-256을 기록한다.

모델 카드는 50개 언어와 384차원 벡터를 명시하고 라이선스를 Apache-2.0으로 표시한다. 배포 전 저장소의 라이선스와 리비전을 다시 확인해야 한다.

```powershell
.venv\Scripts\python.exe tools\prepare_embeddings.py --verify-only
```

manifest와 평가 결과는 모델 폴더의 `sleepwell_model_manifest.json`, `sleepwell_embedding_evaluation.json`에 저장한다.

### 사전 등록 기준과 실제 결과

시험 전에 Recall@3 ≥ 0.80, answerable-query MRR ≥ 0.80, no-evidence accuracy = 1.00을 고정했다. 2026-11-01 시험 결과는 답변 가능 4건에서 Recall@3 `1.00`, MRR `1.00`, 근거 없음 2건에서 정확도 `0.50`(1/2)이므로 **수용 실패**다.

관련 없는 한국어 질의가 기존 hybrid cosine gate를 통과했다. 시험 결과를 본 뒤 임계값을 조정하지 않았다. UI에서는 이 경로를 `실험적 hybrid`로 표시하고 기본 검색은 lexical로 유지해야 한다. 향후 별도 보정 세트에서 임계값을 결정하고 새로운 미사용 최종 세트로 평가해야 한다. 이 평가는 사람의 의미 검토나 실제 연구 문헌 성능이 아니다.

## 공식 자료

- [모델 카드 및 Apache-2.0 표시](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2)
- [고정 리비전을 확인한 Hugging Face 모델 API](https://huggingface.co/api/models/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2)
- [SentenceTransformer revision·local_files_only 문서](https://sbert.net/docs/package_reference/sentence_transformer/model.html)
- [Python keyring 공식 문서](https://keyring.readthedocs.io/en/stable/)
- [Python urllib.request 공식 문서](https://docs.python.org/3/library/urllib.request.html)
