from pathlib import Path

from PIL import Image
import model_manager

TASK = "<GENERATE_TAGS>"

_model = None
_processor = None


def _load():
    global _model, _processor
    if _model is not None:
        return
    from transformers import AutoModelForCausalLM, AutoProcessor

    model_path = str(model_manager.ensure_model())
    load_options = {"trust_remote_code": True, "local_files_only": True}
    _model = AutoModelForCausalLM.from_pretrained(model_path, **load_options)
    _processor = AutoProcessor.from_pretrained(model_path, **load_options)
    _model.eval()


def tag_image(path: Path) -> list[tuple[str, float]]:
    import torch

    _load()
    with Image.open(path) as im:
        image = im.convert("RGB")
        inputs = _processor(text=TASK, images=image, return_tensors="pt")
        with torch.no_grad():
            generated_ids = _model.generate(
                input_ids=inputs["input_ids"],
                pixel_values=inputs["pixel_values"],
                max_new_tokens=512,
                do_sample=False,
                num_beams=3,
            )
        generated_text = _processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
        parsed = _processor.post_process_generation(generated_text, task=TASK, image_size=(image.width, image.height))

    raw = parsed.get(TASK, "")
    tags = []
    for t in raw.split(","):
        t = t.strip().lower()
        if not t or len(t) > 40:
            continue
        if any(c in t for c in "<>\\/{}[]"):
            continue
        tags.append(t)
    return [(t, 1.0) for t in tags]
