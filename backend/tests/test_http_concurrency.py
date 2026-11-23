"""端到端：真实 uvicorn 服务上的双写口 HTTP 并发争用。

两个独立 HTTP 连接（独立 sqlite 连接）在同一刻 POST 办结/过号，验证跨连接写锁下：
- 恰好一个 200、一个 409；终态唯一且与回执一致；
- 窗口指针释放、终态事件唯一、读模型同刷；
- 多个回合中两种仲裁结果都会出现。
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
import uuid

import httpx
import uvicorn

_tmp = tempfile.mkdtemp(prefix="callslip-http-")
os.environ["CALLSLIP_DB"] = os.path.join(_tmp, "http.db")

from app.main import app  # noqa: E402
from app.config import WRITE_POLICY  # noqa: E402

BASE = "http://127.0.0.1:11499"


class HttpConcurrencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config = uvicorn.Config(app, host="127.0.0.1", port=11499, log_level="warning")
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(100):
            if cls.server.started:
                break
            time.sleep(0.05)
        cls.admin = httpx.Client(base_url=BASE, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        cls.admin.close()

    def _make_busy(self) -> int:
        # 二号窗可办个人储蓄（种类 1）；先补一个号确保有等候，再叫号占窗
        kind_id = self.admin.get("/api/业务种类").json()[0]["id"]
        self.admin.post("/api/取号", json={"种类编号": kind_id})
        called = self.admin.post(
            "/api/叫号", json={"窗口编号": 2, "种类编号": kind_id}
        ).json()
        self.assertEqual(called["状态"], "办理中")
        return called["id"]

    def _slip(self, tid: int) -> str:
        return next(t["号牌"] for t in self.admin.get("/api/号牌").json() if t["id"] == tid)

    def test_concurrent_http_dual_write(self):
        summary = {"办结胜": 0, "过号胜": 0}
        for _ in range(15):
            tid = self._make_busy()
            fkey, skey = uuid.uuid4().hex, uuid.uuid4().hex
            results = {}
            barrier = threading.Barrier(2)

            def worker(name, path, key):
                # 每个线程独立 client → 独立连接，真正打到跨连接写锁
                with httpx.Client(base_url=BASE, timeout=30) as c:
                    barrier.wait()
                    results[name] = c.post(
                        f"/api/号牌/{tid}/{path}", json={"请求键": key}
                    )

            t1 = threading.Thread(target=worker, args=("finish", "办结", fkey))
            t2 = threading.Thread(target=worker, args=("skip", "过号", skey))
            t1.start(); t2.start()
            t1.join(); t2.join()

            rf, rs = results["finish"], results["skip"]
            # 恰一胜一负
            self.assertEqual(sorted([rf.status_code, rs.status_code]), [200, 409])

            # 终态唯一：办结页 XOR 过号回收
            in_finished = any(t["id"] == tid for t in self.admin.get("/api/号牌?状态=已办结").json())
            in_skipped = any(t["id"] == tid for t in self.admin.get("/api/号牌?状态=过号").json())
            self.assertNotEqual(in_finished, in_skipped)

            # 窗口 2 当前号必须释放
            wins = {w["id"]: w for w in self.admin.get("/api/窗口").json()}
            self.assertIsNone(wins[2]["当前号"])

            # 终态事件唯一，败方动作不得写入
            slip = self._slip(tid)
            terminal = [e["动作"] for e in self.admin.get("/api/叫号流水").json()
                        if e["号牌"] == slip and e["动作"] in ("办结", "过号")]
            self.assertEqual(len(terminal), 1)

            # 台账一生效一拒绝，口径标记全局一致
            ledger = self.admin.get(f"/api/号牌/{tid}/意图台账").json()
            self.assertEqual({x["结局"] for x in ledger}, {"生效", "拒绝"})
            for r in (rf, rs):
                body = r.json()
                tag = body["仲裁口径"] if r.status_code == 200 else body["detail"]["仲裁口径"]
                self.assertEqual(tag, WRITE_POLICY)

            if in_finished:
                summary["办结胜"] += 1
                self.assertEqual(rf.status_code, 200)
                self.assertEqual(rs.status_code, 409)
                self.assertIn("办结", rs.json()["detail"]["拒因"])
                self.assertEqual(terminal, ["办结"])
            else:
                summary["过号胜"] += 1
                self.assertEqual(rs.status_code, 200)
                self.assertEqual(rf.status_code, 409)
                self.assertIn("已过号", rf.json()["detail"]["拒因"])
                self.assertEqual(terminal, ["过号"])

        # 真实争用下两个仲裁分支都必须被实际调度到
        self.assertGreater(summary["办结胜"], 0)
        self.assertGreater(summary["过号胜"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
