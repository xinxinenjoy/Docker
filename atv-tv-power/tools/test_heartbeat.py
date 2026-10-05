#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""连接探活（心跳）的回归测试 —— 离线跑，**不连任何设备、不发任何请求**。

用法（先装依赖）：

    pip install -r requirements.txt
    python tools/test_heartbeat.py

为什么要有这个文件（2026-09-29 事故的产物）
------------------------------------------
pyatv 的 Companion 协议**没有任何保活**（无应用层心跳、无 TCP keepalive），而
`atv.power.power_state` 读的是 pyatv 的**内存缓存**、不发网络请求。于是长空闲后连接
被静默回收（TCP 半开）时，服务既收不到推送、也读不出异常，`_session` 的循环永远不退出
—— 进程活着、容器健康，服务却彻底哑掉，连 Bark 告警都不响。

2026-09-27 09:34 ~ 09-29 21:11 就这样哑了 60 小时，直到手动重启容器。
修复点 = `LinkService._heartbeat()`：每轮真发一次 FetchAttentionState 探活。

⚠️ 这个文件守的就是「**连接死了必须被发现**」这一条 —— 它一旦失效，故障会再次
变成「无声无息、无人知晓」。改动 `_heartbeat` / `_session` 的循环 / `_raw_to_power_state`
之后，务必离线跑一遍再部署。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

import atv_tv_power as m  # noqa: E402
from pyatv.const import PowerState, Protocol  # noqa: E402
from pyatv.interface import Power as PowerInterface  # noqa: E402
from pyatv.protocols.companion.api import SystemStatus  # noqa: E402

_fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        _fails.append(name)
    print(f"  {'✔' if ok else '✘'} {name:<46} got={got!r} want={want!r}")


# --------------------------------------------------------------------------- #
# 假对象：把设备与网络全部替换掉
# --------------------------------------------------------------------------- #
class FakeApi:
    """假的 CompanionAPI。`fail_on=N` 表示第 N 次调用起抛异常（模拟连接已死）。"""

    def __init__(self, status=SystemStatus.Asleep, fail_on: int | None = None, exc=None):
        self.status = status
        self.fail_on = fail_on
        self.exc = exc or asyncio.TimeoutError("simulated dead connection")
        self.calls = 0
        self.subscribed: list[str] = []

    async def fetch_attention_state(self):
        self.calls += 1
        if self.fail_on is not None and self.calls >= self.fail_on:
            raise self.exc
        return self.status

    def listen_to(self, event, cb):          # 必需：_attach_raw_guard 会调
        pass

    async def subscribe_event(self, event):
        self.subscribed.append(event)


class _ProtoHolder:
    """对应 atv._interfaces[Power]._interfaces[Protocol.Companion]（有 .api）。"""

    def __init__(self, api):
        self._interfaces = {Protocol.Companion: self}
        self.api = api


class _FakePowerIface:
    def __init__(self, api):
        self.power_state = PowerState.Off
        self._listener = None
        self._interfaces = {PowerInterface: _ProtoHolder(api)}

    @property
    def listener(self):
        return self._listener

    @listener.setter
    def listener(self, value):
        self._listener = value


class FakeAtv:
    def __init__(self, api):
        self.power = _FakePowerIface(api)
        self._interfaces = {PowerInterface: _ProtoHolder(api)}
        self.closed = False

    def close(self):
        self.closed = True


class FakeConf:
    name = "假苹果TV"


def make_service(api, poll_seconds: float = 0.05) -> m.LinkService:
    cfg = {
        "delay_seconds": 0.05,
        "poll_seconds": poll_seconds,
        "retries": 1,
        "retry_interval": 0.01,
        "reconnect_delay": 0.01,
        "atv": {"host": "192.168.1.10", "credentials": "keys/atv_credentials.json"},
        "tv": {"host": "192.168.1.11", "port": 6095, "timeout": 1, "verify_delay": 0.01},
        "bark": {"enabled": False},          # 测试绝不允许真推手机
        "guard": {"enabled": True, "on_unknown": "skip"},
    }
    svc = m.LinkService(cfg)
    svc.tv.state = lambda: m.TV_OFF          # 电视当作已关，杜绝真发 HTTP 键
    return svc


async def _coro(value):
    return value


# --------------------------------------------------------------------------- #
# 单元级
# --------------------------------------------------------------------------- #
async def unit_tests() -> None:
    print("=== ① 原始状态 -> PowerState 映射（口径须与 pyatv 一致） ===")
    check("Asleep -> Off", m._raw_to_power_state(SystemStatus.Asleep), PowerState.Off)
    for s in (SystemStatus.Awake, SystemStatus.Screensaver, SystemStatus.Idle):
        check(f"{s.name} -> On", m._raw_to_power_state(s), PowerState.On)
    check("Unknown -> Unknown", m._raw_to_power_state(SystemStatus.Unknown), PowerState.Unknown)

    print("=== ② 心跳正常返回：不抛异常，且更新原始状态 ===")
    svc = make_service(None)
    svc._raw_api = FakeApi(SystemStatus.Asleep)
    svc.state = PowerState.Off
    try:
        await svc._heartbeat()
        check("Asleep 心跳不抛异常", True, True)
    except Exception as exc:                                  # noqa: BLE001
        check("Asleep 心跳不抛异常", f"{type(exc).__name__}: {exc}", True)
    check("_raw 已更新", svc._raw, "Asleep")
    check("state 未变（对账无差异）", svc.state, PowerState.Off)

    print("=== ③ 心跳对账：推送漏了唤醒事件，应把状态纠回来 ===")
    svc = make_service(None)
    svc._raw_api = FakeApi(SystemStatus.Awake)
    svc.state = PowerState.Off                     # 本地以为还待机
    await svc._heartbeat()
    check("state 被纠正为 On", svc.state, PowerState.On)
    check("_raw = Awake", svc._raw, "Awake")

    print("=== ④ ★反例：连接死了（fetch 抛异常）=> 必须抛 RuntimeError ===")
    svc = make_service(None)
    svc._raw_api = FakeApi(fail_on=1)
    try:
        await svc._heartbeat()
        check("抛 RuntimeError", "没抛", "RuntimeError")
    except RuntimeError as exc:
        check("抛 RuntimeError", True, True)
        check("异常文案点明探活失败", "探活失败" in str(exc), True)
    except Exception as exc:                                  # noqa: BLE001
        check("抛 RuntimeError", f"错误类型 {type(exc).__name__}", "RuntimeError")

    print("=== ⑤ 拿不到 Companion API：静默跳过，不误判成失联 ===")
    svc = make_service(None)
    svc._raw_api = None
    try:
        await svc._heartbeat()
        check("不抛异常", True, True)
    except Exception as exc:                                  # noqa: BLE001
        check("不抛异常", f"{type(exc).__name__}: {exc}", True)


# --------------------------------------------------------------------------- #
# 端到端：心跳失败必须让 _session 退出（本次修复的核心）
# --------------------------------------------------------------------------- #
async def e2e_tests() -> None:
    print("=== ⑥ ★端到端：连接静默死亡 => _session 退出、资源清理干净 ===")
    api = FakeApi(SystemStatus.Asleep, fail_on=3)   # 前 2 轮正常，第 3 轮死
    svc = make_service(api)
    svc._resolve_atv_conf = lambda: _coro(FakeConf())
    fake_atv = FakeAtv(api)

    async def fake_connect(conf, loop):
        return fake_atv

    orig_connect = m.pyatv.connect
    m.pyatv.connect = fake_connect
    try:
        try:
            await asyncio.wait_for(svc._session(), timeout=5)
            check("会话退出（抛 RuntimeError）", "没抛（= 永久卡死！）", "RuntimeError")
        except asyncio.TimeoutError:
            check("会话退出（抛 RuntimeError）", "超时未退出（= 修复没生效！）", "RuntimeError")
        except RuntimeError as exc:
            check("会话退出（抛 RuntimeError）", True, True)
            check("原因指向探活失败", "探活失败" in str(exc), True)
    finally:
        m.pyatv.connect = orig_connect

    check("fetch 被调用 3 次", api.calls, 3)
    check("订阅了 SystemStatus", "SystemStatus" in api.subscribed, True)
    check("_raw_api 已清理", svc._raw_api, None)
    check("listener 已释放", svc._listener, None)
    check("atv 已置空", svc.atv, None)
    check("连接已 close", fake_atv.closed, True)

    print("=== ⑦ 对照：连接健康时，会话应一直转下去（不被心跳打断） ===")
    api2 = FakeApi(SystemStatus.Asleep)
    svc2 = make_service(api2)
    svc2._resolve_atv_conf = lambda: _coro(FakeConf())

    async def fake_connect2(conf, loop):
        return FakeAtv(api2)

    m.pyatv.connect = fake_connect2
    try:
        await asyncio.wait_for(svc2._session(), timeout=0.4)
        check("心跳健康时不退出", "退出了", "不退出")
    except asyncio.TimeoutError:
        check("心跳健康时不退出", True, True)         # 超时 = 一直在跑，符合预期
    except Exception as exc:                                  # noqa: BLE001
        check("心跳健康时不退出", f"{type(exc).__name__}: {exc}", True)
    finally:
        m.pyatv.connect = orig_connect
    check("心跳被反复调用", api2.calls > 2, True)


async def main() -> int:
    await unit_tests()
    print()
    await e2e_tests()
    print()
    print("=" * 62)
    if _fails:
        print(f"❌ {len(_fails)} 项未通过：{_fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
