from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.config import settings

_is_sqlite = settings.SQLALCHEMY_DATABASE_URL.startswith("sqlite")

_connect_args: dict = {}
_engine_kwargs: dict = {}
if _is_sqlite:
    # 允许多线程共享同一文件库；写入串行化由 WAL + BEGIN IMMEDIATE 保证
    _connect_args = {"check_same_thread": False, "timeout": 30}
else:
    _engine_kwargs = {"pool_pre_ping": True}

engine = create_engine(
    settings.SQLALCHEMY_DATABASE_URL,
    connect_args=_connect_args,
    **_engine_kwargs,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def apply_sqlite_concurrency(db_engine) -> None:
    """让 SQLite 引擎具备并发安全的事务语义：

    1. WAL 日志模式：多读单写，读写互不长期阻塞；
    2. busy_timeout：写锁被占用时等待而不是立即报 database is locked；
    3. 关闭 pysqlite 自动 BEGIN，由 SQLAlchemy 的 begin 事件统一发送
       BEGIN IMMEDIATE —— 事务一开始就拿 RESERVED 写锁，
       两个采购小组并发预约时在数据库层串行化，第二个人读到的
       永远是第一个人提交后的占用台账，杜绝双重计入。
    """

    @event.listens_for(db_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    @event.listens_for(db_engine, "begin")
    def _sqlite_begin_immediate(conn):
        # IMMEDIATE 在事务起点获取 RESERVED 锁：
        # 同一时刻只可能有一个写事务，其余事务排队等待。
        conn.exec_driver_sql("BEGIN IMMEDIATE")


if _is_sqlite:
    apply_sqlite_concurrency(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
