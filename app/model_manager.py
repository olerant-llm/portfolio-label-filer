from pathlib import Path


REPO_ID = "MiaoshouAI/Florence-2-base-PromptGen-v2.0"
REVISION = "59b6e4bf75d0f3e8a6b1a14211f6a50fcdd48d63"


def ensure_model() -> Path:
    """Download the pinned image model once and return its local cache path."""
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    try:
        cached = snapshot_download(
            repo_id=REPO_ID,
            revision=REVISION,
            local_files_only=True,
        )
    except LocalEntryNotFoundError:
        cached = snapshot_download(repo_id=REPO_ID, revision=REVISION)
    return Path(cached)
