"""Batch command line for the research lab (same engine and charts as the GUI).

Scripted, reproducible runs in the style of Luna / GGIR: one request in, one
self-describing output folder out. Nothing is uploaded or written outside --out.

    python research_lab_cli.py --csv rows.csv --cohorts cmi,mesa --out results/run1
    python research_lab_cli.py --request SYNTHETIC_시연설정.json --out results/demo
    python research_lab_cli.py --csv rows.csv --out results/quick --no-figures

Output folder:
    run.json                       run id, versions, input SHA-256, command, elapsed time
    result.json                    exact evaluation result (same as the GUI 'JSON 받기')
    metrics.csv                    same-night participant-equal metrics (+ available_metrics)
    observed_regularity.csv        SRI, IS/IV and timing variability per participant
    observed_completeness.csv      completeness and reporting delay per participant
    figures.zip                    publication figure bundle (PNG/PDF/SVG, source data, reproduce.py)
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import platform
import sys
from time import perf_counter

from research_lab import COHORTS, LabInputError, SCHEMA_VERSION, evaluate_run

EXIT_INPUT = 2
EXIT_OUTPUT = 3

METRIC_FIELDS = ("model_id", "n", "n_participants", "averaging", "joint_mae_min", "onset_mae_min",
                 "wake_mae_min", "interval_mae_min", "onset_rmse_min", "wake_rmse_min", "skill_vs_recent7")
REGULARITY_FIELDS = ("participant_id", "labeled_nights", "sri_day_pairs", "sri", "interdaily_stability",
                     "intradaily_variability", "onset_mean_clock", "wake_mean_clock", "midpoint_mean_clock",
                     "onset_sd_min", "wake_sd_min", "midpoint_sd_min", "duration_mean_min", "duration_sd_min",
                     "weekend_weekday_midpoint_gap_min", "weekend_nights", "weekday_nights")
COMPLETENESS_FIELDS = ("participant_id", "first_date", "last_date", "span_days", "rows", "labeled_nights",
                       "timely_nights", "late_nights", "prediction_only_nights", "missing_nights",
                       "labeled_coverage", "timely_share_of_labeled", "report_delay_median_min",
                       "report_delay_p90_min", "report_delay_max_min")


def _csv_bytes(fields, rows, extra=None):
    output = io.StringIO(newline="")
    columns = list(fields) + ([extra[0]] if extra else [])
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        values = {key: row.get(key) for key in fields}
        if extra:
            values[extra[0]] = extra[1]
        # Spreadsheet-safe participant labels, as in the publication source CSV.
        for key, value in values.items():
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
                values[key] = "'" + value
        writer.writerow(values)
    return output.getvalue().encode("utf-8-sig")


def build_payload(args):
    if args.request:
        payload = json.loads(Path(args.request).read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise LabInputError("--request must contain a JSON object.")
        source = Path(args.request).read_bytes()
    else:
        text = Path(args.csv).read_text(encoding="utf-8-sig")
        cohorts = [item.strip().lower() for item in args.cohorts.split(",") if item.strip()]
        payload = {"schema_version": SCHEMA_VERSION, "mode": "real", "cohorts": cohorts, "csv_text": text}
        source = Path(args.csv).read_bytes()
    if args.chart_participant:
        payload["chart_participant_id"] = args.chart_participant
    return payload, hashlib.sha256(source).hexdigest()


def run(args):
    out = Path(args.out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        print(f"출력 폴더가 비어 있지 않습니다: {out}", file=sys.stderr)
        return EXIT_OUTPUT
    try:
        payload, input_sha = build_payload(args)
        started = perf_counter()
        result = evaluate_run(payload, models_dir=args.models_dir)
        figures = None
        if not args.no_figures:
            from research_lab_charts import export_figures
            figures = export_figures(result)
        elapsed = perf_counter() - started
    except (LabInputError, ValueError, OSError) as error:
        print(f"입력 오류: {error}", file=sys.stderr)
        return EXIT_INPUT

    observations = result.get("observations", {})
    files = {
        "result.json": json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8"),
        "metrics.csv": _csv_bytes(METRIC_FIELDS, result["metrics"], ("set", "common_nights"))
        + _csv_bytes(METRIC_FIELDS, result["available_metrics"], ("set", "available")).split(b"\n", 1)[1],
        "observed_regularity.csv": _csv_bytes(REGULARITY_FIELDS, observations.get("regularity", [])),
        "observed_completeness.csv": _csv_bytes(COMPLETENESS_FIELDS, observations.get("completeness", [])),
    }
    if figures is not None:
        files["figures.zip"] = figures
    run_record = {
        "tool": "SleepWell research_lab_cli",
        "run_id": result["run_id"],
        "mode": result["mode"],
        "evidence_label": result["evidence_label"],
        "runtime_model_version": result["runtime_model_version"],
        "personalization_policy": result["personalization_policy"],
        "models": [{"id": item.get("id"), "artifact_sha256": item.get("artifact_sha256")}
                   for item in result.get("models", [])],
        "input_sha256": input_sha,
        "command": [Path(sys.argv[0]).name] + sys.argv[1:],
        "python": platform.python_version(),
        "platform": platform.platform(terse=True),
        "elapsed_seconds": round(elapsed, 3),
        "summary": result["summary"],
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
        "scope": "Research comparison only; not clinical, prospective or treatment-effect evidence.",
    }
    try:
        out.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (out / name).write_bytes(content)
        (out / "run.json").write_text(json.dumps(run_record, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as error:
        print(f"출력 저장 실패: {error}", file=sys.stderr)
        return EXIT_OUTPUT

    if not args.quiet:
        summary = result["summary"]
        print(f"run {result['run_id'][:12]} · {result['mode']} · 참가자 {len(summary['participants'])}명 · "
              f"공통 평가 밤 {summary['common_nights']} → {out}")
        for row in result["metrics"]:
            joint = row.get("joint_mae_min")
            print(f"  {row['model_id']:<24} joint MAE {'—' if joint is None else f'{joint:6.2f}'} 분 (n={row['n']})")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="research_lab_cli",
        description="SleepWell 연구 분석실 배치 실행: GUI와 같은 엔진으로 평가하고 결과 폴더를 만듭니다.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", help="표준 5열 CSV (participant_id,cutoff,sleep_start,sleep_end,observed_at)")
    source.add_argument("--request", help="연구 분석실 요청 JSON (예: SYNTHETIC_시연설정.json)")
    parser.add_argument("--cohorts", default=",".join(COHORTS), help="--csv와 함께 쓸 코호트 (기본: cmi,mesa)")
    parser.add_argument("--chart-participant", help="그림에 표시할 참가자 ID")
    parser.add_argument("--out", required=True, help="새 출력 폴더 (비어 있어야 함)")
    parser.add_argument("--models-dir", default=None, help="모델 폴더 (기본: web-app/models)")
    parser.add_argument("--no-figures", action="store_true", help="그림 묶음을 만들지 않음")
    parser.add_argument("--quiet", action="store_true")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
