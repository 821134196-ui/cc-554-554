# 景区缆车停运退票与候补改签系统

缆车因天气或设备原因停运时，游客在原订单内对**同行人逐人**选择「退款 / 换时段 / 加入候补」；
调度员依据恢复后的实际运力安排改签与候补递补。系统把**停运范围、票的核销状态、退款/改签结果**
三者关联并全程留痕。

- 后端：Python 3 · FastAPI · SQLAlchemy 2 · SQLite（WAL）
- 前端：Vue 3（本地 vendor，无需联网/构建）
- 风速站、闸机核销、支付网关均为**本地模拟接口**，支持调节风速、延迟/失败/手动送达支付回执
- **一条命令启动**：`./run.sh`

---

## 一、快速开始

```bash
./run.sh            # 首次启动自动装依赖 + 初始化演示数据，随后起服务
# 打开 http://localhost:8000
```

- 重置演示数据：`./reset.sh`
- 换端口：`PORT=8080 ./run.sh`

页面四个标签页：

| 标签 | 角色 | 能做什么 |
|---|---|---|
| 游客端 | 游客 | 输入订单号查看班次状态与可选时段，对每位同行人分别退款/改签/候补，查看支付回执与处理流水 |
| 调度端 | 调度员 | 并列查看各时段运力/已售/剩余/候补，调整恢复后运力，单人递补或一键 FIFO 递补 |
| 运营/设备 | 运营、设备负责人 | 发起停运（选择范围/原因）、设备负责人确认恢复（校验风速）、查看历次事件 |
| 本地模拟接口 | — | 调节风速、配置支付回执延迟/失败/手动送达、模拟闸机扫码核销 |

演示订单：`2026100301`（08:00 停运班，3 位同行人）、`2026100302`（含 1 张**已过闸**票）。
初始场景：08:00、09:00 因大风（19 m/s）停运，13:00 满员（候补目标），14:30 余 2 座（可直接改签）。

**推荐体验路径**：
1. 游客端 `2026100301`：一人退款、一人候补 13:00、一人改签到 14:30（试试第三人改签同班次会因满员被拒）；
   订单 `2026100302` 的已过闸票任何处理都被拒绝。
2. 模拟接口页：把风速调到 9 m/s。
3. 运营/设备页：勾选「设备负责人」→ 确认恢复（用非负责人身份或高风速会被拒绝）。
4. 调度端：把 13:00 运力调高（恢复加车），点「递补下一位」或「一键 FIFO 递补」，观察只在实际空余运力内出票。
5. 模拟接口页：关掉支付自动回执，退款后手动先送「失败」再送「成功」，观察票结果不被晚到回执推翻。

---

## 二、核心业务规则

### 票的生命周期

```
UNUSED（未使用）
 ├─ CHECKED_IN 已过闸核销（不可逆，不可再退/改）
 ├─ REFUNDED   已退款（终态）
 └─ WAITLISTED 候补中 ──FIFO 递补──► REASSIGNED（旧票终态）+ 新票 UNUSED
 REASSIGNED    已改签（旧票终态；新票可正常过闸）
```

- **退款与改签互斥**：一张票只能有一个最终结果；任何终态票的重复处理都返回 `409 Conflict`。
- **已过闸票不能按未使用票退款/改签**（`already_checked_in`）。
- **停运范围 × 核销状态 × 处理结果**关联：`SuspensionEvent.affected_slot_ids` 圈定范围，
  范围内每张票记录 `event_id`，结果落 `Ticket.status` + 不可变流水 `TicketAction`，资金落 `Payment`。
- **恢复运营必须设备负责人确认**，且当前风速低于安全阈值（15 m/s）；非负责人/高风速均拒绝。
- **再次停运新建事件**，旧事件与其全部处理记录永久保留，不覆盖、不删除。
- **候补只在实际空余运力内递补**，按 `(目标班次, 入列时间)` FIFO；停运未恢复前递补功能锁定。

### 并发一致性（重点）

- 每个写事务以 **`BEGIN IMMEDIATE`** 立即取 SQLite RESERVED 写锁；名额校验、票状态流转、
  占位写入在同一事务内完成并提交，多个调度员并发时在数据库入口处串行排队。
- `seat_allocations(slot_id, seat_seq)` 有**数据库唯一约束**兜底，同一个名额物理上不可能给两张票。
- 只读查询走 WAL 自动提交，读写互不阻塞。
- **COMMIT 阶段的 BUSY 按 SQLite 官方建议重发 COMMIT 本身**（而非重试整个事务），
  避免「提交已落库却向客户端报错、再重试被误判为重复处理」。
- **支付回执幂等且单向**：票结果在受理时即确定，回执只推进 `Payment` 的
  `pending → succeeded/failed`；终态后的任何晚到/并发重复回执都被忽略并留痕，绝不回滚票。

### 模拟接口

- 风速站：`GET/POST /api/meta/wind`（持久化在 `system_state` 表，跨进程生效），超限 → 闸机拒放、禁止恢复。
- 闸机：`POST /api/gate/checkin`，按票号核销；退款票、改签旧票、候补票、停运班、高风速一律拒绝。
- 支付网关：退款受理后按配置延迟异步投递回执；可关闭自动投递改为手动送达，可强制下一笔失败。

---

## 三、并发与一致性验证

脚本会自建独立测试库、起真实 uvicorn 进程，用多线程 HTTP 并发压测：

```bash
python3 verify_concurrency.py        # 38 项断言
```

覆盖：

- **并发递补不超售**：6 人候补 1 座 6 调度员同时递补（恰 1 人成功）；11 人候补 10 座两调度员
  同时一键递补（合计恰 10 张，出票无重叠）；5 人并发改签仅剩 2 座（恰 2 成功 3 满员）。
- **重复处理同一张票被拦截**：同票并发退款 10 次仅 1 次成功；退款终态票再退/改/候补全 409；
  退款/改签/候补三类请求混发同一张票，恰一种生效且最终状态一致。
- **晚到回执不推翻结果**：先失败后成功 → 成功被忽略；失败重发后成功，再并发 5 个重复回执全忽略；
  票始终 `refunded`。
- **权限与边界**：已过闸票不可退改、未恢复不可递补、恢复必须设备负责人+风速达标、不可重复恢复、
  再次停运新建事件且历史可追溯。
- **数据库不变量**：`seat_allocations` 无重复 `(slot, seat)`、各班次占用不超运力、退款票不占名额。

---

## 四、主要 API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/events` | 发起停运（reason、affected_slot_ids） |
| POST | `/api/events/{id}/resume` | 设备负责人确认恢复（role 必须 `equipment_owner`） |
| GET  | `/api/events` | 历次停运事件（含已恢复） |
| GET  | `/api/orders/{order_no}` | 订单详情：票状态、是否可处理、可选时段与余位 |
| POST | `/api/tickets/{id}/refund` | 退款（互斥、释放原名额） |
| POST | `/api/tickets/{id}/rebook` | 立即换时段（余位校验，旧票失效+出新票） |
| POST | `/api/tickets/{id}/waitlist` | 加入候补（满员也可排队） |
| POST | `/api/tickets/{id}/waitlist/cancel` | 撤回候补 |
| GET  | `/api/dashboard` | 调度看板：各时段运力/已售/剩余/候补/待回执 |
| POST | `/api/dispatch/slots/{id}/capacity` | 调整恢复后运力（不得低于已占名额） |
| POST | `/api/dispatch/fill` | 递补指定班次队首 1 人 |
| POST | `/api/dispatch/auto-fill` | 跨班次 FIFO 连续递补至无空可补 |
| POST | `/api/gate/checkin` | 闸机核销 |
| POST | `/api/payments/{no}/callback` | 支付终态回执（幂等，晚到安全） |
| POST | `/api/payments/{no}/retry` | 退款失败后重发（不动票） |
| POST | `/api/mock/wind` `/api/mock/payment/config` `/api/mock/payment/deliver` | 模拟接口控制 |

## 五、目录结构

```
backend/
  database.py    引擎/WAL/BEGIN IMMEDIATE/run_tx（含 COMMIT BUSY 处理）
  models.py      Slot/Order/Ticket/SuspensionEvent/TicketAction/WaitlistEntry/SeatAllocation/Payment/SystemState
  services.py    核心业务：状态机、容量分配、退/改/候补/递补、事件恢复、回执结算
  queries.py     只读序列化（订单、看板、流水）
  mock_services.py 风速站（持久化）+ 异步支付网关模拟
  app.py         FastAPI 路由 + 静态托管
  seed.py        演示数据
frontend/        Vue 3 单页（index.html / app.js / styles.css / vendor）
verify_concurrency.py  并发一致性验证（真实 uvicorn + 多线程）
run.sh / reset.sh
```
