"""测试基建：独立测试库、TestClient、真实 uvicorn 服务（并发测试用）、数据工厂。"""
import os
import socket
import tempfile
import threading
import time
import uuid

# 必须在导入 app 之前指定测试数据库
os.environ["CABLECAR_DB"] = os.path.join(tempfile.mkdtemp(prefix="cablecar_test_"), "test.db")

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, Ticket, TimeSlot  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.services import choose_action, create_event  # noqa: E402

Base.metadata.create_all(engine)
with SessionLocal() as _db:
    seed_if_empty(_db)


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


class _LiveServer:
    def __init__(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("uvicorn 未能启动")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture(scope="session")
def live():
    server = _LiveServer()
    server.start()
    yield f"http://127.0.0.1:{server.port}"
    server.stop()


class Factory:
    """直接在建库会话中构造场景数据（各测试用唯一后缀，互不干扰）。"""

    def __init__(self):
        self.tag = uuid.uuid4().hex[:8]

    def slot(self, date="2099-01-01", start="10:00", capacity=8, sold=0, status="normal"):
        with SessionLocal() as db:
            s = TimeSlot(date=date, start_time=start, end_time="11:00",
                         capacity=capacity, sold=sold, status=status)
            db.add(s)
            db.commit()
            return s.id

    def order_with_tickets(self, slot_id, n, date="2099-01-01"):
        with SessionLocal() as db:
            no = f"ORD-T-{self.tag}-{uuid.uuid4().hex[:6]}"
            order = Order(order_no=no, visitor_name="测试游客", phone="13900000000")
            db.add(order)
            db.flush()
            ids = []
            for i in range(n):
                t = Ticket(ticket_code=f"{no}-{i+1}", order_id=order.id, slot_id=slot_id,
                           passenger_name=f"乘客{i+1}", price=180, status="active")
                db.add(t)
                db.flush()
                ids.append(t.id)
                db.execute(
                    TimeSlot.__table__.update()
                    .where(TimeSlot.id == slot_id)
                    .values(sold=TimeSlot.sold + 1)
                )
            db.commit()
            return no, ids

    def event(self, slot_ids, title=None):
        with SessionLocal() as db:
            ev = create_event(
                db, title=title or f"测试停运-{self.tag}", reason="weather",
                event_date="2099-01-01", slot_ids=slot_ids,
                description="测试事件", operator="测试员",
            )
            return ev.id

    def waitlist(self, ticket_id, desired_slot_id, event_id):
        with SessionLocal() as db:
            action, _ = choose_action(
                db, ticket_id=ticket_id, action_type="waitlist",
                idempotency_key=f"setup-{uuid.uuid4().hex}",
                target_slot_id=desired_slot_id, operator="测试员",
            )
            return action.id


@pytest.fixture()
def factory():
    return Factory()


def waitlist_entry_id(ticket_id, event_id):
    from app.models import WaitlistEntry
    with SessionLocal() as db:
        return db.scalar(
            select(WaitlistEntry.id).where(
                WaitlistEntry.ticket_id == ticket_id,
                WaitlistEntry.event_id == event_id,
            )
        )


def fire_concurrently(urls_bodies, headers=None):
    """多线程并发 POST，返回 (status_code, json) 列表，顺序与输入一致。"""
    results = [None] * len(urls_bodies)

    def worker(i, url, body):
        with httpx.Client(timeout=30) as c:
            r = c.post(url, json=body)
            results[i] = (r.status_code, r.json())

    threads = [
        threading.Thread(target=worker, args=(i, u, b))
        for i, (u, b) in enumerate(urls_bodies)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results
