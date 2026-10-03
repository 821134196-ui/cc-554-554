"""数据模型。

核心关联：停运事件(SuspensionEvent)通过 ``affected_slot_ids`` 把停运范围
与票(Ticket)的原班次(Slot)关联；票上记录核销状态(checked_in)与最终结果
(status)，结果分两条不可变流水（退款流水 Payment 与改签流水 Reassignment）
落库，保证“停运范围—核销状态—退款/改签结果”三者可追溯。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class Slot(Base):
    """缆车班次（时段）。"""

    __tablename__ = "slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    start_time: Mapped[str] = mapped_column(String(5))  # HH:MM
    end_time: Mapped[str] = mapped_column(String(5))
    capacity: Mapped[int] = mapped_column(Integer, default=20)
    # 恢复运营后调度员可按运力调整（如减载运行）
    note: Mapped[str] = mapped_column(String(100), default="")

    tickets: Mapped[list["Ticket"]] = relationship(back_populates="slot")


class Order(Base):
    """游客订单，一张订单含多张票（同行人）。"""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    contact_name: Mapped[str] = mapped_column(String(50))
    contact_phone: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tickets: Mapped[list["Ticket"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class Ticket(Base):
    """单张票，处理的最小单位。

    生命周期：
        UNUSED（未使用）
          ├─ CHECKED_IN（已过闸，不可逆）
          ├─ REFUNDED（已退款，终态）
          ├─ REASSIGNED（已改签，终态）
          └─ WAITLISTED（已加入候补）
                └─ REASSIGNED（递补成功并出票）
                   └─ CHECKED_IN（新票过闸）
    """

    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    passenger_name: Mapped[str] = mapped_column(String(50))
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id"), index=True)
    price: Mapped[float] = mapped_column(Float, default=100.0)
    checked_in: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # unused / waitlisted / reassigned / refunded
    status: Mapped[str] = mapped_column(String(16), default="unused", index=True)
    # 改签来源票（递补/改签产生的新票指向原票）
    source_ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("suspension_events.id"), nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    order: Mapped["Order"] = relationship(back_populates="tickets")
    slot: Mapped["Slot"] = relationship(foreign_keys=[slot_id], viewonly=True)
    source_ticket: Mapped["Ticket | None"] = relationship(remote_side=[id], foreign_keys=[source_ticket_id])


class SuspensionEvent(Base):
    """停运事件。再次停运必须新建事件，旧事件及其处理记录永久保留。"""

    __tablename__ = "suspension_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    reason: Mapped[str] = mapped_column(String(16))  # weather / equipment
    detail: Mapped[str] = mapped_column(String(200), default="")
    # 停运范围：受影响班次 id 列表
    affected_slot_ids: Mapped[list] = mapped_column(JSON, default=list)
    wind_speed: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active / resumed
    created_by: Mapped[str] = mapped_column(String(50), default="ops")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # 恢复必须由设备负责人确认
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resumed_by: Mapped[str | None] = mapped_column(String(50), nullable=True)
    resume_confirmer: Mapped[str | None] = mapped_column(String(50), nullable=True)  # equipment_owner
    resume_remark: Mapped[str | None] = mapped_column(String(200), nullable=True)

    tickets: Mapped[list["Ticket"]] = relationship(foreign_keys=[Ticket.event_id], viewonly=True)


class TicketAction(Base):
    """对票的每一次处理流水（退款/换时段/候补/递补/过闸），只增不改。"""

    __tablename__ = "ticket_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("suspension_events.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(20))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    operator: Mapped[str] = mapped_column(String(50), default="tourist")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WaitlistEntry(Base):
    """候补队列。递补按 event 范围、目标班次、入列时间 FIFO。"""

    __tablename__ = "waitlist_entries"
    __table_args__ = (
        UniqueConstraint("ticket_id", name="uq_waitlist_ticket"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("suspension_events.id"), index=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    target_slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    # waiting / filled / cancelled
    state: Mapped[str] = mapped_column(String(16), default="waiting", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SeatAllocation(Base):
    """名额占用表：每个 (slot_id, seat_seq) 全局唯一。

    正常改签与候补递补都先 INSERT 本表；并发下数据库唯一约束兜底，
    确保同一个名额不可能给两个人。
    """

    __tablename__ = "seat_allocations"
    __table_args__ = (
        UniqueConstraint("slot_id", "seat_seq", name="uq_seat_allocation"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id"), index=True)
    seat_seq: Mapped[int] = mapped_column(Integer)  # 1..capacity
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SystemState(Base):
    """简单 KV 表：持久化本地模拟接口的状态（如当前风速），跨进程生效。"""

    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))


class Payment(Base):
    """退款/改签支付流水，与模拟支付网关的回执一一对应。"""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # refund / rebook_charge
    amount: Mapped[float] = mapped_column(Float)
    # pending / succeeded / failed
    gateway_status: Mapped[str] = mapped_column(String(16), default="pending")
    gateway_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)  # 结果是否已入账
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    callback_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
