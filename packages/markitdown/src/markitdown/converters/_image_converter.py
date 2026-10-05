from typing import BinaryIO, Any, Union
import base64
import mimetypes
from ._exiftool import exiftool_metadata
from . import _bank_transfer
from . import _local_ocr
from . import _local_caption
from . import _vlm_client
from ._bank_fields import to_json
from ._timing import is_debug_timing_enabled, log_timing
from .._base_converter import DocumentConverter, DocumentConverterResult
from .._stream_info import StreamInfo

ACCEPTED_MIME_TYPE_PREFIXES = [
    "image/jpeg",
    "image/png",
    "image/webp",
]

ACCEPTED_FILE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".webp"]


class ImageConverter(DocumentConverter):
    """
    Converts images to markdown via extraction of metadata (if `exiftool` is installed), and description via a multimodal LLM (if an llm_client is configured).
    """

    def accepts(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> bool:
        mimetype = (stream_info.mimetype or "").lower()
        extension = (stream_info.extension or "").lower()

        if extension in ACCEPTED_FILE_EXTENSIONS:
            return True

        for prefix in ACCEPTED_MIME_TYPE_PREFIXES:
            if mimetype.startswith(prefix):
                return True

        return False

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,  # Options to pass to the converter
    ) -> DocumentConverterResult:
        md_content = ""
        debug_timing = is_debug_timing_enabled(kwargs.get("debug_timing"))
        use_cloud = bool(kwargs.get("image_cloud"))
        description_use_cloud = bool(kwargs.get("image_description_cloud", use_cloud))

        # Read a transfer receipt (CLI: --bank). The structured result replaces
        # the markdown body, so callers can consume the JSON directly. This path
        # is local-only: `image_cloud` does not reach it.
        if kwargs.get("image_bank"):
            with log_timing("image.bank.total", debug_timing):
                bank_info = _bank_transfer.extract_bank_info(
                    file_stream,
                    stream_info,
                    debug_timing=debug_timing,
                )
            return DocumentConverterResult(markdown=to_json(bank_info))

        # Add metadata
        with log_timing("image.metadata", debug_timing):
            metadata = exiftool_metadata(
                file_stream, exiftool_path=kwargs.get("exiftool_path")
            )

        if metadata:
            for f in [
                "ImageSize",
                "Title",
                "Caption",
                "Description",
                "Keywords",
                "Artist",
                "Author",
                "DateTimeOriginal",
                "CreateDate",
                "GPSPosition",
            ]:
                if f in metadata:
                    md_content += f"{f}: {metadata[f]}\n"

        # Extract text (CLI: --text): use local OCR by default, or cloud VLM
        # only when the caller explicitly passes --cloud.
        if kwargs.get("image_ocr"):
            with log_timing("image.ocr.total", debug_timing):
                ocr_text = self._get_text(
                    file_stream,
                    stream_info,
                    use_cloud=use_cloud,
                    debug_timing=debug_timing,
                )
            if ocr_text is not None and ocr_text.strip():
                md_content += "\n# Text:\n" + ocr_text.strip() + "\n"

        # Describe the image: an explicitly configured llm_client wins; the
        # env-configured VLM is used only when the CLI passes --cloud.
        description = None
        description_language = kwargs.get("description_language") or "en"
        llm_client = kwargs.get("llm_client")
        llm_model = kwargs.get("llm_model")
        if llm_client is not None and llm_model is not None:
            with log_timing("image.description.llm", debug_timing):
                description = self._get_llm_description(
                    file_stream,
                    stream_info,
                    client=llm_client,
                    model=llm_model,
                    prompt=kwargs.get("llm_prompt"),
                    language=description_language,
                )
        elif kwargs.get("image_description"):
            with log_timing("image.description.total", debug_timing):
                description = self._get_description(
                    file_stream,
                    stream_info,
                    model_id=kwargs.get("caption_model"),
                    language=description_language,
                    use_cloud=description_use_cloud,
                    debug_timing=debug_timing,
                )

        if description is not None and description.strip():
            md_content += "\n# Description:\n" + description.strip() + "\n"

        return DocumentConverterResult(
            markdown=md_content,
        )

    def _get_text(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        *,
        use_cloud: bool = False,
        debug_timing: bool = False,
    ) -> Union[None, str]:
        # Use local OCR by default; --cloud switches to the configured VLM.
        if use_cloud:
            _vlm_client.ensure_configured()
            with log_timing("image.ocr.cloud", debug_timing):
                text = _vlm_client.extract_text(file_stream, stream_info)
            return text
        return self._get_offline_text(
            file_stream, stream_info, debug_timing=debug_timing
        )

    def _get_description(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        *,
        model_id: Union[None, str] = None,
        language: str = "en",
        use_cloud: bool = False,
        debug_timing: bool = False,
    ) -> Union[None, str]:
        # Use local captioning by default; --cloud switches to the VLM.
        if use_cloud:
            _vlm_client.ensure_configured()
            with log_timing("image.description.cloud", debug_timing):
                description = _vlm_client.describe(
                    file_stream, stream_info, language=language
                )
            return description
        return self._get_offline_description(
            file_stream,
            stream_info,
            model_id=model_id,
            debug_timing=debug_timing,
        )

    def _get_offline_text(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        *,
        debug_timing: bool = False,
    ) -> Union[None, str]:
        # Best-effort OCR: tolerate a missing extra or a model failure the same
        # way missing exiftool is tolerated above.
        with log_timing("image.ocr.offline.available", debug_timing):
            is_available = _local_ocr.is_available()
        if not is_available:
            return None
        try:
            with log_timing("image.ocr.offline.extract", debug_timing):
                return _local_ocr.extract_text_offline(file_stream, stream_info)
        except Exception:
            return None

    def _get_offline_description(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        *,
        model_id: Union[None, str] = None,
        debug_timing: bool = False,
    ) -> Union[None, str]:
        # Best-effort captioning: tolerate missing deps / model failures.
        with log_timing("image.description.offline.available", debug_timing):
            is_available = _local_caption.is_available()
        if not is_available:
            return None
        try:
            with log_timing("image.description.offline.extract", debug_timing):
                return _local_caption.describe_image_offline(
                    file_stream, stream_info, model_id=model_id
                )
        except Exception:
            return None

    def _get_llm_description(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        *,
        client,
        model,
        prompt=None,
        language: str = "en",
    ) -> Union[None, str]:
        if prompt is None or prompt.strip() == "":
            prompt = _vlm_client.description_prompt(language)

        # Get the content type
        content_type = stream_info.mimetype
        if not content_type:
            content_type, _ = mimetypes.guess_type(
                "_dummy" + (stream_info.extension or "")
            )
        if not content_type:
            content_type = "application/octet-stream"

        # Convert to base64
        cur_pos = file_stream.tell()
        try:
            base64_image = base64.b64encode(file_stream.read()).decode("utf-8")
        except Exception as e:
            return None
        finally:
            file_stream.seek(cur_pos)

        # Prepare the data-uri
        data_uri = f"data:{content_type};base64,{base64_image}"

        # Prepare the OpenAI API request
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": data_uri,
                        },
                    },
                ],
            }
        ]

        # Call the OpenAI API
        response = client.chat.completions.create(model=model, messages=messages)
        return response.choices[0].message.content
