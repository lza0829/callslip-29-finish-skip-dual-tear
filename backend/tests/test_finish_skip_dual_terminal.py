"""双终态撕裂夹具——口径「单一终态∪窗指针∪总览可重放为准」的收口验证。

全局口径（唯一）：办结灯必须能由 号牌唯一终态 ∪ 窗指针收口 ∪ 总览已办结桶
同命运重放；出现 办结∪过号 双终态则整笔回滚；禁止只改办结回执文案。

四个夹具复现历史撕裂现场，并断言收口后全链路同刷：
1. 办结绿且过号事件在：办结回执绿灯后，过号写口必须整笔硬拒，
   叫号流水不得出现过号事件、过号回收不得挂本号、回执与状态接口同词；
2. 同号双终态：并发双写后同号在办结页源与过号页源必须 XOR，
   状态接口、窗指针、终态事件、审计台账同刷；
3. 窗指针空而总览双计：窗指针随生效写口释放后，总览只能单桶 +1，
   另一桶 +0，且总览不得携带任何 surface_* 粉饰键；
4. 办结过号半截：写口中段故障整笔回滚到争用前快照（状态/窗指针/
   事件/台账/双桶零残留）；半截并发后各读模型必须同刷，
   缺口未清不得宣称已收口。
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from unittest import mock

# 必须在导入 app.* 之前指定独立测试库
_tmp = tempfile.mkdtemp(prefix="callslip-dual-terminal-")
os.environ["CALLSLIP_DB"] = os.path.join(_tmp, "dual.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import WRITE_POLICY  # noqa: E402
from app.db import db, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import tickets as tic  # noqa: E402

TERMINAL_OF_ACTION = {"办结": "已办结", "过号": "过号"}


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
        slip = f"夹{uuid.uuid4().hex[:8]}"
        cur = conn.execute(
            """INSERT INTO tickets(slip_no, kind_id, business_day, status, window_id,
               queue_pos, taken_at, called_at, note)
               VALUES (?,?,?,'办理中',?,?,?,?,'双终态夹具')""",
            (slip, kind_id, today, window_id, pos, f"{today}T09:00:00", f"{today}T09:10:00"),
        )
        tid = cur.lastrowid
        conn.execute(
            "UPDATE windows SET current_ticket_id=? WHERE id=?", (tid, window_id)
        )
        return tid


def db_snapshot(tid: int) -> dict:
    """争用前/后快照：终态、窗指针、事件、台账、当日双桶（实表直查）。"""
    with db() as conn:
        t = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        w = conn.execute(
            "SELECT current_ticket_id FROM windows WHERE id=?", (t["window_id"],)
        ).fetchone()
        ev = conn.execute(
            "SELECT action FROM call_events WHERE ticket_id=? ORDER BY id", (tid,)
        ).fetchall()
        intents = conn.execute(
            "SELECT intent, outcome FROM ticket_intents WHERE ticket_id=? ORDER BY id",
            (tid,),
        ).fetchall()
        day = t["business_day"]
        buckets = {
            s: conn.execute(
                "SELECT COUNT(*) c FROM tickets WHERE business_day=? AND status=?",
                (day, s),
            ).fetchone()["c"]
            for s in ("已办结", "过号")
        }
        return {
            "status": t["status"],
            "window_ptr": w["current_ticket_id"],
            "events": [r["action"] for r in ev],
            "intents": [(r["intent"], r["outcome"]) for r in intents],
            "buckets": buckets,
        }


class DualTerminalFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.client = TestClient(app)

    # ------------------------------------------------------------------
    # 同刷断言：办结/过号写口、号牌终态、窗指针、办理页源、过号页源、
    # 办结页源、总览双桶、叫号流水、审计台账必须讲同一个终态故事。
    # ------------------------------------------------------------------
    def assertConverged(self, tid: int, terminal: str):
        other = "过号" if terminal == "已办结" else "已办结"
        c = self.client

        # 状态接口：本号终态唯一
        all_tickets = {t["id"]: t for t in c.get("/api/号牌").json()}
        self.assertEqual(all_tickets[tid]["状态"], terminal)

        # 办理页源：在办列表不得再挂本号
        busy = c.get("/api/号牌?状态=办理中").json() + c.get("/api/号牌?状态=叫号中").json()
        self.assertFalse(any(t["id"] == tid for t in busy))

        # 办结页源 XOR 过号页源：同号只准出现在终态对应的一侧
        in_terminal = any(t["id"] == tid for t in c.get(f"/api/号牌?状态={terminal}").json())
        in_other = any(t["id"] == tid for t in c.get(f"/api/号牌?状态={other}").json())
        self.assertTrue(in_terminal)
        self.assertFalse(in_other)

        # 窗指针：所有窗口当前号均不得指向本号
        wins = c.get("/api/窗口").json()
        self.assertFalse(any((w["当前号"] or {}).get("id") == tid for w in wins))

        # 叫号流水：本号终态事件恰一个，且与终态同词
        slip = all_tickets[tid]["号牌"]
        terminal_events = [
            e["动作"] for e in c.get("/api/叫号流水").json()
            if e["号牌"] == slip and e["动作"] in ("办结", "过号")
        ]
        self.assertEqual(len(terminal_events), 1)
        self.assertEqual(TERMINAL_OF_ACTION[terminal_events[0]], terminal)

        # 总览双桶：与实表直查同数（无覆盖层、无双计），且无 surface_* 粉饰键
        ov = c.get("/api/总览").json()
        self.assertFalse(any(k.startswith("surface_") for k in ov))
        snap = db_snapshot(tid)
        self.assertEqual(ov["已办结"], snap["buckets"]["已办结"])
        self.assertEqual(ov["过号"], snap["buckets"]["过号"])
        self.assertEqual(snap["status"], terminal)
        self.assertIsNone(snap["window_ptr"])

        # 审计台账：与终态同词的一生效；若双写口都到达，另有一拒绝
        ledger = c.get(f"/api/号牌/{tid}/意图台账").json()
        accepted = [x for x in ledger if x["结局"] == "生效"]
        self.assertEqual(len(accepted), 1)
        want_intent = "办结意图" if terminal == "已办结" else "过号意图"
        self.assertEqual(accepted[0]["意图"], want_intent)
        return ledger

    # ------------------------------------------------------------------
    # 夹具一：办结绿且过号事件在
    # ------------------------------------------------------------------
    def test_01_finish_green_then_skip_event_must_not_coexist(self):
        tid = make_busy_ticket()
        before = db_snapshot(tid)["buckets"]

        # 办结写口：回执绿灯，且回执终态必须与状态接口同词（不许只改文案）
        r = self.client.post(f"/api/号牌/{tid}/办结", json={"请求键": "fix1-f"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["结果"], "成功")
        self.assertEqual(body["仲裁口径"], WRITE_POLICY)
        self.assertEqual(body["号牌"]["状态"], "已办结")
        self.assertIsNotNone(body["号牌"]["办结时刻"])

        # 过号写口随后到达：整笔硬拒 409，不写过号事件、不进过号回收
        r2 = self.client.post(f"/api/号牌/{tid}/过号", json={"请求键": "fix1-s"})
        self.assertEqual(r2.status_code, 409)
        self.assertIn("办结", r2.json()["detail"]["拒因"])

        # 全链路同刷：办结灯可由 终态∪窗指针∪总览已办结桶 重放
        ledger = self.assertConverged(tid, "已办结")
        self.assertEqual({x["结局"] for x in ledger}, {"生效", "拒绝"})

        # 过号事件不在、过号桶零增长、办结桶恰 +1
        after = db_snapshot(tid)
        self.assertNotIn("过号", after["events"])
        self.assertEqual(after["buckets"]["过号"], before["过号"])
        self.assertEqual(after["buckets"]["已办结"], before["已办结"] + 1)

    # ------------------------------------------------------------------
    # 夹具二：同号双终态（并发争用后只准单一终态）
    # ------------------------------------------------------------------
    def test_02_concurrent_dual_write_never_dual_terminal(self):
        for _ in range(10):
            tid = make_busy_ticket()
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

            t1, t2 = threading.Thread(target=do_finish), threading.Thread(target=do_skip)
            t1.start(); t2.start(); t1.join(); t2.join()

            # 恰一胜一负；胜方回执终态 = 状态接口终态
            kinds = sorted(k for k, _ in (results["finish"], results["skip"]))
            self.assertEqual(kinds, ["err", "ok"])
            terminal = db_snapshot(tid)["status"]
            self.assertIn(terminal, ("已办结", "过号"))
            winner = results["finish"] if results["finish"][0] == "ok" else results["skip"]
            self.assertEqual(winner[1]["号牌"]["状态"], terminal)
            loser = results["finish"] if results["finish"][0] == "err" else results["skip"]
            self.assertEqual(loser[1].status_code, 409)

            # 同号不得双挂：办结页源 XOR 过号页源，全链路同刷
            self.assertConverged(tid, terminal)

    # ------------------------------------------------------------------
    # 夹具三：窗指针空而总览双计
    # ------------------------------------------------------------------
    def test_03_window_released_overview_single_bucket_only(self):
        # 办结分支：窗指针空，已办结桶恰 +1、过号桶 +0
        tid = make_busy_ticket()
        ov0 = self.client.get("/api/总览").json()
        r = self.client.post(f"/api/号牌/{tid}/办结", json={"请求键": "fix3-f"})
        self.assertEqual(r.status_code, 200)
        wins = self.client.get("/api/窗口").json()
        self.assertFalse(any((w["当前号"] or {}).get("id") == tid for w in wins))
        ov1 = self.client.get("/api/总览").json()
        self.assertEqual(ov1["已办结"], ov0["已办结"] + 1)
        self.assertEqual(ov1["过号"], ov0["过号"])

        # 过号分支：窗指针空，过号桶恰 +1、已办结桶 +0
        tid2 = make_busy_ticket()
        r = self.client.post(f"/api/号牌/{tid2}/过号", json={"请求键": "fix3-s"})
        self.assertEqual(r.status_code, 200)
        wins = self.client.get("/api/窗口").json()
        self.assertFalse(any((w["当前号"] or {}).get("id") == tid2 for w in wins))
        ov2 = self.client.get("/api/总览").json()
        self.assertEqual(ov2["过号"], ov1["过号"] + 1)
        self.assertEqual(ov2["已办结"], ov1["已办结"])

        # 收口守卫：总览不得携带 surface_* 粉饰键，settings 表不得有残留
        self.assertFalse(any(k.startswith("surface_") for k in ov2))
        with db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM settings WHERE key LIKE 'surface_%'"
            ).fetchone()["c"]
        self.assertEqual(n, 0)

    # ------------------------------------------------------------------
    # 夹具四：办结过号半截——中段故障整笔回滚，并发半截后同刷
    # ------------------------------------------------------------------
    def test_04a_mid_write_failure_rolls_back_to_pre_contention_snapshot(self):
        tid = make_busy_ticket()
        before = db_snapshot(tid)
        ov0 = self.client.get("/api/总览").json()

        # 在 CAS 落终态之后、事件写入之前注入故障：整笔必须回滚
        with mock.patch.object(
            tic, "_release_window", side_effect=RuntimeError("注入半截故障")
        ):
            with self.assertRaises(RuntimeError):
                tic.finish(tid, "fix4-fault")

        # 争用前快照零残留：状态/窗指针/事件/台账/双桶全部原样
        after = db_snapshot(tid)
        self.assertEqual(after, before)
        ov1 = self.client.get("/api/总览").json()
        self.assertEqual(ov1["已办结"], ov0["已办结"])
        self.assertEqual(ov1["过号"], ov0["过号"])

        # 故障解除后同一请求键可重试成功（回滚未留半截台账挡路）
        r = tic.finish(tid, "fix4-fault")
        self.assertEqual(r["结果"], "成功")
        self.assertConverged(tid, "已办结")

    def test_04b_half_state_concurrency_converges_all_read_models(self):
        # 并发半截压测：多回合双写口对撞，每回合后各读模型必须同刷
        for _ in range(10):
            tid = make_busy_ticket()
            with ThreadPoolExecutor(max_workers=2) as ex:
                futs = [
                    ex.submit(tic.finish, tid, uuid.uuid4().hex),
                    ex.submit(tic.mark_skip, tid, uuid.uuid4().hex),
                ]
                outs = []
                for f in futs:
                    try:
                        outs.append(("ok", f.result()))
                    except Exception as e:  # noqa: BLE001
                        outs.append(("err", e))
            self.assertEqual(sorted(k for k, _ in outs), ["err", "ok"])
            terminal = db_snapshot(tid)["status"]
            # 缺口未清不得宣称已收口：同刷断言全部通过才算收敛
            ledger = self.assertConverged(tid, terminal)
            self.assertEqual({x["结局"] for x in ledger}, {"生效", "拒绝"})

    # ------------------------------------------------------------------
    # 全链路同刷 sweep：过号胜出的对称路径
    # ------------------------------------------------------------------
    def test_05_skip_wins_full_surface_sweep(self):
        tid = make_busy_ticket()
        r = self.client.post(f"/api/号牌/{tid}/过号", json={"请求键": "fix5-s"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["号牌"]["状态"], "过号")

        # 办结写口随后到达：硬拒 409（已过号不得再办结），办结事件不得写入
        r2 = self.client.post(f"/api/号牌/{tid}/办结", json={"请求键": "fix5-f"})
        self.assertEqual(r2.status_code, 409)
        self.assertIn("已过号", r2.json()["detail"]["拒因"])

        ledger = self.assertConverged(tid, "过号")
        self.assertEqual({x["结局"] for x in ledger}, {"生效", "拒绝"})
        self.assertNotIn("办结", db_snapshot(tid)["events"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
