"""同步引擎的回归用例 —— **重点在删除判定**。

删除是本工具唯一不可逆的动作，所以用例要覆盖：
  · 正常情况：新增能落盘、未变的不重写（保住 mtime）
  · 删除挂起：第一次发现**不删**，只挂起
  · 等待中：没到时间**还是不动**
  · 二次确认：到点了才删
  · 🔴 异常闸门：消失比例超阈值 ⇒ **一条都不删**（cookie 失效的典型症状）
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
from app.sync import Pending, Syncer


def make_cfg(tmp: Path, **kw) -> Config:
    """造一份指向临时目录的配置（cookie 留空，测试不联网）。"""
    base = dict(
        data_dir=tmp / "data",
        output_dir=tmp / "out",
        strm_prefix="https://example.test/d",
        url_encode=False,
        dry_run=False,
        delete_defer_minutes=10.0,
        delete_ratio_guard=0.3,
        video_ext="mp4,mkv",
    )
    base.update(kw)
    cfg = Config(**base)
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.output_dir).mkdir(parents=True, exist_ok=True)
    return cfg


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


class TestDryRun(unittest.TestCase):
    def test_演练一个文件都不写(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp, dry_run=True))
            res = s.run(remote_files=[f("a/b.mkv")], remote_dirs=["a"], source="tree")
            self.assertEqual(res.written, 0)
            self.assertEqual(list((tmp / "out").rglob("*")), [])
            self.assertEqual(res.diff["counts"]["add"], 1)   # 但算出来了

    def test_演练也不删(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "out").mkdir(parents=True, exist_ok=True)
            (tmp / "out" / "孤儿.mkv.strm").write_text("x", encoding="utf-8")
            s = Syncer(None, make_cfg(tmp, dry_run=True))
            res = s.run(remote_files=[], remote_dirs=[], source="tree")
            self.assertEqual(res.deleted, 0)
            self.assertTrue((tmp / "out" / "孤儿.mkv.strm").exists())
            self.assertEqual(res.diff["counts"]["delete"], 1)


class TestDeleteGuard(unittest.TestCase):
    """🔴 删除判定 —— 最需要正反例的地方。"""

    def _prep(self, tmp: Path, names: list[str]) -> Syncer:
        s = Syncer(None, make_cfg(tmp))
        for nm in names:
            p = tmp / "out" / nm
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("old", encoding="utf-8")
        return s

    def test_第一次发现只挂起不删(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"g{i}.mkv.strm" for i in range(10)])
            # 网盘只剩 9 个 ⇒ 少了 1 个（10% < 30% 阈值，过闸门）
            remote = [f(f"g{i}.mkv") for i in range(9)]
            res = s.run(remote_files=remote, remote_dirs=[], source="tree")
            self.assertEqual(res.deleted, 0)              # ⛔ 不删
            self.assertEqual(res.pending_delete, 1)       # 挂起
            self.assertTrue((tmp / "out" / "g9.mkv.strm").exists())
            p = s.load_pending()
            self.assertEqual(len(p.paths), 1)

    def test_等待中仍不删(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"g{i}.mkv.strm" for i in range(10)])
            remote = [f(f"g{i}.mkv") for i in range(9)]
            s.run(remote_files=remote, remote_dirs=[], source="tree")   # 第 1 轮挂起
            res = s.run(remote_files=remote, remote_dirs=[], source="tree")  # 第 2 轮，时间没到
            self.assertEqual(res.deleted, 0)
            self.assertTrue((tmp / "out" / "g9.mkv.strm").exists())

    def test_到点后二次确认才删(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = make_cfg(tmp, delete_defer_minutes=0.0)     # 不等待
            s = Syncer(None, cfg)
            for i in range(10):
                (tmp / "out" / f"g{i}.mkv.strm").write_text("old", encoding="utf-8")
            remote = [f(f"g{i}.mkv") for i in range(9)]
            # 先手动造一个「早已挂起」的 pending
            s.save_pending(Pending(paths=["g9.mkv.strm"], first_seen=time.time() - 600,
                                   seen_times=1))
            res = s.run(remote_files=remote, remote_dirs=[], source="tree")
            self.assertEqual(res.deleted, 1)
            self.assertFalse((tmp / "out" / "g9.mkv.strm").exists())
            self.assertEqual(s.load_pending().paths, [])      # 清单已清

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
            # 消失 3/10 = 30%，阈值 30%，判据是 `>` ⇒ 不触发闸门，走挂起
            remote = [f(f"g{i}.mkv") for i in range(7)]
            res = s.run(remote_files=remote, remote_dirs=[], source="tree")
            self.assertFalse(res.guard_tripped)
            self.assertEqual(res.deleted, 0)
            self.assertEqual(res.pending_delete, 3)

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

    def test_force跳过等待(self):
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

    def test_整目录改名不触发闸门(self):
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
            self.assertEqual(res.deleted, 0)           # 旧的仍走挂起（不立即删）
            self.assertEqual(res.pending_delete, 30)

    def test_改名到点后能正常删旧的(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp, delete_defer_minutes=0.0))
            for i in range(30):
                p = tmp / "out" / f"电影/旧名{i}/片.mkv.strm"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("old", encoding="utf-8")
            new = [f(f"电影/新名{i}/片.mkv") for i in range(30)]
            s.save_pending(Pending(paths=[f"电影/旧名{i}/片.mkv.strm" for i in range(30)],
                                   first_seen=time.time() - 600, seen_times=1))
            res = s.run(remote_files=new, remote_dirs=[], source="tree")
            self.assertFalse(res.guard_tripped)
            self.assertEqual(res.deleted, 30)          # 到点确认后才删
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

    def test_演练时也标出疑似改名(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"电影/旧名{i}/片.mkv.strm" for i in range(30)])
            cfgd = make_cfg(tmp, dry_run=True)
            s2 = Syncer(None, cfgd)
            new = [f(f"电影/新名{i}/片.mkv") for i in range(30)]
            res = s2.run(remote_files=new, remote_dirs=[], source="tree")
            self.assertTrue(res.looks_like_move)

    def test_只消失不改名不标记(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = self._prep(tmp, [f"g{i}.mkv.strm" for i in range(10)])
            remote = [f(f"g{i}.mkv") for i in range(9)]     # 少一个，没有新增
            res = s.run(remote_files=remote, remote_dirs=[], source="tree")
            self.assertFalse(res.looks_like_move)

    def test_本地改名会被重生成并挂起删除(self):
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
            self.assertEqual(res.deleted, 0)               # 改过名的那份：挂起，没立即删
            self.assertEqual(res.pending_delete, 1)
            self.assertIn("电影/片-我改的.mkv.strm", s.load_pending().paths)

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
            # 内容对不上 ⇒ 认领失败 ⇒ 走正常流程（生成 + 旧的挂起）
            self.assertEqual(res.renamed, 0)
            self.assertEqual(res.written, 1)
            self.assertEqual(res.pending_delete, 1)

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
            self.assertEqual(res.pending_delete, 1)              # 旧名字的挂起
            self.assertEqual(res.renamed, 0)

    def test_同一URL多份只认领一个(self):
        """两份内容一样的 strm（重复件）⇒ 只认领一个，另一个仍判多余。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            u = s.builder.url("a.mkv")
            self._seed(tmp, "副本1.mkv.strm", u)
            self._seed(tmp, "副本2.mkv.strm", u)
            res = s.run(remote_files=[f("a.mkv")], remote_dirs=[], source="tree")
            self.assertEqual(res.renamed, 1)
            self.assertEqual(res.pending_delete, 1)     # 另一份进挂起

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
            self.assertEqual(res.pending_delete, 1)

    def test_演练模式也报出改名(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            u = s.builder.url("电影/片.mkv")
            p = self._seed(tmp, "电影/片.mkv.strm", u)
            p.rename(p.with_name("改名了.mkv.strm"))
            s2 = Syncer(None, make_cfg(tmp, dry_run=True))
            res = s2.run(remote_files=[f("电影/片.mkv")], remote_dirs=[], source="tree")
            self.assertEqual(res.written, 0)
            self.assertEqual(res.diff["counts"]["renamed"], 1)
            self.assertEqual(len(res.diff["renamed"]), 1)


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

    def test_真的消失了会进挂起(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            a = [f(f"电影/片{i}.mkv") for i in range(10)]
            s.run(remote_files=a, remote_dirs=[], source="tree")
            b = a[:9]                                  # 少一个
            r = s.run(remote_files=b, remote_dirs=[], source="tree")
            self.assertEqual(r.deleted, 0)
            self.assertEqual(r.pending_delete, 1)
            self.assertFalse(r.guard_tripped)


class TestPendingPersistence(unittest.TestCase):
    def test_存取往返(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            s = Syncer(None, make_cfg(tmp))
            s.save_pending(Pending(paths=["a", "b"], first_seen=1234.5, seen_times=2))
            p = s.load_pending()
            self.assertEqual(p.paths, ["a", "b"])
            self.assertEqual(p.first_seen, 1234.5)
            self.assertEqual(p.seen_times, 2)

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
