# 구현 근거

- [scikit-learn: Common pitfalls and recommended practices](https://scikit-learn.org/stable/common_pitfalls.html) — 전처리를 `Pipeline` 안에 두어 각 학습 fold에서만 적합하고, 테스트 정보가 학습에 유입되지 않도록 하는 근거.
- [scikit-learn: Cross-validation](https://scikit-learn.org/stable/modules/cross_validation.html) — 검증 fold와 그룹·시간 분할을 구분하는 근거.
- [scikit-learn: Metrics and scoring](https://scikit-learn.org/stable/modules/model_evaluation.html) — 분류·회귀 지표 정의와 연속 점수 기반 ROC-AUC 계산 근거.
- [Matplotlib: Embed in Tk](https://matplotlib.org/stable/gallery/user_interfaces/embedding_in_tk_sgskip.html) — 네이티브 Tk GUI에 Matplotlib Figure를 넣는 공식 예제.
- [Python: multiprocessing](https://docs.python.org/3/library/multiprocessing.html) — Windows에서 학습을 별도 프로세스로 실행하고 종료하는 근거.
- [Python: venv](https://docs.python.org/3/library/venv.html) — 프로젝트 전용 환경 생성 근거.

문서 주소는 2026-11-01에 확인했다. 학습·평가 수치는 외부 문서나 AI가 생성하지 않고 프로그램이 입력 데이터로 직접 계산한다.
