"""网页层回归 —— 状态码、参数校验、路径穿越防护、只读预览。

⚠️ 用 `TestClient`（fastapi 自带），不起真服务、不联网。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

try:
    from fastapi.testclient import TestClient
except Exception:                                    # pragma: no cover
    TestClient = None

BAR_TREE = """|——云下载
| |-电影
| | |-功夫片（2026）
| | | |-a.mkv
| | | |-b.mp4
| |-电视剧
| | |-某剧
| | | |-S01E01.mkv
| |-短剧
| | |-短剧A
| | | |-c.mp4
"""


@unittest.skipIf(TestClient is None, "没装 fastapi/httpx")
class TestWeb(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.td.name)
        (self.root / "tree").mkdir(parents=True, exist_ok=True)
        (self.root / "tree" / "tree.txt").write_text(BAR_TREE, encoding="utf-8")
        (self.root / "out").mkdir(parents=True, exist_ok=True)

        self._env = {k: os.environ.get(k) for k in
                     ("DATA_DIR", "OUTPUT_DIR", "TREE_DIR", "STRM_PREFIX",
                      "ACCESS_TOKEN", "REMOTE_ROOT", "INCLUDE_DIRS")}
        os.environ.update({
            "DATA_DIR": str(self.root),
            "OUTPUT_DIR": str(self.root / "out"),
            "TREE_DIR": str(self.root / "tree"),
            # ⚠️ 同步根必须设成**树里真实存在**的目录 ——
            #    默认值 `影音` 在这份测试树里不存在，会解析失败。
            #    另外 `REMOTE_ROOT=""` 是**没用的**（`_env()` 空值会回落到默认），
            #    必须给一个真名字。
            "REMOTE_ROOT": "云下载",
        })
        for k in ("ACCESS_TOKEN", "INCLUDE_DIRS",
                  # 🔴 `STRM_PREFIX` 是**网页专属字段**（`settings.WEB_ONLY_ENVS`）
                  #    ⇒ 它**不读环境变量**，只能落在 `data/config.json` 里。
                  #    所以下面用 `save_json` 写，而不是设环境变量。
                  "STRM_PREFIX"):
            os.environ.pop(k, None)
        from app import settings as settings_mod
        settings_mod.save_json(self.root, {"strm_prefix": "https://example.test/d"})

        from app import config as cm
        cm.reload_overrides()
        import importlib
        from app import web as web_mod
        importlib.reload(web_mod)
        self.web = web_mod
        self.client = TestClient(web_mod.app)

    def tearDown(self):
        # ⚠️ 后台线程可能还在跑 —— 等它结束再删临时目录，
        #    否则 Windows 上会报 `WinError 32 另一个程序正在使用此文件`。
        r = self.web._runner()
        t = getattr(r, "_thread", None)
        if t and t.is_alive():
            t.join(timeout=10)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        from app import config as cm
        cm.reload_overrides()
        self.td.cleanup()

    # ------------------------------------------------------------------ 基础
    def test_首页能开(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("115 STRM", r.text)

    def test_health(self):
        self.assertEqual(self.client.get("/api/health").json()["ok"], True)

    def test_无口令时全通(self):
        self.assertEqual(self.client.get("/api/state").status_code, 200)

    def test_state_含关键字段(self):
        d = self.client.get("/api/state").json()
        for k in ("data_dir", "output_dir", "strm_prefix", "tree", "cookie", "pending", "run"):
            self.assertIn(k, d)

    def test_state_含这一轮的步骤表(self):
        """🔴 前端照这些字段渲染，缺一个就白屏 —— 钉住契约。

        ⭐ `steps` 是「这一轮会做什么」的**单一事实源**（后端 `pipeline.SYNC_STEPS`）,
           前端不自己抄一份（本项目吃过「两处各写一遍必然分叉」的亏）。
           原来这里断言的是 `modes` / `sync_mode` / `life`，去掉模式后都不存在了。
        """
        d = self.client.get("/api/state").json()
        self.assertIn("steps", d)
        self.assertEqual([s["key"] for s in d["steps"]],
                         ["export", "parse", "diff", "write", "tidy"])
        for s in d["steps"]:
            self.assertTrue(s["title"].strip() and s["help"].strip(),
                            f'{s["key"]} 缺标题或说明')
        # ⛔ 模式相关的字段**不该再出现** —— 出现就说明没删干净
        for gone in ("modes", "route_cn", "sync_mode", "auto_export", "life"):
            self.assertNotIn(gone, d, f"{gone} 应当已经删掉")

    def test_tree_被识别(self):
        d = self.client.get("/api/state").json()
        self.assertTrue(d["tree"]["ok"])

    # ------------------------------------------------------------------ 设置
    def test_设置读写(self):
        r = self.client.put("/api/settings", json={"values": {"throttle_min": "2.5"}})
        self.assertEqual(r.status_code, 200)
        self.assertIn("throttle_min", r.json()["saved"])
        d = self.client.get("/api/settings").json()
        by = {f["key"]: f for f in d["fields"]}
        self.assertAlmostEqual(float(by["throttle_min"]["value"]), 2.5)

    def test_设置拒绝非法值(self):
        r = self.client.put("/api/settings", json={"values": {"schedule_cron": "不是cron"}})
        self.assertIn("schedule_cron", r.json()["rejected"])

    def test_设置重置(self):
        self.client.put("/api/settings", json={"values": {"throttle_min": "5"}})
        r = self.client.post("/api/settings/reset", json={"keys": ["throttle_min"], "all": False})
        self.assertIn("throttle_min", r.json()["removed"])

    # -------------------------------------------------- 前缀必填 + 「网页专属」契约
    def test_state_报出前缀就绪(self):
        """🔴 `prefix_ready` 是前端「开跑前守卫」的开关 —— 缺了它前端就静默放行。"""
        self.assertIs(self.client.get("/api/state").json()["prefix_ready"], True)

    def test_settings_契约含必填与网页专属(self):
        """前端照这些键渲染「首次配置清单」和「还留着环境变量」的告警 —— 钉住契约。"""
        d = self.client.get("/api/settings").json()
        for k in ("required", "missing", "config_file", "overrides"):
            self.assertIn(k, d)
        self.assertIn("strm_prefix", d["required"])
        self.assertEqual(d["missing"], [], "setUp 里已经填了前缀，不该再报缺")
        for f in d["fields"]:
            self.assertIn("web_only", f)
            self.assertIn("stale_env", f)
        sp = {f["key"]: f for f in d["fields"]}["strm_prefix"]
        self.assertTrue(sp["web_only"], "strm_prefix 必须是「网页专属」")
        self.assertNotEqual(sp["source"], "环境变量", "网页专属字段永远不该报「环境变量」")

    def test_settings_前缀空时进missing(self):
        from app import settings as sm
        sm.reset_json(self.root)          # 清空 config.json ⇒ 前缀回到出厂的空值
        d = self.client.get("/api/settings").json()
        self.assertIn("strm_prefix", d["missing"])

    def test_settings_报出stale_env但不改source(self):
        """`.env` 里还留着 `STRM_PREFIX` ⇒ 要**显式报出来**，别静默忽略。"""
        os.environ["STRM_PREFIX"] = "https://from-env.invalid/d"
        try:
            d = self.client.get("/api/settings").json()
        finally:
            os.environ.pop("STRM_PREFIX", None)
        sp = {f["key"]: f for f in d["fields"]}["strm_prefix"]
        self.assertEqual(sp["stale_env"], "https://from-env.invalid/d")
        self.assertNotEqual(sp["source"], "环境变量", "环境变量对它已经无效了")

    def test_前缀空时预览给400而不是500(self):
        """🔴 不加守卫的话会一路走到 `builder.url()` 抛 ValueError ⇒ 500，
        用户只看到「服务器内部错误」，猜不到是没填播放地址。"""
        from app import settings as sm
        sm.reset_json(self.root)
        r = self.client.post("/api/preview", json={"limit": 10})
        self.assertEqual(r.status_code, 400)
        self.assertIn("播放地址", r.json()["detail"])

    def test_前缀空时开跑报错落在运行状态里(self):
        """真跑那条路由 `pipeline.run_once` 的 ⓪ 守卫管 —— 错误要能传到前端。"""
        from app import settings as sm
        sm.reset_json(self.root)
        r = self.client.post("/api/run", json={"mode": "tree", "dry_run": True})
        self.assertEqual(r.status_code, 200)
        self._wait_idle()
        st = self.client.get("/api/state").json()["run"]
        self.assertIn("前缀", st.get("error") or "")
        self.assertEqual(st["written"], 0, "被拦住时一个文件都不该写")

    # ------------------------------------------------------------------ 预览
    def test_预览不发请求只算差异(self):
        r = self.client.post("/api/preview", json={"limit": 50})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        # 树里共 4 个媒体文件（短剧那个 c.mp4 也算，因为没设白名单 = 全都要）
        self.assertEqual(d["counts"]["add"], 4)
        self.assertTrue(d["prefix_sample"].startswith("https://example.test/d/"))

    def test_预览_一个文件都不写(self):
        self.client.post("/api/preview", json={"limit": 50})
        self.assertEqual(list((self.root / "out").rglob("*")), [])

    def test_预览_strm内容用d端点(self):
        d = self.client.post("/api/preview", json={"limit": 50}).json()
        self.assertNotIn("/dav", d["prefix_sample"])
        self.assertIn("/d/", d["prefix_sample"])

    # ------------------------------------------------------------------ 同步范围
    def test_scope_列一层子目录(self):
        d = self.client.get("/api/scope").json()
        names = [i["name"] for i in d["items"]]
        self.assertEqual(names, ["电影", "电视剧", "短剧"])
        self.assertEqual(d["scope"]["remote_root"], "云下载")

    def test_scope_下钻一层(self):
        d = self.client.get("/api/scope?path=电影").json()
        self.assertEqual([i["name"] for i in d["items"]], ["功夫片（2026）"])
        it = d["items"][0]
        # ⚠️ `has_children` 指**有没有子目录**，不是有没有文件 ——
        #    `功夫片（2026）` 下面只有 2 个文件、没有子目录 ⇒ False。
        self.assertFalse(it["has_children"])
        self.assertEqual(it["files"], 2)
        self.assertEqual(it["subdirs"], 0)

    def test_scope_面包屑(self):
        d = self.client.get("/api/scope?path=电影").json()
        self.assertEqual([c["name"] for c in d["crumbs"]], ["云下载", "电影"])

    def test_scope_不存在的路径报错(self):
        self.assertEqual(self.client.get("/api/scope?path=没有这个").status_code, 400)

    def test_scope保存后白名单生效(self):
        r = self.client.post("/api/scope", json={
            "remote_root": "云下载", "include_dirs": ["电影", "电视剧"]})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["scope"]["includes"], ["电影", "电视剧"])
        # 试算：短剧被排除 ⇒ 只剩 3 个媒体文件
        self.assertEqual(d["estimate"]["counts"]["add"], 3)

    def test_scope保存后预览也跟着变(self):
        self.client.post("/api/scope", json={
            "remote_root": "云下载", "include_dirs": ["电影"]})
        d = self.client.post("/api/preview", json={"limit": 50}).json()
        self.assertEqual(d["counts"]["add"], 2)          # 只剩 a.mkv / b.mp4

    def test_scope_带同步根前缀被归一(self):
        r = self.client.post("/api/scope", json={
            "remote_root": "云下载", "include_dirs": ["云下载/电影"]})
        self.assertEqual(r.json()["scope"]["includes"], ["电影"])

    def test_scope_保存带出配对告警(self):
        # 前缀末段是 `d`（`https://example.test/d`）而同步根末段是 `云下载` ⇒ 应告警
        r = self.client.post("/api/scope", json={
            "remote_root": "云下载", "include_dirs": ["电影"]})
        self.assertTrue(r.json()["warn"], "末段不一致时必须告警")

    # ------------------------------------------------------------------ 运行
    def test_运行_多余的mode字段被忽略(self):
        """⛔ 防回归：模式已删 —— `RunIn` 上没有 `mode` 字段，
        再传它应当被**静默忽略**（不再有 400），而不是又被当成选项。"""
        r = self.client.post("/api/run", json={"mode": "瞎写", "dry_run": True})
        self.assertEqual(r.status_code, 200, "多余的字段应当被忽略，不该再报 400")
        self._wait_idle()

    def test_运行_演练(self):
        r = self.client.post("/api/run", json={"dry_run": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["running"], "刚启动的这一瞬间应当是在跑")
        self._wait_idle()

    def test_运行中再起一个会拒绝(self):
        """⚠️ 测试里没有 cookie，`run_once` 会**瞬间**失败 ——
        必须先把执行入口拖住，否则第二个请求打过去时它早就不在跑了（断言会假过）。"""
        import time as _t
        from unittest import mock

        def slow(*_a, **_k):
            _t.sleep(1.5)
            raise RuntimeError("测试用：不真的跑")

        with mock.patch("app.pipeline.run_once", side_effect=slow):
            r = self.client.post("/api/run", json={"dry_run": True})
            self.assertEqual(r.status_code, 200)
            r2 = self.client.post("/api/run", json={"dry_run": True})
            self.assertEqual(r2.status_code, 409)
        self._wait_idle(timeout=15)

    def _wait_idle(self, timeout: float = 20.0):
        """等后台任务跑完 —— 不等的话下一次 POST /api/run 会被 409 顶回来。"""
        import time
        end = time.time() + timeout
        while time.time() < end:
            if not self.client.get("/api/state").json()["run"]["running"]:
                return
            time.sleep(0.05)
        self.fail("后台任务一直没结束")

    # ------------------------------------------------------------------ 已删的接口
    def test_事件流接口已经删干净(self):
        """⛔ 防回归：`/api/life` 与 `/api/life/reset` 都该是 **404**。

        事件流（含游标）2026-10-08 整个去掉了 —— 留着半个接口才是麻烦：
        前端会拿到个空壳、以后有人又去接它。
        """
        self.assertEqual(self.client.get("/api/life").status_code, 404)
        self.assertEqual(self.client.post("/api/life/reset").status_code, 404)

    def test_停止当没有任务时给提示(self):
        d = self.client.post("/api/run/stop").json()
        self.assertFalse(d["ok"])

    # ------------------------------------------------------------------ 挂起
    def test_pending_空(self):
        d = self.client.get("/api/pending").json()
        self.assertEqual(d["count"], 0)

    def test_pending_没东西时apply报错(self):
        r = self.client.post("/api/pending/apply")
        self.assertEqual(r.status_code, 400)

    def test_pending_cancel(self):
        d = self.client.post("/api/pending/cancel").json()
        self.assertEqual(d["cancelled"], 0)

    # ------------------------------------------------------------------ 安全
    def test_回执名穿越被拦(self):
        for bad in ("../etc/passwd", "..%2F..%2Fx", "notsync-1", "sync-1/../x"):
            r = self.client.get("/api/runs/" + bad)
            self.assertIn(r.status_code, (400, 404), bad)

    def test_回执不存在给404(self):
        r = self.client.get("/api/runs/sync-19990101-000000")
        self.assertEqual(r.status_code, 404)

    def test_回执列表(self):
        d = self.client.get("/api/runs").json()
        self.assertIn("items", d)

    # ------------------------------------------------------------------ 账号
    def test_账号文件不存在时不炸(self):
        d = self.client.get("/api/accounts").json()
        self.assertFalse(d["exists"])
        self.assertEqual(d["items"], [])
        self.assertEqual(d["count"], 0)

    def test_添加账号_校验失败也存下(self):
        # 测试环境没装/没连 115，probe 必然失败 —— 但账号要存下来（cookie 可能只是过期）
        r = self.client.post("/api/accounts", json={"name": "测试号",
                                                   "cookie": "UID=1;CID=2;SEID=3"})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["item"]["name"], "测试号")
        self.assertIn("cookie_tail", d["item"])
        self.assertNotIn("UID=1", json.dumps(d))          # 不回显完整 cookie

    def test_添加账号_格式不对拒绝(self):
        r = self.client.post("/api/accounts", json={"name": "x", "cookie": "随便贴的"})
        self.assertEqual(r.status_code, 400)

    def test_添加账号_重复拒绝(self):
        ck = "UID=9;CID=8;SEID=7"
        self.client.post("/api/accounts", json={"name": "a", "cookie": ck})
        r = self.client.post("/api/accounts", json={"name": "b", "cookie": ck})
        self.assertEqual(r.status_code, 409)

    def test_账号列表_不回显完整cookie(self):
        # ⚠️ 假值，长度与结构照真实（见 test_accounts 同名用例的说明）。
        ck = ("UID=10000000_D1_1700000000;CID=0123456789abcdef0123456789abcdef;"
              "SEID=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
              "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
        self.client.post("/api/accounts", json={"name": "a", "cookie": ck})
        blob = json.dumps(self.client.get("/api/accounts").json())
        self.assertNotIn(ck, blob)                 # 完整 cookie 不出现
        self.assertNotIn("deadbeefdeadbeef", blob)  # SEID 前段不出现
        self.assertNotIn("10000000_D1", blob)      # UID 不出现

    def test_改名(self):
        aid = self.client.post("/api/accounts", json={
            "name": "旧", "cookie": "UID=1;SEID=3"}).json()["item"]["id"]
        r = self.client.put(f"/api/accounts/{aid}", json={"name": "新"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["item"]["name"], "新")

    def test_改cookie(self):
        aid = self.client.post("/api/accounts", json={
            "name": "a", "cookie": "UID=1;SEID=3"}).json()["item"]["id"]
        r = self.client.put(f"/api/accounts/{aid}", json={"cookie": "UID=2;SEID=4"})
        self.assertTrue(r.json()["item"]["usable"])
        self.assertEqual(len(self.client.get("/api/accounts").json()["items"]), 1)  # 还是 1 个

    def test_改cookie_格式不对拒绝(self):
        aid = self.client.post("/api/accounts", json={
            "name": "a", "cookie": "UID=1;SEID=3"}).json()["item"]["id"]
        r = self.client.put(f"/api/accounts/{aid}", json={"cookie": "乱写的"})
        self.assertEqual(r.status_code, 400)

    def test_改不存在的账号404(self):
        self.assertEqual(self.client.put("/api/accounts/nope", json={"name": "x"}).status_code, 404)

    def test_删除(self):
        aid = self.client.post("/api/accounts", json={
            "name": "a", "cookie": "UID=1;SEID=3"}).json()["item"]["id"]
        self.assertEqual(self.client.delete(f"/api/accounts/{aid}").status_code, 200)
        self.assertEqual(self.client.get("/api/accounts").json()["count"], 0)

    def test_删不存在的404(self):
        self.assertEqual(self.client.delete("/api/accounts/nope").status_code, 404)

    def test_校验接口存在(self):
        aid = self.client.post("/api/accounts", json={
            "name": "a", "cookie": "UID=1;SEID=3"}).json()["item"]["id"]
        r = self.client.post(f"/api/accounts/{aid}/check")
        self.assertEqual(r.status_code, 200)      # 联网失败也要 200，把原因放 body
        self.assertIn("ok", r.json())

    def test_扫码状态_会话不存在502(self):
        r = self.client.get("/api/qrcode/status?uid=不存在")
        self.assertEqual(r.status_code, 502)

    def test_账号_使用中标记(self):
        aid = self.client.post("/api/accounts", json={
            "name": "唯一", "cookie": "UID=1;SEID=3"}).json()["item"]["id"]
        d = self.client.get("/api/accounts").json()
        # 没显式指定时，第一个能用的就是「使用中」
        self.assertEqual(d["active_id"], aid)


@unittest.skipIf(TestClient is None, "没装 fastapi/httpx")
class TestAuth(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self._env = {k: os.environ.get(k) for k in ("DATA_DIR", "ACCESS_TOKEN")}
        os.environ["DATA_DIR"] = str(self.root)
        os.environ["ACCESS_TOKEN"] = "secret123"
        from app import config as cm
        cm.reload_overrides()
        import importlib
        from app import web as web_mod
        importlib.reload(web_mod)
        self.client = TestClient(web_mod.app)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        from app import config as cm
        cm.reload_overrides()
        self.td.cleanup()

    def test_设了口令_无token拒绝(self):
        self.assertEqual(self.client.get("/api/state").status_code, 401)

    def test_错token拒绝(self):
        # ⚠️ 错误口令的取值必须是 **ASCII** —— httpx 编 header 用 ascii，
        #    写中文会抛 UnicodeEncodeError（那是测试的锅，不是被测代码的）。
        self.assertEqual(
            self.client.get("/api/state",
                            headers={"Authorization": "Bearer wrong-token"}).status_code, 401)

    def test_对token放行(self):
        r = self.client.get("/api/state", headers={"Authorization": "Bearer secret123"})
        self.assertEqual(r.status_code, 200)

    def test_首页不要口令(self):
        """首页本身不鉴权（否则前端拿不到口令时连页面都看不到）。"""
        self.assertEqual(self.client.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
