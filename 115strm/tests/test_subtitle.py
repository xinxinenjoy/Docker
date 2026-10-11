"""外挂字幕的回归 —— 口径见 `app/subtitle.py` 顶部（含 VidHub 真机实测）。

分两层：
  ① **纯函数层**（`classify` / `plan_dir`）—— 配对与命名，不碰磁盘、不发请求
  ② **接入层**（`Syncer.run_subtitles`）—— 下载 / 跳过 / 失败不阻断 / 台账与删除闸门

🔴 钉死的两条关键结论（都是真机实测出来的，不是设计偏好）：
   · 本地名必须是 **`视频全名 + 字幕扩展名`**（`a.mkv.srt`）——
     VidHub 默认加载的就是这一份；`a.srt` 与 `a.mkv.strm.srt` **都不认**
   · 配不上的字幕**一律不下**（配错字幕比没字幕更糟），但**必须进回执**，
     不能因为默认跳过 `.sup` 就把它一起静默吃掉
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.config import Config
from app.namer import StrmBuilder
from app.subtitle import SubFile, classify, is_subtitle, parse_exts, plan_dir
from app.sync import Syncer
from app.treefile import parse_text

PREFIX = "https://alist.example.com:5244/d/媒体/影音"

# 真实结构（取自红领巾 2026-10-11 那棵树，缩到最少行）
TREE = """|——根目录
| |-影音
| | |-电影
| | | |-示例影片一（2025）
| | | | |-示例影片 Example Movie.mkv
| | | | |-示例影片 Example Movie.srt
| | | |-示例影片二（2022）
| | | | |-另一部片 Another Movie (2022).mp4
| | | | |-另一部片 Another Movie (2022).zh.srt
| | | |-示例影片三（2009）
| | | | |-Sample.Movie.2009.mkv
| | | | |-Sample.Movie.2009.mkv.zh.ass
| | | | |-Sample.Movie.2009.sup
| | | | |-另一部片 2025 精修简体中字 V2.sup
| | | |-只有字幕没有视频
| | | | |-孤零零.srt
| | |-电视剧
| | | |-某某剧（2026）
| | | | |-某某剧.S01E01.mkv
| | | | |-某某剧.S01E01.ass
| | | | |-某某剧.S01E01 精修简体中字.sup
"""


def cfg(tmp: Path, **kw) -> Config:
    base = dict(
        data_dir=tmp / "data", output_dir=tmp / "out",
        strm_prefix=PREFIX, url_encode=False,
        remote_root="影音", include_dirs="电影,电视剧", video_ext="mp4,mkv",
        delete_defer_minutes=10.0, delete_ratio_guard=0.3,
    )
    base.update(kw)
    c = Config(**base)
    Path(c.data_dir).mkdir(parents=True, exist_ok=True)
    Path(c.output_dir).mkdir(parents=True, exist_ok=True)
    return c


def builder() -> StrmBuilder:
    return StrmBuilder(prefix=PREFIX, alist_base="", encode=False)


def run_sync(s: Syncer, tree: str = TREE):
    """走一遍真实流程：树 → 配对 → run（含字幕阶段）。"""
    dirs, files = s.walk_from_tree(parse_text(tree))
    return s.run(remote_files=files, remote_dirs=dirs, source="tree")


class TestClassify(unittest.TestCase):
    """配对 + 命名口径（**纯函数**）。"""

    def test_同干(self):
        self.assertEqual(classify("a.mkv", "a.srt"),
                         ("a.mkv.srt", "①同干", ""))

    def test_同干_带语言尾(self):
        self.assertEqual(classify("a.mkv", "a.zh.srt"),
                         ("a.mkv.zh.srt", "②语言尾", "zh"))
        self.assertEqual(classify("a.mkv", "a.cht.ass"),
                         ("a.mkv.cht.ass", "②语言尾", "cht"))

    def test_带视频扩展名(self):
        self.assertEqual(classify("a.mkv", "a.mkv.zh.ass"),
                         ("a.mkv.zh.ass", "③带视频扩展名", "zh"))
        self.assertEqual(classify("a.mkv", "a.mkv.ass"),
                         ("a.mkv.ass", "③带视频扩展名", ""))

    def test_配不上(self):
        """🔴 真机样本：视频是英文名、字幕是中文译名 —— 必须**判死**，不许瞎配。"""
        got = classify("Another.Movie.2025.2160p.mkv",
                       "另一部片 2025 精修简体中字 V2.sup")
        self.assertEqual(got, (None, "⑤配不上", ""))

    def test_冷门语言尾也算语言尾(self):
        """🔴 真机踩到的：本盘 14 个「仅前缀」里 12 个其实是**冷门语种的 ISO 码**。

        `asm`(阿萨姆语) / `nld`(荷兰语) / `ind`(印尼语) / `glv`(曼岛语) /
        `mya`(缅甸语) / `pan`(旁遮普语) / `bos`(波斯尼亚语) —— 原来只列了十几个常见语言，
        这些全被扔进「配不上」，白丢 12 个字幕。
        """
        v = "Example.Show.S02E12.Some Episode Title.mkv"
        self.assertEqual(classify(v, v[:-4] + ".mya.ass"),
                         (v + ".mya.ass", "②语言尾", "mya"))
        self.assertEqual(classify("x.mkv", "x.asm.ass"), ("x.mkv.asm.ass", "②语言尾", "asm"))
        self.assertEqual(classify("x.mkv", "x.bos.ass"), ("x.mkv.bos.ass", "②语言尾", "bos"))

    def test_视频扩展名恰好是语言码时不许拼重(self):
        """`ts` 是聪加语、`as` 是阿萨姆语…… 视频扩展名撞上语言码时，
        必须先判「字幕干 == 视频全名」，否则 `a.ts` + `a.ts.srt` 会拼成 `a.ts.ts.srt`。"""
        self.assertEqual(classify("a.ts", "a.ts.srt"), ("a.ts.srt", "③带视频扩展名", ""))
        self.assertEqual(classify("a.mkv", "a.mkv.srt"), ("a.mkv.srt", "③带视频扩展名", ""))

    def test_同目录多视频时先配更具体的(self):
        """`a.mkv` 与 `a.4k.mkv` 同目录时，`a.4k.zh.srt` 该配给后者（名字更具体）。"""
        matched, _u, _s = plan_dir(
            "d", ["a.mkv", "a.4k.mkv"], ["a.4k.zh.srt"],
            builder=builder(), exts=parse_exts("srt"))
        self.assertEqual([m.video for m in matched], ["a.4k.mkv"])
        self.assertEqual([m.local_rel for m in matched], ["d/a.4k.mkv.zh.srt"])

    def test_真实样本(self):
        self.assertEqual(classify("示例影片 Example Movie.mkv", "示例影片 Example Movie.srt"),
                         ("示例影片 Example Movie.mkv.srt", "①同干", ""))

    def test_剧集(self):
        self.assertEqual(classify("某某剧.S01E01.mkv", "某某剧.S01E01.ass"),
                         ("某某剧.S01E01.mkv.ass", "①同干", ""))
        self.assertEqual(classify("某某剧.S01E01.mkv", "某某剧.S01E01.zh.srt"),
                         ("某某剧.S01E01.mkv.zh.srt", "②语言尾", "zh"))


class TestPlanDir(unittest.TestCase):
    def test_配上的才按设置跳_配不上的照样进报告(self):
        """🔴 这条是 2026-10-11 电影库实测踩到的形态：**配不上的那批恰好全是 `.sup`**。

        若把「跳过 .sup」放在配对**之前**，它们会被当成「按设置跳过」而**悄悄消失**，
        回执里再也看不到「有 N 个字幕配不上」。⇒ 配对先做、跳过后做。
        """
        vids = ["Sample.Movie.2009.mkv"]
        subs = ["Sample.Movie.2009.mkv.zh.ass", "Sample.Movie.2009.sup",
                "另一部片 2025 精修简体中字 V2.sup"]
        matched, unmatched, skipped = plan_dir(
            "电影/示例影片三（2009）", vids, subs,
            builder=builder(), exts=parse_exts("ass,srt,sup"),
            skip_exts=frozenset({"sup"}))
        self.assertEqual([m.local_rel for m in matched],
                         ["电影/示例影片三（2009）/Sample.Movie.2009.mkv.zh.ass"])
        # 配上了、但按设置跳过
        self.assertEqual([s for s, _r in skipped], ["Sample.Movie.2009.sup"])
        # 没配上的 ⇒ **必须进 unmatched**（哪怕它是 `.sup`）
        self.assertEqual([s for _v, s, _k in unmatched],
                         ["另一部片 2025 精修简体中字 V2.sup"])

    def test_跳过的只挡配上的(self):
        vids = ["a.mkv"]
        matched, _um, skipped = plan_dir(
            "d", vids, ["a.sup", "zzz.sup"],
            builder=builder(), exts=parse_exts("sup"),
            skip_exts=frozenset({"sup"}))
        self.assertEqual(matched, [])
        self.assertEqual([s for s, _r in skipped], ["a.sup"])

    def test_没视频的目录不产出(self):
        matched, unmatched, skipped = plan_dir(
            "d", [], ["孤零零.srt"], builder=builder(),
            exts=parse_exts("srt"), skip_exts=frozenset())
        self.assertEqual((matched, unmatched, skipped), ([], [], []))

    def test_url_与视频同一条链(self):
        matched, _u, _s = plan_dir("电影/x", ["a.mkv"], ["a.srt"],
                                   builder=builder(), exts=parse_exts("srt"))
        self.assertEqual(matched[0].url, f"{PREFIX}/电影/x/a.srt")


class TestWalkFromTree(unittest.TestCase):
    """走树时顺手配对 —— **零请求**。"""

    def test_配对结果落在实例上(self):
        with tempfile.TemporaryDirectory() as td:
            s = Syncer(None, cfg(Path(td)))
            _dirs, files = s.walk_from_tree(parse_text(TREE))
            rels = sorted(x.local_rel for x in s.tree_subs)
            self.assertEqual(rels, sorted([
                "电视剧/某某剧（2026）/某某剧.S01E01.mkv.ass",
                "电影/示例影片一（2025）/示例影片 Example Movie.mkv.srt",
                "电影/示例影片二（2022）/另一部片 Another Movie (2022).mp4.zh.srt",
                "电影/示例影片三（2009）/Sample.Movie.2009.mkv.zh.ass",
            ]))
            # 配不上的：另一部片「配不上」+ 剧集那个「仅前缀」（都只进报告，不下载）
            kinds = sorted(k for _v, _s, k in s.tree_sub_unmatched)
            self.assertEqual(kinds, ["④仅前缀", "⑤配不上"])
            self.assertEqual(
                sorted(x for _v, x, _k in s.tree_sub_unmatched),
                sorted(["某某剧.S01E01 精修简体中字.sup", "另一部片 2025 精修简体中字 V2.sup"]))
            # 配上了但按默认设置跳过 `.sup` 的
            self.assertEqual([x for x, _r in s.tree_sub_skipped], ["Sample.Movie.2009.sup"])
            self.assertEqual(len(files), 4, "视频照旧，不受字幕影响")

    def test_没视频的目录连字幕都不看(self):
        with tempfile.TemporaryDirectory() as td:
            s = Syncer(None, cfg(Path(td)))
            s.walk_from_tree(parse_text(TREE))
            self.assertFalse([x for x in s.tree_subs if "孤零零" in x.rel])


class TestSubtitlesRun(unittest.TestCase):
    """下载 / 跳过 / 失败 / 台账与删除。"""

    def test_默认关_一个请求都不发(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, cfg(tmp))          # subtitle_enable 出厂 False
            with mock.patch("app.subtitle.fetch_bytes") as fetch:
                res = run_sync(s)
            fetch.assert_not_called()
            self.assertFalse(res.subs_enabled)
            self.assertEqual(res.subs_downloaded, 0)
            self.assertEqual(res.subs_planned, 4, "照样要报「开了能下多少」")

    def test_开启后真下载_且命名按口径(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, cfg(tmp, subtitle_enable=True))
            with mock.patch("app.subtitle.fetch_bytes",
                            return_value=b"1\n00:00:01,000 --> 00:00:02,000\nhi\n") as fetch:
                res = run_sync(s)
            self.assertEqual(fetch.call_count, 4)
            self.assertEqual(res.subs_downloaded, 4)
            self.assertEqual(res.subs_failed, 0)
            base = tmp / "out" / "电影"
            self.assertTrue((base / "示例影片一（2025）" / "示例影片 Example Movie.mkv.srt").is_file())
            self.assertTrue((base / "示例影片二（2022）" /
                             "另一部片 Another Movie (2022).mp4.zh.srt").is_file())
            self.assertTrue((base / "示例影片三（2009）" / "Sample.Movie.2009.mkv.zh.ass").is_file())
            self.assertTrue((tmp / "out" / "电视剧" / "某某剧（2026）" /
                             "某某剧.S01E01.mkv.ass").is_file())
            # ⛔ 那两个名字「不带视频扩展名」的形态**绝不能**出现
            self.assertFalse((base / "示例影片一（2025）" / "示例影片 Example Movie.srt").exists())
            self.assertFalse((base / "示例影片一（2025）" /
                              "示例影片 Example Movie.mkv.strm.srt").exists())

    def test_本地已有则跳过_零请求(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            keep = tmp / "out" / "电影" / "示例影片一（2025）" / "示例影片 Example Movie.mkv.srt"
            keep.parent.mkdir(parents=True, exist_ok=True)
            keep.write_bytes(b"already here")
            s = Syncer(None, cfg(tmp, subtitle_enable=True))
            with mock.patch("app.subtitle.fetch_bytes", return_value=b"new") as fetch:
                res = run_sync(s)
            self.assertEqual(res.subs_skipped, 1)
            self.assertEqual(fetch.call_count, 3, "已有那个不该再发请求")
            self.assertEqual(keep.read_bytes(), b"already here", "不许覆盖已有文件")

    def test_不碰strm(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, cfg(tmp, subtitle_enable=True))
            with mock.patch("app.subtitle.fetch_bytes", return_value=b"sub"):
                res = run_sync(s)
            self.assertEqual(res.written, 4, "4 个视频照旧写 strm")
            p = tmp / "out" / "电影" / "示例影片一（2025）" / "示例影片 Example Movie.mkv.strm"
            self.assertEqual(p.read_bytes(), f"{PREFIX}/电影/示例影片一（2025）/"
                                             "示例影片 Example Movie.mkv".encode())

    def test_下载失败不阻断主流程(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)

            def boom(url, **_kw):
                if "Example Movie" in url:
                    raise OSError("boom")
                return b"ok"

            s = Syncer(None, cfg(tmp, subtitle_enable=True))
            with mock.patch("app.subtitle.fetch_bytes", side_effect=boom):
                res = run_sync(s)
            self.assertEqual(res.subs_failed, 1)
            self.assertEqual(res.subs_downloaded, 3)
            self.assertEqual(res.written, 4, "strm 照旧全写完 —— 字幕失败**不许**影响它")

    def test_开关没开只报数不下(self):
        """出厂是**关**的 ⇒ 只报「本来能下多少」，一个请求都不发。

        ⚠️ 这条原来是「演练只算不下」—— `dry_run` 2026-10-11 删掉后，
           它守的那件事由开关这条线继续守着（`subtitle_enable` 出厂 False）。
           判据仍是**零请求**：`fetch_bytes` 一次都不许被调用。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, cfg(tmp, subtitle_enable=False))
            with mock.patch("app.subtitle.fetch_bytes") as fetch:
                res = run_sync(s)
            fetch.assert_not_called()
            self.assertFalse(res.subs_enabled)
            self.assertEqual(res.subs_downloaded, 0)
            self.assertEqual(res.subs_planned, 4)


class TestSubsLedgerAndDelete(unittest.TestCase):
    """台账 + 删除闸门 —— 删除是本工具唯一不可逆的动作。"""

    TREE_NO_SUB = TREE.replace("| | | | |-示例影片 Example Movie.srt\n", "")

    def test_台账记下写过的份(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, cfg(tmp, subtitle_enable=True))
            with mock.patch("app.subtitle.fetch_bytes", return_value=b"sub"):
                run_sync(s)
            led = json.loads((tmp / "data" / "subs-managed.json").read_text("utf-8"))
            self.assertEqual(len(led["items"]), 4)
            self.assertIn("电影/示例影片一（2025）/示例影片 Example Movie.mkv.srt", led["items"])

    def test_网盘删了字幕_要过延迟确认才删(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, cfg(tmp, subtitle_enable=True, delete_ratio_guard=1.0,
                                 delete_defer_minutes=0.0))
            with mock.patch("app.subtitle.fetch_bytes", return_value=b"sub"):
                run_sync(s)
            target = tmp / "out" / "电影" / "示例影片一（2025）" / "示例影片 Example Movie.mkv.srt"
            self.assertTrue(target.exists())

            dirs, files = s.walk_from_tree(parse_text(self.TREE_NO_SUB))
            res = s.run(remote_files=files, remote_dirs=dirs, source="tree")
            self.assertEqual(res.subs_deleted, 0, "第一次只挂起")
            self.assertEqual(res.subs_pending_delete, 1)
            self.assertTrue(target.exists())

            dirs, files = s.walk_from_tree(parse_text(self.TREE_NO_SUB))
            res = s.run(remote_files=files, remote_dirs=dirs, source="tree")
            self.assertEqual(res.subs_deleted, 1, "二次确认到点了才删")
            self.assertFalse(target.exists())

    def test_异常比例闸门_一条都不删(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, cfg(tmp, subtitle_enable=True, delete_ratio_guard=0.3,
                                 delete_defer_minutes=0.0))
            with mock.patch("app.subtitle.fetch_bytes", return_value=b"sub"):
                run_sync(s)
            dirs, files = s.walk_from_tree(parse_text(self.TREE_NO_SUB))
            res = s.run(remote_files=files, remote_dirs=dirs, source="tree")
            # 4 个里少了 1 个 = 25% ≤ 30% ⇒ 不触发；这里把阈值压到极小才触发
            self.assertFalse(res.subs_guard_tripped)

            s2 = Syncer(None, cfg(tmp, subtitle_enable=True, delete_ratio_guard=0.01,
                                  delete_defer_minutes=0.0))
            dirs, files = s2.walk_from_tree(parse_text(self.TREE_NO_SUB))
            res = s2.run(remote_files=files, remote_dirs=dirs, source="tree")
            self.assertTrue(res.subs_guard_tripped)
            self.assertEqual(res.subs_deleted, 0)
            self.assertTrue((tmp / "out" / "电影" / "示例影片一（2025）" /
                             "示例影片 Example Movie.mkv.srt").exists())

    def test_你自己换过的字幕不删(self):
        """第二道闸：文件内容已经不是我们写下的那份 ⇒ **保留**（你换了更好的字幕）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, cfg(tmp, subtitle_enable=True, delete_ratio_guard=1.0,
                                 delete_defer_minutes=0.0))
            with mock.patch("app.subtitle.fetch_bytes", return_value=b"sub"):
                run_sync(s)
            target = tmp / "out" / "电影" / "示例影片一（2025）" / "示例影片 Example Movie.mkv.srt"
            mine = "我自己换的一份更好的字幕".encode("utf-8")
            target.write_bytes(mine)

            for _ in range(2):
                dirs, files = s.walk_from_tree(parse_text(self.TREE_NO_SUB))
                res = s.run(remote_files=files, remote_dirs=dirs, source="tree")
            self.assertTrue(target.exists(), "内容变了就不该删")
            self.assertEqual(target.read_bytes(), mine)
            self.assertTrue(any("你自己换过" in n for n in res.subs_notes), res.subs_notes)


class TestRemoveSubs(unittest.TestCase):
    """删的边界：只删字幕扩展名、且只在 output_dir 里面。"""

    def test_只删字幕扩展名(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, cfg(tmp))
            (tmp / "out" / "a.strm").write_text("x", encoding="utf-8")
            (tmp / "out" / "b.srt").write_text("x", encoding="utf-8")
            n = s.remove_subs(["a.strm", "b.srt"])
            self.assertEqual(n, 1)
            self.assertTrue((tmp / "out" / "a.strm").exists())
            self.assertFalse((tmp / "out" / "b.srt").exists())

    def test_越界挡掉(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            outside = tmp / "outside.srt"
            outside.write_text("x", encoding="utf-8")
            s = Syncer(None, cfg(tmp))
            s.remove_subs(["../outside.srt", "sub/../../outside.srt"])
            self.assertTrue(outside.exists())


class TestLangTags(unittest.TestCase):
    """🔴 语言码表**是手敲的，敲漏过 `eng`** —— 症状是「英语字幕整批掉进配不上」，极隐蔽。

    （2026-10-11 实测：漏掉 `eng` 之后本盘 28 个 `.eng.sub/.idx` 全被判「配不上」。）
    """

    COMMON = ("eng", "en", "chi", "zho", "chs", "cht", "zh", "jpn", "jp", "kor",
              "fre", "fra", "ger", "deu", "spa", "ita", "rus", "por", "tha", "vie",
              "ara", "hin", "ind", "may", "msa", "nld", "dut", "swe", "nor", "dan",
              "fin", "ces", "cze", "hun", "ell", "gre", "heb", "pol", "tur",
              "asm", "glv", "mya", "bur", "pan", "bos")   # 本盘实测出现过的冷门码

    def test_关键语言码没漏(self):
        from app.subtitle import LANG_TAGS
        missing = [t for t in self.COMMON if t not in LANG_TAGS]
        self.assertEqual(missing, [], f"这些语言码不在表里 ⇒ 对应字幕会被判「配不上」：{missing}")

    def test_中文写法也在(self):
        from app.subtitle import LANG_TAGS
        for t in ("简体", "繁体", "简中", "繁中", "粤语官中", "中英", "双语", "特效"):
            self.assertIn(t, LANG_TAGS, t)


class TestConfigHelpers(unittest.TestCase):
    def test_扩展名解析(self):
        self.assertEqual(parse_exts(" ass, .SRT ,"), frozenset({"ass", "srt"}))
        self.assertEqual(Config(subtitle_ext="ass,srt").sub_exts, frozenset({"ass", "srt"}))

    def test_sup开关(self):
        self.assertEqual(Config(subtitle_skip_sup=True).sub_skip_exts, frozenset({"sup"}))
        self.assertEqual(Config(subtitle_skip_sup=False).sub_skip_exts, frozenset())

    def test_is_subtitle(self):
        self.assertTrue(is_subtitle("a.SRT", parse_exts("srt")))
        self.assertFalse(is_subtitle("a.mkv", parse_exts("srt")))
        self.assertFalse(is_subtitle("无扩展名", parse_exts("srt")))


class TestSubFileShape(unittest.TestCase):
    def test_字段(self):
        sf = SubFile(rel="d/a.srt", local_rel="d/a.mkv.srt",
                     url="u", video="a.mkv", kind="①同干")
        self.assertEqual(sf.lang, "")
        self.assertEqual(sf.kind, "①同干")
