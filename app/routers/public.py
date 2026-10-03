"""游客端接口：订单查询、班次列表、处理选择。"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import SuspensionEvent, TimeSlot
from ..schemas import ActionRequest
from ..services import choose_action, get_order_view, slot_dict

router = APIRouter(tags=["游客"])


@router.get("/orders/{order_no}")
def order_view(order_no: str, db: Session = Depends(get_db)):
    return get_order_view(db, order_no)


@router.get("/slots")
def list_slots(db: Session = Depends(get_db)):
    slots = db.scalars(
        select(TimeSlot).order_by(TimeSlot.date, TimeSlot.start_time)
    ).all()
    return {"slots": [slot_dict(s) for s in slots]}


@router.get("/events/active")
def active_events(db: Session = Depends(get_db)):
    evs = db.scalars(
        select(SuspensionEvent).where(SuspensionEvent.status == "active")
        .order_by(SuspensionEvent.id.desc())
    ).all()
    return {"events": [{
        "id": e.id, "title": e.title, "reason": e.reason,
        "description": e.description, "event_date": e.event_date,
    } for e in evs]}


@router.post("/tickets/{ticket_id}/action", status_code=200)
def ticket_action(ticket_id: int, req: ActionRequest, db: Session = Depends(get_db)):
    """游客对一张票选择 退款/换时段/候补。携带幂等键，重试安全。"""
    action, created = choose_action(
        db,
        ticket_id=ticket_id,
        action_type=req.action_type,
        idempotency_key=req.idempotency_key,
        target_slot_id=req.target_slot_id,
        operator=req.operator,
    )
    return {
        "created": created,
        "action": {
            "id": action.id, "action_type": action.action_type,
            "status": action.status, "note": action.note,
            "refund_txn": action.refund_txn,
        },
    }
