"""크레딧 규칙(섹션 3에서 정한 값).

- 메시지 한 건에 1크레딧. 차감은 서버에서만, 메시지를 받자마자 한다(동시 요청으로 잔액보다 많이 쓰지 못하게).
- 답을 만들다 실패하면 같은 무작위 값으로 refund 줄(+1)을 쌓아 되돌린다.
- 처음 온 사용자에게 무료 30크레딧을 한 번 준다. ref 가 signup:<user_id> 라서 여러 번 불러도 한 번만 쌓인다.
  단 같은 IP에서는 최근 24시간에 3명까지만 준다(게스트를 새로 만들어 무료 크레딧을 반복해서 받는 공격을 막는다).
  IP 원문은 저장하지 않고, 서버 비밀값과 섞은 해시만 가입 지급 줄에 적는다.
- 위기 표현 메시지는 잔액과 상관없이 차감하지 않는다. 안전 응답은 크레딧 때문에 막히면 안 된다
  (안전 응답은 정해진 문장이라 AI를 부르지 않아 원가도 없다).
"""
import hashlib
import hmac
import uuid

# 상품표(섹션 3). 서버의 이 한 곳에만 둔다. 화면은 /api/products 로 받아 보여 주고, 주문은 상품 ID만 보낸다.
PRODUCTS = {
    "credits_100": {"name": "크레딧 100개", "amount": 1000, "credits": 100},
    "credits_550": {"name": "크레딧 550개", "amount": 5000, "credits": 550},
    "credits_1200": {"name": "크레딧 1,200개", "amount": 10000, "credits": 1200},
}

FREE_CREDITS = 30
SIGNUP_PER_IP_PER_DAY = 3
MESSAGE_COST = 1
NO_CREDITS = {"error": "insufficient_credits",
              "message": "크레딧을 다 썼어요. 크레딧을 충전하면 바로 이어서 이야기할 수 있어요.",
              "balance": 0}


def hash_ip(ip, server_secret):
    """IP를 되돌릴 수 없는 값으로 바꾼다. 서버 비밀값을 섞어서, 해시만 보고 IP를 하나씩 대입해 찾을 수 없게 한다."""
    if not ip:
        return None
    key = hashlib.sha256(f"ip-hash:{server_secret}".encode()).digest()
    return hmac.new(key, ip.encode(), hashlib.sha256).hexdigest()[:32]


def grant_signup(store, uid, ip_hash=None):
    """가입 무료 크레딧. 결과: granted(이번에 지급) / exists(이미 받음) / limited(이 IP의 하루 한도를 넘음)."""
    return store.grant_signup(uid, FREE_CREDITS, ip_hash, SIGNUP_PER_IP_PER_DAY)


def charge_message(store, uid, ip_hash=None):
    """메시지 한 건을 차감한다. 성공하면 (ref, 차감 뒤 잔액), 모자라면 (None, None).

    처음 온 사용자는 원장에 줄이 없어 첫 차감이 실패하므로, 그때 무료 크레딧을 주고 한 번 더 시도한다.
    """
    ref = uuid.uuid4().hex
    balance = store.spend_credit(uid, f"msg:{ref}", MESSAGE_COST)
    if balance is None and grant_signup(store, uid, ip_hash) == "granted":
        balance = store.spend_credit(uid, f"msg:{ref}", MESSAGE_COST)
    return (ref, balance) if balance is not None else (None, None)


def refund_message(store, uid, ref):
    """실패한 메시지의 차감을 되돌린다. 같은 ref 로 두 번 불러도 한 번만 쌓인다."""
    return store.add_credit(uid, MESSAGE_COST, "refund", f"refund:{ref}")
