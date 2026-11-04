"""토스페이먼츠 결제 승인 API 호출.

근거 문서(2026년 11월 확인)
- 결제위젯 연동하기: https://docs.tosspayments.com/guides/v2/payment-widget/integration
- API 레퍼런스 결제 승인: https://docs.tosspayments.com/reference (POST /v1/payments/confirm)
- 멱등키: https://docs.tosspayments.com/reference/using-api/idempotency-key

- 시크릿 키 뒤에 콜론(:)을 붙여 base64 로 인코딩한 Basic 인증을 쓴다(httpx 의 auth=(키, "") 가 그렇게 만든다).
- 결제 인증 뒤 10분 안에 승인해야 한다.
- Idempotency-Key 헤더로 주문 번호를 보낸다. 같은 주문이면 몇 번을 다시 보내도 같은 값이 가서,
  토스페이먼츠는 두 번째 요청을 다시 처리하지 않고 첫 응답을 돌려준다(300자 이하, 15일 유효).
  오류가 났다고 키를 바꿔 다시 보내지 않는다. 바꾸면 승인이 두 번 일어날 수 있다.
- 첫 요청이 아직 처리 중이면 409(IDEMPOTENT_REQUEST_PROCESSING)가 온다. 잠시 뒤 같은 키로 다시 묻는다.
- 시크릿 키와 paymentKey 는 로그에 남기지 않는다.
테스트에서는 confirm 함수를 가짜로 바꿔 끼워, 실제 토스페이먼츠를 부르지 않는다.
"""
import time
from dataclasses import dataclass

import httpx

CONFIRM_URL = "https://api.tosspayments.com/v1/payments/confirm"
PROCESSING = "IDEMPOTENT_REQUEST_PROCESSING"
RETRIES = 3          # 409 처리 중일 때 같은 키로 다시 묻는 횟수
RETRY_SECONDS = 1.0


@dataclass
class Confirmed:
    ok: bool
    data: dict  # 성공이면 Payment 객체, 실패면 {"code", "message"}


def confirm(secret_key, payment_key, order_id, amount, idempotency_key):
    for attempt in range(RETRIES + 1):
        result = _post(secret_key, payment_key, order_id, amount, idempotency_key)
        if result.ok or result.data.get("code") != PROCESSING or attempt == RETRIES:
            return result
        time.sleep(RETRY_SECONDS)
    return result


def _post(secret_key, payment_key, order_id, amount, idempotency_key):
    try:
        r = httpx.post(CONFIRM_URL, auth=(secret_key, ""), timeout=30,
                       headers={"Idempotency-Key": idempotency_key},
                       json={"paymentKey": payment_key, "orderId": order_id, "amount": amount})
    except httpx.HTTPError as error:
        return Confirmed(False, {"code": "NETWORK_ERROR", "message": f"결제 승인 요청을 보내지 못했어요 ({type(error).__name__})"})
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200:
        return Confirmed(False, {"code": data.get("code", f"HTTP_{r.status_code}"),
                                 "message": data.get("message", "결제 승인에 실패했어요")})
    return Confirmed(True, data)
