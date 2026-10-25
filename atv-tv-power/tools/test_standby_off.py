#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「ATV 待机 -> 关电视」触发的回归测试 —— 离线跑，**不连任何设备、不发任何请求**。

用法（先装依赖）：

    pip install -r requirements.txt
    python tools/test_standby_off.py

为什么要有这个文件（前身 test_guard.py，2026-10-25 改写）
------------------------------------------------------
2026-10-23 的现场故障：ATV 在屏保状态下被**人**按遥控器关机，旧判据读到
「待机前最后一帧 = Screensaver」⇒ 判成「ATV 自己睡着的」⇒ **跳过关电视**，
电视没关、ATV 随后还被电视那侧唤醒。

根因是判据在猜意图。红领巾 2026-10-25 已关闭 ATV 的「闲置自动睡眠」⇒ 任何待机
都只可能是人按的 ⇒ 判据删除，规则收敛成一句：**待机就关电视，延迟 3s**。

本文件守的就是这句话，以及它旁边两条**不许顺手删**的保护：
  · 延迟期间 ATV 被唤醒 ⇒ 整个取消（防误触）
  · 到点时 ATV 已不是待机 ⇒ 放弃

（`SystemStatus` 是普通 `Enum` 而非 `IntEnum`：取数值用 `.value`，
直接 `int(SystemStatus.Awake)` 会 TypeError —— 这个坑真踩过。）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from atv_tv_power import LinkService  # noqa: E402
from pyatv.const import PowerState  # noqa: E402
from pyatv.protocols.companion.api import SystemStatus  # noqa: E402

_fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'✔' if ok else '✘'} {name:52s} got={got!r} want={want!r}")
    if not ok:
        _fails.append(name)


class FakeTv:
    """假电视：记录被叫了几次关电视，不联网。"""

    def __init__(self) -> None:
        self.off_calls = 0
        self.state_value = "on"
        self.verify_value = "off"

    def state(self) -> str:
        return self.state_value

    def turn_off(self):
        self.off_calls += 1
        self.state_value = "off"
        return True, "已发送电源键（假）"

    def verify(self) -> str:
        return self.verify_value


def make_service(delay: float = 0.0) -> LinkService:
    cfg = {
        "delay_seconds": delay,
        "poll_seconds": 30,
        "retries": 1,
        "retry_interval": 0.01,
        "reconnect_delay": 30,
        "atv": {"host": "", "credentials": "keys/atv_credentials.json"},
        "tv": {"host": "", "port": 6095, "timeout": 1, "verify_delay": 0.01},
        "bark": {"enabled": False},          # 测试绝不允许真推手机
    }
    svc = LinkService(cfg)
    svc.tv = FakeTv()
    return svc


def run_off(svc: LinkService) -> None:
    """直接跑到点动作那一步（跳过真实等待）。"""
    asyncio.run(svc._do_delayed_tv_off())


def main() -> int:
    print("=== ① 判据已删除：不得再出现「待机前快照」这种状态 ===")
    s = make_service()
    check("没有 _raw_before_asleep 属性", hasattr(s, "_raw_before_asleep"), False)
    check("没有 _guard_verdict 方法", hasattr(s, "_guard_verdict"), False)

    print("\n=== ② ★核心回归：待机前是屏保（人按的关机）也必须关电视 ===")
    for raw in (SystemStatus.Awake, SystemStatus.Screensaver, SystemStatus.Idle):
        s = make_service()
        s._note_raw(raw, "测试")
        s._note_raw(SystemStatus.Asleep, "测试")
        s.state = PowerState.Off
        run_off(s)
        check(f"待机前 {raw.name} -> 关电视次数", s.tv.off_calls, 1)

    print("\n=== ③ 原始状态完全拿不到（Unknown）也照关 ===")
    s = make_service()
    s.state = PowerState.Off
    run_off(s)
    check("Unknown -> 关电视次数", s.tv.off_calls, 1)

    print("\n=== ④ 到点时 ATV 已不是待机 => 放弃，不碰电视 ===")
    s = make_service()
    s.state = PowerState.On            # 延迟窗口内被唤醒（_on_power_change 已把 state 改掉）
    run_off(s)
    check("已唤醒 -> 关电视次数", s.tv.off_calls, 0)

    print("\n=== ⑤ 待机事件会调度一个延迟任务；唤醒会取消它 ===")

    async def sched_case() -> tuple[int, int]:
        svc = make_service(delay=0.05)
        svc._loop = asyncio.get_running_loop()
        svc._on_power_change(PowerState.On, PowerState.Off)   # ATV 待机
        had_task = int(svc._pending is not None)
        await asyncio.sleep(0.15)                             # 等延迟跑完
        return had_task, svc.tv.off_calls

    had_task, off_calls = asyncio.run(sched_case())
    check("待机事件 -> 建了延迟任务", had_task, 1)
    check("延迟到点 -> 关电视 1 次", off_calls, 1)

    async def cancel_case() -> int:
        svc = make_service(delay=0.2)
        svc._loop = asyncio.get_running_loop()
        svc._on_power_change(PowerState.On, PowerState.Off)
        await asyncio.sleep(0.02)
        svc._on_power_change(PowerState.Off, PowerState.On)   # 立刻又被唤醒
        await asyncio.sleep(0.4)
        return svc.tv.off_calls

    check("延迟期间被唤醒 -> 不关电视", asyncio.run(cancel_case()), 0)

    print("\n=== ⑥ 原始状态推送解析（坏载荷不许抛异常） ===")
    s = make_service()
    s._on_raw_push({"state": SystemStatus.Awake.value})
    check("推送 state=3 -> Awake", s._raw, "Awake")
    s._on_raw_push({"state": SystemStatus.Screensaver.value})
    check("推送 state=2 -> Screensaver", s._raw, "Screensaver")
    s._on_raw_push({"state": SystemStatus.Asleep.value})
    check("推送 state=1 -> Asleep", s._raw, "Asleep")
    s._on_raw_push({"nonsense": 1})          # 坏载荷不应抛异常
    check("坏载荷被吞掉、状态不变", s._raw, "Asleep")

    print("\n" + "=" * 62)
    if _fails:
        print(f"❌ 失败 {len(_fails)} 项：{_fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
