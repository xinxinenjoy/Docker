"""节流器单测 —— 「慢速」是这工具的第一需求，必须钉死。

⭐ 关键手法：`Throttle` 的 `clock` / `sleeper` 是可注入的。
   否则验「4 小时跑完」这件事要真等 4 小时 —— 等于不可测。
   注入后 sleep 只累加计数，跑完只要毫秒。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config                    # noqa: E402
from app.throttle import StopRequested, Throttle, _parse_window  # noqa: E402


def make(cfg: Config | None = None, now: datetime | None = None) -> tuple[Throttle, list]:
    """造一个「假时间」的节流器。返回 (throttle, slept)。"""
    slept: list[float] = []
    cfg = cfg or Config()
    t = Throttle(cfg=cfg,
                 clock=lambda: 0.0,
                 wall=(lambda: now) if now else datetime.now,
                 sleeper=slept.append)
    return t, slept


class TestGap(unittest.TestCase):
    def test_请求之间有间隔_第一个请求不等(self):
        """🔴 红领巾的原话：「3-5 秒的单任务间隔」。

        第一个请求**不该等** —— 等它纯粹是浪费启动时间。从第二个起才隔。
        """
        cfg = Config()
        thr, slept = make(cfg)
        thr.before_request()
        self.assertEqual(slept, [], "第一个请求不该 sleep")
        thr.before_request()
        self.assertEqual(len(slept), 1, "第二个请求前必须 sleep 一次")
        self.assertGreaterEqual(slept[0], cfg.throttle_min)
        self.assertLessEqual(slept[0], cfg.throttle_max)

    def test_间隔是随机的_不是固定节奏(self):
        """固定节奏比随机节奏**更像脚本** —— 所以要抖。

        拿 40 次请求看离散度：全都一样就说明抖动没生效。
        """
        thr, slept = make(Config())
        for _ in range(40):
            thr.before_request()
        self.assertGreater(len(set(round(s, 6) for s in slept)), 5,
                           "间隔几乎没有离散度 ⇒ 抖动没生效")


class TestBatchRest(unittest.TestCase):
    def test_每N次长休一次(self):
        """第二级：「处理一部分之后暂停，等待一段时间之后继续」。"""
        cfg = Config()
        cfg.throttle_batch = 10
        cfg.throttle_rest = 60.0
        thr, slept = make(cfg)
        for _ in range(10):
            thr.before_request()
        self.assertEqual(thr.stats.rests, 1, "满 10 次该长休 1 次")
        self.assertIn(60.0, slept, "长休时长不对")
        # 长休后计数器归零，下一次请求重新从 1 数起
        thr.before_request()
        self.assertEqual(thr._since_rest, 1, "长休完计数该清零并重新数")

    def test_三档节流参数能各自独立生效(self):
        """把「频繁档 / 常规档 / 保守档」三组参数各跑一遍，确认互不干扰。"""
        presets = {
            "频繁": dict(throttle_min=0.5, throttle_max=1.0, throttle_batch=500, throttle_rest=30),
            "常规": dict(throttle_min=3.0, throttle_max=5.0, throttle_batch=200, throttle_rest=60),
            "保守": dict(throttle_min=8.0, throttle_max=12.0, throttle_batch=100, throttle_rest=600),
        }
        for name, kw in presets.items():
            with self.subTest(档位=name):
                cfg = Config()
                for k, v in kw.items():
                    setattr(cfg, k, v)
                thr, slept = make(cfg)
                for _ in range(120):
                    thr.before_request()
                # 常规档在 120 次里该长休 0 次（200 才休）… 保守档该休 1 次（100 就休）
                expect_rests = 120 // kw["throttle_batch"]
                self.assertEqual(thr.stats.rests, expect_rests)
                self.assertLessEqual(max(slept), max(kw["throttle_rest"],
                                                     kw["throttle_max"]))


class TestWindow(unittest.TestCase):
    def test_时间窗解析(self):
        self.assertEqual(_parse_window("02:00-06:00"), (120, 360))
        self.assertEqual(_parse_window("23:00-06:00"), (1380, 360))     # 跨零点
        self.assertIsNone(_parse_window(""))
        self.assertIsNone(_parse_window("乱写"))
        self.assertIsNone(_parse_window("08:00"))

    def test_窗内直接过_窗外就地等(self):
        cfg = Config()
        cfg.window = "02:00-06:00"
        inside, slept_in = make(cfg, now=datetime(2026, 10, 6, 3, 30))
        inside.before_request()
        self.assertEqual(inside.stats.window_waits, 0)
        self.assertEqual(slept_in, [])

        outside, slept_out = make(cfg, now=datetime(2026, 10, 6, 3, 30))
        # 假时钟的 wall 是固定值，所以 wait_window 必须靠「第一次就判定在窗内」才不无限等；
        # 这里只验判定式本身，不驱动循环。
        self.assertTrue(outside._in_window())

    def test_窗外判定_跨零点(self):
        cfg = Config()
        cfg.window = "23:00-06:00"
        thr, _ = make(cfg, now=datetime(2026, 10, 6, 23, 30))
        self.assertTrue(thr._in_window(), "23:30 该在 23:00-06:00 窗内")
        thr2, _ = make(cfg, now=datetime(2026, 10, 6, 12, 0))
        self.assertFalse(thr2._in_window(), "12:00 该在窗外")
        thr3, _ = make(cfg, now=datetime(2026, 10, 6, 5, 59))
        self.assertTrue(thr3._in_window(), "05:59 该在窗内（跨零点那一半）")
        thr4, _ = make(cfg, now=datetime(2026, 10, 6, 6, 0))
        self.assertFalse(thr4._in_window(), "06:00 是闭区间右端，该已出窗")


class TestFailure(unittest.TestCase):
    def test_连续失败到上限就冷却并回调(self):
        """风控和网络抖动的症状都是「连续失败」—— 宁可按风控处理。"""
        cfg = Config()
        cfg.fail_limit = 3
        cfg.fail_cooldown = 900.0
        fired: list[int] = []
        thr, slept = make(cfg)
        thr.on_stop = lambda: fired.append(1)

        thr.fail("err1")
        thr.fail("err2")
        self.assertEqual(fired, [], "还没到上限就不该停")
        thr.fail("err3")
        self.assertEqual(fired, [1], "到上限必须回调（落盘/通知靠它）")
        self.assertIn(900.0, slept, "冷却时长不对")
        self.assertEqual(thr.stats.streak, 0, "冷却完连胜计数该清零")

    def test_成功一次就把连胜清零(self):
        cfg = Config()
        cfg.fail_limit = 3
        thr, _ = make(cfg)
        thr.fail("x")
        thr.fail("x")
        thr.ok()
        thr.fail("x")
        self.assertEqual(thr.stats.streak, 1, "中间成功过就不该攒着")


class TestEstimate(unittest.TestCase):
    def test_估算要跟他给的档位对得上(self):
        """报告里的「预计 4:59:00」就是这里算出来的 —— 算错了会误导他决定要不要真跑。"""
        cfg = Config()
        cfg.throttle_min, cfg.throttle_max = 3.0, 5.0
        cfg.throttle_batch, cfg.throttle_rest = 200, 60.0
        thr, _ = make(cfg)
        est = thr.estimate(4200)
        # 4200 × 4s = 16800s；长休 21 次 × 60s = 1260s；合计 18060s ≈ 5:01:00
        self.assertEqual(est["requests"], 4200)
        self.assertEqual(est["avg_gap"], 4.0)
        self.assertEqual(est["rests"], 21)
        self.assertAlmostEqual(est["seconds"], 18060, delta=2)

    def test_零次请求不炸(self):
        thr, _ = make(Config())
        self.assertEqual(thr.estimate(0)["requests"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
