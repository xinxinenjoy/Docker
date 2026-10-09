"""参数层回归 —— 重点在「三层覆盖」的口径不能乱。

⭐ 这张表的默认值**必须在 `Config` 上取**（全局一个真相源）；
   ⛔ 曾经在别处踩过「HTML 里再抄一份默认值」⇒ 改了一边另一边静默失效。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app import settings
from app.config import Config, load


class TestCoerce(unittest.TestCase):
    def test_bool_各种写法(self):
        f = {"type": "bool"}
        for v in (True, "1", "true", "yes", "on", "y", "TRUE"):
            self.assertTrue(settings.coerce(f, v), v)
        for v in (False, "0", "no", "", "off"):
            self.assertFalse(settings.coerce(f, v), v)

    def test_int_带单位文本(self):
        self.assertEqual(settings.coerce({"type": "int"}, " 42 "), 42)
        self.assertEqual(settings.coerce({"type": "int"}, "3.7"), 3)

    def test_int_夹紧到范围(self):
        f = {"type": "int", "min": 1, "max": 10}
        self.assertEqual(settings.coerce(f, "0"), 1)
        self.assertEqual(settings.coerce(f, "99"), 10)

    def test_int_非法返回None(self):
        self.assertIsNone(settings.coerce({"type": "int"}, "abc"))

    def test_float(self):
        self.assertAlmostEqual(settings.coerce({"type": "float"}, "0.35"), 0.35)

    def test_enum_只认白名单(self):
        f = {"type": "enum", "options": [("alist", "a"), ("native", "n")]}
        self.assertEqual(settings.coerce(f, "alist"), "alist")
        self.assertIsNone(settings.coerce(f, "瞎写的"))

    def test_cron_五段才认(self):
        f = {"type": "cron"}
        self.assertEqual(settings.coerce(f, "0 4 * * *"), "0 4 * * *")
        self.assertEqual(settings.coerce(f, ""), "")
        self.assertIsNone(settings.coerce(f, "0 4 * *"))       # 只有四段

    def test_window_格式(self):
        f = {"type": "window"}
        self.assertEqual(settings.coerce(f, "02:00-06:00"), "02:00-06:00")
        self.assertEqual(settings.coerce(f, "02:00 - 06:00"), "02:00-06:00")
        self.assertEqual(settings.coerce(f, ""), "")
        self.assertIsNone(settings.coerce(f, "晚上"))          # 中文不认

    def test_str_去空白(self):
        self.assertEqual(settings.coerce({"type": "str"}, "  x  "), "x")


class TestSaveLoad(unittest.TestCase):
    def test_存取往返_键用env名(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            settings.save_json(d, {"throttle_min": "2.5", "remote_root": "云下载"})
            got = settings.load_json(d)
            self.assertEqual(got["THROTTLE_MIN"], "2.5")
            self.assertEqual(got["REMOTE_ROOT"], "云下载")

    def test_不认识的键进rejected(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            r = settings.save_json(d, {"瞎写的键": "x", "throttle_min": "1"})
            self.assertIn("瞎写的键", r["rejected"])
            self.assertIn("throttle_min", r["saved"])

    def test_非法值进rejected不落盘(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            r = settings.save_json(d, {"schedule_cron": "不是cron"})
            self.assertIn("schedule_cron", r["rejected"])
            self.assertEqual(settings.load_json(d).get("SCHEDULE_CRON"), None)

    def test_文件损坏时返回空(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            settings.config_path(d).write_text("{坏", encoding="utf-8")
            self.assertEqual(settings.load_json(d), {})

    def test_reset_单键(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            settings.save_json(d, {"throttle_min": "5", "throttle_max": "9"})
            r = settings.reset_json(d, ["throttle_min"])
            self.assertIn("throttle_min", r["removed"])
            got = settings.load_json(d)
            self.assertNotIn("THROTTLE_MIN", got)
            # ⚠️ float 字段经 coerce 存进去是 `9.0`（不是 `9`）——
            #    这是刻意的：coerce 统一成 float 再 str，保证读回来类型稳定。
            self.assertEqual(float(got["THROTTLE_MAX"]), 9.0)

    def test_reset_全清(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            settings.save_json(d, {"throttle_min": "5"})
            settings.reset_json(d, None)
            self.assertEqual(settings.load_json(d), {})

    def test_原子写不留tmp(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            settings.save_json(d, {"throttle_min": "5"})
            self.assertEqual(list(d.glob("*.tmp")), [])


class TestPriority(unittest.TestCase):
    """三层覆盖：环境变量 > config.json > 出厂默认。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.d = Path(self.td.name)
        self._saved = {k: os.environ.get(k) for k in ("DATA_DIR", "THROTTLE_MIN")}
        os.environ["DATA_DIR"] = str(self.d)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.td.cleanup()

    def test_默认值(self):
        from app import config as cm
        cm.reload_overrides()
        cfg = Config()
        self.assertAlmostEqual(cfg.throttle_min, 1.0)
        # 🔴 `strm_prefix` 是**网页专属字段**（`settings.WEB_ONLY_ENVS`），
        #    出厂**空**：首次进网页时它就该是空的，让人当场填 ——
        #    而不是悄悄套一个内置地址（旧版内置了作者自己的 alist 地址，两个毛病：
        #    别人不配就用上别人的地址；真域名还被写进了代码）。
        self.assertEqual(cfg.strm_prefix, "")
        self.assertFalse(cfg.prefix_ready)

    def test_前缀填了才算就绪(self):
        self.assertTrue(Config(strm_prefix="https://alist.example.com/d/影音/115影音").prefix_ready)
        self.assertFalse(Config(strm_prefix="   ").prefix_ready)

    def test_网页专属字段忽略环境变量(self):
        """🔴 红领巾 2026-10-08：「strm 的前缀应该从启动参数里去掉 … 存储在 data 目录下」。

        ⇒ 环境变量里就算设了 `STRM_PREFIX` 也**不读**，只认 `config.json`。
           ⛔ 但**不能静默忽略**：`describe()` 会把它标成 `stale_env` 报给网页，
              启动时还有 `bootstrap()` 把它搬进 config.json（见下面两条用例）。
        """
        os.environ["STRM_PREFIX"] = "https://from-env.invalid/d"
        saved = os.environ.get("DATA_DIR")
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            os.environ["DATA_DIR"] = str(d)
            from app import config as cm
            try:
                cm.reload_overrides()
                self.assertEqual(Config().strm_prefix, "", "网页专属字段不该读环境变量")
                settings.save_json(d, {"strm_prefix": "https://from-web.example.com/d"})
                cm.reload_overrides()
                self.assertEqual(Config().strm_prefix, "https://from-web.example.com/d",
                                 "config.json 里的值必须生效")
            finally:
                os.environ.pop("STRM_PREFIX", None)
                if saved is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = saved
                cm._OVERRIDES_LOADED = False
                cm.reload_overrides()

    def test_遗留在env里的值会被搬进config_json(self):
        """升级场景：上一版把前缀写在 `.env` 里 ⇒ 本版启动时搬进 config.json。

        ⚠️ 没有这一步的话，前缀会**静默变空**、下一轮直接跑不起来。
        """
        os.environ["STRM_PREFIX"] = "https://from-env.invalid/d"
        saved = os.environ.get("DATA_DIR")
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            os.environ["DATA_DIR"] = str(d)
            from app import config as cm
            try:
                cm._OVERRIDES_LOADED = False
                res = cm.bootstrap()
                self.assertIn("STRM_PREFIX", res["migrated"])
                self.assertEqual(settings.load_json(d)["STRM_PREFIX"],
                                 "https://from-env.invalid/d")
                self.assertEqual(cm.Config().strm_prefix, "https://from-env.invalid/d")
            finally:
                os.environ.pop("STRM_PREFIX", None)
                if saved is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = saved
                cm._OVERRIDES_LOADED = False
                cm.reload_overrides()

    def test_两边都有值时_以网页为准并报stale(self):
        """网页与 `.env` 都有值且不同 ⇒ **不覆盖**（网页说话算数），只报 `stale`。"""
        os.environ["STRM_PREFIX"] = "https://from-env.invalid/d"
        saved = os.environ.get("DATA_DIR")
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            os.environ["DATA_DIR"] = str(d)
            from app import config as cm
            try:
                settings.save_json(d, {"strm_prefix": "https://from-web.example.com/d"})
                cm._OVERRIDES_LOADED = False
                res = cm.bootstrap()
                self.assertEqual(res["migrated"], [])
                self.assertEqual(res["stale"][0]["env"], "STRM_PREFIX")
                self.assertEqual(cm.Config().strm_prefix, "https://from-web.example.com/d")
            finally:
                os.environ.pop("STRM_PREFIX", None)
                if saved is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = saved
                cm._OVERRIDES_LOADED = False
                cm.reload_overrides()

    def test_describe_报出stale_env(self):
        os.environ["STRM_PREFIX"] = "https://from-env.invalid/d"
        saved = os.environ.get("DATA_DIR")
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            os.environ["DATA_DIR"] = str(d)
            from app import config as cm
            try:
                cm.reload_overrides()
                out = settings.describe(cm.Config(), d)
                by = {f["key"]: f for f in out["fields"]}
                self.assertEqual(by["strm_prefix"]["source"], "默认")
                self.assertTrue(by["strm_prefix"]["web_only"])
                self.assertEqual(by["strm_prefix"]["stale_env"], "https://from-env.invalid/d")
                self.assertIn("strm_prefix", out["required"])
                self.assertIn("strm_prefix", out["missing"])
            finally:
                os.environ.pop("STRM_PREFIX", None)
                if saved is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = saved
                cm._OVERRIDES_LOADED = False
                cm.reload_overrides()

    def test_describe_的missing随保存消失(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            saved = os.environ.get("DATA_DIR")
            os.environ["DATA_DIR"] = str(d)
            from app import config as cm
            try:
                cm.reload_overrides()
                self.assertIn("strm_prefix", settings.describe(cm.Config(), d)["missing"])
                settings.save_json(d, {"strm_prefix": "https://alist.example.com/d"})
                cm.reload_overrides()
                self.assertNotIn("strm_prefix", settings.describe(cm.Config(), d)["missing"])
            finally:
                if saved is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = saved
                cm._OVERRIDES_LOADED = False
                cm.reload_overrides()

    def test_被删掉的字段进rejected(self):
        """⛔ 本版删掉的参数不能再被静默写回 —— 尤其 `noext_as_dir`（规则已作废）。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            gone = ("noext_as_dir", "prefix_mode", "remote_root_id",
                    "export_layer_limit", "export_timeout",
                    "url_encode", "life_cooldown", "life_lookback_days", "log_level")
            r = settings.save_json(d, {k: "x" for k in gone})
            self.assertEqual(sorted(r["rejected"]), sorted(gone))
            self.assertEqual(settings.load_json(d), {})

    def test_config_json覆盖默认(self):
        settings.save_json(self.d, {"throttle_min": "7"})
        from app import config as cm
        cm.reload_overrides()
        self.assertAlmostEqual(Config().throttle_min, 7.0)

    def test_环境变量压过json(self):
        settings.save_json(self.d, {"throttle_min": "7"})
        os.environ["THROTTLE_MIN"] = "3"
        try:
            from app import config as cm
            cm.reload_overrides()
            self.assertAlmostEqual(Config().throttle_min, 3.0)
        finally:
            os.environ.pop("THROTTLE_MIN", None)


class TestDescribe(unittest.TestCase):
    def test_字段表与Config对齐(self):
        """⭐ 每个登记字段都必须在 Config 上存在 —— 否则网页存了也不生效，极难查。"""
        cfg = Config()
        missing = [f["key"] for f in settings.FIELDS if not hasattr(cfg, f["key"])]
        self.assertEqual(missing, [], f"这些字段在 Config 上不存在：{missing}")

    def test_describe_给出source(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            saved = {k: os.environ.get(k) for k in ("DATA_DIR",)}
            os.environ["DATA_DIR"] = str(d)
            try:
                settings.save_json(d, {"throttle_min": "4"})
                from app import config as cm
                cm.reload_overrides()
                out = settings.describe(Config(), d)
                by = {f["key"]: f for f in out["fields"]}
                self.assertEqual(by["throttle_min"]["source"], "已保存")
                self.assertEqual(by["throttle_max"]["source"], "默认")
                self.assertEqual(by["throttle_min"]["value"], 4.0)
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_环境变量源被标出(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            os.environ["DATA_DIR"] = str(d)
            os.environ["THROTTLE_MIN"] = "9"
            try:
                from app import config as cm
                cm.reload_overrides()
                out = settings.describe(Config(), d)
                by = {f["key"]: f for f in out["fields"]}
                self.assertEqual(by["throttle_min"]["source"], "环境变量")
            finally:
                os.environ.pop("THROTTLE_MIN", None)
                os.environ.pop("DATA_DIR", None)

    def test_groups_覆盖所有非空组(self):
        used = {f["group"] for f in settings.FIELDS}
        self.assertTrue(used <= set(settings.GROUPS), used - set(settings.GROUPS))


class TestCriticalFieldsRegistered(unittest.TestCase):
    """🔴 2026-10-08 部署后抓到的坑：**字段没登记进 FIELDS 就存不下来**。

    症状**极其隐蔽**：`save_json` 只认登记过的键，没登记的会被静默放进 `rejected`，
    HTTP 仍然 200 ⇒ 网页上看着「保存成功」，实际配置没变。

    当时漏的是 `dry_run` ⇒ 配置里 `DRY_RUN` 永远是 1
    ⇒ **定时任务每轮都只演练、一个文件都不写**，表面上还「跑成功了」。
    """

    # 这些字段**必须**能持久保存 —— 漏一个都会造成「看着成功、实际没生效」
    MUST_SAVE = ("dry_run", "schedule_cron", "remote_root", "include_dirs",
                 "strm_prefix", "delete_defer_minutes", "delete_ratio_guard",
                 "throttle_min", "video_ext")

    def test_都在字段表里(self):
        keys = {f["key"] for f in settings.FIELDS}
        missing = [k for k in self.MUST_SAVE if k not in keys]
        self.assertEqual(missing, [], f"这些字段没登记进 FIELDS：{missing}")

    def test_都能真的存下来(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            r = settings.save_json(d, {"dry_run": False, "schedule_cron": "0 4 * * *"})
            self.assertIn("dry_run", r["saved"], "dry_run 没存下来 ⇒ 定时任务会永远只演练")
            self.assertEqual(r["rejected"], [])
            got = settings.load_json(d)
            self.assertEqual(got["DRY_RUN"], "False")
            self.assertEqual(got["SCHEDULE_CRON"], "0 4 * * *")

    def test_关闭后读回来是False(self):
        """⚠️ `_flag` 的判据是「值在 ('1','true',…) 里」——
        存成 `"False"` 字符串要能正确读成 False（不能因为「非空」就当真）。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            settings.save_json(d, {"dry_run": False})
            saved = os.environ.get("DATA_DIR")
            os.environ["DATA_DIR"] = str(d)
            try:
                from app import config as cm
                cm.reload_overrides()
                self.assertFalse(cm.Config().dry_run, "存了 False 却读成 True")
            finally:
                if saved is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = saved
                from app import config as cm
                cm._OVERRIDES_LOADED = False
                cm.reload_overrides()


class TestConfigHelpers(unittest.TestCase):
    def test_exts_解析(self):
        cfg = Config(video_ext="mp4,MKV, srt")
        self.assertEqual(cfg.exts, frozenset(("mp4", "mkv", "srt")))

    def test_replace_只认已知键(self):
        cfg = Config()
        object.__setattr__(cfg, "临时属性", 1)
        c2 = cfg.replace(dry_run=False)
        self.assertFalse(c2.dry_run)
        self.assertFalse(hasattr(c2, "临时属性"))

    def test_all_隐藏cookie(self):
        cfg = Config(cookie="UID=1234567890;SEID=abcdefgh")
        self.assertIn("…", cfg.all()["cookie"])
        self.assertNotIn("UID=1234567890", str(cfg.all()["cookie"]))

    def test_data_dir_被解析成Path(self):
        cfg = Config(data_dir="/tmp/x")
        self.assertIsInstance(cfg.data_dir, Path)

    def test_tree_dir_默认跟data_dir(self):
        cfg = Config(data_dir="/tmp/x", tree_dir=None)
        self.assertEqual(cfg.tree_dir, Path("/tmp/x/tree"))


if __name__ == "__main__":
    unittest.main()
