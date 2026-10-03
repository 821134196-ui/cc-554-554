"""业务逻辑层。

一致性设计：
- 所有写操作在 BEGIN IMMEDIATE 事务内串行执行（见 database.py）。
- 名额占用一律使用条件 UPDATE（... WHERE sold < capacity）并校验 rowcount，
  两名调度员同时递补同一个名额时，只有一人能更新成功。
- 同一张票的处理通过 (ticket_id, event_id) 唯一约束 + 状态前置校验双重防护。
- 支付回执按 callback_id 幂等；处理单已确定（非 pending）时回执只记录不生效，
  晚到回执不会推翻已确定的结果。
"""
import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (AuditLog, EventSlot, Order, PaymentCallback, Staff,
                     SuspensionEvent, Ticket, TicketAction, TimeSlot,
                     WaitlistEntry)

TICKET_PRICE = 180
WIND_THRESHOLD = 15.0  # m/s，达到即建议停运

STATUS_LABELS = {
    "active": "有效",
    "used": "已过闸",
    "refund_pending": "退款中",
    "refunded": "已退款",
    "waitlisted": "候补中",
}


class BizError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "BIZ_ERROR"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def _audit(db: Session, actor: str, action: str, detail: str):
    db.add(AuditLog(actor=actor, action=action, detail=detail))


def _slot_desc(slot: TimeSlot) -> str:
    return f"{slot.date} {slot.start_time}-{slot.end_time}"


def _occupy_seat(db: Session, slot_id: int) -> bool:
    """原子占用一个名额；无空余时返回 False（并发安全的核心）。"""
    res = db.execute(
        update(TimeSlot)
        .where(TimeSlot.id == slot_id, TimeSlot.sold < TimeSlot.capacity)
        .values(sold=TimeSlot.sold + 1)
    )
    return res.rowcount == 1


def _release_seat(db: Session, slot_id: int):
    db.execute(
        update(TimeSlot)
        .where(TimeSlot.id == slot_id, TimeSlot.sold > 0)
        .values(sold=TimeSlot.sold - 1)
    )


# ---------------------------------------------------------------- 停运事件

def create_event(db: Session, *, title, reason, event_date, slot_ids, description, operator):
    slots = db.scalars(select(TimeSlot).where(TimeSlot.id.in_(slot_ids))).all()
    if len(slots) != len(set(slot_ids)):
        raise BizError("部分班次不存在", 404)
    ev = SuspensionEvent(
        title=title, reason=reason, event_date=event_date,
        description=description, status="active",
    )
    db.add(ev)
    db.flush()
    for s in slots:
        db.add(EventSlot(event_id=ev.id, slot_id=s.id))
        s.status = "suspended"
    _audit(db, operator, "创建停运事件",
           f"事件#{ev.id}《{title}》影响 {len(slots)} 个班次")
    db.commit()
    return ev


def resume_event(db: Session, *, event_id, manager_name, confirm):
    """恢复运营：必须由在册设备负责人显式确认。"""
    ev = db.get(SuspensionEvent, event_id)
    if not ev:
        raise BizError("事件不存在", 404)
    if ev.status != "active":
        raise BizError("该事件已恢复，请勿重复操作", 409)
    if not confirm:
        raise BizError("恢复运营必须由设备负责人勾选确认", 400)
    mgr = db.scalar(
        select(Staff).where(Staff.name == manager_name, Staff.role == "equipment_manager")
    )
    if not mgr:
        raise BizError("确认人不是在册设备负责人，无权确认恢复", 403)

    ev.status = "resolved"
    ev.resolved_at = datetime.now()
    ev.resumed_by = manager_name
    slot_ids = db.scalars(select(EventSlot.slot_id).where(EventSlot.event_id == ev.id)).all()
    db.execute(update(TimeSlot).where(TimeSlot.id.in_(slot_ids)).values(status="normal"))
    _audit(db, manager_name, "恢复运营", f"事件#{ev.id}《{ev.title}》恢复，{len(slot_ids)} 个班次复运")
    db.commit()
    return ev


# ---------------------------------------------------------------- 游客处理选择

def _active_event_for_slot(db: Session, slot_id: int):
    return db.scalar(
        select(SuspensionEvent)
        .join(EventSlot, EventSlot.event_id == SuspensionEvent.id)
        .where(EventSlot.slot_id == slot_id, SuspensionEvent.status == "active")
    )


def choose_action(db: Session, *, ticket_id, action_type, idempotency_key,
                  target_slot_id=None, operator="游客"):
    """游客对一张受影响车票选择 退款/换时段/候补。幂等。"""
    # 幂等重放：同一 idempotency_key 直接返回首次结果
    existing = db.scalar(
        select(TicketAction).where(TicketAction.idempotency_key == idempotency_key)
    )
    if existing:
        return existing, False

    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        raise BizError("车票不存在", 404)

    if ticket.status == "used":
        raise BizError("该票已过闸核销，不能按未使用票办理退款/改签", 409)
    if ticket.status in ("refund_pending", "refunded", "waitlisted"):
        raise BizError(f"该票已提交处理（{STATUS_LABELS[ticket.status]}），请勿重复操作", 409)

    # 是否已在某个进行中的停运事件里处理过（改签后原班次已不在事件内也能拦下）
    prior = db.scalar(
        select(TicketAction)
        .join(SuspensionEvent, SuspensionEvent.id == TicketAction.event_id)
        .where(TicketAction.ticket_id == ticket.id, SuspensionEvent.status == "active")
    )
    if prior:
        raise BizError("该票在本次停运中已有处理记录，不能既退款又改签", 409)

    ev = _active_event_for_slot(db, ticket.slot_id)
    if not ev:
        raise BizError("该票班次当前未处于停运事件中，无需处理", 409)

    if action_type == "refund":
        action = _do_refund(db, ticket, ev, idempotency_key, operator)
    elif action_type == "rebook":
        action = _do_rebook(db, ticket, ev, idempotency_key, target_slot_id, operator)
    elif action_type == "waitlist":
        action = _do_waitlist(db, ticket, ev, idempotency_key, target_slot_id, operator)
    else:
        raise BizError("不支持的处理方式", 400)

    db.flush()
    try:
        db.commit()
    except IntegrityError:
        # 极端时序下唯一约束兜底（幂等键或 票-事件 唯一）：重放已有结果而非报错
        db.rollback()
        existing = db.scalar(
            select(TicketAction).where(TicketAction.idempotency_key == idempotency_key)
        )
        if existing:
            return existing, False
        raise BizError("该票在本次停运中已有处理记录，不能既退款又改签", 409)
    return action, True


def _do_refund(db, ticket, ev, idem, operator):
    ticket.status = "refund_pending"
    txn = "RF" + uuid.uuid4().hex[:16].upper()
    action = TicketAction(
        ticket_id=ticket.id, event_id=ev.id, action_type="refund", status="pending",
        refund_txn=txn, idempotency_key=idem, operator=operator,
        note=f"退款单 {txn} 已提交支付网关，等待回执",
    )
    db.add(action)
    _audit(db, operator, "申请退款",
           f"票 {ticket.ticket_code}（{ticket.passenger_name}）退款单 {txn}")
    return action


def _do_rebook(db, ticket, ev, idem, target_slot_id, operator):
    if not target_slot_id:
        raise BizError("请选择要改签的时段", 400)
    target = db.get(TimeSlot, target_slot_id)
    if not target:
        raise BizError("目标时段不存在", 404)
    if target.id == ticket.slot_id:
        raise BizError("不能改签至原时段", 400)
    if target.status != "normal":
        raise BizError("目标时段处于停运状态，不可改签", 409)
    if not _occupy_seat(db, target.id):
        raise BizError("目标时段名额已满，请选择其他时段或加入候补", 409)

    old_desc = _slot_desc(ticket.slot)
    _release_seat(db, ticket.slot_id)
    ticket.slot_id = target.id  # 状态保持 active，可直接过闸
    action = TicketAction(
        ticket_id=ticket.id, event_id=ev.id, action_type="rebook", status="confirmed",
        target_slot_id=target.id, idempotency_key=idem, operator=operator,
        note=f"由 {old_desc} 改签至 {_slot_desc(target)}",
    )
    db.add(action)
    _audit(db, operator, "改签",
           f"票 {ticket.ticket_code}（{ticket.passenger_name}）{old_desc} → {_slot_desc(target)}")
    return action


def _do_waitlist(db, ticket, ev, idem, desired_slot_id, operator):
    if not desired_slot_id:
        raise BizError("请选择候补的期望时段", 400)
    desired = db.get(TimeSlot, desired_slot_id)
    if not desired:
        raise BizError("期望时段不存在", 404)

    pos = (db.scalar(
        select(func.max(WaitlistEntry.position)).where(WaitlistEntry.event_id == ev.id)
    ) or 0) + 1
    entry = WaitlistEntry(
        ticket_id=ticket.id, event_id=ev.id, desired_slot_id=desired.id,
        position=pos, status="waiting",
    )
    db.add(entry)
    ticket.status = "waitlisted"
    action = TicketAction(
        ticket_id=ticket.id, event_id=ev.id, action_type="waitlist", status="pending",
        target_slot_id=desired.id, idempotency_key=idem, operator=operator,
        note=f"候补 {_slot_desc(desired)}，排队第 {pos} 位",
    )
    db.add(action)
    _audit(db, operator, "加入候补",
           f"票 {ticket.ticket_code}（{ticket.passenger_name}）候补 {_slot_desc(desired)} 第 {pos} 位")
    return action


# ---------------------------------------------------------------- 调度递补

def _promote_entry(db, entry: WaitlistEntry, slot: TimeSlot, operator: str):
    """在已占用名额成功的前提下，把候补票迁入目标时段。调用方须保证在事务内。"""
    ticket = db.get(Ticket, entry.ticket_id)
    _release_seat(db, ticket.slot_id)
    ticket.slot_id = slot.id
    ticket.status = "active"
    entry.status = "promoted"
    entry.promoted_at = datetime.now()
    entry.promoted_by = operator
    action = db.scalar(
        select(TicketAction).where(
            TicketAction.ticket_id == ticket.id, TicketAction.event_id == entry.event_id
        )
    )
    if action:
        action.status = "confirmed"
        action.target_slot_id = slot.id
        action.note = f"候补递补至 {_slot_desc(slot)}（调度员 {operator}）"
    _audit(db, operator, "候补递补",
           f"票 {ticket.ticket_code}（{ticket.passenger_name}）递补至 {_slot_desc(slot)}")
    db.flush()  # 立即落库：auto_promote 循环内的下一次查询必须看到已更新的状态


def promote_waitlist(db: Session, *, entry_id, slot_id, operator):
    """调度员把一条候补递补到指定时段。并发安全：名额占用是条件更新。"""
    entry = db.get(WaitlistEntry, entry_id)
    if not entry:
        raise BizError("候补记录不存在", 404)
    if entry.status != "waiting":
        raise BizError("该候补已处理，不能重复递补", 409)
    slot = db.get(TimeSlot, slot_id)
    if not slot:
        raise BizError("目标时段不存在", 404)
    if slot.status != "normal":
        raise BizError("目标时段仍处于停运状态，不能递补", 409)
    if not _occupy_seat(db, slot.id):
        raise BizError("目标时段名额已满，无法递补", 409)

    _promote_entry(db, entry, slot, operator)
    db.commit()
    return entry


def auto_promote(db: Session, *, event_id, slot_id, operator):
    """按排队顺序把该时段的候补自动递补进空余名额（一个事务内完成）。"""
    slot = db.get(TimeSlot, slot_id)
    if not slot:
        raise BizError("目标时段不存在", 404)
    if slot.status != "normal":
        raise BizError("目标时段仍处于停运状态，不能递补", 409)

    promoted = []
    while True:
        entry = db.scalar(
            select(WaitlistEntry)
            .where(WaitlistEntry.event_id == event_id,
                   WaitlistEntry.desired_slot_id == slot_id,
                   WaitlistEntry.status == "waiting")
            .order_by(WaitlistEntry.position)
            .limit(1)
        )
        if not entry:
            break
        if not _occupy_seat(db, slot.id):
            break  # 空余运力耗尽，候补不得超售
        _promote_entry(db, entry, slot, operator)
        promoted.append(entry.id)
    db.commit()
    return promoted


# ---------------------------------------------------------------- 支付回执

def payment_callback(db: Session, *, callback_id, txn, result, note=""):
    """支付网关回执。幂等 + 状态机守卫：已确定的结果不被晚到回执推翻。"""
    dup = db.scalar(select(PaymentCallback).where(PaymentCallback.callback_id == callback_id))
    if dup:
        return dup, False  # 同一回执重复送达：直接忽略

    action = db.scalar(select(TicketAction).where(TicketAction.refund_txn == txn))
    if not action:
        cb = PaymentCallback(callback_id=callback_id, txn=txn, result=result,
                             applied=False, note="未知交易号，回执已记录未生效")
        db.add(cb)
        db.commit()
        return cb, True

    if action.status != "pending":
        # 晚到回执：处理单已确定（confirmed/rejected），只记录、不推翻
        cb = PaymentCallback(
            callback_id=callback_id, txn=txn, result=result, applied=False,
            note=f"回执晚到：处理单已处于 {action.status} 状态，结果维持不变",
        )
        db.add(cb)
        _audit(db, "支付网关", "忽略晚到回执",
               f"退款单 {txn} 已 {action.status}，回执 {callback_id}（{result}）未生效")
        db.commit()
        return cb, True

    ticket = db.get(Ticket, action.ticket_id)
    if result == "success":
        action.status = "confirmed"
        action.note = f"退款成功（回执 {callback_id}）"
        ticket.status = "refunded"
        _release_seat(db, ticket.slot_id)  # 退款生效后释放名额
        _audit(db, "支付网关", "退款成功", f"票 {ticket.ticket_code} 退款单 {txn} 已到账")
    else:
        action.status = "rejected"
        action.note = f"退款失败（回执 {callback_id}），车票恢复有效"
        ticket.status = "active"
        _audit(db, "支付网关", "退款失败", f"票 {ticket.ticket_code} 退款单 {txn} 失败，票恢复有效")
    cb = PaymentCallback(callback_id=callback_id, txn=txn, result=result,
                         applied=True, note=note or "回执已生效")
    db.add(cb)
    db.commit()
    return cb, True


# ---------------------------------------------------------------- 闸机核销

def gate_verify(db: Session, *, ticket_code, gate_id="GATE-1"):
    t = db.scalar(select(Ticket).where(Ticket.ticket_code == ticket_code))
    if not t:
        raise BizError("票号不存在", 404)
    if t.status == "used":
        raise BizError("该票已核销，请勿重复过闸", 409)
    if t.status != "active":
        raise BizError(f"该票状态为「{STATUS_LABELS.get(t.status, t.status)}」，不能过闸", 409)
    t.status = "used"
    t.gate_verified_at = datetime.now()
    _audit(db, gate_id, "闸机核销", f"票 {t.ticket_code}（{t.passenger_name}）过闸")
    db.commit()
    return t


# ---------------------------------------------------------------- 查询视图

def slot_dict(s: TimeSlot, affected_ids: set[int] | None = None):
    return {
        "id": s.id, "date": s.date, "start_time": s.start_time, "end_time": s.end_time,
        "capacity": s.capacity, "sold": s.sold, "remaining": s.remaining,
        "status": s.status, "affected": bool(affected_ids and s.id in affected_ids),
    }


def get_order_view(db: Session, order_no: str):
    order = db.scalar(select(Order).where(Order.order_no == order_no))
    if not order:
        raise BizError("订单不存在，请核对订单号", 404)
    tickets = db.scalars(
        select(Ticket).where(Ticket.order_id == order.id).order_by(Ticket.id)
    ).all()
    out = []
    for t in tickets:
        ev = _active_event_for_slot(db, t.slot_id)
        action = None
        waitlist = None
        if ev:
            action = db.scalar(
                select(TicketAction).where(
                    TicketAction.ticket_id == t.id, TicketAction.event_id == ev.id
                )
            )
            waitlist = db.scalar(
                select(WaitlistEntry).where(
                    WaitlistEntry.ticket_id == t.id, WaitlistEntry.event_id == ev.id
                )
            )
        out.append({
            "id": t.id, "ticket_code": t.ticket_code,
            "passenger_name": t.passenger_name, "price": t.price,
            "status": t.status, "status_label": STATUS_LABELS.get(t.status, t.status),
            "gate_verified_at": t.gate_verified_at.isoformat(sep=" ", timespec="seconds") if t.gate_verified_at else None,
            "slot": slot_dict(t.slot),
            "event": {"id": ev.id, "title": ev.title, "reason": ev.reason} if ev else None,
            "action": {
                "action_type": a.action_type, "status": a.status, "note": a.note,
                "refund_txn": a.refund_txn,
                "target_slot": slot_dict(a.target_slot) if a.target_slot else None,
            } if (a := action) else None,
            "waitlist": {
                "position": w.position, "status": w.status,
                "desired_slot": slot_dict(w.desired_slot),
            } if (w := waitlist) else None,
        })
    return {
        "order_no": order.order_no, "visitor_name": order.visitor_name,
        "phone": order.phone, "tickets": out,
    }


def get_board(db: Session, event_id: int):
    ev = db.get(SuspensionEvent, event_id)
    if not ev:
        raise BizError("事件不存在", 404)
    affected_ids = set(db.scalars(
        select(EventSlot.slot_id).where(EventSlot.event_id == ev.id)
    ).all())
    slots = db.scalars(
        select(TimeSlot).where(TimeSlot.date == ev.event_date).order_by(TimeSlot.start_time)
    ).all()
    entries = db.scalars(
        select(WaitlistEntry).where(WaitlistEntry.event_id == ev.id)
        .order_by(WaitlistEntry.position)
    ).all()
    actions = db.scalars(
        select(TicketAction).where(TicketAction.event_id == ev.id)
        .order_by(TicketAction.id.desc())
    ).all()
    return {
        "event": {
            "id": ev.id, "title": ev.title, "reason": ev.reason,
            "description": ev.description, "event_date": ev.event_date,
            "status": ev.status,
            "created_at": ev.created_at.isoformat(sep=" ", timespec="seconds"),
            "resolved_at": ev.resolved_at.isoformat(sep=" ", timespec="seconds") if ev.resolved_at else None,
            "resumed_by": ev.resumed_by,
        },
        "slots": [slot_dict(s, affected_ids) for s in slots],
        "waitlist": [{
            "id": w.id, "position": w.position, "status": w.status,
            "passenger_name": w.ticket.passenger_name,
            "ticket_code": w.ticket.ticket_code,
            "desired_slot": slot_dict(w.desired_slot),
            "promoted_by": w.promoted_by,
            "created_at": w.created_at.isoformat(sep=" ", timespec="seconds"),
        } for w in entries],
        "actions": [{
            "id": a.id, "ticket_code": a.ticket.ticket_code,
            "passenger_name": a.ticket.passenger_name,
            "action_type": a.action_type, "status": a.status,
            "note": a.note, "operator": a.operator, "refund_txn": a.refund_txn,
            "created_at": a.created_at.isoformat(sep=" ", timespec="seconds"),
        } for a in actions],
    }
