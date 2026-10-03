"""初始化演示数据（幂等：已有数据则跳过）。

场景：08:00 与 09:00 两个班次因大风停运；08:00 班满载 10 人，其中一人已过闸
（演示“已过闸票不能按未使用票退款”）；13:00 班满载（候补目标），14:30 班
余 2 位（可直接改签），16:00 班余 7 位。启动时已挂起一个天气停运事件，
风速 19m/s，等待天气转好后由设备负责人确认恢复。
"""
from __future__ import annotations

from sqlalchemy import func, select

from .database import SessionLocal, engine
from .mock_services import payment_gateway, wind_service
from .models import Base, Order, SeatAllocation, Slot, SuspensionEvent, Ticket
from . import services

SLOTS = [
    ("0800", "08:00", "08:30", 10, "早高峰班次"),
    ("0900", "09:00", "09:30", 20, ""),
    ("1030", "10:30", "11:00", 10, ""),
    ("1300", "13:00", "13:30", 8, "午后班次"),
    ("1430", "14:30", "15:00", 8, ""),
    ("1600", "16:00", "16:30", 10, "收班班次"),
]


def seed():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        if db.scalar(select(func.count(Slot.id))) > 0:
            print("seed: 数据已存在，跳过")
            return
        slots = {}
        for code, st, et, cap, note in SLOTS:
            s = Slot(code=code, start_time=st, end_time=et, capacity=cap, note=note)
            db.add(s)
            slots[code] = s
        db.flush()

        def add_order(order_no, name, phone, items):
            """items: [(slot_code, passenger, checked_in)]"""
            o = Order(order_no=order_no, contact_name=name, contact_phone=phone)
            db.add(o)
            db.flush()
            for code, pax, checked in items:
                t = Ticket(ticket_no=f"CW-{order_no}-{pax}", order_id=o.id,
                           passenger_name=pax, slot_id=slots[code].id)
                db.add(t)
                db.flush()
                if checked:
                    t.checked_in = True
                    from datetime import datetime, timedelta
                    t.checked_in_at = datetime.utcnow() - timedelta(hours=2)
                db.add(SeatAllocation(slot_id=slots[code].id,
                                      seat_seq=_next_seq(db, slots[code].id),
                                      ticket_id=t.id))
            return o

        # 08:00 满载 10 人
        add_order("2026100301", "张伟", "13800000001",
                  [("0800", "张伟", False), ("0800", "李娜", False), ("0800", "张小宝", False)])
        add_order("2026100302", "王芳", "13800000002",
                  [("0800", "王芳", False), ("0800", "王磊", True)])  # 王磊已过闸
        add_order("2026100303", "陈晨", "13800000003",
                  [("0800", "陈晨", False), ("0800", "刘洋", False)])
        add_order("2026100304", "周末", "13800000004",
                  [("0800", "周末", False), ("0800", "吴迪", False), ("0800", "郑爽", False)])
        # 09:00 4 人
        add_order("2026100305", "孙强", "13800000005",
                  [("0900", "孙强", False), ("0900", "马丽", False)])
        add_order("2026100306", "朱琳", "13800000006",
                  [("0900", "朱琳", False), ("0900", "胡军", False)])
        add_order("2026100312", "钱坤", "13800000112",
                  [("0900", "钱多", False), ("0900", "钱少", False), ("0900", "钱满", False)])
        # 13:00 满载 8 人（候补目标）
        add_order("2026100307", "甲乙", "13800000007",
                  [("1300", "甲一", False), ("1300", "甲二", False), ("1300", "甲三", False),
                   ("1300", "甲四", False)])
        add_order("2026100308", "丙丁", "13800000008",
                  [("1300", "丙一", False), ("1300", "丙二", False),
                   ("1300", "丁三", False), ("1300", "丁四", False)])
        # 14:30 已售 6 / 8（余 2，可直接改签）
        add_order("2026100309", "东风", "13800000009",
                  [("1430", "东一", False), ("1430", "东二", False), ("1430", "东三", False)])
        add_order("2026100310", "南风", "13800000010",
                  [("1430", "南一", False), ("1430", "南二", False), ("1430", "南三", False)])
        # 16:00 已售 3 / 10
        add_order("2026100311", "西门", "13800000011",
                  [("1600", "西一", False), ("1600", "西二", False), ("1600", "西三", False)])

        db.commit()

        # 模拟环境：大风天气 + 支付回执 2 秒后到
        wind_service.set(19.0)
        payment_gateway.configure(callback_delay=2.0, auto_callback=True)

        # 挂起一个进行中的天气停运事件（08:00、09:00）
        ev_id = services.create_event(
            "weather",
            [slots["0800"].id, slots["0900"].id],
            detail="阵风超过安全阈值，暂停运营",
            created_by="运营-周调度",
        )
        print("seed: 完成，活动停运事件 id =", ev_id["event_id"],
              "关联票", ev_id["linked_tickets"], "张")
    finally:
        db.close()


def _next_seq(db, slot_id: int) -> int:
    from sqlalchemy import text
    row = db.execute(text("SELECT COALESCE(MAX(seat_seq),0) FROM seat_allocations "
                          "WHERE slot_id=:s"), {"s": slot_id}).first()
    return (row[0] or 0) + 1


if __name__ == "__main__":
    seed()
