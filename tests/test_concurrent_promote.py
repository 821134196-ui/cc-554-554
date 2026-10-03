"""验证一：并发递补不超售。

场景：目标时段只剩 3 个名额，12 条候补同时由两名调度员发起递补，
必须恰好 3 条成功、9 条失败，名额不能超售；同一条候补被两人同时递补也只能成功一次。
"""
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Ticket, TimeSlot, WaitlistEntry

from .conftest import fire_concurrently, waitlist_entry_id


def _slot_state(slot_id):
    with SessionLocal() as db:
        s = db.get(TimeSlot, slot_id)
        return s.sold, s.capacity


def test_concurrent_promote_never_oversells(live, factory):
    # 停运班次（容量 20，12 张票全部候补）与目标班次（容量 10，已售 7，仅剩 3）
    src = factory.slot(start="10:00", capacity=20)
    dst = factory.slot(start="14:00", capacity=10, sold=7)
    _, ticket_ids = factory.order_with_tickets(src, 12)
    event_id = factory.event([src])
    entry_ids = []
    for tid in ticket_ids:
        factory.waitlist(tid, dst, event_id)
        entry_ids.append(waitlist_entry_id(tid, event_id))

    # 两名调度员同时递补全部 12 条候补
    reqs = [
        (f"{live}/api/dispatch/promote",
         {"entry_id": eid, "slot_id": dst,
          "operator": "王芳" if i % 2 == 0 else "刘洋"})
        for i, eid in enumerate(entry_ids)
    ]
    results = fire_concurrently(reqs)

    ok = [r for r in results if r[0] == 200]
    full = [r for r in results if r[0] == 409]
    assert len(ok) == 3, f"应恰好 3 条递补成功，实际 {len(ok)}：{results}"
    assert len(full) == 9, f"其余 9 条应因名额不足被拒绝，实际 {len(full)}"

    sold, cap = _slot_state(dst)
    assert sold == cap == 10, "名额必须占满且不得超售"

    with SessionLocal() as db:
        promoted = db.scalars(
            select(WaitlistEntry).where(WaitlistEntry.status == "promoted")
        ).all()
        assert len(promoted) == 3
        moved = db.scalars(
            select(Ticket).where(Ticket.slot_id == dst)
        ).all()
        assert len(moved) == 3, "迁入目标时段的票数必须等于递补成功数"


def test_same_entry_two_dispatchers_one_wins(live, factory):
    src = factory.slot(start="11:00", capacity=20)
    dst = factory.slot(start="15:00", capacity=10, sold=0)
    _, ticket_ids = factory.order_with_tickets(src, 1)
    event_id = factory.event([src])
    factory.waitlist(ticket_ids[0], dst, event_id)
    entry_id = waitlist_entry_id(ticket_ids[0], event_id)

    # 同一名额（同一条候补）被两名调度员同时操作
    reqs = [
        (f"{live}/api/dispatch/promote",
         {"entry_id": entry_id, "slot_id": dst, "operator": op})
        for op in ("王芳", "刘洋")
    ] * 4  # 多来几轮，放大竞争窗口
    results = fire_concurrently(reqs)

    ok = [r for r in results if r[0] == 200]
    assert len(ok) == 1, f"同一条候补只能递补成功一次，实际 {len(ok)}"

    sold, _ = _slot_state(dst)
    assert sold == 1, "目标时段只应增加一个名额占用"
    with SessionLocal() as db:
        entry = db.get(WaitlistEntry, entry_id)
        assert entry.status == "promoted"
        ticket = db.get(Ticket, ticket_ids[0])
        assert ticket.slot_id == dst and ticket.status == "active"
