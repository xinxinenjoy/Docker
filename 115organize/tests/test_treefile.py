"""目录树解析单测。

⭐ 这个文件是**为了一个真跑出来才会发现的 bug** 补的（2026-10-06 真机对照才发现）：

    解析器原来按「名字带扩展名 ⇒ 文件」判类型。可本盘大量**目录名本身就是视频文件名**
    —— `MIDA-753.mp4` 是个目录，里面装着一个同名的 `MIDA-753.mp4` 文件。

    误判成「文件」会连锁出两个后果（都不报错，只是静默算错）：
      ① 它不进目录栈 ⇒ 它的子项被挂到**上一层** ⇒ `云下载` 下凭空多出 3,600+ 个「文件」
         （真机只有 154 个），且同名条目在 `files` 里出现两次；
      ② 计划里对它生成 `move_file` / `trash` 动作 ⇒ 执行时拿**文件的规则去动一个目录**。

    正确判据只有一个：**下一行的缩进比它更深**。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import treefile          # noqa: E402

BAR_TREE = "\n".join([
    "|——根目录",
    "| |-云下载",
    "| | |-MIDA-753.mp4",                    # ← 目录（名字带 .mp4）
    "| | | |-MIDA-753.mp4",                  # ← 它里面的**同名文件**
    "| | |-DLDSS-532",
    "| | | |-4k688.com@DLDSS-532.mp4",
    "| | |-散片.mp4",                         # ← 真散文件：没有子项
])


class TestBinaryTree(unittest.TestCase):
    def setUp(self):
        self.t = treefile.parse_text(BAR_TREE)

    def test_名字带扩展名的目录必须判成目录(self):
        """⭐ 核心回归：`MIDA-753.mp4` 有子项 ⇒ 它是目录，不是文件。"""
        e = self.t.entries["根目录/云下载/MIDA-753.mp4"]
        self.assertTrue(e.is_dir, "有下一层缩进就是目录，跟名字像不像文件无关")

    def test_同名文件挂在目录里而不是上一层(self):
        """子项不能回退挂到上一层 —— 那会让 `云下载` 下多出一个同名「文件」。"""
        inner = self.t.entries["根目录/云下载/MIDA-753.mp4/MIDA-753.mp4"]
        self.assertFalse(inner.is_dir, "它是真文件：后面没有更深的行")
        self.assertEqual(inner.parent, "根目录/云下载/MIDA-753.mp4")
        self.assertEqual(self.t.files_of("根目录/云下载"), ["散片.mp4"],
                         "云下载 下的文件只该有真正的散文件")
        self.assertEqual(self.t.files_of("根目录/云下载/MIDA-753.mp4"), ["MIDA-753.mp4"])

    def test_深度口径_根是0第一层是1(self):
        """曾写成 `len(indent)//2 + 1`，第一层成了 2 ⇒ `top_level` 恒为空。"""
        self.assertEqual(self.t.entries["根目录"].depth, 0)
        self.assertEqual(self.t.entries["根目录/云下载"].depth, 1)
        self.assertEqual(self.t.entries["根目录/云下载/MIDA-753.mp4"].depth, 2)
        self.assertEqual([self.t.entries[p].name for p in self.t.top_level], ["云下载"])

    def test_没有子项的才是文件(self):
        self.assertFalse(self.t.entries["根目录/云下载/DLDSS-532/4k688.com@DLDSS-532.mp4"].is_dir)
        self.assertTrue(self.t.entries["根目录/云下载/DLDSS-532"].is_dir)

    def test_children与files互斥且合计等于本层条目(self):
        kids = [self.t.entries[p].name for p in self.t.children_dirs("根目录/云下载")]
        self.assertEqual(sorted(kids), ["DLDSS-532", "MIDA-753.mp4"])
        self.assertEqual(len(kids) + len(self.t.files_of("根目录/云下载")), 3)

    def test_空目录与文件无法区分_都算文件(self):
        """⚠️ 已知限制：导出格式**没有类型标记**，空目录（没子项）会被记成文件。

        实测本盘这样的约 50 个（真机根下 7,544 目录 / 154 文件，我们记成 7,494 / 203）。
        影响可控 —— executor 移动/清理都按类型闸过一道，类型不符就跳过。
        """
        t = treefile.parse_text("\n".join(["|——根目录", "| |-空目录", "| |-真文件.mp4"]))
        self.assertEqual(sorted(t.files_of("根目录")), ["真文件.mp4", "空目录"],
                         "两个都被当文件 —— 这是导出格式的固有限制，不是解析器的锅")


class TestOtherFormats(unittest.TestCase):
    def test_制表符树(self):
        t = treefile.parse_text("\n".join([
            "根目录",
            "├── 云下载",
            "│   ├── DLDSS-532",
            "│   │   └── a.mp4",
        ]))
        self.assertTrue(t.entries["根目录/云下载"].is_dir)
        self.assertTrue(t.entries["根目录/云下载/DLDSS-532"].is_dir)
        self.assertFalse(t.entries["根目录/云下载/DLDSS-532/a.mp4"].is_dir)

    def test_缩进树(self):
        t = treefile.parse_text("\n".join(["根目录", "  云下载", "    DLDSS-532", "      a.mp4"]))
        self.assertTrue(t.entries["根目录/云下载/DLDSS-532"].is_dir)
        self.assertEqual(t.files_of("根目录/云下载/DLDSS-532"), ["a.mp4"])

    def test_rebase后子项不带原前缀(self):
        t = treefile.parse_text(BAR_TREE).rebase("云下载")
        self.assertEqual(t.root_name, "云下载")
        self.assertIn("MIDA-753.mp4", t.entries, "rebase 后子项的 key 就是它自己的名字")
        self.assertTrue(t.entries["MIDA-753.mp4"].is_dir)
        self.assertEqual(t.entries["MIDA-753.mp4"].parent, "云下载")


class TestEncoding(unittest.TestCase):
    def test_utf16le带BOM能读(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "t.txt"
            p.write_bytes(BAR_TREE.encode("utf-16"))       # 带 BOM 的 UTF-16LE
            raw = p.read_bytes()
            self.assertEqual(raw[:2], b"\xff\xfe")
            self.assertEqual(treefile._detect_encoding(raw), "utf-16")
            self.assertEqual(treefile.parse_file(p).root_name, "根目录")

    def test_utf8带BOM能读(self):
        self.assertEqual(treefile._detect_encoding("\ufeffx".encode("utf-8")), "utf-8-sig")

    def test_找不到根要报错(self):
        t = treefile.parse_text(BAR_TREE)
        with self.assertRaises(treefile.TreeParseError):
            t.rebase("不存在的目录")


if __name__ == "__main__":
    unittest.main(verbosity=2)
