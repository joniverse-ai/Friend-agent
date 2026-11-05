"""사진 글자 읽기 엔진 시험. Vision 은 가짜 응답(httpx.MockTransport)으로 시험해 실제 Google 을 부르지 않는다."""
import base64
import json

import httpx
import pytest

from app import ocr

PHOTO = b"\xff\xd8\xff fake jpeg bytes"


def make_client(vision_status=200, vision_body=None, token_status=200, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "metadata.google.internal":
            assert request.headers["Metadata-Flavor"] == "Google"
            if token_status != 200:
                return httpx.Response(token_status)
            return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
        assert str(request.url) == ocr.VISION_URL
        if seen is not None:
            seen.append(request)
        return httpx.Response(vision_status, json=vision_body if vision_body is not None else {})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_vision_reads_text_and_sends_photo_with_token():
    seen = []
    body = {"responses": [{"fullTextAnnotation": {"text": "  11월 20일 오후 3시  \n\n치과\n"}}]}
    text = ocr.read_text(PHOTO, engine="vision", http=make_client(vision_body=body, seen=seen))

    assert text == "11월 20일 오후 3시\n치과"
    request = seen[0]
    assert request.headers["Authorization"] == "Bearer fake-token"
    sent = json.loads(request.content)["requests"][0]
    assert base64.b64decode(sent["image"]["content"]) == PHOTO
    assert sent["features"] == [{"type": "DOCUMENT_TEXT_DETECTION"}]
    assert sent["imageContext"]["languageHints"] == ["ko", "en"]


def test_vision_photo_without_text_returns_empty():
    body = {"responses": [{}]}
    assert ocr.read_text(PHOTO, engine="vision", http=make_client(vision_body=body)) == ""


def test_vision_permission_error_becomes_ocr_error():
    body = {"error": {"code": 403, "message": "Cloud Vision API has not been used in project"}}
    with pytest.raises(ocr.OcrError, match="403"):
        ocr.read_text(PHOTO, engine="vision", http=make_client(vision_status=403, vision_body=body))


def test_vision_bad_image_becomes_ocr_error():
    body = {"responses": [{"error": {"code": 3, "message": "Bad image data."}}]}
    with pytest.raises(ocr.OcrError, match="읽을 수 없습니다"):
        ocr.read_text(PHOTO, engine="vision", http=make_client(vision_body=body))


def test_vision_without_cloud_run_token_becomes_ocr_error():
    with pytest.raises(ocr.OcrError, match="로그인"):
        ocr.read_text(PHOTO, engine="vision", http=make_client(token_status=404))


def test_photo_over_5mb_is_rejected_before_calling_vision():
    def handler(request):
        raise AssertionError("5MB 넘는 사진은 Vision 을 부르면 안 된다")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ocr.OcrError, match="5MB"):
        ocr.read_text(b"x" * (ocr.MAX_BYTES + 1), engine="vision", http=client)


def test_vision_log_has_count_not_text(capsys):
    body = {"responses": [{"fullTextAnnotation": {"text": "치과 예약 비밀메모"}}]}
    ocr.read_text(PHOTO, engine="vision", http=make_client(vision_body=body))
    out = capsys.readouterr().out
    assert "비밀메모" not in out
    assert '"chars": 10' in out


def test_engine_setting(monkeypatch):
    from app.config import load_settings

    monkeypatch.setenv("OCR_ENGINE", "Vision")
    assert load_settings().ocr_engine == "vision"
    monkeypatch.setenv("OCR_ENGINE", "something-else")
    assert load_settings().ocr_engine == "tesseract"
    monkeypatch.delenv("OCR_ENGINE")
    assert load_settings().ocr_engine == "tesseract"
