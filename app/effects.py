"""질문과 답에 맞는 화면 효과 고르기.

효과는 두 겹이다.
- 배경 장면(scene): 질문이 오면 도구 선택과 동시에 효과 전용 AI 호출을 보낸다(start_scene).
  도구 결과(실제 날씨 등)가 있으면 그게 우선이고, 없으면 AI가 질문의 뜻을 보고 고른 주제를 쓴다.
- 포인트 소품(prop): 답이 끝나면 기억 정리와 동시에 효과 전용 AI 호출을 보낸다(start_prop).
AI는 TOPIC_DESC 목록 안에서만 고르고, 대화와 뚜렷하게 관련된 게 없으면 "none"(효과 없음)을 고른다.
도구 선택과 기억 정리 지시문에 얹지 않고 따로 부르는 이유: 얹었더니 도구 선택 정확도가 떨어졌다(실측).
AI가 없는 fake 모드에서는 단어 사전(TOPICS)이 대신한다.
화면에는 주제 이름과 세기(0~1)만 보낸다. 대화 원문이나 도구 원본 데이터는 담지 않는다.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from app.logs import log

AI_TIMEOUT = 5.0   # 효과 호출이 이 시간(초) 안에 답하지 않으면 효과 없이 넘어간다
_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="effects")

# AI에게 보여 주는 주제 목록. 화면(static/index.html)의 효과 이름과 같아야 한다.
TOPIC_DESC = {
    "sun": "맑은 날, 햇살", "cloud": "흐린 날, 구름", "fog": "안개", "rain": "비, 우산, 장마", "snow": "눈 오는 날, 겨울",
    "storm": "천둥, 번개, 태풍", "dust": "미세먼지, 황사, 탁한 공기", "fresh": "상쾌함, 맑은 공기, 새싹",
    "confetti": "공휴일, 연휴, 축제, 신나는 소식", "alarm": "알림, 약속, 일어나기",
    "christmas": "크리스마스, 연말, 산타", "birthday": "생일, 기념일 축하", "love": "사랑, 설렘, 연애, 고마움",
    "comfort": "슬픔, 지침, 외로움을 달래는 위로", "coffee": "커피, 카페", "cat": "고양이", "dog": "강아지, 반려견",
    "food": "음식, 식사, 배고픔, 맛집", "sleep": "졸림, 피곤, 잠, 밤", "study": "공부, 시험, 면접, 일 준비",
    "cheer": "응원, 격려, 해냈다는 기쁨", "travel": "여행, 휴가, 비행기, 바다", "music": "음악, 노래, 공연",
    "flower": "꽃, 봄", "autumn": "가을, 단풍, 낙엽", "money": "돈, 월급, 저축", "exercise": "운동, 산책, 건강",
    "movie": "영화, 드라마, 영상", "party": "파티, 회식, 술자리, 축배",
}


def topic_menu():
    """AI 지시문에 넣는 주제 목록 한 줄씩."""
    return "\n".join(f"- {name}: {desc}" for name, desc in TOPIC_DESC.items())


def ai_topic(value):
    """AI가 고른 값을 주제 이름으로. 목록에 없거나 none 이면 "none"."""
    value = str(value or "").strip().lower()
    return value if value in TOPIC_DESC else "none"


# ── 효과 전용 AI 호출 ──────────────────────────────────────────────

RULES = """- 단어가 겹치기만 하고 뜻이 다르면 고르지 않는다. 예: '특별한 날'은 별이 아니고, '기술 면담'은 술자리가 아니다.
- 맞는 게 없으면 "none". 짧은 인사나 평범한 말은 대부분 none 이다.
- 위기나 아주 무거운 이야기에는 none 또는 comfort 만 고른다."""


def scene_prompt():
    return ("너는 AI 친구 채팅 화면에 잠깐 보여 줄 배경 효과를 고르는 담당이다.\n"
            "사용자 메시지의 주제나 분위기와 뚜렷하게 맞는 효과 하나를 아래 목록의 이름으로 고른다. 최근 대화는 흐름을 이해하는 데만 쓴다.\n"
            f"{RULES}\n목록:\n{topic_menu()}\n"
            '반드시 JSON 하나로만 답한다: {"scene": "주제 이름 또는 none"}')


def prop_prompt():
    return ("너는 AI 친구 채팅 화면에서 캐릭터의 답이 끝난 뒤 잠깐 보여 줄 소품을 고르는 담당이다.\n"
            "캐릭터 답의 핵심 내용과 뚜렷하게 맞는 소품 하나를 아래 목록의 이름으로 고른다. 사용자 메시지는 답을 이해하는 데만 쓴다.\n"
            f"{RULES}\n목록:\n{topic_menu()}\n"
            '반드시 JSON 하나로만 답한다: {"prop": "주제 이름 또는 none"}')


def _ask(llm, system, user, key):
    try:
        return ai_topic(llm.complete_json(system, user).get(key))
    except Exception as error:   # 효과는 장식이라, 실패하면 효과 없이 넘어간다
        log("WARNING", "화면 효과 선택 실패", step=key, error=type(error).__name__)
        return "none"


def start_scene(message, history, llm):
    """배경 고르기를 바로 시작한다(도구 선택과 동시에). 모델이 없으면 None."""
    if not getattr(llm, "can_reason", False):
        return None
    recent = "\n".join(f"{m['role']}: {m['content']}" for m in history[-4:]) or "(없음)"
    return _pool.submit(_ask, llm, scene_prompt(), f"[최근 대화]\n{recent}\n\n[메시지]\n{message}", "scene")


def start_prop(message, reply, llm):
    """소품 고르기를 바로 시작한다(기억 정리와 동시에). 모델이 없으면 None."""
    if not getattr(llm, "can_reason", False):
        return None
    return _pool.submit(_ask, llm, prop_prompt(), f"[사용자 메시지]\n{message}\n\n[캐릭터 답]\n{reply}", "prop")


def result(job, step):
    """시작해 둔 효과 호출의 결과. 모델이 없으면 None(단어 사전으로 대신), 시간이 넘으면 "none"."""
    if job is None:
        return None
    try:
        return job.result(timeout=AI_TIMEOUT)
    except FutureTimeout:
        log("WARNING", "화면 효과 선택 시간 초과", step=step)
        return "none"

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


# ── 단어 사전 (AI가 없는 fake 모드에서만 쓴다) ───────────────────────
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


GENERIC_TOOL_EFFECTS = ("confetti", "alarm")   # 공휴일·약속 도구의 일반 효과. AI가 더 구체적인 주제를 고르면 양보한다


def scene(message, results, ai=None):
    """배경 장면: 날씨·미세먼지 도구 결과(실제 데이터)가 항상 우선.
    공휴일·약속 도구의 일반 효과는 AI가 주제를 골랐으면 양보한다(예: 크리스마스 질문 → 꽃가루 대신 크리스마스).
    도구 효과가 없으면 AI가 고른 주제(ai), AI가 없으면(None) 질문 속 핵심 단어."""
    by_tool = pick(results)
    if by_tool and not (by_tool["name"] in GENERIC_TOOL_EFFECTS and ai_topic(ai) != "none"):
        return by_tool
    topic = find_topic(message) if ai is None else ai_topic(ai)
    return {"name": topic, "level": 0.6} if topic and topic != "none" else None


def prop(reply, scene_name=None, ai=None):
    """포인트 소품: AI가 고른 주제(ai). AI가 없으면(None) 답 속 핵심 단어, 그것도 없으면 배경과 같은 주제."""
    topic = (find_topic(reply) or scene_name) if ai is None else ai_topic(ai)
    return {"name": topic} if topic and topic != "none" else None
