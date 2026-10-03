"""核心业务服务层。

所有写操作都经过 database.run_tx（BEGIN IMMEDIATE 串行化）：
- 名额判断与占位写入在同一个写事务内，并发下不可能超售；
- 票状态在事务内读取并立刻流转，重复处理同一张票必然被拦截；
- seat_allocations(slot_id, seat_seq) 唯一约束是数据库层最后兜底。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import run_tx
from .errors import BusinessError, ConflictError
from .mock_services import payment_gateway, wind_service
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

EQUIPMENT_OWNER_ROLE = "equipment_owner"


def _utcnow() -> datetime:
    return datetime.utcnow()


def _log(db: Session, ticket_id: int, action: str, detail: dict | None = None,
         operator: str = "tourist", event_id: int | None = None):
    db.add(TicketAction(ticket_id=ticket_id, event_id=event_id, action=action,
                        detail=detail or {}, operator=operator))


def _get_ticket(db: Session, ticket_id: int) -> Ticket:
    t = db.get(Ticket, ticket_id)
    if not t:
        raise BusinessError("票不存在", "not_found")
    return t


def _active_event(db: Session) -> SuspensionEvent | None:
    return db.scalar(select(SuspensionEvent).where(SuspensionEvent.status == "active")
                     .order_by(SuspensionEvent.id.desc()))


def _latest_event(db: Session) -> SuspensionEvent | None:
    """最近一次停运事件（含已恢复）：决定票是否仍在“停运处理期”。"""
    return db.scalar(select(SuspensionEvent).order_by(SuspensionEvent.id.desc()))


def _require_affected_ticket(db: Session, ticket: Ticket) -> SuspensionEvent:
    """票必须属于其被关联的那次停运事件范围，且仍未处理，才能退款/改签/候补。

    - 事件恢复后游客仍可继续选择处理（调度员此时按恢复运力递补）；
    - 旧事件的票不会因后来一次新停运而失去处理资格；
    - 改签产生的新票目标班次不在停运范围，不能再次按停运票处理；
    - 目标班次是否停运只由当前“活动事件”决定（见 _target_open）。
    """
    ev = db.get(SuspensionEvent, ticket.event_id) if ticket.event_id else None
    if not ev or ticket.slot_id not in (ev.affected_slot_ids or []):
        raise BusinessError("该票不在停运范围内，无需停运处理", "not_affected")
    return ev


def _target_open(db: Session, slot_id: int) -> None:
    ev = _active_event(db)
    if ev and slot_id in (ev.affected_slot_ids or []):
        raise BusinessError("目标班次也在停运范围内", "target_suspended")


def _require_unused(ticket: Ticket):
    """票只能从“未使用”进入退款/改签/候补。"""
    if ticket.checked_in:
        raise BusinessError("该票已过闸核销，不能按未使用票退款或改签", "already_checked_in")
    if ticket.status == "refunded":
        raise ConflictError("该票已退款，不能重复处理", "already_refunded")
    if ticket.status == "reassigned":
        raise ConflictError("该票已改签，原票已失效，不能重复处理", "already_reassigned")
    if ticket.status == "waitlisted":
        raise ConflictError("该票已在候补队列中，请先撤回候补", "already_waitlisted")
    if ticket.status != "unused":
        raise ConflictError(f"该票当前状态为 {ticket.status}，不能重复处理", "bad_state")


def _occupied(db: Session, slot_id: int) -> int:
    return db.scalar(select(func.count(SeatAllocation.id))
                     .where(SeatAllocation.slot_id == slot_id)) or 0


def _allocate(db: Session, slot: Slot, ticket: Ticket) -> int:
    """在写事务内占用一个名额；超售时抛 slot_full。"""
    used = _occupied(db, slot.id)
    if used >= slot.capacity:
        raise ConflictError(f"班次 {slot.code} 名额已满", "slot_full")
    # 取最大序号 +1：退票/改签会留下序号空洞，count+1 可能与现存序号相撞。
    max_seq = db.scalar(select(func.coalesce(func.max(SeatAllocation.seat_seq), 0))
                        .where(SeatAllocation.slot_id == slot.id)) or 0
    seq = max_seq + 1
    try:
        db.add(SeatAllocation(slot_id=slot.id, seat_seq=seq, ticket_id=ticket.id))
        db.flush()
    except IntegrityError:
        # 唯一约束兜底：极端并发下同序号竞争
        raise ConflictError(f"班次 {slot.code} 名额竞争失败，请重试", "seat_race")
    return seq


def _release(db: Session, ticket_id: int):
    """释放票当前持有的名额（退款/改签旧票/入候补时调用）。"""
    rows = db.scalars(select(SeatAllocation).where(SeatAllocation.ticket_id == ticket_id)).all()
    for r in rows:
        db.delete(r)


def _new_ticket_no(old_no: str) -> str:
    return f"{old_no}-R{uuid.uuid4().hex[:6].upper()}"


def _issue_reassigned(db: Session, old: Ticket, target: Slot,
                      event_id: int | None, action: str,
                      operator: str, extra: dict | None = None) -> Ticket:
    """出票：旧票置为 reassigned（终态），生成新票并占用目标班次名额。"""
    new = Ticket(
        ticket_no=_new_ticket_no(old.ticket_no),
        order_id=old.order_id,
        passenger_name=old.passenger_name,
        slot_id=target.id,
        price=old.price,
        checked_in=False,
        status="unused",
        source_ticket_id=old.id,
        event_id=event_id,
    )
    db.add(new)
    db.flush()
    seq = _allocate(db, target, new)
    _release(db, old.id)  # 旧票释放原班次名额
    old.status = "reassigned"
    old.handled_at = _utcnow()
    _log(db, old.id, action, {
        "from_slot": old.slot_id, "to_slot": target.id,
        "new_ticket_no": new.ticket_no, "seat_seq": seq, **(extra or {}),
    }, operator=operator, event_id=event_id)
    _log(db, new.id, "issued", {"source_ticket_no": old.ticket_no, "seat_seq": seq},
         operator=operator, event_id=event_id)
    return new


# ---------------------------------------------------------------- 游客处理

def request_refund(ticket_id: int, operator: str = "tourist") -> dict:
    """游客申请退款。

    票立即进入 refunded 终态（处理结果确定），Payment 记录资金结算状态，
    由支付网关异步回执推进 pending→succeeded/failed。回执再晚也只改
    Payment，不会回滚票的结果。
    """
    def tx(db: Session):
        t = _get_ticket(db, ticket_id)
        ev = _require_affected_ticket(db, t)
        _require_unused(t)
        idem = f"refund-t{t.id}"
        exists = db.scalar(select(Payment.id).where(Payment.idempotency_key == idem))
        if exists:
            raise ConflictError("退款申请已存在，请勿重复提交", "duplicate_refund")
        pay = Payment(payment_no=f"PAY-R-{t.id}-{uuid.uuid4().hex[:8]}",
                      ticket_id=t.id, kind="refund", amount=t.price,
                      gateway_status="pending", idempotency_key=idem,
                      detail={"ticket_no": t.ticket_no})
        db.add(pay)
        _release(db, t.id)  # 退款释放原班次名额
        t.status = "refunded"
        t.handled_at = _utcnow()
        _log(db, t.id, "refund_request", {"payment_no": pay.payment_no, "amount": t.price},
             operator=operator, event_id=ev.id)
        db.flush()
        return {"ticket_id": t.id, "status": t.status,
                "payment_no": pay.payment_no, "gateway_status": "pending"}
    result = run_tx(tx)
    # 事务提交后再通知模拟通道，避免回执早于提交到达
    payment_gateway.submit(result["payment_no"])
    return result


def rebook_ticket(ticket_id: int, target_slot_id: int, operator: str = "tourist") -> dict:
    """游客直接换时段（目标班次有余位时立即出票）。"""
    def tx(db: Session):
        t = _get_ticket(db, ticket_id)
        ev = _require_affected_ticket(db, t)
        _require_unused(t)
        target = db.get(Slot, target_slot_id)
        if not target:
            raise BusinessError("目标班次不存在", "not_found")
        if target.id == t.slot_id:
            raise BusinessError("不能改签回原班次", "same_slot")
        _target_open(db, target.id)
        new = _issue_reassigned(db, t, target, ev.id, "rebook", operator)
        return {"ticket_id": t.id, "status": t.status,
                "new_ticket_id": new.id, "new_ticket_no": new.ticket_no,
                "target_slot_id": target.id}
    return run_tx(tx)


def join_waitlist(ticket_id: int, target_slot_id: int, operator: str = "tourist") -> dict:
    """游客加入候补（目标班期满员也可排队，恢复后按运力递补）。"""
    def tx(db: Session):
        t = _get_ticket(db, ticket_id)
        ev = _require_affected_ticket(db, t)
        _require_unused(t)
        target = db.get(Slot, target_slot_id)
        if not target:
            raise BusinessError("目标班次不存在", "not_found")
        if target.id == t.slot_id:
            raise BusinessError("不能候补原班次", "same_slot")
        _target_open(db, target.id)
        dup = db.scalar(select(WaitlistEntry.id)
                        .where(WaitlistEntry.ticket_id == t.id,
                               WaitlistEntry.state == "waiting"))
        if dup:
            raise ConflictError("该票已在候补队列中", "already_waitlisted")
        pos = (db.scalar(select(func.coalesce(func.max(WaitlistEntry.position), 0))
                         .where(WaitlistEntry.target_slot_id == target.id,
                                WaitlistEntry.event_id == ev.id)) or 0) + 1
        entry = WaitlistEntry(event_id=ev.id, ticket_id=t.id,
                              target_slot_id=target.id, position=pos)
        db.add(entry)
        _release(db, t.id)  # 入候补后不再占用原停运班次名额
        t.status = "waitlisted"
        t.handled_at = _utcnow()
        _log(db, t.id, "waitlist_join", {"target_slot_id": target.id, "position": pos},
             operator=operator, event_id=ev.id)
        db.flush()
        return {"ticket_id": t.id, "status": t.status,
                "waitlist_entry_id": entry.id, "position": pos}
    return run_tx(tx)


def cancel_waitlist(ticket_id: int, operator: str = "tourist") -> dict:
    def tx(db: Session):
        t = _get_ticket(db, ticket_id)
        entry = db.scalar(select(WaitlistEntry)
                          .where(WaitlistEntry.ticket_id == t.id,
                                 WaitlistEntry.state == "waiting"))
        if not entry:
            raise BusinessError("该票没有进行中的候补", "no_waitlist")
        entry.state = "cancelled"
        t.status = "unused"  # 可重新选择退款或换时段（原班次停运中，不回收其名额）
        _log(db, t.id, "waitlist_cancel", {"entry_id": entry.id}, operator=operator,
             event_id=entry.event_id)
        return {"ticket_id": t.id, "status": "unused"}
    return run_tx(tx)


# ---------------------------------------------------------------- 闸机核销

def check_in(ticket_no: str) -> dict:
    """模拟闸机核销。只有未使用的有效票可过闸；退款票/已改签旧票一律拒绝。"""
    def tx(db: Session):
        t = db.scalar(select(Ticket).where(Ticket.ticket_no == ticket_no))
        if not t:
            raise BusinessError("闸机未查到该票", "not_found")
        if t.checked_in:
            return {"ticket_no": t.ticket_no, "checked_in": True,
                    "status": t.status, "duplicate": True,
                    "message": "重复过闸，已放行记录"}
        if t.status == "refunded":
            raise BusinessError("该票已退款，禁止过闸", "refunded_gate")
        if t.status == "reassigned":
            raise BusinessError("该票为已改签的旧票，已失效，请使用新票", "stale_ticket")
        if t.status == "waitlisted":
            raise BusinessError("该票在候补队列中，尚未确定班次，禁止过闸", "waitlisted_gate")
        ev = _active_event(db)
        if ev and t.slot_id in (ev.affected_slot_ids or []):
            raise BusinessError("该班次处于停运状态，闸机不予放行", "suspended_gate")
        if not wind_service.is_safe():
            raise BusinessError(
                f"当前风速 {wind_service.get()}m/s 超限，全线暂停检票", "wind_gate")
        t.checked_in = True
        t.checked_in_at = _utcnow()
        _log(db, t.id, "check_in", {"gate": "G1"}, operator="gate")
        return {"ticket_no": t.ticket_no, "checked_in": True,
                "status": t.status, "slot_id": t.slot_id}
    return run_tx(tx)


# ---------------------------------------------------------------- 调度员

def set_capacity(slot_id: int, capacity: int, operator: str = "dispatcher") -> dict:
    def tx(db: Session):
        slot = db.get(Slot, slot_id)
        if not slot:
            raise BusinessError("班次不存在", "not_found")
        if capacity < 0:
            raise BusinessError("运力不能为负", "bad_capacity")
        used = _occupied(db, slot.id)
        if capacity < used:
            raise BusinessError(f"运力不能低于已占用名额（当前已占 {used}）",
                                "capacity_too_low")
        slot.capacity = capacity
        return {"slot_id": slot.id, "capacity": capacity, "occupied": used,
                "remaining": capacity - used}
    return run_tx(tx)


def _fill_one(db: Session, entry: WaitlistEntry, operator: str) -> Ticket:
    """调用方已持有写事务并完成加锁/筛选。容量预检 + 出票（出票内占位）。"""
    t = db.get(Ticket, entry.ticket_id)
    if not t or t.status != "waitlisted":
        entry.state = "cancelled"
        raise ConflictError("候补中的票状态已变化，跳过", "waitlist_stale")
    target = db.get(Slot, entry.target_slot_id)
    if _occupied(db, target.id) >= target.capacity:
        raise ConflictError(f"班次 {target.code} 名额已满", "slot_full")
    new = _issue_reassigned(db, t, target, entry.event_id, "waitlist_fill",
                            operator, {"waitlist_entry_id": entry.id,
                                       "waitlist_position": entry.position})
    entry.state = "filled"
    entry.filled_at = _utcnow()
    return new


def _ensure_resumed(db: Session) -> None:
    """有进行中的停运事件时，调度员必须等设备负责人确认恢复后才能递补。"""
    if _active_event(db) is not None:
        raise BusinessError("尚未恢复运营：请先由设备负责人确认恢复，再安排递补",
                            "not_resumed")


def fill_next_waitlist(slot_id: int | None, operator: str = "dispatcher") -> dict | None:
    """递补队列最前面的一个候补（可指定班次）。返回 None 表示无可递补。"""
    def tx(db: Session):
        _ensure_resumed(db)
        stmt = (select(WaitlistEntry).where(WaitlistEntry.state == "waiting")
                .order_by(WaitlistEntry.created_at, WaitlistEntry.id))
        if slot_id is not None:
            stmt = stmt.where(WaitlistEntry.target_slot_id == slot_id)
        while True:
            entry = db.scalar(stmt)
            if not entry:
                return None
            try:
                new = _fill_one(db, entry, operator)
                return {"waitlist_entry_id": entry.id, "ticket_id": entry.ticket_id,
                        "new_ticket_no": new.ticket_no,
                        "target_slot_id": entry.target_slot_id}
            except ConflictError as e:
                if e.code == "slot_full":
                    return None
                if e.code == "waitlist_stale":
                    continue
                raise
    return run_tx(tx)


def auto_fill(operator: str = "dispatcher") -> dict:
    """按 FIFO 跨班次连续递补，直到没有“有实际空余运力”的候补可填。

    在单个 BEGIN IMMEDIATE 事务内扫描整张候补表：每个目标班次实时计算
    剩余名额，满员的队列跳过但不影响其他班次；两名调度员同时点击时，
    后到事务在入口排队，看到的是前者提交后的最新名额。
    """
    def tx(db: Session):
        _ensure_resumed(db)
        entries = list(db.scalars(
            select(WaitlistEntry).where(WaitlistEntry.state == "waiting")
            .order_by(WaitlistEntry.created_at, WaitlistEntry.id)))
        filled, cancelled = [], 0
        for entry in entries:
            t = db.get(Ticket, entry.ticket_id)
            if not t or t.status != "waitlisted":
                entry.state = "cancelled"
                cancelled += 1
                continue
            target = db.get(Slot, entry.target_slot_id)
            used = _occupied(db, target.id)
            if used >= target.capacity:
                continue  # 该班次暂无实际空余运力，保留候补，继续尝试后面的人
            try:
                new = _fill_one(db, entry, operator)
            except ConflictError as e:
                if e.code == "slot_full":
                    continue
                raise
            filled.append({"waitlist_entry_id": entry.id, "ticket_id": entry.ticket_id,
                           "new_ticket_no": new.ticket_no,
                           "target_slot_id": entry.target_slot_id})
        return {"filled": filled, "count": len(filled), "cancelled_stale": cancelled}
    return run_tx(tx)


# ---------------------------------------------------------------- 支付回执

def apply_callback(payment_no: str, status: str, gateway_ref: str | None = None) -> dict:
    """支付网关终态回执（可能很晚才到）。

    幂等且单向：
    - Payment 已是终态时，任何回执（成功/失败）都不能改写，记为 ignored；
    - 票的退款/改签结果在申请时即确定，本函数绝不修改 Ticket ——
      晚到回执不可能推翻已确定的结果。
    """
    if status not in ("succeeded", "failed"):
        raise BusinessError("回执状态非法", "bad_callback")

    def tx(db: Session):
        pay = db.scalar(select(Payment).where(Payment.payment_no == payment_no))
        if not pay:
            raise BusinessError("支付单不存在", "not_found")
        if pay.gateway_status in ("succeeded", "failed"):
            ignored = list(pay.detail.get("ignored_callbacks", []))
            ignored.append({"status": status, "at": _utcnow().isoformat(),
                            "ref": gateway_ref})
            pay.detail = {**pay.detail, "ignored_callbacks": ignored}
            return {"payment_no": payment_no, "result": "ignored_duplicate",
                    "gateway_status": pay.gateway_status}
        pay.gateway_status = status
        pay.gateway_ref = gateway_ref
        pay.callback_at = _utcnow()
        pay.applied = True
        return {"payment_no": payment_no, "result": "applied",
                "gateway_status": status}
    return run_tx(tx)


def retry_refund(payment_no: str) -> dict:
    """退款失败后重新发起（不改票，只重推资金单）。"""
    def tx(db: Session):
        pay = db.scalar(select(Payment).where(Payment.payment_no == payment_no))
        if not pay:
            raise BusinessError("支付单不存在", "not_found")
        if pay.gateway_status == "succeeded":
            return {"payment_no": payment_no, "gateway_status": "succeeded",
                    "result": "already_settled", "submit": False}
        pay.gateway_status = "pending"
        pay.applied = False
        pay.callback_at = None
        return {"payment_no": payment_no, "gateway_status": "pending",
                "result": "resubmitted", "submit": True}
    result = run_tx(tx)
    if result.pop("submit", False):
        payment_gateway.submit(result["payment_no"])
    return result


# ---------------------------------------------------------------- 停运事件

def create_event(reason: str, affected_slot_ids: list[int], detail: str = "",
                 created_by: str = "ops") -> dict:
    def tx(db: Session):
        if _active_event(db):
            raise BusinessError("已有进行中的停运事件，请先恢复运营后再新建",
                                "event_active")
        if reason not in ("weather", "equipment"):
            raise BusinessError("停运原因非法", "bad_reason")
        slots = list(db.scalars(select(Slot).where(Slot.id.in_(affected_slot_ids))))
        found = {s.id for s in slots}
        missing = [i for i in affected_slot_ids if i not in found]
        if missing:
            raise BusinessError(f"班次不存在: {missing}", "not_found")
        wind = wind_service.get()
        ev = SuspensionEvent(reason=reason, detail=detail,
                             affected_slot_ids=list(affected_slot_ids),
                             wind_speed=wind, created_by=created_by)
        db.add(ev)
        db.flush()
        # 停运范围落到票：关联范围内全部票（含已过闸票——范围与核销状态是
        # 两个维度；已过闸票关联后在 _require_unused 处被单独拦截，
        # 返回“已过闸不能按未使用票退款”）。
        tickets = db.scalars(select(Ticket).where(
            Ticket.slot_id.in_(affected_slot_ids))).all()
        unused_n = 0
        for t in tickets:
            t.event_id = ev.id
            if t.status == "unused" and not t.checked_in:
                unused_n += 1
        if detail:
            ev.detail = f"{detail}｜范围内 {len(tickets)} 张票，其中待处理 {unused_n} 张"
        else:
            ev.detail = f"范围内 {len(tickets)} 张票，其中待处理 {unused_n} 张"
        return {"event_id": ev.id, "reason": ev.reason,
                "affected_slot_ids": ev.affected_slot_ids,
                "wind_speed": wind, "linked_tickets": len(tickets),
                "handleable_tickets": unused_n}
    return run_tx(tx)


def resume_event(event_id: int, confirmer: str, role: str,
                 remark: str = "") -> dict:
    """恢复运营：必须由设备负责人确认，且当前风速低于安全阈值。"""
    def tx(db: Session):
        ev = db.get(SuspensionEvent, event_id)
        if not ev:
            raise BusinessError("停运事件不存在", "not_found")
        if ev.status == "resumed":
            raise ConflictError("该事件已恢复，不能重复确认", "already_resumed")
        if role != EQUIPMENT_OWNER_ROLE or not confirmer.strip():
            raise BusinessError("只有设备负责人可以确认恢复运营", "not_equipment_owner")
        wind = wind_service.get()
        if not wind_service.is_safe():
            raise BusinessError(f"当前风速 {wind}m/s 仍高于安全阈值 "
                                f"{wind_service.limit}m/s，不能恢复", "wind_unsafe")
        ev.status = "resumed"
        ev.resumed_at = _utcnow()
        ev.resumed_by = confirmer
        ev.resume_confirmer = confirmer
        ev.resume_remark = remark
        return {"event_id": ev.id, "status": "resumed",
                "resumed_by": confirmer, "wind_speed": wind}
    return run_tx(tx)
