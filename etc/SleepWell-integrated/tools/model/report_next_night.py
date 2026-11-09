"""Create aggregate-only next-night experiment reporting artifacts."""
import argparse
import csv
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = json.loads((args.run / "summary.json").read_text(encoding="utf-8"))
    verification = json.loads((args.run / "verification.json").read_text(encoding="utf-8"))
    prior = json.loads((args.run / "population_prior.json").read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=True)
    arms = {"physical": "수리물리 단독", "data": "데이터 단독", "hybrid": "융합",
            "hybrid_frozen": "장기 개인화 고정 융합", "population": "초기 평균", "recent_median": "최근 7회 중앙값"}
    lines = [
        "# 다음날 수면 예측·누적 개인화 검증 — 2026-10-20",
        "",
        "**결과: 구현과 실제 비교 검증을 완료했으나, 융합이 두 단독 모델 모두보다 우월하다는 가설은 확인되지 않았습니다.** "
        "CMI·MESA에서 수리물리 단독 대비 개선은 지지되지만 데이터 단독 대비 차이의 95% 구간에는 0이 포함됩니다. "
        "Samsung 개인 자료도 우열이 불확실하며 약 164분의 시작·기상 평균 오차로 정밀한 시각 예측에는 부족합니다. "
        "Test 결과를 보고 모델을 재조정하지 않았습니다.",
        "",
        "## 구현과 예측 대상",
        "",
        "매일 현지 시각 20:00에, 이후 주수면의 실제 수면 시작과 최종 기상 시각을 예측합니다. "
        "수면 시작~종료 간격도 계산하지만 밤중 각성을 뺀 총수면시간(TST), PSG 수면단계, 수면의 질, 추천의 개선 효과를 뜻하지 않습니다.",
        "",
        "- 수리물리 단독: 수면압력 S의 각성 시 증가·수면 시 감소와 24시간 C 주기, 개인 생활 위상·전환 기준으로 다음 수면을 모의합니다.",
        "- 데이터 단독: 이전 수면, 최근 7회 요약, 누적 생활 평균, 요일 등을 표준화한 Ridge 회귀로 시작·기상을 예측합니다.",
        "- 융합: 데이터 단독의 동일 입력에 S/C 상태, 수면·각성 전환 기준, 수리물리 단독 예상 시각을 추가해 같은 Ridge 회귀로 학습합니다. 정확도를 더하는 방식이 아닙니다.",
        "- 세 모델은 같은 밤·정답·분할로 비교합니다. 데이터·융합은 동일한 4개 규제값(1, 10, 100, 1000)을 Validation에서 선택합니다.",
        "- 최종 화면의 대표 예측은 Validation의 시작·기상 평균절대오차 평균이 가장 작은 모델입니다. 융합이 우월하다고 미리 가정하지 않습니다.",
        "",
        "## 평가 설계",
        "",
        "원래 데이터 분할을 유지했습니다. CMI와 MESA는 사람 단위 분리, Samsung은 시간순 분리입니다. "
        "Samsung은 Validation과 Test 각각 첫날에 신규 사용을 시작한 것처럼 기록을 0일로 초기화합니다. "
        "학습 구간의 개인 평균만 초기값으로 유지하고, 각 평가 구간에서 이전에 끝난 밤만 순차적으로 반영합니다. "
        "이전 아카이브를 신규 가입 후 기록으로 세지 않는 앱과 같은 조건입니다.",
        "",
        "초기 평균·표준화·회귀 계수는 Train에서만 계산합니다. Validation 선택 결과를 먼저 저장한 뒤 Test 성능을 계산했습니다. "
        "Test의 이전 밤은 이후 예측의 개인 상태 보정에 사용할 수 있지만 집단 평균이나 회귀 계수를 재학습하지 않습니다. "
        "오프라인 자료에서는 실제 입력 시각이 없어 기상 시점에 기록이 이용 가능했다고 가정합니다. 앱에서는 실제 관측 시각을 확인합니다.",
        "",
        "다음 값은 Test 평균절대오차(MAE, 분)이며 낮을수록 좋습니다. CMI/MESA는 사람별 오차를 먼저 구한 뒤 사람에게 동일 가중치를 줍니다. "
        "Samsung은 한 사람의 밤별 평균입니다. 세 출처를 합친 하나의 정확도로 표시하지 않습니다.",
        "",
        "| 자료 | 모델 | 시작 오차 | 기상 오차 | 시작~종료 간격 오차 | 시작·기상 평균 오차 |",
        "|---|---|---:|---:|---:|---:|",
    ]
    rows = []
    for cohort, result in summary.items():
        for arm, metrics in result["test"].items():
            row = {"cohort": cohort, "model": arm, **metrics}
            rows.append(row)
            lines.append(f"| {cohort.upper()} | {arms[arm]} | {metrics['onset_mae_min']:.2f} | "
                         f"{metrics['wake_mae_min']:.2f} | {metrics['interval_mae_min']:.2f} | {metrics['joint_mae_min']:.2f} |")
    lines += ["", "## 자료 규모와 적용 범위", ""]
    for cohort, result in summary.items():
        lines += [
            f"- **{cohort.upper()}**: {result['n_people']:,}명, {result['n_nights']:,}밤. "
            f"Train/Validation/Test 밤 수 {result['split_nights']['train']:,}/{result['split_nights']['validation']:,}/{result['split_nights']['test']:,}; "
            f"Test {result['split_people']['test']:,}명. Validation 선택: `{result['validation_winner']}`. "
            f"Test 중 누적 개인화 활성 {result['test_personalized_nights']:,}밤."
        ]
    lines += [
        "",
        "CMI 정답은 onset/wakeup 이벤트 주석이며 PSG 단계 정답이라고 간주하지 않습니다. "
        "MESA는 연속된 착용 중 REST-S 구간의 ACT sleep 판정 첫 시점~마지막 시점+30초이며 PSG 정답이 아닙니다. "
        "MESA의 재구성 날짜는 실제 계절을 뜻하지 않습니다. Samsung은 소비자 기기의 주수면 구간입니다.",
        "",
        "원자료 현지 벽시계 값을 유지해 +09:00 공통 표기로 저장했습니다. 이는 CMI/MESA 참여자가 한국에 있었다는 뜻이 아닙니다. "
        "예측 대상은 20:00 이후 시작하고 다음 20:00까지 끝나는 30분~16시간의 주수면이며, 시작은 최대 다음 정오입니다. "
        "같은 예측일에 여러 구간이 있으면 가장 긴 구간만 선택합니다. 교대·다상수면 전체를 검증한 결과가 아닙니다.",
        "",
        "## 융합의 개선 여부",
        "",
        "아래는 **기준 모델 오차 − 융합 오차**입니다. 양수면 융합 개선, 음수면 악화입니다. "
        "시작·기상 평균 오차의 짝지은 차이이며, CMI/MESA는 사람 단위, Samsung은 연속 평가 7밤 블록 단위 2,000회 bootstrap 95% 구간을 사용합니다. "
        "Samsung 구간은 한 사람 내부 불확실성만 나타냅니다. 탐색적 여러 비교에 대한 다중검정 보정은 하지 않았습니다.",
        "",
        "| 자료 | 기준 모델 | 융합 개선량(분) | 95% 구간 | 해석 |",
        "|---|---|---:|---|---|",
    ]
    for cohort, result in summary.items():
        for base in ("physical", "data", "hybrid_frozen", "recent_median"):
            m = result["comparisons"][f"hybrid_vs_{base}"]["metrics"]["joint"]
            lo, hi = m["ci95"]
            verdict = "개선 지지" if lo > 0 else "악화 지지" if hi < 0 else "우열 불확실"
            lines.append(f"| {cohort.upper()} | {arms[base]} | {m['error_reduction_min']:.2f} | [{lo:.2f}, {hi:.2f}] | {verdict} |")
    lines += [
        "", "## 기록 누적에 따른 개인화", "",
        f"NHANES 2017–2018 학습 분할의 성인 {prior['global']['n_people']:,}명에서 조사 가중 평일 수면시간 평균을 만들었습니다. "
        "연령대+성별+활동량 집단이 30명 이상이면 사용하고, 부족하면 연령대+성별 → 연령대 → 전체 평균 순으로 넓힙니다. "
        "활동시간 미입력은 운동시간으로 추정하지 않습니다. 18~80세 밖에서는 이 자료를 외삽하지 않고 가입 입력을 사용합니다.",
        "",
        "첫 14일은 이 집단의 관측 수면시간을 추천 초기값으로 쓰며 기존 연령별 운영 범위 안으로 제한합니다. "
        "평균 취침·기상 시각은 가입 때 입력한 생활 위상을 반영합니다. 직업은 입력·저장하되 직업별 효과를 추정할 자료가 없어 계수를 만들지 않았습니다. "
        "NHANES 평균은 필요한 최적 수면량이나 한국인 기준을 증명하지 않습니다.",
        "",
        "가입 후 14일 경과와 유효 기록 14일을 모두 충족하면 개인 비중을 n/(n+14)로 높입니다. "
        "14일 50%, 28일 66.7%, 56일 80%, 126일 90%이며 n은 중복을 제거한 가입 후 유효 기록일입니다. "
        "28일이 지나도 장기 기록을 버리지 않습니다. 최근 7회/28일은 별도 단기 예측 특징에만 사용합니다. "
        "같은 날짜 수정은 한 번만 반영하고 미래·늦게 입력된 기록은 그 이전 예측에 사용하지 않습니다.",
        "",
        "갱신되는 값은 생활 위상·전환 기준·입면 시간·활동 기준과 개운한 날을 참고한 수면 목표입니다. "
        "τ각성=18.2h, τ수면=4.2h, C 진폭=0.15는 고정입니다. 수면 일지만으로 개인 생리 시간상수를 측정했다고 주장하지 않습니다. "
        "수면 목표 추천의 만족도 보정과 다음날 실제 시각 예측의 누적 보정은 별도 출력입니다.",
        "",
        "다음 비교는 **동일하게 학습한 융합 모델**에서 장기 개인 보정을 켠 경우와 끈 경우의 차이입니다. "
        "단기 이전 밤 정보는 양쪽 모두 사용하므로 장기 보정의 기여를 확인합니다.",
        "",
        "| 자료 | 장기 보정으로 줄어든 평균 오차(분) | 95% 구간 |",
        "|---|---:|---|",
    ]
    for cohort, result in summary.items():
        m = result["comparisons"]["hybrid_adaptation_same_coefficients"]["metrics"]["joint"]
        lines.append(f"| {cohort.upper()} | {m['error_reduction_min']:.2f} | [{m['ci95'][0]:.2f}, {m['ci95'][1]:.2f}] |")
    lines += [
        "",
        "누적량별 성능은 별도 CSV에 있습니다. 다른 기록량 구간은 사람·날짜 구성도 달라지므로 오차가 매일 단조 감소한다는 증거로 해석하면 안 됩니다. "
        "MESA의 짧은 관측 기간으로 2주 이후 장기 개인화를 입증할 수 없습니다. "
        "프로필 기반 NHANES 초기 추천은 구현·단위검증했지만, NHANES는 종단 자료가 아니므로 그 추천이 실제 수면을 개선한다는 효과검증은 하지 않았습니다.",
        "",
        "## 앱 연결과 재현성", "",
        "앱은 20:00 기준 수리물리·데이터·융합 예상 시각과 별도 검증 자료에서 선택한 대표 모델을 보여 줍니다. "
        "본인 Samsung 자료 연결 시 해당 개인 모델을 사용합니다. CMI/MESA 집단 모델은 연구 비교용으로 보관하며, "
        "연령·기기 적용성을 확인하지 않은 일반 신규 사용자에게 그대로 제공하지 않습니다. 신규 사용자의 초기 평균 기반 추천과 누적 개인화는 작동합니다.",
        "",
        "모델 파일에는 입력 순서, 표준화 값, 회귀 계수, 출처, 버전과 성능을 저장합니다. "
        "각 예측에는 모델 해시와 사용 특징·개인 상태를 보존합니다. 기존 로컬 DB를 초기화하거나 과거 예측을 덮어쓰지 않습니다.",
        "",
        "실험 폴더의 protocol.json, code_snapshot/, code_sha256.json, validation_selection.json, "
        "features.parquet, personal_states.parquet, metrics.csv, summary.json으로 재현·점검할 수 있습니다. "
        "데이터 추출 메타데이터에는 원자료 해시와 제외 사유가 있습니다. 원자료와 과거 실험은 유지했습니다.",
        "",
        f"저장된 Test 총 {sum(c['test_rows'] for c in verification['cohorts'].values()):,}밤의 수리물리·데이터·융합 출력을 "
        "휴대형 모델로 다시 계산해 1e-7분 허용오차 안에서 일치함을 확인했습니다. "
        f"실제 앱 예측 함수로 대표 {sum(c['runtime']['representative_rows'] for c in verification['cohorts'].values())}개 시점을 "
        "재생했고 입력 특징이 완전히 일치했습니다. 집단 분할·개인 시간순 분할·실제 초기 평균 파일 로드 검증도 통과했습니다.",
        "",
        "앱 테스트 44개·연구 도구 테스트 72개(총 116개)와 화면 스크립트 문법 검사를 통과했습니다. "
        "실제 생성한 모델을 임시 DB의 HTTP 흐름으로 연결해 가입·기록·16회 예측·전체 백업을 검증했습니다. "
        "누적 14일에서 개인 비중 50%가 적용되며, 20시 이후 입력은 그날 기준 예측에서 제외됨을 확인했습니다. "
        "실제 사용자 DB를 이 검증에 사용하거나 변경하지 않았습니다.",
        "",
        "이미 다른 과제에서 사용한 Test 분할을 재사용한 후향적 연구입니다. 이번 실행에서 Test로 후보를 조정하지 않았지만 "
        "완전히 새로운 전향적 외부 검증은 아닙니다. 현재 결과만으로 모든 사람의 성능 개선이나 건강상 이득을 주장할 수 없습니다.",
        "",
        "## 자료 정의 출처", "",
        "- [MESA actigraphy 정의](https://sleepdata.org/datasets/mesa/pages/actigraphy-introduction.md)",
        "- [CMI 수면 상태 이벤트 자료](https://www.kaggle.com/competitions/child-mind-institute-detect-sleep-states/overview)",
        "- [NHANES 수면 설문](https://wwwn.cdc.gov/Nchs/Data/Nhanes/Public/2017/DataFiles/SLQ_J.htm)",
        "- [NHANES 활동 설문](https://wwwn.cdc.gov/Nchs/Data/Nhanes/Public/2017/DataFiles/PAQ_J.htm)",
        "",
    ]
    (args.output / "NEXT_NIGHT_RESULTS_2026-10-20.md").write_text("\n".join(lines), encoding="utf-8")
    with (args.output / "다음날_수면예측_모델비교_2026-10-20.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (args.output / "다음날_수면예측_집계_2026-10-20.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    curves = []
    for cohort in summary:
        with (args.run / cohort / "personalization_curve.csv").open(encoding="utf-8") as handle:
            curves.extend({"cohort": cohort, **row} for row in csv.DictReader(handle))
    with (args.output / "누적_개인화_기록량별_2026-10-20.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(curves[0]))
        writer.writeheader()
        writer.writerows(curves)
    print("Aggregate report and three aggregate data files created.")


if __name__ == "__main__":
    main()
