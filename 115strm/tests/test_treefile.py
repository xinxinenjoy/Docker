"""目录树解析回归 —— 格式支持来自 115organize 的真机实测（115 导出的 UTF-16LE）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.treefile import TreeParseError, parse_file, parse_text, pick_tree_file

BAR_TREE = """|——云下载
| |-115电影
| | |-功夫女足（2026）
| | | |-功夫女足.Kung.Fu.Soccer.2026.2160p.WEB-DL.mkv
| | |-歪心狼对阵ACME（2026）
| | | |-歪心狼.mkv
| |-115电视剧
| | |-怪奇物语
| | | |-S01E01.mkv
"""

TREE_CMD = """云下载
├── 115电影
│   ├── 功夫女足（2026）
│   │   └── a.mkv
└── 115电视剧
    └── b.mkv
"""


class TestBarTree(unittest.TestCase):
    def test_解析竖线树(self):
        t = parse_text(BAR_TREE)
        self.assertEqual(t.root_path, "云下载")
        top = [t.entries[d].name for d in t.children_dirs("云下载")]
        self.assertEqual(top, ["115电影", "115电视剧"])

    def test_文件归属正确(self):
        t = parse_text(BAR_TREE)
        files = t.files_of("云下载/115电影/功夫女足（2026）")
        self.assertEqual(files, ["功夫女足.Kung.Fu.Soccer.2026.2160p.WEB-DL.mkv"])

    def test_统计(self):
        t = parse_text(BAR_TREE)
        st = t.stats()
        # 云下载 + 115电影 + 功夫女足（2026） + 歪心狼对阵ACME（2026） + 115电视剧 + 怪奇物语
        self.assertEqual(st["dirs"], 6)
        self.assertEqual(st["files"], 3)

    def test_深层目录认得(self):
        t = parse_text(BAR_TREE)
        self.assertIn("云下载/115电影/功夫女足（2026）", t.entries)
        self.assertTrue(t.entries["云下载/115电影/功夫女足（2026）"].is_dir)

    def test_大小后缀被剥掉(self):
        t = parse_text("|——root\n| |-a.mkv 1.2GB\n")
        self.assertIn("root/a.mkv", t.entries)


class TestTreeCmd(unittest.TestCase):
    def test_解析制表符树(self):
        t = parse_text(TREE_CMD)
        self.assertIn("云下载/115电影", t.entries)
        self.assertIn("云下载/115电视剧", t.entries)
        self.assertEqual(t.files_of("云下载/115电影/功夫女足（2026）"), ["a.mkv"])


class TestIndentTree(unittest.TestCase):
    def test_纯缩进(self):
        t = parse_text("root\n  a\n    b.mkv\n  c\n")
        self.assertTrue(t.entries["root/a"].is_dir)
        self.assertIn("root/a/b.mkv", t.entries)
        self.assertIn("root/c", t.entries)


class TestErrors(unittest.TestCase):
    def test_空树抛错(self):
        with self.assertRaises(TreeParseError):
            parse_text("   \n\n")

    def test_rebase_到不存在的目录抛错(self):
        t = parse_text(BAR_TREE)
        with self.assertRaises(TreeParseError):
            t.rebase("不存在的目录")

    def test_rebase_有效(self):
        t = parse_text(BAR_TREE)
        t2 = t.rebase("云下载/115电影")
        self.assertEqual(t2.root_path, "云下载/115电影")


class TestFileIO(unittest.TestCase):
    def test_读utf16le带bom(self):
        """⚠️ 115 实际导出的就是 UTF-16LE + BOM —— 当 UTF-8 读会读不出来。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.txt"
            p.write_bytes(BAR_TREE.encode("utf-16"))      # utf-16 会带 BOM
            t = parse_file(p)
            self.assertEqual(t.root_path, "云下载")

    def test_读utf8带bom(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.txt"
            p.write_bytes(BAR_TREE.encode("utf-8-sig"))
            self.assertEqual(parse_file(p).root_path, "云下载")

    def test_读gbk(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.txt"
            p.write_bytes(BAR_TREE.encode("gbk"))
            self.assertEqual(parse_file(p).root_path, "云下载")

    def test_pick_取最新(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "old.txt").write_text("a", encoding="utf-8")
            (d / "new.txt").write_text("b", encoding="utf-8")
            import os, time
            os.utime(d / "old.txt", (1000, 1000))
            os.utime(d / "new.txt", (2000, 2000))
            self.assertEqual(pick_tree_file(d).name, "new.txt")

    def test_pick_指定名字(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "a.txt").write_text("a", encoding="utf-8")
            (d / "b.txt").write_text("b", encoding="utf-8")
            self.assertEqual(pick_tree_file(d, "a.txt").name, "a.txt")

    def test_pick_目录不存在抛错(self):
        with self.assertRaises(TreeParseError):
            pick_tree_file("/根本不存在的目录")

    def test_pick_目录里没txt抛错(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(TreeParseError):
                pick_tree_file(td)


if __name__ == "__main__":
    unittest.main()
