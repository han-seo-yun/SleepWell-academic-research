# 고도화 기능 제3자 구성요소 및 배포 경계

확인일: 2026-11-02  
범위: `research_workbench_academic`의 문서 근거·보고서·선택적 AI 기능에 새로 사용한 직접 의존성이다.

## 확인 방법

버전과 라이선스는 현재 Windows 프로젝트 `.venv`의 설치된 `*.dist-info/METADATA` 및 `*.dist-info/licenses/*`를 대조했다. 아래 링크는 각 프로젝트의 공식 문서·저장소 또는 공식 모델 카드다. 이 확인은 법률 자문이 아니며, 향후 binary bundle을 만들 경우 포함되는 모든 전이 의존성의 고지 의무를 다시 생성·검토해야 한다.

| 구성요소 | 확인 버전 | 설치 메타데이터/라이선스 파일 | 확인 라이선스 | 공식 자료 | 제품에서의 사용 |
|---|---:|---|---|---|---|
| pypdf | 6.21.4 | `pypdf-6.21.4.dist-info/METADATA`, `licenses/LICENSE` | BSD-3-Clause | [문서](https://pypdf.readthedocs.io/en/latest/), [소스](https://github.com/py-pdf/pypdf) | PDF 텍스트 추출. OCR 기능으로 표시하지 않음. |
| python-docx | 1.2.0 | `python_docx-1.2.0.dist-info/METADATA`, `licenses/LICENSE` | MIT | [문서](https://python-docx.readthedocs.io/en/latest/), [소스](https://github.com/python-openxml/python-docx) | DOCX 추출 및 보고서 생성. |
| ReportLab | 5.1.0 | `reportlab-5.1.0.dist-info/METADATA`, `licenses/LICENSE` | BSD license | [공식 PyPI](https://pypi.org/project/reportlab/), [홈페이지](https://www.reportlab.com/) | 전체 보고서 PDF 렌더링. |
| skops | 0.16.0 | `skops-0.16.0.dist-info/METADATA`, `licenses/LICENSE` | MIT | [문서](https://skops.readthedocs.io/en/stable/), [소스](https://github.com/skops-dev/skops) | 승인 타입 기반 동결 scikit-learn pipeline 저장·로딩. |
| keyring | 25.7.0 | `keyring-25.7.0.dist-info/METADATA`, `licenses/LICENSE` | MIT | [소스](https://github.com/jaraco/keyring) | 선택적 외부 API 자격 증명의 OS keyring 저장. |
| sentence-transformers | 6.2.0 (선택) | `sentence_transformers-6.2.0.dist-info/METADATA`, `licenses/LICENSE`, `licenses/NOTICE.txt` | Apache-2.0 | [문서](https://sbert.net/), [소스](https://github.com/huggingface/sentence-transformers/) | 고정 로컬 모델을 이용한 실험적 hybrid 검색. |
| paraphrase-multilingual-MiniLM-L12-v2 | revision `e8f8c211226b894fcb81acc59f3b34ba3efd5f42` (선택) | 로컬 `sleepwell_model_manifest.json`; model directory SHA-256 `71d5898486c1fcbdd2cd348e36b7e962e7eae828a267f139bd069c281cbebd5c` | Apache-2.0 | [모델 카드](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2) | 한국어·영어 hybrid retrieval. 품질 gate 실패로 기본값이 아님. |

## 배포 및 글꼴 정책

- 현재 source package는 `.venv`와 `.models`를 포함하지 않는다. 따라서 위 Python wheel과 선택적 모델 파일 자체를 현재 ZIP에서 재배포하지 않는다.
- source package는 requirements와 설치 안내를 제공한다. 실제 binary/standalone 배포를 추가할 때는 포함된 직접·전이 의존성의 라이선스 및 NOTICE 원문을 산출물에 동봉하는 절차를 별도로 검증해야 한다.
- 선택적 모델은 사용자가 `tools/prepare_embeddings.py`로 고정 revision을 명시적으로 준비한다. 암묵적 다운로드는 하지 않으며 런타임 검색은 local-files-only다.
- 한국어 PDF는 Windows 시스템의 Malgun Gothic을 사용한다. Malgun Gothic 글꼴 파일은 이 프로젝트가 재배포하지 않으며 source ZIP에도 포함하지 않는다. 시스템에서 찾지 못하면 한국어 PDF 생성은 명시적으로 실패한다.

## 검증 상태

- 위 버전은 현재 Windows의 기존 프로젝트 `.venv`에서 설치 및 import가 확인되었다. 깨끗한 Windows 장비에서의 신규 설치 검증은 아직 수행하지 않았다.
- 선택적 hybrid test fixture 결과는 답변 가능 4건 Recall@3 `1.00`, MRR `1.00`, 근거 없음 2건 정확도 `0.50`(1/2)이다. 사전 기준의 no-evidence accuracy `1.00`을 충족하지 못했으므로 experimental이다.
- 기본 lexical test fixture 결과는 답변 가능 4건 Recall@3 `1.00`, MRR `1.00`, 근거 없음 2건 정확도 `1.00`(2/2)이다. 이 평가는 자동 검색 위치 평가이며 사람의 의미상 지지 검토를 대체하지 않는다.
- Tk 내부 geometry와 한·영 렌더 자동 검사는 실제 Windows 디스플레이 배율 100–200%의 육안 검증이 아니다. 이 세션의 Computer Use가 Python GUI 창 제어를 거부하여 실제 대화형 검증은 미수행 상태다.
