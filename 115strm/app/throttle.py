"""节流器 —— 「慢速」的执行层保障。**独立复制自 115organize**（两个镜像各带一份，不跨项目 import）。

两级 + 两个兜底：
    ① 请求级：每个请求之间 sleep `[MIN, MAX]` 随机秒 —— 随机抖动，固定节奏更像脚本
    ② 批次级：每 `BATCH` 个请求长休 `REST` 秒
    ③ 时间窗：设了 `WINDOW` 就只在窗内发请求，窗外**就地等**
    ④ 连续失败：到 `FAIL_LIMIT` 次就停手冷却 `COOLDOWN` 秒
       （风控与网络抖动症状相同，宁可按风控处理）

⚠️ `sleeper` / `clock` 可注入 —— 否则单测要真等十几分钟，等于没法测。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Config

__all__ = ["Throttle", "ThrottleStats", "StopRequested"]


class StopRequested(RuntimeError):
    """连续失败超过上限 —— 主动停手，等下一轮。"""


@dataclass
class ThrottleStats:
    requests: int = 0
    sleeps: int = 0
    slept_seconds: float = 0.0
    rests: int = 0
    window_waits: int = 0
    failures: int = 0
    streak: int = 0
    cooldowns: int = 0

    def as_dict(self) -> dict:
        d = vars(self).copy()
        d["slept_seconds"] = round(d["slept_seconds"], 1)
        return d


def _parse_window(text: str) -> tuple[int, int] | None:
    """`02:00-06:00` → (120, 360) 分钟。空/格式不对 → None（= 不限时段）。"""
    t = (text or "").strip()
    if "-" not in t:
        return None
    a, _, b = t.partition("-")
    try:
        ah, am = (int(x) for x in a.strip().split(":"))
        bh, bm = (int(x) for x in b.strip().split(":"))
    except ValueError:
        return None
    return (ah * 60 + am) % 1440, (bh * 60 + bm) % 1440


@dataclass
class Throttle:
    cfg: Config
    clock: object = time.monotonic          # 注入点：单测用假时钟
    wall: object = datetime.now            # 注入点：时间窗判定走本地时间
    sleeper: object = time.sleep            # 注入点：单测用假 sleep
    log: object = None
    stats: ThrottleStats = field(default_factory=ThrottleStats)
    on_stop: object = None
    _since_rest: int = 0
    _window: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        self._window = _parse_window(self.cfg.window)
        if self.log is None:
            self.log = lambda *a, **k: None

    # ------------------------------------------------------------------ 基础
    def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self.stats.sleeps += 1
        self.stats.slept_seconds += seconds
        self.sleeper(seconds)

    def _in_window(self) -> bool:
        if not self._window:
            return True
        lo, hi = self._window
        now = self.wall()
        cur = now.hour * 60 + now.minute
        if lo <= hi:
            return lo <= cur < hi
        return cur >= lo or cur < hi          # 跨零点，如 23:00-06:00

    def _seconds_to_window(self) -> float:
        lo, _hi = self._window or (0, 0)
        now = self.wall()
        cur = now.hour * 60 + now.minute
        delta = (lo - cur) % 1440
        return max(30.0, delta * 60.0 - now.second)

    def wait_window(self) -> None:
        """不在时间窗内就等到进窗。⚠️ 长等待分批 sleep（每次 ≤10 分钟），
        这样 Ctrl-C / 容器停止信号能及时响应。"""
        while not self._in_window():
            wait = self._seconds_to_window()
            self.stats.window_waits += 1
            self.log("info", f"不在运行时段 {self.cfg.window}，等待 {wait/60:.0f} 分钟")
            while wait > 0:
                step = min(wait, 600.0)
                self._sleep(step)
                wait -= step

    # ------------------------------------------------------------------ 主流程
    def before_request(self) -> None:
        """**每个** 115 请求前调一次。"""
        self.wait_window()
        if self._since_rest:
            self._sleep(random.uniform(self.cfg.throttle_min, self.cfg.throttle_max))
        self._since_rest += 1
        if self._since_rest >= max(1, self.cfg.throttle_batch):
            rest = self.cfg.throttle_rest
            self.stats.rests += 1
            self.log("info", f"已连续 {self._since_rest} 次操作，按档位长休 {rest:.0f}s")
            self._sleep(rest)
            self._since_rest = 0
        self.stats.requests += 1

    def ok(self) -> None:
        self.stats.streak = 0

    def fail(self, err: object = "") -> None:
        self.stats.failures += 1
        self.stats.streak += 1
        self.log("warning", f"第 {self.stats.streak} 次连续失败：{err}")
        if self.stats.streak >= max(1, self.cfg.fail_limit):
            cd = self.cfg.fail_cooldown
            self.stats.cooldowns += 1
            self.log("error", f"连续失败 {self.stats.streak} 次（疑似风控），冷却 {cd:.0f}s")
            self._sleep(cd)
            self.stats.streak = 0
            if self.on_stop:
                self.on_stop()

    # ------------------------------------------------------------------ 估算
    def estimate(self, count: int) -> dict:
        """粗估 `count` 次请求要多久 —— 让他心里有数。"""
        avg = (self.cfg.throttle_min + self.cfg.throttle_max) / 2
        rests = count // max(1, self.cfg.throttle_batch)
        total = count * avg + rests * self.cfg.throttle_rest
        return {
            "requests": count,
            "avg_gap": round(avg, 1),
            "rests": rests,
            "seconds": round(total, 0),
            "human": str(timedelta(seconds=int(total))),
        }
