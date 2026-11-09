"""Create the Windows source distribution without virtual environments or caches."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.2.0"
ARCHIVE_ROOT = f"SleepWellAcademicWorkbench-{VERSION}"
OUTPUT = ROOT / "release" / f"SleepWellAcademicWorkbench-{VERSION}-Windows.zip"
EXCLUDED_DIRECTORIES = {
    ".git",
    ".omx",
    ".venv",
    "__pycache__",
    "release",
    "example_output",
    "example_output_verified",
    "logs",
    "projects",
    "example_output_academic",
    ".pytest_cache",
    ".mypy_cache",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp", ".lnk"}


def included_files() -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDED_DIRECTORIES for part in relative.parts):
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        files.append(path)
    return sorted(files, key=lambda path: path.relative_to(ROOT).as_posix())


def build() -> Path:
    required = [
        ROOT / "README.md",
        ROOT / "requirements.txt",
        ROOT / "install.cmd",
        ROOT / "launch.cmd",
        ROOT / "researchbench" / "academic_app.py",
        ROOT / "samples" / "sleepwell_50days.csv",
        ROOT / "samples" / "classification_mixed.csv",
        ROOT / "deliverables" / "examples" / "manifest.json",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"release_required_files_missing:{missing}")
    files = included_files()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    manifest = []
    with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(ROOT).as_posix()
            content = path.read_bytes()
            info = zipfile.ZipInfo(f"{ARCHIVE_ROOT}/{relative}", date_time=(2026, 11, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content, compresslevel=9)
            manifest.append(
                {"path": relative, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            )
        manifest_bytes = json.dumps(
            {"name": "SleepWell Academic Workbench", "version": VERSION, "files": manifest},
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        info = zipfile.ZipInfo(f"{ARCHIVE_ROOT}/PACKAGE_MANIFEST.json", date_time=(2026, 11, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, manifest_bytes, compresslevel=9)
    digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    OUTPUT.with_suffix(OUTPUT.suffix + ".sha256").write_text(
        f"{digest}  {OUTPUT.name}\n", encoding="ascii"
    )
    print(f"Built {OUTPUT} with {len(files)} files; sha256={digest}")
    return OUTPUT


if __name__ == "__main__":
    build()
