"""크레딧 원장 테스트. 가짜 LLM과 서버 메모리 저장소로, 테스트마다 새 사용자를 만들어 확인한다."""
import json
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from app import agent, credits
from app.auth import User
from app.main import app, current_user, store

client = TestClient(app)


@pytest.fixture
def uid():
    """테스트마다 처음 온 새 사용자로 요청을 보낸다."""
    user_id = f"credit-test-{uuid.uuid4().hex[:8]}"
    app.dependency_overrides[current_user] = lambda: User(user_id, anonymous=True)
    yield user_id
    app.dependency_overrides.pop(current_user, None)


def send(message):
    return client.post("/api/chat", json={"message": message})


def drain(uid):
    """잔액을 0으로 만든다(테스트 준비용)."""
    credits.grant_signup(store, uid)
    while store.spend_credit(uid, f"msg:drain-{uuid.uuid4().hex}") is not None:
        pass


def test_new_user_gets_free_credits_once(uid):
    # 처음 /api/me 를 부르면 30, 여러 번 불러도 더 늘지 않는다
    assert client.get("/api/me").json()["credits"] == credits.FREE_CREDITS
    client.get("/api/me")
    assert store.credit_balance(uid) == credits.FREE_CREDITS
    assert [r["reason"] for r in store.credit_history(uid)] == ["signup"]


def test_message_spends_one_credit(uid):
    # 메시지 한 건을 보내면 잔액이 1 줄고, 원장에 -1 줄이 생긴다. 스트림 첫 이벤트로 잔액을 알려 준다
    client.get("/api/me")
    r = send("안녕!")
    assert r.status_code == 200
    first = json.loads(r.text.splitlines()[0])
    assert first == {"type": "credits", "balance": credits.FREE_CREDITS - 1}
    assert store.credit_balance(uid) == credits.FREE_CREDITS - 1
    assert store.credit_history(uid)[0]["delta"] == -1


def test_first_message_without_me_still_gets_free_credits(uid):
    # 화면이 /api/me 를 부르기 전에 메시지가 와도 무료 크레딧을 받고 차감된다
    assert send("안녕!").status_code == 200
    assert store.credit_balance(uid) == credits.FREE_CREDITS - 1


def test_zero_balance_is_rejected_with_402(uid):
    # 잔액 0이면 402와 정한 문구로 거절하고, 원장에 아무것도 쌓지 않는다
    drain(uid)
    before = len(store.credit_history(uid, 1000))
    r = send("안녕!")
    assert r.status_code == 402
    assert r.json() == credits.NO_CREDITS
    assert len(store.credit_history(uid, 1000)) == before


def test_crisis_message_is_answered_even_at_zero(uid):
    # 위기 표현은 잔액 0이어도 안전 응답을 받고, 차감도 하지 않는다
    drain(uid)
    r = send("요즘 너무 죽고 싶어")
    assert r.status_code == 200
    text = "".join(json.loads(line).get("text", "") for line in r.text.splitlines())
    assert "109" in text
    assert store.credit_balance(uid) == 0


def test_failed_turn_refunds_the_credit(uid, monkeypatch):
    # 답을 만들다 오류가 나면 되돌리는 +1 줄이 쌓여 잔액이 그대로다
    client.get("/api/me")

    def broken(*args, **kwargs):
        raise RuntimeError("테스트용 실패")
        yield  # 생성기로 만들기 위한 줄

    monkeypatch.setattr(agent, "run_turn", broken)
    r = send("안녕!")
    assert any(json.loads(line)["type"] == "error" for line in r.text.splitlines())
    assert store.credit_balance(uid) == credits.FREE_CREDITS
    assert [row["reason"] for row in store.credit_history(uid, 3)] == ["refund", "message", "signup"]


def test_concurrent_spend_at_balance_one_passes_once(uid):
    # 잔액 1에서 차감 열 개가 동시에 와도 하나만 통과한다(잔액이 음수가 되지 않는다)
    drain(uid)
    store.add_credit(uid, 1, "test", f"test:{uid}")
    results = []
    threads = [threading.Thread(target=lambda: results.append(store.spend_credit(uid, f"msg:{uuid.uuid4().hex}")))
               for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 1
    assert store.credit_balance(uid) == 0


def test_delete_all_keeps_the_ledger(uid):
    # 내 데이터 전체 삭제를 해도 원장은 남아, 무료 크레딧을 다시 받지 못한다
    client.get("/api/me")
    send("안녕!")
    client.delete("/api/data")
    client.get("/api/me")
    assert store.credit_balance(uid) == credits.FREE_CREDITS - 1
