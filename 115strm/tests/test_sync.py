"""同步引擎的回归用例 —— **重点在删除判定**。

删除是本工具唯一不可逆的动作，所以用例要覆盖：
  · 正常情况：新增能落盘、未变的不重写（保住 mtime）
  · **当轮复核即删**（2026-10-20 起）：发现消失 ⇒ 同一轮里复核网盘 ⇒ 确认不在就删
  · 复核说「还在」/ 查不了 ⇒ **保留本地**（宁可多留一轮，不可误删）
  · 整目录改名 ⇒ 父目录已不存在也算「确认不在」（旧版这里会永远删不掉）
  · 🔴 异常闸门：消失比例超阈值 ⇒ **一条都不删**（cookie 失效的典型症状）
  · 变动面过大（候选散在几十个目录）⇒ 不逐个复核，整批挂起等他确认
  · 边界：只删 .strm，绝不碰别的文件、绝不越界到 output 之外
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from app.config import Config
from app.namer import StrmFile
from app.sync import Pending, Syncer, _RECHECK_DIR_CAP
from app.v115 import Node, V115DirNotFound, V115Error


def make_cfg(tmp: Path, **kw) -> Config:
    """造一份指向临时目录的配置（cookie 留空，测试不联网）。"""
    base = dict(
        data_dir=tmp / "data",
        output_dir=tmp / "out",
        strm_prefix="https://example.test/d",
        url_encode=False,
        delete_ratio_guard=0.3,
        video_ext="mp4,mkv",
    )
    base.update(kw)
    cfg = Config(**base)
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.output_dir).mkdir(parents=True, exist_ok=True)
    return cfg


class FakeV:
    """只够 `Syncer._recheck` 用的假 115（真接口那层另有 `test_v115*` 覆盖）。

    `tree` = `{父目录(相对网盘根, 不带首尾斜杠): [文件名, ...]}`
    """

    def __init__(self, tree: dict[str, list[str]] | None = None, *,
                 missing: tuple = (), broken: tuple = (), complete: bool = True,
                 root_broken: bool = False):
        self.tree = tree or {}
        self.missing = set(missing)      # 网盘上**真没有**的目录
        self.broken = set(broken)        # 接口报错的目录
        self.complete = complete         # False = 目录太大没列完
        self.root_broken = root_broken   # 连同步根都解析不出来
        self.last_list_complete = True
        self.listed: list[str] = []      # 列过哪些目录（= 花了几次请求）

    def root_id(self) -> str:
        if self.root_broken:
            raise V115Error("fs_dir_getid 被 115 拒绝：cookie 失效")
        return "cid:"          # 去掉前缀就是 "" ⇒ 对应「网盘根」那一层

    def dir_id(self, path: str) -> str:
        path = (path or "").strip("/")
        if path in self.missing:
            raise V115DirNotFound(f"115 上找不到目录：{path}")
        if path in self.broken:
            raise V115Error("fs_dir_getid 被 115 拒绝：接口抽风")
        return "cid:" + path

    def list_dir(self, cid: str, dirs_only: bool = False) -> list[Node]:
        path = cid[4:] if cid.startswith("cid:") else ""
        self.listed.append(path)
        self.last_list_complete = self.complete
        return [Node(id="f", name=x, is_dir=False) for x in self.tree.get(path, [])]


def f(rel: str, fid: str = "") -> StrmFile:
    return StrmFile(rel=rel, url=f"https://example.test/d/{rel}", fid=fid)


class TestWrite(unittest.TestCase):
    def test_新增会落盘(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            res = s.run(remote_files=[f("a/b.mkv")], remote_dirs=["a"], source="tree")
            self.assertEqual(res.written, 1)
            p = tmp / "out" / "a" / "b.mkv.strm"
            self.assertTrue(p.exists())
            self.assertEqual(p.read_text(encoding="utf-8").strip(),
                             "https://example.test/d/a/b.mkv")

    def test_末尾不加换行(self):
        """🔴 2026-10-11：strm 内容必须是**裸 URL**，末尾不留 `\\n`。

        起因：VidHub 会把结尾的换行当成 URL 的一部分 ⇒ 「文件在、却播不了」
        （红领巾实测删掉换行即正常；VidHub 官方称下个版本修复）。
        其余播放器（Infuse / Emby / Jellyfin）本就会 trim ⇒ 去掉无影响。

        ⚠️ 断言用**字节级**比对 —— 上面那条 `test_新增会落盘` 是 `.strip()` 比对的，
           对末尾空白**不敏感**，挡不住这个回归，所以这里单独钉死。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            s.run(remote_files=[f("a.mkv")], remote_dirs=[], source="tree")
            p = tmp / "out" / "a.mkv.strm"
            self.assertEqual(p.read_bytes(), b"https://example.test/d/a.mkv")

    def test_内容未变不重写(self):
        """⚠️ 重写会刷新 mtime ⇒ 媒体库每次都当新文件重扫。必须跳过。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = make_cfg(tmp)
            s = Syncer(None, cfg)
            s.run(remote_files=[f("a.mkv")], remote_dirs=[], source="tree")
            p = tmp / "out" / "a.mkv.strm"
            before = p.stat().st_mtime_ns
            time.sleep(0.01)
            res = s.run(remote_files=[f("a.mkv")], remote_dirs=[], source="tree")
            self.assertEqual(res.written, 0)
            self.assertEqual(res.unchanged, 1)
            self.assertEqual(p.stat().st_mtime_ns, before)

    def test_内容变了才重写(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            path = tmp / "out" / "a.mkv.strm"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("https://old/d/a.mkv\n", encoding="utf-8")
            res = s.run(remote_files=[f("a.mkv")], remote_dirs=[], source="tree")
            self.assertEqual(res.written, 1)
            self.assertIn("example.test", path.read_text(encoding="utf-8"))

    def test_空目录也建(self):
        """网盘有但本地缺的目录要建出来 —— 这样才看得出缺失。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            s.run(remote_files=[], remote_dirs=["空目录/里面也有"], source="tree")
            self.assertTrue((tmp / "out" / "空目录" / "里面也有").is_dir())

    def test_文件名非法字符被替换(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            s.run(remote_files=[f("a:b.mkv")], remote_dirs=[], source="tree")
            self.assertTrue((tmp / "out" / "a：b.mkv.strm").exists())

    def test_原子写不留tmp(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            s.run(remote_files=[f("a.mkv")], remote_dirs=[], source="tree")
            left = list((tmp / "out").rglob("*.tmp"))
            self.assertEqual(left, [])


class TestDiffPayload(unittest.TestCase):
    """差异**明细**必须落进回执 —— 网页「记录」点开显示的就是它。

    🔴 2026-10-11：以前这份明细**只有「只演练」那条路会填**（`run()` 里那个分支），
       真跑只写 `{"counts": …}` ⇒ 于是记录里只能看到「写了几个」，
       看不到「写的是哪几个」。演练模式删掉后，明细改由真跑这条路提供
       （前端 `renderResList()` 读的就是 `res.diff.add/update/delete/renamed`）。
    """

    def test_新增的明细进回执(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            res = Syncer(None, make_cfg(tmp)).run(
                remote_files=[f("a/b.mkv")], remote_dirs=["a"], source="tree")
            self.assertEqual(res.diff["add"], ["a/b.mkv.strm"])
            self.assertEqual(res.diff["counts"]["add"], 1)

    def test_删除的明细进回执(self):
        """没有 `V115`（离线）时照用清单 ⇒ **当轮就删**，明细照样要能看到它。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "out").mkdir(parents=True, exist_ok=True)
            for i in range(10):
                (tmp / "out" / f"g{i}.mkv.strm").write_text("x", encoding="utf-8")
            # 网盘只剩 9 个（10% < 30% 阈值，过闸门）
            res = Syncer(None, make_cfg(tmp)).run(
                remote_files=[f(f"g{i}.mkv") for i in range(9)],
                remote_dirs=[], source="tree")
            self.assertEqual(res.diff["delete"], ["g9.mkv.strm"])
            self.assertEqual(res.deleted, 1)
            self.assertEqual(res.pending_delete, 0)
            self.assertFalse((tmp / "out" / "g9.mkv.strm").exists())

    def test_明细有上限超出只报数(self):
        """几千条时不把回执撑爆 —— 超出的进 `more`，只报数不列名。"""
        from app.sync import _DIFF_CAP
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            n = _DIFF_CAP + 7
            res = Syncer(None, make_cfg(tmp)).run(
                remote_files=[f(f"a/{i:04d}.mkv") for i in range(n)],
                remote_dirs=["a"], source="tree")
            self.assertEqual(len(res.diff["add"]), _DIFF_CAP)
            self.assertEqual(res.diff["more"]["add"], 7)
            self.assertEqual(res.diff["counts"]["add"], n)


class TestDeleteGuard(unittest.TestCase):
    """🔴 删除判定 —— 最需要正反例的地方。"""

    def _prep(self, tmp: Path, names: list[str]) -> Syncer:
        s = Syncer(None, make_cfg(tmp))
        for nm in names:
            p = tmp / "out" / nm
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("old", encoding="utf-8")
        return s

    def test_发现即删_不再等下一轮(self):
        """🔴 2026-10-20 定的新口径（红领巾：挂起清单躺了 127 分钟还没人处理）。

        以前：第一次发现只挂起，要**等下一轮**才复核删除。
        现在：同一轮里就复核完、删掉 —— `pending-delete.json` 平时压根不该存在。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"g{i}.mkv.strm" for i in range(10)])
            # 网盘只剩 9 个 ⇒ 少了 1 个（10% < 30% 阈值，过闸门）
            remote = [f(f"g{i}.mkv") for i in range(9)]
            res = s.run(remote_files=remote, remote_dirs=[], source="tree")
            self.assertEqual(res.deleted, 1)              # ✅ 当轮就删
            self.assertEqual(res.pending_delete, 0)       # 不再挂起
            self.assertFalse((tmp / "out" / "g9.mkv.strm").exists())
            self.assertEqual(s.load_pending().paths, [])

    def test_复核说还在就保留(self):
        """复核发现「它其实还在网盘上」⇒ 保留本地（宁可多留一轮，不可误删）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"g{i}.mkv.strm" for i in range(10)])
            # 目录树说 g9 没了，但实时列目录里它还在
            s.v = FakeV({"": ["g9.mkv"]})
            remote = [f(f"g{i}.mkv") for i in range(9)]
            res = s.run(remote_files=remote, remote_dirs=[], source="tree")
            self.assertEqual(res.deleted, 0)
            self.assertEqual(res.delete_kept, 1)
            self.assertTrue((tmp / "out" / "g9.mkv.strm").exists())

    def test_父目录已不存在也算确认不在(self):
        """🔴 整目录改名/移走的场景（旧版在这里会**永远删不掉**）。

        旧 `_recheck` 把 `dir_id()` 抛的「找不到目录」当成查不成 ⇒ 按「还在」处理
          ⇒ 目录都没了，它下面的旧 strm 一条也删不掉（只能手动确认）。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, ["电影/旧名/片.mkv.strm"])
            s.v = FakeV({}, missing=("电影/旧名",))
            res = s.run(remote_files=[f("电影/新名/片.mkv")], remote_dirs=[], source="tree")
            self.assertEqual(res.deleted, 1)
            self.assertFalse((tmp / "out" / "电影" / "旧名" / "片.mkv.strm").exists())

    def test_接口报错时不删(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"电影/g{i}.mkv.strm" for i in range(10)])
            s.v = FakeV({}, broken=("电影",))
            remote = [f(f"电影/g{i}.mkv") for i in range(9)]
            res = s.run(remote_files=remote, remote_dirs=[], source="tree")
            self.assertEqual(res.deleted, 0)
            self.assertEqual(res.delete_kept, 1)
            self.assertTrue((tmp / "out" / "电影" / "g9.mkv.strm").exists())

    def test_目录没列完时不删(self):
        """翻页被截断 ⇒ 不能断言「这个名字不在」（否则把没列到的当成删了）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"电影/g{i}.mkv.strm" for i in range(10)])
            s.v = FakeV({}, complete=False)
            remote = [f(f"电影/g{i}.mkv") for i in range(9)]
            res = s.run(remote_files=remote, remote_dirs=[], source="tree")
            self.assertEqual(res.deleted, 0)
            self.assertEqual(res.delete_kept, 1)

    def test_同一目录只列一次(self):
        """一季 12 集 = 1 次请求，不是 12 次（旧写法按条查）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            gone = [f"电视剧/某剧/S01/E{i:02d}.mkv.strm" for i in range(12)]
            # 撑分母（否则 13/20 = 65% 会先撞异常闸门，走不到复核）
            stays = [f"g{i}.mkv.strm" for i in range(48)]
            s = self._prep(tmp, gone + stays + ["g48.mkv.strm"])
            fake = FakeV({"电视剧/某剧/S01": []})
            s.v = fake
            remote = [f(f"g{i}.mkv") for i in range(48)]
            res = s.run(remote_files=remote, remote_dirs=[], source="tree")
            self.assertFalse(res.guard_tripped)
            # 13 条候选只散在 2 个目录里 ⇒ 只列 2 次
            self.assertEqual(sorted(fake.listed), ["", "电视剧/某剧/S01"])
            self.assertEqual(res.deleted, 13)

    def test_同步根不是整盘时按同步根拼路径(self):
        """🔴 本地 rel 是**相对同步根**的，`dir_id()` 却从**网盘根**解析。

        旧写法直接把 `电影/…` 喂给 `dir_id()` ⇒ 同步根不是整盘（如 `影音`）时
        **每条都解析不出来**。这条用例钉死「必须拼上同步根」——
        拼错 + 新口径（当轮即删）＝ 误删整库。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"电影/g{i}.mkv.strm" for i in range(10)])
            s.cfg = make_cfg(tmp, remote_root="影音")
            fake = FakeV({"影音/电影": []})
            s.v = fake
            remote = [f(f"电影/g{i}.mkv") for i in range(9)]
            res = s.run(remote_files=remote, remote_dirs=[], source="tree")
            self.assertIn("影音/电影", fake.listed, "解析目录时没把同步根拼回去")
            self.assertEqual(res.deleted, 1)
            self.assertFalse((tmp / "out" / "电影" / "g9.mkv.strm").exists())

    def test_同步根都解析不出来就一条都不删(self):
        """cookie 失效 / 同步根被改名 ⇒ 没资格判「谁没了」，整批保留。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"电影/g{i}.mkv.strm" for i in range(10)])
            fake = FakeV({}, root_broken=True)
            s.v = fake
            remote = [f(f"电影/g{i}.mkv") for i in range(9)]
            res = s.run(remote_files=remote, remote_dirs=[], source="tree")
            self.assertEqual(res.deleted, 0)
            self.assertEqual(res.delete_kept, 1)
            self.assertEqual(fake.listed, [], "根都解析不出来就不该再列目录")
            self.assertTrue((tmp / "out" / "电影" / "g9.mkv.strm").exists())

    def test_变动面过大就整批挂起(self):
        """候选散在几十个目录里 ⇒ 不逐个复核（会把一轮拖成几百次请求），整批等他拍板。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            n_dirs = _RECHECK_DIR_CAP + 1
            moved = [f"d{i}/片.mkv.strm" for i in range(n_dirs)]
            stays = [f"g{i}.mkv.strm" for i in range(100)]      # 撑着分母，别让闸门先触发
            s = self._prep(tmp, moved + stays)
            fake = FakeV({})
            s.v = fake
            # 那 100 个照旧在网盘上，只有 41 个目录里的「片」没了 ⇒ 41/141 ≈ 29% < 30%
            res = s.run(remote_files=[f(f"g{i}.mkv") for i in range(100)],
                        remote_dirs=[], source="tree")
            self.assertFalse(res.guard_tripped)
            self.assertEqual(res.deleted, 0)
            self.assertEqual(res.pending_delete, n_dirs)
            self.assertEqual(fake.listed, [], "不该真去列目录")
            p = s.load_pending()
            self.assertIn("散在", p.reason)
            self.assertTrue((tmp / "out" / moved[0]).exists())

    def test_异常闸门一条都不删(self):
        """🔴 cookie 失效的典型症状：网盘「看起来全没了」。绝不能照删。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"g{i}.mkv.strm" for i in range(10)])
            # 网盘一个都没看到 ⇒ 消失 100% > 30%
            res = s.run(remote_files=[], remote_dirs=[], source="api")
            self.assertTrue(res.guard_tripped)
            self.assertEqual(res.deleted, 0)
            self.assertEqual(len(list((tmp / "out").glob("*.strm"))), 10)
            self.assertIn("超过阈值", res.guard_reason)

    def test_闸门边界刚好等于阈值时不触发(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"g{i}.mkv.strm" for i in range(10)])
            # 消失 3/10 = 30%，阈值 30%，判据是 `>` ⇒ 不触发闸门 ⇒ 当轮复核后删
            remote = [f(f"g{i}.mkv") for i in range(7)]
            res = s.run(remote_files=remote, remote_dirs=[], source="tree")
            self.assertFalse(res.guard_tripped)
            self.assertEqual(res.deleted, 3)
            self.assertEqual(res.pending_delete, 0)

    def test_恢复后挂起清单作废(self):
        """上一轮挂起、这一轮网盘又都在了 ⇒ 说明是抖动，清单要清掉。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"g{i}.mkv.strm" for i in range(10)])
            s.save_pending(Pending(paths=["g9.mkv.strm"], first_seen=time.time() - 600,
                                   seen_times=1))
            remote = [f(f"g{i}.mkv") for i in range(10)]      # 全都在
            res = s.run(remote_files=remote, remote_dirs=[], source="tree")
            self.assertEqual(res.deleted, 0)
            self.assertEqual(s.load_pending().paths, [])
            self.assertTrue((tmp / "out" / "g9.mkv.strm").exists())

    def test_force一条判定都不做直接删(self):
        """界面上「确认删除」走的就是这条 —— 他点了他负责。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"g{i}.mkv.strm" for i in range(10)])
            remote = [f(f"g{i}.mkv") for i in range(9)]
            s.save_pending(Pending(paths=["g9.mkv.strm"], first_seen=time.time(),
                                   seen_times=1))
            res = s.run(remote_files=remote, remote_dirs=[], source="tree", force_delete=True)
            self.assertEqual(res.deleted, 1)


class TestRenameVsAnomaly(unittest.TestCase):
    """🔴 区分「改名/移动」与「异常」—— 光看消失比例会把整目录改名误判成异常。

    判据：改名/移动 ⇒ 消失的同时**有等量新增**；cookie 失效 ⇒ **只消失不新增**。
    （2026-10-08 红领巾追问「改网盘目录名会不会重构」时发现的口径缺陷）
    """

    def _prep(self, tmp: Path, names: list[str]) -> Syncer:
        s = Syncer(None, make_cfg(tmp))
        for nm in names:
            p = tmp / "out" / nm
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("old", encoding="utf-8")
        return s

    def test_整目录改名不触发闸门_且旧的一并清掉(self):
        """867 个里改掉 283 个（32.6% > 30%）—— 旧行为会误判成异常。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            old = [f"电影/旧名{i}/片.mkv.strm" for i in range(30)]
            s = self._prep(tmp, old)
            # 网盘侧：同样的文件，目录改了名（30 消失 + 30 新增）
            new = [f("电影/新名{i}/片.mkv") for i in range(30)]
            res = s.run(remote_files=new, remote_dirs=[], source="tree")
            self.assertFalse(res.guard_tripped, "改名不该触发异常闸门")
            self.assertTrue(res.looks_like_move)
            self.assertEqual(res.written, 30)          # 新的正常写
            self.assertEqual(res.deleted, 30)          # 旧的当轮就清掉（父目录已不存在也算确认不在）
            self.assertEqual(res.pending_delete, 0)

    def test_改名当轮就把旧名清掉(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            for i in range(30):
                p = tmp / "out" / f"电影/旧名{i}/片.mkv.strm"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("old", encoding="utf-8")
            new = [f(f"电影/新名{i}/片.mkv") for i in range(30)]
            res = s.run(remote_files=new, remote_dirs=[], source="tree")
            self.assertFalse(res.guard_tripped)
            self.assertEqual(res.deleted, 30)          # 当轮即删，不再等下一轮
            self.assertEqual(res.written, 30)

    def test_cookie失效仍然拦得住(self):
        """网盘返回空 = 只消失没新增 ⇒ 必须触发闸门（这条不能因为上面的改动而失效）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"g{i}.mkv.strm" for i in range(10)])
            res = s.run(remote_files=[], remote_dirs=[], source="api")
            self.assertTrue(res.guard_tripped)
            self.assertFalse(res.looks_like_move)
            self.assertEqual(res.deleted, 0)
            self.assertEqual(len(list((tmp / "out").glob("*.strm"))), 10)

    def test_闸门触发时也存下清单(self):
        """⚠️ 万一是「真删了一大批」，他得能在网页上看到清单并手动确认。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"g{i}.mkv.strm" for i in range(10)])
            s.run(remote_files=[], remote_dirs=[], source="api")
            p = s.load_pending()
            self.assertEqual(len(p.paths), 10)

    def test_部分新增不足一半仍判异常(self):
        """网盘只回来 2 条、本地有 10 条 ⇒ 新增太少，是接口不全 ⇒ 拦住。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"g{i}.mkv.strm" for i in range(10)])
            res = s.run(remote_files=[f("隔壁/新片.mkv")], remote_dirs=[], source="api")
            self.assertTrue(res.guard_tripped)

    def test_整目录改名会标出疑似改名(self):
        """30 条全消失、同时 30 条全新增 ⇒ 判「改名/移动」，不该触发异常闸门。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"电影/旧名{i}/片.mkv.strm" for i in range(30)])
            new = [f(f"电影/新名{i}/片.mkv") for i in range(30)]
            res = s.run(remote_files=new, remote_dirs=[], source="tree")
            self.assertTrue(res.looks_like_move)
            self.assertFalse(res.guard_tripped)

    def test_只消失不改名不标记(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"g{i}.mkv.strm" for i in range(10)])
            remote = [f(f"g{i}.mkv") for i in range(9)]     # 少一个，没有新增
            res = s.run(remote_files=remote, remote_dirs=[], source="tree")
            self.assertFalse(res.looks_like_move)

    def test_本地改名会被重生成并清掉旧的(self):
        """🔴 旧行为（2026-10-08 之前）：本地改名 ⇒ 判「丢失 + 多余」。

        ⚠️ **这条已被 `TestLocalRename` 取代** —— 红领巾明确要求
        「修改 strm 的名称不应该被重新生成」。这里保留成「反例」，
        说明**没有**内容反查时会怎样，防止哪天把它去掉。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            p = tmp / "out" / "电影" / "片.mkv.strm"
            p.parent.mkdir(parents=True, exist_ok=True)
            # ⚠️ 故意写**错的内容**（不是那个网盘文件的 URL）⇒ 内容反查也认不出来
            p.write_text("https://别的地方/x.mkv", encoding="utf-8")
            p.rename(p.with_name("片-我改的.mkv.strm"))
            res = s.run(remote_files=[f("电影/片.mkv")], remote_dirs=[], source="tree")
            self.assertEqual(res.written, 1)               # 重新生成原名的
            self.assertTrue((tmp / "out" / "电影" / "片.mkv.strm").exists())
            self.assertEqual(res.deleted, 1)               # 改过名的那份：当轮清掉
            self.assertFalse((tmp / "out" / "电影" / "片-我改的.mkv.strm").exists())

    def test_本地手动改内容会被改回来(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            p = tmp / "out" / "a.mkv.strm"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("https://我乱改的/d/a.mkv\n", encoding="utf-8")
            res = s.run(remote_files=[f("a.mkv")], remote_dirs=[], source="tree")
            self.assertEqual(res.written, 1)
            self.assertIn("example.test", p.read_text(encoding="utf-8"))


class TestLocalRename(unittest.TestCase):
    """⭐ 红领巾 2026-10-08 的需求：「**修改 strm 的名称不应该被重新生成** ——
    因为内部的数据并没有变化，而是有特殊需求需要修改 strm 的名字」。

    实现口径：**用 strm 的内容（URL）当「词典」键**。
    strm 的内容天然唯一指向一个网盘文件 ⇒ 内容对上 = 同一个文件 ⇒ 原样保留。
    """

    def _seed(self, tmp: Path, rel: str, url: str) -> Path:
        p = tmp / "out" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(url + "\n", encoding="utf-8")
        return p

    def _url(self, s: Syncer, rel: str) -> str:
        return s.builder.url(rel)

    def test_改名后不被重生成也不被删(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            target = s.builder.url("电影/片.mkv")
            p = self._seed(tmp, "电影/片.mkv.strm", target)
            # 用户改名
            p.rename(p.with_name("我改的名字.mkv.strm"))

            res = s.run(remote_files=[f("电影/片.mkv")], remote_dirs=[], source="tree")
            self.assertEqual(res.written, 0, "不该重新生成")
            self.assertEqual(res.deleted, 0, "不该删")
            self.assertEqual(res.pending_delete, 0, "不该进挂起")
            self.assertEqual(res.renamed, 1)
            self.assertTrue((tmp / "out" / "电影" / "我改的名字.mkv.strm").exists())
            # ⚠️ 不能又冒出一个「原名的」来
            self.assertFalse((tmp / "out" / "电影" / "片.mkv.strm").exists())

    def test_改名后内容仍会被更新(self):
        """改名 ≠ 冻结内容。URL 变了（如同批文件换了前缀）还是要就地更新。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            old = self._seed(tmp, "电影/片.mkv.strm", "https://旧前缀/d/电影/片.mkv")
            old.rename(old.with_name("我改的名字.mkv.strm"))
            res = s.run(remote_files=[f("电影/片.mkv")], remote_dirs=[], source="tree")
            # 内容对不上 ⇒ 认领失败 ⇒ 走正常流程（生成新的 + 旧的当轮清掉）
            self.assertEqual(res.renamed, 0)
            self.assertEqual(res.written, 1)
            self.assertEqual(res.deleted, 1)

    def test_手动新增的strm被保留(self):
        """手动加一个指向真实网盘文件的 strm ⇒ 内容对得上 ⇒ 保留，不当孤儿删。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            self._seed(tmp, "我给这部片单开的名字.mkv.strm", s.builder.url("电影/片.mkv"))
            res = s.run(remote_files=[f("电影/片.mkv")], remote_dirs=[], source="tree")
            self.assertEqual(res.written, 0)
            self.assertEqual(res.deleted, 0)
            self.assertEqual(res.pending_delete, 0)
            self.assertEqual(res.renamed, 1)

    def test_网盘改名仍然触发新生成(self):
        """⭐ 需求里明确要的：**原始文件改名 ⇒ 应该触发新的 strm 生成**。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            self._seed(tmp, "电影/旧名.mkv.strm", s.builder.url("电影/旧名.mkv"))
            res = s.run(remote_files=[f("电影/新名.mkv")], remote_dirs=[], source="tree")
            self.assertEqual(res.written, 1)                     # 新名字的生成出来
            self.assertTrue((tmp / "out" / "电影" / "新名.mkv.strm").exists())
            self.assertEqual(res.deleted, 1)                     # 旧名字的当轮清掉
            self.assertEqual(res.renamed, 0)

    def test_同一URL多份只认领一个(self):
        """两份内容一样的 strm（重复件）⇒ 只认领一个，另一个仍判多余。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            u = s.builder.url("a.mkv")
            self._seed(tmp, "副本1.mkv.strm", u)
            self._seed(tmp, "副本2.mkv.strm", u)
            # 再撑 9 份正常文件 ⇒ 1/10 = 10%，别撞异常闸门
            for i in range(9):
                self._seed(tmp, f"g{i}.mkv.strm", s.builder.url(f"g{i}.mkv"))
            res = s.run(remote_files=[f("a.mkv")] + [f(f"g{i}.mkv") for i in range(9)],
                        remote_dirs=[], source="tree")
            self.assertFalse(res.guard_tripped)
            self.assertEqual(res.renamed, 1)
            self.assertEqual(res.deleted, 1)     # 另一份判多余 ⇒ 当轮清掉

    def test_内容对的路径也对_走第一级不重复读(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            self._seed(tmp, "电影/片.mkv.strm", s.builder.url("电影/片.mkv"))
            res = s.run(remote_files=[f("电影/片.mkv")], remote_dirs=[], source="tree")
            self.assertEqual(res.written, 0)
            self.assertEqual(res.renamed, 0)      # 路径就配上了，不走内容反查
            self.assertEqual(res.unchanged, 1)

    def test_空文件不参与内容反查(self):
        """内容为空的 strm 不该被当成「对得上任何东西」。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            self._seed(tmp, "空的.mkv.strm", "")
            res = s.run(remote_files=[f("a.mkv")], remote_dirs=[], source="tree")
            self.assertEqual(res.renamed, 0)
            self.assertEqual(res.written, 1)
            self.assertEqual(res.deleted, 1)

    def test_改名的也进回执明细(self):
        """⭐ 「记录」里要能看见「这轮有哪些是我自己改过名的」——
        `res.diff.renamed` 就是它（前端 `renderResList('renamed')` 读这个）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            u = s.builder.url("电影/片.mkv")
            p = self._seed(tmp, "电影/片.mkv.strm", u)
            p.rename(p.with_name("改名了.mkv.strm"))
            res = s.run(remote_files=[f("电影/片.mkv")], remote_dirs=[], source="tree")
            self.assertEqual(res.written, 0)          # 内容一致 ⇒ 什么都不用做
            self.assertEqual(res.diff["counts"]["renamed"], 1)
            self.assertEqual(len(res.diff["renamed"]), 1)
            self.assertEqual(res.diff["renamed"][0]["local"], "电影/改名了.mkv.strm")


class TestRemoveSafety(unittest.TestCase):
    def test_只删strm不动别的文件(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            (tmp / "out" / "keep.txt").write_text("important", encoding="utf-8")
            (tmp / "out" / "a.mkv.strm").write_text("x", encoding="utf-8")
            n = s.remove(["keep.txt", "a.mkv.strm"])     # 故意把非 strm 也传进去
            self.assertEqual(n, 1)
            self.assertTrue((tmp / "out" / "keep.txt").exists())   # ⛔ 没被删
            self.assertFalse((tmp / "out" / "a.mkv.strm").exists())

    def test_删除后清理空目录(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            p = tmp / "out" / "d1" / "d2" / "a.mkv.strm"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")
            s.remove(["d1/d2/a.mkv.strm"])
            self.assertFalse((tmp / "out" / "d1").exists())        # 空壳一起清掉
            self.assertTrue((tmp / "out").exists())                # 但绝不越界

    def test_不越界到output之外(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            outside = tmp / "danger.mkv.strm"
            outside.write_text("x", encoding="utf-8")
            s.remove(["../danger.mkv.strm"])
            self.assertTrue(outside.exists())                      # ⛔ 没碰

    def test_目录非空时不清(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            d = tmp / "out" / "d1"
            d.mkdir(parents=True, exist_ok=True)
            (d / "a.mkv.strm").write_text("x", encoding="utf-8")
            (d / "b.mkv.strm").write_text("x", encoding="utf-8")
            s.remove(["d1/a.mkv.strm"])
            self.assertTrue(d.exists())                            # 还有东西，别动


class TestScanLocal(unittest.TestCase):
    def test_跳过隐藏与群晖垃圾目录(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            for p in [".@sync/a.mkv.strm", "@eaDir/b.mkv.strm", "ok/c.mkv.strm"]:
                fp = tmp / "out" / p
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text("x", encoding="utf-8")
            got = s.scan_local()
            self.assertEqual(list(got), ["ok/c.mkv.strm"])

    def test_分隔符统一成正斜杠(self):
        """🔴 回归：`os.walk` 在 Windows 上给反斜杠，不转的话
        本地键与网盘侧的 `/` 一个都对不上 ⇒ **全库被判成「网盘上没了」**。
        2026-10-08 实测踩到（靠异常闸门拦下），且原来那条用例的期望值
        写的是 `str(Path(...))`（在 Windows 上正好也是反斜杠）⇒ 把 bug 遮住了。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            fp = tmp / "out" / "a" / "b" / "c.mkv.strm"
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text("x", encoding="utf-8")
            got = s.scan_local()
            self.assertEqual(list(got), ["a/b/c.mkv.strm"])
            for k in got:
                self.assertNotIn("\\", k, "本地键里不能有反斜杠")

    def test_与网盘侧键能对上(self):
        """本地扫出来的键，必须能跟 `_safe_rel(远端 rel)` 直接比。"""
        from app.sync import _safe_rel
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            fp = tmp / "out" / "电影" / "片名（2026）" / "x.mkv.strm"
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text("x", encoding="utf-8")
            local = s.scan_local()
            self.assertIn(_safe_rel("电影/片名（2026）/x.mkv.strm"), local)

    def test_只认strm(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            (tmp / "out" / "a.mkv").write_text("x", encoding="utf-8")
            (tmp / "out" / "a.mkv.strm").write_text("x", encoding="utf-8")
            self.assertEqual(len(s.scan_local()), 1)

    def test_输出目录不存在时返回空(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = make_cfg(tmp, output_dir=tmp / "不存在的目录")
            s = Syncer(None, cfg)
            self.assertEqual(s.scan_local(), {})


class TestRoundTripNoFalseDelete(unittest.TestCase):
    """🔴 端到端口径：**自己写出来的，下一轮必须认得出、不能判成删除**。

    这是「分隔符 bug」在业务层的表现 —— 单看 `scan_local` 不一定发现问题，
    整条链路跑一遍才能确认。
    """

    def test_写完再跑一轮不会误判删除(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = make_cfg(tmp)
            s = Syncer(None, cfg)
            remote = [f("电影/片名（2026）/x.mkv"), f("电视剧/某剧/S01E01.mkv")]
            dirs = ["电影", "电影/片名（2026）", "电视剧", "电视剧/某剧"]

            r1 = s.run(remote_files=remote, remote_dirs=dirs, source="tree")
            self.assertEqual(r1.written, 2)
            self.assertEqual(r1.deleted, 0)

            # 第二轮：同样的网盘内容 ⇒ 一条都不该删、也不该重写
            r2 = s.run(remote_files=remote, remote_dirs=dirs, source="tree")
            self.assertEqual(r2.written, 0, "内容没变却重写了")
            self.assertEqual(r2.unchanged, 2)
            self.assertEqual(r2.deleted, 0, "自己写的文件被判成删除")
            self.assertFalse(r2.guard_tripped, "闸门不该被触发")
            self.assertEqual(r2.pending_delete, 0)

    def test_真的消失了当轮就删(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            a = [f(f"电影/片{i}.mkv") for i in range(10)]
            s.run(remote_files=a, remote_dirs=[], source="tree")
            b = a[:9]                                  # 少一个
            r = s.run(remote_files=b, remote_dirs=[], source="tree")
            self.assertEqual(r.deleted, 1)
            self.assertEqual(r.pending_delete, 0)
            self.assertFalse(r.guard_tripped)


class TestPendingPersistence(unittest.TestCase):
    def test_存取往返(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            s.save_pending(Pending(paths=["a", "b"], first_seen=1234.5, seen_times=2,
                                   reason="散在 41 个目录里"))
            p = s.load_pending()
            self.assertEqual(p.paths, ["a", "b"])
            self.assertEqual(p.first_seen, 1234.5)
            self.assertEqual(p.seen_times, 2)
            # ⚠️ `reason` 必须一起存 —— 网页那张卡上「为什么没删」就是它
            self.assertEqual(p.reason, "散在 41 个目录里")

    def test_文件损坏时返回空不抛(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            s.pending_path.write_text("{ 坏 json", encoding="utf-8")
            self.assertEqual(s.load_pending().paths, [])

    def test_age_minutes(self):
        p = Pending(paths=[], first_seen=time.time() - 300)
        self.assertAlmostEqual(p.age_minutes(), 5.0, delta=0.1)

    def test_没有first_seen时年龄为0(self):
        self.assertEqual(Pending().age_minutes(), 0.0)


if __name__ == "__main__":
    unittest.main()
