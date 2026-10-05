#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""防误关判据的回归测试 —— 离线跑决策表，**不连任何设备、不发任何请求**。

用法（先装依赖）：

    pip install -r requirements.txt
    python tools/test_guard.py

为什么要有这个文件：`_note_raw` / `_guard_verdict` / `_on_raw_push` 出错的后果是
**静默判错** —— 要么「该关没关」，要么「误关」，两者都不抛异常、光看日志很难发现。
所以改动这几个函数之后，务必离线跑一遍再部署。

（`SystemStatus` 是普通 `Enum` 而非 `IntEnum`：取数值用 `.value`，
直接 `int(SystemStatus.Awake)` 会 TypeError —— 这个坑真踩过。）
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from atv_tv_power import LinkService  # noqa: E402
from pyatv.protocols.companion.api import SystemStatus  # noqa: E402

BASE_CFG = {
    "delay_seconds": 5, "poll_seconds": 30, "retries": 3,
    "retry_interval": 5, "reconnect_delay": 30,
    "atv": {"host": "", "credentials": "keys/atv_credentials.json"},
    "tv": {"host": "", "port": 6095, "timeout": 4, "verify_delay": 4},
    "bark": {"enabled": False},          # 测试绝不允许真推手机
}

_fails: list[str] = []


def svc(guard: dict | None = None) -> LinkService:
    cfg = dict(BASE_CFG)
    cfg["guard"] = dict(guard or {"enabled": True, "on_unknown": "skip"})
    return LinkService(cfg)


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'✔' if ok else '✘'} {name:52s} got={got!r} want={want!r}")
    if not ok:
        _fails.append(name)


def main() -> int:
    print("=== ① 决策表：待机前状态 -> 是否关电视 ===")
    for raw, want_close in [("Awake", True), ("Screensaver", False), ("Idle", False)]:
        s = svc()
        s._raw_before_asleep = raw
        check(f"{raw} -> 关电视?", s._guard_verdict()[0], want_close)

    print("\n=== ② on_unknown 两种取舍 ===")
    for policy, want in [("skip", False), ("close", True)]:
        s = svc({"enabled": True, "on_unknown": policy})
        s._raw_before_asleep = "Unknown"
        check(f"Unknown + on_unknown={policy}", s._guard_verdict()[0], want)

    print("\n=== ③ guard.enabled=false 时退回旧逻辑 ===")
    s = svc({"enabled": False, "on_unknown": "skip"})
    s._raw_before_asleep = "Screensaver"
    check("已关闭判据 -> 仍关电视", s._guard_verdict()[0], True)

    print("\n=== ④ ★核心：Asleep 不得覆盖「待机前快照」 ===")
    s = svc()
    for st in (SystemStatus.Awake, SystemStatus.Asleep):
        s._note_raw(st, "测试")
    check("Awake -> Asleep 后快照仍为 Awake", s._raw_before_asleep, "Awake")
    check("当前状态已变为 Asleep", s._raw, "Asleep")
    check("→ 判据结论（应关电视）", s._guard_verdict()[0], True)

    s = svc()
    for st in (SystemStatus.Screensaver, SystemStatus.Asleep):
        s._note_raw(st, "测试")
    check("Screensaver -> Asleep 后快照仍为 Screensaver",
          s._raw_before_asleep, "Screensaver")
    check("→ 判据结论（应不关）", s._guard_verdict()[0], False)

    print("\n=== ⑤ 真实推送载荷解析（走 _on_raw_push） ===")
    s = svc()
    s._on_raw_push({"state": SystemStatus.Awake.value})
    check("推送 state=3 -> Awake", s._raw, "Awake")
    s._on_raw_push({"state": SystemStatus.Screensaver.value})
    check("推送 state=2 -> Screensaver", s._raw, "Screensaver")
    check("快照跟随更新", s._raw_before_asleep, "Screensaver")
    s._on_raw_push({"state": SystemStatus.Asleep.value})
    check("推送 state=1 -> Asleep（快照不动）", s._raw_before_asleep, "Screensaver")
    s._on_raw_push({"nonsense": 1})          # 坏载荷不应抛异常
    check("坏载荷被吞掉、状态不变", s._raw, "Asleep")

    print("\n=== ⑥ 唤醒后再睡：快照要跟着刷新 ===")
    s = svc()
    s._note_raw(SystemStatus.Awake, "t")
    check("Awake -> 快照 Awake", s._raw_before_asleep, "Awake")
    s._note_raw(SystemStatus.Asleep, "t")
    check("睡着 -> 快照仍 Awake", s._raw_before_asleep, "Awake")
    s._note_raw(SystemStatus.Awake, "t")     # CEC 唤醒
    s._note_raw(SystemStatus.Screensaver, "t")
    check("唤醒后进屏保 -> 快照转 Screensaver", s._raw_before_asleep, "Screensaver")
    s._note_raw(SystemStatus.Asleep, "t")
    check("→ 这次不该关电视", s._guard_verdict()[0], False)

    print("\n" + "=" * 62)
    if _fails:
        print(f"❌ 失败 {len(_fails)} 项：{_fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
