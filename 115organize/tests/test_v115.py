"""115 封装层单测 —— 用**假 client** 顶掉全部网络行为。

为什么这层必须测：
  · `fs_files` 被 115 风控，单次最多回 200 条 —— **翻页漏了会静默丢 98% 数据**
  · `fs_move` 的 `conflict_policy` 默认 `replace`（覆盖、不可恢复）—— **不显式传 keep_both
    就是把用户的文件覆盖掉**，这是本工具最不能出的一类错
  · 所有请求都要过节流器 —— 漏一个出口就等于风控保护失效
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config          # noqa: E402
from app.throttle import Throttle      # noqa: E402
from app.v115 import V115, V115Error   # noqa: E402


class FakeClient:
    """够用的假 P115Client：只实现被测到的那几个方法，并**记录每次调用的 payload**。"""

    def __init__(self, listing: list[dict] | None = None, total: int | None = None,
                 patch: dict | None = None):
        self.listing = listing or []
        self.total = len(self.listing) if total is None else total
        self.calls: list[tuple[str, dict]] = []
        self.patch = patch or {}          # 指定方法固定返回什么（用来模拟失败）
        self.made: dict[str, str] = {}    # name -> id，模拟 fs_makedirs
        self.ignore_offset = False        # 模拟「offset 无效」的坏接口
        self._n = 0

    def _maybe_fail(self, name: str):
        if name in self.patch:
            resp = self.patch[name]
            if isinstance(resp, Exception):
                raise resp
            return resp
        return None

    # --- 读 ---------------------------------------------------------------
    def fs_files(self, payload: dict) -> dict:
        self.calls.append(("fs_files", dict(payload)))
        got = self._maybe_fail("fs_files")
        if got is not None:
            return got
        off = 0 if self.ignore_offset else int(payload.get("offset") or 0)
        lim = int(payload.get("limit") or 200)
        page = self.listing[off:off + lim]
        if payload.get("nf") == 1:        # 115 原生「只要目录」
            page = [n for n in page if not n.get("fid")]
        resp: dict = {"state": True, "data": page}
        if self.total is not None:        # 有的接口不回 count，这时不能硬塞
            resp["count"] = self.total
        return resp

    def fs_dir_getid(self, payload: dict) -> dict:
        self.calls.append(("fs_dir_getid", dict(payload)))
        return self._maybe_fail("fs_dir_getid") or {"state": True, "id": "ROOT"}

    def fs_history_receive_list(self, payload: dict) -> dict:
        self.calls.append(("fs_history_receive_list", dict(payload)))
        # ⚠️ 照 2026-10-06 真机返回写：`data` 是 **dict**（`{total, list}`），条目在 `data.list` 里
        return self._maybe_fail("fs_history_receive_list") or {
            "state": True,
            "data": {"total": 2, "list": [
                {"id": "rec1", "type": 7, "file_id": "3312870825789104125",
                 "parent_id": "c1", "parent_name": "115电视剧",
                 "file_name": "美剧【怪奇物语】1-4季 4K中字【255G】",
                 "create_time": 1759700000, "update_time": 1759700000},
                {"id": "rec2", "type": 7, "file_id": "",
                 "parent_id": "ROOT", "parent_name": "云下载",
                 "file_name": "主谋_The Mastermind.srt等2个文件",
                 "create_time": 1759700001, "update_time": 1759700001},
            ]},
        }

    # --- 写 ---------------------------------------------------------------
    def fs_makedirs(self, payload: dict) -> dict:
        self.calls.append(("fs_makedirs", dict(payload)))
        got = self._maybe_fail("fs_makedirs")
        if got is not None:
            return got
        name = payload.get("path") or ""
        if name not in self.made:
            self._n += 1
            self.made[name] = f"m{self._n}"
        # ⚠️ 真机形态：id 在 **`data.file_id`** 里，顶层**没有** id（2026-10-06 实测）
        return {"state": True, "data": {"file_id": self.made[name], "is_private": "0"}}

    def fs_dir_getid2(self, payload: dict) -> dict:
        self.calls.append(("fs_dir_getid2", dict(payload)))
        return {"state": True,
                "data": {"file_id": self.made.get(payload.get("path") or "", ""),
                         "is_private": "0"}}

    def fs_move(self, payload: dict) -> dict:
        self.calls.append(("fs_move", dict(payload)))
        return self._maybe_fail("fs_move") or {"state": True}

    def fs_rename(self, payload: dict) -> dict:
        self.calls.append(("fs_rename", dict(payload)))
        return self._maybe_fail("fs_rename") or {"state": True}

    def fs_delete(self, payload: dict) -> dict:
        self.calls.append(("fs_delete", dict(payload)))
        return self._maybe_fail("fs_delete") or {"state": True}

    # --- 辅助 -------------------------------------------------------------
    def payload_of(self, name: str) -> dict:
        for n, p in self.calls:
            if n == name:
                return p
        raise AssertionError(f"没调用过 {name}")


def make_tree(n_dirs: int, n_files: int) -> list[dict]:
    out = []
    for i in range(n_dirs):
        out.append({"cid": f"c{i}", "n": f"目录{i:04d}"})
    for i in range(n_files):
        out.append({"fid": f"f{i}", "n": f"文件{i:04d}.mp4", "s": 100})
    return out


def make_v(client: FakeClient, **cfg_kw) -> V115:
    cfg = Config()
    cfg.throttle_min = cfg.throttle_max = 0.0
    cfg.throttle_batch = 10 ** 9          # 单测里不要长休
    for k, v in cfg_kw.items():
        setattr(cfg, k, v)
    thr = Throttle(cfg=cfg, sleeper=lambda s: None)
    return V115(client, cfg, thr)


class TestListDir(unittest.TestCase):
    def test_必须翻页_否则静默丢掉九成八的数据(self):
        """`fs_files` 单次最多回 200 条（传 500 也只给 200）。

        实测本盘 `云下载` 一层就是 11,206 项 ⇒ 不翻页等于只看见 2%。
        """
        client = FakeClient(make_tree(250, 400))          # 共 650 项
        v = make_v(client)
        nodes = v.list_dir("ROOT")
        self.assertEqual(len(nodes), 650, "翻页没做全")
        self.assertEqual(sum(1 for n, _ in client.calls if n == "fs_files"), 4,
                         "650 项 / 每页 200 ⇒ 该发 4 次（200+200+200+50）")

    def test_dirs_only_走115原生的nf参数(self):
        """`nf=1` 是服务端过滤 —— 文件根本不回来，比自己滤更省流量也更不像脚本。"""
        client = FakeClient(make_tree(3, 7))
        v = make_v(client)
        nodes = v.list_dir("ROOT", dirs_only=True)
        p = client.payload_of("fs_files")
        self.assertEqual(p.get("nf"), 1, "dirs_only 必须传 nf=1")
        self.assertEqual(p.get("show_dir"), 1)
        self.assertEqual(len(nodes), 3)

    def test_翻页以count为准_没有count才退化为试探(self):
        """`count` 是总数 ⇒ 整数页正好停下，不多发一次。

        没有 `count` 时退化成长短判断（回空才停），并受 MAX_PAGES 兜底 ——
        这条是为「接口不认 offset」的极端情况留的，不能把工具转死。
        """
        # ① 有 count：200 项恰好一页 ⇒ 一次请求收工
        client = FakeClient(make_tree(200, 0))
        v = make_v(client)
        self.assertEqual(len(v.list_dir("ROOT")), 200)
        self.assertEqual(sum(1 for n, _ in client.calls if n == "fs_files"), 1,
                         "有 count 就该正好停下，不必再探一次")

        # ② 没有 count：靠「回空了」收尾 ⇒ 会多一次探测
        c2 = FakeClient(make_tree(200, 0))
        c2.total = None
        v2 = make_v(c2)
        self.assertEqual(len(v2.list_dir("ROOT")), 200)
        self.assertEqual(sum(1 for n, _ in c2.calls if n == "fs_files"), 2,
                         "缺 count 时该继续探到回空为止")

    def test_接口不认offset时有页数兜底(self):
        """最坏情况：`count` 缺失 + 每页都回满（offset 无效）⇒ 必须能被 MAX_PAGES 掐断。"""
        from app.v115 import MAX_PAGES
        full = make_tree(200, 0)
        c = FakeClient(full)
        c.total = None
        c.ignore_offset = True
        v = make_v(c)
        nodes = v.list_dir("ROOT")
        self.assertEqual(len(nodes), MAX_PAGES * 200, "该在 MAX_PAGES 处收手")
        self.assertEqual(sum(1 for n, _ in c.calls if n == "fs_files"), MAX_PAGES)

    def test_目录与文件的id各自取cid_fid(self):
        client = FakeClient([{"cid": "cX", "n": "一个目录"},
                             {"fid": "fY", "n": "一个文件.mp4", "s": 9}])
        v = make_v(client)
        d, f = v.list_dir("ROOT")
        self.assertTrue(d.is_dir)
        self.assertEqual(d.id, "cX")
        self.assertFalse(f.is_dir)
        self.assertEqual(f.id, "fY")
        self.assertEqual(f.ext, "mp4")


class TestMoveConflict(unittest.TestCase):
    def test_移动必须显式带keep_both(self):
        """🔴 本工具最不能出的一类错。

        p115client 的 `fs_move` 对**文件**默认走 `replace` —— 目标重名就**覆盖、不可恢复**。
        所以每一次移动都必须显式告诉 115「重名就改名并存」。
        """
        client = FakeClient()
        v = make_v(client)
        v.move(["f1", "f2"], "pidX")
        p = client.payload_of("fs_move")
        policy = json.loads(p["conflict_policy"])
        self.assertEqual(policy, {"f1": {"action": "keep_both"},
                                  "f2": {"action": "keep_both"}})
        self.assertEqual(p["pid"], "pidX")
        # `fid[0]`/`fid[1]` 而不是 `fid[]` 挂 list —— 照 p115client 自己处理 id 序列的写法
        self.assertEqual(p["fid[0]"], "f1")
        self.assertEqual(p["fid[1]"], "f2")
        self.assertNotIn("fid[]", p, "⛔ 别再退回 `fid[]`+list：那段序列化行为随 requests 版本变")

    def test_空列表不发请求(self):
        client = FakeClient()
        v = make_v(client)
        self.assertTrue(v.move([], "pidX"))
        self.assertEqual(client.calls, [])


class TestEnsureDir(unittest.TestCase):
    def test_逐级建_且同一路径不重复发请求(self):
        """计划的 mkdir 有 1183 条，很多是同一父路径下的兄弟 —— 缓存能省掉大半请求。"""
        client = FakeClient()
        v = make_v(client)
        cid1 = v.ensure_dir("DLDSS")
        cid2 = v.ensure_dir("DLDSS")
        self.assertEqual(cid1, cid2)
        self.assertEqual(sum(1 for n, _ in client.calls if n == "fs_makedirs"), 1,
                         "同一路径第二次调用不该再发请求")

    def test_多级路径逐段建(self):
        client = FakeClient()
        v = make_v(client)
        v.ensure_dir("系列/DLDSS")
        names = [p["path"] for n, p in client.calls if n == "fs_makedirs"]
        self.assertEqual(names, ["系列", "DLDSS"], "必须逐级建，不能一次塞整串")

    def test_根路径返回根id且不发请求(self):
        client = FakeClient()
        v = make_v(client)
        v._targets[""] = "ROOT"
        self.assertEqual(v.ensure_dir(""), "ROOT")
        self.assertEqual(client.calls, [])

    def test_目录id在data_file_id里而不是顶层id(self):
        """⛔ 2026-10-06 真机实测：两个查目录的接口**形态不一样**。

            fs_dir_getid   → {"state":true, "id":"438124340056368324"}            ← 顶层 id
            fs_dir_getid2  → {"state":true, "data":{"file_id":"3533…"}}           ← data.file_id

        `fs_makedirs` 就是 `fs_dir_getid2(is_create=1)` 的封装 ⇒ 同一形态。
        只按顶层 `id`/`cid` 取 ⇒ `ensure_dir()` 拿不到 id ⇒ 抛「建目录失败」
        ⇒ **全量计划里 1,183 条 mkdir 第一步就全崩**。
        """
        client = FakeClient()
        v = make_v(client)
        self.assertTrue(v.ensure_dir("DLDSS"), "必须能从 data.file_id 里取到 id")
        raw = client.fs_makedirs({"path": "另一个"})
        self.assertNotIn("id", raw, "真机返回的顶层就是没有 id —— 免得以后有人「顺手」改回去")
        self.assertTrue(raw["data"]["file_id"])

    def test_拿不到id才会走兜底第二次请求(self):
        """正常情况（`data.file_id` 有值）只发 1 次请求，别白多花一次。"""
        client = FakeClient()
        v = make_v(client)
        v.ensure_dir("DLDSS")
        self.assertEqual(sum(1 for n, _ in client.calls if n == "fs_dir_getid2"), 0,
                         "能直接拿到 id 就不该调兜底")
        self.assertEqual(sum(1 for n, _ in client.calls if n == "fs_makedirs"), 1)


class TestError(unittest.TestCase):
    def test_115拒绝要抛_V115Error_并且计入节流器的失败计数(self):
        """「最近接收」和风控的共同表现就是接口报错 —— 失败必须被节流器看见。"""
        client = FakeClient(patch={"fs_move": {"state": False, "error": "请求过于频繁"}})
        v = make_v(client)
        with self.assertRaises(V115Error) as cm:
            v.move(["f1"], "pid")
        self.assertIn("请求过于频繁", str(cm.exception))
        self.assertEqual(v.throttle.stats.streak, 1, "失败没被节流器记账")
        v.throttle.cfg.fail_limit = 9
        v.throttle.ok()
        self.assertEqual(v.throttle.stats.streak, 0)

    def test_网络异常也要归一成_V115Error(self):
        client = FakeClient(patch={"fs_delete": ConnectionError("connection reset")})
        v = make_v(client)
        with self.assertRaises(V115Error):
            v.delete(["f1"])

    def test_每个请求都过闸_没有一个出口能绕过(self):
        """出口唯一性回归：所有公开方法各调一次，请求数必须等于节流器计数。"""
        client = FakeClient(make_tree(1, 1))
        v = make_v(client)
        v.root_cid()
        v.list_dir("ROOT")
        v.ensure_dir("A")
        v.move(["f1"], "p")
        v.rename([("f1", "新名.mp4")])
        v.delete(["f1"])
        self.assertEqual(v.calls, v.throttle.stats.requests,
                         "有请求绕过了 throttle.before_request()")


class TestRenameDelete(unittest.TestCase):
    def test_批量改名一次请求带多组(self):
        client = FakeClient()
        v = make_v(client)
        v.rename([("f1", "DLDSS-532.mp4"), ("f2", "DLDSS-533.mp4")])
        p = client.payload_of("fs_rename")
        self.assertEqual(p["files_new_name[f1]"], "DLDSS-532.mp4")
        self.assertEqual(p["files_new_name[f2]"], "DLDSS-533.mp4")

    def test_删除是进回收站的接口(self):
        client = FakeClient()
        v = make_v(client)
        v.delete(["f1", "f2"])
        p = client.payload_of("fs_delete")
        self.assertEqual(p["fid[0]"], "f1")
        self.assertEqual(p["fid[1]"], "f2")


class TestReceiveList(unittest.TestCase):
    """⚠️ 这个接口的返回形态跟 `fs_files` **完全是两套**，用真机样本钉住。"""

    def test_data是dict不是list(self):
        """曾经按 `data` 是 list 解析 ⇒ **静默拿到空表** ⇒ 自动化那半直接失效。"""
        v = make_v(FakeClient())
        items = v.receive_list(limit=50)
        self.assertEqual(len(items), 2, "条目在 data.list 里，不是 data 本身")

    def test_字段口径(self):
        v = make_v(FakeClient())
        it = v.receive_list(limit=50)[0]
        self.assertEqual(it["record_id"], "rec1", "`id` 是接收记录的 id，不是文件的")
        self.assertEqual(it["file_id"], "3312870825789104125")
        self.assertEqual(it["file_name"], "美剧【怪奇物语】1-4季 4K中字【255G】")
        self.assertEqual(it["parent_id"], "c1")
        self.assertEqual(it["parent_name"], "115电视剧", "落在哪个目录 —— 发现新目录全靠它")
        self.assertEqual(it["time"], 1759700000)

    def test_空file_id不等于目录(self):
        """`file_id=''` 是多文件聚合项（`xxx.srt等2个文件`），**不是目录**。"""
        v = make_v(FakeClient())
        agg = v.receive_list(limit=50)[1]
        self.assertEqual(agg["file_id"], "")
        self.assertEqual(agg["parent_name"], "云下载")
        self.assertNotIn("is_dir", agg, "本接口根本不说它是不是目录，别自己编这个字段")

    def test_限额真的传下去(self):
        client = FakeClient()
        make_v(client).receive_list(limit=7)
        self.assertEqual(client.payload_of("fs_history_receive_list")["limit"], 7)

    def test_万一哪天data直接给list也不能崩(self):
        client = FakeClient()
        client.fs_history_receive_list = lambda payload: {
            "state": True,
            "data": [{"id": "r", "file_id": "f", "file_name": "x",
                      "parent_id": "p", "parent_name": "P"}]}
        items = make_v(client).receive_list(limit=5)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["file_name"], "x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
