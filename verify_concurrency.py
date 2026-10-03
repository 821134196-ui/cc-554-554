"""并发与一致性验证脚本（针对真实运行中的 uvicorn 发 HTTP 请求）。

验证项：
  1. 多名调度员并发递补 / 并发自动递补 / 游客并发改签 —— 绝不超售；
  2. 同一张票并发重复处理（退款×10、退款/改签/候补混发）—— 只有一个结果生效；
  3. 支付回执晚到、失败后又来成功、并发重复回执 —— 不推翻已确定的票务结果；
  4. 已过闸票不能按未使用票退款；未恢复不能递补；恢复必须设备负责人+风速达标；
  5. seat_allocations 数据库层唯一约束无任何重复行。

用法：python3 verify_concurrency.py
脚本会自行准备测试库、启动/关闭 uvicorn。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEST_DB = ROOT / "backend" / "verify_test.db"
PORT = int(os.environ.get("VERIFY_PORT", "8123"))
BASE = f"http://127.0.0.1:{PORT}"

PASS, FAIL = 0, 0
FAILURES: list[str] = []


def check(label: str, cond: bool, evidence: str = ""):
    global PASS, FAIL
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {label}" + (f"  ｜ {evidence}" if evidence else ""))
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(label)


def http(method: str, path: str, body=None, timeout: float = 15.0):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def wait_health(proc, seconds: float = 15.0):
    t0 = time.time()
    while time.time() - t0 < seconds:
        if proc.poll() is not None:
            raise RuntimeError("uvicorn 提前退出")
        try:
            s, _ = http("GET", "/api/health", timeout=2)
            if s == 200:
                return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("服务未在规定时间内就绪")


def concurrently(fn, items, workers: int | None = None):
    with ThreadPoolExecutor(max_workers=workers or max(2, len(items))) as ex:
        return list(ex.map(fn, items))


# ---------------------------------------------------------------- 准备数据

def prepare_database():
    """独立测试库：基础种子 + 额外 8 张停运班票（供并发场景消耗）。"""
    for ext in ("", "-wal", "-shm"):
        p = Path(str(TEST_DB) + ext)
        if p.exists():
            p.unlink()
    os.environ["CABLEWAY_DB"] = str(TEST_DB)
    sys.path.insert(0, str(ROOT))
    from backend.seed import seed  # noqa: E402
    from backend.database import SessionLocal  # noqa: E402
    from backend.models import Order, SeatAllocation, Slot, Ticket  # noqa: E402
    from sqlalchemy import select, text  # noqa: E402

    seed()
    db = SessionLocal()
    slot0900 = db.scalar(select(Slot).where(Slot.code == "0900"))
    o = Order(order_no="VERIFY-EXTRA", contact_name="压测团", contact_phone="13900000000")
    db.add(o)
    db.flush()
    for i, name in enumerate(["压一", "压二", "压三", "压四", "压五",
                              "压六", "压七", "压八", "压九"]):
        t = Ticket(ticket_no=f"CW-VERIFY-{name}", order_id=o.id,
                   passenger_name=name, slot_id=slot0900.id,
                   event_id=1)  # 直接归入活动事件范围
        db.add(t)
        db.flush()
        nxt = db.execute(text("SELECT COALESCE(MAX(seat_seq),0)+1 FROM seat_allocations "
                              "WHERE slot_id=:s"), {"s": slot0900.id}).scalar()
        db.add(SeatAllocation(slot_id=slot0900.id, seat_seq=nxt, ticket_id=t.id))
    db.commit()
    db.close()


def collect_scope_ticket_ids() -> list[int]:
    ids = []
    s, data = http("GET", "/api/orders")
    for o in data["orders"]:
        _, detail = http("GET", f"/api/orders/{o['order_no']}")
        for t in detail["tickets"]:
            if t.get("can_handle"):
                ids.append(t["id"])
    return ids


def slot_map() -> dict:
    s, data = http("GET", "/api/slots")
    return {x["code"]: x for x in data["slots"]}


def occ(slots: dict, code: str) -> tuple[int, int, int]:
    s, d = http("GET", "/api/dashboard")
    row = [x for x in d["slots"] if x["code"] == code][0]
    return row["occupied"], row["capacity"], row["remaining"]


# ---------------------------------------------------------------- 各场景

def scenario_resume_gate(slots):
    print("\n== 场景 0：恢复权限与停运锁定 ==")
    # 先拿一张票候补，未恢复时递补必须被拒
    ids = collect_scope_ticket_ids()
    s, d = http("POST", f"/api/tickets/{ids[0]}/waitlist",
                {"target_slot_id": slots["1600"]["id"]})
    check("停运期间可加入候补", s == 200, f"http {s}")
    s, d = http("POST", "/api/dispatch/fill", {"slot_id": slots["1600"]["id"]})
    check("未恢复运营时递补被拦截(400 not_resumed)",
          s == 400 and d.get("code") == "not_resumed", f"http {s} {d.get('code')}")

    http("POST", "/api/mock/wind", {"speed": 19.0})
    s, d = http("POST", "/api/events/1/resume",
                {"confirmer": "王调度", "role": "dispatcher"})
    check("非设备负责人不能确认恢复", s == 400 and d.get("code") == "not_equipment_owner",
          f"http {s}")
    s, d = http("POST", "/api/events/1/resume",
                {"confirmer": "李工", "role": "equipment_owner"})
    check("风速超限时设备负责人也不能恢复", s == 400 and d.get("code") == "wind_unsafe",
          f"http {s} {d.get('error', '')[:40]}")
    http("POST", "/api/mock/wind", {"speed": 8.0})
    s, d = http("POST", "/api/events/1/resume",
                {"confirmer": "李工", "role": "equipment_owner", "remark": "复检正常"})
    check("风速达标+设备负责人确认 → 恢复", s == 200, f"http {s}")
    s, d = http("POST", "/api/events/1/resume",
                {"confirmer": "李工", "role": "equipment_owner"})
    check("重复确认恢复被拦截(409)", s == 409, f"http {s}")
    return ids


def scenario_concurrent_fill(tickets, slots):
    print("\n== 场景 1：6 人候补 1 个名额，6 名调度员同时点击递补 ==")
    target = slots["1600"]
    s, d = http("POST", f"/api/dispatch/slots/{target['id']}/capacity", {"capacity": 4})
    check("把恢复后运力调到 4（已占 3，实际空余 1）", s == 200, str(d))
    pool = tickets[0:6]  # 第 0 张已在场景 0 入候补
    for tid in pool[1:]:
        st, _ = http("POST", f"/api/tickets/{tid}/waitlist",
                     {"target_slot_id": target["id"]})
        assert st == 200, f"候补失败 ticket={tid}"

    ops = ["dispatcher-A", "dispatcher-B"] * 3
    results = concurrently(
        lambda i: http("POST", "/api/dispatch/fill",
                       {"slot_id": target["id"], "operator": ops[i]}),
        range(6))
    success = [r for r in results if r[0] == 200 and r[1].get("count") == 1]
    nullfill = [r for r in results if r[0] == 200 and r[1].get("filled") is None]
    check("6 个并发递补恰好 1 个拿到名额", len(success) == 1,
          f"成功 {len(success)}，空转 {len(nullfill)}，HTTP {sorted({r[0] for r in results})}")
    occupied, cap, remaining = occ(slots, "1600")
    check("目标班次占用恰好 4/4，未超售", occupied == 4 and remaining == 0,
          f"occupied={occupied}, capacity={cap}, remaining={remaining}")
    # 成功递补的票彼此不重复
    filled_nos = [r[1]["filled"]["new_ticket_no"] for r in success]
    check("胜出的递补只有一张票", len(set(filled_nos)) == 1, str(filled_nos))


def scenario_concurrent_autofill(tickets, slots):
    print("\n== 场景 2：11 人候补同一班次（10 座），两名调度员同时点“一键递补” ==")
    target = slots["1030"]  # 空班，10 座
    pool = tickets[6:17]
    for tid in pool:
        st, _ = http("POST", f"/api/tickets/{tid}/waitlist",
                     {"target_slot_id": target["id"]})
        assert st == 200, f"候补失败 ticket={tid}"
    results = concurrently(
        lambda op: http("POST", "/api/dispatch/auto-fill", {"operator": op}),
        ["dispatcher-A", "dispatcher-B"], workers=2)
    total = sum(r[1].get("count", -1) for r in results if r[0] == 200)
    a, b = (results[0][1].get("count"), results[1][1].get("count"))
    check("两个并发自动递补合计恰好出票 10 张", total == 10,
          f"A={a}, B={b}, 合计={total}")
    occupied, cap, remaining = occ(slots, "1030")
    check("1030 班次恰好 10/10，未超售", occupied == 10 and remaining == 0,
          f"occupied={occupied}")
    all_new = [f["new_ticket_no"] for r in results for f in r[1].get("filled", [])]
    check("两名调度员的出票结果无重叠", len(all_new) == len(set(all_new)),
          f"出票 {len(all_new)} 张，去重后 {len(set(all_new))}")


def scenario_concurrent_rebook(tickets, slots):
    print("\n== 场景 3：5 名游客并发改签仅剩 2 座的班次 ==")
    target = slots["1430"]  # 已售 6/8
    pool = tickets[17:22]
    results = concurrently(
        lambda tid: http("POST", f"/api/tickets/{tid}/rebook",
                         {"target_slot_id": target["id"]}),
        pool)
    ok = [r for r in results if r[0] == 200]
    full = [r for r in results if r[0] == 409 and r[1].get("code") == "slot_full"]
    check("5 个并发改签恰好 2 个成功、3 个满员 409",
          len(ok) == 2 and len(full) == 3,
          f"成功 {len(ok)}，满员拒绝 {len(full)}，其他 {[(r[0], r[1].get('code')) for r in results if r not in ok and r not in full]}")
    occupied, cap, remaining = occ(slots, "1430")
    check("1430 班次恰好 8/8，未超售", occupied == 8 and remaining == 0,
          f"occupied={occupied}")


def scenario_duplicate_ticket(tickets, slots):
    print("\n== 场景 4：同一张票并发重复处理 ==")
    tid = tickets[22]
    # 4.1 同一笔退款并发 10 次
    results = concurrently(
        lambda _: http("POST", f"/api/tickets/{tid}/refund", {}), range(10), workers=10)
    ok = [r for r in results if r[0] == 200]
    blocked = [r for r in results if r[0] == 409]
    check("同票并发退款 10 次：恰好 1 次成功，9 次冲突拦截",
          len(ok) == 1 and len(blocked) == 9,
          f"成功 {len(ok)}，409 {len(blocked)}，其他 {[r[0] for r in results if r[0] not in (200,409)]}")
    # 4.2 已退款票再尝试改签 / 再退款 / 候补，全部顺序拦截
    s1, _ = http("POST", f"/api/tickets/{tid}/rebook",
                 {"target_slot_id": slots["1600"]["id"]})
    s2, _ = http("POST", f"/api/tickets/{tid}/refund", {})
    s3, _ = http("POST", f"/api/tickets/{tid}/waitlist",
                 {"target_slot_id": slots["1600"]["id"]})
    check("退款终态票：改签/再退款/候补全部 409",
          s1 == 409 and s2 == 409 and s3 == 409, f"{s1}/{s2}/{s3}")

    # 4.3 另一张票：退款 / 改签 / 候补 三类请求同时混发，只能有一个结果
    tid2 = tickets[23]

    def mixed_call(kind: str):
        # 注意：必须用分支，不能用 dict 字面量——后者会在每次调用时把
        # 三个 HTTP 请求全部发出（每个线程发 3 个），导致 9 个请求打同一张票。
        if kind == "refund":
            return "refund", http("POST", f"/api/tickets/{tid2}/refund", {})
        if kind == "rebook":
            return "rebook", http("POST", f"/api/tickets/{tid2}/rebook",
                                  {"target_slot_id": slots["1600"]["id"]})
        return "wait", http("POST", f"/api/tickets/{tid2}/waitlist",
                            {"target_slot_id": slots["1600"]["id"]})

    mixed = concurrently(mixed_call, ["refund", "rebook", "wait"], workers=3)
    winners = [k for k, r in mixed if r[0] == 200]
    check("退款/改签/候补混发同一张票：仅一种处理生效", len(winners) == 1,
          f"生效={winners}, 全部响应={[(k, r[0]) for k, r in mixed]}")
    s, d = http("GET", "/api/orders/VERIFY-EXTRA")  # 票在该订单不影响，仅查状态用
    # 直接查票状态：用 dashboard 无法查，改用 DB 只读连接
    conn = sqlite3.connect(str(TEST_DB))
    status = conn.execute("SELECT status FROM tickets WHERE id=?", (tid2,)).fetchone()[0]
    conn.close()
    expected = {"refund": "refunded", "rebook": "reassigned", "wait": "waitlisted"}
    check("票最终状态与唯一胜出操作一致", status == expected[winners[0]],
          f"winner={winners[0]}, db status={status}")


def scenario_late_callbacks(tickets):
    print("\n== 场景 5：支付回执晚到 / 失败后又成功 / 并发重复回执 ==")
    http("POST", "/api/mock/payment/config",
         {"callback_delay": 0, "auto_callback": False})  # 回执全部人工控制
    tid = tickets[24]
    s, d = http("POST", f"/api/tickets/{tid}/refund", {})
    check("退款受理成功，回执挂起", s == 200 and d["gateway_status"] == "pending", str(d))
    pay_no = d["payment_no"]

    # 先到的是“失败”
    s, d = http("POST", f"/api/payments/{pay_no}/callback", {"status": "failed"})
    check("失败回执正常入账", s == 200 and d["result"] == "applied")
    # 随后晚到的“成功”回执绝不能改写
    s, d = http("POST", f"/api/payments/{pay_no}/callback",
                {"status": "succeeded", "gateway_ref": "LATE-SUCCESS"})
    check("晚到的成功回执被忽略（ignored_duplicate）",
          s == 200 and d["result"] == "ignored_duplicate" and d["gateway_status"] == "failed",
          str(d))
    conn = sqlite3.connect(str(TEST_DB))
    row = conn.execute("SELECT status FROM tickets WHERE id=?", (tid,)).fetchone()
    pay = conn.execute("SELECT gateway_status FROM payments WHERE payment_no=?",
                       (pay_no,)).fetchone()
    ignored = conn.execute(
        "SELECT json_array_length(detail, '$.ignored_callbacks') FROM payments WHERE payment_no=?",
        (pay_no,)).fetchone()[0]
    conn.close()
    check("票结果始终为 refunded，不被任何回执推翻", row[0] == "refunded", f"ticket={row[0]}")
    check("支付单保持首个终态 failed，且留痕了被忽略回执",
          pay[0] == "failed" and ignored >= 1, f"payment={pay[0]}, ignored={ignored}")

    # 重发退款 → 成功；此后再来失败回执同样不能推翻
    s, d = http("POST", f"/api/payments/{pay_no}/retry", {})
    check("失败后重新发起退款", s == 200 and d["result"] == "resubmitted")
    s, d = http("POST", f"/api/payments/{pay_no}/callback", {"status": "succeeded"})
    check("重新发起后成功回执入账", s == 200 and d["gateway_status"] == "succeeded")
    results = concurrently(lambda st: http(
        "POST", f"/api/payments/{pay_no}/callback", {"status": st}),
        ["failed", "succeeded", "failed", "succeeded", "failed"], workers=5)
    ignored_all = all(r[1].get("result") == "ignored_duplicate" for r in results)
    conn = sqlite3.connect(str(TEST_DB))
    final = conn.execute("SELECT status FROM tickets WHERE id=?", (tid,)).fetchone()[0]
    final_pay = conn.execute("SELECT gateway_status FROM payments WHERE payment_no=?",
                             (pay_no,)).fetchone()[0]
    conn.close()
    check("5 个并发重复回执全部忽略，终态保持成功", ignored_all and final_pay == "succeeded",
          f"payment={final_pay}")
    check("票结果仍为 refunded（资金重试不影响票务结论）", final == "refunded", f"ticket={final}")

    # 闸机对退款票拒绝放行
    conn = sqlite3.connect(str(TEST_DB))
    tno = conn.execute("SELECT ticket_no FROM tickets WHERE id=?", (tid,)).fetchone()[0]
    conn.close()
    s, d = http("POST", "/api/gate/checkin", {"ticket_no": tno})
    check("退款票无法过闸", s == 400 and d.get("code") == "refunded_gate", f"http {s}")


def scenario_checked_in(slots):
    print("\n== 场景 6：已过闸票 / 停运范围 / 再次停运 ==")
    _, order = http("GET", "/api/orders/2026100302")
    ci = [t for t in order["tickets"] if t["checked_in"]][0]
    s, d = http("POST", f"/api/tickets/{ci['id']}/refund", {})
    check("已过闸票不能按未使用票退款", s == 400 and d.get("code") == "already_checked_in",
          d.get("error", "")[:40])
    s, d = http("POST", f"/api/tickets/{ci['id']}/rebook",
                {"target_slot_id": slots["1600"]["id"]})
    check("已过闸票不能改签", s == 400 and d.get("code") == "already_checked_in", f"http {s}")

    # 再次停运必须新建事件，旧事件与处理记录保留
    http("POST", "/api/mock/wind", {"speed": 19.0})
    s, d = http("POST", "/api/events",
                {"reason": "equipment", "affected_slot_ids": [slots["1030"]["id"]],
                 "detail": "轮组异响复检", "created_by": "verify"})
    check("再次停运新建事件 #2", s == 200 and d["event_id"] == 2, str(d))
    s, data = http("GET", "/api/events")
    evs = {e["id"]: e for e in data["events"]}
    check("旧事件 #1 及其恢复记录保留", evs[1]["status"] == "resumed" and evs[1]["resumed_by"] == "李工")
    check("新事件 #2 为活动状态", evs[2]["status"] == "active")

    # 历史处理记录仍可追溯：旧票上有“入候补→递补”，新票上有“出票”
    conn = sqlite3.connect(str(TEST_DB))
    old = conn.execute(
        "SELECT ticket_id FROM ticket_actions WHERE action='waitlist_fill' LIMIT 1").fetchone()[0]
    old_actions = {r[0] for r in conn.execute(
        "SELECT action FROM ticket_actions WHERE ticket_id=?", (old,))}
    issued = conn.execute(
        "SELECT COUNT(*) FROM ticket_actions WHERE action='issued'").fetchone()[0]
    new_ticket = conn.execute(
        "SELECT id FROM tickets WHERE source_ticket_id=? AND status='unused'",
        (old,)).fetchone()
    conn.close()
    check("递补票保留完整流水（旧票：入候补→递补；新票：出票）",
          {"waitlist_join", "waitlist_fill"} <= old_actions and issued >= 1
          and new_ticket is not None,
          f"old={sorted(old_actions)}, issued={issued}")


def scenario_db_invariants():
    print("\n== 场景 7：数据库层不变量 ==")
    conn = sqlite3.connect(str(TEST_DB))
    dup = conn.execute(
        "SELECT slot_id, seat_seq, COUNT(*) c FROM seat_allocations "
        "GROUP BY slot_id, seat_seq HAVING c > 1").fetchall()
    check("seat_allocations 无重复 (slot, seat) —— 同一名额绝不可能给两人",
          dup == [], str(dup))
    over = conn.execute(
        "SELECT s.code, COUNT(a.id), s.capacity FROM slots s "
        "JOIN seat_allocations a ON a.slot_id=s.id "
        "GROUP BY s.id HAVING COUNT(a.id) > s.capacity").fetchall()
    check("所有班次占用数不超过运力（无超售）", over == [], str(over))
    # 互斥性：原票一旦 reassigned/refunded 不应再有支付/占位歧义
    bad = conn.execute(
        "SELECT COUNT(*) FROM tickets t WHERE t.status='refunded' AND EXISTS ("
        "SELECT 1 FROM seat_allocations a WHERE a.ticket_id=t.id)").fetchone()[0]
    check("退款票不再持有任何名额", bad == 0, f"违规 {bad} 张")
    conn.close()


def main():
    print("准备测试数据库 …")
    prepare_database()
    env = os.environ.copy()
    env["CABLEWAY_DB"] = str(TEST_DB)
    _log = open("/tmp/verify_uvicorn.log", "w") if os.environ.get("TX_DEBUG") else subprocess.DEVNULL
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(ROOT), env=env,
        stdout=_log, stderr=subprocess.STDOUT)
    try:
        wait_health(proc)
        slots = slot_map()
        tickets = collect_scope_ticket_ids()
        print(f"停运范围内可处理票 {len(tickets)} 张，班次 {list(slots)}")
        assert len(tickets) >= 25, f"测试票不足：{len(tickets)}"

        scenario_resume_gate(slots)
        scenario_concurrent_fill(tickets, slots)
        scenario_concurrent_autofill(tickets, slots)
        scenario_concurrent_rebook(tickets, slots)
        scenario_duplicate_ticket(tickets, slots)
        scenario_late_callbacks(tickets)
        scenario_checked_in(slots)
        scenario_db_invariants()
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("\n" + "=" * 64)
    print(f"结果：{PASS} 通过，{FAIL} 失败")
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print("  -", f)
    print("=" * 64)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
