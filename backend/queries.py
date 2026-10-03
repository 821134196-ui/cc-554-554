"""只读查询与序列化（走 WAL 自动提交，永远读到最新已提交数据）。"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    Order,
    Payment,
    SeatAllocation,
    Slot,
    SuspensionEvent,
    Ticket,
    TicketAction,
    WaitlistEntry,
)

TICKET_STATUS_CN = {
    "unused": "未使用",
    "checked_in": "已过闸",
    "waitlisted": "候补中",
    "reassigned": "已改签(旧票)",
    "refunded": "已退款",
}


def _active_event(db: Session) -> SuspensionEvent | None:
    return db.scalar(select(SuspensionEvent).where(SuspensionEvent.status == "active")
                     .order_by(SuspensionEvent.id.desc()))


def slot_occupancy(db: Session) -> dict[int, int]:
    rows = db.execute(select(SeatAllocation.slot_id, func.count(SeatAllocation.id))
                      .group_by(SeatAllocation.slot_id)).all()
    return {slot_id: cnt for slot_id, cnt in rows}


def slot_dict(db: Session, s: Slot, occ: dict[int, int] | None = None,
              active_ev: SuspensionEvent | None = None) -> dict:
    if occ is None:
        occ = slot_occupancy(db)
    used = occ.get(s.id, 0)
    suspended = bool(active_ev and s.id in (active_ev.affected_slot_ids or []))
    return {
        "id": s.id, "code": s.code, "start_time": s.start_time,
        "end_time": s.end_time, "capacity": s.capacity,
        "occupied": used, "remaining": s.capacity - used,
        "suspended": suspended, "note": s.note,
        "status": "停运中" if suspended else "运行中",
    }


def list_slots(db: Session) -> list[dict]:
    active_ev = _active_event(db)
    occ = slot_occupancy(db)
    slots = list(db.scalars(select(Slot).order_by(Slot.start_time)))
    return [slot_dict(db, s, occ, active_ev) for s in slots]


def _latest_payment(db: Session, ticket_id: int) -> dict | None:
    p = db.scalar(select(Payment).where(Payment.ticket_id == ticket_id)
                  .order_by(Payment.id.desc()))
    if not p:
        return None
    return {"payment_no": p.payment_no, "kind": p.kind, "amount": p.amount,
            "gateway_status": p.gateway_status, "applied": p.applied,
            "callback_at": p.callback_at.isoformat() if p.callback_at else None}


def _waitlist_state(db: Session, ticket_id: int) -> dict | None:
    w = db.scalar(select(WaitlistEntry).where(WaitlistEntry.ticket_id == ticket_id)
                  .order_by(WaitlistEntry.id.desc()))
    if not w:
        return None
    return {"id": w.id, "state": w.state, "position": w.position,
            "target_slot_id": w.target_slot_id}


def ticket_dict(db: Session, t: Ticket, active_ev: SuspensionEvent | None,
                slots_by_id: dict[int, Slot], occ: dict[int, int] | None = None,
                events_by_id: dict[int, SuspensionEvent] | None = None) -> dict:
    if occ is None:
        occ = slot_occupancy(db)
    events_by_id = events_by_id or {}
    slot = slots_by_id.get(t.slot_id)
    own_ev = events_by_id.get(t.event_id) if t.event_id else None
    # 票属于其被关联的那次停运事件范围（事件恢复后仍可继续处理）
    in_scope = bool(own_ev and t.slot_id in (own_ev.affected_slot_ids or []))
    can_handle = (not t.checked_in and t.status == "unused" and in_scope)
    # 可改签/候补目标：未停运、且不是原班次
    options = [
        {"slot_id": s.id, "code": s.code, "start_time": s.start_time,
         "end_time": s.end_time, "remaining": s.capacity - occ.get(s.id, 0)}
        for s in slots_by_id.values()
        if not (active_ev and s.id in (active_ev.affected_slot_ids or []))
        and s.id != t.slot_id
    ]
    return {
        "id": t.id, "ticket_no": t.ticket_no, "passenger_name": t.passenger_name,
        "slot_id": t.slot_id,
        "slot_code": slot.code if slot else "?",
        "slot_time": f"{slot.start_time}-{slot.end_time}" if slot else "",
        "price": t.price,
        "checked_in": t.checked_in,
        "checked_in_at": t.checked_in_at.isoformat() if t.checked_in_at else None,
        "status": t.status,
        "status_cn": TICKET_STATUS_CN.get(t.status, t.status),
        "in_suspension_scope": in_scope,
        "can_handle": can_handle,
        "event_id": t.event_id,
        "source_ticket_id": t.source_ticket_id,
        "handled_at": t.handled_at.isoformat() if t.handled_at else None,
        "payment": _latest_payment(db, t.id),
        "waitlist": _waitlist_state(db, t.id),
        "options": options,
    }


def get_order(db: Session, order_no: str) -> dict | None:
    o = db.scalar(select(Order).where(Order.order_no == order_no))
    if not o:
        return None
    active_ev = _active_event(db)
    slots = {s.id: s for s in db.scalars(select(Slot))}
    events = {e.id: e for e in db.scalars(select(SuspensionEvent))}
    occ = slot_occupancy(db)
    tickets = [ticket_dict(db, t, active_ev, slots, occ, events) for t in
               sorted(o.tickets, key=lambda x: x.id)]
    return {
        "order_no": o.order_no, "contact_name": o.contact_name,
        "contact_phone": o.contact_phone,
        "created_at": o.created_at.isoformat(),
        "active_event": event_brief(active_ev) if active_ev else None,
        "tickets": tickets,
    }


def event_brief(ev: SuspensionEvent) -> dict:
    return {
        "id": ev.id, "reason": ev.reason,
        "reason_cn": {"weather": "天气原因", "equipment": "设备原因"}.get(ev.reason, ev.reason),
        "detail": ev.detail, "status": ev.status,
        "affected_slot_ids": ev.affected_slot_ids or [],
        "wind_speed": ev.wind_speed,
        "created_at": ev.created_at.isoformat(),
        "resumed_at": ev.resumed_at.isoformat() if ev.resumed_at else None,
        "resumed_by": ev.resumed_by,
    }


def list_events(db: Session) -> list[dict]:
    evs = db.scalars(select(SuspensionEvent).order_by(SuspensionEvent.id.desc())).all()
    return [event_brief(e) for e in evs]


def dashboard(db: Session) -> dict:
    active_ev = _active_event(db)
    occ = slot_occupancy(db)
    slots = list(db.scalars(select(Slot).order_by(Slot.start_time)))
    slot_dicts = [slot_dict(db, s, occ, active_ev) for s in slots]
    slots_by_id = {s.id: s for s in slots}

    # 候补队列，按目标班次分组，FIFO 排序
    waits = db.scalars(select(WaitlistEntry).where(WaitlistEntry.state == "waiting")
                       .order_by(WaitlistEntry.target_slot_id,
                                 WaitlistEntry.created_at, WaitlistEntry.id)).all()
    wait_by_slot: dict[int, list] = {}
    for w in waits:
        t = db.get(Ticket, w.ticket_id)
        wait_by_slot.setdefault(w.target_slot_id, []).append({
            "entry_id": w.id, "ticket_id": w.ticket_id,
            "ticket_no": t.ticket_no if t else "?",
            "passenger_name": t.passenger_name if t else "?",
            "position": w.position,
            "created_at": w.created_at.isoformat(),
        })

    pending_pays = db.scalars(select(Payment).where(Payment.gateway_status == "pending")
                              .order_by(Payment.id)).all()
    return {
        "active_event": event_brief(active_ev) if active_ev else None,
        "slots": slot_dicts,
        "waitlist_by_slot": wait_by_slot,
        "waitlist_total": len(waits),
        "pending_payments": [
            {"payment_no": p.payment_no, "ticket_id": p.ticket_id,
             "kind": p.kind, "amount": p.amount} for p in pending_pays
        ],
    }


def ticket_actions(db: Session, ticket_id: int) -> list[dict]:
    rows = db.scalars(select(TicketAction).where(TicketAction.ticket_id == ticket_id)
                      .order_by(TicketAction.id)).all()
    return [{"id": a.id, "action": a.action, "detail": a.detail,
             "operator": a.operator, "event_id": a.event_id,
             "created_at": a.created_at.isoformat()} for a in rows]


def list_orders_brief(db: Session) -> list[dict]:
    orders = db.scalars(select(Order).order_by(Order.id)).all()
    return [{"order_no": o.order_no, "contact_name": o.contact_name,
             "ticket_count": len(o.tickets)} for o in orders]
