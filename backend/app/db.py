from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from fastapi import HTTPException
from app.config import BUSY_TIMEOUT_MS, DB_PATH

SCHEMA = '''
CREATE TABLE IF NOT EXISTS business_kinds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL UNIQUE,
  seq INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  kind_ids TEXT NOT NULL,
  open INTEGER NOT NULL DEFAULT 1,
  current_ticket_id INTEGER
);
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no TEXT NOT NULL UNIQUE,
  kind_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL,
  window_id INTEGER,
  queue_pos INTEGER NOT NULL,
  taken_at TEXT NOT NULL,
  called_at TEXT,
  finished_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(kind_id) REFERENCES business_kinds(id),
  FOREIGN KEY(window_id) REFERENCES windows(id)
);
CREATE TABLE IF NOT EXISTS call_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL,
  window_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(ticket_id) REFERENCES tickets(id),
  FOREIGN KEY(window_id) REFERENCES windows(id)
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS day_seals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL UNIQUE,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
-- 双写口争用台账：办结/过号每一笔写口意图都在此记账，
-- 结局（生效/拒绝）、拒因、争用前快照、胜方意图均可重放审计。
CREATE TABLE IF NOT EXISTS ticket_intents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL,
  request_key TEXT NOT NULL,
  intent TEXT NOT NULL,                 -- 办结意图 / 过号意图
  outcome TEXT NOT NULL,                -- 生效 / 拒绝
  reject_reason TEXT NOT NULL DEFAULT '',
  status_before TEXT NOT NULL,          -- 争用前可解释快照：号牌状态
  window_before INTEGER,                -- 争用前可解释快照：窗口当前号指向
  winning_intent_id INTEGER,            -- 拒绝时指向生效的胜方意图
  created_at TEXT NOT NULL,
  UNIQUE(ticket_id, request_key),
  FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);
'''


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def db():
    """只读/普通写路径：自动提交，异常整体回滚。"""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_tx():
    """写口专用串行事务。

    BEGIN IMMEDIATE 一进入即取数据库级写锁，办结写口与过号写口因此被
    全局串行化：后到者在锁外等待，拿到锁后读到的必是已提交终态，再以
    条件 UPDATE（CAS）做最后一道闸门，从根上消除一结一过双终态。

    语义：
    - 业务拒绝（HTTPException，含 409 争用拒绝）要保留“拒绝意图”记账，
      因此在上下文出口显式提交；
    - 其余任何异常（含 CAS 失败、SQLITE_BUSY）一律回滚到争用前快照，
      不留半截写入。
    """
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except HTTPException:
        # 拒绝意图已在同一事务内落台账，提交该记账，不回滚业务行之外的内容
        conn.commit()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        # 存量假账收口（幂等）：surface 旁路已拆除，其写入的粉饰快照键
        # （surface_last / surface_ok）一律清除；总览双桶只由 tickets 实表聚合。
        conn.execute("DELETE FROM settings WHERE key LIKE 'surface_%'")
