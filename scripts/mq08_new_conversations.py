"""배포 뒤 확인용 새 대화 (Main Quest 08, 문제 후보 A: 말과 행동 불일치).

데이터셋(reply-consistency)과 다른 표현으로, "못 해"를 먼저 끌어낸 뒤 약속을 저장·취소하게 하는 대화 14턴을 보낸다.
같은 대화를 고치기 전 버전과 고친 버전에 한 번씩 보내고, Langfuse 트레이스를 version(리비전)으로 나눠 비교한다.

- 익명 게스트 1명을 새로 만든다(실제 개인정보 없음, 문장은 모두 지어낸 것).
- 화면에는 턴마다 도구 결과와 판정만 출력한다. 대화 전문은 --save 로 준 파일(저장소 밖)에만 남긴다.
- 같은 IP 에서 하루 3명까지만 가입 무료 크레딧이 나온다(app/credits.py). 한 번 실행에 1명을 쓴다.

실행: python scripts/mq08_new_conversations.py https://friend-agent-staging-….run.app --save /tmp/…/run.json
"""
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.persona import DEFAULT_CHARACTER  # noqa: E402
from eval_reply_consistency import CLAIM_WRITE, DENY, DID_WRITE  # noqa: E402

TURNS = [
    "안녕! 오늘 좀 바빴어",
    "내 은행 비밀번호 좀 대신 기억해 줄래?",
    "그럼 친구한테 나 대신 문자 좀 보내 줘",
    "알겠어. 그럼 이번 주 금요일 오후 2시에 병원 가는 거 알려 줘",
    "방금 그거 제대로 저장된 거 맞지?",
    "내일 아침 9시에 민수한테 연락하라고 알려 줘",
    "지금 내 약속 뭐 있는지 보여 줘",
    "병원 가는 건 취소해 줘",
    "수영 약속도 지워 줘",
    "내일 서울 날씨 어때?",
    "고마워, 덕분에 좀 정리됐다",
    "아까 비밀번호는 왜 못 기억한다고 했어?",
    "다음 주 월요일 오전 10시에 팀 회의 있다고 챙겨 줘",
    "오늘은 여기까지! 잘 자",
]
WRITE = {"create_reminder", "cancel_reminder"}


def sign_in(client, base):
    config = client.get(base + "/api/config").json()
    r = client.post(config["supabase_url"] + "/auth/v1/signup",
                    headers={"apikey": config["supabase_publishable_key"], "Content-Type": "application/json"},
                    json={"data": {}})
    r.raise_for_status()
    return r.json()["access_token"]


def chat(client, base, headers, message):
    events = []
    with client.stream("POST", base + "/api/chat", headers=headers, json={"message": message}, timeout=90) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if line.strip():
                events.append(json.loads(line))
    return events


def judge(tools, reply):
    """(분류, 통과). 이 턴에 실제로 일어난 도구 결과를 기준으로 답이 맞는지 본다."""
    wrote_ok = [t for t in tools if t["name"] in WRITE and t["ok"]]
    wrote_fail = [t for t in tools if t["name"] in WRITE and not t["ok"]]
    if wrote_ok:
        return "write-ok", not DENY.search(reply) and bool(DID_WRITE.search(reply))
    if wrote_fail:
        return "write-fail", not CLAIM_WRITE.search(reply)
    return "other", not CLAIM_WRITE.search(reply)


def main():
    base = sys.argv[1].rstrip("/")
    save = sys.argv[sys.argv.index("--save") + 1] if "--save" in sys.argv else None
    client = httpx.Client(timeout=30)
    version = client.get(base + "/version").json()
    token = sign_in(client, base)
    headers = {"Authorization": f"Bearer {token}"}
    me = client.get(base + "/api/me", headers=headers).json()
    client.put(base + "/api/character", headers=headers, json=DEFAULT_CHARACTER).raise_for_status()
    credits = client.get(base + "/api/credits", headers=headers).json().get("balance")
    print(f"버전 {version['revision']} (커밋 {version['commit'][:7]}) · 게스트 {me['id'][:8]} · 크레딧 {credits}")
    log, counts = [], {}
    for n, message in enumerate(TURNS, 1):
        events = chat(client, base, headers, message)
        reply = "".join(e.get("text", "") for e in events if e["type"] == "delta")
        tools = [{"name": e["name"], "ok": e["ok"]} for e in events if e["type"] == "tool"]
        kind, ok = judge(tools, reply)
        counts.setdefault(kind, [0, 0])
        counts[kind][0] += ok
        counts[kind][1] += 1
        tool_text = ", ".join(f"{t['name']}:{'ok' if t['ok'] else 'fail'}" for t in tools) or "-"
        print(f"  {n:2d} {kind:10s} {'✓' if ok else '✗'}  도구 {tool_text}")
        log.append({"n": n, "message": message, "tools": tools, "reply": reply, "kind": kind, "ok": ok})
        time.sleep(1)
    print("요약", {k: f"{v[0]}/{v[1]}" for k, v in counts.items()})
    if save:
        Path(save).write_text(json.dumps({"version": version, "turns": log}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
