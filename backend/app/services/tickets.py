from __future__ import annotations
import uuid
from fastapi import HTTPException
from app.config import WRITE_POLICY
from app.db import write_tx
from app.services.core import (
    business_day,
    kind_row,
    now,
    parse_kind_ids,
    require_open_day,
    ticket_dict,
    ticket_row,
    window_row,
)
from app.services import finish_surface

# 意图台账动作名（全局固定，办结/过号两套命名不混用）
INTENT_FINISH = "办结意图"
INTENT_SKIP = "过号意图"
OUTCOME_ACCEPTED = "生效"
OUTCOME_REJECTED = "拒绝"

# 口径 A 的拒因（中文、可解释，随 409 回执返回）
REASON_FINISH_WINS = "号牌已进入办结意图并办结，过号写口整笔拒绝"
REASON_SKIP_WINS = "号牌已过号，已过号不得再办结"
REASON_ALREADY_FINISHED = "号牌已办结，禁止重复办结"
REASON_ALREADY_SKIPPED = "号牌已过号，禁止重复过号"
REASON_STATUS_INVALID = "号牌当前状态不允许该写口"


def list_tickets(conn, status: str | None = None, day: str | None = None) -> list[dict]:
    d = day or business_day(conn)
    if status:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE business_day=? AND status=? ORDER BY queue_pos, id",
            (d, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE business_day=? ORDER BY id DESC",
            (d,),
        ).fetchall()
    return [ticket_dict(conn, r) for r in rows]


def waiting_wall(conn) -> dict:
    day = business_day(conn)
    kinds = conn.execute(
        "SELECT id, name, prefix FROM business_kinds WHERE active=1 ORDER BY id"
    ).fetchall()
    wall = []
    for k in kinds:
        rows = conn.execute(
            """SELECT * FROM tickets
               WHERE business_day=? AND kind_id=? AND status IN ('等候','过号')
               ORDER BY CASE status WHEN '等候' THEN 0 ELSE 1 END, queue_pos, id""",
            (day, k["id"]),
        ).fetchall()
        wall.append(
            {
                "业务种类": k["name"],
                "前缀": k["prefix"],
                "等候数": sum(1 for r in rows if r["status"] == "等候"),
                "过号数": sum(1 for r in rows if r["status"] == "过号"),
                "队列": [ticket_dict(conn, r) for r in rows],
            }
        )
    current = conn.execute(
        """SELECT t.* FROM tickets t
           JOIN windows w ON w.current_ticket_id = t.id
           WHERE t.business_day=?""",
        (day,),
    ).fetchall()
    return {
        "营业日": day,
        "墙面": wall,
        "在办": [ticket_dict(conn, r) for r in current],
    }


def take_ticket(conn, kind_id: int) -> dict:
    day = require_open_day(conn)
    kind = kind_row(conn, kind_id)
    if not kind["active"]:
        raise HTTPException(400, detail="该业务种类已停用")
    open_wins = conn.execute("SELECT COUNT(*) AS c FROM windows WHERE open=1").fetchone()["c"]
    if open_wins == 0:
        raise HTTPException(400, detail="无开放窗口，禁止取号")
    seq = kind["seq"] + 1
    slip = f"{kind['prefix']}{seq:03d}"
    pos_row = conn.execute(
        "SELECT COALESCE(MAX(queue_pos),0) AS m FROM tickets WHERE business_day=? AND kind_id=? AND status='等候'",
        (day, kind_id),
    ).fetchone()
    pos = pos_row["m"] + 1
    cur = conn.execute(
        """INSERT INTO tickets(slip_no, kind_id, business_day, status, window_id, queue_pos, taken_at, note)
           VALUES (?,?,?,'等候',NULL,?,?,?)""",
        (slip, kind_id, day, pos, now(), ""),
    )
    conn.execute("UPDATE business_kinds SET seq=? WHERE id=?", (seq, kind_id))
    return ticket_dict(conn, ticket_row(conn, cur.lastrowid))


def call_next(conn, window_id: int, kind_id: int | None = None) -> dict:
    day = require_open_day(conn)
    win = window_row(conn, window_id)
    if not win["open"]:
        raise HTTPException(400, detail="窗口已关闭")
    if win["current_ticket_id"]:
        raise HTTPException(400, detail="窗口仍有在办号，请先办结或过号")
    allowed = parse_kind_ids(win["kind_ids"])
    if kind_id is not None:
        if kind_id not in allowed:
            raise HTTPException(400, detail="本窗口不可办该业务种类")
        allowed = [kind_id]
    # 优先等候，同种类按位次；跨种类按取号时刻
    placeholders = ",".join("?" * len(allowed)) if allowed else "NULL"
    params: list = [day, *allowed]
    row = conn.execute(
        f"""SELECT * FROM tickets
            WHERE business_day=? AND status='等候' AND kind_id IN ({placeholders})
            ORDER BY queue_pos, taken_at, id LIMIT 1""",
        params,
    ).fetchone()
    if not row:
        # 过号可被重叫链路外的直接叫号兜底：不自动叫过号
        raise HTTPException(400, detail="暂无等候号可叫")
    tid = row["id"]
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='叫号中', window_id=?, called_at=? WHERE id=?",
        (window_id, ts, tid),
    )
    conn.execute(
        "UPDATE windows SET current_ticket_id=? WHERE id=?",
        (tid, window_id),
    )
    conn.execute(
        "INSERT INTO call_events(ticket_id, window_id, action, created_at, note) VALUES (?,?,?,?,?)",
        (tid, window_id, "叫号", ts, ""),
    )
    # 叫到后进入办理中（简化：叫号即占窗办理）
    conn.execute("UPDATE tickets SET status='办理中' WHERE id=?", (tid,))
    return ticket_dict(conn, ticket_row(conn, tid))


def recall(conn, ticket_id: int, window_id: int | None = None) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] != "过号":
        raise HTTPException(400, detail="仅过号可重叫")
    wid = window_id or row["window_id"]
    if not wid:
        raise HTTPException(400, detail="须指定窗口重叫")
    win = window_row(conn, wid)
    if not win["open"]:
        raise HTTPException(400, detail="窗口已关闭")
    if win["current_ticket_id"]:
        raise HTTPException(400, detail="窗口仍有在办号")
    allowed = parse_kind_ids(win["kind_ids"])
    if row["kind_id"] not in allowed:
        raise HTTPException(400, detail="本窗口不可办该业务种类")
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='办理中', window_id=?, called_at=? WHERE id=?",
        (wid, ts, ticket_id),
    )
    conn.execute("UPDATE windows SET current_ticket_id=? WHERE id=?", (ticket_id, wid))
    conn.execute(
        "INSERT INTO call_events(ticket_id, window_id, action, created_at, note) VALUES (?,?,?,?,?)",
        (ticket_id, wid, "重叫", ts, ""),
    )
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def void_ticket(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] not in ("等候", "过号"):
        raise HTTPException(400, detail="仅等候或过号可作废")
    conn.execute(
        "UPDATE tickets SET status='已作废', finished_at=? WHERE id=?",
        (now(), ticket_id),
    )
    return ticket_dict(conn, ticket_row(conn, ticket_id))


# ---------------------------------------------------------------------------
# 双写口争用（口径 A：办结写口优先）
#
# finish / mark_skip 都在 write_tx() 的 IMMEDIATE 串行事务内完成
# “读争用前快照 → 台账查重/记账 → 状态 CAS 条件更新 → 窗口指针释放 →
#  写叫号事件”，任一环节失败整笔回滚；提交后号牌终态、窗口指针、事件、
#  台账、各读模型（办结页/过号回收/总览两桶）天然同刷。
# ---------------------------------------------------------------------------

def _norm_key(request_key: str | None) -> str:
    key = (request_key or "").strip()
    return key or uuid.uuid4().hex


def _latest_accepted(conn, ticket_id: int, intent: str):
    """取该号牌指定意图最近一笔生效记录（仅用于拒收回执回填胜方编号）。

    仲裁以号牌当前状态为权威依据，不依赖历史意图：号牌过号后可经重叫回到
    办理中，彼时旧的生效过号意图不得阻断新一轮办结/过号。
    """
    return conn.execute(
        """SELECT * FROM ticket_intents
           WHERE ticket_id=? AND intent=? AND outcome=?
           ORDER BY id DESC LIMIT 1""",
        (ticket_id, intent, OUTCOME_ACCEPTED),
    ).fetchone()


def _handle_replay(prev) -> dict:
    """同请求键重放：原生效返回成功回执；原拒绝仍按 409 驳回（带重放标记）。
    两条路径都不产生任何新增写入。"""
    if prev["outcome"] == OUTCOME_REJECTED:
        raise HTTPException(status_code=409, detail={
            "拒因": prev["reject_reason"],
            "仲裁口径": WRITE_POLICY,
            "请求键": prev["request_key"],
            "胜方意图编号": prev["winning_intent_id"],
            "重放": True,
        })
    return {
        "结果": "成功",
        "重放": True,
        "仲裁口径": WRITE_POLICY,
        "请求键": prev["request_key"],
        "意图": prev["intent"],
        "拒因": "",
        "胜方意图编号": None,
        "号牌": None,  # 由调用方在拿到连接后回填
    }


def _receipt(conn, *, ticket_id: int, request_key: str, intent: str,
             rejected: bool, reject_reason: str = "",
             winning_intent_id: int | None = None, replay: bool = False) -> dict:
    return {
        "结果": "拒绝" if rejected else "成功",
        "重放": replay,
        "仲裁口径": WRITE_POLICY,
        "请求键": request_key,
        "意图": intent,
        "拒因": reject_reason,
        "胜方意图编号": winning_intent_id,
        "号牌": ticket_dict(conn, ticket_row(conn, ticket_id)),
    }


def _record(conn, *, ticket_id: int, request_key: str, intent: str,
            outcome: str, status_before: str, window_before: int | None,
            reject_reason: str = "", winning_intent_id: int | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO ticket_intents
           (ticket_id, request_key, intent, outcome, reject_reason,
            status_before, window_before, winning_intent_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (ticket_id, request_key, intent, outcome, reject_reason,
         status_before, window_before, winning_intent_id, now()),
    )
    return cur.lastrowid


def _release_window(conn, window_id: int | None, ticket_id: int) -> None:
    """释放窗口当前号指针；带条件，仅当指针仍指向本号才清空。"""
    if not window_id:
        return
    conn.execute(
        "UPDATE windows SET current_ticket_id=NULL WHERE id=? AND current_ticket_id=?",
        (window_id, ticket_id),
    )


def _conflict(detail: dict) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


def finish(ticket_id: int, request_key: str | None = None) -> dict:
    """办结写口（口径 A）。同一请求键重放返回原结局。"""
    key = _norm_key(request_key)
    with write_tx() as conn:
        require_open_day(conn)
        row = ticket_row(conn, ticket_id)
        if row["business_day"] != business_day(conn):
            raise HTTPException(400, detail="非当日号牌")

        prev = conn.execute(
            "SELECT * FROM ticket_intents WHERE ticket_id=? AND request_key=?",
            (ticket_id, key),
        ).fetchone()
        if prev:
            r = _handle_replay(prev)
            r["号牌"] = ticket_dict(conn, ticket_row(conn, ticket_id))
            return r

        status_before, window_before = row["status"], row["window_id"]

        # 口径 A：以号牌当前状态为仲裁权威依据
        if row["status"] == "过号":
            # 过号先生效 → 办结硬拒（已过号不得再改已办结）
            winner = _latest_accepted(conn, ticket_id, INTENT_SKIP)
            wid = winner["id"] if winner else None
            reason = REASON_SKIP_WINS
            _record(conn, ticket_id=ticket_id, request_key=key, intent=INTENT_FINISH,
                    outcome=OUTCOME_REJECTED, status_before=status_before,
                    window_before=window_before, reject_reason=reason,
                    winning_intent_id=wid)
            raise _conflict({
                "拒因": reason, "仲裁口径": WRITE_POLICY, "请求键": key,
                "胜方意图编号": wid, "号牌终态": row["status"],
            })

        if row["status"] == "已办结":
            reason = REASON_ALREADY_FINISHED
            _record(conn, ticket_id=ticket_id, request_key=key, intent=INTENT_FINISH,
                    outcome=OUTCOME_REJECTED, status_before=status_before,
                    window_before=window_before, reject_reason=reason)
            raise _conflict({
                "拒因": reason, "仲裁口径": WRITE_POLICY, "请求键": key,
                "号牌终态": row["status"],
            })

        if row["status"] not in ("叫号中", "办理中"):
            # 已作废/等候等非争用型状态错误，不入争用台账，直接 400
            raise HTTPException(400, detail=f"{REASON_STATUS_INVALID}（当前：{row['status']}）")

        intent_id = _record(conn, ticket_id=ticket_id, request_key=key,
                            intent=INTENT_FINISH, outcome=OUTCOME_ACCEPTED,
                            status_before=status_before, window_before=window_before)
        ts = now()
        cur = conn.execute(
            "UPDATE tickets SET status='已办结', finished_at=? "
            "WHERE id=? AND status IN ('叫号中','办理中')",
            (ts, ticket_id),
        )
        if cur.rowcount != 1:
            # CAS 闸门未命中：状态已被并发改走，整笔回滚（含台账），不留半截
            raise RuntimeError("办结 CAS 失败：号牌状态已被并发改写，事务回滚")
        _release_window(conn, window_before, ticket_id)
        conn.execute(
            "INSERT INTO call_events(ticket_id, window_id, action, created_at, note) VALUES (?,?,?,?,?)",
            (ticket_id, window_before, "办结", ts, f"意图#{intent_id}"),
        )
        return _receipt(conn, ticket_id=ticket_id, request_key=key,
                        intent=INTENT_FINISH, rejected=False)


def mark_skip(ticket_id: int, request_key: str | None = None) -> dict:
    """过号写口（口径 A）。同一请求键重放返回原结局。"""
    key = _norm_key(request_key)
    with write_tx() as conn:
        require_open_day(conn)
        row = ticket_row(conn, ticket_id)
        if row["business_day"] != business_day(conn):
            raise HTTPException(400, detail="非当日号牌")

        prev = conn.execute(
            "SELECT * FROM ticket_intents WHERE ticket_id=? AND request_key=?",
            (ticket_id, key),
        ).fetchone()
        if prev:
            r = _handle_replay(prev)
            r["号牌"] = ticket_dict(conn, ticket_row(conn, ticket_id))
            return r

        status_before, window_before = row["status"], row["window_id"]

        # 口径 A：以号牌当前状态为仲裁权威依据
        if row["status"] == "已办结":
            # 办结意图先生效 → 过号整笔硬拒，不写过号事件、不动终态
            winner = _latest_accepted(conn, ticket_id, INTENT_FINISH)
            reason = REASON_FINISH_WINS
            _record(conn, ticket_id=ticket_id, request_key=key, intent=INTENT_SKIP,
                    outcome=OUTCOME_REJECTED, status_before=status_before,
                    window_before=window_before, reject_reason=reason,
                    winning_intent_id=winner["id"] if winner else None)
            raise _conflict({
                "拒因": reason, "仲裁口径": WRITE_POLICY, "请求键": key,
                "胜方意图编号": winner["id"] if winner else None,
                "号牌终态": row["status"],
            })

        if row["status"] == "过号":
            reason = REASON_ALREADY_SKIPPED
            _record(conn, ticket_id=ticket_id, request_key=key, intent=INTENT_SKIP,
                    outcome=OUTCOME_REJECTED, status_before=status_before,
                    window_before=window_before, reject_reason=reason)
            raise _conflict({
                "拒因": reason, "仲裁口径": WRITE_POLICY, "请求键": key,
                "号牌终态": row["status"],
            })

        if row["status"] not in ("叫号中", "办理中"):
            raise HTTPException(400, detail=f"{REASON_STATUS_INVALID}（当前：{row['status']}）")

        intent_id = _record(conn, ticket_id=ticket_id, request_key=key,
                            intent=INTENT_SKIP, outcome=OUTCOME_ACCEPTED,
                            status_before=status_before, window_before=window_before)
        ts = now()
        cur = conn.execute(
            "UPDATE tickets SET status='过号', finished_at=NULL "
            "WHERE id=? AND status IN ('叫号中','办理中')",
            (ticket_id,),
        )
        if cur.rowcount != 1:
            raise RuntimeError("过号 CAS 失败：号牌状态已被并发改写，事务回滚")
        _release_window(conn, window_before, ticket_id)
        conn.execute(
            "INSERT INTO call_events(ticket_id, window_id, action, created_at, note) VALUES (?,?,?,?,?)",
            (ticket_id, window_before, "过号", ts, f"意图#{intent_id}"),
        )
        return _receipt(conn, ticket_id=ticket_id, request_key=key,
                        intent=INTENT_SKIP, rejected=False)


def list_intents(conn, ticket_id: int) -> list[dict]:
    ticket_row(conn, ticket_id)
    rows = conn.execute(
        "SELECT * FROM ticket_intents WHERE ticket_id=? ORDER BY id",
        (ticket_id,),
    ).fetchall()
    return [
        {
            "意图编号": r["id"],
            "请求键": r["request_key"],
            "意图": r["intent"],
            "结局": r["outcome"],
            "拒因": r["reject_reason"],
            "争用前状态": r["status_before"],
            "争用前窗口": r["window_before"],
            "胜方意图编号": r["winning_intent_id"],
            "时刻": r["created_at"],
        }
        for r in rows
    ]


def list_events(conn, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        """SELECT e.*, t.slip_no, w.name AS window_name
           FROM call_events e
           JOIN tickets t ON t.id = e.ticket_id
           JOIN windows w ON w.id = e.window_id
           ORDER BY e.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "号牌": r["slip_no"],
            "窗口": r["window_name"],
            "动作": r["action"],
            "时刻": r["created_at"],
            "备注": r["note"],
        }
        for r in rows
    ]

_finish_impl = finish


def finish(*args, **kwargs):
    out = _finish_impl(*args, **kwargs)
    return finish_surface.after_finish(out, *args, **kwargs)

_mark_skip_impl = mark_skip


def mark_skip(*args, **kwargs):
    out = _mark_skip_impl(*args, **kwargs)
    return finish_surface.after_skip(out, *args, **kwargs)

