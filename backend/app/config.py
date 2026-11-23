from __future__ import annotations
import os
from pathlib import Path

PROJECT_NAME = "叫号条"
ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("CALLSLIP_DB", str(ROOT / "data" / "callslip.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# 号牌状态（仅中文）
TICKET_STATUSES = ("等候", "叫号中", "办理中", "过号", "已办结", "已作废")
ACTIVE_QUEUE = ("等候", "叫号中", "办理中", "过号")

# ---------------------------------------------------------------------------
# 双写口（办结写口 / 过号写口）争用仲裁口径——全局唯一，禁止与另一口径混用。
# 口径 A「办结写口生效为准」：
#   1. 已存在生效的办结意图（号牌已办结）→ 过号写口整笔硬拒，不写过号事件；
#   2. 过号先生效落到过号态 → 此后办结写口硬拒，已过号不得改写为已办结；
#   3. 任一时刻一个号牌只允许一个终态，窗口指针随生效的一笔释放。
# 本常量同时进入每笔回执，作为口径自洽的运行期标记。
# ---------------------------------------------------------------------------
WRITE_POLICY = "办结写口优先"

# 写锁等待上限（毫秒）。BEGIN IMMEDIATE 争锁超时会抛 SQLITE_BUSY，
# 由事务上下文回滚，绝不允许半截写入残留。
BUSY_TIMEOUT_MS = 5000
