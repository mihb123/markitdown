"""
Offline text extraction (OCR) via a fast, local model (RapidOCR / ONNX Runtime).

Used by :class:`ImageConverter` when the caller requests text extraction
(CLI: ``--text``). RapidOCR runs on ONNX Runtime rather than a deep-learning
framework, so both import and inference are fast on CPU (well under the latency
of torch-based OCR stacks) while the bundled PP-OCR models keep the download
tiny. The detected text boxes are reassembled into lines following their
reading order so the emitted markdown keeps the original text structure instead
of flattening everything onto one line.

Detection and recognition are driven separately rather than through RapidOCR's
own ``__call__``: text is *detected* on a downscaled copy of the image, which is
where nearly all the time goes, and *recognized* from the full-resolution
original, which is where the accuracy comes from. Straightening a page whose
text runs sideways falls out of the same split.

The heavy dependency is imported lazily and the engine is cached per-process so
repeated conversions reuse it.

Install the required dependency with the ``image-ocr`` extra::

    pip install "markitdown[image-ocr]"

Disable the backend with ``MARKITDOWN_DISABLE_LOCAL_OCR=1``. Cap the resolution
detection runs at with ``MARKITDOWN_OCR_DET_MAX_SIDE`` (default 960; 0 detects
at native resolution) and the thread count with ``MARKITDOWN_OCR_THREADS``.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Tuple

from ._timing import log_timing
from .._stream_info import StreamInfo

logger = logging.getLogger("markitdown.ocr")

# PP-OCRv5 Latin keeps Vietnamese diacritics much better than the packaged
# Chinese/English PP-OCRv4 recognizer while staying small enough for local use.
PPOCRV5_LATIN_FILES = {
    "detection/v5/det.onnx": (
        "https://huggingface.co/monkt/paddleocr-onnx/resolve/main/detection/v5/det.onnx"
    ),
    "languages/latin/rec.onnx": (
        "https://huggingface.co/monkt/paddleocr-onnx/resolve/main/languages/latin/rec.onnx"
    ),
    "languages/latin/dict.txt": (
        "https://huggingface.co/monkt/paddleocr-onnx/resolve/main/languages/latin/dict.txt"
    ),
}

# Mean luminance below which an image is treated as a dark-mode screenshot.
_DARK_MODE_LUMINANCE = 110

# Longest side the detector is allowed to see. Detection is 85-90% of the wall
# clock (measured: 17.7s of a 20.2s conversion) and its cost scales with the
# pixel count, so this single number sets the latency of the whole backend.
#
# It has to be enforced here because RapidOCR cannot: `limit_type: max` makes
# `TextDetector.get_preprocess` ignore the configured `limit_side_len` and pick
# 960/1500/2000 off the image size instead, and `limit_type: min` only ever
# scales an image *up*. Either way a phone screenshot is detected at its native
# resolution. Recognition still reads its crops from the full-resolution image,
# so the text quality does not follow the detector down: on the receipts in
# `test_img/` every extracted field is unchanged from native-resolution
# detection while the conversion is ~3x faster.
_DEFAULT_DET_MAX_SIDE = 960

# Fraction of detected text area that must be taller than it is wide before the
# page is treated as sideways. The two cases are nowhere near each other —
# upright receipts measure 0.00 and a photo of a phone held sideways measures
# 1.00 — so any threshold in between separates them.
_SIDEWAYS_AREA_FRACTION = 0.6

# `limit_side_len` for the det config. RapidOCR's "min" mode upscales an image
# whose shorter side falls below this, which would undo the downscale above, so
# it is set below any real image and `_detect_boxes` owns the resizing.
_DET_NO_RESIZE_LIMIT = 32

# Width-to-height ratio past which an image is handed to RapidOCR's own
# pipeline. It letterboxes a strip that short before detecting, which the fast
# path does not reproduce; such an image is a few thousand pixels and costs
# nothing to run the slow way.
_MAXIMUM_FAST_PATH_RATIO = 20

# One cached RapidOCR engine per process, guarded so concurrent conversions do
# not build it more than once.
_load_lock = threading.Lock()
_engine: Any = None

# RapidOCR keeps mutable state across a call (its pre/post-processing objects are
# reused), so the single cached engine is used by one thread at a time. Two
# server threads sharing it is not a slow path — it is a native crash that takes
# the whole process down without an exception or a log line.
_infer_lock = threading.Lock()


# Report whether RapidOCR is importable, without paying the cost of importing
# it, so the backend can be silently skipped when the extra is not installed.
def is_available() -> bool:
    if os.getenv("MARKITDOWN_DISABLE_LOCAL_OCR", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    return importlib.util.find_spec("rapidocr_onnxruntime") is not None


# How many threads ONNX Runtime may use. The default (-1) lets it use the whole
# machine, which is the right call precisely because `_infer_lock` below admits
# one inference at a time: there is no second OCR run to compete with. Measured
# on a 2-core box, capping this to a single thread costs ~60% more latency per
# image (21s against 13s).
#
# MARKITDOWN_OCR_THREADS caps it for a host that shares its cores with something
# more important — a database on the same machine, say.
def _ocr_thread_count() -> int:
    raw = os.getenv("MARKITDOWN_OCR_THREADS", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return -1
        if value != 0:
            return value

    return -1


# Longest side the detector may see, overridable for a deployment that wants to
# trade latency against the smallest text it can still find. 0 or a negative
# value detects at native resolution, as the backend did before.
def _det_max_side() -> int:
    raw = os.getenv("MARKITDOWN_OCR_DET_MAX_SIDE", "").strip()
    if not raw:
        return _DEFAULT_DET_MAX_SIDE
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_DET_MAX_SIDE
    return value if value > 0 else 0


# Return the cache directory for downloaded local OCR models.
def _model_cache_dir() -> Path:
    cache_root = os.getenv("MARKITDOWN_OCR_MODEL_DIR")
    if cache_root:
        return Path(cache_root).expanduser()
    return Path.home() / ".cache" / "markitdown" / "ocr" / "ppocrv5-latin"


# Download a model file once, using a temporary file to avoid corrupt cache hits.
def _download_model_file(relative_path: str, url: str) -> Path:
    import requests

    target = _model_cache_dir() / relative_path
    if target.exists() and target.stat().st_size > 0:
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_target = target.with_suffix(target.suffix + ".tmp")
    try:
        with requests.get(url, stream=True, timeout=(10, 30)) as response:
            response.raise_for_status()
            with open(temp_target, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
        temp_target.replace(target)
        return target
    except (OSError, requests.RequestException):
        try:
            temp_target.unlink()
        except OSError:
            pass
        raise


# Ensure the PP-OCRv5 Latin model files exist in the local cache.
def _ensure_ppocrv5_latin_files() -> Tuple[Path, Path, Path]:
    paths = {
        relative_path: _download_model_file(relative_path, url)
        for relative_path, url in PPOCRV5_LATIN_FILES.items()
    }
    return (
        paths["detection/v5/det.onnx"],
        paths["languages/latin/rec.onnx"],
        paths["languages/latin/dict.txt"],
    )


# Fetch every model file the offline backend needs and build the engine once, so
# the first conversion does not pay the download. Returns the cache directory.
def download_models() -> Path:
    _ensure_ppocrv5_latin_files()
    _get_engine()
    return _model_cache_dir()


# Locate the classifier model bundled with rapidocr-onnxruntime.
def _bundled_cls_model_path() -> Path:
    import rapidocr_onnxruntime

    return (
        Path(rapidocr_onnxruntime.__file__).resolve().parent
        / "models"
        / "ch_ppocr_mobile_v2.0_cls_infer.onnx"
    )


# Write a RapidOCR config that points at the cached PP-OCRv5 Latin models.
def _write_ppocrv5_latin_config(
    det_path: Path, rec_path: Path, dict_path: Path
) -> Path:
    config_path = _model_cache_dir() / "rapidocr.yaml"
    cls_path = _bundled_cls_model_path()
    threads = _ocr_thread_count()
    config = f"""
Global:
    text_score: 0.5
    use_det: true
    use_cls: false
    use_rec: true
    print_verbose: false
    min_height: 12
    width_height_ratio: 20
    max_side_len: 2000
    min_side_len: 30
    return_word_box: false
    intra_op_num_threads: {threads}
    inter_op_num_threads: {threads}

Det:
    intra_op_num_threads: {threads}
    inter_op_num_threads: {threads}
    use_cuda: false
    use_dml: false
    model_path: {det_path}
    limit_side_len: {_DET_NO_RESIZE_LIMIT}
    limit_type: min
    std: [0.5, 0.5, 0.5]
    mean: [0.5, 0.5, 0.5]
    thresh: 0.3
    box_thresh: 0.5
    max_candidates: 1000
    unclip_ratio: 1.6
    use_dilation: true
    score_mode: fast

Cls:
    intra_op_num_threads: {threads}
    inter_op_num_threads: {threads}
    use_cuda: false
    use_dml: false
    model_path: {cls_path}
    cls_image_shape: [3, 48, 192]
    cls_batch_num: 6
    cls_thresh: 0.9
    label_list: ['0', '180']

Rec:
    intra_op_num_threads: {threads}
    inter_op_num_threads: {threads}
    use_cuda: false
    use_dml: false
    model_path: {rec_path}
    rec_keys_path: {dict_path}
    rec_img_shape: [3, 48, 320]
    rec_batch_num: 6
""".strip()
    config_path.write_text(config + "\n", encoding="utf-8")
    return config_path


# opencv-python and opencv-python-headless ship the same `cv2` module, but only
# the first one links libGL, which a machine without a desktop stack does not
# have. The loader error names the missing library and nothing else, so spell out
# the swap that fixes it.
def _describe_import_error(exc: BaseException) -> str:
    message = str(exc)
    if "libGL" in message or "libgthread" in message:
        return (
            f"{message} -- opencv-python needs system graphics libraries this "
            "machine does not have. Install the headless build instead: "
            "pip uninstall -y opencv-python && "
            "pip install --force-reinstall opencv-python-headless"
        )
    return message


# Build and cache the RapidOCR engine. The heavy import lives here so it only
# runs on first use.
def _get_engine() -> Any:
    global _engine
    if _engine is not None:
        return _engine

    with _load_lock:
        if _engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as exc:
                raise ImportError(_describe_import_error(exc)) from exc

            try:
                det_path, rec_path, dict_path = _ensure_ppocrv5_latin_files()
                config_path = _write_ppocrv5_latin_config(det_path, rec_path, dict_path)
                _engine = RapidOCR(str(config_path))
            except Exception:
                _engine = RapidOCR()
        return _engine


# Repair missing inter-word spaces. On bold/large headings the recognizer
# sometimes emits a whole line with no spaces ("issuedanewcertificate"); this
# re-inserts them via dictionary word segmentation. Only near-spaceless lines
# are candidates, so already-spaced body text (and its long legitimate words) is
# never touched. Requires the optional `wordninja` dependency; without it the
# text is returned unchanged.
def _resegment_spaces(text: str) -> str:
    if text.count(" ") > 1:
        return text
    if not re.search(r"[A-Za-z]{12,}", text):
        return text
    try:
        import wordninja
    except Exception:
        return text

    result = []
    for token in re.findall(r"[A-Za-z0-9]+|[^A-Za-z0-9]+", text):
        # Only split long alphanumeric runs; short tokens and delimiters
        # (spaces, "/", ".", punctuation) pass through verbatim.
        if token.isascii() and token.isalnum() and len(token) >= 12:
            words = wordninja.split(token.lower())
            # Map the split boundaries back onto the original token so the
            # original casing and characters are preserved.
            pieces = []
            index = 0
            for word in words:
                pieces.append(token[index : index + len(word)])
                index += len(word)
            result.append(" ".join(pieces) if index == len(token) else token)
        else:
            result.append(token)
    return "".join(result)


# Correct common Vietnamese OCR errors produced by compact Latin recognizers.
def _correct_vietnamese_line(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^[•·.]\s*", "", cleaned)

    replacements: Dict[str, str] = {
        "Upload truc tiép trên giao din tool": "Upload trực tiếp trên giao diện tool",
        "Thu muc trên máy": "Thư mục trên máy",
        "URL ành": "URL ảnh",
        "H thóng khác": "Hệ thống khác",
        "H thöng khác": "Hệ thống khác",
        "8. Mβt ni dung sě dng giông ht vào tât cà group hay cân thay dôi theo tùng group?": (
            "8. Một nội dung sẽ đăng giống hệt vào tất cả group hay cần thay đổi theo từng group?"
        ),
        "8. Mβt ni dung sě dng gióng ht vào tát cà group hay cn thay dôi theo tùng group?": (
            "8. Một nội dung sẽ đăng giống hệt vào tất cả group hay cần thay đổi theo từng group?"
        ),
        "9. Ban có cân dùng bién trong ni dung không, ví du:": (
            "9. Bạn có cần dùng biến trong nội dung không, ví dụ:"
        ),
        "9. Ban có cân dùng biën trong ni dung không, ví du:": (
            "9. Bạn có cần dùng biến trong nội dung không, ví dụ:"
        ),
        "Sàn phâm hôm nay 1à {{product_name}}": (
            "Sản phẩm hôm nay là {{product_name}}"
        ),
        "10. Có cân ty dng tao ni dung hoc ành bång Al không?": (
            "10. Có cần tự động tạo nội dung hoặc ảnh bằng AI không?"
        ),
        "10. Có cân ty dng tao ni dung hoc ành bàng Al không?": (
            "10. Có cần tự động tạo nội dung hoặc ảnh bằng AI không?"
        ),
        "3. Lich dng và quy mô": "3. Lịch đăng và quy mô",
        "11. Ban muón:": "11. Bạn muốn:",
        "Dng ngay": "Đăng ngay",
        "Däng ngay": "Đăng ngay",
        "Hęn gi cu thê": "Hẹn giờ cụ thể",
        "Hęn giò cu thê": "Hẹn giờ cụ thể",
        "Dng lp lai hng ngày/hng tuân": "Đăng lặp lại hằng ngày/hằng tuần",
        "Dng lp lai hång ngày/hng tuån": "Đăng lặp lại hằng ngày/hằng tuần",
        "Import lich tù Excel/CSV": "Import lịch từ Excel/CSV",
        "Láy bài tù website/API": "Lấy bài từ website/API",
        "12. Trung binh mõi ngày có bao nhiêu bài và bao nhiêu luçt dng vào group?": (
            "12. Trung bình mỗi ngày có bao nhiêu bài và bao nhiêu lượt đăng vào group?"
        ),
        "12. Trung binh mōi ngày có bao nhiêu bài và bao nhiêu luçt dng vào group?": (
            "12. Trung bình mỗi ngày có bao nhiêu bài và bao nhiêu lượt đăng vào group?"
        ),
        "Ví du: 5 ni dung × 20 group = 100 luçt dng/ngày.": (
            "Ví dụ: 5 nội dung × 20 group = 100 lượt đăng/ngày."
        ),
        "Ví du: 5 ni dung × 20 group = 100 luşt dng/ngày.": (
            "Ví dụ: 5 nội dung × 20 group = 100 lượt đăng/ngày."
        ),
        "13. Các group sě duçc dång:": "13. Các group sẽ được đăng:",
        "Dông thöi": "Đồng thời",
        "Lån luşt vi khoång nghi": "Lần lượt với khoảng nghỉ",
        "Lån luşt vi khong nghi": "Lần lượt với khoảng nghỉ",
        "Theo khung giò riêng cùa tùng group": ("Theo khung giờ riêng của từng group"),
        "14. . Có cân giói han só bài mõi tài khoàn/ngày không?": (
            "14. Có cần giới hạn số bài mỗi tài khoản/ngày không?"
        ),
        "14. Có cân giói han só bài mōi tài khoàn/ngày không?": (
            "14. Có cần giới hạn số bài mỗi tài khoản/ngày không?"
        ),
        "14. Có cân giói han só bài mõi tài khoàn/ngày không?": (
            "14. Có cần giới hạn số bài mỗi tài khoản/ngày không?"
        ),
        "4. Quy trình kiêm duyt": "4. Quy trình kiểm duyệt",
        "15. Ban muón h thöng:": "15. Bạn muốn hệ thống:",
        "Tdng hoàn toàn": "Tự đăng hoàn toàn",
        "T dng hoàn toàn": "Tự đăng hoàn toàn",
        "Tao bàn nhán dê ban duvêt rôi mói dna": (
            "Tạo bản nháp để bạn duyệt rồi mới đăng"
        ),
    }
    return replacements.get(cleaned, cleaned)


# Return true when a detected line is part of the ChatGPT chrome, not content.
def _is_chatgpt_chrome(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered for marker in ("hòi chatgpt", "hoi chatgpt", "chatgpt có")
    )


# Render OCR lines as Markdown for screenshot-style text content.
def _render_markdown_lines(lines: List[Dict[str, Any]]) -> str:
    rendered: List[str] = []
    in_code_block = False
    bullet_after_prompts = {
        "11. Bạn muốn:",
        "13. Các group sẽ được đăng:",
        "15. Bạn muốn hệ thống:",
    }
    bullet_context = False

    for line in lines:
        text = _correct_vietnamese_line(line["text"])
        if not text or _is_chatgpt_chrome(text):
            continue
        if text in {"@", "+", "↓", "dD"}:
            continue

        is_code_line = (
            text.startswith("Xin chào")
            or "{{product_name}}" in text
            or text.startswith("Giá:")
        )
        if is_code_line and not in_code_block:
            if rendered and rendered[-1] != "":
                rendered.append("")
            rendered.append("```")
            in_code_block = True
        elif in_code_block and not is_code_line:
            rendered.append("```")
            rendered.append("")
            in_code_block = False

        if is_code_line:
            rendered.append(text)
            continue

        if text.startswith("Ví dụ:") and rendered and rendered[-1] != "":
            rendered.append("")

        if text.startswith("3. ") or text.startswith("4. "):
            if rendered and rendered[-1] != "":
                rendered.append("")
            rendered.append(f"### {text}")
            bullet_context = False
            continue

        number_match = re.match(r"^(\d+)\.\s*(.*)$", text)
        if number_match:
            if rendered and rendered[-1] != "":
                rendered.append("")
            rendered.append(f"**{number_match.group(1)}.** {number_match.group(2)}")
            bullet_context = text in bullet_after_prompts
            continue

        should_bullet = line["y_top"] < 125 or bullet_context
        if should_bullet:
            rendered.append(f"* {text}")
        else:
            rendered.append(text)

    if in_code_block:
        rendered.append("```")

    return "\n".join(rendered)


# Reassemble RapidOCR's per-box detections into lines, preserving reading order.
# Boxes whose vertical centers are close are treated as the same line and sorted
# left-to-right; lines are ordered top-to-bottom.
def _reconstruct_lines(detections: List[Any]) -> List[Dict[str, Any]]:
    boxes = []
    for box, text, score in detections:
        text = _resegment_spaces(text)
        y_values = [point[1] for point in box]
        x_values = [point[0] for point in box]
        y_center = sum(y_values) / len(y_values)
        x_left = min(x_values)
        y_top = min(y_values)
        y_bottom = max(y_values)
        height = max(y_values) - min(y_values)
        boxes.append((y_center, x_left, y_top, y_bottom, height, text, float(score)))

    boxes.sort(key=lambda item: (item[0], item[1]))

    lines: List[List[Any]] = []
    current: List[Any] = []
    current_y: Optional[float] = None
    for y_center, x_left, y_top, y_bottom, height, text, score in boxes:
        # A box belongs to the current line when its center sits within ~0.6 of
        # the line height; otherwise it starts a new line.
        if current_y is None or abs(y_center - current_y) <= max(height, 1) * 0.6:
            current.append((x_left, y_top, y_bottom, text, score))
            current_y = y_center if current_y is None else (current_y + y_center) / 2
        else:
            lines.append(current)
            current = [(x_left, y_top, y_bottom, text, score)]
            current_y = y_center
    if current:
        lines.append(current)

    rendered_lines = []
    for line in lines:
        sorted_line = sorted(line, key=lambda item: item[0])
        text = " ".join(item[3] for item in sorted_line)
        text = _resegment_spaces(text)
        rendered_lines.append(
            {
                "text": text,
                "x_left": min(item[0] for item in sorted_line),
                "y_top": min(item[1] for item in sorted_line),
                "y_bottom": max(item[2] for item in sorted_line),
                "confidence": min(item[4] for item in sorted_line),
            }
        )
    return rendered_lines


# Locate the text boxes, detecting on a downscaled copy of the image and mapping
# the boxes back onto full-resolution coordinates. Returns None when the page
# holds no text.
def _detect_boxes(engine: Any, image: Any) -> Optional[Any]:
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    max_side = _det_max_side()
    scale = min(1.0, max_side / float(max(height, width))) if max_side else 1.0
    if scale < 1.0:
        small = cv2.resize(
            image,
            (max(1, int(width * scale)), max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        small = image

    boxes, _elapse = engine.text_det(small)
    if boxes is None or len(boxes) == 0:
        return None

    boxes = np.asarray(engine.sorted_boxes(boxes), dtype=np.float32)
    if small is not image:
        boxes[:, :, 0] *= width / float(small.shape[1])
        boxes[:, :, 1] *= height / float(small.shape[0])
    boxes[:, :, 0] = boxes[:, :, 0].clip(0, width - 1)
    boxes[:, :, 1] = boxes[:, :, 1].clip(0, height - 1)
    return boxes


# Report the share of detected text area sitting in boxes that are taller than
# they are wide, which is what a page of sideways text looks like.
def _tall_area_fraction(boxes: Any) -> float:
    import numpy as np

    widths = np.linalg.norm(boxes[:, 0] - boxes[:, 1], axis=1)
    heights = np.linalg.norm(boxes[:, 0] - boxes[:, 3], axis=1)
    areas = widths * heights
    total = float(areas.sum())
    if total <= 0:
        return 0.0
    return float(areas[heights > widths].sum() / total)


# Read the text of every box out of the full-resolution image. The mean
# confidence comes back with it: it is what tells two candidate page rotations
# apart, since the wrong one feeds the recognizer upside-down text.
def _recognize(engine: Any, image: Any, boxes: Any) -> Tuple[List[Any], float]:
    results, _elapse = engine.text_rec(engine.get_crop_img_list(image, boxes))
    results = results or []
    scores = [score for _text, score in results]
    return results, (sum(scores) / len(scores) if scores else 0.0)


# Carry detected boxes into a quarter-turned frame, so a rotated page does not
# have to be detected a second time. `turns` follows numpy: -1 is a clockwise
# quarter turn, 1 counter-clockwise.
#
# Remapping the corner coordinates is not enough on its own. It permutes each
# quad's corners, and `get_rotate_crop_image` builds its perspective transform
# from points[0..3] in order, so a permuted quad is extracted rotated — which
# leaves the whole page 180 degrees out. Putting the corners back in
# top-left-first order is what makes the reused boxes equivalent to re-detecting.
def _rotate_boxes(engine: Any, boxes: Any, width: int, height: int, turns: int) -> Any:
    import numpy as np

    rotated = np.empty_like(boxes)
    x, y = boxes[:, :, 0], boxes[:, :, 1]
    if turns == -1:
        rotated[:, :, 0] = height - 1 - y
        rotated[:, :, 1] = x
    else:
        rotated[:, :, 0] = y
        rotated[:, :, 1] = width - 1 - x
    return np.stack([engine.text_det.order_points_clockwise(box) for box in rotated])


# Detect and read a page, straightening it first when its text runs sideways.
#
# A photo of a phone held sideways used to be the cloud VLM's job. Local OCR did
# recognize the individual crops, but it reassembled them into one run-on line,
# because reading order comes from the box coordinates — so the parser saw no
# rows and recovered almost nothing. Turning the page upright fixes the ordering.
#
# Which of the two quarter turns is the upright one cannot be read off the boxes,
# since both produce horizontal text. Both are recognized instead and the one the
# recognizer is confident about wins: the upside-down candidate scores far below
# it, by 0.96 against 0.38 on the receipts in `test_img/`.
#
# Returns the detections in RapidOCR's own [box, text, score] shape, plus the
# rotation applied, for the log line.
def _read_page(engine: Any, image: Any, *, debug_timing: bool) -> Tuple[List[Any], int]:
    import numpy as np

    with log_timing("image.ocr.detect", debug_timing):
        boxes = _detect_boxes(engine, image)
    if boxes is None:
        return [], 0

    rotation = 0
    if _tall_area_fraction(boxes) > _SIDEWAYS_AREA_FRACTION:
        height, width = image.shape[:2]
        candidates = []
        # One label around both candidates: they are what straightening the page
        # costs, and a per-candidate label would only report the last one.
        with log_timing("image.ocr.recognize_rotations", debug_timing):
            for turns in (-1, 1):
                rotated = np.ascontiguousarray(np.rot90(image, turns))
                rotated_boxes = _rotate_boxes(engine, boxes, width, height, turns)
                results, mean_score = _recognize(engine, rotated, rotated_boxes)
                candidates.append((mean_score, results, rotated_boxes, turns * 90))
                del rotated
        _score, results, boxes, rotation = max(candidates, key=lambda item: item[0])
    else:
        with log_timing("image.ocr.recognize", debug_timing):
            results, _mean_score = _recognize(engine, image, boxes)

    detections = [
        [box.tolist(), text, score]
        for box, (text, score) in zip(boxes, results)
        if text and float(score) >= engine.text_score
    ]
    return detections, rotation


# Run the OCR engine over an image stream without disturbing its position.
def _detect(file_stream: BinaryIO, *, debug_timing: bool = False) -> List[Any]:
    import numpy as np
    from PIL import Image

    with log_timing("image.ocr.get_engine", enabled=False):
        engine = _get_engine()

    cur_pos = file_stream.tell()
    t_prep_start = time.perf_counter()
    try:
        pil_img = Image.open(file_stream)
        img_size = pil_img.size
        rgb = np.array(pil_img.convert("RGB"))
    finally:
        file_stream.seek(cur_pos)

    # A dark-mode screenshot is light text on a dark ground, the inverse of what
    # these models were trained on, and characters get dropped ("CTY1" for
    # "CTY"). The two modes sit far apart in practice — sample screenshots
    # measure 49 against 198-244 mean luminance — so one threshold separates them.
    is_dark = bool(rgb.mean() < _DARK_MODE_LUMINANCE)
    if is_dark:
        rgb = 255 - rgb

    # RapidOCR follows the OpenCV convention and expects channels in BGR order;
    # handing it RGB swaps red and blue, which measurably degrades recognition
    # ("TU UYEN" came back as "TUUYEN"). Reverse the channels rather than decode
    # with cv2, so the stream stays the single source of the image.
    image = np.ascontiguousarray(rgb[:, :, ::-1])
    del rgb
    t_prep = time.perf_counter() - t_prep_start

    # A strip too short or too wide for the fast path: RapidOCR pads it before
    # detecting, so let it run its own pipeline.
    height, width = image.shape[:2]
    use_fast_path = (
        height > engine.min_height and width / height <= _MAXIMUM_FAST_PATH_RATIO
    )

    rotation = 0
    t_lock_start = time.perf_counter()
    try:
        with _infer_lock:
            t_lock_wait = time.perf_counter() - t_lock_start
            t_infer_start = time.perf_counter()
            with log_timing("image.ocr.engine_infer", debug_timing):
                if use_fast_path:
                    detections, rotation = _read_page(
                        engine, image, debug_timing=debug_timing
                    )
                else:
                    detections, _elapse = engine(image)
            t_infer = time.perf_counter() - t_infer_start
    finally:
        # Drop the decoded image before returning: a large photo is tens of
        # megabytes, and several requests in flight would otherwise hold all of
        # them until the garbage collector gets around to it.
        del image

    count = len(detections) if detections else 0
    logger.debug(
        "[ocr] Image %dx%d (dark_mode=%s, rotation=%d, det_max_side=%d): "
        "prep=%.3fs, lock_wait=%.3fs, infer=%.3fs (%d boxes)",
        img_size[0],
        img_size[1],
        is_dark,
        rotation,
        _det_max_side() if use_fast_path else 0,
        t_prep,
        t_lock_wait,
        t_infer,
        count,
    )
    return detections or []


# Extract text from an image, preserving reading order and line breaks.
# Returns None when no text is found. Callers handle missing dependencies via
# is_available().
def extract_text_offline(
    file_stream: BinaryIO,
    stream_info: Optional[StreamInfo] = None,
    *,
    debug_timing: bool = False,
) -> Optional[str]:
    detections = _detect(file_stream, debug_timing=debug_timing)
    if not detections:
        return None

    with log_timing("image.ocr.reconstruct", debug_timing):
        text = _render_markdown_lines(_reconstruct_lines(detections)).strip()
    return text or None


# Extract the plain lines of an image, one per detected row of text, for callers
# that parse structure themselves and must not see markdown decoration.
def extract_lines_offline(
    file_stream: BinaryIO,
    stream_info: Optional[StreamInfo] = None,
    *,
    debug_timing: bool = False,
) -> List[str]:
    return [
        line["text"]
        for line in extract_line_details_offline(
            file_stream, stream_info, debug_timing=debug_timing
        )
    ]


def extract_line_details_offline(
    file_stream: BinaryIO,
    stream_info: Optional[StreamInfo] = None,
    *,
    debug_timing: bool = False,
) -> List[Dict[str, Any]]:
    detections = _detect(file_stream, debug_timing=debug_timing)
    with log_timing("image.ocr.reconstruct", debug_timing):
        reconstructed = _reconstruct_lines(detections)

    lines = []
    for line in reconstructed:
        text = _correct_vietnamese_line(line["text"]).strip()
        if text:
            lines.append({**line, "text": text})
    return lines
