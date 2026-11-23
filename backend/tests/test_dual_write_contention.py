"""双写口（办结/过号）争用并发测试——口径 A：办结写口优先。

覆盖：
1. 真多线程同时双提交：终态唯一、回执与终态一致、无双重终态、无半截；
2. 两个仲裁分支的确定性顺序（办结先生效 / 过号先生效）；
3. 请求键重放：返回原结局、零新增写入、拒绝重放仍为 409；
4. 终态硬拒（已过号再办结、重复办结/过号）；
5. 事务回滚到争用前快照；
6. 号牌/窗口指针/叫号事件/办结页/过号回收/总览两桶同刷。
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date

# 必须在导入 app.* 之前指定独立测试库
_tmp = tempfile.mkdtemp(prefix="callslip-test-")
os.environ["CALLSLIP_DB"] = os.path.join(_tmp, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import WRITE_POLICY  # noqa: E402
from app.db import db, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import tickets as tic  # noqa: E402


def make_busy_ticket(window_id: int = 1) -> int:
    """造一个当日、办理中、占用指定窗口的号牌，返回其 id。"""
    today = date.today().isoformat()
    with db() as conn:
        kind_id = conn.execute(
            "SELECT id FROM business_kinds ORDER BY id LIMIT 1"
        ).fetchone()["id"]
        pos = conn.execute(
            "SELECT COALESCE(MAX(queue_pos),0)+1 AS p FROM tickets "
            "WHERE business_day=? AND kind_id=?",
            (today, kind_id),
        ).fetchone()["p"]
        slip = f"测{uuid.uuid4().hex[:8]}"
        cur = conn.execute(
            """INSERT INTO tickets(slip_no, kind_id, business_day, status, window_id,
               queue_pos, taken_at, called_at, note)
               VALUES (?,?,?,'办理中',?,?,?,?,'测试号')""",
            (slip, kind_id, today, window_id, pos, f"{today}T09:00:00", f"{today}T09:10:00"),
        )
        tid = cur.lastrowid
        conn.execute(
            "UPDATE windows SET current_ticket_id=? WHERE id=?", (tid, window_id)
        )
        return tid


def snapshot(tid: int) -> dict:
    with db() as conn:
        t = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        w = conn.execute(
            "SELECT current_ticket_id FROM windows WHERE id=?", (t["window_id"],)
        ).fetchone()
        ev = conn.execute(
            "SELECT action FROM call_events WHERE ticket_id=? ORDER BY id", (tid,)
        ).fetchall()
        intents = conn.execute(
            "SELECT intent, outcome, reject_reason, status_before, window_before, "
            "winning_intent_id FROM ticket_intents WHERE ticket_id=? ORDER BY id",
            (tid,),
        ).fetchall()
        day = t["business_day"]
        finished = conn.execute(
            "SELECT COUNT(*) c FROM tickets WHERE business_day=? AND status='已办结'", (day,)
        ).fetchone()["c"]
        skipped = conn.execute(
            "SELECT COUNT(*) c FROM tickets WHERE business_day=? AND status='过号'", (day,)
        ).fetchone()["c"]
        return {
            "status": t["status"],
            "window_ptr": w["current_ticket_id"],
            "events": [r["action"] for r in ev],
            "intents": [dict(r) for r in intents],
            "bucket_finished": finished,
            "bucket_skipped": skipped,
        }


def run_concurrent(tid: int):
    """两个写口各带独立请求键，真线程同时提交。"""
    fkey, skey = uuid.uuid4().hex, uuid.uuid4().hex
    results = {}

    def do_finish():
        try:
            results["finish"] = ("ok", tic.finish(tid, fkey))
        except Exception as e:  # noqa: BLE001
            results["finish"] = ("err", e)

    def do_skip():
        try:
            results["skip"] = ("ok", tic.mark_skip(tid, skey))
        except Exception as e:  # noqa: BLE001
            results["skip"] = ("err", e)

    t1 = threading.Thread(target=do_finish)
    t2 = threading.Thread(target=do_skip)
    t1.start(); t2.start()
    t1.join(); t2.join()
    return results, fkey, skey


class DualWriteContentionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.client = TestClient(app)

    def assertSingleTerminal(self, snap: dict):
        # 终态唯一：要么已办结，要么过号，绝不可能同时
        self.assertIn(snap["status"], ("已办结", "过号"))
        # 窗口指针必须随生效写口释放
        self.assertIsNone(snap["window_ptr"])
        # 叫号事件里每个号牌至多一个终态事件（办结 XOR 过号）
        terminal = [a for a in snap["events"] if a in ("办结", "过号")]
        self.assertEqual(len(terminal), 1)
        # 恰有一个生效意图、一个拒绝意图（双写口都到达）
        accepted = [i for i in snap["intents"] if i["outcome"] == "生效"]
        rejected = [i for i in snap["intents"] if i["outcome"] == "拒绝"]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 1)
        # 争用前快照可解释：生效者在办理中占窗时落子；
        # 拒绝者因 IMMEDIATE 串行，拿锁后读到的是胜方已提交的终态
        self.assertEqual(accepted[0]["status_before"], "办理中")
        self.assertIn(rejected[0]["status_before"], ("已办结", "过号"))
        # 拒绝台账指向胜方意图
        self.assertTrue(rejected[0]["winning_intent_id"])

    def test_01_concurrent_race_single_terminal_many_iterations(self):
        outcomes = {"已办结": 0, "过号": 0}
        for _ in range(30):
            tid = make_busy_ticket()
            results, _, _ = run_concurrent(tid)
            snap = snapshot(tid)
            outcomes[snap["status"]] += 1
            self.assertSingleTerminal(snap)

            f_kind, f_val = results["finish"]
            s_kind, s_val = results["skip"]
            if snap["status"] == "已办结":
                # 办结成功回执；过号 409 硬拒，拒因为办结优先
                self.assertEqual(f_kind, "ok")
                self.assertEqual(f_val["结果"], "成功")
                self.assertEqual(f_val["仲裁口径"], WRITE_POLICY)
                self.assertEqual(s_kind, "err")
                self.assertEqual(s_val.status_code, 409)
                self.assertIn("办结", s_val.detail["拒因"])
                # 过号事件不得写入
                self.assertNotIn("过号", snap["events"])
                self.assertEqual(snap["events"].count("办结"), 1)
                # 总览办结桶 +1、过号桶不含本号
                self.assertEqual(
                    [i for i in snap["intents"] if i["outcome"] == "拒绝"][0]["intent"],
                    "过号意图",
                )
            else:
                # 过号先生效：过号成功；办结 409 硬拒（已过号不得再办结）
                self.assertEqual(s_kind, "ok")
                self.assertEqual(s_val["结果"], "成功")
                self.assertEqual(f_kind, "err")
                self.assertEqual(f_val.status_code, 409)
                self.assertIn("已过号", f_val.detail["拒因"])
                self.assertNotIn("办结", snap["events"])
                self.assertEqual(snap["events"].count("过号"), 1)
        # 两个分支在 30 次真实争用中都应被调度到（IMMEDIATE 串行，二者皆可能先拿锁）
        self.assertGreater(outcomes["已办结"], 0)
        self.assertGreater(outcomes["过号"], 0)

    def test_02_deterministic_finish_first_then_skip_rejected(self):
        tid = make_busy_ticket()
        r = tic.finish(tid, "key-finish-1")
        self.assertEqual(r["结果"], "成功")
        self.assertEqual(r["号牌"]["状态"], "已办结")
        # 随后过号整笔拒绝
        with self.assertRaises(Exception) as ctx:
            tic.mark_skip(tid, "key-skip-1")
        self.assertEqual(ctx.exception.status_code, 409)
        snap = snapshot(tid)
        self.assertEqual(snap["status"], "已办结")
        self.assertIsNone(snap["window_ptr"])
        self.assertEqual(snap["events"], ["办结"])  # 无过号事件
        rej = [i for i in snap["intents"] if i["outcome"] == "拒绝"][0]
        self.assertEqual(rej["intent"], "过号意图")
        self.assertTrue(rej["winning_intent_id"])

    def test_03_deterministic_skip_first_then_finish_rejected(self):
        tid = make_busy_ticket()
        r = tic.mark_skip(tid, "key-skip-2")
        self.assertEqual(r["结果"], "成功")
        self.assertEqual(r["号牌"]["状态"], "过号")
        with self.assertRaises(Exception) as ctx:
            tic.finish(tid, "key-finish-2")
        self.assertEqual(ctx.exception.status_code, 409)
        snap = snapshot(tid)
        self.assertEqual(snap["status"], "过号")
        self.assertIsNone(snap["window_ptr"])
        self.assertEqual(snap["events"], ["过号"])  # 无办结事件

    def test_04_replay_returns_original_outcome_no_new_writes(self):
        # 生效写口重放：成功且不新增台账/事件
        tid = make_busy_ticket()
        first = tic.finish(tid, "dup-key")
        again = tic.finish(tid, "dup-key")
        self.assertTrue(again["重放"])
        self.assertEqual(again["结果"], "成功")
        self.assertEqual(again["号牌"]["状态"], "已办结")
        snap = snapshot(tid)
        self.assertEqual(len(snap["intents"]), 1)  # 只有一条意图，重放未新增
        self.assertEqual(snap["events"].count("办结"), 1)

        # 拒绝写口同键重放：首次与重放都是 409，且台账只记一次
        with self.assertRaises(Exception):
            tic.mark_skip(tid, "dup-skip")  # 首次拒绝
        with self.assertRaises(Exception) as ctx:
            tic.mark_skip(tid, "dup-skip")  # 同键重放
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertTrue(ctx.exception.detail["重放"])
        snap2 = snapshot(tid)
        skip_intents = [i for i in snap2["intents"] if i["intent"] == "过号意图"]
        self.assertEqual(len(skip_intents), 1)  # 重放零新增

    def test_05_hard_reject_skipped_then_finish_and_duplicates(self):
        tid = make_busy_ticket()
        tic.mark_skip(tid, "s")
        # 已过号再办结：硬拒 409
        with self.assertRaises(Exception) as ctx:
            tic.finish(tid, "f")
        self.assertEqual(ctx.exception.status_code, 409)
        # 重复过号：硬拒 409
        with self.assertRaises(Exception) as ctx2:
            tic.mark_skip(tid, "s2")
        self.assertEqual(ctx2.exception.status_code, 409)

        tid2 = make_busy_ticket()
        tic.finish(tid2, "f2")
        with self.assertRaises(Exception) as ctx3:
            tic.finish(tid2, "f2b")
        self.assertEqual(ctx3.exception.status_code, 409)

    def test_06_rollback_on_failure_leaves_pre_contention_snapshot(self):
        tid = make_busy_ticket()
        before = snapshot(tid)
        # 对等候号牌触发 400（非争用型拒绝）：不得写任何台账、状态不变
        with db() as conn:
            wait_id = conn.execute(
                "SELECT id FROM tickets WHERE status='等候' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
        with self.assertRaises(Exception) as ctx:
            tic.finish(wait_id, "bad-finish")
        self.assertEqual(ctx.exception.status_code, 400)
        with db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM ticket_intents WHERE ticket_id=?", (wait_id,)
            ).fetchone()["c"]
            st = conn.execute(
                "SELECT status FROM tickets WHERE id=?", (wait_id,)
            ).fetchone()["status"]
        self.assertEqual(n, 0)
        self.assertEqual(st, "等候")
        # 原办理中号牌快照完全未受影响
        self.assertEqual(snapshot(tid), before)

    def test_07_read_models_refresh_together(self):
        tid = make_busy_ticket()
        tic.finish(tid, "rm")
        with db() as conn:
            # 办结页（按状态查已办结）含本号
            finished = tic.list_tickets(conn, status="已办结")
            # 过号回收列表不含本号
            skipped = tic.list_tickets(conn, status="过号")
            ov = self.client.get("/api/总览").json()
            wall = tic.waiting_wall(conn)
        self.assertTrue(any(t["id"] == tid for t in finished))
        self.assertFalse(any(t["id"] == tid for t in skipped))
        self.assertEqual(ov["已办结"], snapshot(tid)["bucket_finished"])
        self.assertEqual(ov["过号"], snapshot(tid)["bucket_skipped"])
        # 等候墙“在办”不再含已释放窗口上的本号
        self.assertFalse(any(t["id"] == tid for t in wall["在办"]))

        # 过号分支：本号进过号回收、不进办结页
        tid2 = make_busy_ticket()
        tic.mark_skip(tid2, "rm2")
        with db() as conn:
            skipped2 = tic.list_tickets(conn, status="过号")
            finished2 = tic.list_tickets(conn, status="已办结")
        self.assertTrue(any(t["id"] == tid2 for t in skipped2))
        self.assertFalse(any(t["id"] == tid2 for t in finished2))

    def test_08_http_layer_409_and_replay_marker(self):
        tid = make_busy_ticket()
        r1 = self.client.post(f"/api/号牌/{tid}/办结", json={"请求键": "http-f"})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json()["仲裁口径"], WRITE_POLICY)
        r2 = self.client.post(f"/api/号牌/{tid}/过号", json={"请求键": "http-s"})
        self.assertEqual(r2.status_code, 409)
        self.assertIn("拒因", r2.json()["detail"])
        # 同键重放过号拒绝
        r3 = self.client.post(f"/api/号牌/{tid}/过号", json={"请求键": "http-s"})
        self.assertEqual(r3.status_code, 409)
        self.assertTrue(r3.json()["detail"]["重放"])
        # 意图台账可审计
        led = self.client.get(f"/api/号牌/{tid}/意图台账").json()
        self.assertEqual({x["结局"] for x in led}, {"生效", "拒绝"})


    def test_09_recall_cycle_does_not_let_stale_intent_block_new_write(self):
        # 过号 → 重叫回办理中：旧的生效过号意图不得阻断新一轮写口
        tid = make_busy_ticket()
        tic.mark_skip(tid, "cycle-skip")
        self.assertEqual(snapshot(tid)["status"], "过号")
        with db() as conn:
            tic.recall(conn, tid, 1)  # 回一号窗重叫
        self.assertEqual(snapshot(tid)["status"], "办理中")
        # 此时办结必须成功（历史过号意图不构成阻断）
        r = tic.finish(tid, "cycle-finish")
        self.assertEqual(r["结果"], "成功")
        snap = snapshot(tid)
        self.assertEqual(snap["status"], "已办结")
        # 台账完整：过号(生效) → 办结(生效)，无错误拒绝
        outcomes = [(i["intent"], i["outcome"]) for i in snap["intents"]]
        self.assertEqual(outcomes, [("过号意图", "生效"), ("办结意图", "生效")])

        # 另一轮：过号 → 重叫 → 再次过号，也应合法
        tid2 = make_busy_ticket()
        tic.mark_skip(tid2, "c2-skip1")
        with db() as conn:
            tic.recall(conn, tid2, 1)
        r2 = tic.mark_skip(tid2, "c2-skip2")
        self.assertEqual(r2["结果"], "成功")
        self.assertEqual(snapshot(tid2)["status"], "过号")


if __name__ == "__main__":
    unittest.main(verbosity=2)
