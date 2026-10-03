"""种子数据：班次、员工、示例订单与车票。仅在空库时执行。"""
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Order, Staff, Ticket, TimeSlot
from .services import TICKET_PRICE

SLOT_TIMES = [("09:00", "10:00"), ("10:00", "11:00"), ("11:00", "12:00"),
              ("13:00", "14:00"), ("14:00", "15:00"), ("15:00", "16:00")]


def seed_if_empty(db: Session):
    if db.scalar(select(func.count()).select_from(TimeSlot)):
        return False

    today = date.today()
    slots = {}
    for day_offset in (0, 1):
        d = (today + timedelta(days=day_offset)).isoformat()
        for start, end in SLOT_TIMES:
            s = TimeSlot(date=d, start_time=start, end_time=end,
                         capacity=8, sold=0, status="normal")
            db.add(s)
            db.flush()
            slots[(d, start)] = s

    db.add_all([
        Staff(name="张伟", role="equipment_manager"),
        Staff(name="李强", role="equipment_manager"),
        Staff(name="王芳", role="dispatcher"),
        Staff(name="刘洋", role="dispatcher"),
    ])

    def make_order(order_no, visitor, phone, passengers, slot_key, used_count=0):
        order = Order(order_no=order_no, visitor_name=visitor, phone=phone)
        db.add(order)
        db.flush()
        slot = slots[slot_key]
        for i, name in enumerate(passengers):
            used = i < used_count
            t = Ticket(
                ticket_code=f"T-{order_no}-{i+1}",
                order_id=order.id, slot_id=slot.id,
                passenger_name=name, price=TICKET_PRICE,
                status="used" if used else "active",
                gate_verified_at=datetime.now() if used else None,
            )
            db.add(t)
            slot.sold += 1
        return order

    d0 = today.isoformat()
    # 10:00 班次：3 + 2 + 2 = 7 张，接近满员（容量 8）
    make_order("ORD-1001", "张三", "13800000001",
               ["张三", "张妻", "张小宝"], (d0, "10:00"))
    make_order("ORD-1002", "李四", "13800000002",
               ["李四", "李母"], (d0, "10:00"), used_count=1)  # 李四本人已过闸
    make_order("ORD-1004", "赵六", "13800000004",
               ["赵六", "赵友"], (d0, "10:00"))
    # 11:00 班次：3 张
    make_order("ORD-1003", "王五", "13800000003",
               ["王五", "王五妻", "王五子"], (d0, "11:00"))

    db.commit()
    return True
