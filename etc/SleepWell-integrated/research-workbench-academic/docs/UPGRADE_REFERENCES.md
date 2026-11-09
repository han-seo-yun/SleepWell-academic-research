# 고도화 구현 참조 기록

기록일: 2026-11-02  
범위: 로컬 Windows/Tkinter 연구 도구의 문서 근거·검색·보고서 기능을 위한 구현 참조이다. 이 문서는 사용자 보고서의 자동 참고문헌이 아니며, 아래 표준·논문은 해당 보고서에서 실제로 사용한 경우에만 인용한다.

## 채택한 의존성과 실제 적용 범위

아래 버전은 현재 프로젝트의 기존 Windows 전용 `.venv`에서 설치와 import를 확인했고 requirements에 고정했다. 이는 **새 Windows 장비의 clean install 검증이 아니다**. 라이선스 확인 근거와 배포 경계는 [THIRD_PARTY_UPGRADE.md](THIRD_PARTY_UPGRADE.md)에 별도로 기록한다.

| 구성 | 채택 버전·공식 근거 | 실제 용도와 제약 |
|---|---|---|
| Python `sqlite3` | [SQLite documentation](https://www.sqlite.org/docs.html) | 프로젝트별 문서·청크·검색 감사 저장소다. 구현된 기본 검색은 FTS5가 아니라 scikit-learn의 word/character TF-IDF다. |
| scikit-learn `TfidfVectorizer` | [scikit-learn text feature extraction](https://scikit-learn.org/stable/modules/feature_extraction.html#text-feature-extraction) | 기본 lexical 검색이다. 의미 임베딩이나 사실 확률로 표시하지 않는다. 고정 test fixture에서 답변 가능 4건 Recall@3 `1.00`, MRR `1.00`, 근거 없음 2건 정확도 `1.00`(2/2)을 확인했다. 사람 의미 검토 결과는 아니다. |
| `python-docx` | `1.2.0`, [공식 문서](https://python-docx.readthedocs.io/en/latest/) | DOCX 추출과 구조화된 보고서 DOCX 생성에 채택했다. 고정 페이지가 없는 DOCX 인용은 절/문단 anchor를 사용한다. |
| `pypdf` | `6.21.4`, [공식 문서](https://pypdf.readthedocs.io/en/latest/), [추출 한계](https://pypdf.readthedocs.io/en/stable/user/extract-text.html) | PDF 텍스트 추출에 채택했다. OCR이 아니므로 이미지 전용 PDF는 `ocr_needed`로 표시하고 전문을 읽은 것으로 취급하지 않는다. |
| ReportLab | `5.1.0`, [공식 PyPI](https://pypi.org/project/reportlab/), [글꼴 안내](https://docs.reportlab.com/reportlab/userguide/ch3_fonts/) | 전체 보고서 PDF 생성에 채택했다. 한국어 출력은 Windows에 설치된 Malgun Gothic을 사용하며 글꼴 파일은 재배포하지 않는다. 글꼴이 없으면 명시적으로 실패한다. |
| `skops` | `0.16.0`, [공식 persistence 문서](https://skops.readthedocs.io/en/stable/persistence.html) | 동결한 scikit-learn pipeline의 안전 경계가 있는 저장·외부 재평가에 채택했다. 승인된 타입만 로드하며 환경·schema 불일치 시 거부한다. 기존 결과를 자동 변환하거나 덮어쓰지 않는다. |
| `keyring` | `25.7.0`, [공식 저장소](https://github.com/jaraco/keyring) | 선택적 외부 AI 자격증명을 Windows 자격 증명 저장소에 보관하는 데 채택했다. 키는 프로젝트·로그·내보내기 파일에 저장하지 않는다. 실제 외부 공급자 호출은 API 키 부재로 검증하지 못했다. |
| `sentence-transformers` | 선택 설치 `6.2.0`, [공식 문서](https://sbert.net/docs/sentence_transformer/pretrained_models.html) | 아래 고정 로컬 모델을 이용한 experimental hybrid 검색에만 사용한다. 기본 설치와 기본 검색의 필수 조건이 아니다. |

### 채택한 선택적 다국어 모델과 고정 평가

[sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2)를 Apache-2.0 조건으로 준비했고 revision `e8f8c211226b894fcb81acc59f3b34ba3efd5f42`에 고정했다. 로컬 model directory SHA-256은 `71d5898486c1fcbdd2cd348e36b7e962e7eae828a267f139bd069c281cbebd5c`이며 `local_files_only=True` 재로딩을 확인했다.

사전에 정한 기준은 Recall@3 ≥ `0.80`, answerable MRR ≥ `0.80`, no-evidence accuracy = `1.00`이었다. 실제 미사용 test fixture에서 답변 가능 4건은 Recall@3 `1.00`, MRR `1.00`이었지만 근거 없음 2건은 `0.50`(1/2)이어서 **hybrid 수용 기준을 통과하지 못했다**. test 결과를 보고 임계값을 조정하지 않았다. 따라서 기본값은 lexical이며 hybrid는 UI와 문서에서 `실험적`으로 표시한다. 이 수치는 검색 fixture 결과이며 인용의 의미상 뒷받침이나 실제 연구 문헌 성능을 입증하지 않는다.

### 오프라인·캐시 경계

[Hugging Face Transformers installation documentation](https://huggingface.co/docs/transformers/installation)는 사전 다운로드된 캐시/로컬 경로를 사용하고 `HF_HUB_OFFLINE=1`로 HTTP 요청을 막는 오프라인 사용법을 설명한다. Windows 기본 cache 위치에 의존하지 말고 프로젝트 설정에 승인된 model path 또는 cache root를 기록한다. 인터넷이 없고 검증된 모델 snapshot이 없으면 임베딩 검색은 `사용 불가`로 남기고 keyword search만 실행한다. 로컬 임베딩 인덱스가 존재한다는 사실은 로컬 생성형 AI 또는 전체 AI 기능의 오프라인 동작을 뜻하지 않는다.

## 권장된 데이터 경계

1. 실험 결과 저장소와 지식베이스를 분리한다. 실험 수치·분모·단위·반올림은 구조화된 run 결과에서만 읽고, 벡터/키워드 검색 또는 생성 모델에서 생성하지 않는다.
2. 문서 원본, 버전, 접근일, 원본 해시, 권한, 추출 도구/오류를 document record에 보존한다. 청크에는 안정 ID와 source ID, source hash, 위치(page 또는 section/paragraph anchor), 권한/프로젝트 범위를 전달한다.
3. 검색 전에 프로젝트·권한·언어·버전 필터를 적용하고, query/filters/rank/score/선택·제외 사유를 audit log에 남긴다. 검색 점수는 관련성 점수이지 사실성·검증 신뢰도가 아니다.
4. 문서 안의 지시는 데이터일 뿐 도구·파일·권한·시스템 설정을 바꾸는 명령이 아니다. 독성/프롬프트 인젝션과 교차 프로젝트 문서 노출은 평가 fixture로 검증한다.

이 경계는 [OWASP RAG Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/RAG_Security_Cheat_Sheet.html)의 retrieval 단계 접근 제어, chunk metadata 전파, prompt-injection 방어, 검색 감사 권고와 일치하도록 설계한다. OWASP는 검색된 문서를 신뢰 지시가 아니라 데이터로 다룰 것을 요구한다.

## 보고서·거버넌스 참고의 적용 범위

| 자료 | 확인된 범위 | 제품 내 적용 방식 |
|---|---|---|
| [NIST AI RMF 1.0 (NIST AI 100-1)](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf) | 2023-01 발행, 자발적·비분야특정·use-case agnostic risk-management guidance. [NIST publication page](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-ai-rmf-10)도 현재 개정 작업을 알린다. | 위험·평가·문서화·추적성 체크리스트의 설계 참고다. 인증, 규제 적합성 또는 품질 보증을 의미하지 않는다. 적용한 edition/version을 보고서 manifest에 기록한다. |
| [NIST Generative AI Profile (NIST AI 600-1)](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf) | 2024-07 생성형 AI risk-management profile. | 선택적 LLM 서술에서 provenance, data/content flow, 한계 및 human review 기록을 설계하는 참고다. API/LLM을 쓰지 않는 구조화 보고서의 필수 전제는 아니다. |
| [Model Cards for Model Reporting](https://arxiv.org/abs/1810.03993) | 2019 개정 논문; 모델의 intended use, 평가 절차·데이터·집단별 성능을 문서화하는 틀을 제안한다. | 모델 카드 템플릿의 의도된/비지원 용도, 평가 범위, 제한사항, 검토 정보를 구성하는 참고다. 모든 모델의 외부 타당성을 증명하지 않는다. |
| [Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks](https://arxiv.org/abs/2005.11401) | 2021 개정 논문; parametric memory와 retrieved non-parametric memory 결합을 제안한다. | 문헌 근거 검색과 모델 생성 결과를 분리하는 역사적·개념적 참고다. 안전성, 인용 정확성, 또는 현 제품 성능의 보증 근거가 아니다. |
| [TRIPOD+AI](https://www.bmj.com/content/385/bmj-2023-078378) | 의료 예측 연구의 보고 지침에만 조건부로 검토할 공식 자료다. 이 작업 환경에서는 전문 접근을 검증하지 못했다. | 프로젝트가 의료 예측 연구임을 사용자가 지정한 경우에만 checklist applicability를 `검토 필요`로 열고, 확보·검증한 정확한 출판 버전과 항목 근거에 연결한다. 그 외 프로젝트에는 `해당 없음`으로 표시한다. |

## 완료된 확인과 남은 검증 경계

- 위 패키지는 requirements에 버전을 고정했고 현재 Windows의 **기존 프로젝트 `.venv`**에서 설치/import와 관련 테스트를 확인했다. 별도의 깨끗한 Windows 장비에서 처음부터 설치하는 검증은 수행하지 않았다.
- source ZIP packaging은 `.venv`, `.models`, 사용자 `projects`를 제외한다. 사용자는 requirements로 의존성을 설치하고, 선택적 모델은 별도 준비 스크립트를 실행한다.
- DOCX/PDF 한글·영문 자동 테스트와 Tk 내부 geometry 검사는 수행했다. 그러나 Computer Use가 Python GUI 창 제어를 허용하지 않아 실제 Windows 디스플레이 배율 100–200%에서 육안·대화형 검증은 수행하지 못했다.
- Malgun Gothic은 Windows 시스템 글꼴을 참조하며 패키지에 재배포하지 않는다. 이 글꼴이 없는 환경의 한국어 전체 보고서 PDF는 명시적으로 실패한다.
- PDF의 OCR은 별도 사용자의 선택·도구·언어팩·데이터 처리 범위로 취급한다. 추출 텍스트가 없거나 위치를 보존하지 못하면 원문을 읽은 것처럼 인용하지 않는다.
- frozen-model external evaluation은 안전한 persistence, 스키마 검증, 입력 hash, 코드/환경 version, 데이터 격리 및 새로운 run version을 갖출 때만 제공한다.
- 실제 검색/인용 품질 주장은 versioned Korean/English evaluation set의 holdout 결과, 실제 분모, 인용 위치 검증 및 사람 표본 검토와 분리해 제시한다.
