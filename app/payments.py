"""토스페이먼츠 결제 승인 API 호출.

근거 문서(2026년 11월 확인)
- 결제위젯 연동하기: https://docs.tosspayments.com/guides/v2/payment-widget/integration
- API 레퍼런스 결제 승인: https://docs.tosspayments.com/reference (POST /v1/payments/confirm)

- 시크릿 키 뒤에 콜론(:)을 붙여 base64 로 인코딩한 Basic 인증을 쓴다(httpx 의 auth=(키, "") 가 그렇게 만든다).
- 결제 인증 뒤 10분 안에 승인해야 한다.
- 시크릿 키와 paymentKey 는 로그에 남기지 않는다.
테스트에서는 confirm 함수를 가짜로 바꿔 끼워, 실제 토스페이먼츠를 부르지 않는다.
"""
from dataclasses import dataclass

import httpx

CONFIRM_URL = "https://api.tosspayments.com/v1/payments/confirm"


@dataclass
class Confirmed:
    ok: bool
    data: dict  # 성공이면 Payment 객체, 실패면 {"code", "message"}


def confirm(secret_key, payment_key, order_id, amount):
    try:
        r = httpx.post(CONFIRM_URL, auth=(secret_key, ""), timeout=30,
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
