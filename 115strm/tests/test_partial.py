"""手动补录（`walk_remote_sub` + `run(add_only=True)`）的回归。

红领巾 2026-11-17 的需求：

    「临时新加电影或者电视剧的时候我需要手动将新的这些加进来，
      不然每次都全量导出，官方生成目录树很慢（这个还需要保留，每天保底刷新）」

⇒ 全量那轮不动，另加一条「只看他点的那几个目录」的快捷路。**只增不删**。

这一组用例盯的就是「别把库删了」：
  · 局部清单 + 全库本地现状 ⇒ 若不按范围筛，其余几千条全会被判「网盘上没了」
  · 即便按范围筛了，范围内对不上的也**一条都不删**
  · 不 prune 空目录、不碰挂起清单、不删本地字幕
以及路径校验（同步根 / 白名单 / 目录上限）—— 那三条都是「宁可拒绝也不猜」。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.config import Config
from app.namer import StrmFile
from app.sync import Syncer, _PARTIAL_DIR_CAP
from app.v115 import Node, V115DirNotFound, V115Error

PREFIX = "https://alist.example.com:5244/d/媒体/影音"


def make_cfg(tmp: Path, **kw) -> Config:
    base = dict(
        data_dir=tmp / "data", output_dir=tmp / "out",
        strm_prefix=PREFIX, url_encode=False,
        remote_root="影音", include_dirs="",
        video_ext="mp4,mkv",
        delete_ratio_guard=0.3,
    )
    base.update(kw)
    cfg = Config(**base)
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.output_dir).mkdir(parents=True, exist_ok=True)
    return cfg


class _Reqs:
    """`V115.reqs` 的最小桩（`run_partial` 结尾要取请求明细）。"""

    def add(self, *a, **k) -> None:
        return None

    def summary(self) -> dict:
        return {"total": 0, "by_name": [], "items": [],
                "request_ms": 0, "wait_ms": 0, "failed": 0}


class FakeTreeV:
    """能**递归**列目录的假 115（`test_sync.FakeV` 只够复核用，不能递归）。

    `tree` = `{相对网盘根的目录路径: [(名字, 是否目录), ...]}`
    目录的 cid 直接编码成 `cid:<路径>`，这样 `list_dir` 能反解回来，
    也就不用维护一张 id 表（测试里 id 本身毫无意义）。
    """

    def __init__(self, tree: dict[str, list[tuple[str, bool]]], *,
                 root: str = "影音", includes: tuple[str, ...] = ()):
        self.tree = tree
        self._root = root
        self._incs = includes
        self.listed: list[str] = []
        self.last_list_complete = True
        self.stage = ""
        self.reqs = _Reqs()

    # --- Syncer / walk_remote_sub 用到的接口 ---
    def root_id(self) -> str:
        return "cid:" + self._root

    def dir_id(self, path: str) -> str:
        path = (path or "").strip("/")
        if path not in self.tree:
            raise V115DirNotFound(f"115 上找不到目录：{path}")
        return "cid:" + path

    def list_dir(self, cid: str, dirs_only: bool = False) -> list[Node]:
        path = cid[4:] if cid.startswith("cid:") else ""
        self.listed.append(path)
        out: list[Node] = []
        for name, is_dir in self.tree.get(path, []):
            if is_dir:
                child = f"{path}/{name}" if path else name
                out.append(Node(id="cid:" + child, name=name, is_dir=True))
            else:
                out.append(Node(id="fid:" + name, name=name, is_dir=False, size=1024))
        return out

    def to_sync_rel(self, full: str) -> str | None:
        full = (full or "").strip("/")
        root = self._root.strip("/")
        if not root:
            return full
        if full == root:
            return ""
        if full.startswith(root + "/"):
            return full[len(root) + 1:]
        return None

    def in_scope(self, rel: str) -> bool:
        if not self._incs:
            return True
        rel = (rel or "").strip("/")
        return any(rel == i or rel.startswith(i + "/") for i in self._incs)


# 一棵最小的真实形态树：同步根 `影音` 下有 电影 / 电视剧 / 云下载（不在范围里）
TREE: dict[str, list[tuple[str, bool]]] = {
    "": [("影音", True)],
    "影音": [("电影", True), ("电视剧", True), ("云下载", True)],
    "影音/电影": [("片A（2026）", True)],
    "影音/电影/片A（2026）": [("a.mkv", False), ("a.zh.srt", False)],
    "影音/电视剧": [("某剧（2026）", True)],
    "影音/电视剧/某剧（2026）": [("S01", True)],
    "影音/电视剧/某剧（2026）/S01": [("E01.mkv", False), ("E02.mkv", False)],
    "影音/云下载": [("b.mkv", False)],
}


def make_syncer(tmp: Path, *, includes: str = "", **kw) -> tuple[Syncer, FakeTreeV]:
    cfg = make_cfg(tmp, include_dirs=includes, **kw)
    v = FakeTreeV(TREE, includes=tuple(cfg.includes))
    return Syncer(v, cfg), v


class TestWalkRemoteSub(unittest.TestCase):
    def test_列一部电影(self):
        with tempfile.TemporaryDirectory() as td:
            s, v = make_syncer(Path(td))
            dirs, files, info = s.walk_remote_sub(["影音/电影/片A（2026）"])
            # 相对**同步根**的路径（不是相对网盘根）—— 与走目录树那条路同一个口径
            self.assertEqual([x.rel for x in files], ["电影/片A（2026）/a.mkv"])
            self.assertEqual(info["bases"], ["电影/片A（2026）"])
            self.assertEqual(info["dir_count"], 0)
            # 字幕不生成 strm（它走另一条链路）
            self.assertFalse(any(x.rel.endswith(".srt.strm") for x in files))

    def test_列一季剧会带子目录(self):
        with tempfile.TemporaryDirectory() as td:
            s, v = make_syncer(Path(td))
            dirs, files, info = s.walk_remote_sub(["影音/电视剧/某剧（2026）"])
            self.assertEqual(sorted(x.rel for x in files),
                             ["电视剧/某剧（2026）/S01/E01.mkv",
                              "电视剧/某剧（2026）/S01/E02.mkv"])
            self.assertEqual(dirs, ["电视剧/某剧（2026）/S01"])
            self.assertEqual(info["dir_count"], 1)

    def test_容忍相对同步根的写法(self):
        """`电影/片A（2026）` 也要认 —— 人不会记得自己同步根叫什么。"""
        with tempfile.TemporaryDirectory() as td:
            s, _ = make_syncer(Path(td))
            _d, files, info = s.walk_remote_sub(["电影/片A（2026）"])
            self.assertEqual([x.rel for x in files], ["电影/片A（2026）/a.mkv"])
            self.assertEqual(info["bases"], ["电影/片A（2026）"])

    def test_单个视频文件也认(self):
        with tempfile.TemporaryDirectory() as td:
            s, _ = make_syncer(Path(td))
            _d, files, info = s.walk_remote_sub(["影音/云下载/b.mkv"])
            self.assertEqual([x.rel for x in files], ["云下载/b.mkv"])
            # 文件没有「自己的目录」，base 取它的父目录
            self.assertEqual(info["bases"], ["云下载"])

    def test_不在同步根下要报错(self):
        """⚠️ 那条路**永远补不出正确地址**，报错里必须点明只认同步根以下。"""
        with tempfile.TemporaryDirectory() as td:
            s, _ = make_syncer(Path(td))
            with self.assertRaises(V115Error) as ctx:
                s.walk_remote_sub(["别人的目录/片B"])
            msg = str(ctx.exception)
            self.assertIn("同步根", msg)
            self.assertIn("115 上找不到", msg)

    def test_不在白名单要报错(self):
        """⛔ 别静默忽略：不按范围补录，本地结构会跟每日全量那轮对不上。"""
        with tempfile.TemporaryDirectory() as td:
            s, _ = make_syncer(Path(td), includes="电影")
            with self.assertRaises(V115Error) as ctx:
                s.walk_remote_sub(["影音/云下载"])
            self.assertIn("同步范围", str(ctx.exception))

    def test_目录数超上限要停手(self):
        """实时列目录 = 每个目录一个请求 ⇒ 选太大范围就变成几十分钟。"""
        with tempfile.TemporaryDirectory() as td:
            s, _ = make_syncer(Path(td))
            with self.assertRaises(V115Error) as ctx:
                s.walk_remote_sub(["影音"], cap=2)
            self.assertIn("目录太多", str(ctx.exception))

    def test_上限是跨子树共享的(self):
        """勾若干个目录时，上限必须**合计**算 —— 按每棵各算等于没有上限。"""
        with tempfile.TemporaryDirectory() as td:
            s, _ = make_syncer(Path(td))
            # 每棵子树 1 个目录；给 cap=2 ⇒ 第三棵就该被拦下
            with self.assertRaises(V115Error):
                s.walk_remote_sub(["影音/电影", "影音/电视剧", "影音/电影/片A（2026）"],
                                  cap=2)

    def test_空入参要报错(self):
        with tempfile.TemporaryDirectory() as td:
            s, _ = make_syncer(Path(td))
            with self.assertRaises(V115Error):
                s.walk_remote_sub(["  ", "/"])


class TestAddOnly(unittest.TestCase):
    """🔴 这一组的判据只有一句：**补录一条都不许删**。"""

    def _seed(self, out: Path) -> None:
        # 范围内：一个该保留的、一个「网盘上对不上」的
        (out / "电影" / "片A（2026）").mkdir(parents=True, exist_ok=True)
        (out / "电影" / "片A（2026）" / "a.mkv.strm").write_text(
            PREFIX + "/电影/片A（2026）/a.mkv", encoding="utf-8")
        (out / "电影" / "片A（2026）" / "对不上.mkv.strm").write_text(
            PREFIX + "/电影/片A（2026）/对不上.mkv", encoding="utf-8")
        # 范围外：几千条里的两条（局部清单里根本没有它们）
        (out / "电视剧" / "别的剧").mkdir(parents=True, exist_ok=True)
        (out / "电视剧" / "别的剧" / "x.mkv.strm").write_text(
            PREFIX + "/电视剧/别的剧/x.mkv", encoding="utf-8")
        # 一个空壳目录（全量那轮会收掉，补录这趟不许碰）
        (out / "空壳").mkdir(parents=True, exist_ok=True)

    def test_只增不删(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s, _ = make_syncer(tmp)
            out = tmp / "out"
            self._seed(out)

            remote = [StrmFile(rel="电影/片A（2026）/a.mkv",
                               url=PREFIX + "/电影/片A（2026）/a.mkv")]
            res = s.run(remote_files=remote, remote_dirs=[], source="partial",
                        add_only=True, local_scope=["电影/片A（2026）"])

            self.assertTrue(res.partial)
            self.assertEqual(res.deleted, 0)
            self.assertEqual(res.written, 0)
            self.assertEqual(res.unchanged, 1)
            # 范围内那条对不上的：记账但**不删**
            self.assertEqual(res.delete_suppressed, 1)
            self.assertEqual(res.delete_suppressed_sample, ["电影/片A（2026）/对不上.mkv.strm"])
            self.assertTrue((out / "电影" / "片A（2026）" / "对不上.mkv.strm").exists())
            # 范围外的原样不动
            self.assertTrue((out / "电视剧" / "别的剧" / "x.mkv.strm").exists())
            # 回执的 delete 明细必须是空的 —— 否则「删 N」与「deleted=0」摆一起像坏了
            self.assertEqual(res.diff["delete"], [])
            self.assertEqual(res.diff["counts"]["delete"], 0)

    def test_不收空目录(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s, _ = make_syncer(tmp)
            out = tmp / "out"
            (out / "空壳" / "更深").mkdir(parents=True, exist_ok=True)
            res = s.run(remote_files=[], remote_dirs=[], source="partial",
                        add_only=True, local_scope=["电影"])
            self.assertEqual(res.dirs_pruned, 0)
            self.assertTrue((out / "空壳" / "更深").is_dir())

    def test_不碰挂起清单(self):
        """局部一趟没发现删除 ≠ 全库恢复正常 ⇒ ⛔ 别清别人的挂起清单。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s, _ = make_syncer(tmp)
            p = tmp / "data" / "pending-delete.json"
            p.write_text(json.dumps({"paths": ["某/条.strm"], "first_seen": 1,
                                     "seen_times": 1, "reason": "异常"}), encoding="utf-8")
            s.run(remote_files=[], remote_dirs=[], source="partial",
                  add_only=True, local_scope=["电影"])
            self.assertTrue(p.exists())

    def test_范围外的不参与对账(self):
        """范围外的本地 strm 连「本地已有」都不该算进去 —— 否则数字会骗人。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s, _ = make_syncer(tmp)
            out = tmp / "out"
            self._seed(out)
            res = s.run(remote_files=[], remote_dirs=[], source="partial",
                        add_only=True, local_scope=["电影/片A（2026）"])
            self.assertEqual(res.scanned_local, 2)

    def test_同步根本身不做前缀筛选(self):
        """`local_scope=[""]` = 同步根 ⇒ 不能筛成空（`startswith("/")` 恒假的坑）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s, _ = make_syncer(tmp)
            out = tmp / "out"
            self._seed(out)
            res = s.run(remote_files=[], remote_dirs=[], source="partial",
                        add_only=True, local_scope=[""])
            self.assertEqual(res.scanned_local, 3)     # 3 个 strm，空壳不算

    def test_全量轮仍然照旧判删除(self):
        """补录不能把全量那条路带坏：`add_only=False` 时**候选删除照常算出来**。

        ⚠️ 这里网盘返回空 ⇒ 消失比例 100% ⇒ **异常闸门**本来就该拦下（既有口径，
           判据见 `_handle_delete`；真删的那套由 `test_sync.py` 覆盖）。
           所以断言的是「候选被算出来了、`partial` 没被误设」，
           ⛔ 不是「真的删了 3 个」—— 那样等于绕过闸门。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s, _ = make_syncer(tmp)
            out = tmp / "out"
            self._seed(out)
            res = s.run(remote_files=[], remote_dirs=[], source="tree")
            self.assertFalse(res.partial)
            self.assertEqual(res.deleted, 0)
            self.assertTrue(res.guard_tripped)
            self.assertEqual(len(res.diff["delete"]), 3)
            # 没被 add_only 的清空逻辑误伤（那条逻辑只在补录时生效）
            self.assertEqual(res.delete_suppressed, 0)

    def test_补录也会补上本地没有的(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s, _ = make_syncer(tmp)
            remote = [StrmFile(rel="电视剧/某剧（2026）/S01/E01.mkv",
                               url=PREFIX + "/电视剧/某剧（2026）/S01/E01.mkv")]
            res = s.run(remote_files=remote, remote_dirs=["电视剧/某剧（2026）/S01"],
                        source="partial", add_only=True,
                        local_scope=["电视剧/某剧（2026）"])
            self.assertEqual(res.written, 1)
            p = tmp / "out" / "电视剧" / "某剧（2026）" / "S01" / "E01.mkv.strm"
            self.assertTrue(p.exists())
            self.assertEqual(p.read_text(encoding="utf-8"), remote[0].url)


class TestRunPartialWiring(unittest.TestCase):
    """`pipeline.run_partial` 的组装 —— 这条才是网页 / 命令行真正按下去的那条路。

    ⚠️ 把「建客户端」三步全部打桩（`load_cookie` / `build_client` / `V115`），
       否则这条用例会真联网、真往 115 发请求。
    """

    def test_端到端只增不删(self):
        from unittest import mock

        from app import pipeline as P

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = make_cfg(tmp)
            out = tmp / "out"
            (out / "电视剧" / "旧剧").mkdir(parents=True, exist_ok=True)
            (out / "电视剧" / "旧剧" / "x.mkv.strm").write_text(
                PREFIX + "/电视剧/旧剧/x.mkv", encoding="utf-8")

            v = FakeTreeV(TREE, includes=tuple(cfg.includes))
            with mock.patch.object(P, "load_cookie", return_value="UID=1_A1_1"), \
                 mock.patch.object(P, "build_client", return_value=object()), \
                 mock.patch.object(P, "V115", return_value=v):
                o = P.run_partial(cfg, lambda *a, **k: None,
                                  ["影音/电影/片A（2026）"], sleeper=lambda s: None)

            self.assertEqual(o.source, "partial")
            res = o.res
            self.assertTrue(res.partial)
            self.assertEqual(res.partial_paths, ["电影/片A（2026）"])
            self.assertEqual(res.partial_dirs, 0)
            self.assertEqual(res.written, 1)
            self.assertEqual(res.deleted, 0)
            self.assertTrue((out / "电影" / "片A（2026）" / "a.mkv.strm").exists())
            # 范围外的老文件一个都不能少
            self.assertTrue((out / "电视剧" / "旧剧" / "x.mkv.strm").exists())
            self.assertEqual(o.detail["paths"], ["电影/片A（2026）"])
            # 耗时三段都要有值（前端「耗时拆开」那行读它们）
            self.assertGreaterEqual(res.elapsed_total_ms, 0)
            self.assertTrue(res.elapsed_total)

    def test_空前缀要拦下(self):
        from app import pipeline as P

        with tempfile.TemporaryDirectory() as td:
            cfg = make_cfg(Path(td), strm_prefix="")
            with self.assertRaises(ValueError) as ctx:
                P.run_partial(cfg, lambda *a, **k: None, ["影音/电影"])
            self.assertIn("播放地址", str(ctx.exception))


class TestCapConstant(unittest.TestCase):
    def test_上限是有限的正数(self):
        """⚠️ 别把它调成 0/None（会和 `walk_remote` 的 `cap is not None` 打架）。"""
        self.assertIsInstance(_PARTIAL_DIR_CAP, int)
        self.assertGreater(_PARTIAL_DIR_CAP, 0)


if __name__ == "__main__":
    unittest.main()
