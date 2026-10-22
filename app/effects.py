"""질문에 맞는 화면 효과 고르기.

도구 결과를 보고 대화창에 잠깐 보여 줄 애니메이션 하나를 정한다.
화면에는 효과 이름과 세기(0~1)만 보낸다. 대화 원문이나 도구 원본 데이터는 담지 않는다.
"""

WEATHER = {
    "맑음": ("sun", 0.7), "구름 조금": ("cloud", 0.6), "안개": ("fog", 0.6),
    "이슬비": ("rain", 0.35), "비": ("rain", 0.7), "소나기": ("rain", 0.95),
    "눈": ("snow", 0.7), "눈 소나기": ("snow", 0.95), "뇌우": ("storm", 1.0),
}
AIR_GRADES = ["좋음", "보통", "나쁨", "매우 나쁨"]
AIR_LEVEL = {"보통": 0.35, "나쁨": 0.7, "매우 나쁨": 1.0}


def pick(results):
    """성공한 도구 결과 중 처음으로 효과가 정해지는 것을 고른다. 없으면 None."""
    for r in results:
        if not r.ok:
            continue
        d = r.data
        if r.name == "get_weather" and d.get("weather") in WEATHER:
            name, level = WEATHER[d["weather"]]
            return {"name": name, "level": level}
        if r.name == "get_air_quality":
            grades = [g for g in (d.get("pm10_grade"), d.get("pm2_5_grade")) if g in AIR_GRADES]
            if grades:
                worst = max(grades, key=AIR_GRADES.index)
                return {"name": "fresh", "level": 0.6} if worst == "좋음" else {"name": "dust", "level": AIR_LEVEL[worst]}
        if r.name == "get_holidays" and d.get("holidays"):
            return {"name": "confetti", "level": 0.8}
        if r.name == "create_reminder":
            return {"name": "alarm", "level": 0.7}
    return None
