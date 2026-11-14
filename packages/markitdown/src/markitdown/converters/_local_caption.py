"""
Offline image captioning via a dedicated model (BLIP).

Used by :class:`ImageConverter` when the caller requests a description
(CLI: ``--des``) and no cloud ``llm_client`` is configured. The heavy
dependencies (``torch`` / ``transformers``) are imported lazily and the model
is cached per-process so repeated conversions reuse it.

Install the required dependencies with the ``image-caption`` extra::

    pip install "markitdown[image-caption]"

Override the model with ``MARKITDOWN_CAPTION_MODEL`` (a HuggingFace id or local
path) and disable the backend with ``MARKITDOWN_DISABLE_LOCAL_CAPTION=1``.
"""

from __future__ import annotations

import importlib.util
import os
import threading
from typing import Any, BinaryIO, Dict, Optional, Tuple

from .._stream_info import StreamInfo

DEFAULT_CAPTION_MODEL = "Salesforce/blip-image-captioning-base"

# One loaded (model, processor, device) tuple per model id, guarded so that
# concurrent conversions do not load the weights more than once.
_load_lock = threading.Lock()
_loaded_models: Dict[str, Tuple[Any, Any, str]] = {}


# Report whether the optional ML dependencies are importable without importing
# them, so the backend can be silently skipped when the extra is absent.
def is_available() -> bool:
    if os.getenv("MARKITDOWN_DISABLE_LOCAL_CAPTION", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    return (
        importlib.util.find_spec("torch") is not None
        and importlib.util.find_spec("transformers") is not None
    )


# Resolve the model id from an explicit argument, then the environment, then the
# packaged default.
def _resolve_model_id(explicit: Optional[str]) -> str:
    return explicit or os.getenv("MARKITDOWN_CAPTION_MODEL") or DEFAULT_CAPTION_MODEL


# Load and cache the BLIP captioning model + processor. Heavy imports live here
# so they only run when a caption is actually requested.
def _get_model(model_id: str) -> Tuple[Any, Any, str]:
    cached = _loaded_models.get(model_id)
    if cached is not None:
        return cached

    with _load_lock:
        cached = _loaded_models.get(model_id)
        if cached is not None:
            return cached

        import torch
        from transformers import BlipForConditionalGeneration, BlipProcessor

        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        processor = BlipProcessor.from_pretrained(model_id)
        # Prefer safetensors so only a single weight file is fetched (keeps the
        # download small); fall back for models that ship only .bin weights.
        try:
            model = BlipForConditionalGeneration.from_pretrained(
                model_id, use_safetensors=True
            )
        except Exception:
            model = BlipForConditionalGeneration.from_pretrained(model_id)
        model = model.to(device).eval()

        loaded = (model, processor, device)
        _loaded_models[model_id] = loaded
        return loaded


# Produce an offline natural-language caption for an image. Returns None when no
# caption is produced. Callers handle missing dependencies via is_available().
def describe_image_offline(
    file_stream: BinaryIO,
    stream_info: Optional[StreamInfo] = None,
    *,
    model_id: Optional[str] = None,
) -> Optional[str]:
    import torch
    from PIL import Image

    model, processor, device = _get_model(_resolve_model_id(model_id))

    cur_pos = file_stream.tell()
    try:
        image = Image.open(file_stream).convert("RGB")
    finally:
        file_stream.seek(cur_pos)

    inputs = processor(image, return_tensors="pt").to(device)
    with torch.no_grad():
        generated_ids = model.generate(**inputs, max_new_tokens=50, num_beams=3)

    caption = processor.decode(generated_ids[0], skip_special_tokens=True).strip()
    return caption or None
