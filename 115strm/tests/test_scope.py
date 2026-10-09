"""同步范围（同步根 + 白名单）的回归 —— **这一组是 2026-10-08 实测踩坑后补的**。

🔴 两个坑（都用真实目录树复现过）：
  ① **基准路径拿错** ⇒ 拼出重复层级
     树根 `根目录` / 影音库 `115影音` / 前缀 `…/d/影音/115影音`
     若拿 `tree.root_path` 当基准，相对路径会带 `115影音/`，
     拼出来 `…/d/影音/115影音/115影音/115电影/…` —— 重复一层，alist 找不到存储。
  ② **白名单勾了却不生效** ⇒ 「明明勾了却没同步」极难查。所以填了树里没有的路径要**报错**，
     不能静默忽略。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import Config
from app.sync import Syncer
from app.namer import StrmBuilder
from app.treefile import parse_text

# 真实结构（取自红领巾 2026-10-08 导出的目录树，缩到最少行）
REAL_TREE = """|——根目录
| |-115影音
| | |-115电影
| | | |-功夫女足（2026）
| | | | |-功夫女足.Kung.Fu.Soccer.2026.mkv
| | | |-阿凡达 (系列)
| | | | |-阿凡达2：水之道（2022）
| | |-115电视剧
| | | |-灵魂摆渡·十年（2026）
| | | | |-S01E01.mkv
| | | | |-S01E02.mkv
| | |-115短剧
| | | |-短剧A
| | | | |-01.mp4
"""

PREFIX = "https://alist.example.com:5244/d/影音/115影音"
# ⚠️ 上面是**显然不是本机的占位域名** —— 本仓库不写任何真实地址/主机名，
#    真实前缀由用户自己在网页上填、存在 `data/config.json`。


def cfg(tmp: Path, **kw) -> Config:
    base = dict(
        data_dir=tmp / "data", output_dir=tmp / "out",
        strm_prefix=PREFIX, url_encode=False, dry_run=True,
        remote_root="115影音", include_dirs="", video_ext="mp4,mkv",
    )
    base.update(kw)
    c = Config(**base)
    Path(c.data_dir).mkdir(parents=True, exist_ok=True)
    Path(c.output_dir).mkdir(parents=True, exist_ok=True)
    return c


class TestIncludesParsing(unittest.TestCase):
    def test_逗号分隔(self):
        self.assertEqual(Config(include_dirs="115电影,115电视剧").includes,
                         ("115电影", "115电视剧"))

    def test_换行分隔(self):
        self.assertEqual(Config(include_dirs="115电影\n115电视剧").includes,
                         ("115电影", "115电视剧"))

    def test_混用与空白(self):
        self.assertEqual(Config(include_dirs=" 115电影 , \n 115电视剧 ").includes,
                         ("115电影", "115电视剧"))

    def test_去重保序(self):
        self.assertEqual(Config(include_dirs="a,b,a").includes, ("a", "b"))

    def test_空表示全部(self):
        self.assertEqual(Config(include_dirs="").includes, ())
        self.assertEqual(Config(include_dirs="  \n ").includes, ())

    def test_首尾斜杠被剥(self):
        self.assertEqual(Config(include_dirs="/115电影/").includes, ("115电影",))

    def test_带同步根前缀容错(self):
        """⚠️ UI 上可能选到 `115影音/115电影`，那也该认 ——
        否则症状是「勾了却没同步」，极难查。"""
        c = Config(remote_root="115影音", include_dirs="115影音/115电影")
        self.assertEqual(c.includes, ("115电影",))

    def test_多余斜杠被规整(self):
        self.assertEqual(Config(include_dirs="a//b").includes, ("a/b",))


class TestWalkFromTree(unittest.TestCase):
    def test_基准是同步根_不重复层级(self):
        """🔴 核心回归：相对路径**不含同步根那一层**，但**含白名单那一层**。

        为什么含白名单层：本地目录是 `/out/115电影/功夫女足（2026）/x.strm` ——
        从本地到 alist 看到的结构是「同步根之下的层级」，正好对上
        `prefix(…/影音/115影音) + rel(115电影/…)`。少一层会拼错 URL。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            tree = parse_text(REAL_TREE)
            s = Syncer(None, cfg(tmp, include_dirs="115电影"))
            dirs, files = s.walk_from_tree(tree)
            rels = [f.rel for f in files]
            self.assertIn("115电影/功夫女足（2026）/功夫女足.Kung.Fu.Soccer.2026.mkv", rels)
            for r in rels + dirs:
                self.assertFalse(r.startswith("115影音/"), f"重复了同步根那一层：{r}")
                self.assertFalse(r.startswith("根目录/"), f"带上了树根：{r}")

    def test_URL拼出来正确(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            tree = parse_text(REAL_TREE)
            s = Syncer(None, cfg(tmp, include_dirs="115电影"))
            _dirs, files = s.walk_from_tree(tree)
            self.assertEqual(
                files[0].url,
                PREFIX + "/115电影/功夫女足（2026）/功夫女足.Kung.Fu.Soccer.2026.mkv")

    def test_白名单只走指定子树(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            tree = parse_text(REAL_TREE)
            s = Syncer(None, cfg(tmp, include_dirs="115电影,115电视剧"))
            dirs, files = s.walk_from_tree(tree)
            joined = "\n".join(dirs + [f.rel for f in files])
            self.assertIn("115电影", joined)
            self.assertIn("115电视剧", joined)
            self.assertNotIn("115短剧", joined)          # ⛔ 短剧完全不碰
            self.assertNotIn("短剧A", joined)

    def test_白名单为空则全要(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            tree = parse_text(REAL_TREE)
            s = Syncer(None, cfg(tmp, include_dirs=""))
            dirs, files = s.walk_from_tree(tree)
            joined = "\n".join(dirs + [f.rel for f in files])
            self.assertIn("115短剧", joined)             # 全都要时含短剧
            self.assertIn("115电影", joined)

    def test_白名单目录不存在要报错(self):
        """🔴 「勾了却没同步」极难查 ⇒ 必须报出来，不能静默忽略。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            tree = parse_text(REAL_TREE)
            s = Syncer(None, cfg(tmp, include_dirs="115电影,不存在的目录"))
            with self.assertRaises(Exception) as cm:
                s.walk_from_tree(tree)
            self.assertIn("不存在的目录", str(cm.exception))

    def test_同步根不存在要报错(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            tree = parse_text(REAL_TREE)
            s = Syncer(None, cfg(tmp, remote_root="根本没有这个", include_dirs=""))
            with self.assertRaises(Exception):
                s.walk_from_tree(tree)

    def test_只有含视频的目录才会被列出(self):
        """🔴 2026-10-08 二轮定调：「没有具体文件的目录直接抛弃不生成」。

        `115电影/功夫女足（2026）` 下面有 mkv ⇒ 建；
        `115电影/阿凡达 (系列)` 整棵子树一个视频都没有（它下面只有一个被截断的条目）
        ⇒ **不建** —— 免得媒体库里点进去一片空白。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            tree = parse_text(REAL_TREE)
            s = Syncer(None, cfg(tmp, include_dirs="115电影"))
            dirs, _files = s.walk_from_tree(tree)
            self.assertIn("115电影/功夫女足（2026）", dirs)
            self.assertNotIn("115电影/阿凡达 (系列)", dirs)
            self.assertEqual(s.tree_notes.get("skipped_dirs"), 1)

    def test_非媒体扩展名不生成(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            tree = parse_text(REAL_TREE)
            s = Syncer(None, cfg(tmp, include_dirs="115电视剧", video_ext="mp4"))
            _d, files = s.walk_from_tree(tree)
            self.assertEqual(files, [])                  # 剧集是 mkv，被排除

    def test_没有任何视频时连目录都不建(self):
        """把扩展名收窄到只剩 mp4 ⇒ 电视剧那棵子树一个都不剩，目录也不该建。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            tree = parse_text(REAL_TREE)
            s = Syncer(None, cfg(tmp, include_dirs="115电视剧", video_ext="mp4"))
            dirs, files = s.walk_from_tree(tree)
            self.assertEqual(dirs, [])
            self.assertEqual(files, [])

    def test_深层白名单(self):
        """白名单支持多级路径（如 `115电影/功夫女足（2026）`）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            tree = parse_text(REAL_TREE)
            s = Syncer(None, cfg(tmp, include_dirs="115电影/功夫女足（2026）"))
            _d, files = s.walk_from_tree(tree)
            self.assertEqual(len(files), 1)
            self.assertIn("功夫女足", files[0].rel)
            self.assertNotIn("阿凡达", files[0].rel)

    # ------------------------------------------- 无扩展名条目 = 疑似「被截断的子树」
    def test_无扩展名叶子不建任何东西_只报截断(self):
        """🔴 这是上一版**判断错**的那件事，必须钉住。

        上一版把这 386 个「既没子项、又没扩展名」的条目当**空目录**建出来，
        理由是「能看出哪个系列缺片」。真实原因是**导出时层级选少了**：
        那些「空壳」不是空的，是**被截断的子树**。

        ⇒ 现在：既不建目录、也不生成 strm，只在 `tree_notes` 里**计数告警**。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            tree = parse_text(REAL_TREE)
            s = Syncer(None, cfg(tmp, include_dirs="115电影"))
            dirs, files = s.walk_from_tree(tree)
            joined = "\n".join(dirs + [f.rel for f in files])
            self.assertNotIn("阿凡达2：水之道（2022）", joined)
            self.assertEqual(s.tree_notes.get("extensionless"), 1)
            self.assertIn("阿凡达2：水之道（2022）",
                          s.tree_notes.get("extensionless_sample", [])[0])

    def test_树统计能数出疑似截断(self):
        from app.treefile import parse_text as pt
        st = pt(REAL_TREE).stats()
        self.assertEqual(st["extensionless"], 1)
        self.assertTrue(st["extensionless_sample"])

    def test_keep_only_file_dirs_保留祖先链(self):
        """辅助函数本身的口径：只留「至少一个文件的祖先」。"""
        from app.sync import keep_only_file_dirs
        dirs = ["a", "a/b", "a/b/c", "空目录", "a/另一支"]
        files = ["a/b/c/x.mkv"]
        self.assertEqual(keep_only_file_dirs(dirs, files), ["a", "a/b", "a/b/c"])

    def test_keep_only_file_dirs_顶层文件不产生目录(self):
        from app.sync import keep_only_file_dirs
        self.assertEqual(keep_only_file_dirs(["x"], ["顶层的.mkv"]), [])

    def test_有扩展名的非媒体件不当目录(self):
        """`.txt` / `.exe` 这类**有**扩展名但不在白名单里的，既不建目录也不生成 strm。"""
        tree = parse_text(
            "|——root\n| |-片名（2026）\n| | |-a.mkv\n| | |-说明.txt\n| | |-坏东西.exe\n")
        with tempfile.TemporaryDirectory() as td:
            s = Syncer(None, cfg(Path(td), remote_root="", include_dirs=""))
            dirs, files = s.walk_from_tree(tree)
            self.assertEqual([f.rel for f in files], ["片名（2026）/a.mkv"])
            self.assertNotIn("root/片名（2026）/说明.txt", dirs)
            self.assertNotIn("root/片名（2026）/坏东西.exe", dirs)
            self.assertEqual(s.tree_notes.get("extensionless"), 0)

    def test_只有非媒体文件的目录不建(self):
        """一个目录里全是字幕 ⇒ 子树里没有任何 strm ⇒ 目录也不建。"""
        tree = parse_text("|——root\n| |-只有字幕\n| | |-a.ass\n| | |-b.srt\n")
        with tempfile.TemporaryDirectory() as td:
            s = Syncer(None, cfg(Path(td), remote_root="", include_dirs=""))
            dirs, files = s.walk_from_tree(tree)
            self.assertEqual(dirs, [])
            self.assertEqual(files, [])


class TestScopeWarn(unittest.TestCase):
    """同步根 ↔ 前缀 配对告警（离线判据）。"""

    def _w(self, **kw):
        from app.health import scope_warn
        base = dict(strm_prefix=PREFIX, remote_root="115影音")
        base.update(kw)
        return scope_warn(Config(**base))

    def test_配对正确不告警(self):
        self.assertEqual(self._w(), "")

    def test_末段不一致要告警(self):
        w = self._w(remote_root="115电影")            # 前缀末段还是 115影音
        self.assertIn("没配对", w)
        self.assertIn("115影音", w)
        self.assertIn("115电影", w)

    def test_前缀只到d要告警(self):
        w = self._w(strm_prefix="https://x/d")
        self.assertIn("挂载路径", w)

    def test_缺d段要告警(self):
        w = self._w(strm_prefix="https://x/影音/115影音")
        self.assertIn("/d/", w)

    def test_空前缀要告警(self):
        w = self._w(strm_prefix="")
        self.assertIn("还没配置", w)

    def test_多级前缀也认配对(self):
        self.assertEqual(self._w(strm_prefix="https://x/d/a/b/115影音"), "")


class TestTreeHelpers(unittest.TestCase):
    def test_resolve_相对树根(self):
        t = parse_text(REAL_TREE)
        self.assertEqual(t.resolve("115影音"), "根目录/115影音")
        self.assertEqual(t.resolve(""), "根目录")
        self.assertEqual(t.resolve("根目录/115影音"), "根目录/115影音")

    def test_resolve_不存在抛错(self):
        t = parse_text(REAL_TREE)
        with self.assertRaises(Exception):
            t.resolve("不存在")

    def test_has(self):
        t = parse_text(REAL_TREE)
        self.assertTrue(t.has("115影音"))
        self.assertFalse(t.has("没有这个"))

    def test_list_level(self):
        t = parse_text(REAL_TREE)
        items = t.list_level("115影音")
        names = [i["name"] for i in items]
        self.assertEqual(names, ["115电影", "115电视剧", "115短剧"])
        movie = next(i for i in items if i["name"] == "115电影")
        self.assertTrue(movie["has_children"])
        self.assertEqual(movie["files"], 0)            # 电影下面全是子目录

    def test_list_level_给相对树根的路径(self):
        t = parse_text(REAL_TREE)
        items = t.list_level("115影音/115电影")
        paths = [i["path"] for i in items]
        self.assertIn("115影音/115电影/功夫女足（2026）", paths)

    def test_subtree_stats(self):
        t = parse_text(REAL_TREE)
        st = t.subtree_stats("115影音/115电视剧")
        self.assertEqual(st["files"], 2)               # S01E01/S01E02
        self.assertEqual(st["dirs"], 1)                # 只有「灵魂摆渡·十年（2026）」

    def test_subtree_stats_电影子集(self):
        t = parse_text(REAL_TREE)
        st = t.subtree_stats("115影音/115短剧")
        self.assertEqual(st["files"], 1)


if __name__ == "__main__":
    unittest.main()
