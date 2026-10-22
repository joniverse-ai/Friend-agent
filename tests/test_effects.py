import json

from fastapi.testclient import TestClient

from app.effects import pick
from app.main import app
from app.tools import ToolResult

client = TestClient(app)


def result(name, data, ok=True):
    return ToolResult(name, ok, "", data)


def events(message):
    r = client.post("/api/chat", json={"message": message})
    assert r.status_code == 200
    return [json.loads(line) for line in r.text.splitlines()]


def test_weather_picks_matching_effect():
    assert pick([result("get_weather", {"weather": "비"})])["name"] == "rain"
    assert pick([result("get_weather", {"weather": "눈"})])["name"] == "snow"
    assert pick([result("get_weather", {"weather": "맑음"})])["name"] == "sun"
    assert pick([result("get_weather", {"weather": "뇌우"})])["name"] == "storm"


def test_air_quality_uses_the_worse_grade():
    good = pick([result("get_air_quality", {"pm10_grade": "좋음", "pm2_5_grade": "좋음"})])
    bad = pick([result("get_air_quality", {"pm10_grade": "좋음", "pm2_5_grade": "나쁨"})])
    assert good["name"] == "fresh"
    assert bad["name"] == "dust" and bad["level"] > pick([result("get_air_quality", {"pm10_grade": "보통"})])["level"]


def test_holidays_only_when_found():
    assert pick([result("get_holidays", {"holidays": [{"date": "2026-12-25", "name": "기독탄신일"}]})])["name"] == "confetti"
    assert pick([result("get_holidays", {"holidays": []})]) is None


def test_failed_tool_has_no_effect():
    assert pick([result("get_weather", {"weather": "비"}, ok=False)]) is None


def test_reminder_request_sends_alarm_effect():
    effect = [e for e in events("내일 아침 9시에 스트레칭하라고 알려 줘") if e["type"] == "effect"]
    assert effect == [{"type": "effect", "name": "alarm", "level": 0.7}]


def test_crisis_message_sends_no_effect():
    assert not [e for e in events("요즘 너무 죽고 싶어") if e["type"] == "effect"]
