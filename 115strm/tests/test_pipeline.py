"""执行入口的**前置守卫**与「这一轮做什么」的回归 —— 不联网、不写盘。

🔴 为什么单独开一个文件：`run_once` 是 web / cli **唯一共用**的执行入口，
   「strm 前缀空着就不许开跑」这条规则落在它身上（2026-10-08 红领巾要求
   「首次配置时为空…存储在 data 目录下」之后新增的）。

   它一旦松掉，症状是 **「跑完了、写了一堆文件、但一个都播不了」** ——
   最难看出来的那种失败：日志一片正常、回执也说成功。

⭐ 2026-10-08 晚：`events` / `api` / `tree` 三个模式全删，`run_once` 不再收 `mode`。
   原来那条「未知模式先于前缀被拒」的用例随之删掉（没有模式可传了），
   换成一条**反过来**的用例：谁把 `mode` 加回来，就让它报错。
"""
from __future__ import annotations

import unittest

from app.config import Config
from app.pipeline import SYNC_STEPS, run_once


class TestRunGuard(unittest.TestCase):
    def _log(self) -> tuple[list, object]:
        lines: list = []

        def log(level: str, msg: str, *a, **k) -> None:      # noqa: ANN002, ANN003
            lines.append((level, msg))

        return lines, log

    def test_前缀空时开跑前就拦住(self):
        lines, log = self._log()
        with self.assertRaises(ValueError) as cm:
            run_once(Config(strm_prefix=""), log)
        msg = str(cm.exception)
        self.assertIn("前缀", msg)
        self.assertIn("播放地址", msg)         # 必须直接告诉「去哪填」，不能只说「配错了」
        self.assertEqual(lines, [], "守卫必须在**任何动作之前** —— 连一行日志都不该产生")

    def test_纯空白也算空(self):
        _lines, log = self._log()
        with self.assertRaises(ValueError):
            run_once(Config(strm_prefix="   "), log)

    def test_不再接受mode参数(self):
        """⛔ 防回归：模式已经删了，谁再把 `mode` 加回来就该当场炸。"""
        _lines, log = self._log()
        with self.assertRaises(TypeError):
            run_once(Config(strm_prefix=""), log, mode="auto")   # type: ignore[call-arg]


class TestSyncSteps(unittest.TestCase):
    """界面上那张「这一轮会做什么」取自 `SYNC_STEPS` —— 它是**单一事实源**。"""

    def test_步骤键与顺序(self):
        keys = [k for k, _t, _h in SYNC_STEPS]
        self.assertEqual(keys, ["export", "parse", "diff", "write", "tidy"])

    def test_每步都有标题和说明(self):
        for k, title, help_ in SYNC_STEPS:
            self.assertTrue(title.strip(), f"{k} 没有标题")
            self.assertTrue(help_.strip(), f"{k} 没有说明")

    def test_文案讲清了代价(self):
        """必须讲清「只写差异」—— 否则会让人以为每轮都在全库重写。"""
        titles = " ".join(t for _k, t, _h in SYNC_STEPS)
        self.assertIn("只写差异", titles)


if __name__ == "__main__":
    unittest.main()
