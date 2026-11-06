"""퀴즈 풀이 API 시험. 가짜 LLM(늘 1번 보기, 단답은 '가짜답')과 가짜 모델로 돌려 실제 AI를 부르지 않는다."""
import base64
import io

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


def jpeg(size=(400, 300), color=(200, 40, 40)):
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG")
    return out.getvalue()


def test_hidden_question_with_photo():
    photo = base64.b64encode(jpeg()).decode()
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


def test_not_an_image_photo_is_400():
    photo = base64.b64encode(b"not a picture").decode()
    r = solve(question={"id": 5, "type": "choice", "hidden": True, "choice_count": 4}, photo=photo)
    assert r.status_code == 400


def test_enhance_photo_shrinks_keeps_color_and_raises_contrast():
    from PIL import Image, ImageDraw

    big = Image.new("RGB", (4000, 3000), (120, 120, 120))
    ImageDraw.Draw(big).rectangle([100, 100, 900, 900], fill=(150, 60, 60))  # 흐린 빨강
    raw = io.BytesIO()
    big.save(raw, "JPEG")
    out = Image.open(io.BytesIO(quiz.enhance_photo(raw.getvalue())))
    assert max(out.size) == quiz.PHOTO_LONG_EDGE
    r, g, b = out.getpixel((100, 100))  # 빨강 영역(1600/4000 비율로 줄어든 자리)
    assert r > g + 60  # 흑백으로 바꾸지 않고 색을 살린다
    lo, hi = out.convert("L").getextrema()
    assert hi - lo > 100  # 대비가 펴졌다


def test_read_is_returned_for_screen_check():
    class ReadLLM:
        model = "t"

        def complete_vision_json(self, system, user, image=None, model=None):
            return {"read": {"question": "1+1은?", "choices": ["1", "2"]}, "think": "1+1=2", "choice": 2, "reason": "2"}

    answer = quiz.solve(ReadLLM(), {"id": "t1", "type": "choice", "hidden": True, "choice_count": 2}, image=(b"x", "image/jpeg"))
    assert answer["choice"] == 1
    assert answer["read"] == {"question": "1+1은?", "choices": ["1", "2"]}


def test_answers_are_not_shown_to_model():
    seen = []

    class ShortLLM:
        model = "t"

        def complete_vision_json(self, system, user, image=None, model=None):
            seen.append(user)
            return {"text": "x", "reason": "r"}

    quiz.solve(ShortLLM(), {"id": "s1", "type": "short", "text": "?", "answers": ["200"], "answer": 3})
    assert "200" not in seen[0] and '"answer"' not in seen[0]


def test_easy_questions_use_short_instructions_without_copying():
    seen = []

    class SysLLM:
        model = "t"

        def complete_vision_json(self, system, user, image=None, model=None):
            seen.append(system)
            return {"choice": 1, "reason": "r"}

    quiz.solve(SysLLM(), {"id": "t1", "type": "choice", "text": "?", "choices": ["a", "b"]})
    quiz.solve(SysLLM(), {"id": "t1", "type": "choice", "hidden": True, "choice_count": 2}, image=(b"x", "image/jpeg"))
    assert seen[0] == quiz.SYSTEM_TEXT and '"read"' not in seen[0]
    assert seen[1] == quiz.SYSTEM and '"read"' in seen[1]


def test_image_questions_get_step_by_step_counting():
    seen = []

    class PromptLLM:
        model = "t"

        def complete_vision_json(self, system, user, image=None, model=None):
            seen.append(user)
            return {"text": "7", "reason": "r"}

    easy = {"id": "i4", "type": "short", "mode": "image", "text": "빨간색 원은 몇 개?", "image_url": "/x"}
    hard = {"id": "i4", "type": "short", "mode": "image", "hidden": True}
    text_only = {"id": "s1", "type": "short", "text": "상태 코드?"}
    quiz.solve(PromptLLM(), easy, image=(b"x", "image/png"))
    quiz.solve(PromptLLM(), hard, image=(b"x", "image/jpeg"))
    quiz.solve(PromptLLM(), text_only)
    assert quiz.IMAGE_STEPS in seen[0] and quiz.IMAGE_STEPS in seen[1]
    assert quiz.IMAGE_STEPS not in seen[2]  # 글 문제는 짧게 푼다


def test_solve_response_has_trace_id_key():
    r = solve(question={"id": "s1", "type": "short", "text": "?"})
    assert r.status_code == 200 and "trace_id" in r.json()


def test_score_only_my_own_quiz_trace(monkeypatch):
    sent = []
    monkeypatch.setattr("app.tracing.score", lambda *a, **k: sent.append((a, k)))
    app.dependency_overrides[current_user] = lambda: User("quiz-owner", anonymous=True)
    quiz.remember_trace("trace-abc", "quiz-owner")
    ok = client.post("/api/quiz/score", json={"trace_id": "trace-abc", "correct": True, "note": "내 답 2번 · 정답 2번"})
    assert ok.status_code == 200
    assert sent[0][0][:3] == ("trace-abc", "quiz-correct", 1)
    # 남의 트레이스, 모르는 트레이스에는 붙이지 않는다
    app.dependency_overrides[current_user] = lambda: User("someone-else", anonymous=True)
    assert client.post("/api/quiz/score", json={"trace_id": "trace-abc", "correct": False}).status_code == 404
    assert client.post("/api/quiz/score", json={"trace_id": "nope", "correct": False}).status_code == 404
    assert len(sent) == 1


def test_photo_goes_to_langfuse_as_media_not_text(monkeypatch):
    from contextlib import contextmanager

    from langfuse.media import LangfuseMedia

    seen = []

    class Step:
        def update(self, **kwargs):
            pass

    @contextmanager
    def fake_observation(name, as_type="span", **fields):
        seen.append((name, as_type, fields))
        yield Step()

    monkeypatch.setattr("app.tracing.observation", fake_observation)
    quiz.solve(RecordingLLM(), {"id": "t1", "type": "choice", "hidden": True, "choice_count": 3}, image=(b"jpg", "image/jpeg"))
    name, as_type, fields = seen[0]
    assert (name, as_type) == ("solve", "generation")
    assert isinstance(fields["input"]["image"], LangfuseMedia)  # 긴 base64 글자가 가리기(mask)에 망가지지 않게 첨부로
    assert "prompt" in fields["input"] and "system" in fields["input"]
