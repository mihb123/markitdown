#!/usr/bin/env python3 -m pytest
"""
Tests for the HTTP API surface: response envelope, error mapping, upload limits
and the download-link flow. Conversions here use formats that need no model or
network, so the suite runs offline.
"""

import io
import os
import struct
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from markitdown_api import create_app, load_config  # noqa: E402

# Reuse the core package's fixtures instead of duplicating binary test files.
TEST_FILES_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "markitdown", "tests", "test_files"
)


# Build a valid 1x1 PNG in memory, so the URL tests need no binary fixture.
def _tiny_png() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (
            struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\xff\xff")
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixels)
        + chunk(b"IEND", b"")
    )


TINY_PNG = _tiny_png()


class _FileHandler(BaseHTTPRequestHandler):
    """Serves the fixtures the URL tests download: an image, text, and a redirect."""

    ROUTES = {
        # path: (status, content type, body)
        "/receipt": (200, "image/png", TINY_PNG),
        # An object-storage style link: an image behind a generic content type.
        "/opaque": (200, "application/octet-stream", TINY_PNG),
        "/notes.txt": (200, "text/plain", b"hello from a link"),
        "/huge.txt": (200, "text/plain", b"x" * (2 * 1024 * 1024)),
    }

    # A link that takes its time, so a conversion holds its slot long enough for
    # the concurrency limit to be observable.
    SLOW_SECONDS = 3

    def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        if self.path == "/slow":
            time.sleep(self.SLOW_SECONDS)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(TINY_PNG)))
            self.end_headers()
            self.wfile.write(TINY_PNG)
            return

        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/receipt")
            self.end_headers()
            return

        status, content_type, body = self.ROUTES.get(
            self.path, (404, "text/plain", b"not found")
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def file_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FileHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


API_KEY = "test-key-0123456789"


# Run every test against a known configuration instead of whatever `.env` the
# developer has in the repo — that file also holds real cloud VLM credentials,
# which no test may pick up.
@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MARKITDOWN_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("MARKITDOWN_API_OUTPUT_DIR", str(tmp_path / "files"))
    for key in ("MARKITDOWN_API_KEYS", "MARKITDOWN_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    # Importing markitdown pulls in magika, which calls dotenv.load_dotenv() and
    # copies the developer's `.env` — cloud VLM credentials included — into the
    # real environment, where it outranks MARKITDOWN_ENV_FILE above. Clearing the
    # keys is what keeps this suite offline instead of billing a live endpoint.
    for key in (
        "MARKITDOWN_VLM_BASE_URL",
        "MARKITDOWN_VLM_API_KEY",
        "MARKITDOWN_VLM_MODEL",
    ):
        monkeypatch.setenv(key, "")


# A client holding a valid key, on a server that may fetch the local fixture
# server and never loads the OCR model.
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MARKITDOWN_API_MAX_UPLOAD_MB", "1")
    # The fixture server lives on 127.0.0.1, which the SSRF guard blocks by default.
    monkeypatch.setenv("MARKITDOWN_API_ALLOW_PRIVATE_URLS", "true")
    # These tests assert on request handling, not on recognition quality.
    monkeypatch.setenv("MARKITDOWN_DISABLE_LOCAL_OCR", "1")
    monkeypatch.setenv("MARKITDOWN_API_KEYS", API_KEY)
    app = TestClient(create_app(load_config()))
    app.headers.update({"X-API-Key": API_KEY})
    return app


# Same, but with the SSRF guard left at its default.
@pytest.fixture
def strict_client(monkeypatch):
    monkeypatch.setenv("MARKITDOWN_API_KEYS", API_KEY)
    app = TestClient(create_app(load_config()))
    app.headers.update({"X-API-Key": API_KEY})
    return app


# A server with no keys configured, i.e. authentication disabled.
@pytest.fixture
def open_client():
    return TestClient(create_app(load_config()))


# A server that admits one conversion at a time and gives up on the queue after
# a second, so load shedding can be observed without a slow test.
@pytest.fixture
def busy_client(monkeypatch):
    monkeypatch.setenv("MARKITDOWN_API_ALLOW_PRIVATE_URLS", "true")
    monkeypatch.setenv("MARKITDOWN_DISABLE_LOCAL_OCR", "1")
    monkeypatch.setenv("MARKITDOWN_API_KEYS", API_KEY)
    monkeypatch.setenv("MARKITDOWN_API_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("MARKITDOWN_API_QUEUE_TIMEOUT_SECONDS", "1")
    client = TestClient(create_app(load_config()))
    client.headers.update({"X-API-Key": API_KEY})
    return client


def test_health_reports_capabilities(client):
    payload = client.get("/health").json()
    assert payload["status"] == "success"
    assert payload["data"]["version"]
    assert set(payload["data"]) >= {
        "cloud_vlm_configured",
        "local_ocr_available",
        "plugins_enabled",
    }


def test_document_conversion_returns_a_working_download_link(client):
    with open(os.path.join(TEST_FILES_DIR, "test.docx"), "rb") as stream:
        response = client.post(
            "/v1/documents", files={"file": ("test.docx", stream, "")}
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["file_name"] == "test.md"
    assert data["characters"] > 0
    assert data["download_url"].endswith(f"/v1/files/{data['file_id']}")

    download = client.get(f"/v1/files/{data['file_id']}")
    assert download.status_code == 200
    assert 'filename="test.md"' in download.headers["content-disposition"]
    assert len(download.text) > 0


def test_expired_links_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKITDOWN_API_FILE_TTL_MINUTES", "1")
    client = TestClient(create_app(load_config()))

    response = client.post("/v1/documents", files={"file": ("note.txt", b"hello", "")})
    file_id = response.json()["data"]["file_id"]

    stored = tmp_path / "files" / file_id
    os.utime(stored, (0, 0))
    assert client.get(f"/v1/files/{file_id}").status_code == 404


def test_unknown_and_malformed_file_ids_are_not_served(client):
    assert client.get("/v1/files/unknown.txt").status_code == 404
    assert client.get("/v1/files/../../../etc/passwd").status_code == 404


def test_generated_file_names_cannot_escape_the_output_directory(client):
    response = client.post(
        "/v1/documents", files={"file": ("../../etc/passwd", b"hello", "")}
    )
    assert response.status_code == 200
    assert response.json()["data"]["file_name"] == "passwd.md"


def test_image_endpoints_reject_other_formats(client):
    response = client.post("/v1/text", files={"file": ("report.docx", b"data", "")})
    assert response.status_code == 415
    assert response.json()["status"] == "error"


def test_describe_rejects_an_unsupported_language(client):
    response = client.post(
        "/v1/describe",
        files={"file": ("photo.png", b"data", "image/png")},
        data={"language": "fr"},
    )
    assert response.status_code == 400
    assert "en, vi" in response.json()["message"]


def test_uploads_over_the_limit_are_refused(client):
    oversized = io.BytesIO(b"x" * (2 * 1024 * 1024))
    response = client.post(
        "/v1/documents", files={"file": ("big.txt", oversized, "text/plain")}
    )
    assert response.status_code == 413


def test_empty_uploads_are_refused(client):
    response = client.post("/v1/documents", files={"file": ("empty.txt", b"", "")})
    assert response.status_code == 400


def test_a_request_without_file_or_url_is_refused(client):
    response = client.post("/v1/bank", data={"debug": "false"})
    assert response.status_code == 400
    assert response.json()["status"] == "error"
    assert response.json()["data"] is None


def test_a_request_with_both_file_and_url_is_refused(client, file_server):
    response = client.post(
        "/v1/bank",
        files={"file": ("receipt.png", TINY_PNG, "image/png")},
        data={"url": f"{file_server}/receipt"},
    )
    assert response.status_code == 400
    assert "exactly one" in response.json()["message"]


def test_the_bank_endpoint_never_calls_the_cloud_vlm(client, file_server, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("/v1/bank must not reach the cloud VLM")

    monkeypatch.setattr("markitdown.converters._vlm_client.extract_bank_transfer", fail)
    # `cloud=true` used to force the cloud backend; the field is gone, so an
    # unknown form field is all it is now.
    response = client.post(
        "/v1/bank", data={"url": f"{file_server}/receipt", "cloud": "true"}
    )
    assert response.status_code == 200
    assert set(response.json()["data"]) == {
        "amount",
        "account_number",
        "account_name",
        "bank",
        "content",
        "transaction_time",
    }


def test_bank_debug_does_not_report_cloud_state(client, file_server):
    response = client.post(
        "/v1/bank", data={"url": f"{file_server}/receipt", "debug": "true"}
    )
    assert response.status_code == 200
    assert "cloud_vlm_configured" not in response.json()["debug"]


def test_bank_endpoint_with_debug_includes_timings(client, file_server):
    response = client.post(
        "/v1/bank",
        data={"url": f"{file_server}/receipt", "debug": "true"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "debug" in body
    assert "timings" in body["debug"]
    assert "total" in body["debug"]["timings"]
    assert "source_load" in body["debug"]["timings"]


def test_a_document_url_is_downloaded_and_converted(client, file_server):
    response = client.post("/v1/documents", data={"url": f"{file_server}/notes.txt"})
    assert response.status_code == 200

    data = response.json()["data"]
    assert data["file_name"] == "notes.md"
    assert "hello from a link" in client.get(f"/v1/files/{data['file_id']}").text


def test_an_image_url_is_accepted_by_the_image_endpoints(client, file_server):
    response = client.post("/v1/text", data={"url": f"{file_server}/receipt"})
    assert response.status_code == 200
    assert response.json()["data"]["file_name"] == "receipt.txt"


def test_an_image_behind_a_generic_content_type_is_recognized(client, file_server):
    response = client.post("/v1/text", data={"url": f"{file_server}/opaque"})
    assert response.status_code == 200


def test_a_non_image_url_is_refused_by_the_bank_endpoint(client, file_server):
    response = client.post("/v1/bank", data={"url": f"{file_server}/notes.txt"})
    assert response.status_code == 415
    assert response.json()["error_code"] == "unsupported_format"


def test_redirects_are_followed(client, file_server):
    response = client.post("/v1/text", data={"url": f"{file_server}/redirect"})
    assert response.status_code == 200


def test_a_url_over_the_limit_is_refused(client, file_server):
    response = client.post("/v1/documents", data={"url": f"{file_server}/huge.txt"})
    assert response.status_code == 413


def test_a_missing_url_reports_the_upstream_status(client, file_server):
    response = client.post("/v1/documents", data={"url": f"{file_server}/nope.txt"})
    # 424, not 5xx: a proxy in front of this API rewrites 5xx bodies, and the
    # client needs to read error_code to tell a dead link from a dead server.
    assert response.status_code == 424
    assert response.json()["error_code"] == "upstream_fetch_failed"
    assert "404" in response.json()["message"]


def test_non_http_schemes_are_refused(client):
    response = client.post("/v1/documents", data={"url": "file:///etc/passwd"})
    assert response.status_code == 400
    assert "http" in response.json()["message"]


def test_private_addresses_are_blocked_by_default(strict_client, file_server):
    response = strict_client.post(
        "/v1/documents", data={"url": f"{file_server}/notes.txt"}
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "blocked_url"
    assert "non-public" in response.json()["message"]


def test_every_failure_carries_a_machine_readable_code(client, file_server):
    cases = [
        ("missing_source", client.post("/v1/bank")),
        (
            "ambiguous_source",
            client.post(
                "/v1/bank",
                files={"file": ("a.png", TINY_PNG, "image/png")},
                data={"url": f"{file_server}/receipt"},
            ),
        ),
        (
            "empty_file",
            client.post("/v1/documents", files={"file": ("a.txt", b"", "")}),
        ),
        (
            "file_too_large",
            client.post("/v1/documents", data={"url": f"{file_server}/huge.txt"}),
        ),
        (
            "unsupported_format",
            client.post("/v1/text", data={"url": f"{file_server}/notes.txt"}),
        ),
        (
            "invalid_url",
            client.post("/v1/text", data={"url": "ftp://example.com/a.png"}),
        ),
        (
            "upstream_fetch_failed",
            client.post("/v1/documents", data={"url": f"{file_server}/nope"}),
        ),
        (
            "invalid_parameter",
            client.post(
                "/v1/describe",
                files={"file": ("a.png", TINY_PNG, "image/png")},
                data={"language": "de"},
            ),
        ),
        ("file_not_found", client.get("/v1/files/nothing-here")),
    ]

    for expected_code, response in cases:
        payload = response.json()
        assert payload["status"] == "error", expected_code
        assert payload["error_code"] == expected_code, payload
        assert payload["data"] is None
        assert payload["message"]


def test_the_unsupported_format_message_names_what_arrived(client, file_server):
    message = client.post("/v1/bank", data={"url": f"{file_server}/notes.txt"}).json()[
        "message"
    ]
    assert "text/plain" in message
    assert "notes.txt" in message


def test_control_characters_never_reach_the_client(client):
    response = client.post(
        "/v1/documents", data={"url": "https://exa\r\nmple.com/a\npng"}
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_url"
    assert "\n" not in response.text and "\r" not in response.text


def test_urls_with_embedded_credentials_are_refused(client):
    response = client.post(
        "/v1/documents", data={"url": "https://user:secret@example.com/a.pdf"}
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_url"
    assert "secret" not in response.json()["message"]


def test_overlong_urls_are_refused(client):
    response = client.post(
        "/v1/documents", data={"url": "https://example.com/" + "a" * 4096}
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_url"
    assert len(response.json()["message"]) < 500


def test_requests_without_a_key_are_rejected(client):
    response = client.post(
        "/v1/bank",
        files={"file": ("a.png", TINY_PNG, "image/png")},
        headers={"X-API-Key": ""},
    )
    assert response.status_code == 401
    assert response.json()["error_code"] == "unauthorized"


def test_a_wrong_key_is_rejected(client):
    response = client.post(
        "/v1/documents",
        files={"file": ("a.txt", b"hello", "")},
        headers={"X-API-Key": "not-the-key"},
    )
    assert response.status_code == 401


def test_a_bearer_token_is_accepted(client):
    response = client.post(
        "/v1/documents",
        files={"file": ("a.txt", b"hello", "")},
        headers={"X-API-Key": "", "Authorization": f"Bearer {API_KEY}"},
    )
    assert response.status_code == 200


def test_health_and_docs_stay_public(client):
    assert client.get("/health", headers={"X-API-Key": ""}).status_code == 200
    assert client.get("/health").json()["data"]["auth_required"] is True
    assert client.get("/openapi.json", headers={"X-API-Key": ""}).status_code == 200


def test_the_docs_ask_for_the_key_once_for_every_endpoint(client):
    schema = client.get("/openapi.json").json()

    scheme = schema["components"]["securitySchemes"]["ApiKeyHeader"]
    assert (scheme["type"], scheme["in"], scheme["name"]) == (
        "apiKey",
        "header",
        "X-API-Key",
    )
    assert all(
        operation["security"] == [{"ApiKeyHeader": []}]
        for operations in schema["paths"].values()
        for operation in operations.values()
    )


def test_download_links_work_without_the_key(client):
    created = client.post("/v1/documents", files={"file": ("a.txt", b"hello", "")})
    file_id = created.json()["data"]["file_id"]

    download = client.get(f"/v1/files/{file_id}", headers={"X-API-Key": ""})
    assert download.status_code == 200
    assert download.headers["x-content-type-options"] == "nosniff"


def test_downloads_can_be_locked_down_too(monkeypatch):
    monkeypatch.setenv("MARKITDOWN_API_KEYS", API_KEY)
    monkeypatch.setenv("MARKITDOWN_API_PROTECT_DOWNLOADS", "true")
    client = TestClient(create_app(load_config()))

    created = client.post(
        "/v1/documents",
        files={"file": ("a.txt", b"hello", "")},
        headers={"X-API-Key": API_KEY},
    )
    file_id = created.json()["data"]["file_id"]

    assert client.get(f"/v1/files/{file_id}").status_code == 401
    assert (
        client.get(f"/v1/files/{file_id}", headers={"X-API-Key": API_KEY}).status_code
        == 200
    )


def test_health_reports_load_and_memory(client):
    data = client.get("/health").json()["data"]
    assert data["max_concurrency"] >= 1
    assert data["in_flight"] == 0
    assert data["uptime_seconds"] >= 0
    # /proc is what the reading comes from; elsewhere the field is simply absent.
    assert data.get("rss_mb", 1) > 0


def test_every_response_carries_a_request_id(client):
    # The id is what ties a client-side failure to the line the server logged.
    assert client.get("/health").headers["X-Request-ID"]
    refused = client.post("/v1/bank")
    assert refused.status_code == 400
    assert refused.headers["X-Request-ID"]


def test_work_is_refused_with_a_retryable_code_when_the_queue_is_full(
    busy_client, file_server
):
    results = []

    def call():
        response = busy_client.post("/v1/bank", data={"url": f"{file_server}/slow"})
        results.append(response)

    threads = [threading.Thread(target=call) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    codes = sorted(response.status_code for response in results)
    assert codes.count(200) == 1, codes
    assert codes.count(503) == 2, codes

    busy = next(response for response in results if response.status_code == 503)
    assert busy.json()["error_code"] == "server_busy"
    # The client is told when to come back rather than left to guess.
    assert int(busy.headers["Retry-After"]) >= 1
    assert busy_client.get("/health").json()["data"]["rejected_busy"] == 2


def test_health_answers_while_conversions_are_queued(busy_client, file_server):
    slow = threading.Thread(
        target=lambda: busy_client.post("/v1/bank", data={"url": f"{file_server}/slow"})
    )
    slow.start()
    try:
        time.sleep(0.5)
        # Monitoring must not be blocked by the conversion queue.
        assert busy_client.get("/health").status_code == 200
    finally:
        slow.join(timeout=30)


def test_authentication_is_off_when_no_key_is_configured(open_client):
    assert open_client.get("/health").json()["data"]["auth_required"] is False
    response = open_client.post("/v1/documents", files={"file": ("a.txt", b"hi", "")})
    assert response.status_code == 200


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
