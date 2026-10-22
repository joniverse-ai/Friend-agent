"""질문과 답에 맞는 화면 효과 고르기.

효과는 두 겹이다.
- 배경 장면(scene): 질문을 보내자마자. 도구 결과로 정해지면 그걸 쓰고, 아니면 질문 속 핵심 단어로 고른다.
- 포인트 소품(prop): 답이 다 온 뒤. 답 속 핵심 단어로 고르고, 없으면 배경과 같은 주제를 쓴다.
핵심 단어는 글에서 가장 먼저 나오는 사전 단어 하나다(같은 자리면 더 긴 단어).
화면에는 주제 이름과 세기(0~1)만 보낸다. 대화 원문이나 도구 원본 데이터는 담지 않는다.
"""

# ── 도구 결과로 정하는 배경 ────────────────────────────────────────

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


# ── 단어 사전 ────────────────────────────────────────────────────
# 짧은 글자는 다른 단어 안에 숨어 잘못 잡히기 쉽다("특별"의 별, "기술"의 술, "잠깐"의 잠).
# 그래서 두 글자 이상이거나 뜻이 분명한 표현만 넣는다.

TOPICS = {
    # 날씨와 생활 (도구 없이도 말에 나오면 잡는다)
    "sun": ["맑아", "맑은 날", "햇살", "화창", "햇빛"],
    "cloud": ["흐려", "흐린 날", "구름"],
    "fog": ["안개"],
    "rain": ["비가", "비 와", "비와", "비 온", "우산", "장마", "소나기", "빗소리"],
    "snow": ["눈이 와", "눈 와", "눈이 내", "눈사람", "첫눈", "함박눈", "눈싸움"],
    "storm": ["천둥", "번개", "태풍"],
    "dust": ["미세먼지", "황사", "먼지가"],
    "fresh": ["공기 좋", "상쾌", "새싹"],
    "confetti": ["공휴일", "연휴", "빨간 날", "빨간날", "축제"],
    "alarm": ["알려 줘", "알려줘", "알람", "깨워", "리마인드"],
    # 이야기 주제
    "christmas": ["크리스마스", "성탄", "산타", "트리", "캐럴", "루돌프"],
    "birthday": ["생일", "생신", "케이크", "축하해", "축하합니다"],
    "love": ["사랑", "좋아해", "설레", "데이트", "연애", "고백", "하트"],
    "coffee": ["커피", "카페", "라떼", "아메리카노", "에스프레소", "바리스타"],
    "cat": ["고양이", "냥이", "야옹", "츄르", "집사"],
    "dog": ["강아지", "멍멍", "댕댕", "반려견", "산책시키"],
    "food": ["배고파", "배고프", "맛집", "점심", "저녁 뭐", "야식", "밥 먹", "라면", "치킨", "피자", "맛있"],
    "sleep": ["졸려", "졸리", "피곤", "잠이", "잠을", "자야", "꿀잠", "불면", "잘 자", "굿나잇"],
    "study": ["시험", "공부", "면접", "합격", "자격증", "과제", "발표"],
    "cheer": ["화이팅", "파이팅", "힘내", "응원", "잘될 거", "잘 될 거"],
    "travel": ["여행", "비행기", "휴가", "공항", "바다", "해외"],
    "music": ["노래", "음악", "재즈", "콘서트", "플레이리스트", "가수"],
    "flower": ["벚꽃", "꽃이", "꽃을", "꽃구경", "봄이", "봄에", "봄날", "튤립"],
    "autumn": ["가을", "단풍", "낙엽"],
    "money": ["월급", "용돈", "적금", "보너스", "부자", "돈이", "돈을"],
    "exercise": ["운동", "헬스", "달리기", "러닝", "요가", "등산", "산책"],
    "comfort": ["슬퍼", "슬프", "우울", "속상", "외로", "힘들", "지쳤", "지쳐"],
    "movie": ["영화", "넷플릭스", "드라마", "팝콘", "극장"],
    "party": ["파티", "맥주", "소주", "와인", "회식", "건배"],
}


def find_topic(text):
    """글에서 가장 먼저 나오는 사전 단어의 주제. 같은 자리면 더 긴 단어. 없으면 None."""
    best = None
    for name, words in TOPICS.items():
        for word in words:
            i = (text or "").find(word)
            if i >= 0 and (best is None or (i, -len(word)) < (best[0], -best[1])):
                best = (i, len(word), name)
    return best[2] if best else None


def scene(message, results):
    """배경 장면: 도구 결과가 우선, 없으면 질문 속 핵심 단어."""
    by_tool = pick(results)
    if by_tool:
        return by_tool
    topic = find_topic(message)
    return {"name": topic, "level": 0.6} if topic else None


def prop(reply, scene_name=None):
    """포인트 소품: 답 속 핵심 단어, 없으면 배경과 같은 주제."""
    topic = find_topic(reply) or scene_name
    return {"name": topic} if topic else None
