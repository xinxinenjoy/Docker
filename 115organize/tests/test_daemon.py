"""入站口（自动整理）单测 —— 取代旧的 cron/快照/最近接收测试。

旧模型（每天 04:00 全盘 diff + 最近接收）整套删掉了，换成入站口模式：
只处理 `ROOT_PATH/待整理/` 下的一层子项，处理完移走 = 天然增量。

这里测：
  · `list_inbox` —— 入站目录列举（含未建时不炸）
  · `build_inbox_plan` —— 各类条目的归位规则
  · `InboxDaemon.once` —— 一轮执行（dry-run / 真跑 / 空目录）
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config                       # noqa: E402
from app.inbox import InboxDaemon, build_inbox_plan, list_inbox  # noqa: E402
from app.v115 import Node                           # noqa: E402


class InboxCase(unittest.TestCase):
    """造一棵「整理根 + 入站口 + 几个系列目录」的假 115。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config()
        self.cfg.data_dir = Path(self._tmp.name)
        self.cfg.inbox_dir = "待整理"
        # 整理根下已存在的目录（判断「系列目录已存在」用）
        self.root_dirs: dict[str, str] = {"影视": "R-MOVIE", "动漫": "R-ANIME"}
        # 入站口里的内容
        self.inbox_dirs: dict[str, str] = {}
        self.inbox_files: dict[str, Node] = {}
        self.logs: list[tuple[str, str]] = []

        class Fake:
            def __init__(self, outer):
                self.o = outer
                self.calls = 0
                from app.throttle import ThrottleStats
                self.throttle = type("T", (), {"stats": ThrottleStats()})()

            def root_cid(self) -> str:
                return "ROOT"

            def list_dir(self, cid: str, dirs_only: bool = False) -> list:
                if cid == "ROOT":
                    items = [Node(id=i, name=n, is_dir=True) for n, i in self.o.root_dirs.items()]
                    if self.o.cfg.inbox_dir not in self.o.root_dirs:
                        items.append(Node(id="INBOX", name=self.o.cfg.inbox_dir, is_dir=True))
                    return items
                if cid == "INBOX":
                    dirs = [Node(id=i, name=n, is_dir=True) for n, i in self.o.inbox_dirs.items()]
                    files = list(self.o.inbox_files.values())
                    return dirs + files
                return []

        self.v = Fake(self)

    def tearDown(self):
        self._tmp.cleanup()

    def _log(self, level: str, msg: str) -> None:
        self.logs.append((level, msg))

    def _set_inbox(self, dirs: dict[str, str] | None = None,
                   files: dict[str, Node] | None = None):
        self.inbox_dirs = dirs or {}
        self.inbox_files = files or {}


class TestListInbox(InboxCase):
    def test_入站目录不存在返回空(self):
        self.root_dirs = {"影视": "R-MOVIE"}          # 没有「待整理」
        self.assertEqual(list_inbox(self.v, self.cfg), ([], []))

    def test_列出子目录与文件(self):
        self._set_inbox(dirs={"DLDSS-532": "1"}, files={"screens.jpg": Node(id="f1", name="screens.jpg", is_dir=False)})
        dirs, files = list_inbox(self.v, self.cfg)
        self.assertEqual([d.name for d in dirs], ["DLDSS-532"])
        self.assertEqual([f.name for f in files], ["screens.jpg"])


class TestBuildInboxPlan(InboxCase):
    def test_番号目录_够阈值移到系列(self):
        self._set_inbox(dirs={"DLDSS-532": "1", "DLDSS-533": "2", "DLDSS-534": "3"})
        plan = build_inbox_plan(self.v, self.cfg)
        # 3 部同系列，够 SERIES_MIN=3 ⇒ 建「DLDSS」目录，全部移进去
        kinds = [op.kind for op in plan.ops]
        self.assertIn("mkdir", kinds)
        moves = [op for op in plan.ops if op.kind == "move_dir"]
        self.assertEqual(len(moves), 3)
        self.assertTrue(all(op.target == "DLDSS" for op in moves))

    def test_番号目录_不够阈值留在根下(self):
        self._set_inbox(dirs={"DLDSS-532": "1"})
        plan = build_inbox_plan(self.v, self.cfg)
        moves = [op for op in plan.ops if op.kind == "move_dir"]
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0].target, "", "不够阈值 ⇒ 移到根下（target 空 = 整理根）")

    def test_番号目录_系列已存在直接搬入(self):
        self._set_inbox(dirs={"IPZZ-001": "1"})
        self.root_dirs["IPZZ"] = "R-IPZZ"             # 系列目录已存在
        plan = build_inbox_plan(self.v, self.cfg)
        moves = [op for op in plan.ops if op.kind == "move_dir"]
        self.assertEqual(moves[0].target, "IPZZ")

    def test_影视目录移到根下保持原名(self):
        self._set_inbox(dirs={"蝙蝠侠（2022）": "1"})
        plan = build_inbox_plan(self.v, self.cfg)
        moves = [op for op in plan.ops if op.kind == "move_dir"]
        self.assertEqual(moves[0].target, "", "影视目录 → 根下")
        self.assertEqual(moves[0].new_name, "", "保持原名")

    def test_垃圾目录进隔离(self):
        self._set_inbox(dirs={"文宣": "1"})
        plan = build_inbox_plan(self.v, self.cfg)
        trash = [op for op in plan.ops if op.kind == "trash"]
        self.assertTrue(trash, "广告目录应被识别为垃圾")
        self.assertTrue(all(op.group == "junk" for op in trash))

    def test_无法识别留原地进skipped(self):
        self._set_inbox(dirs={"随便一个东西": "1"})
        plan = build_inbox_plan(self.v, self.cfg)
        self.assertEqual(len(plan.skipped), 1)
        self.assertIn("无法识别", plan.skipped[0]["reason"])
        # 没有动作（没有能自动处理的）
        self.assertEqual([op for op in plan.ops if op.auto], [])

    def test_散落番号文件移到系列(self):
        self.root_dirs["DLDSS"] = "R-DLDSS"
        self._set_inbox(files={"DLDSS-532.mp4": Node(id="f1", name="DLDSS-532.mp4", is_dir=False)})
        plan = build_inbox_plan(self.v, self.cfg)
        moves = [op for op in plan.ops if op.kind == "move_file"]
        self.assertEqual(moves[0].target, "DLDSS/DLDSS-532")
        self.assertEqual(moves[0].new_name, "DLDSS-532.mp4")

    def test_入站口散文件垃圾进隔离(self):
        self._set_inbox(files={"manko.fun.mp4": Node(id="f1", name="manko.fun.mp4", is_dir=False)})
        plan = build_inbox_plan(self.v, self.cfg)
        trash = [op for op in plan.ops if op.kind == "trash"]
        self.assertTrue(trash)

    def test_补建目标目录(self):
        self._set_inbox(dirs={"DLDSS-532": "1", "DLDSS-533": "2", "DLDSS-534": "3"})
        plan = build_inbox_plan(self.v, self.cfg)
        mkdirs = [op.target for op in plan.ops if op.kind == "mkdir"]
        self.assertIn("DLDSS", mkdirs)


class TestInboxDaemon(InboxCase):
    def test_空目录无事可做(self):
        d = InboxDaemon(self.cfg, self._log)
        d.v = self.v
        res = d.once(execute=True)
        self.assertEqual(res["targets"], 0)
        self.assertEqual(res["requests"], 0)

    def test_dry_run_不执行(self):
        self.cfg.dry_run = True
        self._set_inbox(dirs={"DLDSS-532": "1", "DLDSS-533": "2", "DLDSS-534": "3"})
        d = InboxDaemon(self.cfg, self._log)
        d.v = self.v
        res = d.once(execute=False)
        self.assertEqual(res["dry_run"], True)
        self.assertEqual(res["requests"], 0, "dry-run 一个请求都不发")

    def test_执行一轮_调用executor(self):
        """验证 inbox 会调 Executor 并正确传参（dry_run / 断点文件名）。"""
        from unittest import mock

        self.cfg.dry_run = False
        self._set_inbox(dirs={"DLDSS-532": "1"})
        d = InboxDaemon(self.cfg, self._log)
        d.v = self.v

        captured: dict = {}

        class FakeEx:
            def __init__(self, v, cfg, log, *, state_name):
                captured["state_name"] = state_name
                captured["cfg_dry_run"] = cfg.dry_run

            def run(self, plan, resume=False, max_requests=None):
                captured["resume"] = resume
                captured["n_ops"] = len(plan.ops)
                return {"started": "", "finished": "", "elapsed_human": "0:00:00",
                        "dry_run": False, "planned": len(plan.ops), "done": len(plan.ops),
                        "failed": 0, "requests": 0, "by_kind": {}, "errors": [],
                        "throttle": {}, "skipped_manual": 0, "skipped_dirs": 0,
                        "trash_dirs": 0, "unroutable": 0}

        with mock.patch("app.inbox.Executor", FakeEx):
            res = d.once(execute=True)

        self.assertIn("targets", res)
        self.assertEqual(captured["state_name"], "inbox-state.json", "入站口用独立断点文件")
        self.assertEqual(captured["resume"], False, "入站口永远从头跑（天然增量）")
        self.assertTrue(captured["n_ops"] >= 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
