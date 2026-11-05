"""AI 친구 에이전트 서비스.

화면(/)과 API를 FastAPI 하나가 함께 제공한다.
- /health, /version: 서비스 상태와 배포 버전
- /api/config: 화면이 쓰는 설정(로그인 방식, 개발용 패널 표시 여부)
- /api/character, /api/settings: 캐릭터와 사용자 설정
- /api/chat: 메시지 한 턴. 결과를 이벤트(JSON 한 줄씩)로 스트리밍한다. 받자마자 1크레딧 차감, 모자라면 402
- /api/credits: 크레딧 잔액과 최근 기록 10줄
- /api/products, /api/orders, /api/payments/confirm: 크레딧 결제(토스페이먼츠 결제위젯)
- /api/greeting: 앱을 열 때 먼저 말 걸기
- /api/ocr: 사진 속 글자 읽기(OCR_ENABLED=true 일 때)
- /api/pdf: PDF 공지문 글자 읽기(PDF_ENABLED=true 일 때)
- /api/memories, /api/reminders, /api/data: 기억, 약속, 내 데이터 관리
"""
import html
import json
import logging
import os
import re
import time
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import agent, credits, ocr, payments, pdf, tracing, when
from app.auth import Auth, User
from app.config import ROOT, load_settings
from app.llm import make_llm
from app.logs import log
from app.persona import DEFAULT_CHARACTER, OPTIONS, build_persona, clean_character
from app.safety import is_crisis
from app.store.base import make_store
from app.tools import CITIES

class HidePaymentKey(logging.Filter):
    """uvicorn 접근 기록에서 결제 성공 주소의 paymentKey 값을 앞 6자만 남기고 가린다.

    토스페이먼츠가 성공 주소(/?pay=success&paymentKey=…)에 붙여 보내는 값이라, 그대로 두면 서버 로그에 전체가 남는다.
    배포한 뒤 Cloud Run 이 따로 남기는 요청 기록의 주소는 앱에서 가릴 수 없다.
    """
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) >= 3 and isinstance(record.args[2], str):
            args = list(record.args)
            args[2] = PAYMENT_KEY_IN_URL.sub(r"\1…", args[2])
            record.args = tuple(args)
        return True


PAYMENT_KEY_IN_URL = re.compile(r"(paymentKey=[^&\s]{0,6})[^&\s]*")
logging.getLogger("uvicorn.access").addFilter(HidePaymentKey())

settings = load_settings()  # 꼭 필요한 값이 없으면 여기서 종료한다
tracing.setup()  # .env 를 읽은 뒤에 만든다. Langfuse 키가 없으면 꺼진 채로 둔다
llm = make_llm(settings)
store = make_store(settings)
current_user = Auth(settings)

# FastAPI 0.150 부터 켜져 있는 HTTP 요청 추적을 끈다. 켜 두면 Cloud Run 이 붙이는 traceparent 헤더를
# 부모로 삼아, 헤더가 "기록 안 함(00)"인 요청의 Langfuse 트레이스가 통째로 버려진다(섹션 6에서 발견).
app = FastAPI(title="AI 친구 에이전트", telemetry={"tracing": False})

# 시작 지연 스위치(배포 실습용). 포트는 바로 열지만 정한 시간 동안은 준비 중이라 모든 요청에 503을 돌려준다.
# AI 서비스가 시작할 때 모델 설정이나 색인을 불러오느라 늦게 준비되는 상황을 흉내 낸다. 기본값 0(끔).
SLOW_START_SECONDS = float(os.environ.get("SLOW_START_SECONDS", "0") or 0)
READY_AT = time.time() + SLOW_START_SECONDS
if SLOW_START_SECONDS:
    log("WARNING", f"시작 지연 스위치가 켜져 있습니다. {SLOW_START_SECONDS:.0f}초 동안 준비 중으로 응답합니다.")


@app.middleware("http")
async def not_ready_yet(request, call_next):
    if time.time() < READY_AT:
        return JSONResponse({"status": "starting"}, status_code=503)
    return await call_next(request)


def get_llm():
    """테스트에서는 이 함수를 가짜 LLM을 돌려주는 함수로 바꿔 끼운다."""
    return llm


class ChatRequest(BaseModel):
    message: str


class SettingsRequest(BaseModel):
    location: str | None = None
    timezone: str | None = None


class ReportRequest(BaseModel):
    message_id: int
    reason: str = ""


class FeedbackRequest(BaseModel):
    message_id: int
    liked: bool


class OrderRequest(BaseModel):
    product_id: str  # 금액은 받지 않는다. 서버가 상품표에서 찾는다


class ConfirmRequest(BaseModel):
    payment_key: str
    order_id: str
    amount: int


class FailRequest(BaseModel):
    code: str = ""


@app.get("/", response_class=HTMLResponse)
def home():
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    banner = "" if settings.env in ("production", "local") else f'<div class="banner">테스트 환경 ({settings.env})</div>'
    if os.environ.get("BANNER"):  # 이미지를 새로 만들지 않고 환경변수만 바꿔 새 리비전을 만들 때 쓰는 화면 문구
        banner += f'<div class="banner">{html.escape(os.environ["BANNER"][:80])}</div>'
    return page.replace("<!--ENV_BANNER-->", banner)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/version")
def version():
    return {"commit": settings.commit, "revision": settings.revision, "env": settings.env,
            "llm": settings.provider, "model": settings.model, "storage": settings.storage, "auth": settings.auth,
            "banner": os.environ.get("BANNER", "")}


@app.get("/api/config")
def config():
    # publishable key 는 브라우저에 공개해도 되는 키다. secret key 는 절대 여기에 넣지 않는다.
    return {"env": settings.env, "auth": settings.auth, "storage": settings.storage,
            "supabase_url": settings.supabase_url if settings.auth == "supabase" else "",
            "supabase_publishable_key": settings.supabase_publishable_key if settings.auth == "supabase" else "",
            "google_login": settings.google_login, "show_turn_panel": settings.show_turn_panel,
            "ocr_enabled": settings.ocr_enabled, "pdf_enabled": settings.pdf_enabled,
            # 클라이언트 키는 결제위젯을 띄우는 공개 키다. 시크릿 키는 여기에 넣지 않는다.
            "payments_enabled": payments_enabled(),
            "toss_client_key": settings.toss_client_key if payments_enabled() else "",
            "options": OPTIONS, "cities": list(CITIES)}


@app.get("/api/me")
def me(request: Request, user: User = Depends(current_user)):
    # 처음 온 사용자에게 무료 크레딧(한 번만, 같은 IP에서는 하루 3명까지)
    granted = credits.grant_signup(store, user.id, ip_hash(request))
    if granted == "granted":
        log("INFO", "가입 무료 크레딧 지급", credits=credits.FREE_CREDITS)
    elif granted == "limited":
        log("WARNING", "가입 무료 크레딧 한도 초과(같은 IP)", limit=credits.SIGNUP_PER_IP_PER_DAY)
    return {"id": user.id, "anonymous": user.anonymous, "email": user.email,
            "has_character": store.get_character(user.id) is not None,
            "credits": store.credit_balance(user.id)}


def ip_hash(request: Request):
    """요청 보낸 곳의 IP 해시. IP 원문은 저장하거나 로그에 남기지 않는다.

    Cloud Run 앞단은 X-Forwarded-For 맨 뒤에 실제 접속 IP를 붙인다. 앞쪽 값은 요청한 사람이 마음대로 넣을 수
    있으므로 맨 뒤 값만 쓴다. 헤더가 없으면(내 컴퓨터) 연결된 주소를 쓴다.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[-1].strip() if forwarded else (request.client.host if request.client else "")
    return credits.hash_ip(ip, settings.supabase_secret_key or "local")


@app.get("/api/credits")
def my_credits(user: User = Depends(current_user)):
    return {"balance": store.credit_balance(user.id),
            "history": [{"delta": r["delta"], "reason": r["reason"], "created_at": r["created_at"].isoformat()}
                        for r in store.credit_history(user.id, 10)]}


# ── 크레딧 결제 ─────────────────────────────────────────────────────
# 주문 저장 → 결제위젯(화면) → successUrl → 금액·상태 확인 → 시크릿 키로 승인 → 주문 완료 + 원장 + 줄.
# 크레딧을 늘리는 곳은 confirm_payment 한 곳뿐이다.

def payments_enabled():
    return bool(settings.toss_client_key and settings.toss_secret_key)


@app.get("/api/products")
def products():
    return [{"id": pid, "name": p["name"], "amount": p["amount"], "credits": p["credits"],
             "unit_price": round(p["amount"] / p["credits"], 1)} for pid, p in credits.PRODUCTS.items()]


@app.post("/api/orders")
def create_order(req: OrderRequest, user: User = Depends(current_user)):
    if not payments_enabled():
        raise HTTPException(503, "결제가 아직 준비되지 않았습니다")
    product = credits.PRODUCTS.get(req.product_id)
    if not product:
        raise HTTPException(400, "없는 상품입니다")
    order_id = f"ord_{uuid.uuid4().hex}"  # 추측할 수 없는 무작위 값
    store.create_order(user.id, order_id, req.product_id, product["amount"], product["credits"])
    log("INFO", "주문 저장", order_id=order_id, product_id=req.product_id, amount=product["amount"])
    return {"order_id": order_id, "order_name": product["name"], "amount": product["amount"],
            "credits": product["credits"]}


def paid_result(uid, order):
    return {"ok": True, "order_id": order["order_id"], "credits": order["credits"],
            "balance": store.credit_balance(uid)}


@app.post("/api/payments/confirm")
def confirm_payment(req: ConfirmRequest, user: User = Depends(current_user)):
    if not payments_enabled():
        raise HTTPException(503, "결제가 아직 준비되지 않았습니다")
    order = store.get_order(user.id, req.order_id)  # user_id 조건: 남의 주문이면 없는 주문과 같다
    if not order:
        log("WARNING", "결제 승인 거절: 주문 없음", order_id=req.order_id)
        raise HTTPException(404, "주문을 찾지 못했습니다")
    if req.amount != order["amount"]:
        log("WARNING", "결제 승인 거절: 금액 불일치", order_id=req.order_id)
        raise HTTPException(400, "결제 금액이 주문 금액과 다릅니다")
    if order["status"] == "done":  # 새로고침 등으로 다시 온 요청: 승인 API를 다시 부르지 않고 처음 결과를 돌려준다
        return paid_result(user.id, order)
    if order["status"] != "pending":
        raise HTTPException(409, "이미 끝난 주문입니다")

    # 멱등키는 주문 번호. 같은 주문의 승인 요청에는 몇 번을 다시 보내도 같은 값이 간다
    idempotency_key = req.order_id
    log("INFO", "결제 승인 요청", order_id=req.order_id, idempotency_key=idempotency_key)
    result = payments.confirm(settings.toss_secret_key, req.payment_key, req.order_id, req.amount, idempotency_key)
    if result.ok and (result.data.get("status") != "DONE" or result.data.get("totalAmount") != order["amount"]):
        result = payments.Confirmed(False, {"code": "UNEXPECTED_RESPONSE", "message": "승인 결과가 주문과 맞지 않아요"})
    if not result.ok and result.data["code"] in (payments.PROCESSING, "NETWORK_ERROR"):
        # 승인이 됐는지 아직 모른다. 주문을 실패로 바꾸지 않고 대기로 둔다(새로고침하면 같은 키로 다시 묻는다)
        log("WARNING", "결제 승인 결과 미확인", order_id=req.order_id, code=result.data["code"])
        return JSONResponse({"ok": False, "code": result.data["code"],
                             "message": "결제 확인이 늦어지고 있어요. 잠시 뒤 이 페이지를 새로고침해 주세요."}, status_code=503)
    if not result.ok:
        store.fail_order(user.id, req.order_id)
        log("WARNING", "결제 승인 실패", order_id=req.order_id, code=result.data["code"])
        return JSONResponse({"ok": False, "code": result.data["code"], "message": result.data["message"]}, status_code=400)

    done = store.finish_order(user.id, req.order_id, req.payment_key, result.data.get("approvedAt"))
    if done is None:  # 같은 주문의 다른 요청이 먼저 완료했다
        return paid_result(user.id, store.get_order(user.id, req.order_id))
    store.add_credit(user.id, order["credits"], "purchase", f"order:{req.order_id}")
    log("INFO", "결제 완료, 크레딧 지급", order_id=req.order_id, credits=order["credits"])
    return paid_result(user.id, done)


@app.post("/api/orders/{order_id}/fail")
def fail_order(order_id: str, req: FailRequest, user: User = Depends(current_user)):
    # 실패 주소(failUrl)로 돌아온 주문. 내 주문이 대기일 때만 실패로 바꾼다
    store.fail_order(user.id, order_id)
    log("INFO", "결제 실패로 돌아옴", order_id=order_id, code=req.code[:60])
    return {"ok": True}


# ── 캐릭터와 설정 ──────────────────────────────────────────────────

@app.get("/api/character")
def get_character(user: User = Depends(current_user)):
    c = store.get_character(user.id)
    return {"character": c or DEFAULT_CHARACTER, "saved": c is not None}


@app.put("/api/character")
def put_character(data: dict, user: User = Depends(current_user)):
    c = clean_character(data)
    store.save_character(user.id, c)
    log("INFO", "캐릭터 저장")
    return {"character": c}


@app.post("/api/character/preview")
def preview_character(data: dict):
    return {"prompt": build_persona(clean_character(data))}


@app.get("/api/settings")
def get_settings(user: User = Depends(current_user)):
    return store.get_settings(user.id)


@app.put("/api/settings")
def put_settings(req: SettingsRequest, user: User = Depends(current_user)):
    changes = {}
    if req.location in CITIES:
        changes["location"] = req.location
    if req.timezone:
        changes["timezone"] = req.timezone if req.timezone == "Asia/Seoul" else "Asia/Seoul"  # 지금은 한국 시간만
    store.save_settings(user.id, changes)
    return store.get_settings(user.id)


# ── 대화 ───────────────────────────────────────────────────────────

@app.get("/api/messages")
def messages(user: User = Depends(current_user)):
    show = settings.show_turn_panel
    return [{"id": m["id"], "role": m["role"], "content": m["content"],
             "meta": m["meta"] if show else {}, "created_at": m["created_at"].isoformat()}
            for m in store.recent_messages(user.id, 50)]


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request, user: User = Depends(current_user), model=Depends(get_llm)):
    message = req.message.strip()[:1000]
    if not message:
        raise HTTPException(400, "메시지가 비어 있습니다")

    # 크레딧: 답을 만들기 전에, 받자마자 서버에서 차감한다. 위기 표현은 차감하지 않고 안전 응답으로 보낸다.
    charge = None
    if not is_crisis(message):
        charge, balance = credits.charge_message(store, user.id, ip_hash(request))
        if charge is None:
            log("INFO", "크레딧 부족으로 메시지 거절")
            return JSONResponse(credits.NO_CREDITS, status_code=402)

    def events():
        try:
            if charge:
                yield json.dumps({"type": "credits", "balance": balance}) + "\n"
            for event in agent.run_turn(message, user.id, model, store, settings.show_turn_panel):
                yield json.dumps(event, ensure_ascii=False, default=str) + "\n"
        except Exception as error:
            log("ERROR", "대화 처리 실패", error=type(error).__name__)
            if charge and credits.refund_message(store, user.id, charge):
                log("INFO", "실패한 메시지 크레딧 되돌림")
            yield json.dumps({"type": "error", "message": "답을 만들지 못했어요. 잠시 뒤 다시 보내 주세요."}, ensure_ascii=False) + "\n"
        finally:
            tracing.flush()  # 응답을 닫기 전에 트레이스를 보낸다(Cloud Run 은 응답 뒤 CPU를 거의 주지 않는다)

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.post("/api/greeting")
def greeting(user: User = Depends(current_user), model=Depends(get_llm)):
    try:
        return {"greeting": agent.greet(user.id, model, store)}
    finally:
        tracing.flush()


@app.post("/api/ocr")
async def read_photo(photo: UploadFile, user: User = Depends(current_user)):
    if not settings.ocr_enabled:
        raise HTTPException(404, "사진 글자 읽기가 꺼져 있습니다")
    try:
        # 글자 읽기는 몇 초 걸릴 수 있어, 다른 사용자의 대화가 멈추지 않도록 별도 스레드에서 돌린다
        data = await photo.read()
        return {"text": await run_in_threadpool(ocr.read_text, data, settings.ocr_engine)}
    except ocr.OcrError as error:
        raise HTTPException(500, str(error)) from error


@app.post("/api/pdf")
async def read_pdf(file: UploadFile, user: User = Depends(current_user)):
    if not settings.pdf_enabled:
        raise HTTPException(404, "PDF 글자 읽기가 꺼져 있습니다")
    try:
        return {"text": pdf.read_text(await file.read())}
    except pdf.PdfError as error:
        raise HTTPException(500, str(error)) from error


def my_answer(uid, message_id):
    """로그인한 사용자의 답(assistant 메시지)만 꺼낸다. 남의 메시지나 없는 메시지면 404."""
    message = store.get_message(uid, message_id)
    if not message or message["role"] != "assistant":
        raise HTTPException(404, "메시지를 찾지 못했습니다")
    return message


@app.post("/api/feedback")
def feedback(req: FeedbackRequest, user: User = Depends(current_user)):
    # 좋아요 1, 별로예요 0. 트레이스 ID는 화면이 아니라 서버에 저장된 meta 에서 꺼낸다
    message = my_answer(user.id, req.message_id)
    tracing.score(message["meta"].get("trace_id"), "user-feedback", 1 if req.liked else 0)
    tracing.flush()
    log("INFO", "답변 반응", message_id=req.message_id, liked=req.liked)
    return {"ok": True}


@app.post("/api/report")
def report(req: ReportRequest, user: User = Depends(current_user)):
    # 신고 1 과 사유(comment)를 Langfuse 점수로 보낸다. 서버 로그에는 사유 원문 대신 길이만 남긴다
    message = my_answer(user.id, req.message_id)
    tracing.score(message["meta"].get("trace_id"), "report", 1, comment=req.reason)
    tracing.flush()
    log("WARNING", "부적절한 답변 신고", message_id=req.message_id, reason_length=len(req.reason))
    return {"ok": True}


# ── 기억, 약속, 내 데이터 ───────────────────────────────────────────

@app.get("/api/memories")
def list_memories(user: User = Depends(current_user)):
    return [{"id": m["id"], "kind": m["kind"], "text": m["text"]} for m in store.list_memories(user.id)]


@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: int, user: User = Depends(current_user)):
    store.delete_memory(user.id, memory_id)
    return {"ok": True}


@app.delete("/api/memories")
def clear_memories(user: User = Depends(current_user)):
    store.clear_memories(user.id)
    return {"ok": True}


@app.get("/api/reminders")
def list_reminders(user: User = Depends(current_user)):
    tz = when.now_in(store.get_settings(user.id)["timezone"]).tzinfo
    return [{"id": r["id"], "text": r["text"], "status": r["status"],
             "due_label": when.label(r["due_at"].astimezone(tz))} for r in store.list_reminders(user.id)]


@app.delete("/api/reminders/{reminder_id}")
def cancel_reminder(reminder_id: int, user: User = Depends(current_user)):
    store.cancel_reminder(user.id, reminder_id)
    return {"ok": True}


@app.delete("/api/data")
def delete_data(user: User = Depends(current_user)):
    store.delete_all(user.id)
    log("INFO", "사용자 데이터 전체 삭제")
    return {"ok": True}
