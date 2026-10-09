"""定时器（`schedule_cron`）的回归 —— cron 解析、到点判定、**不许提前触发**。

🔴 本文件的存在理由就是 v1.1 那个 bug：设置填 `0 6 * * *`（每天 06:00），
   实际**每小时跑一次**（实测周期恒为 `61m01s`）。病根 = 把「睡满一段」当成了到点：

       if wait > 0 and stop.wait(min(wait, 3600)):   # 超时返回 False ⇒ 没重算就往下走
           continue                                 # ⇒ 每睡满 1 小时误触发一次

   ⇒ 这里钉的不是「cron 算得对不对」，而是**「没到点就绝不允许触发」**这条不变量。
   全程离线（假时钟 / 假 Event / 假 runner），不连网、不睡觉（最长 0.5 秒）。
"""
from __future__ import annotations

import importlib
import threading
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from app import web

try:
    from fastapi.testclient import TestClient
except Exception:                                    # pragma: no cover
    TestClient = None


# --------------------------------------------------------------------------- 假件
class FakeClock:
    """可推进的假时钟 —— `_wait_due` 的 `now` 是可注入的，就是为这个留的口子。"""

    def __init__(self, start: datetime):
        self.t = start

    def now(self) -> datetime:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += timedelta(seconds=sec)


class FakeStop:
    """假的 `threading.Event`：只记「被睡了几次」，不真的睡；可顺带推进假时钟。"""

    def __init__(self, clock: FakeClock | None = None, stop_after: int | None = None):
        self.clock = clock
        self.stop_after = stop_after
        self.calls: list[float] = []

    def is_set(self) -> bool:
        return self.stop_after is not None and len(self.calls) >= self.stop_after

    def wait(self, sec: float) -> bool:
        self.calls.append(sec)
        if self.stop_after is not None and len(self.calls) >= self.stop_after:
            return True                              # 停止信号：睡满 N 次后置位
        if self.clock:
            self.clock.advance(sec)
        return False


class TestCronNext(unittest.TestCase):
    """`_cron_next` —— 5 段 cron 的解析与「下一次」计算。"""

    def test_今天还没到点_就报今天(self):
        self.assertEqual(web._cron_next("0 6 * * *", now=datetime(2026, 10, 9, 5, 0)),
                         datetime(2026, 10, 9, 6, 0))

    def test_今天已过点_就报明天(self):
        self.assertEqual(web._cron_next("0 6 * * *", now=datetime(2026, 10, 9, 8, 0)),
                         datetime(2026, 10, 10, 6, 0))

    def test_正好卡在那一分钟也算已过(self):
        """⚠️ 实现是「从 now+1min 起找」⇒ 06:00:00.x 重算会得到**明天**。

        这不是缺陷 —— 但**调度器不能到点后立刻重算**（会算成明天、这一轮永远不触发），
        所以 `_wait_due` 必须持住 `nxt` 判 `left <= 0`。见 `test_调度线程…`。
        """
        self.assertEqual(web._cron_next("0 6 * * *", now=datetime(2026, 10, 9, 6, 0)),
                         datetime(2026, 10, 10, 6, 0))

    def test_斜杠步长(self):
        self.assertEqual(web._cron_next("*/15 * * * *", now=datetime(2026, 10, 9, 8, 7)),
                         datetime(2026, 10, 9, 8, 15))

    def test_星期几(self):
        # 2026-10-09 是周五 ⇒ 下一个「周一（0）06:00」= 10-12
        self.assertEqual(web._cron_next("0 6 * * 0", now=datetime(2026, 10, 9, 8, 0)),
                         datetime(2026, 10, 12, 6, 0))

    def test_不合法一律回None(self):
        for bad in ("", "0 6 * *", "0 6 * * * *", "61 6 * * *", "0 24 * * *",
                    "0 6 * * 9", "0 0 32 * *", "不是cron"):
            with self.subTest(bad=bad):
                self.assertIsNone(web._cron_next(bad, now=datetime(2026, 10, 9, 8, 0)))

    def test_说人话的格式化(self):
        now = datetime(2026, 10, 9, 8, 0)
        self.assertEqual(web._fmt_next(datetime(2026, 10, 9, 20, 0), now=now),
                         "今天 20:00（还有 12 小时 0 分）")
        self.assertEqual(web._fmt_next(datetime(2026, 10, 10, 6, 0), now=now),
                         "明天 06:00（还有 22 小时 0 分）")
        self.assertEqual(web._fmt_next(datetime(2026, 10, 12, 6, 0), now=now),
                         "10-12 06:00（还有 70 小时 0 分）")
        self.assertEqual(web._fmt_next(datetime(2026, 10, 9, 8, 30), now=now),
                         "今天 08:30（还有 30 分钟）")


class TestWaitDue(unittest.TestCase):
    """`_wait_due` —— 分段睡，但**只有到点才 fire**。"""

    def test_到点那一刻才fire(self):
        clock = FakeClock(datetime(2026, 10, 9, 5, 0))
        stop = FakeStop(clock)
        v = web._wait_due(datetime(2026, 10, 9, 6, 0), "0 6 * * *", stop,
                          read_cron=lambda: "0 6 * * *", now=clock.now, seg=30.0)
        self.assertEqual(v, "fire")
        self.assertEqual(clock.t, datetime(2026, 10, 9, 6, 0))
        self.assertEqual(len(stop.calls), 120)       # 3600 秒 / 30 秒

    def test_离下一轮23小时_中途一次都不许fire(self):
        """🔴 定点回归 —— 段长直接用**老代码那个 1 小时**，原样复现老 bug 的场景。

        老实现：每睡满 3600 秒就 `wait()` 超时 ⇒ 直接开跑 ⇒ 23 小时里误触发 23 次。
        正确实现：醒来重算，`left` 还 > 0 就接着睡 ⇒ **只在 `nxt` 那一刻**返回 `fire`。
        """
        clock = FakeClock(datetime(2026, 10, 9, 7, 0))
        stop = FakeStop(clock)
        nxt = datetime(2026, 10, 10, 6, 0)           # 23 小时后
        v = web._wait_due(nxt, "0 6 * * *", stop,
                          read_cron=lambda: "0 6 * * *", now=clock.now, seg=3600.0)
        self.assertEqual(v, "fire")
        self.assertEqual(clock.t, nxt, "必须精确在到点那一刻 —— 早了就是误触发")
        self.assertEqual(len(stop.calls), 23, "23 段里一次都不许提前开跑")

    def test_配置改了要回去重算(self):
        """网页上改了 cron ⇒ 不能拿旧目标死等（下一段醒来就该感知到）。"""
        clock = FakeClock(datetime(2026, 10, 9, 7, 0))
        stop = FakeStop(clock)
        calls = {"n": 0}

        def read_cron() -> str:
            calls["n"] += 1
            return "0 6 * * *" if calls["n"] < 3 else "0 3 * * *"

        v = web._wait_due(datetime(2026, 10, 10, 6, 0), "0 6 * * *", stop,
                          read_cron=read_cron, now=clock.now, seg=30.0)
        self.assertEqual(v, "reconfig")
        self.assertLessEqual(len(stop.calls), 3, "不该拿旧目标死等")

    def test_停止信号优先(self):
        clock = FakeClock(datetime(2026, 10, 9, 7, 0))
        stop = FakeStop(clock, stop_after=2)
        v = web._wait_due(datetime(2026, 10, 10, 6, 0), "0 6 * * *", stop,
                          read_cron=lambda: "0 6 * * *", now=clock.now, seg=30.0)
        self.assertEqual(v, "cancel")


class TestSchedulerThread(unittest.TestCase):
    """`start_scheduler()` 的**整条线程** —— 真正跑在线上的是它，必须单独钉。"""

    def setUp(self):
        self._backup = {k: getattr(web, k) for k in
                        ("_SCHED_SEG", "_cron_next", "load", "_runner")}
        self._stop = web._SCHED["stop"]
        web._SCHED["stop"] = threading.Event()
        self.addCleanup(self._restore)

    def _restore(self):
        web._SCHED["stop"].set()
        t = web._SCHED.get("thread")
        if t and t.is_alive():
            t.join(timeout=5)
        for k, v in self._backup.items():
            setattr(web, k, v)
        web._SCHED["stop"] = self._stop
        web._SCHED["thread"] = None

    def test_没到点绝不触发(self):
        """🔴 定点回归（整条线程版）：把段长压成 50 毫秒 = 老代码「睡满 1 小时」的等价物。

        老实现：每 50 毫秒误触发一次 ⇒ 0.5 秒内能触发好几次 ⇒ 本用例红。
        正确实现：目标在 23 小时后，0.5 秒内**一次都不该触发**。
        """
        fired: list = []

        class FakeLog:
            def add(self, *a, **kw):
                fired.append(("log", a))

        class FakeRunner:
            status = {"running": False}
            log = FakeLog()

            def start(self, cfg=None, **kw):
                fired.append(("start", None))
                return {}

        fake = FakeRunner()
        web._SCHED_SEG = 0.05
        web._cron_next = lambda cron, now=None: datetime.now() + timedelta(hours=23)
        web.load = lambda: SimpleNamespace(schedule_cron="0 6 * * *")
        web._runner = lambda: fake

        web.start_scheduler()
        time.sleep(0.5)
        self.assertEqual(fired, [], f"没到点却触发了：{fired}")

    def test_到点会触发一轮(self):
        """另一半保险：别把 bug 修成「永远不触发」。"""
        fired: list = []

        class FakeLog:
            def add(self, *a, **kw):
                fired.append(("log", a))

        class FakeRunner:
            status = {"running": False}
            log = FakeLog()

            def start(self, cfg=None, **kw):
                fired.append(("start", None))
                return {}

        web._SCHED_SEG = 0.05
        web._cron_next = lambda cron, now=None: datetime.now() + timedelta(seconds=0.15)
        web.load = lambda: SimpleNamespace(schedule_cron="0 6 * * *")
        web._runner = lambda: FakeRunner()

        web.start_scheduler()
        deadline = time.time() + 3
        while not any(k == "start" for k, _ in fired) and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(any(k == "start" for k, _ in fired), "到点了却没触发 —— 修反了")


@unittest.skipIf(TestClient is None, "没装 fastapi/httpx")
class TestScheduleNextApi(unittest.TestCase):
    """`/api/schedule/next` —— 前端「下次运行」那一行吃的是它（钉住契约）。"""

    def setUp(self):
        importlib.reload(web)                        # 别吃别的用例留下的补丁
        self.client = TestClient(web.app)

    def test_留空_说不自动跑(self):
        d = self.client.get("/api/schedule/next", params={"cron": ""}).json()
        self.assertFalse(d["ok"])
        self.assertIn("不自动跑", d["text"])

    def test_非法_cron_给可读提示(self):
        d = self.client.get("/api/schedule/next", params={"cron": "0 6 * *"}).json()
        self.assertFalse(d["ok"])
        self.assertIn("5 段", d["text"])

    def test_合法_给出下次时刻(self):
        d = self.client.get("/api/schedule/next", params={"cron": "0 6 * * *"}).json()
        self.assertTrue(d["ok"])
        self.assertIn("06:00", d["text"])
        self.assertIn("还有", d["text"])
        # 「下次时刻」必须是**未来**的、且分钟为 0（cron = 0 6 * * *）
        nxt = datetime.strptime(d["next"], "%Y-%m-%d %H:%M")
        self.assertGreater(nxt, datetime.now())
        self.assertEqual((nxt.hour, nxt.minute), (6, 0))


if __name__ == "__main__":                           # pragma: no cover
    unittest.main()
