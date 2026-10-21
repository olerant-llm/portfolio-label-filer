import csv
from pathlib import Path

from tag_normalize import normalize_tag

CATEGORY_PATH = Path(__file__).resolve().parent.parent / "data" / "danbooru_20250919.csv"

_cache: dict[str, tuple[int, int]] | None = None
_alias_cache: dict[str, str] | None = None


def load_tag_categories() -> dict[str, tuple[int, int]]:
    """data/danbooru_20250919.csv（タグ名,category,post_count,alias）を読み込む。
    ユーザーが編集する想定の無い静的な参考データのため、初回読み込み後はキャッシュする。"""
    global _cache, _alias_cache
    if _cache is not None:
        return _cache
    result: dict[str, tuple[int, int]] = {}
    aliases: dict[str, str] = {}
    if CATEGORY_PATH.exists():
        with open(CATEGORY_PATH, encoding="utf-8", newline="") as f:
            for row in csv.reader(f):
                if len(row) < 3:
                    continue
                name = row[0].strip()
                try:
                    category = int(row[1])
                    post_count = int(float(row[2]))
                except ValueError:
                    continue
                if name:
                    result[name] = (category, post_count)
                if name and len(row) >= 4 and row[3]:
                    for alias in row[3].split(","):
                        alias = alias.strip()
                        if alias:
                            aliases[alias] = name
    _cache = result
    _alias_cache = aliases
    return result


def load_alias_map() -> dict[str, str]:
    """別名（alias）→正式タグ名の対応表。load_tag_categories()と同じCSVの4列目から作る。"""
    load_tag_categories()
    return _alias_cache or {}


def _resolve(name: str) -> tuple[int, int] | None:
    cats = load_tag_categories()
    if name in cats:
        return cats[name]
    norm = normalize_tag(name)
    if norm in cats:
        return cats[norm]
    aliases = load_alias_map()
    canonical = aliases.get(name) or aliases.get(norm)
    if canonical and canonical in cats:
        return cats[canonical]
    return None


def get_category(name: str) -> int | None:
    entry = _resolve(name)
    return entry[0] if entry else None


def get_post_count(name: str) -> int:
    entry = _resolve(name)
    return entry[1] if entry else 0
