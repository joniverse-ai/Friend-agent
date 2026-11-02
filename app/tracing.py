"""Langfuse 트레이스를 한곳에 모아 둔다.

한 턴이 어떤 단계(안전장치, 도구 선택, 도구 실행, 답 생성, 기억 정리)를 거쳤고,
단계마다 시간과 토큰이 얼마나 들었는지를 Langfuse 에 남긴다. 이름은 agent.py 에서 붙인다.

- 키(LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY)가 둘 다 있을 때만 켜진다. 없으면 아무것도 보내지 않고 앱은 그대로 돈다.
- 보내기 전에 전화번호, 이메일, 카드 번호를 가린다(mask). 대화 원문은 Langfuse 에만 남고, 서버 로그에는 여전히 남기지 않는다.
- 근거 문서: https://langfuse.com/docs/observability/sdk/python/overview (SDK 5.1.0)
- 사용자·세션·버전 속성: https://langfuse.com/docs/observability/sdk/python/instrumentation (propagate_attributes)
  한 번 정하면 그 안에서 만드는 모든 단계에 함께 붙는다. 환경(environment)은 클라이언트를 만들 때 한 번 정한다.
"""
import os
import re

from langfuse import Langfuse, get_client, observe, propagate_attributes

__all__ = ["setup", "observation", "update_generation", "update_span", "flush", "observe",
           "user_attributes", "tag_turn"]

# 카드 번호를 전화번호보다 먼저 가린다(16자리 안에 전화번호 모양이 들어 있을 수 있어서).
# 한글은 글자(\w)로 쳐서 "010-1234-5678이야" 에서 \b 가 듣지 않는다. 그래서 숫자 앞뒤만 본다.
MASKS = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[이메일]"),
    (re.compile(r"(?<!\d)\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}(?!\d)"), "[카드번호]"),
    (re.compile(r"(?<!\d)0\d{1,2}[- ]?\d{3,4}[- ]?\d{4}(?!\d)"), "[전화번호]"),
]


def mask_text(text):
    for pattern, label in MASKS:
        text = pattern.sub(label, text)
    return text


def mask(*, data, **kwargs):
    """Langfuse 로 보내는 입력·출력·메타데이터 안의 글을 모두 훑어 가린다."""
    if isinstance(data, str):
        return mask_text(data)
    if isinstance(data, dict):
        return {key: mask(data=value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [mask(data=value) for value in data]
    return data


def enabled():
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip() and os.environ.get("LANGFUSE_SECRET_KEY", "").strip())


def setup():
    """.env 를 읽은 뒤(main.py) 한 번 부른다. 이후 observe 와 get_client 는 이 클라이언트를 쓴다."""
    return Langfuse(tracing_enabled=enabled(), mask=mask,
                    environment=os.environ.get("ENV", "local").strip() or "local",   # local, staging, production
                    release=os.environ.get("COMMIT_SHA", "local").strip() or "local")


def user_attributes(uid, now, tags=()):
    """with 문 안에서 만드는 트레이스에 사용자, 세션, 버전을 붙인다.

    - user_id: 로그인으로 확인한 사용자 ID. 이메일은 넣지 않는다.
    - session_id: 사용자 ID + 사용자 시간대의 날짜. 이 앱은 대화방이 따로 없어서 하루치 대화를 한 세션으로 본다.
    - version: Cloud Run 이 넣어 주는 리비전 이름(K_REVISION). 내 컴퓨터에서는 local.
    """
    return propagate_attributes(
        user_id=uid, session_id=f"{uid}-{now.date().isoformat()}",
        version=os.environ.get("K_REVISION", "local"),
        metadata={"commit": os.environ.get("COMMIT_SHA", "local")}, tags=list(tags) or None)


def tag_turn(tags, **metadata):
    """도구를 고른 뒤처럼 턴 중간에 알게 된 값을 지금 열린 트레이스에 tags 와 metadata 로 더한다."""
    with propagate_attributes(tags=list(tags) or None, metadata=metadata):
        pass  # 들어가는 순간 지금 열린 단계(chat-turn)에 붙는다. 이미 끝난 단계에는 붙지 않는다


def observation(name, as_type="span", **fields):
    """with 문으로 단계 하나를 연다. 안에서 연 단계와 AI 호출은 이 단계의 자식이 된다."""
    return get_client().start_as_current_observation(name=name, as_type=as_type, **fields)


def update_generation(**fields):
    """지금 열려 있는 generation 에 모델, 토큰 수, 출력 등을 적는다. 꺼져 있으면 아무것도 하지 않는다."""
    if enabled():
        get_client().update_current_generation(**fields)


def update_span(**fields):
    if enabled():
        get_client().update_current_span(**fields)


def flush():
    """쌓아 둔 트레이스를 바로 보낸다. Cloud Run 은 응답이 끝나면 CPU를 거의 주지 않아서 응답을 닫기 전에 보낸다."""
    if enabled():
        get_client().flush()
