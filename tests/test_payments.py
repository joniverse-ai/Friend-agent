"""결제 승인 테스트. 실제 토스페이먼츠는 부르지 않고 승인 API 호출(payments.confirm)을 가짜로 바꿔 끼운다.

가짜 응답 모양은 토스페이먼츠 API 레퍼런스의 결제 승인 응답(Payment 객체)과 오류 응답({code, message})을 따른다.
https://docs.tosspayments.com/reference
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app import main, payments
from app.auth import User
from app.main import app, current_user, store

client = TestClient(app)


def payment_object(payment_key, order_id, amount):
    """승인 성공 응답(Payment 객체)에서 서버가 쓰는 칸과 대표 칸."""
    return {"mId": "tgen_docs", "version": "2022-11-16", "paymentKey": payment_key, "orderId": order_id,
            "orderName": "크레딧 100개", "type": "NORMAL", "status": "DONE", "method": "카드", "currency": "KRW",
            "totalAmount": amount, "balanceAmount": amount, "requestedAt": "2026-11-04T15:13:50+09:00",
            "approvedAt": "2026-11-04T15:14:17+09:00"}


class FakeToss:
    """승인 API를 대신한다. 불린 횟수와 받은 멱등키를 기록하고, 정해 둔 결과를 돌려준다."""
    def __init__(self):
        self.calls = []
        self.fail_with = None  # {"code", "message"} 를 넣으면 승인 실패

    def __call__(self, secret_key, payment_key, order_id, amount, idempotency_key):
        self.calls.append({"order_id": order_id, "idempotency_key": idempotency_key})
        if self.fail_with:
            return payments.Confirmed(False, dict(self.fail_with))
        return payments.Confirmed(True, payment_object(payment_key, order_id, amount))


@pytest.fixture
def toss(monkeypatch):
    fake = FakeToss()
    monkeypatch.setattr(payments, "confirm", fake)
    monkeypatch.setattr(main.settings, "toss_client_key", "test_gck_fake")
    monkeypatch.setattr(main.settings, "toss_secret_key", "test_gsk_fake")
    return fake


@pytest.fixture
def login():
    """login("이름") 으로 요청을 보내는 사용자를 바꾼다. 테스트마다 새 사용자."""
    users = {}

    def as_user(name):
        uid = users.setdefault(name, f"pay-{name}-{uuid.uuid4().hex[:8]}")
        app.dependency_overrides[current_user] = lambda: User(uid, anonymous=True)
        return uid

    yield as_user
    app.dependency_overrides.pop(current_user, None)


def new_order(product_id="credits_100"):
    return client.post("/api/orders", json={"product_id": product_id}).json()


def confirm(order, amount=None, payment_key="tgen_test_key"):
    return client.post("/api/payments/confirm", json={"payment_key": payment_key, "order_id": order["order_id"],
                                                      "amount": order["amount"] if amount is None else amount})


def plus_rows(uid):
    return [r for r in store.credit_history(uid, 1000) if r["reason"] == "purchase"]


def test_same_confirm_twice_gives_credits_once(toss, login):
    # 성공 페이지 새로고침: 두 번째 요청은 승인 API를 다시 부르지 않고 첫 결과를 돌려주며, + 줄은 하나뿐이다
    uid = login("a")
    order = new_order()
    first, second = confirm(order), confirm(order)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() and first.json()["credits"] == 100
    assert len(plus_rows(uid)) == 1 and store.credit_balance(uid) == 100
    assert len(toss.calls) == 1
    assert toss.calls[0]["idempotency_key"] == order["order_id"]  # 멱등키는 주문 번호


def test_tampered_amount_is_rejected_before_toss(toss, login):
    # successUrl 의 amount 를 고친 요청: 저장한 금액과 달라 거절하고, 승인 API는 한 번도 불리지 않는다
    uid = login("a")
    order = new_order("credits_1200")
    r = confirm(order, amount=100)
    assert r.status_code == 400
    assert toss.calls == []
    assert store.get_order(uid, order["order_id"])["status"] == "pending"
    assert store.credit_balance(uid) == 0


def test_someone_elses_order_is_rejected_before_toss(toss, login):
    # 다른 사용자의 주문 번호로 승인 요청: 없는 주문과 같이 404, 승인 API는 불리지 않고 주문 주인의 주문도 그대로다
    owner = login("owner")
    order = new_order()
    other = login("other")
    r = confirm(order)
    assert r.status_code == 404
    assert toss.calls == []
    assert store.get_order(owner, order["order_id"])["status"] == "pending"
    assert store.credit_balance(other) == 0 and store.credit_balance(owner) == 0


def test_toss_rejection_marks_order_failed_without_credits(toss, login):
    # 카드사 거절 같은 승인 실패: 주문은 실패, 크레딧은 늘지 않고, 토스페이먼츠의 오류 코드와 문구를 돌려준다
    uid = login("a")
    toss.fail_with = {"code": "REJECT_CARD_PAYMENT", "message": "한도초과 혹은 잔액부족으로 결제에 실패했습니다."}
    order = new_order()
    r = confirm(order)
    assert r.status_code == 400
    assert r.json()["code"] == "REJECT_CARD_PAYMENT"
    assert store.get_order(uid, order["order_id"])["status"] == "failed"
    assert plus_rows(uid) == [] and store.credit_balance(uid) == 0


def test_db_error_after_approval_recovers_with_same_key(toss, login, monkeypatch):
    # 승인은 됐는데 주문을 완료로 적다가 오류: 주문은 대기로 남고, 새로고침하면 같은 멱등키로 다시 물어 한 번만 지급한다
    uid = login("a")
    order = new_order()
    real_finish = store.finish_order
    monkeypatch.setattr(store, "finish_order", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("DB 오류")))
    with pytest.raises(RuntimeError):
        confirm(order)
    assert store.get_order(uid, order["order_id"])["status"] == "pending" and store.credit_balance(uid) == 0
    monkeypatch.setattr(store, "finish_order", real_finish)
    assert confirm(order).json()["credits"] == 100
    assert len(plus_rows(uid)) == 1 and store.credit_balance(uid) == 100
    assert [c["idempotency_key"] for c in toss.calls] == [order["order_id"]] * 2
