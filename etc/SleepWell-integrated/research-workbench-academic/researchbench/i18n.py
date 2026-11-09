import json
import os
from pathlib import Path

CATALOG = json.loads((Path(__file__).parent / "locales" / "ui.json").read_text(encoding="utf-8"))


def tr(key, lang="ko", **kwargs):
    value = CATALOG.get(key, [key, key])[1 if lang == "en" else 0]
    return value.format(**kwargs) if kwargs else value


def preferences_path():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SleepWellAcademicWorkbench" / "preferences.json"


def read_language():
    try:
        return json.loads(preferences_path().read_text(encoding="utf-8"))["language"]
    except (OSError, ValueError, KeyError):
        return None


def save_language(language):
    path = preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"language": language}), encoding="utf-8")
    os.replace(temporary, path)
