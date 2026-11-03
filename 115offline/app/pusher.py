# -*- coding: utf-8 -*-
"""分批推送的纯逻辑 —— **不依赖 fastapi / p115client**，便于脱离框架做回归测试。

红领巾 2026-11-03 的原话：「115 对于一次可离线的数量有限制，如果我给你的地址过多，
可以内部分批推送，注意不要过快避免风控」。

所以这里的口径是：
  · 115 一条请求最多认 100 个 `url[i]`（超了它直接回「一次最多 100 条」）——
    `MAX_PER_REQUEST` 是**它的**硬限，不是我们挑的；
  · 超出的部分由**后端**自己切成多批（不是让用户在界面上手动分批）；
  · 批与批之间必须 sleep（默认 3 秒），别连着怼 —— 这才是躲风控的地方；
  · 遇到「配额不足 / 风控限流 / 登录失效」这类**再推也不会成**的错误要立刻停，
    别把剩下的几千条白推一遍（还会把风控越推越紧）。

本模块只管「切批 + 记进度 + 认错误类型」，真正的 115 调用在 server.py。
"""
from __future__ import annotations

import threading
import time
import uuid

# 115 单条请求的硬上限（是它定的，见 `clouddownload_task_add_urls`）
MAX_PER_REQUEST = 100
# 默认值：一批 100 条、批间隔 3 秒（1644 条 ≈ 17 批 ≈ 48 秒推完）
DEFAULT_CHUNK = 100
DEFAULT_DELAY = 3.0
# 一次提交的总上限（按默认节奏 ≈ 100 秒跑完；再多请分两次，别一挂半小时）
MAX_TOTAL = 3000
# 被限流时的退避秒数（只重试一次，再失败就停）
THROTTLE_BACKOFF = 8.0

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"        # 全部批次都提交成功
STATE_STOPPED = "stopped"  # 中途遇到致命错误，主动停（剩下的没推）
STATE_ERROR = "error"      # 连第一批都没发出去（账号/cookie 层面就坏了）

# ⚠️ 顺序有意义：**先判限流**。因为「频繁」「风控」这两个词也常出现在配额类报错里，
#    而限流的处置是「退避重试一次」，配额不足的处置是「直接停」—— 别搞反。
THROTTLE_HINTS = ("频繁", "限流", "风控", "速率", "太快", "稍后再试", "稍后重试",
                  "服务器繁忙", "too many", "rate limit", "429")
FATAL_HINTS = ("配额", "quota", "不足", "超出", "超额", "登录", "未登录", "重新登录",
               "失效", "过期", "禁止", "封禁", "无权", "too many", "403")


def clamp_chunk(value) -> int:
    """批大小收敛到 1..MAX_PER_REQUEST —— 允许调小，但不许超过 115 的硬限。

    ⚠️ ≤0 一律回落到默认 100：`PUSH_CHUNK=0` 若被当成 1，1644 条会变成 1644 个请求，
       那不是「慢一点」，而是直接把账号推到风控脸上。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_CHUNK
    if n <= 0:
        return DEFAULT_CHUNK
    return min(n, MAX_PER_REQUEST)


def clamp_delay(value) -> float:
    """批间隔收敛到 0..60 秒（0 = 不等待，只在自测里用）。"""
    try:
        sec = float(value)
    except (TypeError, ValueError):
        return DEFAULT_DELAY
    return max(0.0, min(sec, 60.0))


def split_batches(urls, chunk: int = DEFAULT_CHUNK) -> list[list[str]]:
    """保序切批；空串丢掉，不会产生空批。"""
    size = clamp_chunk(chunk)
    items = [u for u in (urls or []) if u]
    return [items[i: i + size] for i in range(0, len(items), size)]


def classify_error(text) -> str:
    """把 115 的报错文案归类：`throttle` / `fatal` / `batch`。

    · `throttle` ⇒ 被限流：退避一次再试，还失败就当致命处理（别继续怼）
    · `fatal`    ⇒ 配额 / 登录 / 权限：**立刻停**，剩下的不用推了
    · `batch`    ⇒ 只坏这一批（单条链接无效之类），继续推下一批
    """
    t = str(text or "").lower()
    if not t:
        return "batch"
    if any(h.lower() in t for h in THROTTLE_HINTS):
        return "throttle"
    if any(h.lower() in t for h in FATAL_HINTS):
        return "fatal"
    return "batch"


def _hm(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else ""


class PushJob:
    """一次分批推送任务的进度（线程安全）。前端拿 `snapshot()` 画进度条。"""

    def __init__(self, *, account_id: str, account: str = "", dir_id: str = "",
                 dir_name: str = "", batches=None, chunk: int = DEFAULT_CHUNK,
                 delay: float = DEFAULT_DELAY, skipped: int = 0,
                 job_id: str = "", sources: dict | None = None) -> None:
        self.id = job_id or uuid.uuid4().hex[:10]
        self.account_id = str(account_id or "")
        self.account = str(account or "")
        self.dir_id = str(dir_id or "")
        self.dir_name = str(dir_name or "")
        self.chunk = clamp_chunk(chunk)
        self.delay = clamp_delay(delay)
        self.skipped = max(0, int(skipped or 0))
        self.batches = [list(b) for b in (batches or [])]
        self.total = sum(len(b) for b in self.batches)
        self.sources = dict(sources or {})   # 小写 url → 来源（链接 / 佛曰 …），记账用

        self.state = STATE_QUEUED
        self.pushed = 0
        self._done = 0            # 已处理的批数（成败都算）
        self.failed: list[dict] = []
        self.message = ""
        self.quota_remain = None
        self.quota_total = None
        self.created = time.time()
        self.started = 0.0
        self.finished = 0.0
        self.updated = self.created
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- 执行侧
    def start(self) -> None:
        with self._lock:
            self.state = STATE_RUNNING
            self.started = time.time()
            self.updated = self.started

    def note_quota(self, remain, total) -> None:
        """顺手把 115 的离线剩余次数记下来（拿不到就算了，不阻断推送）。"""
        with self._lock:
            try:
                self.quota_remain = None if remain is None else int(remain)
            except (TypeError, ValueError):
                self.quota_remain = None
            try:
                self.quota_total = None if total is None else int(total)
            except (TypeError, ValueError):
                self.quota_total = None

    def batch_ok(self, count: int) -> None:
        with self._lock:
            self.pushed += int(count)
            self.updated = time.time()

    def batch_failed(self, index: int, count: int, error: str) -> None:
        """记一批失败（`index` 从 0 起）。是否继续由调用方决定。"""
        with self._lock:
            self.failed.append({
                "batch": index + 1, "count": int(count), "error": str(error or ""),
            })
            self.updated = time.time()

    def finish(self, state: str, message: str = "") -> None:
        with self._lock:
            self.state = state
            self.message = str(message or self.message or "")
            self.finished = time.time()
            self.updated = self.finished

    # ---------------------------------------------------------------- 展示侧
    @property
    def batches_done(self) -> int:
        with self._lock:
            return min(len(self.batches), self._done)

    def tick(self) -> None:
        """每处理完一批（无论成败）调一次，进度条靠它往前走。"""
        with self._lock:
            self._done = min(len(self.batches), self._done + 1)
            self.updated = time.time()

    def summary(self) -> str:
        """一句人话 —— 直接给界面 / 推送账本用。"""
        with self._lock:
            n = len(self.batches)
            if self.state == STATE_ERROR:
                return self.message or "推送没能开始"
            if self.state == STATE_QUEUED:
                return f"排队中：共 {self.total} 条 / {n} 批"
            if not self.failed:
                return f"已推送 {self.pushed} 条（{n} 批，每批 {self.chunk} 条）"
            first = self.failed[0]
            head = f"第 {first['batch']}/{n} 批失败：{first['error'] or '未知错误'}"
            # 「没推」= 总数 - 已推（失败那批也算没推出去，别把它从缺口里扣掉 ——
            #  扣掉就会出现「已推 0 条，剩 0 条未推」这种自相矛盾的话，2026-11-03 冒烟时踩到）
            left = max(0, self.total - self.pushed)
            if self.state == STATE_STOPPED:
                return f"{head} —— 已停：已推 {self.pushed} 条，还有 {left} 条没推"
            return (f"{head} —— 已推 {self.pushed} 条，"
                    f"{sum(f['count'] for f in self.failed)} 条失败，其余批次已提交")

    def snapshot(self) -> dict:
        with self._lock:
            n = len(self.batches)
            done = min(n, self._done)
            now = time.time()
            elapsed = round((self.finished or now) - self.started, 1) if self.started else 0.0
            if self.state == STATE_RUNNING and done and self.started:
                # 按「已跑批次的平均耗时 + 批间隔」外推；首批还没跑完就不瞎猜
                per = (now - self.started) / done
                eta = round((n - done) * (per + self.delay))
            else:
                eta = 0
            return {
                "id": self.id,
                "state": self.state,
                "done": self.state in (STATE_DONE, STATE_STOPPED, STATE_ERROR),
                "ok": self.state == STATE_DONE and not self.failed,
                "account_id": self.account_id,
                "account": self.account,
                "dir_id": self.dir_id,
                "dir_name": self.dir_name,
                "total": self.total,
                "pushed": self.pushed,
                "failed_count": sum(f["count"] for f in self.failed),
                "failed": list(self.failed),
                "skipped": self.skipped,
                "chunk": self.chunk,
                "delay": self.delay,
                "batches": n,
                "batch": min(done + 1, n) if self.state == STATE_RUNNING and n else done,
                "batches_done": done,
                "percent": (100 if not n else round(done * 100 / n)),
                "elapsed": elapsed,
                "eta": int(eta),
                "quota_remain": self.quota_remain,
                "quota_total": self.quota_total,
                "message": self.message,
                "summary": self.summary(),
                "created": _hm(self.created),
                "updated": _hm(self.updated),
            }
