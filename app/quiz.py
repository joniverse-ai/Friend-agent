"""퀴즈 대결 에이전트의 풀이. 휴대폰 앱(quiz-agent)이 문제나 퀴즈 화면 사진을 보내면 답과 이유를 돌려준다.

- 앱은 퀴즈 서버와 직접 주고받고(참가, 0.5초 확인, 답 제출), 답을 정하는 일만 여기에 맡긴다.
  LLM API 키는 서버에만 있다.
- 이지모드: 퀴즈 서버가 준 문제 JSON(문제 글, 보기, 그림 주소)으로 푼다.
  그림 문제(mode=image)는 서버가 퀴즈 서버에서 그림을 직접 받아 온다. 허용한 퀴즈 서버 주소만 받는다.
- 하드모드(hidden=true): 문제 글이 오지 않으므로 앱이 찍은 퀴즈 화면 사진으로 푼다.
- 모델에게는 보기 번호를 화면처럼 1부터 받고, 퀴즈 서버 형식(0부터)으로 바꿔 돌려준다.
- 문제와 답은 로그에 남기지 않는다. 종류, 걸린 시간, 모델만 남긴다.
"""
import base64
import json
import os
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

import httpx

from app.logs import log

MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_TIMEOUT = 5.0
PER_MINUTE = 30  # 사용자 한 명이 1분에 풀 수 있는 문제 수. 한 판이 11문제 안팎이라 넉넉하다

DEFAULT_HOSTS = "quiz-battle-140312194135.us-central1.run.app,quiz-battle-practice-140312194135.us-central1.run.app"


class QuizError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def allowed_hosts():
    return {h.strip().lower() for h in os.environ.get("QUIZ_HOSTS", DEFAULT_HOSTS).split(",") if h.strip()}


def quiz_model(default):
    """QUIZ_MODEL 이 있으면 퀴즈 풀이만 그 모델로 부른다. 없으면 대화와 같은 모델."""
    return os.environ.get("QUIZ_MODEL", "").strip() or default


# ---- 요청 수 제한 (서버 한 대 안에서만 센다) ----
_calls = defaultdict(deque)
_lock = threading.Lock()


def check_rate(user_id, now=None):
    now = time.monotonic() if now is None else now
    with _lock:
        recent = _calls[user_id]
        while recent and now - recent[0] > 60:
            recent.popleft()
        if len(recent) >= PER_MINUTE:
            raise QuizError(429, "1분에 풀 수 있는 문제 수를 넘었습니다. 잠시 뒤 다시 시도하세요.")
        recent.append(now)


# ---- 그림 받기 ----
def fetch_image(server_url, image_url, http=None):
    """퀴즈 서버의 그림을 받아 (bytes, mime) 로 돌려준다. 허용한 https 주소만 받는다."""
    parsed = urlparse(server_url or "")
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in allowed_hosts():
        raise QuizError(400, "허용하지 않은 퀴즈 서버 주소입니다.")
    if not image_url or not image_url.startswith("/") or image_url.startswith("//"):
        raise QuizError(400, "그림 주소가 올바르지 않습니다.")
    url = f"https://{parsed.netloc}{image_url}"
    client = http or httpx.Client(timeout=IMAGE_TIMEOUT, follow_redirects=False)
    try:
        response = client.get(url)
    except httpx.HTTPError as error:
        raise QuizError(502, "퀴즈 서버에서 그림을 받지 못했습니다.") from error
    finally:
        if http is None:
            client.close()
    if response.status_code != 200:
        raise QuizError(502, f"퀴즈 서버에서 그림을 받지 못했습니다({response.status_code}).")
    if len(response.content) > MAX_IMAGE_BYTES:
        raise QuizError(400, "그림이 5MB보다 큽니다.")
    mime = response.headers.get("content-type", "image/png").split(";")[0].strip() or "image/png"
    return response.content, mime


def decode_photo(photo):
    """앱이 보낸 사진(base64, data URL 머리말 있어도 됨)을 bytes 로."""
    if "," in photo[:100] and photo.startswith("data:"):
        photo = photo.split(",", 1)[1]
    try:
        data = base64.b64decode(photo, validate=False)
    except (ValueError, TypeError) as error:
        raise QuizError(400, "사진을 읽을 수 없습니다.") from error
    if not data:
        raise QuizError(400, "사진이 비어 있습니다.")
    if len(data) > MAX_IMAGE_BYTES:
        raise QuizError(400, "사진이 5MB보다 큽니다.")
    return data


# ---- 지시문 ----
SYSTEM = """너는 퀴즈 대결에 나간 에이전트다. 빠르고 정확하게 정답 하나를 고른다.
퀴즈 범위는 일반 상식, 코딩, 이미지 해석이다.

반드시 아래 JSON 객체 하나만 출력한다.
- 객관식: {"choice": 화면에 보이는 보기 번호(1부터 세는 정수), "reason": "고른 이유"}
- 단답형: {"text": "답", "reason": "고른 이유"}

규칙:
- reason 은 한국어 한두 문장으로 짧게 쓴다. 대회 화면에 공개된다.
- 단답형 text 에는 답만 쓴다. 문제가 요구한 형식(숫자만인지, 단위를 붙이는지, 영어인지)을 그대로 따른다.
  "7" 과 "7개" 는 다른 답이다. 문장이나 설명을 붙이지 않는다.
- 확실하지 않아도 가장 그럴듯한 답 하나를 반드시 고른다. 답을 비워 두지 않는다.
- 사진이 함께 오면 사진 속 퀴즈 화면에서 문제와 보기를 직접 읽는다. 문제와 관계없는 화면 요소(점수, 타이머, QR)는 무시한다."""


def build_prompt(question, has_image):
    kind = question.get("type")
    lines = []
    if question.get("hidden"):
        lines.append("하드모드다. 문제 글이 오지 않았으니 함께 보낸 사진(퀴즈 화면을 찍은 것)에서 문제를 읽어라.")
        if kind == "choice":
            lines.append(f"객관식이고 보기는 {question.get('choice_count', '?')}개다. 화면의 보기 번호(1부터)로 답한다.")
        elif kind == "short":
            lines.append("단답형이다. text 에 답을 쓴다.")
        else:
            lines.append("객관식이면 choice, 단답형이면 text 로 답한다.")
    else:
        # 필드 이름이 바뀌어도 풀 수 있게 문제 데이터를 통째로 넘긴다(그림 주소, 정답 관련 값은 뺀다)
        shown = {k: v for k, v in question.items() if k not in ("image_url", "answer", "correct")}
        lines.append("문제 데이터(JSON): " + json.dumps(shown, ensure_ascii=False))
        choices = question.get("choices") or []
        if kind == "choice" or choices:
            lines.append("보기:")
            lines += [f"{i}. {c}" for i, c in enumerate(choices, start=1)]
            lines.append("객관식이다. choice 에 보기 번호(1부터)를 쓴다.")
        else:
            lines.append("단답형이다. text 에 답을 쓴다.")
        if has_image:
            lines.append("문제에 딸린 그림을 함께 보낸다. 그림을 보고 푼다.")
    return "\n".join(lines)


def choice_count(question):
    if question.get("choices"):
        return len(question["choices"])
    count = question.get("choice_count")
    return int(count) if isinstance(count, int) or (isinstance(count, str) and count.isdigit()) else None


def to_answer(question, result):
    """모델 답을 퀴즈 서버 형식으로. 객관식은 0부터 세는 choice."""
    reason = str(result.get("reason") or "").strip()[:300] or "에이전트가 고른 답"
    kind = question.get("type")
    if kind == "short" or (kind != "choice" and "text" in result and "choice" not in result):
        text = str(result.get("text") or "").strip()
        if not text:
            raise QuizError(502, "에이전트가 답을 만들지 못했습니다.")
        return {"text": text[:200], "reason": reason}
    try:
        number = int(result.get("choice"))
    except (TypeError, ValueError) as error:
        raise QuizError(502, "에이전트가 보기 번호를 고르지 못했습니다.") from error
    count = choice_count(question)
    if number < 1 or (count is not None and number > count):
        raise QuizError(502, "에이전트가 없는 보기 번호를 골랐습니다.")
    return {"choice": number - 1, "reason": reason}


def solve(llm, question, image=None, model=None):
    """문제 하나를 푼다. image 는 (bytes, mime) 또는 None. 돌려주는 값: choice 또는 text, reason, ms."""
    if not isinstance(question, dict) or not question:
        raise QuizError(400, "문제(question)가 필요합니다.")
    if question.get("hidden") and image is None:
        raise QuizError(400, "하드모드 문제는 퀴즈 화면 사진이 필요합니다.")
    started = time.monotonic()
    prompt = build_prompt(question, image is not None)
    try:
        result = llm.complete_vision_json(SYSTEM, prompt, image, model=model)
    except QuizError:
        raise
    except Exception as error:  # 모델 호출 실패. 원인 종류만 남긴다
        log("ERROR", "퀴즈 풀이 실패", error=type(error).__name__)
        raise QuizError(502, "에이전트가 답을 만들지 못했습니다.") from error
    answer = to_answer(question, result)
    ms = int((time.monotonic() - started) * 1000)
    log("INFO", "퀴즈 풀이", type=question.get("type"), hidden=bool(question.get("hidden")),
        image=image is not None, ms=ms, model=model or getattr(llm, "model", ""))
    return {**answer, "ms": ms}
