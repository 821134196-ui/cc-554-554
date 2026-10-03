# 景区缆车停运退票与候补改签系统

缆车因天气/设备原因停运时，游客在原订单中按同行人分别选择 **退款 / 换时段 / 加入候补**；
调度员在调度台按恢复后的运力安排递补。系统保证：**名额不超售、同票不重复处理、晚到回执不推翻已确定结果**。

## 一条命令启动

```bash
./start.sh            # 首次自动建虚拟环境装依赖，然后启动服务
```

打开浏览器：

| 页面 | 地址 | 说明 |
|---|---|---|
| 游客服务 | http://127.0.0.1:8000/ | 查询订单（示例 `ORD-1001` ~ `ORD-1004`），按同行人分别退款/换时段/候补 |
| 调度台 | http://127.0.0.1:8000/dispatch.html | 各时段已售/剩余并列展示，候补队列递补 |
| 管理台 | http://127.0.0.1:8000/admin.html | 风速/闸机/支付模拟接口，创建停运事件，设备负责人确认恢复 |

运行验证测试：

```bash
.venv/bin/python -m pytest tests/ -v
```

## 典型操作流程

1. **管理台**：设定风速超阈值 → 创建停运事件（勾选受影响班次）。
2. **游客页**：查询订单 → 每张票分别选择退款（等支付回执）、换时段（即时生效）或候补（排队）。
3. **管理台**：支付网关模拟区给退款单发「成功回执」；闸机模拟区可核销票（已过闸的票不能再退款）。
4. **管理台**：设备负责人（张伟/李强）勾选确认 → 恢复运营。
5. **调度台**：候补队列按实际空余名额递补（可逐个或自动）；两名调度员同时操作也不会超售。
6. 再次停运：新建事件，历史事件与处理记录全部保留。

## 技术架构

- **后端** Python + FastAPI + SQLAlchemy + SQLite（WAL 模式）
- **前端** Vue 3（本地 vendor，无构建步骤），FastAPI 直接托管静态页
- **模拟接口** `/api/mock/wind`（风速）、`/api/mock/gate/verify`（闸机核销）、`/api/mock/payment/callback`（支付回执）

## 一致性设计（对应三项验证）

1. **并发递补不超售** —— 所有事务以 `BEGIN IMMEDIATE` 开启，名额占用使用条件更新
   `UPDATE time_slots SET sold=sold+1 WHERE id=? AND sold<capacity` 并校验影响行数；
   两名调度员同时递补同一名额，只有一人成功（`tests/test_concurrent_promote.py`）。
2. **重复处理被拦截** —— `(ticket_id, event_id)` 数据库唯一约束 + 状态前置校验，
   同一张票不能既退款又改签；已过闸的票拒绝按未使用票退款；
   游客端每次操作携带幂等键，重试/双击返回首次结果（`tests/test_duplicate_action.py`）。
3. **晚到回执不推翻结果** —— 支付回执按 `callback_id` 幂等；处理单已确定（confirmed/rejected）后，
   晚到回执只记录不生效（`tests/test_late_callback.py`）。

## 目录结构

```
app/
  main.py            # FastAPI 入口（建表、种子数据、路由、静态页）
  database.py        # SQLite 引擎：WAL + busy_timeout + BEGIN IMMEDIATE
  models.py          # 班次/订单/车票/停运事件/处理记录/候补/回执/审计
  services.py        # 业务核心：事件、退改候补、递补、回执、核销
  routers/           # public(游客) / events / dispatch / mock
  seed.py            # 示例班次、员工、订单
  static/            # Vue 页面（游客/调度/管理）
tests/               # 并发与一致性验证 + 端到端业务流
start.sh             # 一条命令启动
```
