"""퀴즈 풀이 API 시험. 가짜 LLM(늘 1번 보기, 단답은 '가짜답')과 가짜 모델로 돌려 실제 AI를 부르지 않는다."""
import base64

import httpx
import pytest
from fastapi.testclient import TestClient

from app import quiz
from app.auth import User
from app.main import app, current_user

client = TestClient(app)
SERVER = "https://quiz-battle-practice-140312194135.us-central1.run.app"


@pytest.fixture(autouse=True)
def fresh_rate_limit():
    quiz._calls.clear()
    yield
    quiz._calls.clear()
    app.dependency_overrides.pop(current_user, None)


def solve(**body):
    return client.post("/api/quiz/solve", json=body)


def test_easy_choice_returns_zero_based_choice():
    q = {"id": 3, "type": "choice", "text": "1+1은?", "choices": ["2", "3", "4"]}
    r = solve(question=q)
    assert r.status_code == 200
    body = r.json()
    assert body["choice"] == 0  # 모델은 1번을 골랐고, 퀴즈 서버 형식(0부터)으로 바뀐다
    assert body["reason"]
    assert "ms" in body


def test_easy_short_returns_text():
    r = solve(question={"id": 4, "type": "short", "text": "대한민국의 수도는?"})
    assert r.status_code == 200
    assert r.json()["text"] == "가짜답"


def test_hidden_question_needs_photo():
    r = solve(question={"id": 5, "type": "choice", "hidden": True, "choice_count": 4})
    assert r.status_code == 400


def test_hidden_question_with_photo():
    photo = base64.b64encode(b"\xff\xd8\xff fake jpeg").decode()
    r = solve(question={"id": 5, "type": "choice", "hidden": True, "choice_count": 4}, photo=photo)
    assert r.status_code == 200
    assert r.json()["choice"] == 0


def test_image_question_from_unknown_host_is_refused():
    q = {"id": 6, "type": "choice", "mode": "image", "image_url": "/img/1.png", "choices": ["a", "b"]}
    assert solve(question=q, server_url="https://evil.example.com").status_code == 400
    # 서버 안쪽 주소를 엿보는 요청도 막는다
    assert solve(question=q, server_url="http://metadata.google.internal").status_code == 400


def test_image_url_must_be_a_path_on_the_quiz_server():
    with pytest.raises(quiz.QuizError):
        quiz.fetch_image(SERVER, "//evil.example.com/a.png")
    with pytest.raises(quiz.QuizError):
        quiz.fetch_image(SERVER, "https://evil.example.com/a.png")


def test_fetch_image_from_allowed_host():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=b"PNGDATA", headers={"content-type": "image/png"})

    data, mime = quiz.fetch_image(SERVER, "/img/1.png", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert (data, mime) == (b"PNGDATA", "image/png")
    assert seen == [SERVER + "/img/1.png"]


def test_rate_limit_per_user():
    app.dependency_overrides[current_user] = lambda: User("quiz-rate-user", anonymous=True)
    q = {"id": 1, "type": "short", "text": "?"}
    for _ in range(quiz.PER_MINUTE):
        assert solve(question=q).status_code == 200
    assert solve(question=q).status_code == 429


class OutOfRangeLLM:
    model = "test"

    def complete_vision_json(self, system, user, image=None, model=None):
        return {"choice": 9, "reason": "?"}


def test_out_of_range_choice_is_an_error():
    with pytest.raises(quiz.QuizError) as error:
        quiz.solve(OutOfRangeLLM(), {"id": 1, "type": "choice", "choices": ["a", "b"]})
    assert error.value.status == 502


class RecordingLLM:
    model = "chat-model"

    def __init__(self):
        self.calls = []

    def complete_vision_json(self, system, user, image=None, model=None):
        self.calls.append({"user": user, "image": image, "model": model})
        return {"choice": "2", "reason": "두 번째"}


def test_prompt_numbers_choices_from_one_and_passes_image_and_model():
    llm = RecordingLLM()
    q = {"id": 1, "type": "choice", "text": "고르세요", "choices": ["가", "나", "다"], "image_url": "/x.png"}
    answer = quiz.solve(llm, q, image=(b"img", "image/png"), model="quiz-model")
    assert answer["choice"] == 1
    call = llm.calls[0]
    assert "1. 가" in call["user"] and "3. 다" in call["user"]
    assert "/x.png" not in call["user"]  # 그림 주소는 모델에게 넘기지 않는다
    assert call["image"] == (b"img", "image/png") and call["model"] == "quiz-model"


def test_log_has_no_question_text(capsys):
    quiz.solve(RecordingLLM(), {"id": 1, "type": "choice", "text": "비밀문제글", "choices": ["가", "나"]})
    out = capsys.readouterr().out
    assert "비밀문제글" not in out and "퀴즈 풀이" in out


def test_quiz_model_setting(monkeypatch):
    monkeypatch.delenv("QUIZ_MODEL", raising=False)
    assert quiz.quiz_model("gpt-4o-mini") == "gpt-4o-mini"
    monkeypatch.setenv("QUIZ_MODEL", "gpt-4.1-mini")
    assert quiz.quiz_model("gpt-4o-mini") == "gpt-4.1-mini"
