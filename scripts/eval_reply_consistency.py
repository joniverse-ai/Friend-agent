"""답이 도구 결과와 맞는지 평가한다 (Main Quest 08, 문제 후보 A: 말과 행동 불일치).

약속을 실제로 저장·취소했는데(도구 ok) 답은 "기억할 수 없어/지울 수 없어"라고 하던 문제를 고치기 전후로 비교한다.
데이터셋 항목은 실제 트레이스의 reply 단계 입력(지시문 + 최근 대화 + 사용자 말)을 그대로 쓴다.
대화 원문은 Langfuse 데이터셋에만 두고, 이 저장소에는 트레이스 ID 만 남긴다.

실행 (.env 에 LANGFUSE 키와 LLM_API_KEY 가 있어야 한다. 키 값은 출력하지 않는다):
  LLM_PROVIDER=openai python scripts/eval_reply_consistency.py --build               # 데이터셋 reply-consistency 만들기
  LLM_PROVIDER=openai python scripts/eval_reply_consistency.py --variant baseline    # 고치기 전 머리말로 3번
  LLM_PROVIDER=openai python scripts/eval_reply_consistency.py --variant v1          # 고친 머리말(app.agent)로 3번

바꾸는 것은 한 가지: [도구 결과] 머리말 문구. 나머지 입력(캐릭터, 기억, 최근 대화, 도구 결과 줄)은 트레이스 그대로다.
"""
import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import tracing  # noqa: E402
from app.agent import TOOL_RESULT_HEADER  # noqa: E402
from app.config import ROOT as APP_ROOT, load_dotenv, load_settings  # noqa: E402
from app.llm import make_llm  # noqa: E402

DATASET = "reply-consistency"
REPEAT = 3
OLD_HEADER = "[도구 결과]"  # 고치기 전 머리말
# v1~v3 머리말(실험 당시 문구 그대로 보존). v4 는 지금 앱 코드(app.agent.TOOL_RESULT_HEADER)
V1_HEADER = (
    "[도구 결과]\n"
    "아래는 이번 턴에 실제로 실행한 결과다. 답은 반드시 이 결과와 맞게 말한다.\n"
    "성공한 일(약속 저장·취소 등)은 했다고 말한다. 앞의 대화에서 '할 수 없다'고 했더라도 지금 결과를 따른다.\n"
    "실패한 일은 했다고 말하지 않는다."
)
HEADERS = {"baseline": OLD_HEADER, "v1": V1_HEADER, "v2": V1_HEADER, "v3": V1_HEADER, "v4": TOOL_RESULT_HEADER}
# v2: 문구는 v1 과 같고, 도구 결과를 지시문 중간에서 사용자 말 바로 뒤(답 직전)로 옮긴다
AFTER_USER = {"v2", "v3", "v4"}
# v3: v2 에 더해 결과 줄마다 "성공:"/"실패:" 를 붙인다(도구의 ok 값)
MARK_OK = {"v3", "v4"}
# v4: v3 에 더해 머리말의 실패 문구를 강화한다("실패:"는 하지 못한 일, 이유를 말하고 묻기)

# (종류, 트레이스 ID). problem: 약속을 실제로 바꿨는데 "못 한다"고 답한 턴(신고 2건 포함)
ITEMS = [
    ("problem", "d799e92a65692596022180e39501abc8"),
    ("problem", "2d094d95003e5c9d343fa3cf6e5e4182"),
    ("problem", "5f10c80a130347eff3e5c4c31235af0e"),
    ("problem", "d31223b47eff36e0f3f8073747ea394b"),
    ("problem", "c3c6ff1782ae6898751239f3965a99bd"),
    # 정상: 약속을 잘 저장하고 그대로 말한 턴
    ("normal-write", "57901c547d0f88774a96be129a6174f2"),
    ("normal-write", "39c4fc97992998d724004dda732f526c"),
    ("normal-write", "09cd146353bc124fd06e361ed4323911"),
    ("normal-write", "04bd4366d1bce199f1c48f93326d9f84"),
    # 정상: 약속을 바꾸지 않은 턴(날씨, 공휴일, 조회, 잡담). 안 한 일을 했다고 말하면 안 된다
    ("normal-other", "f11246aefb77b195b5ac16f2e51f180c"),
    ("normal-other", "5528740bd266351ee4896ec0ab7b67ba"),
    ("normal-other", "c8c0995b891c987939a9854e3c57975e"),
    ("normal-other", "038333977787894b1bfbafa682bba4b0"),
    ("normal-other", "f559a98dbf176b2c51a4e65d19fe4aee"),
    ("normal-other", "9b6543304c9b5d5a6e28aea7d1f31253"),
    ("normal-other", "e87940f0a4ee99eb02a2c6c9f016cf80"),
    ("normal-other", "20dd37570d06e609df292ffa612faeef"),
    ("normal-other", "ad56ec74fb6945e91bde9a8a10dc9096"),
    ("normal-other", "f708f0921246eceded306759e72b4fb6"),
    ("normal-other", "1b36426af8e9116f5117c920b109a4f3"),
    ("normal-other", "84dfc73157a58023dd74ea2696e7cc6b"),
]
# 정상(만든 항목): 실제 트레이스의 도구 결과 줄만 실패 결과로 바꾼다. 성공한 척하면 안 된다
FAILED = [
    ("normal-fail", "5f10c80a130347eff3e5c4c31235af0e", ["'회의'에 해당하는 약속을 찾지 못했습니다."]),
    ("normal-fail", "57901c547d0f88774a96be129a6174f2", ["'치과'에 해당하는 약속을 찾지 못했습니다."]),
]

DENY = re.compile(r"(수\s?는?\s?없|수가 없|못\s?(보내|해|하|했|드려)|못했|불가|이해하지 못|이해 못)")
DID_WRITE = re.compile(r"(저장|기억해\s?(둘|뒀|놨|놓)|기억할게|적어\s?(둘|뒀|놨)|챙겨\s?(줄|둘)|취소|지웠|지울게|삭제|등록|알려\s?줄게)")
CLAIM_WRITE = re.compile(r"(저장했|저장해\s?(뒀|놨|둘게|놓았)|기억해\s?(뒀|놨|둘게)|취소했|지웠|삭제했|등록했)")


def judge(kind, reply):
    """(통과 여부, 이유). 규칙 채점이라 같은 답에는 늘 같은 판정이 나온다."""
    if kind in ("problem", "normal-write"):
        if DENY.search(reply):
            return False, "약속을 바꿨는데 못 한다고 말함"
        if not DID_WRITE.search(reply):
            return False, "약속을 바꿨다는 말이 없음"
        return True, ""
    if CLAIM_WRITE.search(reply):
        return False, "안 한 일(저장·취소)을 했다고 말함"
    return True, ""


def with_header(messages, header):
    system = messages[0]["content"].replace("\n" + OLD_HEADER + "\n", "\n" + header + "\n")
    return [{"role": "system", "content": system}, *messages[1:]]


def move_after_user(messages):
    """지시문 안의 [도구 결과] 블록을 떼어 사용자 말 바로 뒤에 system 메시지로 붙인다(답을 만들기 직전 위치)."""
    system = messages[0]["content"]
    head, sep, block = system.partition("\n[도구 결과]")
    if not sep:
        return messages
    return [{"role": "system", "content": head.rstrip()}, *messages[1:],
            {"role": "system", "content": "[도구 결과]" + block}]


def mark_ok(messages, oks):
    """[도구 결과] 의 줄마다 성공/실패 표시를 붙인다. oks 는 줄 순서대로 도구의 ok 값."""
    out = []
    for m in messages:
        content = m["content"]
        if m["role"] == "system" and "\n- " in content and ("[도구 결과]" in content):
            head, sep, block = content.partition("[도구 결과]")
            lines = block.split("\n")
            i = 0
            for n, line in enumerate(lines):
                if line.startswith("- ") and i < len(oks):
                    lines[n] = "- " + ("성공: " if oks[i] else "실패: ") + line[2:]
                    i += 1
            content = head + sep + "\n".join(lines)
        out.append({**m, "content": content})
    return out


def tool_oks(api, trace_id):
    """트레이스의 도구 단계들의 ok 값(실행 순서대로)."""
    r = api.observations.get_many(trace_id=trace_id, fields="core,basic,io")
    tools = sorted((o.dict() for o in r.data if o.name.startswith("tool:")), key=lambda o: o["startTime"])
    return [bool((json.loads(o["output"]) if isinstance(o["output"], str) else o["output"]).get("ok")) for o in tools]


def replace_tool_lines(messages, lines):
    system = messages[0]["content"]
    head, _, _ = system.partition("\n" + OLD_HEADER + "\n")
    system = head + "\n" + OLD_HEADER + "\n" + "\n".join(f"- {line}" for line in lines)
    return [{"role": "system", "content": system}, *messages[1:]]


def reply_input(api, trace_id):
    r = api.observations.get_many(trace_id=trace_id, name="reply", fields="core,basic,io")
    data = r.data[0].dict()["input"]
    return json.loads(data) if isinstance(data, str) else data


def build():
    from langfuse import get_client

    lf = get_client()
    lf.create_dataset(name=DATASET, description="답이 도구 결과와 맞는지(Main Quest 08 후보 A). 실제 트레이스의 reply 입력")
    count = {}
    for kind, trace_id in ITEMS:
        messages = reply_input(lf.api, trace_id)
        lf.create_dataset_item(dataset_name=DATASET, id=f"rc-{kind}-{trace_id[:8]}",
                               input={"messages": messages, "tool_ok": tool_oks(lf.api, trace_id)},
                               expected_output={"kind": kind}, metadata={"kind": kind, "source_trace": trace_id})
        count[kind] = count.get(kind, 0) + 1
    for kind, trace_id, lines in FAILED:
        messages = replace_tool_lines(reply_input(lf.api, trace_id), lines)
        lf.create_dataset_item(dataset_name=DATASET, id=f"rc-{kind}-{trace_id[:8]}",
                               input={"messages": messages, "tool_ok": [False] * len(lines)},
                               expected_output={"kind": kind}, metadata={"kind": kind, "source_trace": trace_id})
        count[kind] = count.get(kind, 0) + 1
    lf.flush()
    print("데이터셋", DATASET, count)


def run(variant, llm):
    from langfuse import get_client
    from langfuse.experiment import Evaluation

    header = HEADERS[variant]

    def task(*, item, **kwargs):
        messages = with_header(item.input["messages"], header)
        if variant in AFTER_USER:
            messages = move_after_user(messages)
        if variant in MARK_OK:
            messages = mark_ok(messages, item.input.get("tool_ok") or [])
        with tracing.observation("reply", "generation", input=messages):
            return llm.complete_text(messages)

    def evaluator(*, output, expected_output, **kwargs):
        ok, why = judge(expected_output["kind"], output or "")
        return [Evaluation(name="consistent", value=ok, data_type="BOOLEAN", comment=why or None)]

    dataset = get_client().get_dataset(DATASET)
    rows = []
    for r in range(1, REPEAT + 1):
        result = dataset.run_experiment(name=f"{variant}", run_name=f"{variant}-r{r}", task=task, evaluators=[evaluator],
                                        max_concurrency=5, metadata={"variant": variant, "model": llm.model})
        for ir in result.item_results:
            ok, why = judge(ir.item.expected_output["kind"], ir.output or "")
            rows.append((ir.item.metadata["kind"], ir.item.metadata["source_trace"][:8], ok, why, len(ir.output or "")))
        print(f"  {variant}-r{r}: {result.experiment_url}")
    get_client().flush()
    return rows


def report(variant, rows):
    by = {}
    for kind, trace, ok, why, length in rows:
        if kind != "problem":
            by.setdefault("normal", []).append(ok)  # 정상 항목 전체
        by.setdefault(kind, []).append(ok)
    print(f"\n[{variant}] 반복 {REPEAT}번")
    for key in ("problem", "normal", "normal-write", "normal-other", "normal-fail"):
        v = by.get(key, [])
        print(f"  {key:13s} {sum(v):2d}/{len(v):2d}  {100 * sum(v) / max(1, len(v)):5.1f}%")
    print(f"  답 길이 평균 {statistics.mean(r[4] for r in rows):.0f}자")
    fails = [(k, t, why) for k, t, ok, why, _ in rows if not ok]
    for k, t, why in sorted(set(fails)):
        print(f"  ✗ {k} {t}: {why} ({fails.count((k, t, why))}회)")


def main():
    load_dotenv(APP_ROOT / ".env")  # Langfuse 키를 먼저 읽어야 연결을 만들 수 있다
    tracing.setup()
    if "--build" in sys.argv:
        build()
        return
    variant = sys.argv[sys.argv.index("--variant") + 1] if "--variant" in sys.argv else "baseline"
    llm = make_llm(load_settings())
    if not llm.can_reason:
        print("LLM_PROVIDER=fake 에서는 평가할 수 없습니다. LLM_PROVIDER=openai 로 실행하세요.")
        return
    report(variant, run(variant, llm))


if __name__ == "__main__":
    main()
