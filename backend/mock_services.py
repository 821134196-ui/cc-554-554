"""本地模拟外部接口：风速站、支付网关。

- WindService：内存中的当前风速，运营端可调节，停运/恢复/过闸时读取。
- PaymentGateway：模拟异步支付通道。提交后不立即给结果，而是由后台线程
  在 ``callback_delay`` 秒后把回执送回业务层（独立事务），还原
  “支付回执晚到”的真实时序；并支持手动送达、模拟失败、失败后补送成功。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

# 风速安全阈值（m/s）。超过即视为不具备运行条件。
WIND_LIMIT = 15.0


class WindService:
    """风速模拟站。

    风速值持久化在 system_state 表中（种子进程设置后，Web 进程能读到），
    读取走 WAL 自动提交，永远拿到最新已提交值。
    """

    KEY = "wind_speed"

    def __init__(self, default: float = 8.0, limit: float = WIND_LIMIT):
        self._lock = threading.Lock()
        self.default = default
        self.limit = limit

    def get(self) -> float:
        from sqlalchemy import select, text
        from .database import SessionLocal
        db = SessionLocal()
        try:
            row = db.execute(text("SELECT value FROM system_state WHERE key=:k"),
                             {"k": self.KEY}).first()
            return float(row[0]) if row else self.default
        finally:
            db.close()

    def set(self, speed: float) -> float:
        from sqlalchemy import text
        from .database import run_tx
        speed = max(0.0, float(speed))
        def tx(db):
            db.execute(text(
                "INSERT INTO system_state(key,value) VALUES(:k,:v) "
                "ON CONFLICT(key) DO UPDATE SET value=:v"),
                {"k": self.KEY, "v": str(speed)})
        with self._lock:
            run_tx(tx)
        return speed

    def is_safe(self) -> bool:
        return self.get() < self.limit

    def snapshot(self) -> dict:
        return {"speed": self.get(), "limit": self.limit, "safe": self.is_safe()}


@dataclass
class GatewayConfig:
    # 自动回执延迟秒数（0 表示尽快）
    callback_delay: float = 2.0
    # 是否自动回送回执；关闭后只能人工送达（便于演示/测试竞态）
    auto_callback: bool = True
    # 为真时，下一笔提交的回执直接失败（一次性开关）
    fail_next: bool = False
    _fail_seq: set = field(default_factory=set)  # 强制失败的 payment_no


class PaymentGateway:
    """极简异步通道：只负责记账状态与延迟投递，业务判定全部在 services 层。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.config = GatewayConfig()
        # 已投递过终态回执的 payment_no，重复投递直接丢弃（通道侧幂等视角）
        self._delivered: set[str] = set()

    # ---- 配置（mock 管理端） ----
    def configure(self, callback_delay: float | None = None,
                  auto_callback: bool | None = None,
                  fail_next: bool | None = None) -> dict:
        with self._lock:
            if callback_delay is not None:
                self.config.callback_delay = max(0.0, float(callback_delay))
            if auto_callback is not None:
                self.config.auto_callback = bool(auto_callback)
            if fail_next is not None:
                self.config.fail_next = bool(fail_next)
            return self._config_locked()

    def _config_locked(self) -> dict:
        return {
            "callback_delay": self.config.callback_delay,
            "auto_callback": self.config.auto_callback,
            "fail_next": self.config.fail_next,
        }

    def config_snapshot(self) -> dict:
        with self._lock:
            return self._config_locked()

    def force_fail(self, payment_no: str):
        with self._lock:
            self.config._fail_seq.add(payment_no)

    # ---- 通道提交与回执 ----
    def submit(self, payment_no: str):
        """业务层提交一笔退款/收费。通道按配置延迟投递回执。"""
        with self._lock:
            will_fail = self.config.fail_next or payment_no in self.config._fail_seq
            self.config.fail_next = False
            delay = self.config.callback_delay
            auto = self.config.auto_callback
        if auto:
            t = threading.Thread(
                target=self._deliver_later,
                args=(payment_no, "failed" if will_fail else "succeeded", delay),
                daemon=True,
                name=f"pay-cb-{payment_no}",
            )
            t.start()

    def _deliver_later(self, payment_no: str, status: str, delay: float):
        if delay:
            threading.Event().wait(delay)
        # 延迟导入，避免 gateway ↔ services 循环依赖
        from . import services
        try:
            services.apply_callback(payment_no, status,
                                    gateway_ref=f"GW-{payment_no}-LATE")
        except Exception as exc:  # 通道投递异常绝不拖垮主线程
            print(f"[gateway] callback delivery failed for {payment_no}: {exc}")

    def deliver_now(self, payment_no: str, status: str) -> bool:
        """人工立即送达终态回执（mock 控制台用）。"""
        from . import services
        services.apply_callback(payment_no, status,
                                gateway_ref=f"GW-{payment_no}-MANUAL")
        return True


wind_service = WindService()
payment_gateway = PaymentGateway()
