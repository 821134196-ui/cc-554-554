"""数据模型。

关键约束（数据库层兜底，业务层先做友好校验）：
- TicketAction: (ticket_id, event_id) 唯一 —— 同一张票在同一次停运事件中只能有一种处理结果，
  从根上保证“不能既退款又改签”。
- TicketAction.idempotency_key 唯一 —— 游客端重试/双击不产生第二笔处理单。
- WaitlistEntry: (ticket_id, event_id) 唯一 —— 同一张票在同一事件中只排一次队。
- PaymentCallback.callback_id 唯一 —— 支付回执幂等，重复回执直接忽略。
"""
from datetime import datetime

from sqlalchemy import (Boolean, DateTime, ForeignKey, Integer, String,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Staff(Base):
    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    role: Mapped[str] = mapped_column(String(30))  # equipment_manager / dispatcher


class TimeSlot(Base):
    __tablename__ = "time_slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    start_time: Mapped[str] = mapped_column(String(5))         # HH:MM
    end_time: Mapped[str] = mapped_column(String(5))
    capacity: Mapped[int] = mapped_column(Integer)
    sold: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="normal")  # normal / suspended

    @property
    def remaining(self) -> int:
        return max(0, self.capacity - self.sold)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    visitor_name: Mapped[str] = mapped_column(String(50))
    phone: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    tickets: Mapped[list["Ticket"]] = relationship(back_populates="order")


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    slot_id: Mapped[int] = mapped_column(ForeignKey("time_slots.id"))
    passenger_name: Mapped[str] = mapped_column(String(50))
    price: Mapped[int] = mapped_column(Integer)  # 元
    # active 有效 / used 已过闸 / refund_pending 退款中 / refunded 已退款 / waitlisted 候补中
    status: Mapped[str] = mapped_column(String(20), default="active")
    gate_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    order: Mapped[Order] = relationship(back_populates="tickets")
    slot: Mapped[TimeSlot] = relationship()


class SuspensionEvent(Base):
    __tablename__ = "suspension_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(20))  # weather / equipment
    description: Mapped[str] = mapped_column(String(500), default="")
    event_date: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active / resolved
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resumed_by: Mapped[str | None] = mapped_column(String(50), nullable=True)

    slots: Mapped[list["EventSlot"]] = relationship(back_populates="event")


class EventSlot(Base):
    """停运事件影响的班次（停运范围）。"""
    __tablename__ = "event_slots"
    __table_args__ = (UniqueConstraint("event_id", "slot_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("suspension_events.id"))
    slot_id: Mapped[int] = mapped_column(ForeignKey("time_slots.id"))

    event: Mapped[SuspensionEvent] = relationship(back_populates="slots")
    slot: Mapped[TimeSlot] = relationship()


class TicketAction(Base):
    """一张票在一次停运事件中的处理记录（退款/改签/候补）。"""
    __tablename__ = "ticket_actions"
    __table_args__ = (UniqueConstraint("ticket_id", "event_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    event_id: Mapped[int] = mapped_column(ForeignKey("suspension_events.id"))
    action_type: Mapped[str] = mapped_column(String(20))  # refund / rebook / waitlist
    # pending 待回执或待递补 / confirmed 已生效 / rejected 已驳回 / cancelled 已取消
    status: Mapped[str] = mapped_column(String(20), default="pending")
    target_slot_id: Mapped[int | None] = mapped_column(ForeignKey("time_slots.id"), nullable=True)
    refund_txn: Mapped[str | None] = mapped_column(String(60), unique=True, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    operator: Mapped[str] = mapped_column(String(50), default="游客")
    note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    ticket: Mapped[Ticket] = relationship()
    target_slot: Mapped[TimeSlot | None] = relationship()


class WaitlistEntry(Base):
    __tablename__ = "waitlist_entries"
    __table_args__ = (UniqueConstraint("ticket_id", "event_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    event_id: Mapped[int] = mapped_column(ForeignKey("suspension_events.id"))
    desired_slot_id: Mapped[int] = mapped_column(ForeignKey("time_slots.id"))
    position: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="waiting")  # waiting / promoted / cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    promoted_by: Mapped[str | None] = mapped_column(String(50), nullable=True)

    ticket: Mapped[Ticket] = relationship()
    desired_slot: Mapped[TimeSlot] = relationship()


class PaymentCallback(Base):
    """支付网关回执流水（含被忽略的晚到/重复回执，便于审计）。"""
    __tablename__ = "payment_callbacks"

    id: Mapped[int] = mapped_column(primary_key=True)
    callback_id: Mapped[str] = mapped_column(String(80), unique=True)
    txn: Mapped[str] = mapped_column(String(60))
    result: Mapped[str] = mapped_column(String(20))  # success / failure
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(String(300), default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    actor: Mapped[str] = mapped_column(String(50))
    action: Mapped[str] = mapped_column(String(50))
    detail: Mapped[str] = mapped_column(String(500), default="")
