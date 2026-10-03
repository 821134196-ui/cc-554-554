"""端到端业务流：停运 → 游客分别选择 → 恢复确认 → 递补 → 再次停运保留记录。"""
import uuid
from datetime import date

from sqlalchemy import select

from app.database import SessionLocal
from app.models import TimeSlot


def _today_slot_id(start):
    with SessionLocal() as db:
        return db.scalar(
            select(TimeSlot.id).where(
                TimeSlot.date == date.today().isoformat(),
                TimeSlot.start_time == start,
            )
        )


def test_full_business_flow(client):
    # 1. 查询种子订单：ORD-1001 三张 10:00 的票
    r = client.get("/api/orders/ORD-1001")
    assert r.status_code == 200
    order = r.json()
    assert len(order["tickets"]) == 3
    slot_10 = order["tickets"][0]["slot"]["id"]
    assert order["tickets"][0]["event"] is None

    # 2. 创建停运事件（天气原因，影响 10:00 班次）
    r = client.post("/api/events", json={
        "title": "大风红色预警停运", "reason": "weather",
        "event_date": order["tickets"][0]["slot"]["date"],
        "slot_ids": [slot_10], "description": "阵风超阈值",
    })
    assert r.status_code == 201
    event_id = r.json()["id"]

    # 3. 订单中每张票都关联到事件，可分别选择处理方式
    order = client.get("/api/orders/ORD-1001").json()
    t1, t2, t3 = order["tickets"]
    assert all(t["event"] and t["event"]["id"] == event_id for t in order["tickets"])

    r = client.post(f"/api/tickets/{t1['id']}/action",
                    json={"action_type": "refund",
                          "idempotency_key": f"flow-{uuid.uuid4().hex}"})
    assert r.json()["action"]["status"] == "pending"

    slot_14 = _today_slot_id("14:00")
    r = client.post(f"/api/tickets/{t2['id']}/action",
                    json={"action_type": "rebook", "target_slot_id": slot_14,
                          "idempotency_key": f"flow-{uuid.uuid4().hex}"})
    assert r.json()["action"]["status"] == "confirmed"

    r = client.post(f"/api/tickets/{t3['id']}/action",
                    json={"action_type": "waitlist", "target_slot_id": slot_14,
                          "idempotency_key": f"flow-{uuid.uuid4().hex}"})
    assert r.json()["action"]["status"] == "pending"

    # 4. 退款回执到账
    pending = client.get("/api/mock/payment/pending").json()["pending"]
    txn = [p for p in pending if p["ticket_code"] == t1["ticket_code"]][0]["refund_txn"]
    r = client.post("/api/mock/payment/callback",
                    json={"callback_id": f"cb-{uuid.uuid4().hex}", "txn": txn,
                          "result": "success"})
    assert r.json()["applied"]

    # 5. 恢复运营：未勾选确认 / 非设备负责人在册人员 都被拒绝
    r = client.post(f"/api/events/{event_id}/resume",
                    json={"manager_name": "张伟", "confirm": False})
    assert r.status_code == 400
    r = client.post(f"/api/events/{event_id}/resume",
                    json={"manager_name": "王芳", "confirm": True})
    assert r.status_code == 403
    r = client.post(f"/api/events/{event_id}/resume",
                    json={"manager_name": "张伟", "confirm": True})
    assert r.status_code == 200 and r.json()["resumed_by"] == "张伟"

    # 6. 调度看板：时段并列展示已售/剩余；候补自动递补
    board = client.get(f"/api/dispatch/board?event_id={event_id}").json()
    assert board["event"]["status"] == "resolved"
    slot_map = {s["id"]: s for s in board["slots"]}
    assert slot_map[slot_14]["remaining"] >= 1
    assert any(w["status"] == "waiting" for w in board["waitlist"])

    r = client.post("/api/dispatch/auto_promote",
                    json={"event_id": event_id, "slot_id": slot_14, "operator": "王芳"})
    assert r.json()["count"] == 1
    board = client.get(f"/api/dispatch/board?event_id={event_id}").json()
    assert all(w["status"] == "promoted" for w in board["waitlist"])

    # 7. 再次停运：新建事件，历史处理记录保留
    r = client.post("/api/events", json={
        "title": "设备年检临时停运", "reason": "equipment",
        "event_date": board["event"]["event_date"], "slot_ids": [slot_10],
    })
    assert r.status_code == 201
    new_event_id = r.json()["id"]
    assert new_event_id != event_id

    old_board = client.get(f"/api/dispatch/board?event_id={event_id}").json()
    assert len(old_board["actions"]) == 3, "此前事件的处理记录必须保留"

    # 8. 风速模拟接口
    r = client.post("/api/mock/wind", json={"speed": 18.5})
    assert r.json()["exceeded"] is True
    r = client.post("/api/mock/wind", json={"speed": 5.0})
    assert r.json()["exceeded"] is False


def test_gate_verify_blocks_used_ticket_flow(client):
    """过闸核销后，该票不能再按未使用票退款。"""
    order = client.get("/api/orders/ORD-1002").json()
    used = [t for t in order["tickets"] if t["status"] == "used"]
    assert used, "种子数据中 ORD-1002 应有一张已过闸的票"
    slot_id = used[0]["slot"]["id"]

    r = client.post("/api/events", json={
        "title": "临时停运", "reason": "equipment",
        "event_date": used[0]["slot"]["date"], "slot_ids": [slot_id],
    })
    event_id = r.json()["id"]

    r = client.post(f"/api/tickets/{used[0]['id']}/action",
                    json={"action_type": "refund",
                          "idempotency_key": f"flow-{uuid.uuid4().hex}"})
    assert r.status_code == 409
    assert "过闸" in r.json()["detail"]["message"]

    # 同订单未过闸的票仍可正常处理
    fresh = [t for t in order["tickets"] if t["status"] == "active"][0]
    r = client.post(f"/api/tickets/{fresh['id']}/action",
                    json={"action_type": "refund",
                          "idempotency_key": f"flow-{uuid.uuid4().hex}"})
    assert r.status_code == 200

    # 清理：恢复事件，避免影响其他测试
    client.post(f"/api/events/{event_id}/resume",
                json={"manager_name": "李强", "confirm": True})
