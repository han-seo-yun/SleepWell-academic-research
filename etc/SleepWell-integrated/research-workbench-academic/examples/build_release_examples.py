"""Build real, deterministic release examples in a new empty directory."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from researchbench.data import fingerprint, load_data
from researchbench.engine import make_split_plan, run_experiment
from researchbench.plotting import export_figure, export_tables, make_figure
from researchbench.storage import Project

from .end_to_end import ROOT, config as base_config, run_case


def _roles(df, targets: list[str], features: list[str]) -> dict:
    roles = {column: "excluded" for column in df.columns}
    roles.update({feature: "feature" for feature in features})
    for column, role in (("participant_id", "id"), ("group_id", "group"), ("visit_date", "time")):
        if column in roles:
            roles[column] = role
    roles.update({target: "target" for target in targets})
    return roles


def _formats(output: Path, stem: str, figure) -> None:
    for suffix in ("png", "pdf", "svg"):
        export_figure(figure, output / f"{stem}.{suffix}", dpi=300)


def _english_baselines(output: Path) -> None:
    for task, metric in (("classification", "roc_auc"), ("regression", "rmse")):
        project = Project.open(output / f"{task}.rwb")
        results = project.experiments()
        baseline = project.state["baselines"][task]
        figure = make_figure("comparison", results=results, metric=metric, lang="en", baseline_id=baseline)
        _formats(output, f"{task}_comparison_en", figure)
        export_tables(results, output / f"{task}_results_en.xlsx", baseline, "en")
        project.close()


def _multiclass(output: Path) -> None:
    source = ROOT / "samples" / "multiclass_mixed.csv"
    df = load_data(source)
    features = ["age", "biomarker", "treatment", "site"]
    config = {
        **base_config("classification", "risk_group", features, fingerprint(df), "multiclass-linear"),
        "name": "Multiclass linear baseline",
        "positive_class": None,
        "average": "macro",
        "seed": 84,
        "split": "random",
        "test_size": 0.2,
        "folds": 3,
    }
    plan = make_split_plan(df, config)
    result = run_experiment(df, config, plan)
    project = Project.create(output / "multiclass.rwb", df, "Multiclass example", str(source))
    project.state.update(
        roles=_roles(df, ["risk_group"], features),
        tasks={"classification": config},
        splits={"classification": plan},
        baselines={"classification": result["id"], "regression": None},
    )
    project.save_experiment(result)
    project.close()
    for language in ("ko", "en"):
        figure = make_figure("confusion", results=[result], lang=language)
        _formats(output, f"multiclass_confusion_{language}", figure)
        export_tables([result], output / f"multiclass_results_{language}.xlsx", result["id"], language)


def _mlp(output: Path) -> None:
    source = ROOT / "samples" / "regression_mixed.xlsx"
    df = load_data(source)
    features = ["age", "biomarker", "treatment", "site"]
    config = {
        **base_config("regression", "outcome_score", features, fingerprint(df), "mlp-regression"),
        "name": "MLP regression baseline",
        "model": "mlp",
        "params": {"epochs": 8, "hidden_layer_sizes": [24, 12], "batch_size": 24},
        "target_transform": "standard",
        "seed": 62,
        "split": "random",
        "test_size": 0.2,
        "folds": 3,
    }
    plan = make_split_plan(df, config)
    result = run_experiment(df, config, plan)
    project = Project.create(output / "mlp_regression.rwb", df, "MLP regression example", str(source))
    project.state.update(
        roles=_roles(df, ["outcome_score"], features),
        tasks={"regression": config},
        splits={"regression": plan},
        baselines={"classification": None, "regression": result["id"]},
    )
    project.save_experiment(result)
    project.close()
    for language in ("ko", "en"):
        figure = make_figure("learning", results=[result], lang=language)
        _formats(output, f"mlp_learning_{language}", figure)
        export_tables([result], output / f"mlp_results_{language}.xlsx", result["id"], language)


def _dual(output: Path) -> None:
    source = ROOT / "samples" / "dual_tasks.csv"
    df = load_data(source)
    features = ["age", "biomarker", "treatment", "site"]
    shared = ["clinical_response", "outcome_score"]
    classification = {
        **base_config("classification", "clinical_response", features, fingerprint(df), "dual-classification"),
        "name": "Dual classification",
        "positive_class": "responder",
        "shared_targets": shared,
        "stratify_target": None,
        "split": "random",
        "test_size": 0.2,
        "folds": 3,
        "seed": 55,
    }
    regression = {
        **base_config("regression", "outcome_score", features, fingerprint(df), "dual-regression"),
        "name": "Dual regression",
        "shared_targets": shared,
        "split": "random",
        "test_size": 0.2,
        "folds": 3,
        "seed": 55,
    }
    plan = make_split_plan(df, classification)
    class_result = run_experiment(df, classification, plan)
    reg_result = run_experiment(df, regression, plan)
    project = Project.create(output / "dual_tasks.rwb", df, "Dual task example", str(source))
    project.state.update(
        roles=_roles(df, shared, features),
        tasks={"classification": classification, "regression": regression},
        splits={"classification": plan, "regression": plan},
        baselines={"classification": class_result["id"], "regression": reg_result["id"]},
        ui={"language": "ko", "task": "classification", "drafts": {}},
    )
    project.save_experiment(class_result)
    project.save_experiment(reg_result)
    project.close()
    for language in ("ko", "en"):
        class_figure = make_figure("roc", results=[class_result], lang=language)
        regression_figure = make_figure("predicted", results=[reg_result], lang=language)
        _formats(output, f"dual_classification_roc_{language}", class_figure)
        _formats(output, f"dual_regression_predicted_{language}", regression_figure)
        export_tables([class_result], output / f"dual_classification_{language}.xlsx", class_result["id"], language)
        export_tables([reg_result], output / f"dual_regression_{language}.xlsx", reg_result["id"], language)


def build(output: Path) -> None:
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"release_example_directory_not_empty:{output}")
    output.mkdir(parents=True, exist_ok=True)
    run_case(output, "classification")
    run_case(output, "regression")
    _english_baselines(output)
    _multiclass(output)
    _mlp(output)
    _dual(output)
    files = []
    for path in sorted(item for item in output.rglob("*") if item.is_file()):
        files.append(
            {
                "path": path.relative_to(output).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    (output / "manifest.json").write_text(
        json.dumps({"generated_from_synthetic_samples": True, "files": files}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Built {len(files)} real example files in {output}")


if __name__ == "__main__":
    build(ROOT / "deliverables" / "examples")
