"""数据库引擎与会话管理。

并发正确性的基础：
1. WAL 模式 + busy_timeout，读写不互相阻塞，写锁等待而非立即报错。
2. 所有事务以 BEGIN IMMEDIATE 开启（连接级事件替换 pysqlite 默认的延迟 BEGIN），
   写事务从第一条 SQL 起即持有保留锁，并发写请求被串行化，
   配合“条件 UPDATE + rowcount 校验”即可保证名额不超售、票据状态不冲突。
"""
import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

DB_PATH = os.environ.get(
    "CABLECAR_DB",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cablecar.db")),
)
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@event.listens_for(engine, "begin")
def _begin_immediate(conn):
    # 以 BEGIN IMMEDIATE 替代默认的延迟 BEGIN：事务一开始即取得写锁，
    # 之后的条件 UPDATE 读到的一定是最新已提交数据。
    conn.exec_driver_sql("BEGIN IMMEDIATE")


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
