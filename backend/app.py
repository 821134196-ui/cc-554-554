"""FastAPI 应用：REST API + 静态前端托管。"""
from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import queries, services
from .database import engine, get_session
from .errors import BusinessError, ConflictError
from .mock_services import payment_gateway, wind_service
from .models import Base

Base.metadata.create_all(engine)

app = FastAPI(title="景区缆车停运退票与候补改签系统", version="1.0")


@app.exception_handler(BusinessError)
async def _biz_handler(_req: Request, exc: BusinessError):
    return JSONResponse(status_code=400,
                        content={"ok": False, "error": exc.message, "code": exc.code})


@app.exception_handler(ConflictError)
async def _conflict_handler(_req: Request, exc: ConflictError):
    return JSONResponse(status_code=409,
                        content={"ok": False, "error": exc.message, "code": exc.code})


# ----------------------------------------------------------------  schemas

class WindIn(BaseModel):
    speed: float = Field(ge=0, le=60)


class RefundIn(BaseModel):
    operator: str = "tourist"


class RebookIn(BaseModel):
    target_slot_id: int
    operator: str = "tourist"


class WaitlistIn(BaseModel):
    target_slot_id: int
    operator: str = "tourist"


class CheckInIn(BaseModel):
    ticket_no: str


class CapacityIn(BaseModel):
    capacity: int = Field(ge=0, le=10000)
    operator: str = "dispatcher"


class FillIn(BaseModel):
    slot_id: int | None = None
    operator: str = "dispatcher"


class EventIn(BaseModel):
    reason: str
    affected_slot_ids: list[int]
    detail: str = ""
    created_by: str = "ops"


class ResumeIn(BaseModel):
    confirmer: str
    role: str  # 必须为 equipment_owner
    remark: str = ""


class CallbackIn(BaseModel):
    status: str  # succeeded / failed
    gateway_ref: str | None = None


class PayConfigIn(BaseModel):
    callback_delay: float | None = Field(default=None, ge=0, le=3600)
    auto_callback: bool | None = None
    fail_next: bool | None = None


class DeliverIn(BaseModel):
    status: str
    payment_no: str | None = None


# ----------------------------------------------------------------  模拟接口

@app.get("/api/meta/wind")
def get_wind():
    return wind_service.snapshot()


@app.post("/api/mock/wind")
def set_wind(body: WindIn):
    speed = wind_service.set(body.speed)
    return wind_service.snapshot() | {"set": speed}


@app.get("/api/mock/payment/config")
def pay_config():
    return payment_gateway.config_snapshot()


@app.post("/api/mock/payment/config")
def set_pay_config(body: PayConfigIn):
    return payment_gateway.configure(
        callback_delay=body.callback_delay,
        auto_callback=body.auto_callback,
        fail_next=body.fail_next)


@app.post("/api/mock/payment/deliver")
def manual_deliver(body: DeliverIn):
    if not body.payment_no:
        raise BusinessError("需要 payment_no", "bad_request")
    payment_gateway.deliver_now(body.payment_no, body.status)
    return {"ok": True, "payment_no": body.payment_no, "status": body.status}


# ----------------------------------------------------------------  班次 / 订单

@app.get("/api/slots")
def slots(db: Session = Depends(get_session)):
    return {"slots": queries.list_slots(db)}


@app.get("/api/orders")
def orders(db: Session = Depends(get_session)):
    return {"orders": queries.list_orders_brief(db)}


@app.get("/api/orders/{order_no}")
def order_detail(order_no: str, db: Session = Depends(get_session)):
    data = queries.get_order(db, order_no)
    if not data:
        raise BusinessError("订单不存在", "not_found")
    return data


@app.get("/api/tickets/{ticket_id}/actions")
def ticket_timeline(ticket_id: int, db: Session = Depends(get_session)):
    return {"actions": queries.ticket_actions(db, ticket_id)}


# ----------------------------------------------------------------  游客处理

@app.post("/api/tickets/{ticket_id}/refund")
def refund(ticket_id: int, body: RefundIn):
    return services.request_refund(ticket_id, operator=body.operator)


@app.post("/api/tickets/{ticket_id}/rebook")
def rebook(ticket_id: int, body: RebookIn):
    return services.rebook_ticket(ticket_id, body.target_slot_id, operator=body.operator)


@app.post("/api/tickets/{ticket_id}/waitlist")
def waitlist(ticket_id: int, body: WaitlistIn):
    return services.join_waitlist(ticket_id, body.target_slot_id, operator=body.operator)


@app.post("/api/tickets/{ticket_id}/waitlist/cancel")
def waitlist_cancel(ticket_id: int, body: RefundIn):
    return services.cancel_waitlist(ticket_id, operator=body.operator)


# ----------------------------------------------------------------  闸机

@app.post("/api/gate/checkin")
def gate_checkin(body: CheckInIn):
    return services.check_in(body.ticket_no.strip())


# ----------------------------------------------------------------  调度

@app.get("/api/dashboard")
def dispatch_dashboard(db: Session = Depends(get_session)):
    return queries.dashboard(db)


@app.post("/api/dispatch/slots/{slot_id}/capacity")
def set_slot_capacity(slot_id: int, body: CapacityIn):
    return services.set_capacity(slot_id, body.capacity, operator=body.operator)


@app.post("/api/dispatch/fill")
def fill_one(body: FillIn):
    result = services.fill_next_waitlist(body.slot_id, operator=body.operator)
    return {"filled": result, "count": 1 if result else 0}


@app.post("/api/dispatch/auto-fill")
def auto_fill(body: FillIn):
    return services.auto_fill(operator=body.operator)


# ----------------------------------------------------------------  支付回执

@app.post("/api/payments/{payment_no}/callback")
def payment_callback(payment_no: str, body: CallbackIn):
    return services.apply_callback(payment_no, body.status, body.gateway_ref)


@app.post("/api/payments/{payment_no}/retry")
def payment_retry(payment_no: str):
    return services.retry_refund(payment_no)


# ----------------------------------------------------------------  停运事件

@app.get("/api/events")
def events(db: Session = Depends(get_session)):
    return {"events": queries.list_events(db)}


@app.post("/api/events")
def create_event(body: EventIn):
    return services.create_event(body.reason, body.affected_slot_ids,
                                 detail=body.detail, created_by=body.created_by)


@app.post("/api/events/{event_id}/resume")
def resume_event(event_id: int, body: ResumeIn):
    return services.resume_event(event_id, body.confirmer, body.role, body.remark)


@app.get("/api/health")
def health():
    return {"ok": True}


# ----------------------------------------------------------------  静态前端

_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.isdir(_FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
