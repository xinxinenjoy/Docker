"""请求流水与耗时口径的回归 —— 不联网、不写盘。

🔴 为什么单独开一个文件（2026-10-13）：

   红领巾看网页时两个诉求，**各自都对应一个会静默骗人的坑**：

     · 「耗时只显示 0」—— 病根是 `_human()` 里的 `int()` 截断：
       一轮全部跳过的同步实测只花 1 秒，`int(0.98)` = 0 ⇒ 显示「0 秒」。
       **它不会报错，只是把「跑完了」和「根本没跑」显示成同一个东西。**

     · 「请求 60」看不出做了什么 —— 光有计数时，「导树轮询 60 次」（正常）
       和「逐层扫了 60 个目录」（接口选错，正是本项目从 `api` 模式改掉的原因）
       **数字上一模一样**。

   ⇒ 这两条都得有用例钉住，否则下次重构又会悄悄退回去。
"""
from __future__ import annotations

import unittest

from app.reqlog import RequestLog
from app.sync import _human


class TestHuman(unittest.TestCase):
    """`_human()` 的精度 —— ⛔ 别再退回 `int(seconds)`。"""

    def test_不足一秒给毫秒而不是0秒(self):
        # 🔴 这条是本次改动**唯一**要防的回归：原来 `int(0.4)` ⇒ "0 秒"。
        self.assertEqual(_human(0.0), "0 毫秒")
        self.assertEqual(_human(0.4), "400 毫秒")
        self.assertEqual(_human(0.98), "980 毫秒")
        self.assertNotIn("0 秒", _human(0.98))

    def test_十秒内保留一位小数(self):
        self.assertEqual(_human(1.0), "1.0 秒")
        self.assertEqual(_human(3.42), "3.4 秒")
        self.assertEqual(_human(9.96), "10.0 秒")

    def test_十秒以上取整秒(self):
        self.assertEqual(_human(12.4), "12 秒")
        self.assertEqual(_human(59.6), "60 秒")

    def test_分与小时(self):
        self.assertEqual(_human(60), "1 分 0 秒")
        self.assertEqual(_human(193), "3 分 13 秒")
        self.assertEqual(_human(3725), "1 小时 2 分")

    def test_单调递增不会出现0(self):
        """⛔ 回归护栏：任何正数耗时都不该渲染成「0 秒 / 0 毫秒」。"""
        for sec in (0.001, 0.05, 0.5, 1, 5.5, 30, 61, 600):
            out = _human(sec)
            self.assertFalse(out.startswith("0 "), f"{sec}s 渲染成了 {out!r}")


class TestRequestLog(unittest.TestCase):
    def test_归并按次数降序(self):
        rl = RequestLog()
        for _ in range(3):
            rl.add("fs_export_dir_status", note="等导出任务", ms=120, wait_ms=2000)
        rl.add("fs_export_dir", note="提交导出任务", ms=400, wait_ms=0)
        s = rl.summary()
        self.assertEqual(s["total"], 4)
        # 「哪个接口被反复调」必须排在最前 —— 这是这个视图存在的理由
        self.assertEqual(s["by_name"][0]["name"], "fs_export_dir_status")
        self.assertEqual(s["by_name"][0]["count"], 3)
        self.assertEqual(s["by_name"][0]["wait_ms"], 6000)
        self.assertEqual(s["by_name"][1]["name"], "fs_export_dir")

    def test_节流等待与请求耗时分开算(self):
        """⚠️ 并成一个数，就永远查不出「这一轮为什么慢」。"""
        rl = RequestLog()
        rl.add("fs_files", ms=80, wait_ms=3000)
        s = rl.summary()
        self.assertEqual(s["request_ms"], 80)      # 115 真花的
        self.assertEqual(s["wait_ms"], 3000)       # 节流睡的

    def test_失败也记账(self):
        rl = RequestLog()
        rl.add("fs_file", ok=False, err="被 115 拒绝")
        rl.add("fs_file", ok=True)
        s = rl.summary()
        self.assertEqual(s["failed"], 1)
        self.assertEqual(s["by_name"][0]["failed"], 1)
        self.assertEqual(s["items"][0]["err"], "被 115 拒绝")

    def test_超上限只丢明细不丢统计(self):
        """⛔ 截断时**不许**连带把计数也丢掉 —— 那等于报假数字。"""
        rl = RequestLog(limit=2)
        for i in range(5):
            rl.add("fs_files", ms=10, note=f"第 {i} 次")
        s = rl.summary()
        self.assertEqual(s["total"], 5)
        self.assertEqual(s["shown"], 2)
        self.assertEqual(s["dropped"], 3)
        self.assertEqual(s["by_name"][0]["count"], 5)   # 统计仍然完整
        self.assertEqual(len(s["items"]), 2)

    def test_序号连续且时刻带毫秒(self):
        """序号是「这一轮第几次请求」—— 网页流水表的第一列，必须从 1 连续排。"""
        rl = RequestLog()
        for _ in range(3):
            rl.add("a")
        items = rl.summary()["items"]
        self.assertEqual([x["seq"] for x in items], [1, 2, 3])
        self.assertRegex(items[0]["at"], r"^\d{2}:\d{2}:\d{2}\.\d{3}$")


if __name__ == "__main__":
    unittest.main()
