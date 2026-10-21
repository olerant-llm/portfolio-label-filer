import csv
from pathlib import Path

from tag_category import load_alias_map
from tag_normalize import normalize_tag

DICT_PATH = Path(__file__).resolve().parent.parent / "data" / "tag_dictionary.csv"


def load_seed_translations() -> dict[str, str]:
    """data/tag_dictionary.csv（english,japanese の2列）を読み込む。
    ユーザーが外部で編集・追記できるよう、毎回ファイルから読み直す。"""
    if not DICT_PATH.exists():
        return {}
    raw = DICT_PATH.read_bytes()
    text = None
    for encoding in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return {}

    translations: dict[str, str] = {}
    reader = csv.DictReader(text.splitlines())
    for row in reader:
        en = (row.get("english") or "").strip()
        ja = (row.get("japanese") or "").strip()
        if en and ja:
            translations[en] = ja
    return translations


def resolve_translation(name: str, seed: dict[str, str] | None = None) -> str | None:
    """完全一致で辞書に無いタグ（Florence-2の自由な言い回しなど）でも、
    正規化やDanbooruのalias経由で近いタグの訳が見つかればそれを返す。見つからなければNone。"""
    if seed is None:
        seed = load_seed_translations()
    if name in seed:
        return seed[name]
    norm = normalize_tag(name)
    if norm in seed:
        return seed[norm]
    alias_map = load_alias_map()
    canonical = alias_map.get(name) or alias_map.get(norm)
    if canonical and canonical in seed:
        return seed[canonical]
    return None
