import json

from fastapi.testclient import TestClient

from app.effects import TOPICS, find_topic, pick, prop, scene
from app.main import app
from app.tools import ToolResult

client = TestClient(app)


def result(name, data, ok=True):
    return ToolResult(name, ok, "", data)


def effects_of(message):
    r = client.post("/api/chat", json={"message": message})
    assert r.status_code == 200
    return [json.loads(line) for line in r.text.splitlines() if '"effect"' in line]


# ── 도구 결과로 정하는 배경 ──

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


# ── 단어 사전 ──

def test_dictionary_has_20_to_30_topics():
    assert 20 <= len(TOPICS) <= 30


def test_question_keyword_sets_scene_when_no_tool():
    assert scene("크리스마스가 언제야?", [])["name"] == "christmas"
    assert scene("요즘 고양이 키우고 싶어", [])["name"] == "cat"
    assert scene("오늘 회사에서 일했어", []) is None


def test_tool_result_wins_over_question_keyword():
    assert scene("크리스마스에 비 와?", [result("get_weather", {"weather": "눈"})])["name"] == "snow"


def test_first_keyword_in_text_wins():
    assert find_topic("커피 마시고 영화 봤어") == "coffee"
    assert find_topic("영화 보고 커피 마셨어") == "movie"


def test_hidden_short_words_do_not_match():
    for text in ["특별한 날이야", "기술 면담 있어", "잠깐만 기다려"]:
        assert find_topic(text) is None, text


def test_prop_uses_reply_keyword_then_falls_back_to_scene():
    assert prop("12월 25일이야! 트리 꾸밀 거야?", "christmas") == {"name": "christmas"}
    assert prop("케이크 사 갈게", "christmas") == {"name": "birthday"}
    assert prop("그렇구나", "cat") == {"name": "cat"}
    assert prop("그렇구나", None) is None


# ── 대화 흐름 ──

def test_reminder_request_sends_scene_and_prop():
    got = effects_of("내일 아침 9시에 스트레칭하라고 알려 줘")
    assert got[0] == {"type": "effect", "layer": "scene", "name": "alarm", "level": 0.7}
    assert got[-1]["layer"] == "prop"


def test_keyword_question_without_tool_sends_effects():
    got = effects_of("나 요즘 커피 너무 많이 마셔")
    assert [(e["layer"], e["name"]) for e in got] == [("scene", "coffee"), ("prop", "coffee")]


def test_crisis_message_sends_no_effect():
    assert effects_of("요즘 너무 죽고 싶어") == []
