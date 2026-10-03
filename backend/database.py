"""数据库引擎与会话。

并发要点：
- SQLite 默认日志模式下写锁是数据库级的，开启 WAL 后读写不互斥，但写写仍串行。
- 每个写事务以 ``BEGIN IMMEDIATE`` 开启：连接立刻获取 RESERVED 写锁，
  而不是等第一次写时才升级。多个调度员并发递补时，第二个事务会在
  ``BEGIN IMMEDIATE`` 处阻塞/收到 SQLITE_BUSY，由我们重试，从根本上
  避免两个事务同时读到同一个剩余名额。
- busy_timeout 让锁等待最多等 8 秒，配合应用层重试兜底。
"""
from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Callable, TypeVar

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

T = TypeVar("T")

DB_PATH = os.environ.get("CABLEWAY_DB", os.path.join(os.path.dirname(__file__), "cableway.db"))
DB_URL = f"sqlite:///{DB_PATH}"


class Base(DeclarativeBase):
    pass


engine = create_engine(
    DB_URL,
    connect_args={"timeout": 8, "check_same_thread": False},
    future=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    # 关闭 pysqlite 在 DML 前自动发 BEGIN 的行为（SQLAlchemy 官方 SQLite 配方）。
    # 写事务由 run_tx() 显式发出 BEGIN IMMEDIATE；只读查询因此在
    # 自动提交模式下执行，WAL 允许读写并发、互不阻塞。
    dbapi_conn.isolation_level = None
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=8000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()



SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_session() -> Iterator[Session]:
    """FastAPI 依赖：请求级会话（只读自动提交；写操作请用 run_tx）。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_tx(fn: Callable[[Session], T], retries: int = 8, commit_retries: int = 6) -> T:
    """在独立短事务中执行写操作。

    以 BEGIN IMMEDIATE 立即取 RESERVED 写锁，所有名额/票状态检查都在
    同一把锁内完成并提交，保证并发递补绝不超售。

    两层 BUSY 处理（关键）：
    1. BEGIN/执行期收到 SQLITE_BUSY：事务里什么都还没提交，整体重试；
    2. COMMIT 瞬间收到 SQLITE_BUSY：事务仍然存活，提交结果未定——
       此时绝不能整体重试（那会把一次成功误判成“重复处理”冲突），
       而是按 SQLite 官方建议在同一连接上重发 COMMIT，直到恰好一次提交。
    """
    for attempt in range(retries):
        db = SessionLocal()
        try:
            db.execute(text("BEGIN IMMEDIATE"))
            result = fn(db)
            raw = db.connection().connection  # sqlite3.Connection
            try:
                db.commit()
            except OperationalError as exc:
                if not _is_busy(ex):
                    db.rollback()
                    raise
                # 拿住底层连接：事务仍开着就重发 COMMIT 本身
                if getattr(raw, "in_transaction", False):
                    _retry_raw_commit(raw, commit_retries)
                # in_transaction 为 False：提交已落库，BUSY 只是返回路径误报
                return result
            return result
        except OperationalError as exc:
            db.rollback()
            if not _is_busy(exc) or attempt == retries - 1:
                raise
            time.sleep(0.02 * (attempt + 1))
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    raise RuntimeError("unreachable")


def _is_busy(exc: OperationalError) -> bool:
    msg = str(exc.orig if hasattr(exc, "orig") else exc).lower()
    return "locked" in msg or "busy" in msg


def _retry_raw_commit(raw, attempts: int):
    for i in range(attempts):
        try:
            raw.execute("COMMIT")
            return
        except Exception as exc:  # sqlite3.OperationalError
            if not ("locked" in str(exc).lower() or "busy" in str(exc).lower()) \
                    or i == attempts - 1:
                raw.execute("ROLLBACK")
                raise
            time.sleep(0.02 * (i + 1))
