from pathlib import Path

import numpy as np
from PIL import Image

from tag_category import load_tag_categories

REPO_ID = "openai/clip-vit-base-patch32"
TOP_N_VOCAB = 8000  # category0(一般)タグのうち、よく使われる順の上位件数
TOP_K_TAGS = 20  # 画像ごとに返すタグの件数

_model = None
_processor = None
_vocab: list[str] | None = None
_vocab_embeds: np.ndarray | None = None


def _load():
    global _model, _processor
    if _model is not None:
        return
    from transformers import CLIPModel, CLIPProcessor

    _model = CLIPModel.from_pretrained(REPO_ID)
    _processor = CLIPProcessor.from_pretrained(REPO_ID)
    _model.eval()


def _build_vocabulary() -> list[str]:
    cats = load_tag_categories()  # {name: (category, post_count)}
    general = [(name, post_count) for name, (category, post_count) in cats.items() if category == 0]
    general.sort(key=lambda x: -x[1])
    return [name for name, _ in general[:TOP_N_VOCAB]]


def _tag_to_text(tag: str) -> str:
    return tag.replace("_", " ")


def _ensure_vocab_embeddings():
    import torch

    global _vocab, _vocab_embeds
    if _vocab_embeds is not None:
        return
    _load()
    _vocab = _build_vocabulary()
    texts = [_tag_to_text(t) for t in _vocab]
    batch_size = 256
    all_embeds = []
    with torch.no_grad():
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            inputs = _processor(text=batch, return_tensors="pt", padding=True, truncation=True)
            feats = _model.get_text_features(**inputs)
            feats = feats / feats.norm(dim=-1, keepdim=True)
            all_embeds.append(feats.numpy())
    _vocab_embeds = np.concatenate(all_embeds, axis=0)


def tag_image(path: Path) -> list[tuple[str, float]]:
    import torch

    _ensure_vocab_embeddings()
    with Image.open(path) as im:
        image = im.convert("RGB")
        inputs = _processor(images=image, return_tensors="pt")
        with torch.no_grad():
            feats = _model.get_image_features(**inputs)
        feats = feats / feats.norm(dim=-1, keepdim=True)
        image_embed = feats.numpy()[0]

    sims = _vocab_embeds @ image_embed
    order = np.argsort(-sims)[:TOP_K_TAGS]
    return [(_vocab[i], float(sims[i])) for i in order]
