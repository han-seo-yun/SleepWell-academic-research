"""Transparent suggestions use development rows only, never held-out outcomes."""
import numpy as np
import pandas as pd


def target_review(df, target, features, task):
    """Review prompts only. Strong association never implies causal leakage."""
    reviews = []
    y = df[target]
    for feature in features:
        if feature == target:
            continue
        valid = df[[feature, target]].dropna()
        if len(valid) > 4 and valid[feature].astype(str).equals(valid[target].astype(str)):
            reviews.append({"code": "review_target_copy", "column": feature})
        elif target.lower() in feature.lower():
            reviews.append({"code": "review_target_name", "column": feature})
        elif pd.api.types.is_numeric_dtype(df[feature]) and pd.api.types.is_numeric_dtype(y):
            r = valid[feature].corr(valid[target]) if len(valid) > 4 else np.nan
            if np.isfinite(r) and abs(r) >= .999:
                reviews.append({"code": "review_target_association", "column": feature})
    if task == "regression":
        values = pd.to_numeric(y, errors="coerce").dropna()
        q1, q3 = values.quantile([.25, .75]) if len(values) else (0, 0)
        iqr = q3 - q1
        count = int(((values < q1 - 1.5 * iqr) | (values > q3 + 1.5 * iqr)).sum())
        reviews.append({"code": "review_outliers", "column": target, "count": count})
    return reviews


def suggest(df, config):
    suggestions = []
    features = config.get("features", [])
    for feature in features:
        rate = float(df[feature].isna().mean()) * 100
        if rate >= 15 and len(features) > 1:
            suggestions.append({"code": "rec_missing", "values": {"feature": feature, "rate": rate},
                                "changes": {"features": [f for f in features if f != feature]}, "metric": config["primary_metric"]})
    numeric = df[features].select_dtypes(include="number").iloc[:, :40]
    corr = numeric.corr()
    seen = set()
    for i, feature in enumerate(corr.columns):
        for other in corr.columns[:i]:
            value = corr.loc[feature, other]
            if np.isfinite(value) and abs(value) >= .9 and feature not in seen and len(features) > 1:
                suggestions.append({"code": "rec_correlated", "values": {"feature": feature, "other": other, "r": abs(float(value))},
                                    "changes": {"features": [f for f in features if f != feature]}, "metric": config["primary_metric"]})
                seen.add(feature)
    if config["task"] == "classification" and config["model"] in {"linear", "forest", "mlp"}:
        counts = df[config["target"]].value_counts()
        if len(counts) > 1 and counts.max() / counts.min() >= 3 and config.get("class_weight") != "balanced":
            suggestions.append({"code": "rec_imbalance", "values": {"ratio": float(counts.max() / counts.min())},
                                "changes": {"class_weight": "balanced"}, "metric": "balanced_accuracy"})
    if config["model"] in {"linear", "mlp"} and config.get("scaling") != "robust":
        suggestions.append({"code": "rec_scale", "values": {}, "changes": {"scaling": "robust"}, "metric": config["primary_metric"]})
    return suggestions
