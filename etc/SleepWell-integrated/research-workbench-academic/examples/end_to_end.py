"""Run classification and regression examples without opening the GUI.

This produces real validation results, persisted projects, tables and figures. It
is also a useful installation diagnostic: any exception causes a non-zero exit.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from researchbench.data import fingerprint, load_data
from researchbench.engine import make_split_plan, run_experiment
from researchbench.plotting import export_figure, export_tables, make_figure
from researchbench.storage import Project


ROOT = Path(__file__).resolve().parents[1]


def config(task: str, target: str, features: list[str], data_hash: str, experiment_id: str) -> dict:
    classification = task == "classification"
    return {
        "id": experiment_id,
        "name": "기준 실험" if experiment_id.endswith("baseline") else "biomarker 제거",
        "task": task,
        "target": target,
        "features": features,
        "model": "linear",
        "params": {"max_iter": 400},
        "imputer": "median",
        "encoding": "onehot",
        "scaling": "standard",
        "positive_class": "yes" if classification else None,
        "threshold": 0.5,
        "average": "macro",
        "class_weight": "balanced" if classification else None,
        "target_unit": "점" if not classification else None,
        "target_transform": "none",
        "device": "cpu",
        "primary_metric": "roc_auc" if classification else "rmse",
        "data_hash": data_hash,
        "seed": 42,
    }


def run_case(output: Path, task: str) -> None:
    if task == "classification":
        source = ROOT / "samples" / "classification_mixed.csv"
        target = "response"
    else:
        source = ROOT / "samples" / "regression_mixed.xlsx"
        target = "outcome_score"
    df = load_data(source)
    data_hash = fingerprint(df)
    features = ["age", "biomarker", "treatment", "site"]
    base = config(task, target, features, data_hash, f"{task}-baseline")
    split_config = {**base, "split": "random", "test_size": 0.2, "folds": 3, "seed": 42}
    plan = make_split_plan(df, split_config)
    comparison = config(task, target, [f for f in features if f != "biomarker"], data_hash, f"{task}-drop")

    results = [run_experiment(df, base, plan), run_experiment(df, comparison, plan)]
    project = Project.create(output / f"{task}.rwb", df, f"예제 {task}", str(source))
    roles = {column: "excluded" for column in df.columns}
    roles.update({feature: "feature" for feature in features})
    roles.update({"participant_id": "id", "group_id": "group", "visit_date": "time", target: "target"})
    project.state["roles"] = roles
    project.state["tasks"] = {task: split_config}
    project.state["baselines"] = {task: base["id"]}
    project.state["splits"] = {task: plan}
    for result in results:
        project.save_experiment(result)
    project.save()
    project.close()

    metric = "roc_auc" if task == "classification" else "rmse"
    figure = make_figure("comparison", results=results, metric=metric, lang="ko", baseline_id=base["id"])
    export_figure(figure, output / f"{task}_comparison.png", dpi=300)
    export_figure(figure, output / f"{task}_comparison.pdf", dpi=300)
    export_figure(figure, output / f"{task}_comparison.svg", dpi=300)
    export_tables(results, output / f"{task}_results.xlsx", baseline_id=base["id"], lang="ko")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "example_output_verified")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    run_case(args.output, "classification")
    run_case(args.output, "regression")
    print(f"완료: {args.output.resolve()}")


if __name__ == "__main__":
    main()
