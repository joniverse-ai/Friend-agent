"""크레딧 규칙(섹션 3에서 정한 값).

- 메시지 한 건에 1크레딧. 차감은 서버에서만, 메시지를 받자마자 한다(동시 요청으로 잔액보다 많이 쓰지 못하게).
- 답을 만들다 실패하면 같은 무작위 값으로 refund 줄(+1)을 쌓아 되돌린다.
- 처음 온 사용자에게 무료 30크레딧을 한 번 준다. ref 가 signup:<user_id> 라서 여러 번 불러도 한 번만 쌓인다.
- 위기 표현 메시지는 잔액과 상관없이 차감하지 않는다. 안전 응답은 크레딧 때문에 막히면 안 된다
  (안전 응답은 정해진 문장이라 AI를 부르지 않아 원가도 없다).
"""
import uuid

FREE_CREDITS = 30
MESSAGE_COST = 1
NO_CREDITS = {"error": "insufficient_credits",
              "message": "크레딧을 다 썼어요. 크레딧을 충전하면 바로 이어서 이야기할 수 있어요.",
              "balance": 0}


def grant_signup(store, uid):
    """가입 무료 크레딧. 이번에 새로 쌓았으면 True."""
    return store.add_credit(uid, FREE_CREDITS, "signup", f"signup:{uid}")


def charge_message(store, uid):
    """메시지 한 건을 차감한다. 성공하면 (ref, 차감 뒤 잔액), 모자라면 (None, None).

    처음 온 사용자는 원장에 줄이 없어 첫 차감이 실패하므로, 그때 무료 크레딧을 주고 한 번 더 시도한다.
    """
    ref = uuid.uuid4().hex
    balance = store.spend_credit(uid, f"msg:{ref}", MESSAGE_COST)
    if balance is None and grant_signup(store, uid):
        balance = store.spend_credit(uid, f"msg:{ref}", MESSAGE_COST)
    return (ref, balance) if balance is not None else (None, None)


def refund_message(store, uid, ref):
    """실패한 메시지의 차감을 되돌린다. 같은 ref 로 두 번 불러도 한 번만 쌓인다."""
    return store.add_credit(uid, MESSAGE_COST, "refund", f"refund:{ref}")
