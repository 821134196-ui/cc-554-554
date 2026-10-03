"""本地模拟接口：风速、闸机核销、支付回执。"""
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import PaymentCallback, TicketAction
from ..schemas import GateVerifyRequest, PaymentCallbackRequest, WindSet
from ..services import WIND_THRESHOLD, gate_verify, payment_callback

router = APIRouter(prefix="/mock", tags=["模拟接口"])

# 本地模拟的风速计（进程内状态）
_wind = {"speed": 6.2, "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds")}


@router.get("/wind")
def get_wind():
    return {
        "speed": _wind["speed"],
        "threshold": WIND_THRESHOLD,
        "exceeded": _wind["speed"] >= WIND_THRESHOLD,
        "updated_at": _wind["updated_at"],
    }


@router.post("/wind")
def set_wind(req: WindSet):
    _wind["speed"] = req.speed
    _wind["updated_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
    return get_wind()


@router.post("/gate/verify")
def gate(req: GateVerifyRequest, db: Session = Depends(get_db)):
    """模拟闸机核销：票变为「已过闸」，之后不能按未使用票退款。"""
    t = gate_verify(db, ticket_code=req.ticket_code, gate_id=req.gate_id)
    return {"ticket_code": t.ticket_code, "status": t.status,
            "gate_verified_at": t.gate_verified_at.isoformat(sep=" ", timespec="seconds")}


@router.post("/payment/callback")
def payment(req: PaymentCallbackRequest, db: Session = Depends(get_db)):
    """模拟支付网关回执（可重复、可晚到）。系统保证幂等且不推翻已确定结果。"""
    cb, fresh = payment_callback(db, callback_id=req.callback_id, txn=req.txn,
                                 result=req.result, note=req.note)
    return {
        "fresh": fresh, "applied": cb.applied, "note": cb.note,
        "callback_id": cb.callback_id, "txn": cb.txn, "result": cb.result,
    }


@router.get("/payment/pending")
def pending_refunds(db: Session = Depends(get_db)):
    """等待支付回执的退款单（供模拟网关页面选择）。"""
    actions = db.scalars(
        select(TicketAction).where(
            TicketAction.action_type == "refund", TicketAction.status == "pending"
        ).order_by(TicketAction.id)
    ).all()
    return {"pending": [{
        "refund_txn": a.refund_txn,
        "ticket_code": a.ticket.ticket_code,
        "passenger_name": a.ticket.passenger_name,
        "amount": a.ticket.price,
        "created_at": a.created_at.isoformat(sep=" ", timespec="seconds"),
    } for a in actions]}


@router.get("/payment/callbacks")
def list_callbacks(db: Session = Depends(get_db)):
    cbs = db.scalars(select(PaymentCallback).order_by(PaymentCallback.id.desc()).limit(50)).all()
    return {"callbacks": [{
        "callback_id": c.callback_id, "txn": c.txn, "result": c.result,
        "applied": c.applied, "note": c.note,
        "received_at": c.received_at.isoformat(sep=" ", timespec="seconds"),
    } for c in cbs]}
