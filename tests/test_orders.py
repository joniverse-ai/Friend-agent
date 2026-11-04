"""상품표와 주문 테스트. 결제 승인 테스트 네 가지는 섹션 7에서 tests/ 에 따로 더한다."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app import main
from app.auth import User
from app.main import app, current_user, store

client = TestClient(app)


@pytest.fixture
def uid(monkeypatch):
    """새 사용자로 요청하고, 결제 키가 있는 것처럼 둔다(값은 가짜, 토스페이먼츠를 부르지 않는다)."""
    user_id = f"order-test-{uuid.uuid4().hex[:8]}"
    app.dependency_overrides[current_user] = lambda: User(user_id, anonymous=True)
    monkeypatch.setattr(main.settings, "toss_client_key", "test_gck_fake")
    monkeypatch.setattr(main.settings, "toss_secret_key", "test_gsk_fake")
    yield user_id
    app.dependency_overrides.pop(current_user, None)


def test_products_come_from_server_table():
    # 상품표는 서버 한 곳에서 내려온다(섹션 3에서 정한 세 묶음)
    got = {p["id"]: (p["amount"], p["credits"]) for p in client.get("/api/products").json()}
    assert got == {"credits_100": (1000, 100), "credits_550": (5000, 550), "credits_1200": (10000, 1200)}


def test_order_uses_server_amount_not_client(uid):
    # 화면이 금액을 같이 보내도 무시하고 상품표의 금액으로 대기 주문을 저장한다
    r = client.post("/api/orders", json={"product_id": "credits_100", "amount": 1})
    assert r.status_code == 200
    order = store.get_order(uid, r.json()["order_id"])
    assert (order["amount"], order["credits"], order["status"]) == (1000, 100, "pending")


def test_order_ids_are_random(uid):
    # 주문 번호는 추측할 수 없는 무작위 값이고 토스페이먼츠 규칙(6~64자)에 맞는다
    ids = {client.post("/api/orders", json={"product_id": "credits_100"}).json()["order_id"] for _ in range(3)}
    assert len(ids) == 3 and all(6 <= len(i) <= 64 for i in ids)


def test_unknown_product_is_rejected(uid):
    assert client.post("/api/orders", json={"product_id": "credits_999999"}).status_code == 400


def test_config_has_client_key_but_never_secret(uid):
    # 클라이언트 키는 화면으로 가고, 시크릿 키는 /api/config 어디에도 없다
    text = client.get("/api/config").text
    assert "test_gck_fake" in text and "test_gsk" not in text


def test_access_log_hides_payment_key():
    # uvicorn 접근 기록에 성공 주소가 찍혀도 paymentKey 는 앞 6자만 남는다
    import logging
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', None, None)
    record.args = ("127.0.0.1:1", "GET", "/?pay=success&paymentKey=tgen_ABCDEFGHIJKL&amount=5000", "1.1", 200)
    main.HidePaymentKey().filter(record)
    line = record.getMessage()
    assert "paymentKey=tgen_A…&amount=5000" in line and "ABCDEFGHIJKL" not in line


def test_fail_return_marks_my_pending_order_failed(uid):
    order_id = client.post("/api/orders", json={"product_id": "credits_100"}).json()["order_id"]
    client.post(f"/api/orders/{order_id}/fail", json={"code": "PAY_PROCESS_CANCELED"})
    assert store.get_order(uid, order_id)["status"] == "failed"
    assert store.credit_balance(uid) == 0  # 실패한 주문으로는 크레딧이 늘지 않는다
