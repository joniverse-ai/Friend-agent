"""사진 속 글자 읽기(OCR). 일정표나 공지 사진을 보내면 글자를 읽어 대화 입력창에 넣어 준다.

엔진은 OCR_ENGINE 으로 고른다.
- tesseract(기본): 서버 안에서 읽는다. Python 라이브러리(pytesseract)는 requirements.txt 로 설치되지만,
  실제로 글자를 읽는 tesseract 는 운영체제에 따로 설치하는 프로그램이다. 노트북에는 직접 설치하고,
  서버에서는 실행 환경(이미지)에 들어 있어야 한다.
- vision: Google Cloud Vision 의 문서 글자 읽기. 한국어와 손글씨를 더 정확히 읽는다.
  Cloud Run 이 실행 계정으로 내주는 임시 토큰으로 부르므로 키 파일이 필요 없다.
  프로젝트에서 Vision API(vision.googleapis.com)를 켜 두어야 한다.
"""
import base64
import io

import httpx

from app.logs import log

MAX_BYTES = 5 * 1024 * 1024  # 사진 한 장 최대 5MB

# 미국 전용 창구. 사진이 미국 안에서 처리된다.
VISION_URL = "https://us-vision.googleapis.com/v1/images:annotate"
# Cloud Run 안에서만 열리는 주소. 실행 계정의 임시 토큰을 내준다.
TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
TIMEOUT = 20.0


class OcrError(Exception):
    pass


def read_text(data: bytes, engine: str = "tesseract", http: httpx.Client | None = None) -> str:
    if len(data) > MAX_BYTES:
        raise OcrError("사진이 5MB보다 큽니다.")
    if engine == "vision":
        text = _read_with_vision(data, http)
        lang = "vision"
    else:
        text, lang = _read_with_tesseract(data)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    # 읽은 글은 개인정보라 남기지 않는다. 엔진과 글자 수만 남긴다.
    log("INFO", "사진 글자 읽기", engine=engine, lang=lang, chars=len(text))
    return text


def _read_with_tesseract(data: bytes) -> tuple[str, str]:
    import pytesseract
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(io.BytesIO(data))
    except UnidentifiedImageError as error:
        raise OcrError("사진 파일을 읽을 수 없습니다.") from error
    try:
        langs = pytesseract.get_languages(config="")
        lang = "kor+eng" if "kor" in langs else "eng"
        if lang == "eng":
            log("WARNING", "tesseract 한국어 데이터가 없어 영어로만 읽습니다.")
        # --psm 6: 사진 전체를 글자 덩어리 하나로 본다. 기본값(자동)은 여백이 많은 공지 사진을 빈 페이지로 읽는 경우가 있다.
        text = pytesseract.image_to_string(image, lang=lang, config="--psm 6")
    except pytesseract.TesseractNotFoundError as error:
        log("ERROR", "tesseract 프로그램을 찾을 수 없습니다. 실행 환경에 tesseract 가 설치되어 있지 않습니다.")
        raise OcrError("사진에서 글자를 읽는 프로그램이 서버에 없습니다.") from error
    return text, lang


def _read_with_vision(data: bytes, http: httpx.Client | None) -> str:
    client = http or httpx.Client(timeout=TIMEOUT)
    try:
        token = _access_token(client)
        body = {
            "requests": [{
                "image": {"content": base64.b64encode(data).decode("ascii")},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                "imageContext": {"languageHints": ["ko", "en"]},
            }]
        }
        try:
            response = client.post(VISION_URL, json=body, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as error:
            log("ERROR", "Vision API 에 연결하지 못했습니다.", error=type(error).__name__)
            raise OcrError("사진 글자 읽기 서비스에 연결하지 못했습니다.") from error
        if response.status_code != 200:
            # 오류 본문에는 사진 내용이 없고, 권한·API 꺼짐 같은 원인이 들어 있다.
            log("ERROR", "Vision API 오류", status=response.status_code, detail=response.text[:300])
            raise OcrError(f"사진 글자 읽기 서비스 오류({response.status_code})")
        result = (response.json().get("responses") or [{}])[0]
        if "error" in result:
            log("ERROR", "Vision API 가 사진을 읽지 못했습니다.", detail=str(result["error"].get("message", ""))[:300])
            raise OcrError("사진 파일을 읽을 수 없습니다.")
        return (result.get("fullTextAnnotation") or {}).get("text", "")
    finally:
        if http is None:
            client.close()


def _access_token(client: httpx.Client) -> str:
    try:
        response = client.get(TOKEN_URL, headers={"Metadata-Flavor": "Google"})
        response.raise_for_status()
        return response.json()["access_token"]
    except (httpx.HTTPError, KeyError, ValueError) as error:
        log("ERROR", "실행 계정 토큰을 받지 못했습니다. OCR_ENGINE=vision 은 Cloud Run 에서만 쓸 수 있습니다.")
        raise OcrError("사진 글자 읽기 서비스에 로그인하지 못했습니다.") from error
