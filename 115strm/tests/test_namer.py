"""路径映射 / URL 拼接 / 文件名安全 —— 核心口径的回归用例。

⚠️ 这些口径全部来自 2026-10-08 对着 alist 真机的实测：
    · `/d/` 端点 → 302 到 115 CDN，免认证 ✅
    · `/dav/` 端点 → 401 ⛔ 不能写
    · 编码与不编码 alist 都接受，选编码（更稳）
"""
from __future__ import annotations

import unittest

from app.namer import (DiffResult, StrmBuilder, StrmFile, is_video, local_path_of,
                       safe_name, url_of_path)


class TestUrlPath(unittest.TestCase):
    def test_逐段编码保留斜杠(self):
        # 🔴 关键：不能整体 quote()，否则 `/` 变 `%2F`，整个路径塌成一段
        got = url_of_path("115电影/功夫女足（2026）/a b.mkv", encode=True)
        self.assertEqual(
            got,
            "115%E7%94%B5%E5%BD%B1/%E5%8A%9F%E5%A4%AB%E5%A5%B3%E8%B6%B3%EF%BC%882026%EF%BC%89/a%20b.mkv")
        self.assertIn("/", got)                       # 分隔符必须保留
        self.assertNotIn("%2F", got.upper().replace("%2f", "%2F").replace("%2F", "%2F"))
        self.assertNotIn("%2F", got)

    def test_不编码时原样输出(self):
        self.assertEqual(url_of_path("115电影/a b.mkv", encode=False), "115电影/a b.mkv")

    def test_前导斜杠被吃掉(self):
        self.assertEqual(url_of_path("/a/b/c", encode=False), "a/b/c")

    def test_空路径(self):
        self.assertEqual(url_of_path("", encode=False), "")


class TestStrmBuilder(unittest.TestCase):
    def test_整串都编码_纯ASCII(self):
        """🔴 核心口径：**前缀里的中文也要编**，产出必须是纯 ASCII。

        只编相对路径会得到「前缀原始中文 + 后面 %XX」的混合形态；
        curl / 浏览器能忍，但严格 HTTP 客户端编不了（urllib 直接抛 ascii 错）。
        播放器用的库五花八门，赌不起 ⇒ 全编。
        """
        b = StrmBuilder(prefix="https://alist.example.com:5244/d/影音/115影音", encode=True)
        u = b.url("115电影/功夫女足（2026）/x.mkv")
        self.assertEqual(
            u,
            "https://alist.example.com:5244/d/%E5%BD%B1%E9%9F%B3/115%E5%BD%B1%E9%9F%B3/"
            "115%E7%94%B5%E5%BD%B1/%E5%8A%9F%E5%A4%AB%E5%A5%B3%E8%B6%B3%EF%BC%882026%EF%BC%89/x.mkv")
        u.encode("ascii")                       # 必须能编 —— 编不了就会抛
        self.assertNotIn("影音", u)

    def test_保留斜杠不做成单段(self):
        b = StrmBuilder(prefix="https://x/d/影音/115影音", encode=True)
        u = b.url("a/b/c.mkv")
        self.assertNotIn("%2F", u)
        self.assertEqual(u.count("/"), u.count("/"))     # 结构没塌

    def test_不编码时原样(self):
        b = StrmBuilder(prefix="https://x/d/影音/115影音", encode=False)
        self.assertEqual(b.url("a.mkv"), "https://x/d/影音/115影音/a.mkv")

    def test_带alist_base(self):
        b = StrmBuilder(prefix="https://x/d", alist_base="影音/115影音", encode=False)
        self.assertEqual(b.url("115电影/a.mkv"), "https://x/d/影音/115影音/115电影/a.mkv")

    def test_带alist_base且编码(self):
        b = StrmBuilder(prefix="https://x/d", alist_base="影音/115影音", encode=True)
        self.assertEqual(
            b.url("a.mkv"),
            "https://x/d/%E5%BD%B1%E9%9F%B3/115%E5%BD%B1%E9%9F%B3/a.mkv")

    def test_前缀尾斜杠被规整(self):
        b = StrmBuilder(prefix="https://x/d/", encode=False)
        self.assertEqual(b.url("a.mkv"), "https://x/d/a.mkv")

    def test_路径里的相对段首尾斜杠被剥(self):
        b = StrmBuilder(prefix="https://x/d", encode=False)
        self.assertEqual(b.url("/a/b.mkv/"), "https://x/d/a/b.mkv")

    def test_空格与特殊字符被编(self):
        b = StrmBuilder(prefix="https://x/d", encode=True)
        u = b.url("a b/749局 749局 (2024).mkv")
        self.assertNotIn(" ", u)
        self.assertIn("%20", u)
        self.assertIn("%282024%29", u)          # 半角括号
        self.assertIn("%EF%BC%88", b.url("（全角）"))

    def test_无scheme前缀不产出畸形URL(self):
        """前缀手抖写成 `alist.example.com:5244/d/…`（没带 https://）时别炸。"""
        b = StrmBuilder(prefix="alist.example.com:5244/d/影音/115影音", encode=True)
        u = b.url("a.mkv")
        self.assertTrue(u.endswith("a.mkv"))
        u.encode("ascii")

    def test_绝不产出dav(self):
        # 🔴 回归：/dav 实测 401，任何情况下都不该出现
        b = StrmBuilder(prefix="https://alist.example.com:5244/d/影音/115影音", encode=True)
        self.assertNotIn("/dav", b.url("a/b.mkv"))
        self.assertNotIn("dav", b.url("a/b.mkv"))


class TestLocalPath(unittest.TestCase):
    def test_拼接(self):
        self.assertEqual(local_path_of("/out", "a/b/c.mkv"), "/out/a/b/c.mkv")

    def test_尾斜杠规整(self):
        self.assertEqual(local_path_of("/out/", "a.mkv"), "/out/a.mkv")

    def test_空相对路径(self):
        self.assertEqual(local_path_of("/out", ""), "/out")


class TestSafeName(unittest.TestCase):
    def test_非法字符换全角(self):
        self.assertEqual(safe_name('a<b>c:d"e'), "a＜b＞c：d＂e")

    def test_反斜杠与竖线(self):
        self.assertEqual(safe_name("a\\b|c?d*e"), "a＼b｜c？d＊e")

    def test_控制字符去掉(self):
        self.assertEqual(safe_name("a\x00b\x1fc"), "abc")

    def test_尾点空格去掉(self):
        # Windows 会静默吃掉目录名末尾的点与空格
        self.assertEqual(safe_name("name. "), "name")

    def test_全非法时给占位(self):
        self.assertEqual(safe_name("..."), "_")

    def test_中文括号不动(self):
        # ⚠️ 全角括号是合法字符，绝不能动（动了就和网盘对不上了）
        self.assertEqual(safe_name("功夫女足（2026）"), "功夫女足（2026）")


class TestIsVideo(unittest.TestCase):
    def setUp(self):
        self.exts = frozenset(("mp4", "mkv", "srt", "nfo", "jpg"))

    def test_命中(self):
        self.assertTrue(is_video("a.mkv", self.exts))
        self.assertTrue(is_video("A.MKV", self.exts))       # 大小写不敏感
        self.assertTrue(is_video("a.zh-CN.srt", self.exts))

    def test_不命中(self):
        self.assertFalse(is_video("a.txt", self.exts))
        self.assertFalse(is_video("README", self.exts))     # 没扩展名
        self.assertFalse(is_video("a.exe", self.exts))


class TestDefaultExts(unittest.TestCase):
    """⭐ 默认白名单**只放视频** —— 这是 2026-10-08 真机部署后纠正的。

    原来把字幕 / 元数据 / 图片也放进来，以为「播放器认外挂字幕」——**那是错的**：
    媒体服务器读字幕靠的是「与视频同目录同名的真实文件」，不认 `xxx.srt.strm`。
    实测首轮跑出 867 个 strm，其中 42 个是这类无用件。
    """

    def test_默认不含字幕与元数据(self):
        from app.config import VIDEO_EXT_DEFAULT
        exts = frozenset(e.strip() for e in VIDEO_EXT_DEFAULT.split(",") if e.strip())
        for bad in ("ass", "srt", "ssa", "sub", "idx", "vtt", "sup",
                    "nfo", "jpg", "jpeg", "png", "webp"):
            self.assertNotIn(bad, exts, f"默认白名单不该含 {bad}（生成 strm 没用）")

    def test_默认含常见视频格式(self):
        from app.config import VIDEO_EXT_DEFAULT
        exts = frozenset(e.strip() for e in VIDEO_EXT_DEFAULT.split(",") if e.strip())
        for good in ("mp4", "mkv", "avi", "ts", "mov", "wmv", "rmvb", "webm"):
            self.assertIn(good, exts)


class TestStrmFile(unittest.TestCase):
    def test_local_rel_保留原扩展名(self):
        # ⚠️ `x.mkv` → `x.mkv.strm`（不是 `x.strm`）——
        #    媒体服务器靠后缀识别类型，这样更容易正确归类。
        f = StrmFile(rel="a/b.mkv", url="http://x/y.mkv")
        self.assertEqual(f.local_rel, "a/b.mkv.strm")


class TestDiffCounts(unittest.TestCase):
    def test_counts_键齐全(self):
        d = DiffResult(
            to_add=[StrmFile("a", "u1")],
            to_update=[StrmFile("b", "u2")],
            to_delete=["c.strm"],
        )
        c = d.counts()
        self.assertEqual(c["add"], 1)
        self.assertEqual(c["update"], 1)
        self.assertEqual(c["delete"], 1)
        self.assertEqual(c["total_write"], 2)

    def test_空差异(self):
        c = DiffResult().counts()
        self.assertEqual(c["total_write"], 0)


if __name__ == "__main__":
    unittest.main()
