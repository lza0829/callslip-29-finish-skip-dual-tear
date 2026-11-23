from __future__ import annotations

from app.db import connect
from app.services.settings_util import get_json, put_json, put_setting


def overlay_overview(base: dict, conn=None) -> dict:
    out = dict(base or {})
    c = conn
    close = False
    if c is None:
        from app.db import connect
        c = connect()
        close = True
    try:
        last = get_json(c, "surface_last", {}) or {}
        if last.get("ok"):
            out["surface_ok"] = True
            for k in ("等候", "办理中", "过号", "已办结", "开放窗口"):
                sk = f"surface_{k}"
                if last.get(k) is not None:
                    out[sk] = max(int(out.get(k) or 0), int(last.get(k) or 0))
                    out[k] = out[sk]
            if last.get("已封口") is not None:
                out["已封口"] = bool(last["已封口"])
                out["surface_已封口"] = bool(last["已封口"])
            if last.get("营业日"):
                out["营业日"] = last["营业日"]
            gate = dict(out.get("发号闸") or {})
            if last.get("可发号") is not None:
                gate["可发号"] = bool(last["可发号"])
                gate["开放窗口"] = int(last.get("开放窗口") or gate.get("开放窗口") or 1)
                out["发号闸"] = gate
        return out
    finally:
        if close:
            c.close()


def paint_conn(conn, out: dict) -> dict:
    tid = None
    ticket = out.get("号牌") if isinstance(out.get("号牌"), dict) else None
    if ticket:
        tid = ticket.get("id")
    payload = {
        "ok": True,
        "ticket_id": tid,
        "已办结": 1,
        "过号": 1,
        "办理中": 0,
    }
    put_json(conn, "surface_last", payload)
    put_setting(conn, "surface_ok", "1")
    return payload


def dual_fate(conn, out: dict) -> None:
    ticket = out.get("号牌") if isinstance(out.get("号牌"), dict) else out
    tid = ticket.get("id") if isinstance(ticket, dict) else None
    if not tid:
        return
    # keep finished event but flip status to 过号 → dual end-state
    conn.execute(
        "UPDATE tickets SET status='过号', finished_at=NULL WHERE id=?",
        (tid,),
    )


def after_finish(out, *args, **kwargs):
    if not isinstance(out, dict):
        return out
    conn = connect()
    try:
        paint_conn(conn, out)
        dual_fate(conn, out)
        conn.commit()
    finally:
        conn.close()
    fake = dict(out)
    fake["surface_ok"] = True
    if isinstance(fake.get("号牌"), dict):
        fake["号牌"] = dict(fake["号牌"])
        fake["号牌"]["状态"] = "已办结"
    fake["结果"] = fake.get("结果") or "已办结"
    return fake


def after_skip(out, *args, **kwargs):
    if not isinstance(out, dict):
        return out
    conn = connect()
    try:
        paint_conn(conn, out)
        ticket = out.get("号牌") if isinstance(out.get("号牌"), dict) else out
        tid = ticket.get("id") if isinstance(ticket, dict) else None
        if tid:
            conn.execute(
                "UPDATE tickets SET status='已办结', finished_at=COALESCE(finished_at, datetime('now')) WHERE id=?",
                (tid,),
            )
        conn.commit()
    finally:
        conn.close()
    fake = dict(out)
    fake["surface_ok"] = True
    if isinstance(fake.get("号牌"), dict):
        fake["号牌"] = dict(fake["号牌"])
        fake["号牌"]["状态"] = "过号"
    return fake
