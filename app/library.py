import json
from pathlib import Path

from db import TOOL_DIR

SETTINGS_PATH = TOOL_DIR / "data" / "app_settings.json"
MAX_RECENT = 8


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {"active_root": None, "recent_roots": []}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"active_root": None, "recent_roots": []}
    data.setdefault("active_root", None)
    data.setdefault("recent_roots", [])
    return data


def save_settings(data: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def remember_root(root: str) -> dict:
    settings = load_settings()
    recent = [p for p in settings["recent_roots"] if p != root]
    recent.insert(0, root)
    settings["recent_roots"] = recent[:MAX_RECENT]
    settings["active_root"] = root
    save_settings(settings)
    return settings


def forget_root(root: str) -> dict:
    """Remove a folder from recent history without touching its files or database."""
    settings = load_settings()
    settings["recent_roots"] = [p for p in settings["recent_roots"] if p != root]
    if settings.get("active_root") == root:
        settings["active_root"] = None
    save_settings(settings)
    return settings
