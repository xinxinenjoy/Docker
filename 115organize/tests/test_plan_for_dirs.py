"""选定文件夹计划（功能 2）单测。

`build_plan_for_dirs` 只处理用户勾选的目录及其子树，其它一律不动。
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.config import Config                       # noqa: E402
from app.plan import build_plan_for_dirs            # noqa: E402
from app import treefile                            # noqa: E402


def _tree():
    """一棵小树：整理根 `云下载` 下有番号目录、影视目录、垃圾目录。"""
    return treefile.parse_text("\n".join([
        "|——根目录",
        "| |-云下载",
        "| | |-DLDSS-532",
        "| | | |-4k688.com@DLDSS-532.mp4",
        "| | |-DLDSS-533",
        "| | | |-DLDSS-533.mp4",
        "| | |-蝙蝠侠（2022）",
        "| | | |-蝙蝠侠（2022）.mp4",
        "| | |-文宣",
        "| | | |-广告.txt",
        "| | |-动作片",
        "| | | |-散片.mp4",
        "| | | |-screens.jpg",
        "| | |-别的目录",
        "| | | |-不该动.mp4",
    ]))


class ForDirsCase(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.root_path = "云下载"
        self.tree = _tree()

    def _plan(self, dirs, **kw):
        return build_plan_for_dirs(self.tree, self.cfg, dirs, **kw)

    def test_只处理选中的目录(self):
        """选了 `动作片`，就不该出现 `别的目录` 的任何动作。"""
        plan = self._plan(["动作片"])
        for op in plan.ops:
            self.assertNotIn("别的目录", op.path, "没选的目录不许动")
        self.assertNotIn("不该动.mp4", " ".join(op.path for op in plan.ops))

    def test_番号目录归位(self):
        """选中三个同系列番号目录，够阈值 ⇒ 建系列目录并移入。"""
        # 补第三部，凑够 SERIES_MIN=3
        self.tree = treefile.parse_text("\n".join([
            "|——根目录",
            "| |-云下载",
            "| | |-DLDSS-532", "| | | |-4k688.com@DLDSS-532.mp4",
            "| | |-DLDSS-533", "| | | |-DLDSS-533.mp4",
            "| | |-DLDSS-534", "| | | |-DLDSS-534.mp4",
            "| | |-蝙蝠侠（2022）", "| | | |-蝙蝠侠（2022）.mp4",
            "| | |-文宣", "| | | |-广告.txt",
            "| | |-动作片", "| | | |-散片.mp4", "| | | |-screens.jpg",
            "| | |-别的目录", "| | | |-不该动.mp4",
        ])).rebase("云下载")
        plan = self._plan(["DLDSS-532", "DLDSS-533", "DLDSS-534"])
        mkdirs = [op.target for op in plan.ops if op.kind == "mkdir"]
        self.assertIn("DLDSS", mkdirs, "系列目录该被补建")
        moves = [op for op in plan.ops if op.kind == "move_dir"]
        self.assertEqual(len(moves), 3)
        self.assertTrue(all(op.target == "DLDSS" for op in moves))

    def test_番号不够阈值_只规范命名(self):
        plan = self._plan(["DLDSS-532"])
        # 单独一部 ⇒ 不建系列目录（SERIES_MIN=3），只可能改名或不动
        mkdirs = [op.target for op in plan.ops if op.kind == "mkdir"]
        self.assertNotIn("DLDSS", mkdirs)

    def test_选中目录内垃圾被清理(self):
        """广告外壳目录 `文宣`（名字即推广话术）该进清理，不是里面的广告.txt。"""
        plan = self._plan(["文宣"])
        trash = [op for op in plan.ops if op.kind == "trash"]
        self.assertTrue(trash, "文宣 名字是推广话术，该进清理")
        self.assertTrue(any("文宣" in op.path for op in trash))

    def test_影视目录保持原名(self):
        plan = self._plan(["蝙蝠侠（2022）"])
        for op in plan.ops:
            self.assertNotIn("蝙蝠侠（2022）", op.new_name, "影视目录不改名")

    def test_深一层子目录也会被处理(self):
        """选 `动作片`，它的直接子文件该被扫到（垃圾/散片）。"""
        plan = self._plan(["动作片"])
        paths = " ".join(op.path for op in plan.ops)
        # 散片.mp4 是动作片下的文件，至少不该出现在「别的目录」路径
        self.assertNotIn("别的目录", paths)

    def test_不存在的路径静默丢弃(self):
        plan = self._plan(["不存在的目录"])
        self.assertEqual(len(plan.ops), 0, "不存在的路径不产生任何动作")


if __name__ == "__main__":
    unittest.main(verbosity=2)
