"""分级垃圾判定的回归测试。

样本来自他那份真实目录树里的**高频文件名**（括号是实际出现次数）。
最要紧的是 `test_白名单_正规封面永不碰` 与 `test_不误判_这些是正片` 两组 ——
它们锁住的是**会导致删错东西**的那几条规则。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import junk  # noqa: E402
from app.junk import LEVEL_KEEP, LEVEL_STRONG, LEVEL_SUSPECT  # noqa: E402


def j(fname: str, dup: int = 1, parent: str = "") -> junk.Verdict:
    return junk.judge(fname, dup_count=dup, dup_min=5, parent_name=parent)


class TestKeep(unittest.TestCase):
    def test_白名单_正规封面永不碰(self):
        """🔴 2026-10-06 实测：粗规则会把这三兄弟判成垃圾，但它们是**正规封面**。"""
        for name in ("screens.jpg", "poster.jpg", "1.jpg", "folder.jpg", "fanart.png"):
            with self.subTest(name=name):
                self.assertEqual(j(name, dup=120).level, LEVEL_KEEP)

    def test_白名单_字幕与元数据(self):
        for name in ("DLDSS-532.srt", "DLDSS-532.ass", "DLDSS-532.nfo",
                     "movie.sfv", "part.r00", "x.par2"):
            with self.subTest(name=name):
                self.assertEqual(j(name).level, LEVEL_KEEP)

    def test_不误判_这些是正片(self):
        """⛔ 四条误判防线：它们带域名/广告字样，但都是**正片**。"""
        cases = [
            ("4k688.com@DLDSS-532.mp4", "DLDSS-532"),          # 域名+番号，就是正片
            ("DLDSS-532-AI.mp4", "DLDSS-532"),
            ("(大鸟十八Bigbird18)撸先生和玲的射4分钟.mp4", "云下载"),   # 标题里带「撸」
            ("Larkin Love BallBustingPornstars.com Big Tits 2012.wmv", "+Larkin Love"),
        ]
        for name, parent in cases:
            with self.subTest(name=name):
                self.assertEqual(j(name, parent=parent).level, LEVEL_KEEP, name)

    def test_不误判_含水印字样的正片(self):
        """`【無水印原版---新片速遞】…` —— 「无水印」是**画质声明**，不是广告。"""
        v = j("guochan2048.com-【無水印原版---新片速遞】2022.3.29，真实校园.mp4",
              parent="某个目录")
        self.assertNotEqual(v.reason, "推广话术")


class TestStrong(unittest.TestCase):
    CASES = [
        ("聚 合 全 網 H 直 播.html", 119, "推广页"),
        ("最 新 位 址 獲 取：489155.com 收藏不迷路.txt", 110, "推广"),
        ("18+游戏大全(996gg.cc)-七龍珠H版-三國志H版-三國群淫傳等.mp4", 180, "推广话术"),
        ("(PLUS18GAMES) 下载APP签到领代币游戏免费玩~.mp4", 1, "推广话术"),
        ("乐鱼娱乐APP，新注册送38元，存款再送iphone 12：le233.com.url", 1, "推广页"),
        ("★★★女皇調教手冊-免费18禁手游.mp4", 7, "可疑话术"),
    ]

    def test_真广告都能判出来(self):
        for name, dup, hint in self.CASES:
            with self.subTest(name=name):
                v = j(name, dup=dup)
                self.assertEqual(v.level, LEVEL_STRONG, f"{name} → {v.reason}")
                self.assertIn(hint, v.reason)

    def test_整名即域名(self):
        for name in ("manko.fun.mp4", "x u u 6 2 . c o m.mp4", "StraplessDildo.com"):
            with self.subTest(name=name):
                self.assertEqual(j(name).level, LEVEL_STRONG)

    def test_可疑词要叠加重复次数(self):
        """「直播」单独出现**不算证据**（可能是正经标题），必须跨目录重复达标。"""
        self.assertNotEqual(j("有趣的台湾妹妹直播.mp4", dup=1).level, LEVEL_STRONG)
        self.assertEqual(j("有趣的台湾妹妹直播.mp4", dup=11).level, LEVEL_STRONG)

    def test_不误判_打飞机不是引流(self):
        """`1 宅男打飞机推荐…` 曾因「飞机」被误判成引流件。"""
        v = j("1 宅男打飞机推荐长相甜美网红美少女VIP大尺度自拍.mp4", dup=1)
        self.assertNotEqual(v.reason, "引流话术")


class TestSuspect(unittest.TestCase):
    def test_灰区只报告(self):
        for name, dup in (("网页数据抓取分析.docx", 1), ("readme", 1),
                          ("新片首发 每天更新 同步日韩.mp4", 9)):
            with self.subTest(name=name):
                v = j(name, dup=dup)
                self.assertIn(v.level, (LEVEL_SUSPECT, LEVEL_KEEP))
                self.assertFalse(v.auto)


class TestSplitExt(unittest.TestCase):
    def test_只认已知扩展名(self):
        """🐛 `…@NongPink.CoM` 曾被拆成主名 `…@NongPink` + 扩展名 `com`。

        后果：按 `HEYZO-0768.com` 去改名 —— 等于给视频文件套了个伪域名后缀。
        """
        self.assertEqual(junk.split_ext("a.b@NongPink.CoM"), ("a.b@NongPink.CoM", ""))
        self.assertEqual(junk.split_ext("DLDSS-532.mp4"), ("DLDSS-532", "mp4"))
        self.assertEqual(junk.split_ext("xx.1080p"), ("xx.1080p", ""))
        self.assertEqual(junk.split_ext("pkg.r00"), ("pkg", "r00"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
