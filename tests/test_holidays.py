"""공휴일 도구의 연휴 계산과 기간 밖 첫 공휴일. 인터넷 대신 고정한 공휴일 목록을 쓴다."""
from datetime import date, datetime, timezone

import pytest

from app import tools

HOLIDAYS = {
    2026: [(date(2026, 10, 3), "개천절"), (date(2026, 10, 9), "한글날"), (date(2026, 12, 25), "크리스마스")],
    2027: [(date(2027, 1, 1), "새해"), (date(2027, 2, 6), "설날"), (date(2027, 2, 8), "설날"), (date(2027, 2, 9), "설날"),
           (date(2027, 5, 5), "어린이날")],
}


@pytest.fixture(autouse=True)
def fixed_holidays(monkeypatch):
    monkeypatch.setattr(tools, "_holiday_cache", {y: list(v) for y, v in HOLIDAYS.items()})
    monkeypatch.setattr(tools.when, "now_in", lambda tz: datetime(2026, 11, 2, 14, tzinfo=timezone.utc))


def run(**args):
    return tools.get_holidays(args, tools.Context(None, "u1", "서울", "Asia/Seoul"))


def test_friday_holiday_joins_weekend():
    r = run(**{"from": "2026-12-01", "to": "2026-12-31"})
    assert "12월 25일(금)~27일(일) 사흘" in r.summary
    assert r.data["breaks"] == [{"from": "2026-12-25", "to": "2026-12-27", "days": 3}]
    assert r.data["holidays"] == [{"date": "2026-12-25", "name": "크리스마스"}]   # 화면 효과가 쓰는 칸은 그대로


def test_break_crosses_month_and_merges_holidays():
    r = run(**{"from": "2027-02-01", "to": "2027-02-28"})
    assert "2월 6일(토)~9일(화) 나흘" in r.summary          # 토 설날, 일, 월 설날, 화 설날이 하나로
    assert len(r.data["breaks"]) == 1


def test_saturday_holiday_is_not_a_long_break():
    r = run(**{"from": "2026-10-01", "to": "2026-10-05"})
    assert r.data["breaks"] == [] and "연휴" not in r.summary    # 10/3(토)~4(일)은 이틀이라 연휴로 치지 않는다
    r = run(**{"from": "2026-10-09", "to": "2026-10-09"})
    assert "10월 9일(금)~11일(일) 사흘" in r.summary


def test_no_holiday_gives_next_one_with_break():
    r = run(**{"from": "2026-11-02", "to": "2026-11-30"})
    assert r.data["holidays"] == []
    assert "그 뒤 첫 공휴일: 12월 25일(금) 크리스마스, 연휴 12월 25일(금)~27일(일) 사흘." in r.summary
    assert r.data["next"] == {"date": "2026-12-25", "name": "크리스마스"}


def test_next_holiday_without_break():
    r = run(**{"from": "2027-05-01", "to": "2027-05-04"})
    assert r.summary.endswith("그 뒤 첫 공휴일: 5월 5일(수) 어린이날.")
