"""Reproducible allowlist-only Windows distribution; never include user databases."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import re
import shutil
import sys
import zipfile

HERE = Path(__file__).resolve().parent
WORK = HERE.parent
PACKAGE = WORK / "package" / "SleepWell"
RUNTIME_VERSION = "3.14.7"
RUNTIME_SHA256 = "d297e5ff019966817ad8502465176139f2d3d840fa4ed84b13bed399a6ab1f15"
PREPARED_MANIFEST = WORK / "prepared-manifest.json"
WEB_FILES = [
    "server.py", "engine.py", "daily_context.py", "prospective.py",
    "personalization.py", "night_core.py", "night_forecast.py",
    "forecast_model.py", "research.py", "mobile_store.py",
    "fusion_forecast.py", "robust_features.py", "habit_outcomes.py", "mobile_backup.py",
    "static/index.html", "static/app.js", "static/style.css", "static/mobile.js",
    "models/population_prior.json",
    "models/robust_cmi_v2.json", "models/robust_mesa_v2.json",
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def validate_prior(prior):
    top_fields = {"schema_version", "artifact_type", "version", "source", "source_sha256",
                  "supported_age", "groups", "global", "unsupported_fields",
                  "interpretation", "activity_definition", "minimum_group_people"}
    if not isinstance(prior, dict) or set(prior) != top_fields:
        raise ValueError("Population artifact has missing or unexpected top-level fields")
    if type(prior["schema_version"]) is not int or prior["schema_version"] != 1 or prior["artifact_type"] != "sleepwell_population_prior":
        raise ValueError("Unexpected population artifact")
    for field in ("version", "source", "interpretation", "activity_definition"):
        if not isinstance(prior[field], str) or not 1 <= len(prior[field]) <= 512:
            raise ValueError("Invalid population metadata")
    if not isinstance(prior["source_sha256"], str) or not re.fullmatch("[0-9a-f]{64}", prior["source_sha256"]):
        raise ValueError("Invalid source hash")
    if prior["supported_age"] != [18, 80] or any(type(age) is not int for age in prior["supported_age"]):
        raise ValueError("Unsupported age schema")
    if prior["unsupported_fields"] != ["occupation", "usual_bed"]:
        raise ValueError("Unexpected unsupported fields")
    if type(prior["minimum_group_people"]) is not int or not 1 <= prior["minimum_group_people"] <= 1000000:
        raise ValueError("Invalid minimum group size")

    def check_average(value, fields):
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("Unexpected aggregate fields")
        if type(value["n_people"]) is not int or not 1 <= value["n_people"] <= 100000000:
            raise ValueError("Invalid aggregate count")
        duration = value["mean_sleep_min"]
        if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 1440:
            raise ValueError("Invalid aggregate duration")

    check_average(prior["global"], {"n_people", "mean_sleep_min"})
    if not isinstance(prior["groups"], list) or not 1 <= len(prior["groups"]) <= 200:
        raise ValueError("Invalid groups")
    observed = set()
    for group in prior["groups"]:
        check_average(group, {"age_group", "sex", "activity_band", "n_people", "mean_sleep_min"})
        if group["age_group"] not in ("18-29", "30-44", "45-59", "60-80") or group["sex"] not in ("male", "female", "any") or group["activity_band"] not in ("low", "medium", "high", "any"):
            raise ValueError("Unexpected group label")
        identity = group["age_group"], group["sex"], group["activity_band"]
        if identity in observed or group["n_people"] > prior["global"]["n_people"]:
            raise ValueError("Duplicate or oversized aggregate group")
        observed.add(identity)


def assert_exact_paths(directory, expected):
    actual = set()
    for path in directory.rglob("*"):
        if path.is_symlink() or path.is_junction():
            raise ValueError("Links are forbidden in the distribution")
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
    if actual != expected:
        raise ValueError(f"Package allowlist mismatch: unexpected={sorted(actual - expected)}, missing={sorted(expected - actual)}")


def prepare(repository):
    archive = WORK / "downloads" / f"python-{RUNTIME_VERSION}-embed-amd64.zip"
    if digest(archive) != RUNTIME_SHA256:
        raise ValueError("Official Python runtime checksum mismatch")
    if PACKAGE.exists():
        raise ValueError("Package exists: use a new build workspace rather than overwrite")
    PACKAGE.mkdir(parents=True)
    runtime = PACKAGE / "runtime"
    runtime.mkdir()
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            if "/" in entry.filename or "\\" in entry.filename or entry.filename.startswith("."):
                raise ValueError("Unexpected embedded runtime archive entry")
        bundle.extractall(runtime)
    (runtime / "python314._pth").write_text(
        "python314.zip\n.\n../app\n../app/web-app\n../app/vendor\n", encoding="ascii")
    for relative in WEB_FILES:
        copy(repository / "web-app" / relative, PACKAGE / "app" / "web-app" / relative)
    prior = json.loads((PACKAGE / "app/web-app/models/population_prior.json").read_text(encoding="utf-8"))
    validate_prior(prior)
    sys.path.insert(0, str(PACKAGE / "app/web-app"))
    from fusion_forecast import FusionForecasts
    if not FusionForecasts(PACKAGE / "app/web-app/models").status()["available"]:
        raise ValueError("Both validated CMI/MESA research artifacts are required")
    # Keep the same form contract but do not offer unavailable private archive linkage.
    html_path = PACKAGE / "app/web-app/static/index.html"
    html = html_path.read_text(encoding="utf-8")
    old = '<label class="check"><input name="link_history" type="checkbox"> 이 디렉터리의 기존 본인 수면·활동 자료 연결</label>'
    if old not in html:
        raise ValueError("Desktop archive-link adaptation needs review")
    html = html.replace(old, '<label class="check" hidden><input name="link_history" type="checkbox" disabled> 연구 자료 연결 없음</label><p class="muted small">이 PC에서 입력한 기록으로 시작합니다. 배포판에는 다른 사람의 기록이 들어 있지 않습니다.</p>')
    html_path.write_text(html, encoding="utf-8")
    # This research bridge is never instantiated by desktop_host, but its
    # compatibility imports are retained. Remove the developer's personal folder
    # label from its source before including it in a distributable package.
    research_path = PACKAGE / "app/web-app/research.py"
    research_source = research_path.read_text(encoding="utf-8")
    personal_label = "\ud55c\uc11c\uc724\ub370\uc774\ud130_\uc804\ucc98\ub9ac"
    research_path.write_text(research_source.replace(personal_label, "personal_data"),
                             encoding="utf-8")
    copy(HERE / "desktop_host.py", PACKAGE / "app/desktop_host.py")
    copy(HERE / "mobile_sync.py", PACKAGE / "app/mobile_sync.py")
    copy(HERE / "bin/SyncCertificate.exe", PACKAGE / "app/SyncCertificate.exe")
    copy(HERE / "MOBILE_SYNC.md", PACKAGE / "docs/휴대폰 연결 안내.md")
    copy(HERE / "FUSION_RESEARCHER.md", PACKAGE / "docs/연구자 융합모델 안내.md")
    distribution = importlib.metadata.distribution("tzdata")
    for source in distribution.files:
        real = distribution.locate_file(source)
        if source.parts[0] == "tzdata" and "__pycache__" not in source.parts and source.suffix != ".pyc":
            copy(real, PACKAGE / "app/vendor" / source)
        elif "licenses" in source.parts and real.is_file():
            copy(real, PACKAGE / "licenses/tzdata" / Path(*source.parts[1:]))
    copy(WORK / "downloads/python-runtime.spdx.json", PACKAGE / "licenses/python-runtime.spdx.json")
    copy(repository / "NOTICE_SOURCES.md", PACKAGE / "licenses/SleepWell-SOURCES.md")
    (PACKAGE / "licenses/THIRD_PARTY.md").write_text(
        "# Included components\n\n"
        f"- CPython {RUNTIME_VERSION} Windows x64 embeddable, official python.org archive. "
        "See runtime/LICENSE.txt and python-runtime.spdx.json for Python and bundled-component notices.\n"
        f"- tzdata {distribution.version}, Python Software Foundation; Apache-2.0 package and IANA data. "
        "See tzdata/ licenses.\n"
        "- NHANES 2017–2018 training-split aggregate sleep-duration prior; no participant-level data.\n"
        "- Existing CMI/MESA data and fusion model parameters exported to JSON; participant records and personal Samsung model excluded.\n\n"
        "Official runtime: https://www.python.org/downloads/release/python-3147/\n"
        "Embedding: https://docs.python.org/3/using/windows.html#the-embeddable-package\n"
        "Python license: https://docs.python.org/3/license.html\n"
        "Time-zone data: https://tzdata.readthedocs.io/\n",
        encoding="utf-8")
    (PACKAGE / "build-info.json").write_text(json.dumps({
        "app_version": "0.3.0", "architecture": "Windows x64",
        "python_version": RUNTIME_VERSION, "python_archive_sha256": RUNTIME_SHA256,
        "tzdata_version": distribution.version,
        "source_files": [{"path": "web-app/" + relative,
                          "sha256": digest(repository / "web-app" / relative)} for relative in WEB_FILES],
        "personal_data_included": False,
        "research_models_included": True,
        "research_model_cohorts": ["cmi", "mesa"],
        "personal_model_included": False,
        "product_role": "researcher",
        "physical_model": "two_process_fixed_v1",
        "pcr_included": False,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    # PACKAGE was required to be absent, and every preceding write has an
    # explicit source. Freeze both names and bytes outside the distributable
    # directory, before tests or UI runs can create accidental export files.
    prepared = [{"path": path.relative_to(PACKAGE).as_posix(), "sha256": digest(path)}
                for path in sorted(PACKAGE.rglob("*")) if path.is_file()]
    PREPARED_MANIFEST.write_text(json.dumps(prepared, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Prepared isolated runtime and public app files: {PACKAGE}")


def seal():
    prepared = json.loads(PREPARED_MANIFEST.read_text(encoding="utf-8"))
    for row in prepared:
        if digest(PACKAGE / row["path"]) != row["sha256"]:
            raise ValueError(f"Prepared source changed; rebuild and revalidate: {row['path']}")
    validate_prior(json.loads((PACKAGE / "app/web-app/models/population_prior.json").read_text(encoding="utf-8")))
    copy(HERE / "bin/SleepWell.exe", PACKAGE / "SleepWell.exe")
    copy(HERE / "USER_GUIDE.md", PACKAGE / "사용 안내.md")
    copy(HERE / "PROSPECTIVE_APP_2026-10-20.md", PACKAGE / "docs/사전검증 안내.md")
    manifest_path = PACKAGE / "package-manifest.json"
    allowed = {row["path"] for row in prepared} | {"SleepWell.exe", "사용 안내.md", "docs/사전검증 안내.md"}
    assert_exact_paths(PACKAGE, allowed | ({"package-manifest.json"} if manifest_path.exists() else set()))
    rows = [{"path": relative, "sha256": digest(PACKAGE / relative)} for relative in sorted(allowed)]
    manifest_path.write_text(json.dumps({"version": "0.3.0", "files": rows},
                                       ensure_ascii=False, indent=2), encoding="utf-8")
    payload = WORK / "SleepWellPayload.zip"
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for relative in sorted(allowed | {"package-manifest.json"}):
            bundle.write(PACKAGE / relative, relative)
    print(json.dumps({"files": len(rows), "payload_bytes": payload.stat().st_size,
                      "sha256": digest(payload)}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("prepare", "seal"))
    parser.add_argument("--repository", type=Path)
    args = parser.parse_args()
    if args.operation == "prepare":
        if not args.repository:
            parser.error("--repository is required for prepare")
        prepare(args.repository.resolve())
    else:
        seal()
