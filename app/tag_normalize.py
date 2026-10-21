import re


def normalize_tag(text: str) -> str:
    """Florence-2が出す自由な言い回しを、Danbooruタグの命名規則（小文字・アンダースコア区切り）に寄せる。
    形態素解析や単数化は行わない（誤変換のリスクが高く、新規依存も必要なため）。"""
    t = text.strip().lower()
    t = re.sub(r"[^\w\s-]", "", t)
    t = re.sub(r"[\s-]+", "_", t)
    return t.strip("_")
