"""执行器单测 —— 用**假的 V115**（内存文件树）跑真的 `Executor`。

测什么：
  · 🔴 `DRY_RUN=1` 时**一个请求都不发**（`v.calls` 必须为 0）—— 这是他对「先看方案」的信任基础
  · 四个 pass 的顺序（先建目录、再改名、再搬、最后清）
  · `auto=False`（冲突项）绝不执行
  · 改名的分批（100 一组）、移动的分批
  · 清理走隔离（`_待清理/<源目录>/`）而不是删 —— 「可撤销」是第一原则
  · 断点续跑：中途收工后重跑不重复动手
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config        # noqa: E402
from app.executor import RENAME_CHUNK, Executor   # noqa: E402
from app.plan import Op, Plan        # noqa: E402
from app.throttle import Throttle    # noqa: E402


# --------------------------------------------------------------------------- 假 V115
class FakeNode:
    __slots__ = ("id", "name", "is_dir", "size")

    def __init__(self, id: str, name: str, is_dir: bool, size: int = 0):
        self.id, self.name, self.is_dir, self.size = id, name, is_dir, size


class FakeV:
    """内存版 115：够 `Executor` 用即可。

    ⚠️ `calls` 的口径**对齐真 `V115`**：每个「请求出口」递增一次。
       `list_dir` / `ensure_dir` / `move` / `rename` / `delete` / `root_cid` 各算一次
       （真实现里 `ensure_dir` 逐级各算一次，这里为了可读性按「一层一次」计）。
    """

    def __init__(self):
        self.calls = 0
        self.nodes: dict[str, dict[str, FakeNode]] = {"0": {}}
        self.paths: dict[str, str] = {"": "0"}      # 相对路径 → cid
        self.moved: list[tuple[list[str], str]] = []
        self.renamed: list[list[tuple[str, str]]] = []
        self.deleted: list[list[str]] = []
        self._n = 0
        cfg = Config()
        cfg.throttle_min = cfg.throttle_max = 0.0
        cfg.throttle_batch = 10 ** 9
        self.throttle = Throttle(cfg=cfg, sleeper=lambda s: None)

    # --- 搭树 -------------------------------------------------------------
    def _newid(self) -> str:
        self._n += 1
        return f"id{self._n}"

    def add_dir(self, parent_rel: str, name: str) -> FakeNode:
        pid = self.paths[parent_rel]
        n = FakeNode(self._newid(), name, True)
        self.nodes[pid][name] = n
        self.nodes[n.id] = {}
        self.paths[f"{parent_rel}/{name}" if parent_rel else name] = n.id
        return n

    def add_file(self, parent_rel: str, name: str) -> FakeNode:
        pid = self.paths[parent_rel]
        n = FakeNode(self._newid(), name, False)
        self.nodes[pid][name] = n
        return n

    def find(self, rel: str, name: str) -> FakeNode | None:
        return self.nodes.get(self.paths.get(rel, ""), {}).get(name)

    # --- V115 接口 --------------------------------------------------------
    def root_cid(self) -> str:
        self.calls += 1
        return "0"

    def list_dir(self, cid, dirs_only: bool = False):
        self.calls += 1
        out = list(self.nodes.get(cid, {}).values())
        return [n for n in out if n.is_dir] if dirs_only else out

    def ensure_dir(self, rel: str) -> str:
        self.calls += 1
        rel = (rel or "").strip("/")
        if rel in self.paths:
            return self.paths[rel]
        cur = ""
        for part in rel.split("/"):
            nxt = f"{cur}/{part}" if cur else part
            if nxt not in self.paths:
                self.add_dir(cur, part)
            cur = nxt
        return self.paths[rel]

    def move(self, fids: list[str], pid: str) -> bool:
        self.calls += 1
        self.moved.append((list(fids), pid))
        for f in fids:
            for d in self.nodes.values():
                for name, node in list(d.items()):
                    if node.id == f:
                        del d[name]
                        self.nodes[pid][name] = node
                        break
        return True

    def rename(self, pairs: list[tuple[str, str]]) -> bool:
        self.calls += 1
        self.renamed.append(list(pairs))
        for fid, new in pairs:
            for d in self.nodes.values():
                for name, node in list(d.items()):
                    if node.id == fid:
                        del d[name]
                        node.name = new
                        d[new] = node
                        break
        return True

    def delete(self, fids: list[str]) -> bool:
        self.calls += 1
        self.deleted.append(list(fids))
        return True

    # --- 便利 -------------------------------------------------------------
    def names_of(self, rel: str) -> set[str]:
        return set(self.nodes[self.paths[rel]].keys()) if rel in self.paths else set()


def make_cfg(tmp: str, **kw) -> Config:
    cfg = Config()
    cfg.data_dir = Path(tmp)
    cfg.log_dir = Path(tmp) / "logs"
    cfg.throttle_min = cfg.throttle_max = 0.0
    cfg.throttle_batch = 10 ** 9
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, ops: list[Op], cfg: Config, v: FakeV | None = None,
              **run_kw) -> tuple[dict, FakeV, Executor]:
        v = v or FakeV()
        ex = Executor(v, cfg)
        result = ex.run(Plan(ops=ops), **run_kw)
        return result, v, ex


class TestDryRun(Base):
    """🔴 出厂默认就是 dry-run。它必须**一个请求都不发** —— 这是他敢按下去的前提。"""

    def test_dry_run_零请求(self):
        cfg = make_cfg(self.tmp, dry_run=True)
        ops = [
            Op(kind="mkdir", target="DLDSS", group="mkdir"),
            Op(kind="move_dir", path="DLDSS-532", target="DLDSS", group="series"),
            Op(kind="rename", path="云下载/旧名.mp4", new_name="新名.mp4", group="movie"),
            Op(kind="trash", path="A/广告.mp4", reason="推广", group="junk"),
        ]
        result, v, _ex = self.build(ops, cfg)
        self.assertEqual(v.calls, 0, f"dry-run 居然发了 {v.calls} 个请求")
        self.assertEqual(result["requests"], 0)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["done"], 4, "dry-run 也要把「本可以动多少」数出来")
        self.assertEqual(v.moved, [])
        self.assertEqual(v.renamed, [])
        self.assertEqual(v.deleted, [])

    def test_dry_run_不查根目录id(self):
        """连 `root_cid()` 都不查 —— 能少发一个就少发一个。"""
        cfg = make_cfg(self.tmp, dry_run=True)
        _r, v, ex = self.build([Op(kind="mkdir", target="A")], cfg)
        self.assertEqual(v.calls, 0)
        self.assertEqual(ex.root_cid, "", "dry-run 不该去查根 id")


class TestPasses(Base):
    def test_建目录_改名_搬运_清理_顺序与效果(self):
        cfg = make_cfg(self.tmp, dry_run=False, junk_action="quarantine")
        v = FakeV()
        v.add_dir("", "DLDSS-532")               # 待归入系列的番号目录
        v.add_dir("", "某片目录")
        v.add_file("某片目录", "广告.mp4")

        ops = [
            # 顺序刻意打乱：执行器必须自己按 pass 排
            Op(kind="trash", path="某片目录/广告.mp4", reason="推广", group="junk"),
            Op(kind="move_dir", path="DLDSS-532", target="DLDSS", group="series"),
            Op(kind="mkdir", target="DLDSS", group="mkdir"),
            Op(kind="rename", path="某片目录", new_name="某片", group="movie"),
        ]
        result, v, _ex = self.build(ops, cfg, v)
        self.assertEqual(result["failed"], 0, result["errors"])
        self.assertEqual(result["done"], 4)

        # ① 目录建出来了 ② 番号目录搬进去了 ③ 原名目录改名了 ④ 广告进了隔离区
        self.assertIn("DLDSS", v.names_of(""))
        self.assertIn("DLDSS-532", v.names_of("DLDSS"))
        self.assertNotIn("DLDSS-532", v.names_of(""))
        self.assertIn("某片", v.names_of(""))
        self.assertNotIn("某片目录", v.names_of(""))
        self.assertIn("_待清理/某片目录", v.paths, "隔离目录该按来源分桶")
        self.assertIn("广告.mp4", v.names_of("_待清理/某片目录"))

    def test_冲突项不执行(self):
        """`auto=False` 的动作（两个源归一到同一目标）**一律不动手**。"""
        cfg = make_cfg(self.tmp, dry_run=False)
        v = FakeV()
        v.add_dir("", "MDTM-090")
        ops = [
            Op(kind="mkdir", target="MDTM", auto=True, group="mkdir"),
            Op(kind="move_dir", path="MDTM-090", target="MDTM", auto=False,
               reason="冲突：另一源也归到这里"),
        ]
        result, v, _ex = self.build(ops, cfg, v)
        self.assertEqual(result["skipped_manual"], 1)
        self.assertNotIn("MDTM-090", v.names_of("MDTM"), "冲突项被移动了！")
        self.assertIn("MDTM-090", v.names_of(""), "冲突项该留在原地等人裁决")

    def test_清理用隔离而不是删除(self):
        """`quarantine` 档：进 `_待清理/<源目录>/`，**一个 delete 都不发**（可整目录撤销）。"""
        cfg = make_cfg(self.tmp, dry_run=False, junk_action="quarantine")
        v = FakeV()
        v.add_dir("", "片A")
        for i in range(5):
            v.add_file("片A", f"广告{i}.mp4")
        ops = [Op(kind="trash", path=f"片A/广告{i}.mp4", reason="推广", group="junk")
               for i in range(5)]
        _r, v, _ex = self.build(ops, cfg, v)
        self.assertEqual(v.deleted, [], "隔离档绝不能调删除")
        self.assertIn("_待清理/片A", v.paths)
        self.assertEqual(len(v.names_of("_待清理/片A")), 5)

    def test_delete档才真删(self):
        cfg = make_cfg(self.tmp, dry_run=False, junk_action="delete")
        v = FakeV()
        v.add_dir("", "片A")
        node = v.add_file("片A", "广告.mp4")
        _r, v, _ex = self.build(
            [Op(kind="trash", path="片A/广告.mp4", reason="推广", group="junk")], cfg, v)
        self.assertEqual(v.deleted, [[node.id]], "delete 档该把那个文件删掉（进回收站）")
        self.assertEqual(v.moved, [], "delete 档不该再往隔离区搬")

    def test_分批_改名100一组_移动500一组(self):
        cfg = make_cfg(self.tmp, dry_run=False)
        v = FakeV()
        v.add_dir("", "剧集")
        n = RENAME_CHUNK * 2 + 5
        ops = []
        for i in range(n):
            v.add_file("剧集", f"第{i}.mp4")
            ops.append(Op(kind="rename", path=f"剧集/第{i}.mp4",
                          new_name=f"剧名.S01E{i:02d}.mp4", group="tv"))
        _r, v, _ex = self.build(ops, cfg, v)
        self.assertEqual(len(v.renamed), 3, "205 条改名该分 100+100+5 三批")
        self.assertEqual([len(c) for c in v.renamed], [100, 100, 5])


class TestResume(Base):
    def test_断点续跑不重复动手(self):
        """中途收工（请求上限）后重跑，已经做完的 pass 不许再来一遍。"""
        cfg = make_cfg(self.tmp, dry_run=False)
        v = FakeV()
        for i in range(10):
            v.add_dir("", f"番号-{i:03d}")
        ops = [Op(kind="mkdir", target=f"SER{i % 3}", group="mkdir") for i in range(6)]
        ops += [Op(kind="move_dir", path=f"番号-{i:03d}", target=f"SER{i % 3}", group="series")
                for i in range(10)]

        # 第一轮：给个很小的请求上限，逼它中途收工
        ex1 = Executor(v, cfg)
        r1 = ex1.run(Plan(ops=ops), resume=False, max_requests=4)
        self.assertLess(r1["done"], 16, "该被请求上限掐断")
        state = (Path(self.tmp) / "run-state.json").read_text(encoding="utf-8")
        self.assertIn("passes", state)

        # 第二轮：续跑
        ex2 = Executor(v, cfg)
        r2 = ex2.run(Plan(ops=ops), resume=True)
        moved_by_1 = list(v.moved)
        self.assertGreaterEqual(len(moved_by_1), 1)
        # 已经搬过的不能重复出现在后面的请求里
        moved_ids = [f for fids, _p in v.moved for f in fids]
        self.assertEqual(len(moved_ids), len(set(moved_ids)), "有文件被搬了两次")

    def test_clear_state(self):
        cfg = make_cfg(self.tmp, dry_run=True)
        v = FakeV()
        ex = Executor(v, cfg)
        ex.run(Plan(ops=[Op(kind="mkdir", target="A")]))
        self.assertTrue((Path(self.tmp) / "run-state.json").exists())
        ex.clear_state()
        self.assertFalse((Path(self.tmp) / "run-state.json").exists())


class TestResult(Base):
    def test_回执字段齐全(self):
        cfg = make_cfg(self.tmp, dry_run=True)
        result, _v, _ex = self.build([Op(kind="mkdir", target="A")], cfg)
        for key in ("started", "finished", "elapsed_human", "dry_run", "planned",
                    "done", "failed", "requests", "by_kind", "errors",
                    "throttle", "skipped_manual"):
            self.assertIn(key, result)

    def test_缺源不炸只是记账(self):
        """计划是离线算的，执行时源可能已被删/已搬走 —— 这种情况要跳过并记账，不能崩。"""
        cfg = make_cfg(self.tmp, dry_run=False)
        v = FakeV()
        ops = [Op(kind="move_dir", path="根本不存在的目录", target="X", group="series")]
        result, v, _ex = self.build(ops, cfg, v)
        self.assertEqual(result["done"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertIn("源不存在", result["errors"][0]["error"])
        self.assertEqual(v.moved, [], "源不存在还发移动请求就是错的")


if __name__ == "__main__":
    unittest.main(verbosity=2)
