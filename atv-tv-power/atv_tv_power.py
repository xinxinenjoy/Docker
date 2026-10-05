#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Apple TV 待机 -> 自动关闭小米电视

背景
----
客厅小米电视的 HDMI-CEC 实现不完整：能响应 Apple TV 的 CEC 开机，但**不响应**
CEC Standby，所以按 ATV 遥控器关机后电视还亮着。本服务补上这一环。

原理
----
1. pyatv 通过 Companion 协议连 Apple TV，订阅电源状态推送（事件驱动，非轮询）。
2. 收到 PowerState.Off 后等待 delay_seconds（默认 20s）再二次确认；
   期间若 ATV 被重新唤醒则取消本次动作（防误触）。
3. 确认仍待机后，用小米电视的局域网 HTTP 接口（6095）发送「电源键」。

为什么走 6095 HTTP 而不是 ADB
---------------------------
ADB 曾经能用（`input keyevent 26` 实测有效），但小米电视在关机后会把「ADB 调试」
开关复位，导致下次开机后 5555 不再监听 —— 每次都要人工去设置里重开，不可持续。

而 6095 这个小米电视自带的 HTTP 接口：
  - 不需要 token、不需要 ADB、不需要开发者选项、不需要任何硬件
  - 同时提供「控制」和「状态判断」两种能力（见下表）
  - 是米家 App / 小爱同学控制该电视所用的本地通道

★ 实测结论（2026-09-19，勿凭直觉推翻）
----
【Apple TV 侧】
1. pyatv 的 StateProducer.listener setter 内部是 weakref.ref(target)。
   ⇒ listener 对象必须被强引用持有！写成 `atv.power.listener = MyListener()`
     会因对象当场被 GC 回收而**永远收不到推送**。
2. ATV 进入待机后，Companion 长连接【不会】断开，待机事件实时送达。
3. ATV 待机时 7000 / 49153 端口【仍然开放】⇒ 不能靠探端口判断 ATV 电源状态。
4. 单纯 connect + 读 power_state【不会】唤醒 ATV（可放心做兜底轮询）。
5. pyatv 0.18 的配对是两段式：pairing.pin(x) 然后 await pairing.finish()。

【小米电视侧】
6. UDP 54321 有 miio 响应（device_id 0x4e9ec433），但**本方案不需要 token**。
7. TCP 6095 = 小米电视局域网控制接口，实测行为：
       电视开机 → 端口开放，/request?action=isalive 返回 200
       电视待机 → 端口超时，任何请求无响应
       关机瞬间 → HTTP 502（过渡态）
   ⇒ 「6095 是否响应 200」即可判断电视开关，无需 ADB。
8. keyevent 为**切换语义**（power = 开/关翻转），
   所以发键前必须先确认电视处于开机态，否则会把已关的电视打开。
9. 待机时 6095 无响应 ⇒ 无法用它开机。开机由 ATV 的 CEC 负责（用户侧已支持）。
10. 电视型号 MiTV-ANSP0 / Android 9 / MIUI TV 22.12.6.2963，iOS 侧 AirPlay 报 AppleTV3,2。

【防误关判据 —— 2026-09-22 实测，勿凭直觉推翻】
11. pyatv 把设备上报的原始状态 SystemStatus 做了**有损压缩**（见 companion/__init__.py
    的 _system_status_to_power_state）：Asleep -> Off；Screensaver / Awake / Idle -> On。
    ⇒ 只读 PowerState 时，「你在用 ATV」和「ATV 闲置后自己睡着」都是 On，区分被抹掉。
12. 原始枚举（companion/api.py 的 SystemStatus）：
        Unknown=0x00（pyatv 注明非协议值）/ Asleep=0x01 / Screensaver=0x02 /
        Awake=0x03 / Idle=0x04（pyatv 自己标注 Not verified，实测也从未见过）
13. 绕过压缩：`atv._interfaces[Power]._interfaces[Protocol.Companion].api` 拿到
    CompanionAPI，再 listen_to("SystemStatus", cb) + subscribe_event("SystemStatus")。
    ★ listen_to 走 MessageDispatcher，是 append ⇒ **不会覆盖** pyatv 自己的回调；
      subscribe_event 内部有去重 ⇒ 重复订阅安全。（同步回调会被 loop.call_soon 调用）
14. 实测对照（2026-09-22 21:32~21:46）：播放中 = Awake；自然进入/手动触发屏保 =
    Screensaver；两者 pyatv 都报 PowerState.On。Asleep 对应 PowerState.Off。
15. 电视侧 6095 **没有任何「读当前输入源 / 前台 App」的接口**（17 个候选全 404；
    document 常写的 getCurrentApp 是内容农场编的）。电视开机状态下，反复切换 App /
    HDMI 输入源共 6 次，`url`/`build`/`platform`/`stream` 四个字段零变化
    ⇒ 「问电视你在放什么」在这台机器上无解，判据只能建在 ATV 侧。

【长连接保活 —— 2026-09-29 实测，勿凭直觉推翻】
16. pyatv 的 Companion 协议【没有任何保活机制】：
    · 协议内无应用层心跳、无周期任务（grep create_task / call_later / while True
      于 pyatv/protocols/companion/ ⇒ **零命中**，只有一处 call_soon 做回调分发）；
    · TCP keepalive 也没开 —— pyatv/support/net.py 的 tcp_keepalive() **只有 MRP
      协议调用**（protocols/mrp/connection.py），Companion 一次都没调。
      （旁证：容器 /proc/net/tcp 上该连接的 timeout 字段 = 0，keepalive 定时器未运行）
    ⇒ 长空闲后连接被路由器 / AP 静默回收（TCP 半开）时，客户端【无从察觉】。
17. `atv.power.power_state` 读的是 pyatv 的【内存缓存】（companion/__init__.py 的
    `return self._power_state`），**不发任何网络请求** ⇒ 既不会因连接死亡而抛异常，
    也永远返回最后一次推送的旧值。⚠️ 拿它当探活 = **假探活**（本事故的直接死因）。
18. 真正走网络的探活是 CompanionAPI.fetch_attention_state()：发一次
    FetchAttentionState 并等响应，pyatv 内置 5s 超时（protocol.py 的 DEFAULT_TIMEOUT）
    ⇒ 连接死了必然在 5s 内失败。本服务每 poll_seconds 调它一次作心跳（见
    LinkService._heartbeat），失败即退出会话并重连。
19. 事故复盘（2026-09-27 09:34 ~ 09-29 21:11，**哑掉 60 小时**）：会话订阅成功后
    连接静默死亡 ⇒ 推送收不到、power_state 读缓存不报错、_session 循环永不退出 ⇒
    _fail_streak 不累加 ⇒ **连 Bark 告警都没响**。进程活着、容器健康、服务全哑，
    只能手动重启恢复。根因是 16+17 叠加：没有任何走网络的探活（见 _heartbeat）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode

import pyatv
from pyatv.const import PowerState, Protocol
from pyatv.interface import Power as PowerInterface
from pyatv.interface import PowerListener

try:                                        # 原始状态枚举，pyatv 未从 const 导出
    from pyatv.protocols.companion.api import SystemStatus
except Exception:                           # pragma: no cover - 版本差异兜底
    SystemStatus = None                     # type: ignore[assignment]

BASE = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("tvlink")

TV_ON = "on"
TV_OFF = "off"
TV_TRANSITION = "transition"


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    path = Path(os.environ.get("TVLINK_CONFIG", BASE / "config.json"))
    if not path.exists():
        raise SystemExit(f"配置文件不存在: {path}")
    cfg = json.loads(path.read_text(encoding="utf-8"))

    cfg.setdefault("delay_seconds", 20)      # ATV 待机后延迟多久才关电视
    cfg.setdefault("poll_seconds", 30)       # 兜底轮询间隔（防推送丢事件）
    cfg.setdefault("retries", 3)             # 关电视重试次数
    cfg.setdefault("retry_interval", 5)      # 重试间隔
    cfg.setdefault("reconnect_delay", 30)    # ATV 掉线后重连间隔

    atv = cfg.setdefault("atv", {})
    atv.setdefault("host", "")
    atv.setdefault("credentials", "keys/atv_credentials.json")

    tv = cfg.setdefault("tv", {})
    tv.setdefault("host", "")
    tv.setdefault("port", 6095)
    tv.setdefault("timeout", 4)
    tv.setdefault("verify_delay", 4)         # 发键后多久复查状态

    bark = cfg.setdefault("bark", {})
    bark.setdefault("enabled", False)
    bark.setdefault("server", "https://api.day.app")
    bark.setdefault("key", "")               # 直接写 key（临时用）
    bark.setdefault("key_file", "")          # 从文件读 key（凭据不进 git 用这个）
    bark.setdefault("group", "")
    bark.setdefault("sound", "")
    bark.setdefault("icon", "")
    bark.setdefault("offline_after", 3)      # ATV 连续失败几次才告警
    return cfg


def load_guard(cfg: dict) -> dict:
    """防误关判据的配置。

    ATV 待机时不能无条件关电视：它可能是「闲置后自己睡着」，而你正在用电视的
    另一个输入源（Apple TV 的屏保/睡眠是它自己的计时器，与你切没切输入源无关）。
    """
    guard = cfg.setdefault("guard", {})
    guard.setdefault("enabled", True)
    # 拿不到 ATV 原始状态时的取舍：skip=不关电视（保守，默认）/ close=按旧逻辑关
    guard.setdefault("on_unknown", "skip")
    return guard


# --------------------------------------------------------------------------- #
# 电视侧：小米电视 6095 局域网 HTTP 接口
# --------------------------------------------------------------------------- #
class TvController:
    """通过小米电视自带的局域网 HTTP 接口控制电视。无状态、线程内使用。"""

    def __init__(self, cfg: dict) -> None:
        self.host: str = cfg["host"]
        self.port: int = int(cfg["port"])
        self.timeout: int = int(cfg["timeout"])
        self.verify_delay: int = int(cfg["verify_delay"])
        self.base = f"http://{self.host}:{self.port}"

    # -- 内部 ------------------------------------------------------------- #
    def _request(self, path: str, timeout: float | None = None):
        return urllib.request.urlopen(
            f"{self.base}{path}", timeout=timeout or self.timeout
        )

    # -- 对外 ------------------------------------------------------------- #
    def state(self) -> str:
        """判断电视状态：on / off / transition。

        · 200        -> 开机（HTTP 服务正常应答）
        · 502        -> 关机过程中的过渡态
        · 超时/拒绝  -> 已待机（待机后该端口不再响应）
        """
        try:
            resp = self._request("/request?action=isalive")
            return TV_ON if resp.status == 200 else TV_TRANSITION
        except urllib.error.HTTPError as exc:
            return TV_TRANSITION if exc.code == 502 else TV_OFF
        except Exception:
            return TV_OFF

    def turn_off(self) -> tuple[bool, str]:
        """让电视进入待机。返回 (是否达成目的, 说明)。"""
        st = self.state()
        if st == TV_OFF:
            return True, "电视本就处于待机状态，无需操作"
        if st == TV_TRANSITION:
            return True, "电视正在关机过程中，跳过操作"

        try:
            self._request("/controller?action=keyevent&keycode=power")
        except urllib.error.HTTPError as exc:
            # 关机瞬间接口可能直接 502，属预期
            log.debug("发送电源键返回 HTTP %s（预期内）", exc.code)
        except Exception as exc:
            return False, f"发送电源键失败: {type(exc).__name__}: {exc}"
        return True, "已发送电源键"

    def verify(self) -> str:
        """发键后复查：返回复查到的状态。"""
        return self.state()


# --------------------------------------------------------------------------- #
# 通知侧：Bark 推送
# --------------------------------------------------------------------------- #
class Notifier:
    """Bark 推送 —— 只在「需要人处理」时出声。

    口径与其它服务的 bark_push 保持一致
    （同一个 key、同一个分组），这样手机通知列表里是一个风格。

    两条纪律：
      · **推送永不影响主流程**：任何异常都只写日志，绝不让关电视失败。
      · **同一个问题只推一条**：首次触发推，恢复了补一条；中间不刷屏。
    """

    def __init__(self, cfg: dict) -> None:
        self.enabled = bool(cfg.get("enabled", False))
        self.server = str(cfg.get("server") or "https://api.day.app").rstrip("/")
        self.group = cfg.get("group") or ""
        self.sound = cfg.get("sound") or ""
        self.icon = cfg.get("icon") or ""
        self.offline_after = int(cfg.get("offline_after", 3))
        self._active: set[str] = set()          # 当前「已告警、未恢复」的问题
        self.key = self._resolve_key(cfg)

        if self.enabled and not self.key:
            log.error("Bark 已启用但拿不到 key，推送将被禁用（其余功能不受影响）")
            self.enabled = False
        if self.enabled:
            log.info("Bark 告警已启用（分组「%s」，ATV 连续失败 %d 次即告警）",
                     self.group or "默认", self.offline_after)

    @staticmethod
    def _resolve_key(cfg: dict) -> str:
        """key 优先取配置项；没有则从 key_file 读（便于把凭据挪出 config.json）。"""
        if cfg.get("key"):
            return str(cfg["key"])
        key_file = cfg.get("key_file")
        if not key_file:
            return ""
        path = BASE / key_file
        if not path.exists():
            log.warning("Bark 凭据文件不存在: %s", path)
            return ""
        try:
            return str(json.loads(path.read_text(encoding="utf-8")).get("key", ""))
        except Exception:
            log.exception("读取 Bark 凭据失败: %s", path)
            return ""

    def _push(self, title: str, body: str) -> None:
        data = {"device_key": self.key, "title": title, "body": body}
        for field, value in (("group", self.group), ("sound", self.sound), ("icon", self.icon)):
            if value:
                data[field] = value
        req = urllib.request.Request(
            f"{self.server}/push",
            data=urlencode(data).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read(200)
            log.info("Bark 已推送：%s", title)
        except Exception as exc:
            log.warning("Bark 推送失败（不影响主流程）: %s: %s", type(exc).__name__, exc)

    async def push(self, title: str, body: str) -> None:
        """直接推一条（不参与去重，仅测试/一次性通知用）。"""
        if self.enabled:
            await asyncio.to_thread(self._push, title, body)

    async def raise_once(self, key: str, title: str, body: str) -> None:
        """问题首次出现时推一条；在它恢复之前不再重复推。"""
        if not self.enabled or key in self._active:
            return
        self._active.add(key)
        await self.push(title, body)

    async def clear(self, key: str, title: str, body: str) -> None:
        """问题恢复时补推一条 —— 之前没告警过就不推，免得「恢复」消息凭空冒出来。"""
        if not self.enabled or key not in self._active:
            return
        self._active.discard(key)
        await self.push(title, body)


# --------------------------------------------------------------------------- #
# Apple TV 侧：监听
# --------------------------------------------------------------------------- #
def _raw_to_power_state(status) -> PowerState:
    """设备原始状态（SystemStatus）-> PowerState。

    口径与 pyatv 的 CompanionPower._system_status_to_power_state 一致（见模块头结论 11），
    但这里按【枚举名】判断，不依赖 pyatv 的私有方法，免得版本一变就踩空。
    """
    name = getattr(status, "name", None)
    if name == "Asleep":
        return PowerState.Off
    if name in ("Screensaver", "Awake", "Idle"):
        return PowerState.On
    return PowerState.Unknown


class _PowerListener(PowerListener):
    """把 pyatv 的推送桥接到服务回调。

    ⚠️ 实例必须被强引用持有（见实测结论 1），否则永远收不到事件。
    """

    def __init__(self, on_change) -> None:
        self._on_change = on_change

    def powerstate_update(self, old_state: PowerState, new_state: PowerState) -> None:
        try:
            self._on_change(old_state, new_state)
        except Exception:
            log.exception("处理电源状态变化时出错")


class LinkService:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        load_guard(cfg)                     # 判据配置的默认值在这里补齐，别依赖调用方
        self.tv = TvController(cfg["tv"])
        self.notifier = Notifier(cfg.get("bark", {}))
        self.state: PowerState = PowerState.Unknown
        self.atv = None
        self._listener: _PowerListener | None = None   # ★ 强引用，勿删
        self._pending: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._fail_streak = 0                          # ATV 连续连接失败次数

        # 防误关判据：设备原始状态（pyatv 把 Screensaver/Awake/Idle 压成了同一个 On）
        self._raw: str = "Unknown"                     # 当前原始状态
        self._raw_before_asleep: str = "Unknown"       # 待机前最后一帧 —— 判据靠它
        self._raw_api = None                           # Companion API（订阅 / 心跳 / 读原始状态）

    # -- ATV 连接 --------------------------------------------------------- #
    async def _resolve_atv_conf(self):
        loop = asyncio.get_running_loop()
        cred_path = BASE / self.cfg["atv"]["credentials"]
        if not cred_path.exists():
            raise RuntimeError(f"ATV 凭据不存在: {cred_path}（需先执行配对）")
        creds = json.loads(cred_path.read_text(encoding="utf-8"))

        host = self.cfg["atv"]["host"]
        atvs = await pyatv.scan(loop, hosts=[host] if host else None, timeout=5)
        if not atvs and host:
            atvs = await pyatv.scan(loop, timeout=5)   # 兜底：全量 mDNS

        want = creds.get("identifier", "").lower().replace(":", "")
        conf = None
        for c in atvs:
            if c.identifier.lower().replace(":", "") == want:
                conf = c
                break
        if conf is None and atvs:
            conf = atvs[0]
        if conf is None:
            raise RuntimeError("局域网内找不到 Apple TV")

        conf.set_credentials(Protocol.Companion, creds["companion_credentials"])
        return conf

    async def _session(self) -> None:
        loop = asyncio.get_running_loop()
        self._loop = loop

        conf = await self._resolve_atv_conf()
        self.atv = await pyatv.connect(conf, loop)

        # 初始状态只记录、不触发，避免服务重启时把电视误关
        try:
            self.state = self.atv.power.power_state
        except Exception:
            self.state = PowerState.Unknown
        log.info("已连接 Apple TV「%s」，当前电源状态 = %s", conf.name, self.state)
        log.info("当前电视状态 = %s", await asyncio.to_thread(self.tv.state))

        # 连上了 ⇒ 之前若报过「ATV 失联」，这里补一条恢复通知
        self._fail_streak = 0
        await self.notifier.clear(
            "atv_offline",
            "🟢 Apple TV 已恢复",
            "已重新连上 Apple TV，「待机 → 关电视」联动恢复正常。",
        )

        self._listener = _PowerListener(self._on_power_change)
        self.atv.power.listener = self._listener
        log.info("已订阅电源状态推送（延迟 %ss 确认后关电视）", self.cfg["delay_seconds"])

        # 原始状态按会话重置：宁可「未知」，也不要拿上一会话的陈旧值去下判断
        self._raw = "Unknown"
        self._raw_before_asleep = "Unknown"
        await self._attach_raw_guard()

        try:
            while True:
                await asyncio.sleep(self.cfg["poll_seconds"])

                # ★ 这是【唯一】真正走网络的探活。pyatv 的 Companion 连接没有保活
                #   （结论 16），而下面读的 power_state 是内存缓存（结论 17）——
                #   没有这一步，连接静默死亡后本循环会永远转下去、什么都发现不了。
                #   失败会抛异常 ⇒ 退出本会话 ⇒ run() 重连（结论 19 的修复点）。
                await self._heartbeat()

                try:
                    ps = self.atv.power.power_state
                except Exception as exc:
                    raise RuntimeError(f"读取电源状态失败，判定连接已失效: {exc}") from exc
                if ps != self.state and ps != PowerState.Unknown:
                    log.warning("轮询发现推送漏了事件: %s -> %s，按新状态补偿处理", self.state, ps)
                    self._on_power_change(self.state, ps)
        finally:
            try:
                self.atv.power.listener = None
            except Exception:
                pass
            self._listener = None
            self._raw_api = None
            try:
                self.atv.close()
            except Exception:
                pass
            self.atv = None

    # -- 防误关判据：ATV 原始状态（SystemStatus） -------------------------- #
    async def _attach_raw_guard(self, alert: bool = True) -> None:
        """挂上设备原始状态的推送，用于区分「你在用 ATV」与「ATV 闲置后自己睡着」。

        为什么不用 atv.power.power_state：它已经把 Screensaver/Awake/Idle 三态压成
        同一个 PowerState.On，判据要的区分恰好被它丢掉了（见模块头实测结论 11）。

        三层保障：
          ① 初始快照 fetch_attention_state()；② 事件推送 listen_to("SystemStatus")；
          ③ 兜底：_session 每轮调 _heartbeat() 重取一次（推送全丢也能把状态纠回来）。
        ①② 都不成则告警。注意 ③ 是【无条件】跑的，同时也是整个服务的连接探活
        （结论 16~19：pyatv 的 Companion 没有保活，不主动探就发现不了连接静默死亡）。
        """
        api = None
        try:
            api = self.atv._interfaces[PowerInterface]._interfaces[Protocol.Companion].api
        except Exception as exc:
            log.warning("拿不到 Companion API，防误关判断不可用: %s: %s",
                        type(exc).__name__, exc)

        fetched = pushed = False
        if api is not None:
            try:
                self._note_raw(await api.fetch_attention_state(), "初始快照")
                fetched = True
            except Exception as exc:
                log.warning("读取 ATV 原始状态失败（不影响主流程）: %s: %s",
                            type(exc).__name__, exc)

            try:
                api.listen_to("SystemStatus", self._on_raw_push)
                await api.subscribe_event("SystemStatus")
                try:                            # 有些设备只推 TVSystemStatus
                    api.listen_to("TVSystemStatus", self._on_raw_push)
                    await api.subscribe_event("TVSystemStatus")
                except Exception:
                    pass
                self._raw_api = api
                pushed = True
                log.info("已订阅 ATV 原始状态推送，防误关判据生效")
            except Exception as exc:
                # 订阅失败不影响探活：_heartbeat 每轮都会自己去取一次（结论 18）
                self._raw_api = api
                log.warning("订阅原始状态推送失败，改由每 %ss 心跳对账: %s: %s",
                            self.cfg["poll_seconds"], type(exc).__name__, exc)

        if not alert:
            return
        if not (fetched or pushed):
            await self.notifier.raise_once(
                "guard_degraded",
                "🟡 防误关判断已降级",
                "拿不到 Apple TV 的原始状态（SystemStatus），无法区分「你正在用 ATV」"
                "和「ATV 闲置后自己睡着」。\n"
                "影响：待机时可能误关电视（你正在看别的输入源时）。\n"
                f"当前取舍：{'不关电视（保守）' if self.cfg['guard']['on_unknown'] == 'skip' else '按旧逻辑关电视'}",
            )
        else:
            await self.notifier.clear(
                "guard_degraded",
                "🟢 防误关判断已恢复",
                "已能读取 Apple TV 的原始状态，误关保护重新生效。",
            )

    async def _heartbeat(self) -> None:
        """每轮「真的走一次网络」，确认 Companion 长连接还活着，并顺手对账状态。

        为什么非做不可（2026-09-27~29 哑掉 60 小时事故的修复点）
        --------------------------------------------------------
        pyatv 的 Companion 协议没有任何保活（结论 16），而 atv.power.power_state 读的是
        内存缓存、不发网络请求（结论 17）⇒ 长空闲后连接被静默回收（TCP 半开）时：
          · 推送收不到 —— ATV 的待机 / 唤醒事件根本到不了；
          · 读 power_state 既不报错、还一直返回旧值；
          · _session 的循环永不退出 ⇒ _fail_streak 不累加 ⇒ 连 Bark 告警都不响。
        结果就是：进程活着、容器健康，服务却彻底哑掉，只能手动重启。

        fetch_attention_state() 会真发一次 FetchAttentionState 并等响应（pyatv 内置
        5s 超时）⇒ 连接死了必然失败 ⇒ 这里抛异常 ⇒ _session 退出 ⇒ run() 重连。

        ⚠️ 是只读查询、不是 HID 命令 ⇒ 不会唤醒 ATV（--show-state 一直走同一路径）。
        """
        api = self._raw_api
        if api is None:
            # 拿不到 Companion API 属已知降级场景，_attach_raw_guard 已推过 🟡 告警
            return

        try:
            status = await api.fetch_attention_state()
        except Exception as exc:
            raise RuntimeError(
                f"探活失败（Companion 长连接可能已静默断开）: {type(exc).__name__}: {exc}"
            ) from exc

        self._note_raw(status, "心跳")

        # 对账：心跳拿到的电源状态与本地记录不一致 ⇒ 推送丢了，按新值补偿
        ps = _raw_to_power_state(status)
        if ps != self.state and ps != PowerState.Unknown:
            log.warning("心跳对账发现推送漏了事件: %s -> %s，按新状态补偿处理", self.state, ps)
            self._on_power_change(self.state, ps)

    def _on_raw_push(self, data: Mapping[str, Any]) -> None:
        """设备推送的原始状态。

        ⚠️ 同步函数：MessageDispatcher 对非协程回调走 loop.call_soon（见 core/protocol.py）。
        """
        try:
            self._note_raw(SystemStatus(int(data["state"])), "推送")  # type: ignore[misc]
        except Exception:
            log.warning("收到无法解析的 SystemStatus 推送: %r", data)

    def _note_raw(self, status, source: str) -> None:
        """记录原始状态。★ 关键：Asleep 不覆盖 _raw_before_asleep（判据靠它）。"""
        name = getattr(status, "name", None) or str(status)
        if name != self._raw:
            log.info("ATV 原始状态: %s -> %s（%s）", self._raw, name, source)
        self._raw = name
        if name != "Asleep":
            self._raw_before_asleep = name

    def _guard_verdict(self) -> tuple[bool, str]:
        """待机前的原始状态 -> (是否该关电视, 理由)。"""
        guard = self.cfg["guard"]
        if not guard["enabled"]:
            return True, "防误关判断未启用，按原逻辑关电视"

        raw = self._raw_before_asleep
        if raw == "Awake":
            return True, "待机前 = Awake（你正在用 ATV，是主动关机）"
        if raw in ("Screensaver", "Idle"):
            return False, (
                f"待机前 = {raw}（ATV 闲置后自己睡着的，"
                f"电视很可能正在放别的内容 / 你人已离开）"
            )
        if guard["on_unknown"] == "close":
            return True, f"待机前原始状态未知（{raw}），按旧逻辑关电视"
        return False, f"待机前原始状态未知（{raw}），保守起见不关电视"

    async def probe_once(self) -> None:
        """现场诊断：打印 ATV 电源 / 原始状态，以及「此刻若待机会怎么判」。

        全程只读：不唤醒设备、不发任何按键、不关任何东西。
        """
        loop = asyncio.get_running_loop()
        self._loop = loop
        conf = await self._resolve_atv_conf()
        self.atv = await pyatv.connect(conf, loop)
        try:
            try:
                ps = self.atv.power.power_state
            except Exception as exc:
                ps = f"{PowerState.Unknown}（读取失败: {type(exc).__name__}）"
            await self._attach_raw_guard(alert=False)
            should_close, reason = self._guard_verdict()
            print(f"设备       : {conf.name}")
            print(f"电源状态   : {ps}")
            print(f"原始状态   : {self._raw}")
            print(f"待机前快照 : {self._raw_before_asleep}")
            print(f"此刻若待机 : {'【关电视】' if should_close else '【不关电视】'} {reason}")
        finally:
            try:
                self.atv.close()
            except Exception:
                pass
            self.atv = None
            await asyncio.sleep(0.3)

    async def run(self) -> None:
        while True:
            try:
                await self._session()
                self._fail_streak += 1
                log.warning("ATV 会话结束，准备重连（连续失败 %d 次）", self._fail_streak)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fail_streak += 1
                log.warning("ATV 会话异常（连续失败 %d 次）: %s: %s",
                            self._fail_streak, type(exc).__name__, exc)

            # 连续失败到阈值才告警：单次抖动（ATV 重启、路由器抽风）不值得推送
            if self._fail_streak >= self.notifier.offline_after:
                await self.notifier.raise_once(
                    "atv_offline",
                    "🔴 Apple TV 失联",
                    f"连续 {self._fail_streak} 次连接 Apple TV 失败，"
                    f"「ATV 待机 → 关电视」已失效。\n"
                    f"配置的 ATV 地址：{self.cfg['atv']['host'] or '(自动扫描)'}\n"
                    f"处理：服务仍在每 {self.cfg['reconnect_delay']}s 重试；"
                    f"若长期不恢复，请检查 ATV 是否断电、IP 是否变化"
                    f"（建议在路由里做 DHCP 静态绑定）。",
                )
            await asyncio.sleep(self.cfg["reconnect_delay"])

    # -- 状态变化 --------------------------------------------------------- #
    def _on_power_change(self, old: PowerState, new: PowerState) -> None:
        self.state = new
        if new == PowerState.Off:
            log.info("ATV 进入待机（%s -> %s）", old, new)
            self._schedule_tv_off()
        elif new == PowerState.On:
            log.info("ATV 已唤醒（%s -> %s）", old, new)
            self._cancel_pending("ATV 被重新唤醒")

    def _schedule_tv_off(self) -> None:
        self._cancel_pending("重新计时")
        loop = self._loop or asyncio.get_event_loop()
        self._pending = loop.create_task(self._delayed_tv_off())

    def _cancel_pending(self, reason: str) -> None:
        if self._pending and not self._pending.done():
            self._pending.cancel()
            log.info("取消待执行的关电视动作（%s）", reason)
        self._pending = None

    async def _delayed_tv_off(self) -> None:
        """包一层异常兜底：任务里的异常若不处理会被静默吞掉。"""
        try:
            await self._do_delayed_tv_off()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("关电视流程出现未预期异常")

    async def _do_delayed_tv_off(self) -> None:
        try:
            await asyncio.sleep(self.cfg["delay_seconds"])
        except asyncio.CancelledError:
            return

        # 二次确认，防止延迟期间 ATV 又被唤醒
        try:
            ps = self.atv.power.power_state if self.atv else self.state
        except Exception:
            ps = self.state
        if ps != PowerState.Off:
            log.info("延迟结束后 ATV 已不是待机（%s），放弃关电视", ps)
            return

        should_close, reason = self._guard_verdict()
        if not should_close:
            log.warning("跳过关电视 —— %s", reason)
            return

        log.info("确认 ATV 仍在待机，开始关闭电视（%s）", reason)
        after = "?"
        for attempt in range(1, int(self.cfg["retries"]) + 1):
            ok, msg = await asyncio.to_thread(self.tv.turn_off)
            if ok:
                log.info("关电视：%s（第 %d 次尝试）", msg, attempt)
                await asyncio.sleep(int(self.cfg["tv"].get("verify_delay", 4)))
                after = await asyncio.to_thread(self.tv.verify)
                log.info("关电视后复查状态 = %s", after)
                await self.notifier.clear(
                    "tv_off_failed",
                    "🟢 关电视已恢复正常",
                    f"这次已正常处理（复查状态 = {after}）。",
                )
                return
            log.warning("关电视未成功（第 %d 次尝试）: %s", attempt, msg)
            if attempt < int(self.cfg["retries"]):
                await asyncio.sleep(int(self.cfg["retry_interval"]))

        log.error("关电视重试耗尽，放弃。")
        await self.notifier.raise_once(
            "tv_off_failed",
            "🔴 关电视失败",
            f"已确认 Apple TV 处于待机，但连续 {self.cfg['retries']} 次都没能关掉电视。\n"
            f"电视地址：{self.cfg['tv']['host']}:{self.cfg['tv']['port']}\n"
            f"最后一次复查状态：{after}\n"
            f"可能原因：电视 6095 接口异常 / 电视 IP 变化 / 电视卡在过渡态。\n"
            f"注意：电视很可能还亮着，需要手动关一下。",
        )


# --------------------------------------------------------------------------- #
def main() -> None:
    cfg = load_config()
    load_guard(cfg)

    # 手动测 Bark 通道：python atv_tv_power.py --test-bark
    if "--test-bark" in sys.argv:
        notifier = Notifier(cfg.get("bark", {}))
        if not notifier.enabled:
            print("Bark 未启用：请检查 config.json 的 bark.enabled 与 key / key_file")
            return
        notifier._push(
            "✅ 电视联动告警测试",
            "atv-tv-power 的 Bark 通道正常。\n"
            "以后只有两种情况会推给你：\n"
            "① 关电视连续失败　② Apple TV 失联。\n"
            f"分组：{notifier.group or '默认'}",
        )
        return

    # 现场诊断：只看不动作 —— python atv_tv_power.py --show-state
    if "--show-state" in sys.argv:
        asyncio.run(LinkService(cfg).probe_once())
        return

    service = LinkService(cfg)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    stop = asyncio.Event()

    def _shutdown(*_):
        log.info("收到退出信号，正在停止 ...")
        loop.call_soon_threadsafe(stop.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except (NotImplementedError, ValueError):
            signal.signal(sig, _shutdown)

    async def _runner():
        task = asyncio.create_task(service.run())
        await stop.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # pyatv 断开连接时会派生清理任务。给它一点时间跑完，否则 loop 关闭时
        # 会打印 "Task was destroyed but it is pending" 之类的噪音日志。
        await asyncio.sleep(0.5)

    try:
        loop.run_until_complete(_runner())
    finally:
        loop.close()
    log.info("已退出")


if __name__ == "__main__":
    main()
