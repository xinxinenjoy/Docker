"""「等导出任务」的轮询节奏 —— 不联网、不真等。

🔴 为什么单独一条（2026-10-19）：

   红领巾观察到一轮 60 次请求里 **50 多次都是 `fs_export_dir_status`**。
   原来固定 2 秒查一次，而 115 的导出是**任务级**异步动作 —— 查得再密也不会让它更早结束，
   那些请求没有一条带来新信息。

   ⇒ 改成按「已等时间」退避（`v115.export_poll_delay`）。这条用例钉三件事：

     ① 等待序列**只增不减、有上限** —— 不许下次重构又退回固定 sleep
     ② 首次等待必须小 —— 本库实测 13 秒就导完，开局就等 30 秒等于白等
     ③ 长任务 / 超时场景下轮询是**十几次**，不是几十上百次
"""
from __future__ import annotations

import io
import unittest

from app.config import Config
from app.throttle import Throttle
from app.v115 import V115, V115Error, export_poll_delay


class _Clock:
    """假时钟 —— `_sleep` 推进它，于是退避与超时判定都能在毫秒内跑完。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += float(seconds)


class _Client:
    """假 P115Client —— 只实现 `export_tree_bytes` 用到的那几个接口。

    `ready_after` = 第几次 `fs_export_dir_status` 才回「导完了」。
    """

    def __init__(self, ready_after: int) -> None:
        self.ready_after = ready_after
        self.polls = 0

    def fs_export_dir(self, payload, **kw):                    # noqa: ANN001, ANN003
        return {"state": True, "export_id": "e1"}

    def fs_export_dir_status(self, payload, **kw):             # noqa: ANN001, ANN003
        self.polls += 1
        if self.polls < self.ready_after:
            return {"state": True, "data": {"status": 1}}
        return {"state": True, "data": {"status": 2, "file_id": "f1", "pick_code": "p1"}}

    def download_url(self, fid, **kw):                         # noqa: ANN001, ANN003
        return "http://example.invalid/tree.txt"

    def fs_delete(self, ids, **kw):                            # noqa: ANN001, ANN003
        return {"state": True}

    def open(self, url):                                       # noqa: ANN001
        return io.BytesIO("根目录\n| |-影音\n".encode("utf-8"))


class TestPollDelay(unittest.TestCase):
    """退避序列本身 —— 纯函数，不涉及时钟。"""

    def test_只增不减且有上限(self):
        delays = [export_poll_delay(i) for i in range(40)]
        self.assertAlmostEqual(delays[0], 2.0)
        for a, b in zip(delays, delays[1:]):
            self.assertLessEqual(a, b, f"等待时间倒退了：{delays}")
        self.assertEqual(max(delays), 30.0, "必须封顶在 poll_max")

    def test_首次等待要小(self):
        # 本库 727 目录 / 6071 文件实测 13 秒导完（README）——
        # 开局就等 30 秒会让小库白白多等二十几秒。
        self.assertLessEqual(export_poll_delay(0), 3.0)

    def test_参数可覆盖且不会归零(self):
        self.assertAlmostEqual(export_poll_delay(0, poll=1.0, poll_max=10.0), 1.0)
        self.assertEqual(export_poll_delay(50, poll=2.0, poll_max=10.0), 10.0)
        self.assertGreaterEqual(export_poll_delay(0, poll=0.0, poll_max=10.0), 0.5)


class TestExportPolling(unittest.TestCase):
    """整条循环：`export_tree_bytes` 实际查了几次、花了多久（假时钟）。"""

    def _mk(self, ready_after: int, timeout: float = 900.0):
        # ⚠️ 这里**故意把节流抖动压成 0**（`throttle_min = throttle_max = 0`）。
        #    节流是 `random.uniform(1, 3)` —— 挂上假时钟之后，每次请求还会额外推进
        #    假时钟 1~3 秒，于是「等了多久」变成随机的 ⇒ `test_超时上限内轮询次数封顶`
        #    里那条 `clock.t <= 900` 会偶发失败（实测撞到 901.5：最后一次 sleep 刚好压在
        #    899.9，请求前的抖动把它顶过了上限）。
        #    ⛔ 别改成「上限放宽到 940」把问题糊过去 —— 那条断言要验的是
        #       「超时判定把**下一次等待**算进去了」，抖动混进来就验不准了。
        cfg = Config(strm_prefix="https://example.invalid/d/x", remote_root="",
                     throttle_min=0.0, throttle_max=0.0)
        clock = _Clock()
        throttle = Throttle(cfg=cfg, sleeper=clock.sleep, log=lambda *a, **k: None)
        client = _Client(ready_after)
        v = V115(client, cfg, throttle)
        # 注入假时钟 / 假 sleep —— 否则这条用例要真等几分钟
        v._clock = clock                      # noqa: SLF001
        v._sleep = clock.sleep                # noqa: SLF001
        return v, clock, client

    def test_小库只查一两次(self):
        v, _clock, client = self._mk(ready_after=2)
        self.assertTrue(v.export_tree_bytes())
        self.assertEqual(client.polls, 2)
        # 提交 + 2 次状态 + 取链 + 删临时件（下载走 CDN，不算接口请求）
        self.assertLessEqual(v.reqs.total, 6)

    def test_长任务不再打几十次轮询(self):
        """第 10 次查询才导完（≈200 秒）—— 旧写法要查 50 次以上。"""
        v, clock, client = self._mk(ready_after=10)
        v.export_tree_bytes()
        self.assertEqual(client.polls, 10)
        self.assertLessEqual(client.polls, 15, "长任务的轮询次数必须是个位数到十几")
        self.assertLessEqual(clock.t, 300.0)

    def test_超时上限内轮询次数封顶(self):
        """任务永远不完成 ⇒ 900 秒超时。旧写法 ≈ 450 次，现在应在 35 次以内。"""
        v, clock, client = self._mk(ready_after=10 ** 9, timeout=900.0)
        with self.assertRaises(V115Error) as cm:
            v.export_tree_bytes()
        self.assertIn("还没好", str(cm.exception))
        self.assertLessEqual(client.polls, 35)
        self.assertLessEqual(clock.t, 900.0, "超时判定必须把「下一次等待」算进去")

    def test_轮询都记进了请求流水(self):
        """省请求不能靠「不记账」—— 网页上「请求」那个数字必须还是真的。"""
        v, _clock, client = self._mk(ready_after=5)
        v.export_tree_bytes()
        by_name = {r["name"]: r["count"] for r in v.reqs.summary()["by_name"]}
        self.assertEqual(by_name.get("fs_export_dir_status"), client.polls)


if __name__ == "__main__":
    unittest.main()
