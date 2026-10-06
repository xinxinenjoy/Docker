"""把 115offline 那份**独立脚本** `check_namer.py` 挂进 unittest 套件。

为什么不在 `check_namer.py` 里直接改成 unittest：
  那份文件是跟 `115offline/app/namer.py` **成对同步**的（改名字规则要两边一起回归），
  它本身要求「`python tests/check_namer.py` 就能跑、退出码即结论」。
  为了塞进 `unittest discover` 而重写它，会让两边越来越不像。

所以做法是：**脚本保持原样**，这里用 `runpy` 以 `__main__` 身份跑它，把它自己的
`sys.exit(0/1)` 翻译成断言。两边的好处都留住。
"""
from __future__ import annotations

import runpy
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check_namer.py"


class TestNamerStandalone(unittest.TestCase):
    def test_命名规则独立脚本全过(self):
        """`check_namer.py` 退出码 0 = 全过；非 0 = 有 case 挂了。"""
        self.assertTrue(SCRIPT.exists(), f"找不到 {SCRIPT}")
        with self.assertRaises(SystemExit) as cm:
            runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertEqual(cm.exception.code, 0,
                         "check_namer.py 里有命名用例没过 —— 单独跑 `python tests/check_namer.py` 看是哪条")


if __name__ == "__main__":
    unittest.main(verbosity=2)
