import base64

import pytest

from scripts.benchmark_checkout_recovery import blob_sha, compare_bytes, decode_blob, parse_response, safe_path


def test_blob_requires_declared_identity_and_actual_git_hash():
    raw = b"a\r\nb\r\n"
    sha = blob_sha(raw)
    blob = {"sha": sha, "size": len(raw), "encoding": "base64", "content": base64.b64encode(raw).decode()}
    assert decode_blob(blob, sha, len(raw)) == raw
    with pytest.raises(ValueError):
        decode_blob({**blob, "content": base64.b64encode(b"c\r\nd\r\n").decode()}, sha, len(raw))
    with pytest.raises(ValueError):
        decode_blob(blob, "0" * 40, len(raw))
    with pytest.raises(ValueError):
        decode_blob(blob, sha, len(raw) + 1)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../../escape", "a\\..\\escape", ""])
def test_repository_overlay_paths_cannot_escape(name):
    with pytest.raises(ValueError):
        safe_path(name)


def test_byte_differences_are_described_without_rewriting_archive():
    assert compare_bytes(None, b"x") == "missing_from_original_archive"
    assert compare_bytes(b"a\r\n", b"a\n") == "line_endings_only"
    assert compare_bytes(b"a\n", b"b\n") == "other_byte_difference"
    assert compare_bytes(b"a", b"a") == "identical"


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_http_capture_keeps_status_and_body_bytes(newline):
    raw = newline.join([b"HTTP/2.0 404 Not Found", b"Content-Type: application/json", b"", b'{"message":"Not Found"}'])
    status, headers, body = parse_response(raw)
    assert status == 404 and body == b'{"message":"Not Found"}'
    assert headers.startswith(b"HTTP/")
    with pytest.raises(ValueError):
        parse_response(body)
