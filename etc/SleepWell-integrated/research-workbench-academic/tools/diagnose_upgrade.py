"""Read-only installation diagnostics for the Windows research workbench."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import io
import json
import os
import platform
import sqlite3
import struct
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
CORE_IMPORTS = {
    "numpy": "numpy", "pandas": "pandas", "scipy": "scipy", "scikit-learn": "sklearn",
    "matplotlib": "matplotlib", "Pillow": "PIL", "openpyxl": "openpyxl",
    "pypdf": "pypdf", "python-docx": "docx", "reportlab": "reportlab",
    "skops": "skops", "keyring": "keyring",
}


def _version(module: Any) -> str | None:
    return getattr(module, "__version__", None) or getattr(module, "VERSION", None)


def _check(name: str, function: Callable[[], Any], *, required: bool = True) -> dict[str, Any]:
    try:
        detail = function()
        return {"name": name, "required": required, "status": "pass", "detail": detail}
    except Exception as error:
        return {"name": name, "required": required, "status": "fail" if required else "unavailable",
                "error": f"{type(error).__name__}: {error}"}


def _python_check() -> dict[str, Any]:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError(f"Python 3.12 required; running {platform.python_version()}")
    if struct.calcsize("P") * 8 != 64:
        raise RuntimeError("64-bit Python is required")
    return {"version": platform.python_version(), "bits": 64, "executable": sys.executable,
            "implementation": platform.python_implementation()}


def _venv_check() -> dict[str, Any]:
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("not running inside a virtual environment")
    expected = (ROOT / ".venv").resolve()
    actual = Path(sys.prefix).resolve()
    if actual != expected:
        raise RuntimeError(f"wrong virtual environment: {actual}; expected {expected}")
    return {"prefix": str(actual), "isolated_from": sys.base_prefix}


def _imports_check() -> dict[str, Any]:
    versions = {}
    for distribution, module_name in CORE_IMPORTS.items():
        module = importlib.import_module(module_name)
        versions[distribution] = _version(module) or importlib.metadata.version(distribution)
    return versions


def _requirements_lock_check() -> dict[str, Any]:
    expected = {}
    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            raise RuntimeError(f"core requirement is not exactly pinned: {line}")
        name, version = line.split("==", 1)
        expected[name.strip()] = version.strip()
    mismatches = {}
    for name, version in expected.items():
        actual = importlib.metadata.version(name)
        if actual != version:
            mismatches[name] = {"expected": version, "actual": actual}
    if mismatches:
        raise RuntimeError(f"requirements lock mismatch: {mismatches}")
    return {"pinned_distributions": len(expected), "versions_match": True}


def _tk_check() -> dict[str, Any]:
    import tkinter as tk
    interpreter = tk.Tcl()
    patchlevel = interpreter.eval("info patchlevel")
    root = tk.Tk()
    root.withdraw()
    root.update_idletasks()
    windowing_system = root.tk.call("tk", "windowingsystem")
    root.destroy()
    return {"tk_version": str(tk.TkVersion), "tcl_version": str(tk.TclVersion),
            "tcl_patchlevel": patchlevel, "windowing_system": windowing_system,
            "native_module": tk._tkinter.__file__}


def _font_check() -> dict[str, Any]:
    from matplotlib import font_manager
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen.canvas import Canvas
    path = Path(font_manager.findfont("Malgun Gothic", fallback_to_default=False)).resolve()
    if not path.is_file():
        raise RuntimeError("Malgun Gothic font file was not found")
    font_name = "SleepWellDiagnosticMalgun"
    pdfmetrics.registerFont(TTFont(font_name, str(path)))
    output = io.BytesIO()
    canvas = Canvas(output)
    canvas.setFont(font_name, 10)
    canvas.drawString(30, 800, "한글 PDF 진단")
    canvas.save()
    if not output.getvalue().startswith(b"%PDF"):
        raise RuntimeError("Korean PDF font embedding smoke failed")
    return {"family": "Malgun Gothic", "path": str(path),
            "pdf_embedding_smoke": True}


def _cpu_model_smoke() -> dict[str, Any]:
    import numpy as np
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    numeric = np.asarray([[0.0], [1.0], [2.0], [3.0], [4.0], [5.0]])
    target = np.asarray([0, 0, 0, 1, 1, 1])
    preprocessor = ColumnTransformer([("numeric", StandardScaler(), [0])])
    scores = {}
    for name, estimator in (
        ("linear", LogisticRegression(random_state=7)),
        ("forest", RandomForestClassifier(n_estimators=5, random_state=7, n_jobs=1)),
    ):
        pipeline = Pipeline([("preprocessor", preprocessor), ("estimator", estimator)])
        pipeline.fit(numeric, target)
        probability = pipeline.predict_proba([[2.5]])[0].tolist()
        if len(probability) != 2 or not all(np.isfinite(probability)):
            raise RuntimeError(f"{name} probability smoke failed")
        scores[name] = probability
    OneHotEncoder(handle_unknown="ignore", sparse_output=False).fit([['a'], ['b']]).transform([['c']])
    return {"device": "cpu", "probability_smoke": scores}


def _sqlite_check() -> dict[str, Any]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE evidence USING fts5(text)")
        connection.execute("INSERT INTO evidence VALUES (?)", ("근거 evidence",))
        count = connection.execute("SELECT count(*) FROM evidence WHERE evidence MATCH 'evidence'").fetchone()[0]
    finally:
        connection.close()
    if count != 1:
        raise RuntimeError("SQLite FTS5 query failed")
    return {"sqlite_version": sqlite3.sqlite_version, "fts5": True}


def _storage_check() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="sleepwell 진단 ") as directory:
        path = Path(directory) / "한글 공백 경로" / "roundtrip.json"
        path.parent.mkdir()
        payload = {"한글": "정상", "path": str(path)}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        if json.loads(path.read_text(encoding="utf-8")) != payload:
            raise RuntimeError("UTF-8 path round-trip failed")
    return {"unicode_and_space_path": True, "temporary_root": tempfile.gettempdir()}


def _vendor_check() -> dict[str, Any]:
    model_dir = ROOT / "vendor" / "sleepwell" / "models"
    files = sorted(model_dir.glob("*.json"))
    if not files:
        raise FileNotFoundError("SleepWell model JSON files are missing")
    return {"model_files": len(files), "directory": str(model_dir)}


def _optional_embedding_check() -> dict[str, Any]:
    model = ROOT / ".models" / "paraphrase-multilingual-MiniLM-L12-v2"
    manifest = model / "sleepwell_model_manifest.json"
    sentence_transformers = importlib.util.find_spec("sentence_transformers") is not None
    if not sentence_transformers or not manifest.is_file():
        raise RuntimeError("optional local embedding packages/model are not prepared")
    value = json.loads(manifest.read_text(encoding="utf-8"))
    return {"prepared": True, "path": str(model), "model_id": value.get("model_id"),
            "revision": value.get("revision"), "local_only_verified": value.get("local_files_only_verified")}


def diagnose() -> dict[str, Any]:
    checks = [
        _check("python_3_12_64bit", _python_check),
        _check("project_virtual_environment", _venv_check),
        _check("core_dependencies", _imports_check),
        _check("requirements_lock", _requirements_lock_check),
        _check("native_tk", _tk_check),
        _check("korean_pdf_font", _font_check),
        _check("cpu_model_smoke", _cpu_model_smoke),
        _check("sqlite_fts5", _sqlite_check),
        _check("unicode_space_paths", _storage_check),
        _check("sleepwell_cpu_models", _vendor_check),
        _check("optional_local_embeddings", _optional_embedding_check, required=False),
    ]
    failures = [item["name"] for item in checks if item["required"] and item["status"] != "pass"]
    return {"schema": 1, "generated_at": datetime.now().astimezone().isoformat(),
            "project_root": str(ROOT), "platform": platform.platform(),
            "status": "pass" if not failures else "fail", "required_failures": failures,
            "checks": checks,
            "notes": ["This release supports CPU execution; GPU execution is not provided or verified.",
                      "Local embeddings are optional; lexical evidence search and template reports work without them."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print JSON only")
    parser.add_argument("--output", type=Path, help="also write the JSON report to this path")
    arguments = parser.parse_args()
    result = diagnose()
    if arguments.output:
        destination = arguments.output.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if arguments.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"SleepWell upgrade diagnostics: {result['status'].upper()}")
        for item in result["checks"]:
            marker = "OK" if item["status"] == "pass" else ("OPTIONAL" if not item["required"] else "FAIL")
            detail = item.get("detail", item.get("error", ""))
            print(f"[{marker:8}] {item['name']}: {detail}")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
