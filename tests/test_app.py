import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app, store

client = TestClient(app)


def chat(message):
    r = client.post("/api/chat", json={"message": message})
    assert r.status_code == 200
    return [json.loads(line) for line in r.text.splitlines()]


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_weather_question_picks_weather_tool():
    turn = next(e for e in chat("내일 서울 날씨 어때?") if e["type"] == "turn")
    assert [t["name"] for t in turn["tools"]] == ["get_weather"]


def test_crisis_message_gets_fixed_safety_reply():
    events = chat("요즘 너무 죽고 싶어")
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "109" in text


def test_reminder_is_saved_with_timezone():
    chat("내일 아침 8시에 물 마시라고 알려 줘")
    pending = store.list_reminders("local-user", status="pending")
    assert pending and pending[-1]["due_at"].tzinfo is not None


def test_due_reminder_is_delivered_only_once():
    due = datetime.now(timezone.utc) - timedelta(minutes=1)
    store.add_reminder("local-user", "테스트 약속", due)
    now = datetime.now(timezone.utc)
    first = store.claim_due_reminders("local-user", now)
    second = store.claim_due_reminders("local-user", now)
    assert [r["text"] for r in first].count("테스트 약속") == 1
    assert "테스트 약속" not in [r["text"] for r in second]


def test_ocr_is_off_by_default():
    r = client.post("/api/ocr", files={"photo": ("a.png", b"x", "image/png")})
    assert r.status_code == 404


def test_pdf_is_off_by_default():
    r = client.post("/api/pdf", files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 404


def answer_id(events):
    return next(e for e in events if e["type"] == "done")["message_id"]


def test_feedback_and_report_on_my_answer_are_ok():
    # Langfuse 키가 없는 테스트 환경에서는 점수 보내기를 건너뛰고 오류 없이 응답한다
    mid = answer_id(chat("오늘 좀 피곤했어"))
    assert client.post("/api/feedback", json={"message_id": mid, "liked": False}).status_code == 200
    assert client.post("/api/report", json={"message_id": mid, "reason": "이상해요"}).status_code == 200


def test_feedback_on_someone_elses_message_is_rejected():
    other = store.add_message("someone-else", "assistant", "남의 답")
    assert client.post("/api/feedback", json={"message_id": other["id"], "liked": True}).status_code == 404
    assert client.post("/api/report", json={"message_id": other["id"], "reason": ""}).status_code == 404


def test_feedback_on_my_own_question_is_rejected():
    chat("안녕")
    mine = next(m for m in reversed(store.recent_messages("local-user", 10)) if m["role"] == "user")
    assert client.post("/api/feedback", json={"message_id": mine["id"], "liked": True}).status_code == 404


def test_same_reminder_request_twice_saves_one():
    # 같은 부탁을 두 번 해도 약속은 하나만 남는다(중복 저장 막기)
    for _ in range(2):
        chat("모레 저녁에 화분 물 주라고 알려 줘")
    pending = [r for r in store.list_reminders("local-user", status="pending") if r["text"] == "화분 물 주기"]
    assert len(pending) == 1


class AskingModel:
    """도구 선택 모델 흉내: 약속을 묻는 질문에도 create_reminder 를 고른다(staging 에서 실제로 본 실수)."""
    can_reason = True

    def complete_json(self, system, user):
        return {"tools": [{"name": "create_reminder", "args": {"text": "친구 만나기", "due_at": "2026-12-01T09:00:00+09:00"}},
                          {"name": "list_reminders", "args": {}}], "confidence": 0.8, "reason": "약속 이야기"}


def test_question_about_reminder_does_not_save_reminder():
    from app.router import choose
    from app import when
    now = when.now_in("Asia/Seoul")
    for message in ["나 내일 약속이 어디에서였지?", "이해 못 했다면서. 왜 기억하고 그래..", "나 다음 주 토요일에 이사 가"]:
        result = choose(message, [], now, "서울", AskingModel())
        assert [t["name"] for t in result.tools] == ["list_reminders"], message
    asked = choose("모레 9시에 친구 만나는 거 알려 줘", [], now, "서울", AskingModel())
    assert "create_reminder" in [t["name"] for t in asked.tools]
