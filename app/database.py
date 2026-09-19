"""SQLite 持久化层。

四张表：
- chains：链条头信息与链尾缓存（tail_hash / current_holder 是事件表的可信投影）
- events：链上的全部事件，序号 0 为创世记录（文件摘要 + 首位持有人），
  序号 >=1 为移交记录。
- offers：待接收移交邀约。只保存一次性确认码的 SHA-256 摘要（code_digest），
  不保存明文；tail_hash 记录邀约发起时锁定的链尾，确认时必须仍然匹配。
  witness_threshold 为 NULL 表示无见证邀约（兼容旧数据），否则为见证通过阈值。
- offer_witnesses：见证确认模式下的见证人登记与表态。只保存一次性令牌的
  SHA-256 摘要（token_digest），不保存明文；decision 为 NULL 表示未表态，
  同一见证人（offer_id, witness）只有一行，表态只能写入一次。

防分叉保障：
1. 所有写操作在进程内串行锁 + SQLite `BEGIN IMMEDIATE` 中完成，
   追加前校验链尾哈希，校验与插入在同一事务内原子完成；
2. (chain_id, seq) 唯一、(chain_id, nonce) 唯一，由数据库兜底，
   任何竞争写入都不可能产生第二条分叉链或重放随机数。

建表语句全部为 CREATE ... IF NOT EXISTS，init_db() 可重复执行，
对已有数据库文件只做增量补表，不触碰存量数据。
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from . import config

# 进程内写锁：保证“读链尾 -> 校验 -> 追加提交”整体串行
_write_lock = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chains (
    chain_id        TEXT PRIMARY KEY,
    file_hash       TEXT NOT NULL,
    first_holder    TEXT NOT NULL,
    tail_hash       TEXT NOT NULL,
    current_holder  TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id      TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    prev_hash     TEXT NOT NULL DEFAULT '',
    giver         TEXT NOT NULL DEFAULT '',
    receiver      TEXT NOT NULL DEFAULT '',
    nonce         TEXT NOT NULL DEFAULT '',
    event_hash    TEXT NOT NULL,
    canonical     TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE(chain_id, seq),
    UNIQUE(chain_id, nonce),
    FOREIGN KEY(chain_id) REFERENCES chains(chain_id)
);

CREATE INDEX IF NOT EXISTS idx_events_chain_seq
    ON events(chain_id, seq);

CREATE TABLE IF NOT EXISTS offers (
    offer_id      TEXT PRIMARY KEY,
    chain_id      TEXT NOT NULL,
    tail_hash     TEXT NOT NULL,
    receiver      TEXT NOT NULL,
    code_digest   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    expires_at    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    accepted_at   TEXT,
    accepted_seq  INTEGER,
    CHECK(status IN ('pending', 'accepted')),
    FOREIGN KEY(chain_id) REFERENCES chains(chain_id)
);

CREATE INDEX IF NOT EXISTS idx_offers_chain
    ON offers(chain_id);

CREATE TABLE IF NOT EXISTS offer_witnesses (
    offer_id      TEXT NOT NULL,
    witness       TEXT NOT NULL,
    token_digest  TEXT NOT NULL,
    decision      TEXT,
    decided_at    TEXT,
    PRIMARY KEY (offer_id, witness),
    UNIQUE (offer_id, token_digest),
    CHECK(decision IS NULL OR decision IN ('approve', 'reject')),
    FOREIGN KEY(offer_id) REFERENCES offers(offer_id)
);
"""

# 存量库的增量列迁移：CREATE TABLE IF NOT EXISTS 管不到已存在的 offers 表。
# (表名, 列名, ADD COLUMN 子句)
_MIGRATIONS = (
    ("offers", "witness_threshold", "witness_threshold INTEGER"),
)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.DB_PATH,
        timeout=10.0,
        isolation_level=None,  # 手动事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def _get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """对存量数据库补充新列（幂等：已存在则跳过）。"""
    for table, column, ddl in _MIGRATIONS:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db() -> None:
    """初始化（或校验）数据库结构。可重复执行。"""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _get_conn() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """串行化的写事务：BEGIN IMMEDIATE 立即取得写锁，提交前不会被插队。"""
    with _write_lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


@contextmanager
def read_tx() -> Iterator[sqlite3.Connection]:
    with _get_conn() as conn:
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
