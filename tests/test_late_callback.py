"""验证三：晚到/重复的支付回执不会推翻已确定的结果。

- 退款成功后，晚到的「失败」回执不得把已退款票改回去；
- 退款失败（票已恢复有效）后，晚到的「成功」回执不得再退款；
- 同一 callback_id 重复送达完全幂等。
"""
import uuid

from app.database import SessionLocal
from app.models import Ticket, TicketAction, TimeSlot


def _states(ticket_id, slot_id):
    from sqlalchemy import select
    with SessionLocal() as db:
        t = db.get(Ticket, ticket_id)
        a = db.scalar(select(TicketAction).where(TicketAction.ticket_id == ticket_id))
        s = db.get(TimeSlot, slot_id)
        return t.status, a.status, s.sold


def _refund(client, tid):
    key = f"idem-{uuid.uuid4().hex}"
    r = client.post(f"/api/tickets/{tid}/action",
                    json={"action_type": "refund", "idempotency_key": key})
    assert r.status_code == 200
    return r.json()["action"]["refund_txn"]


def test_late_failure_callback_does_not_overturn_confirmed_refund(client, factory):
    src = factory.slot(start="10:00", capacity=20)
    _, (tid,) = factory.order_with_tickets(src, 1)
    factory.event([src])
    txn = _refund(client, tid)

    # 正常回执：退款成功
    r = client.post("/api/mock/payment/callback",
                    json={"callback_id": f"cb-{uuid.uuid4().hex}", "txn": txn,
                          "result": "success"})
    assert r.status_code == 200 and r.json()["applied"]
    assert _states(tid, src) == ("refunded", "confirmed", 0)

    # 晚到的重复「成功」回执（新 callback_id）：只记录，不重复生效
    r = client.post("/api/mock/payment/callback",
                    json={"callback_id": f"cb-{uuid.uuid4().hex}", "txn": txn,
                          "result": "success"})
    assert r.status_code == 200 and not r.json()["applied"]
    assert "晚到" in r.json()["note"]

    # 晚到的「失败」回执：不得推翻已确定的退款
    r = client.post("/api/mock/payment/callback",
                    json={"callback_id": f"cb-{uuid.uuid4().hex}", "txn": txn,
                          "result": "failure"})
    assert r.status_code == 200 and not r.json()["applied"]
    assert _states(tid, src) == ("refunded", "confirmed", 0), \
        "已确定的退款结果不得被晚到回执推翻"


def test_late_success_callback_does_not_overturn_rejected_refund(client, factory):
    src = factory.slot(start="11:00", capacity=20)
    _, (tid,) = factory.order_with_tickets(src, 1)
    factory.event([src])
    txn = _refund(client, tid)

    # 网关先回「失败」：票恢复有效
    r = client.post("/api/mock/payment/callback",
                    json={"callback_id": f"cb-{uuid.uuid4().hex}", "txn": txn,
                          "result": "failure"})
    assert r.json()["applied"]
    assert _states(tid, src) == ("active", "rejected", 1)

    # 晚到的「成功」回执：不得把已恢复有效的票再退掉
    r = client.post("/api/mock/payment/callback",
                    json={"callback_id": f"cb-{uuid.uuid4().hex}", "txn": txn,
                          "result": "success"})
    assert not r.json()["applied"]
    assert _states(tid, src) == ("active", "rejected", 1), \
        "已驳回的处理单不得被晚到回执推翻"


def test_duplicate_callback_id_is_idempotent(client, factory):
    src = factory.slot(start="13:00", capacity=20)
    _, (tid,) = factory.order_with_tickets(src, 1)
    factory.event([src])
    txn = _refund(client, tid)

    cbid = f"cb-{uuid.uuid4().hex}"
    r1 = client.post("/api/mock/payment/callback",
                     json={"callback_id": cbid, "txn": txn, "result": "success"})
    assert r1.json()["fresh"] and r1.json()["applied"]

    # 同一 callback_id 重发：幂等，不重复退款
    r2 = client.post("/api/mock/payment/callback",
                     json={"callback_id": cbid, "txn": txn, "result": "success"})
    assert not r2.json()["fresh"]
    assert _states(tid, src) == ("refunded", "confirmed", 0)
