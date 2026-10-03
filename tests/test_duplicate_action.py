"""验证二：同一张票的重复/冲突处理被拦截。

- 退款、改签、候补并发打到同一张票上，只能有一种生效；
- 相同幂等键重试返回首次结果，不产生第二笔处理单；
- 已退款/已过闸的票再发起处理一律拒绝。
"""
import uuid

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Ticket, TicketAction

from .conftest import fire_concurrently


def _action_count(ticket_id):
    with SessionLocal() as db:
        return db.scalar(
            select(func.count()).select_from(TicketAction)
            .where(TicketAction.ticket_id == ticket_id)
        )


def test_concurrent_conflicting_actions_only_one_wins(live, factory):
    src = factory.slot(start="10:00", capacity=20)
    dst = factory.slot(start="14:00", capacity=10, sold=0)
    _, (tid,) = factory.order_with_tickets(src, 1)
    event_id = factory.event([src])

    key_refund = f"idem-refund-{uuid.uuid4().hex}"
    reqs = [
        (f"{live}/api/tickets/{tid}/action",
         {"action_type": "refund", "idempotency_key": key_refund}),
        (f"{live}/api/tickets/{tid}/action",
         {"action_type": "rebook", "idempotency_key": f"idem-rebook-{uuid.uuid4().hex}",
          "target_slot_id": dst}),
        (f"{live}/api/tickets/{tid}/action",
         {"action_type": "waitlist", "idempotency_key": f"idem-wl-{uuid.uuid4().hex}",
          "target_slot_id": dst}),
        # 同一幂等键的重复提交（如网络重试/双击）
        (f"{live}/api/tickets/{tid}/action",
         {"action_type": "refund", "idempotency_key": key_refund}),
    ]
    results = fire_concurrently(reqs)

    created = [r for r in results if r[0] == 200 and r[1]["created"]]
    assert len(created) == 1, f"同一张票只能成功创建一笔处理单，实际 {len(created)}"
    assert _action_count(tid) == 1, "数据库中该票只能有一条处理记录"

    # 同一幂等键的两次并发提交，结果必须一致：
    # 要么一个创建、一个重放（同一处理单），要么都因冲突被 409 拦截
    pair = [results[0], results[3]]
    codes = sorted(r[0] for r in pair)
    if codes == [200, 200]:
        flags = sorted(bool(r[1]["created"]) for r in pair)
        assert flags == [False, True], f"同键请求应一个创建一个重放：{pair}"
        assert len({r[1]["action"]["id"] for r in pair}) == 1, "重放必须返回同一处理单"
    else:
        assert codes == [409, 409], f"同键请求结果必须一致：{pair}"


def test_sequential_double_action_rejected(live, factory):
    src = factory.slot(start="11:00", capacity=20)
    dst = factory.slot(start="15:00", capacity=10, sold=0)
    _, (tid,) = factory.order_with_tickets(src, 1)
    factory.event([src])

    import httpx
    key = f"idem-{uuid.uuid4().hex}"
    with httpx.Client(base_url=live, timeout=30) as c:
        r1 = c.post(f"/api/tickets/{tid}/action",
                    json={"action_type": "refund", "idempotency_key": key})
        assert r1.status_code == 200 and r1.json()["created"]

        # 同一张票再改签：必须被拦截（不能既退款又改签）
        r2 = c.post(f"/api/tickets/{tid}/action",
                    json={"action_type": "rebook",
                          "idempotency_key": f"idem-{uuid.uuid4().hex}",
                          "target_slot_id": dst})
        assert r2.status_code == 409
        assert "重复" in r2.json()["detail"]["message"] or "已有处理" in r2.json()["detail"]["message"]

        # 相同幂等键重试：返回首次结果而非报错
        r3 = c.post(f"/api/tickets/{tid}/action",
                    json={"action_type": "refund", "idempotency_key": key})
        assert r3.status_code == 200 and not r3.json()["created"]
        assert r3.json()["action"]["id"] == r1.json()["action"]["id"]
    assert _action_count(tid) == 1


def test_used_ticket_cannot_refund(live, factory):
    """已过闸的票不能按未使用票退款。"""
    import httpx
    src = factory.slot(start="13:00", capacity=20)
    order_no, (tid,) = factory.order_with_tickets(src, 1)
    factory.event([src])
    with SessionLocal() as db:
        code = db.get(Ticket, tid).ticket_code

    with httpx.Client(base_url=live, timeout=30) as c:
        r = c.post("/api/mock/gate/verify", json={"ticket_code": code})
        assert r.status_code == 200 and r.json()["status"] == "used"

        r = c.post(f"/api/tickets/{tid}/action",
                   json={"action_type": "refund",
                         "idempotency_key": f"idem-{uuid.uuid4().hex}"})
        assert r.status_code == 409
        assert "过闸" in r.json()["detail"]["message"]
    assert _action_count(tid) == 0
