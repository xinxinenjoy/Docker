"""网页层单测 —— 全离线，一个 115 请求都不发。

⭐ 这里最有价值的三条，都是「不测就一定会踩」的：

  ① **演练（dry-run）不许要求 cookie。**
     演练的用处正是「**还没配好账号时先看看这份计划长什么样**」。
     为了演练去要一个 cookie 是本末倒置 —— 而且它的症状是「一按演练就报没账号」，
     让人以为是账号问题，去折腾半天 cookie。

  ② **网页执行的断点绝不能写进 `run-state.json`。**
     网页跑的是「勾选的子集」，pass 计数跟整份计划不是一回事。
     两处共用同一个文件 ⇒ 之后从命令行跑整份计划时，会拿子集的计数当起点、
     **静默跳过一大批动作**（看起来像「跑完了但什么都没做」）。

  ③ **回执文件名是唯一一处拿用户输入拼路径的地方**，必须锁死形态，
     否则 `../` 直接越出去读别的文件。

⚠️ `ACCESS_TOKEN` 是**模块导入时**读的，所以必须在 `import app.web` **之前**清掉环境变量，
   否则（外面 shell 恰好设了的话）整套测试会全部 401。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ENVS = ("DATA_DIR", "TREE_DIR", "TREE_FILE", "ACCESS_TOKEN", "P115_COOKIE",
         "ACCOUNTS_FILE", "ROOT_PATH", "DRY_RUN", "PORT", "ACCOUNT_ID")
_SAVED_ENV = {k: os.environ.get(k) for k in _ENVS}
for _k in _ENVS:
    os.environ.pop(_k, None)

from fastapi.testclient import TestClient      # noqa: E402

from app import config, web                    # noqa: E402

# 一棵够小、但足以产出「系列聚合 / 改名 / 清理」三类动作的树。
TREE = "\n".join([
    "|——根目录",
    "| |-云下载",
    "| | |-DLDSS-532",
    "| | | |-4k688.com@DLDSS-532.mp4",
    "| | |-DLDSS-533",
    "| | | |-4k688.com@DLDSS-533.mp4",
    "| | |-DLDSS-534",
    "| | | |-4k688.com@DLDSS-534.mp4",
    "| | |-老电影.2019.1080p.mp4",
    "| | |-聚合全網H直播.txt",
])


class WebCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="115org-web-"))
        (self.tmp / "tree").mkdir(parents=True, exist_ok=True)
        (self.tmp / "tree" / "t.txt").write_text(TREE, encoding="utf-8")
        os.environ["DATA_DIR"] = str(self.tmp)
        os.environ["TREE_DIR"] = str(self.tmp / "tree")
        self._env_before = set(os.environ)          # 给 tearDown 兜底用，见下
        config.reload_overrides()
        web.PLANS.reset()
        web.RUNNER = None
        self.c = TestClient(web.app)

    def tearDown(self) -> None:
        for k, v in _SAVED_ENV.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # 兜底：本用例（含被测代码）**新加**的 env 一律清掉 —— 别指望每个用例都记得还原。
        # ⚠️ 只清「新加的键」，改不掉「把已有键改成别的值」的那种；那种请自己 try/finally。
        for k in set(os.environ) - self._env_before:
            os.environ.pop(k, None)
        config.reload_overrides()
        web.PLANS.reset()
        web.RUNNER = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 工具
    def build_plan(self) -> dict:
        r = self.c.post("/api/plan", json={"depth": 2, "force": True})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def wait_run(self, timeout: float = 20.0) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            s = self.c.get("/api/run").json()
            if not s.get("running"):
                return s
            time.sleep(0.08)
        self.fail("执行没有在 %.0f 秒内结束" % timeout)


# ---------------------------------------------------------------- 基础
class TestBasics(WebCase):
    def test_health(self):
        d = self.c.get("/api/health").json()
        self.assertTrue(d["ok"])
        self.assertFalse(d["auth"], "没设 ACCESS_TOKEN 时不该要求口令")

    def test_首页能打开(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("115 网盘整理", r.text)

    def test_state_首屏一次拿齐(self):
        d = self.c.get("/api/state").json()
        for k in ("auth", "port", "root_path", "cookie", "tree", "plan", "run", "config_file"):
            self.assertIn(k, d)
        self.assertTrue(d["tree"]["ok"])
        self.assertFalse(d["plan"]["built"], "还没生成计划")
        self.assertFalse(d["cookie"]["ok"], "没配 cookie 时要如实说没有")

    def test_默认端口是8766(self):
        self.assertEqual(self.c.get("/api/state").json()["port"], 8766)


# ---------------------------------------------------------------- 设置
class TestSettingsApi(WebCase):
    def test_拿到字段与来源(self):
        d = self.c.get("/api/settings").json()
        keys = [f["key"] for f in d["fields"]]
        self.assertIn("series_min", keys)
        self.assertIn("dry_run", keys)
        self.assertEqual(d["groups"][0], "115 账号")
        # ⚠️ 不能断言「全部都是 default」—— 测试自己就会设 TREE_DIR（那是**对的**，它确实是 env）。
        #    这里挑两个业务参数验来源就够了。
        src = {f["key"]: f["source"] for f in d["fields"]}
        self.assertEqual(src["series_min"], "default")
        self.assertEqual(src["dry_run"], "default")

    def test_保存后来源变_json_并且真的生效(self):
        d = self.c.put("/api/settings", json={"values": {"series_min": 7}}).json()
        self.assertIn("series_min", d["saved"])
        f = next(x for x in d["fields"] if x["key"] == "series_min")
        self.assertEqual(f["value"], 7)
        self.assertEqual(f["source"], "json")
        # 关键：config.json 落盘 + `config.load()` 立刻看得到（懒加载缓存要重读）
        self.assertEqual(config.load().series_min, 7)
        on_disk = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["series_min"], 7)

    def test_坏值被拒但好值照存(self):
        d = self.c.put("/api/settings", json={"values": {"series_min": 6, "junk_action": "??"}}).json()
        self.assertIn("series_min", d["saved"])
        self.assertEqual([x["key"] for x in d["rejected"]], ["junk_action"])

    def test_恢复默认(self):
        self.c.put("/api/settings", json={"values": {"series_min": 7}})
        self.c.post("/api/settings/reset", json={"all": True})
        self.assertEqual(config.load().series_min, 3)
        self.assertFalse((self.tmp / "config.json").exists())

    def test_环境变量钉住的参数会标出来(self):
        # 🔴 这个 env 是**本用例自己设的**，而 `WebCase._ENVS` 里没有 `SERIES_MIN`
        #    ⇒ tearDown 根本不管它。以前没在 finally 里摘掉，这个 "9" 就漏给后面所有用例：
        #    `TestPlanApi` 按 series_min=9 算计划（3 个 DLDSS 不再聚合成系列）
        #    ⇒ 条目数、搜索命中、估算、执行选中数一起崩（实测 5 个用例挂在这上面）。
        #    ⚠️ 想加新的临时 env，请一律照这个 try/finally 写，别裸设。
        os.environ["SERIES_MIN"] = "9"
        try:
            config.reload_overrides()
            d = self.c.get("/api/settings").json()
            self.assertIn("SERIES_MIN", d["env_locked"])
            f = next(x for x in d["fields"] if x["key"] == "series_min")
            self.assertEqual(f["source"], "env")
        finally:
            os.environ.pop("SERIES_MIN", None)
            config.reload_overrides()


# ---------------------------------------------------------------- 账号
class TestAccountsApi(WebCase):
    def test_列出共用账号(self):
        (self.tmp / "accounts.json").write_text(json.dumps([
            {"id": "a1", "name": "主号", "cookie": "UID=1;CID=c;SEID=s"},
            {"id": "a2", "name": "备用", "cookie": ""},
        ], ensure_ascii=False), encoding="utf-8")
        d = self.c.get("/api/accounts").json()
        self.assertTrue(d["exists"])
        self.assertEqual([x["name"] for x in d["items"]], ["主号", "备用"])
        self.assertTrue(d["items"][0]["usable"])
        self.assertFalse(d["items"][1]["usable"], "空 cookie 不能算可用")
        self.assertEqual(d["active_id"], "a1")
        # ⚠️ 别直接 grep "cookie" —— 响应里本来就有 `cookie_tail` 这个键名。
        #    要证的是「**完整的那串**没回出去」。
        blob = json.dumps(d)
        self.assertNotIn("UID=1;CID=c;SEID=s", blob, "接口绝不能把完整 cookie 回出去")
        self.assertIn("…", d["items"][0]["cookie_tail"])

    def test_没有账号文件也不报错(self):
        d = self.c.get("/api/accounts").json()
        self.assertFalse(d["exists"])
        self.assertEqual(d["items"], [])


# ---------------------------------------------------------------- 计划
class TestPlanApi(WebCase):
    def test_没生成计划时列条目要报错(self):
        r = self.c.get("/api/plan/ops")
        self.assertEqual(r.status_code, 400)
        self.assertIn("还没生成计划", r.json()["detail"])

    def test_生成计划(self):
        d = self.build_plan()
        self.assertTrue(d["built"])
        self.assertGreater(d["counts"]["actions"], 0)
        self.assertIn("estimate", d)
        self.assertGreater(d["tree_stats"]["root_entries"], 0, "根下条目数要给出来（耗时估算靠它）")

    def test_计划缓存_参数变了要重建(self):
        a = self.build_plan()
        self.assertEqual(a["settings"]["series_min"], 3)
        self.c.put("/api/settings", json={"values": {"series_min": 2}})
        b = self.c.post("/api/plan", json={"depth": 2}).json()
        # ⚠️ 不能拿 `created` 比 —— 同一秒内两次生成，时间戳字符串一模一样。
        #    拿「这份计划是按哪套参数算的」比才可靠，而且它本身也是页面要显示的信息。
        self.assertEqual(b["settings"]["series_min"], 2, "参数变了必须重算，不能吃旧缓存")

    def test_条目分页与过滤(self):
        self.build_plan()
        d = self.c.get("/api/plan/ops?limit=2").json()
        self.assertEqual(len(d["items"]), 2)
        self.assertGreater(d["total"], 2)
        kinds = d["kinds"]
        one = sorted(kinds, key=lambda k: -kinds[k])[0]
        f = self.c.get(f"/api/plan/ops?kind={one}&limit=500").json()
        self.assertTrue(all(x["kind"] == one for x in f["items"]))
        self.assertEqual(f["total"], kinds[one])

    def test_搜索命中路径或目标(self):
        self.build_plan()
        d = self.c.get("/api/plan/ops?q=DLDSS-532&limit=500").json()
        self.assertGreater(d["total"], 0)
        for x in d["items"]:
            blob = f"{x['path']} {x['target']} {x['new_name']}"
            self.assertIn("DLDSS-532", blob)

    def test_only_indices_回全部匹配而不是当前页(self):
        """「勾选全部筛选结果」靠它 —— 只勾到当前页会让人以为勾全了。"""
        self.build_plan()
        allops = self.c.get("/api/plan/ops?limit=1").json()
        d = self.c.get("/api/plan/ops?only_indices=1").json()
        self.assertEqual(len(d["indices"]), allops["total"])
        self.assertEqual(d["indices"], sorted(d["indices"]), "索引要按计划顺序给")
        self.assertNotIn("items", d)

    def test_估算_勾得少耗时也要跟着少(self):
        self.build_plan()
        idx = self.c.get("/api/plan/ops?limit=300").json()["items"]
        few = [x["i"] for x in idx[:2]]
        a = self.c.post("/api/plan/estimate", json={"indices": few}).json()
        b = self.c.post("/api/plan/estimate", json={}).json()
        self.assertEqual(a["selected"], 2)
        self.assertLessEqual(a["requests"], b["requests"])


# ---------------------------------------------------------------- 执行
class TestRunApi(WebCase):
    def test_演练不要求_cookie(self):
        """⭐ 本条是这次特意修的：演练的用处就是「还没配账号时先看一眼」。"""
        self.build_plan()
        idx = [x["i"] for x in self.c.get("/api/plan/ops?limit=10").json()["items"]]
        r = self.c.post("/api/run", json={"indices": idx, "dry_run": True})
        self.assertEqual(r.status_code, 200, r.text)
        s = self.wait_run()
        self.assertEqual(s.get("error"), "", f"不该报错：{s.get('error')}")
        self.assertTrue(s["result"], "要有回执")
        self.assertEqual(s["result"]["requests"], 0, "演练必须一个请求都不发")
        self.assertTrue(s["result"]["dry_run"])

    def test_真跑没账号要给出清楚的错_且不动手(self):
        self.build_plan()
        idx = [x["i"] for x in self.c.get("/api/plan/ops?limit=5").json()["items"]]
        self.c.post("/api/run", json={"indices": idx, "dry_run": False})
        s = self.wait_run()
        self.assertIn("cookie", s["error"].lower())
        self.assertIsNone(s["result"], "没账号就不该产出回执（说明一个请求都没发）")

    def test_断点文件必须是独立的一份(self):
        """⭐ 网页跑子集 ⇒ 绝不能写 `run-state.json`（会污染命令行的整份计划断点）。"""
        self.build_plan()
        idx = [x["i"] for x in self.c.get("/api/plan/ops?limit=5").json()["items"]]
        self.c.post("/api/run", json={"indices": idx, "dry_run": True})
        self.wait_run()
        self.assertTrue((self.tmp / "web-state.json").exists(), "网页的断点该写这里")
        self.assertFalse((self.tmp / "run-state.json").exists(),
                         "🔴 命令行那份断点不许被网页碰")

    def test_只能跑选中那几条(self):
        self.build_plan()
        idx = [x["i"] for x in self.c.get("/api/plan/ops?limit=2").json()["items"]]
        self.c.post("/api/run", json={"indices": idx, "dry_run": True})
        s = self.wait_run()
        self.assertEqual(s["selected"], 2)
        self.assertLessEqual(s["result"]["planned"], 2)

    def test_一条都不勾要被拦住(self):
        self.build_plan()
        r = self.c.post("/api/run", json={"indices": [], "dry_run": True})
        self.assertEqual(r.status_code, 400)

    def test_没生成计划不许跑(self):
        r = self.c.post("/api/run", json={"indices": [0], "dry_run": True})
        self.assertEqual(r.status_code, 400)

    def test_日志能按序号增量取(self):
        self.build_plan()
        idx = [x["i"] for x in self.c.get("/api/plan/ops?limit=3").json()["items"]]
        self.c.post("/api/run", json={"indices": idx, "dry_run": True})
        s = self.wait_run()
        self.assertGreater(s["log_seq"], 0)
        self.assertTrue(s["log"], "要留下日志给页面看")
        again = self.c.get(f"/api/run?after={s['log_seq']}").json()
        self.assertEqual(again["log"], [], "带 after 时不该重复回已看过的行")

    def test_停止_没在跑时要说清楚(self):
        d = self.c.post("/api/run/stop").json()
        self.assertFalse(d["ok"])
        self.assertIn("没有在跑", d["msg"])

    def test_真跑开关只影响这一次_不写回配置(self):
        """⛔ 否则「我明明只试跑一次」会变成「以后每次都是真跑」。"""
        self.build_plan()
        idx = [x["i"] for x in self.c.get("/api/plan/ops?limit=3").json()["items"]]
        self.c.post("/api/run", json={"indices": idx, "dry_run": False})
        self.wait_run()
        self.assertFalse((self.tmp / "config.json").exists(), "不该顺手写一份配置出来")
        self.assertTrue(config.load().dry_run, "配置里的 DRY_RUN 必须还是 1")


# ---------------------------------------------------------------- 回执
class TestRunsApi(WebCase):
    def _one_run(self) -> str:
        self.build_plan()
        idx = [x["i"] for x in self.c.get("/api/plan/ops?limit=3").json()["items"]]
        self.c.post("/api/run", json={"indices": idx, "dry_run": True})
        self.wait_run()
        return self.c.get("/api/runs").json()["items"][0]["stem"]

    def test_回执列表与详情(self):
        stem = self._one_run()
        items = self.c.get("/api/runs").json()["items"]
        self.assertTrue(items[0]["dry_run"])
        d = self.c.get(f"/api/runs/{stem}").json()
        self.assertIn("markdown", d)
        self.assertIn("json", d)

    def test_回执名不许越界(self):
        """🔴 唯一一处拿用户输入拼路径的地方。

        ⚠️ 别指望用 HTTP 打 `../` 来测 —— Starlette 会在路由**之前**把 `/api/runs/../accounts`
           规范化成 `/api/accounts`，于是它 200 了，但那是**另一个端点**，不是目录穿越。
           真正要钉的是那个守卫本身，所以直接调函数。
        """
        from fastapi import HTTPException
        for bad in ["../accounts", "run-../../etc/passwd", "run-a/b", "run-a\\b", "..", "nope"]:
            with self.assertRaises(HTTPException, msg=f"{bad} 应该被挡住"):
                web.get_run(bad)
        # 形态合法但文件不存在 ⇒ 404，不是读到别的东西
        with self.assertRaises(HTTPException) as cm:
            web.get_run("run-19700101-000000")
        self.assertEqual(cm.exception.status_code, 404)
        # 正常的那份仍然读得到
        stem = self._one_run()
        self.assertIn("markdown", web.get_run(stem))

    def test_列回执时坏文件不该炸整页(self):
        (self.tmp / "reports").mkdir(exist_ok=True)
        (self.tmp / "reports" / "run-坏.json").write_text("{坏了", encoding="utf-8")
        d = self.c.get("/api/runs").json()
        self.assertTrue(any(x.get("broken") for x in d["items"]))


# ---------------------------------------------------------------- 口令
class TestAuth(WebCase):
    def test_设了口令才拦(self):
        web.ACCESS_TOKEN = "s3cret"
        try:
            self.assertEqual(self.c.get("/api/health").status_code, 200, "健康检查不拦")
            self.assertEqual(self.c.get("/api/state").status_code, 401)
            ok = self.c.get("/api/state", headers={"Authorization": "Bearer s3cret"})
            self.assertEqual(ok.status_code, 200)
            bad = self.c.get("/api/state", headers={"Authorization": "Bearer nope"})
            self.assertEqual(bad.status_code, 401)
        finally:
            web.ACCESS_TOKEN = ""


# ---------------------------------------------------------------- 选定文件夹（功能 2）
class TestForDirsApi(WebCase):
    def test_没选文件夹报错(self):
        r = self.c.post("/api/plan/for-dirs", json={"dirs": []})
        self.assertEqual(r.status_code, 400)

    def test_生成选定文件夹计划(self):
        d = self.c.post("/api/plan/for-dirs", json={"dirs": ["DLDSS-532", "DLDSS-533"]}).json()
        self.assertTrue(d["built"])
        self.assertEqual(d["mode"], "for_dirs")
        self.assertIn("DLDSS-532", d["dirs"])
        self.assertGreater(d["counts"]["actions"], 0)

    def test_换个文件夹_计划跟着变(self):
        a = self.c.post("/api/plan/for-dirs", json={"dirs": ["DLDSS-532"]}).json()
        b = self.c.post("/api/plan/for-dirs", json={"dirs": ["老电影.2019.1080p.mp4"]}).json()
        self.assertNotEqual(a["dirs"], b["dirs"])

    def test_选定文件夹_只看范围内(self):
        d = self.c.post("/api/plan/for-dirs", json={"dirs": ["DLDSS-532"]}).json()
        ops = self.c.get("/api/plan/ops?limit=500").json()["items"]
        for x in ops:
            self.assertIn("DLDSS-532", x["path"], "只该出现选中目录的动作")

    def test_整理当前目录_没计划报错(self):
        """没生成计划、也没传 dirs ⇒ 报错而不是瞎跑。"""
        r = self.c.post("/api/run/for-dirs", json={})
        self.assertEqual(r.status_code, 400)

    def test_tree_level_离线读树零请求(self):
        """选定文件夹的「目录树」来源 —— 离线、不需要 cookie。"""
        d = self.c.get("/api/tree/level?path=").json()
        self.assertEqual(d["node"]["name"], "云下载")
        names = {i["name"] for i in d["items"]}
        self.assertIn("DLDSS-532", names)
        self.assertIn("老电影.2019.1080p.mp4", names, "根下散文件也该列出来")

    def test_tree_level_子层(self):
        d = self.c.get("/api/tree/level?path=DLDSS-532").json()
        self.assertTrue(any(i["name"] == "4k688.com@DLDSS-532.mp4" for i in d["items"]))

    def test_tree_level_不存在的目录报错(self):
        r = self.c.get("/api/tree/level?path=不存在")
        self.assertEqual(r.status_code, 400)


# ---------------------------------------------------------------- 入站口
class TestInboxApi(WebCase):
    def test_状态返回目录与间隔(self):
        d = self.c.get("/api/inbox").json()
        self.assertEqual(d["dir"], "待整理")
        self.assertGreater(d["poll_interval"], 0)
        self.assertFalse(d["running"])

    def test_整理当前目录_演练也要cookie(self):
        """入站口整理**必须先列入站口**（读请求），所以演练也要 cookie ——
        这是与「全盘演练」不同的地方（全盘演练直接离线给空计划）。"""
        r = self.c.post("/api/inbox/run", json={"dry_run": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("cookie", r.json()["detail"].lower())


# ---------------------------------------------------------------- 扫描
class TestScanApi(WebCase):
    def test_扫描需要cookie(self):
        """扫描 = 在线列目录，必须真账号。"""
        r = self.c.get("/api/scan")
        self.assertEqual(r.status_code, 400)
        self.assertIn("cookie", r.json()["detail"].lower())

    def test_扫描成功_用假client(self):
        """有 cookie 时在线列一层 —— 用假 client 顶掉真网络。"""
        from unittest import mock
        from app.scan import Scanner
        from app.v115 import Node

        os.environ["P115_COOKIE"] = "UID=1;CID=c;SEID=s"
        config.reload_overrides()

        class FakeClient:
            def fs_files(self, payload):
                return {"state": True, "count": 2, "data": [
                    {"cid": "d1", "n": "DLDSS-532", "fid": None},
                    {"fid": "f1", "n": "x.mp4"},
                ]}

            def fs_dir_getid(self, payload):
                return {"state": True, "id": "ROOT"}

        with mock.patch("app.web.build_client", return_value=FakeClient()):
            d = self.c.get("/api/scan?path=").json()
        items = {i["name"]: i for i in d["items"]}
        self.assertIn("DLDSS-532", items)
        self.assertTrue(items["DLDSS-532"]["is_dir"])
        self.assertIn("x.mp4", items)
        self.assertFalse(items["x.mp4"]["is_dir"])
        self.assertEqual(items["DLDSS-532"]["path"], "DLDSS-532")


if __name__ == "__main__":
    unittest.main(verbosity=2)
