# 실제 GUI 시험 관찰

- 실행 창: SleepWell Research, E: 설치 코드. 기존 OneDrive 창은 조작하지 않음.
- CMI: 기본 제공 CMI 자료 4,627행 / 269명. 결측 9,254개, 중복 0개. 선택 범위 train 3명 / 최대 12밤, 실제 36행 / 공통 비교 33밤.
- 연결 모드: 연결한 AI로 분석, ChatGPT subscription / gpt-6-sol, 자동 예산.
- 대화 작업 ID: 1dd33cb0. 실험 요청 직후 작업 중 / 실험 계획 표시; 이어 성능 분석 / 지침 근거 검토 / 보고서 작성 표시.
- 초기 AI 분석 완료 시 같은 ID와 작업 완료 표시, 중단 버튼 비활성화. 재요청 보고서 생성 시 작업 중으로 전환.
- 데이터 품질 화면 이동 후 대화 복귀에도 작업 ID, 선택 실험 a7f89b67, 9개 모델, 취침 시각 / 참여자 동일 가중 / ±30분 유지.
- 사용량 패널: 실제 호출과 제공자 반환 입력·출력 토큰 표시; 진행 중 응답은 미완료로 구분. 계정 전체 사용량 / 구독 잔여량 / 초기화 시각 / 비용 조회 불가 표시.
- CMI 보고서 요청 중 unknown_evidence_reference 자동 검사 경고에 bounded_repair가 실행됨. 최종 결과는 별도 검증 기록 참조.

## 실제 시험 중 발견한 오류와 수정
- AI 계획의 공통 '최소 7밤' 설명을 모델별 실제 조건으로 수정. MESA 실제 계획에서 새 규칙 전달 확인.
- 자동 분석/보고서 예산의 9모델 50,000토큰 프로필이 1회 수정 응답 후 작성 단계를 막던 문제: 필수 3개 역할과 수정 1회 추정 예약을 반영, 76,000토큰으로 조정. 사용자 상한 80,000토큰·24회는 그대로 유지. 실제 계정 한도 아님.
- 프로젝트 변경 시 이전 실험 완료 표시가 남던 문제: 프로젝트 연결 시 이벤트/진행률 초기화.
- 대화 없는 직접 실험에서도 실제 실행 상태와 실험 수행 표시를 상단에 반영.
- E 앱 환경 통합 회귀시험: 95개 통과 (85.437초). 종료된 Tk 창의 기존 ThemeChanged 경고 1건; 시험 결과 OK.
- E 앱 환경 자동 예산 변경 시험: 7개 통과. 이전 base Python 확장 시험의 keyring/fixture 오류는 앱 환경 실행 결과와 분리.
- MESA 데이터 품질: 14,761행 / 2,139명 / 결측 40셀 / 중복 0행. train에서 첫 3명, 최대 12밤을 선택했지만 실제 보유 자료는 21밤, 공통 비교 18밤.
- MESA MAE, RMSE, ROC·분류 지표 탭 확인. ROC·분류 탭은 적용 대상 아님을 설명하고 분류 수치를 생성하지 않음.
- 수정본 재실행 후 MESA 작업 ID ee82bf11: 실험 계획 → 성능 분석 → 지침 근거 검토. 자동 예산 trace effective=76,000 확인.

## 추가 실시간 확인
- MESA: 실험 계획 실제 ChatGPT 2회(계획 검수 수정 포함), 비교 분석 3회, RAG 보고서 3회 완료. GUI에서 MESA_live_report.pdf 저장 성공.
- CMI 재시도: effective 76,000 / hard 80,000 토큰 계획으로 46,706토큰 사용 뒤 지침 문장 검사에서 중단. 예산 초과가 아니라 unsupported_or_unbound_statement에 따른 안전한 실패이며 상단 실패 표시/중단 비활성/재시도 가능을 확인.
- CMI 사용량 패널: 이 프로젝트 4개 에이전트 실행(완료2/실패2), 실제 응답10회, 입력119,619/출력14,453=134,072토큰 표시가 trace와 일치. 비용·계정전체 사용량·구독잔여량·초기화시각은 조회할 수 없음.
- 기존 프로젝트 입력·완료실험·보고서·모델 보존 목록 96개 SHA-256 재확인: 변경0.
- PDF 세부 검토에서 여러 참여자의 동일 날짜를 기준 그래프가 덮어쓰던 오류 발견. 기존 숫자/예측값은 보존하고 그래프 렌더링 수정 중. 최초 MESA PDF는 보존하고 수정본을 별도 파일로 저장 예정.

## 최종 결과
- CMI report_407e1318ff4b4e0cb795e3ab921b4e60: 실제 ChatGPT 3역할/3호출 완료, 42,108토큰 / 자동76,000토큰 / 사용자상한80,000. 이번 재시도는 수정 호출 없이 통과. 작업ID1dd33cb0, 선택실험a7f89b67 유지.
- CMI 미리보기에서 PDF 저장 버튼을 눌러 CMI_live_report.pdf 저장. 기본 RAG 화면에 정확한 파일 경로의 PDF 저장 완료 표시 확인. 미리보기 child와 root의 저장 대화상자 소유 관계는 root 창으로 조작하여 완료.
- CMI 작업 완료 이후 실행 중 에이전트 표시가 사라지고 중단 버튼 비활성화 확인. 분석 중에는 진행 문구만 표시, 선행 로컬 답변/표 없음.
- MESA 최종 참조본: MESA_live_report_verified.pdf. 최초 MESA_live_report.pdf와 중간 산출물은 보존. 표의 기존 지표는 변경하지 않고 누락된 참여자별 점과 평가 범위 표기를 수정.
- PDF 검증: MESA/CMI 각각19쪽, 자동 검사 오류0/경고28, 한글/Matplotlib 그림/지침 근거표 존재. 저장된 예측값에서 재계산한 지표와 일치. 상세 결과 live_output_audit.json 및 각 visual_review 참조.
- PDF 재저장: ProviderClient.complete를 호출하면 즉시 실패하는 모의 경계로 두 기존 확정 보고서를 내보내도 성공. 기존 report/trace/project.json 해시 변경 없음. 실제 AI 재호출0.
- 추가 GUI 재저장 시험은 입력 동작 이전 사용자 입력 감지로 실행되지 않음. 최신 화면을 확인하니 사용자가 데이터 품질 탭으로 이동한 상태라 그대로 보존. 최초 GUI 저장과 서비스 재저장 검증 결과로 구분 기록.
- 근거 프롬프트 보완: A/B 문헌·프로젝트 출처 별칭과 실제 참조 ID를 명시적으로 구분. 엄격한 수치·출처 검증 및 수정1회 제한 유지. 관련25시험 통과.
- 대표 페이지 직접 시각 확인: CMI1·4쪽, MESA5쪽. 참여자별33/18개 기록이 그림에 유지되고 표/그림/한글 잘림 없음. 추가 페이지 검토는 verifier 기록 참조.
- 기존 고정 모델9개, 취침 시각, 참여자 동일 가중, ±30분 프로젝트 설정. 각 자료 train 첫3명에 최대12밤 범위를 택한 기능 시험이며 전체 코호트나 독립 최종 검증이 아님. 외부 검증/연구자 최종 TRIPOD·PROBAST 판단은 완료 처리하지 않음.

## 이번 실제 시험 중 변경한 코드
- researchbench/academic_app.py, researchbench/simple_workflow.py, researchbench/desktop_shell.py: 프로젝트/직접 실행 상태 표시.
- researchbench/desktop_chat.py: 모델별 실제 과거 관측 조건을 계획에 전달.
- researchbench/ai_budget.py: 필수 역할 및 수정 호출을 고려한 자동 토큰 예산(사용자 상한 유지).
- researchbench/report_agents.py: 출처 ID와 허용 문장 규칙을 초기·수정 프롬프트에 명시.
- researchbench/comparison_report_figures.py: 참여자·cutoff별 그래프 관측점 보존.
- researchbench/report_export.py: 명시적 최종 분할이 없으면 공통 평가로 표기.
- 대응 tests/test_desktop_chat.py, tests/test_desktop_shell.py, tests/test_simple_workflow.py, tests/test_ai_budget.py, tests/test_ai_connections.py, tests/test_report_agents.py, tests/test_comparison_report_figures.py, tests/test_comparison_report_export.py.
