"""메시지 하나를 처리하는 흐름(한 턴).

① 안전장치: 위기 표현이면 정해진 안전 응답으로 끝낸다(코드 규칙).
② 도구 선택: 모델이 도구를 0~2개 고른다. 확신이 낮으면 되묻는다(router.py).
③ 도구 실행: 날씨, 미세먼지, 공휴일, 약속(tools.py). 질문에 맞는 배경 효과를 보낸다(effects.py, ②와 동시에 고름).
④ 답 생성: 캐릭터 지시문 + 기억 + 도구 결과 + 최근 대화로 답을 스트리밍한다.
⑤ 기억 정리: 답을 다 보낸 뒤 같은 요청 안에서 기억 메모와 대화 요약을 갱신한다(memory.py).
   같은 때에 답에 맞는 소품 효과를 고른다(effects.py).
   Cloud Run 기본 설정(요청 기반 과금)에서는 응답이 끝난 뒤 CPU가 거의 주어지지 않아서,
   응답 뒤 백그라운드로 돌리면 기억 정리가 멈추거나 늦어진다. 그래서 응답을 닫기 전에 처리한다.

결과는 이벤트(dict)로 하나씩 내보낸다. show_panel 이 꺼져 있으면(production) 개발용 이벤트는 아예 만들지 않는다.

트레이스(tracing.py): 한 턴은 chat-turn 트레이스 하나이고, 단계마다 safety, choose-tools, tool:<이름>, reply,
memory-update, memory-summary 로 나눠 남긴다. AI를 부르는 단계는 generation 이라 토큰과 요금이 함께 남는다.
"""
from datetime import datetime, timedelta, timezone

from app import effects, memory, tracing, when
from app.logs import log
from app.persona import DEFAULT_CHARACTER, build_persona
from app.router import choose
from app.safety import CRISIS_REPLY, is_crisis
from app.tools import TOOLS, Context, run_tool

GREETING_GAP = timedelta(hours=6)  # 마지막 대화 뒤 이만큼 지나면 앱을 열 때 먼저 말을 건다


# 대화 턴의 도구 결과. 약속을 실제로 저장·취소하고도 앞 대화의 "기억할 수 없어"를 따라 "못 한다"고 답하던 문제를 막는다
# (Main Quest 08, 문제 후보 A: 약속 쓰기 13턴 중 5턴, 신고 2건. 근거와 실험은 REPORT.md, scripts/eval_reply_consistency.py)
# 실험 결과 세 가지를 함께 쓴다: 이 머리말, 결과 줄마다 성공/실패 표시, 사용자 말 바로 뒤(답 직전) 위치.
TOOL_RESULT_HEADER = (
    "[도구 결과]\n"
    "아래는 이번 턴에 실제로 실행한 결과다. 답은 반드시 이 결과와 맞게 말한다.\n"
    "성공한 일(약속 저장·취소 등)은 했다고 말한다. 앞의 대화에서 '할 수 없다'고 했더라도 지금 결과를 따른다.\n"
    "'실패:'로 표시된 일은 하지 못한 것이다. 했다고 말하지 말고, 못 했다는 것과 이유를 짧게 말한 뒤 어떻게 할지 묻는다."
)


def system_prompt(character, now, location, memories, summary, tool_lines=(), clarify=False):
    parts = [build_persona(character),
             f"[지금] {when.label(now)} ({now.tzinfo}), 사용자 지역: {location}\n[달력]\n{when.calendar_hint(now)}"]
    block = memory.memory_block(memories, summary)
    if block:
        parts.append(block)
    if tool_lines:  # 먼저 말 걸기(greeting)가 쓰는 정보 줄. 대화 턴의 도구 결과는 tool_result_message 로 따로 붙인다
        parts.append("[도구 결과]\n" + "\n".join(f"- {line}" for line in tool_lines))
    if clarify:
        parts.append("[되묻기]\n사용자가 무엇을 원하는지 확실하지 않다. 짧게 되물어서 확인한다.")
    return "\n\n".join(parts)


def tool_result_message(results):
    """이번 턴의 도구 결과를 답 직전에 붙일 system 메시지로 만든다. 도구를 안 썼으면 None.

    지시문 중간에 두면 바로 앞 대화의 "못 해"를 더 따라 해서, 사용자 말 뒤(답을 만들기 직전)에 둔다.
    """
    if not results:
        return None
    lines = "\n".join(f"- {'성공' if r.ok else '실패'}: {r.summary}" for r in results)
    return {"role": "system", "content": TOOL_RESULT_HEADER + "\n" + lines}


@tracing.observe(name="chat-turn", capture_input=False, capture_output=False)
def run_turn(message, uid, llm, store, show_panel):
    settings = store.get_settings(uid)
    now = when.now_in(settings["timezone"])
    # 사용자·세션·버전을 턴 시작에서 한 번 붙이면 안의 모든 단계에 함께 붙는다
    with tracing.user_attributes(uid, now):
        tracing.update_span(input=message)
        yield from _turn(message, uid, llm, store, show_panel, settings, now)


def _turn(message, uid, llm, store, show_panel, settings, now):
    character = store.get_character(uid) or DEFAULT_CHARACTER
    memories, summary, recent = memory.build_context(store, uid)
    history = [{"role": m["role"], "content": m["content"]} for m in recent]
    store.add_message(uid, "user", message)

    # ① 위기 표현
    with tracing.observation("safety", input=message) as step:
        crisis = is_crisis(message)
        step.update(output={"crisis": crisis})
    if crisis:
        guards = ["위기 표현이 있어 캐릭터 설정과 상관없이 정해진 안전 응답을 보냈습니다."]
        tracing.tag_turn(["safety"], safety=True)
        if show_panel:
            yield {"type": "turn", "tools": [], "confidence": None, "reason": "", "decided_by": "rule",
                   "action": "safety", "guards": guards}
        yield {"type": "delta", "text": CRISIS_REPLY}
        saved = store.add_message(uid, "assistant", CRISIS_REPLY, {"action": "safety", "guards": guards,
                                                                   "trace_id": tracing.current_trace_id()})
        log("WARNING", "위기 표현 안전 응답")
        tracing.update_span(output={"reply": CRISIS_REPLY, "message_id": saved["id"]})
        yield {"type": "done", "message_id": saved["id"]}
        return

    # ② 도구 선택. 화면 배경 효과 고르기(효과 전용 AI 호출)를 동시에 시작해 둔다.
    scene_job = effects.start_scene(message, history, llm)
    with tracing.observation("choose-tools", "generation", input=message) as step:
        choice = choose(message, history, now, settings["location"], llm, user_id=uid)
        step.update(output={"tools": choice.tools, "confidence": choice.confidence, "decided_by": choice.decided_by,
                            "action": choice.action, "reason": choice.reason})
    tool_names = [t["name"] for t in choice.tools]
    tracing.tag_turn(tool_names + (["clarify"] if choice.action == "clarify" else []),
                     tools=tool_names, confidence=choice.confidence, safety=False,
                     **({"ab_variant": choice.ab_variant} if choice.ab_variant else {}))
    if show_panel:
        yield {"type": "turn", "tools": [{"name": t["name"], "label": TOOLS[t["name"]]["label"], "args": t["args"]}
                                         for t in choice.tools],
               "confidence": choice.confidence, "reason": choice.reason, "decided_by": choice.decided_by,
               "action": choice.action, "guards": choice.guards}

    # ③ 도구 실행
    ctx = Context(store, uid, settings["location"], settings["timezone"])
    results = []
    for t in choice.tools:
        with tracing.observation(f"tool:{t['name']}", input=t["args"]) as step:
            r = run_tool(t["name"], t["args"], ctx)
            step.update(output={"ok": r.ok, "summary": r.summary, "data": r.data},
                        level="DEFAULT" if r.ok else "ERROR")
        results.append(r)
    for r in results:
        if show_panel:
            yield {"type": "tool", "name": r.name, "label": TOOLS[r.name]["label"], "ok": r.ok,
                   "summary": r.summary, "data": r.data}
        if r.ok and r.name in ("create_reminder", "cancel_reminder"):
            yield {"type": "reminder", "action": r.name, "summary": r.summary}
    # 화면 효과 ① 배경 장면: 도구 결과가 우선, 없으면 효과 전용 AI가 고른 주제(fake 모드는 단어 사전).
    # 개발용 패널과 상관없이 보내고, 주제 이름과 세기만 담는다.
    scene_ai = effects.result(scene_job, "scene")
    scene = effects.scene(message, results, scene_ai) if choice.action != "clarify" else None
    if scene:
        yield {"type": "effect", "layer": "scene", **scene}

    # ④ 답 생성
    system = system_prompt(character, now, settings["location"], memories, summary,
                           clarify=choice.action == "clarify")
    if show_panel:
        yield {"type": "context", "memories": [m["text"] for m in memories],
               "summary": summary["text"] if summary else None, "recent_messages": len(history)}
    reply = ""
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": message}]
    tool_message = tool_result_message(results)
    if tool_message:
        messages.append(tool_message)
    with tracing.observation("reply", "generation", input=messages) as step:
        for chunk in llm.chat_stream(messages):
            if not reply:  # 첫 글자가 나온 시각. 사용자가 기다린 시간은 여기까지다
                step.update(completion_start_time=datetime.now(timezone.utc))
            reply += chunk
            yield {"type": "delta", "text": chunk}
        step.update(output=reply)
    meta = {"tools": [t["name"] for t in choice.tools], "confidence": choice.confidence,
            "action": choice.action, "guards": choice.guards,
            "trace_id": tracing.current_trace_id()}   # 좋아요·별로예요·신고가 오면 이 트레이스에 점수를 붙인다
    saved = store.add_message(uid, "assistant", reply, meta)

    # ⑤ 기억 정리. 화면 소품 효과 고르기(효과 전용 AI 호출)를 동시에 시작해 둔다.
    prop_job = effects.start_prop(message, reply, llm) if choice.action != "clarify" else None
    yield {"type": "memory_start"}
    with tracing.observation("memory-update", "generation", input={"message": message, "reply": reply}) as step:
        changes = memory.update_memories(store, uid, message, reply, now, llm)
        step.update(output={k: [m["text"] for m in v] for k, v in changes.items()})
    with tracing.observation("memory-summary", "generation") as step:
        step.update(output={"summarized": memory.maybe_summarize(store, uid, llm)})
    yield {"type": "memory", "added": [m["text"] for m in changes["added"]],
           "updated": [m["text"] for m in changes["updated"]], "removed": [m["text"] for m in changes["removed"]]}
    # 화면 효과 ② 포인트 소품: 효과 전용 AI가 완성된 답을 보고 고른 주제(fake 모드는 단어 사전)
    accent = effects.prop(reply, scene and scene["name"], effects.result(prop_job, "prop")) if choice.action != "clarify" else None
    if accent:
        yield {"type": "effect", "layer": "prop", **accent}
    tracing.update_span(output={"reply": reply, "message_id": saved["id"]})
    yield {"type": "done", "message_id": saved["id"]}


def greet(uid, llm, store):
    """앱을 열 때 먼저 말을 건다. 때가 된 약속이 있거나, 대화가 처음이거나, 오래 쉬었을 때만.

    약속은 '전달됨'으로 바꾸는 데 성공한 것만 전한다. 창을 두 개 열어도 같은 약속을 두 번 전하지 않는다.
    """
    settings = store.get_settings(uid)
    character = store.get_character(uid) or DEFAULT_CHARACTER
    now = when.now_in(settings["timezone"])
    due = store.claim_due_reminders(uid, now)
    last = store.recent_messages(uid, 1)
    if not due and last and now - last[-1]["created_at"] < GREETING_GAP:
        return None

    memories, summary, _ = memory.build_context(store, uid)
    lines = [f"약속 알림: '{r['text']}' ({when.label(r['due_at'].astimezone(now.tzinfo))} 예정이었음)" for r in due]
    system = system_prompt(character, now, settings["location"], memories, summary, lines)
    guide = ["사용자가 방금 앱을 열었다. 네가 먼저 1~2문장으로 말을 건다."]
    guide.append("[도구 결과]의 약속 알림을 꼭 전한다." if due else "전할 약속은 없다. 약속이나 알림 이야기를 꺼내지 않는다.")
    guide.append("기억 메모에 오늘이거나 이미 지난 일정이 있으면 어땠는지 물어본다.")
    if not last:
        guide.append("처음 만난 사이다. 이름을 넣어 짧게 자기소개를 한다.")
    system += "\n\n[먼저 말 걸기]\n" + " ".join(guide)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "(앱을 열었다. 먼저 말을 걸어 줘)"}]
    # 먼저 말 걸기는 대화 턴과 따로인 greeting 트레이스 하나다. AI를 부르니 generation 으로 남긴다
    with tracing.user_attributes(uid, now, tags=["greeting"]), \
            tracing.observation("greeting", "generation", input=messages,
                                metadata={"reminders": len(due), "first_meeting": not last}) as step:
        text = llm.complete_text(messages)
        step.update(output=text)
    saved = store.add_message(uid, "assistant", text, {"action": "greeting", "reminders": [r["id"] for r in due]})
    log("INFO", "먼저 말 걸기", reminders=len(due))
    return {"id": saved["id"], "text": text, "reminders": [r["text"] for r in due]}
