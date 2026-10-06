"""参数配置层 + cookie 共用 —— 单测（全离线）。

钉死三件事：
  ① **三层优先级**：显式 env > `config.json` > 默认值。搞反了会出「网页上改了不生效」
     或者「.env 里留了个旧值把网页的改动永久盖住」这类极难查的怪事。
  ② **cookie 与 115offline 共用**：`accounts.json` 必须排在 `cookie.txt` 前面。
     🔴 老顺序反了 —— 盘上残留一份手贴的 `cookie.txt` 会把扫码登录的活凭据盖掉，
     症状是「115offline 里明明登录好了，整理工具却说 cookie 失效」。
  ③ `settings.FIELDS` 与 `Config` **不许对不上**：字段表少一个 ⇒ 网页上就少一个开关，
     多一个 ⇒ 保存时静默无效（`replace()` 因为不认识而丢掉）。所以两边都逐项对账。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, settings          # noqa: E402
from app.v115 import CookieMissing, account_of, load_cookie, read_accounts  # noqa: E402

# ⚠️ 这些环境变量一旦被外面的 shell 设过，就会盖住测试想验的「json 层」。
#    逐个存起来，测完还原（⛔ 别直接 del —— 外面可能真的有）。
_TOUCHED = ("DATA_DIR", "ACCOUNT_ID", "P115_COOKIE", "COOKIE", "ACCOUNTS_FILE",
            "SERIES_MIN", "JUNK_ACTION", "DRY_RUN", "ROOT_PATH", "WINDOW", "SCHEDULE_CRON")


class SettingCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="115org-cfg-"))
        self._saved = {k: os.environ.get(k) for k in _TOUCHED}
        for k in _TOUCHED:
            os.environ.pop(k, None)
        os.environ["DATA_DIR"] = str(self.tmp)
        config.reload_overrides()

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.reload_overrides()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_accounts(self, *items: dict) -> Path:
        p = self.tmp / "accounts.json"
        p.write_text(json.dumps(list(items), ensure_ascii=False), encoding="utf-8")
        return p


# ---------------------------------------------------------------- 三层优先级
class TestPrecedence(SettingCase):
    def test_默认值来自代码(self):
        self.assertEqual(config.load().series_min, 3)
        self.assertEqual(config.load().junk_action, "quarantine")

    def test_config_json_能盖过默认值(self):
        settings.save_json(self.tmp, {"series_min": 5, "junk_action": "report"})
        cfg = config.load()
        self.assertEqual(cfg.series_min, 5)
        self.assertEqual(cfg.junk_action, "report")

    def test_环境变量能盖过_config_json(self):
        settings.save_json(self.tmp, {"series_min": 5})
        os.environ["SERIES_MIN"] = "9"
        config.reload_overrides()
        self.assertEqual(config.load().series_min, 9)

    def test_环境变量留空不算设过(self):
        """`.env` 里留空（最常见）⇒ 这一层必须让路，否则网页改了永远不生效。"""
        settings.save_json(self.tmp, {"series_min": 5})
        os.environ["SERIES_MIN"] = ""
        config.reload_overrides()
        self.assertEqual(config.load().series_min, 5)

    def test_删掉_config_json_就回到默认(self):
        settings.save_json(self.tmp, {"series_min": 5})
        self.assertEqual(config.load().series_min, 5)
        settings.reset_json(self.tmp)
        self.assertEqual(config.load().series_min, 3)

    def test_只重置指定字段(self):
        settings.save_json(self.tmp, {"series_min": 5, "junk_action": "report"})
        settings.reset_json(self.tmp, ["series_min"])
        cfg = config.load()
        self.assertEqual(cfg.series_min, 3, "这一个该回默认")
        self.assertEqual(cfg.junk_action, "report", "其它字段不该被连带清掉")

    def test_data_dir_不吃_config_json(self):
        """`data_dir` 是引导项 —— 它决定 config.json 在哪，吃了就自我引用绕不出来。"""
        (self.tmp / "config.json").write_text(json.dumps({"data_dir": "/nope"}), encoding="utf-8")
        config.reload_overrides()
        self.assertEqual(Path(config.load().data_dir), self.tmp)

    def test_树目录默认跟着数据目录走(self):
        """🔴 曾经默认是「项目目录下的 data/tree」—— 容器里那是 `/app/data/tree`，
        而挂载卷在 `/data`，两者根本不是一个地方 ⇒ 症状是「树丢进去了，页面说没有」。
        以前靠 compose 显式设 TREE_DIR 兜住，把业务参数从 .env 撤走才暴露出来。
        """
        self.assertEqual(config.load().tree_dir, self.tmp / "tree")
        os.environ["TREE_DIR"] = "/somewhere/else"
        config.reload_overrides()
        self.assertEqual(config.load().tree_dir, Path("/somewhere/else"), "显式设了还是要认")

    def test_账号文件默认在数据目录下(self):
        self.assertEqual(config.load().accounts_file, self.tmp / "accounts.json")


# ---------------------------------------------------------------- 保存与校验
class TestSave(SettingCase):
    def test_坏值只拒绝那一条_其余照存(self):
        """一个笔误不该把整页改动丢掉。"""
        r = settings.save_json(self.tmp, {"series_min": 4, "junk_action": "nope"})
        self.assertIn("series_min", r["saved"])
        self.assertEqual([x["key"] for x in r["rejected"]], ["junk_action"])
        self.assertEqual(config.load().series_min, 4)

    def test_不认识的参数名被拒(self):
        r = settings.save_json(self.tmp, {"cookie": "UID=evil"})
        self.assertEqual(r["saved"], [])
        self.assertEqual(r["rejected"][0]["key"], "cookie")
        # 🔴 重点：cookie 进不了 config.json（明文凭据不该落在参数文件里）。
        #    全部被拒时**连文件都不建** —— 这也是对的，别写一份空配置出来。
        p = self.tmp / "config.json"
        raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        self.assertNotIn("cookie", raw)

    def test_布尔转成_0_1_存(self):
        settings.save_json(self.tmp, {"series_enable": False})
        raw = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertIs(raw["series_enable"], False, "JSON 里存原生 bool，不是字符串")
        self.assertFalse(config.load().series_enable)

    def test_数值越界被拒(self):
        self.assertEqual(len(settings.save_json(self.tmp, {"series_min": 0})["rejected"]), 1)
        self.assertEqual(len(settings.save_json(self.tmp, {"series_min": 9999})["rejected"]), 1)

    def test_cron_段数不对被拒(self):
        r = settings.save_json(self.tmp, {"inbox_poll_interval": 0})
        self.assertEqual(len(r["rejected"]), 1)
        self.assertIn("inbox_poll_interval", settings.save_json(
            self.tmp, {"inbox_poll_interval": 30})["saved"])

    def test_时段格式校验(self):
        self.assertEqual(len(settings.save_json(self.tmp, {"window": "2-6"})["rejected"]), 1)
        self.assertIn("window", settings.save_json(self.tmp, {"window": "02:00-06:00"})["saved"])
        self.assertIn("window", settings.save_json(self.tmp, {"window": ""})["saved"], "留空=不限，合法")

    def test_坏_json_不炸_当空处理(self):
        (self.tmp / "config.json").write_text("{ 这不是 json", encoding="utf-8")
        config.reload_overrides()
        self.assertEqual(config.load().series_min, 3)


# ---------------------------------------------------------------- 字段表对账
class TestFieldTable(SettingCase):
    def test_字段表的_key_都是_Config_真字段(self):
        known = {f.name for f in __import__("dataclasses").fields(config.Config)}
        missing = [f["key"] for f in settings.FIELDS if f["key"] not in known]
        self.assertEqual(missing, [], f"字段表里有 Config 上不存在的字段：{missing}")

    def test_必填字段一个都不能少(self):
        """⭐ 漏一个 = 网页上少一个开关，而且**不会报错**，只能靠这条测试发现。"""
        expected = {
            "account_id", "root_path", "tree_dir", "tree_file",
            "series_enable", "series_min", "series_rename",
            "movie_enable", "episode_group_enable", "episode_group_min", "paren",
            "junk_action", "quarantine_dir", "junk_dup_min", "junk_allow_dir",
            "throttle_min", "throttle_max", "throttle_batch", "throttle_rest",
            "window", "fail_limit", "fail_cooldown",
            "inbox_dir", "inbox_poll_interval", "inbox_max_requests",
            "scan_throttle_min", "scan_throttle_max",
            "dry_run", "log_level",
        }
        got = {f["key"] for f in settings.FIELDS}
        self.assertEqual(expected - got, set(), "字段表漏了这些")
        self.assertEqual(got - expected, set(), "字段表多了这些（要么补 Config，要么删掉）")

    def test_env_名不重复(self):
        envs = [f["env"] for f in settings.FIELDS]
        self.assertEqual(len(envs), len(set(envs)), "两个字段映射到同一个环境变量名")

    def test_分组顺序稳定(self):
        self.assertEqual(settings.GROUPS[0], "115 账号")
        self.assertIn("安全阀", settings.GROUPS)

    def test_describe_标出每个值从哪来(self):
        settings.save_json(self.tmp, {"series_min": 5})
        os.environ["JUNK_DUP_MIN"] = "9"
        config.reload_overrides()
        got = {f["key"]: f["source"] for f in settings.describe(config.load(), self.tmp)["fields"]}
        self.assertEqual(got["series_min"], "json")
        self.assertEqual(got["junk_dup_min"], "env")
        self.assertEqual(got["series_rename"], "default")
        self.assertIn("JUNK_DUP_MIN", settings.describe(config.load(), self.tmp)["env_locked"])

    def test_危险项带告警文案(self):
        """页面要在这些开关旁边直接给出后果，⛔ 不能只写个字段名。"""
        warn = {f["key"] for f in settings.FIELDS if f.get("warn")}
        self.assertEqual({"dry_run", "junk_action"} - warn, set())


# ---------------------------------------------------------------- cookie 共用
class TestCookieShared(SettingCase):
    def test_优先用_115offline_的_accounts_json(self):
        self.write_accounts({"id": "a1", "name": "主号", "cookie": "UID=1_A;CID=x;SEID=y"})
        cfg = config.load()
        self.assertEqual(load_cookie(cfg), "UID=1_A;CID=x;SEID=y")
        self.assertIn("共用账号", cfg.cookie_source)
        self.assertIn("主号", cfg.cookie_source)

    def test_accounts_json_必须排在_cookie_txt_前面(self):
        """🔴 这条是本次改动的核心 —— 顺序反了会出「明明登录好了却说失效」。"""
        self.write_accounts({"id": "a1", "name": "主号", "cookie": "UID=LIVE;CID=x;SEID=y"})
        (self.tmp / "cookie.txt").write_text("UID=STALE;CID=z;SEID=w", encoding="utf-8")
        self.assertEqual(load_cookie(config.load()), "UID=LIVE;CID=x;SEID=y")

    def test_没有账号文件时回落到_cookie_txt(self):
        (self.tmp / "cookie.txt").write_text("UID=MANUAL;CID=z;SEID=w", encoding="utf-8")
        cfg = config.load()
        self.assertEqual(load_cookie(cfg), "UID=MANUAL;CID=z;SEID=w")
        self.assertIn("cookie.txt", cfg.cookie_source)

    def test_环境变量优先级最高(self):
        self.write_accounts({"id": "a1", "cookie": "UID=ACC;CID=x;SEID=y"})
        os.environ["P115_COOKIE"] = "UID=ENV;CID=x;SEID=y"
        config.reload_overrides()
        cfg = config.load()
        self.assertEqual(load_cookie(cfg), "UID=ENV;CID=x;SEID=y")
        self.assertIn("环境变量", cfg.cookie_source)

    def test_指定账号_id_只认那一个(self):
        self.write_accounts(
            {"id": "a1", "name": "一号", "cookie": "UID=ONE;CID=x;SEID=y"},
            {"id": "a2", "name": "二号", "cookie": "UID=TWO;CID=x;SEID=y"},
        )
        cfg = config.load().replace(account_id="a2")
        self.assertEqual(load_cookie(cfg), "UID=TWO;CID=x;SEID=y")

    def test_指了不存在的账号要抛_不静默换人(self):
        """⛔ 静默换账号 = 在别人的网盘上动手，必须显式失败。"""
        self.write_accounts({"id": "a1", "cookie": "UID=ONE;CID=x;SEID=y"})
        with self.assertRaises(CookieMissing) as cm:
            load_cookie(config.load().replace(account_id="ghost"))
        self.assertIn("ghost", str(cm.exception))

    def test_账号文件包一层也认(self):
        (self.tmp / "accounts.json").write_text(
            json.dumps({"items": [{"id": "a1", "cookie": "UID=WRAP;CID=x;SEID=y"}]}),
            encoding="utf-8")
        self.assertEqual(load_cookie(config.load()), "UID=WRAP;CID=x;SEID=y")

    def test_坏账号文件不炸_按没有处理(self):
        (self.tmp / "accounts.json").write_text("{ 坏了", encoding="utf-8")
        self.assertEqual(read_accounts(self.tmp / "accounts.json"), [])
        self.assertEqual(account_of(config.load()), ("", ""), "读不出来应当是「没有」，不是抛错")

    def test_没配置时错误信息要指出推荐做法(self):
        with self.assertRaises(CookieMissing) as cm:
            load_cookie(config.load())
        msg = str(cm.exception)
        self.assertIn("115offline", msg, "要指引他去 115offline 扫码，而不是让他再手抓一份")

    def test_accounts_file_可以指到别处(self):
        """这就是「两个容器共用一份 cookie」的接线口：ACCOUNTS_FILE 指向挂进来的那份。"""
        other = self.tmp / "shared" / "accounts.json"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text(json.dumps([{"id": "s1", "cookie": "UID=SHARED;CID=x;SEID=y"}]),
                         encoding="utf-8")
        os.environ["ACCOUNTS_FILE"] = str(other)
        self.assertEqual(load_cookie(config.load()), "UID=SHARED;CID=x;SEID=y")

    def test_长得不像_cookie_的条目不采用(self):
        self.write_accounts({"id": "a1", "cookie": "not-a-cookie"},
                            {"id": "a2", "cookie": "UID=GOOD;CID=x;SEID=y"})
        self.assertEqual(load_cookie(config.load()), "UID=GOOD;CID=x;SEID=y")


# ---------------------------------------------------------------- replace()
class TestReplace(SettingCase):
    def test_replace_改指定字段(self):
        cfg = config.load().replace(series_min=8)
        self.assertEqual(cfg.series_min, 8)
        self.assertEqual(config.load().series_min, 3, "replace 不该写回配置文件")

    def test_replace_容忍外部挂的临时属性(self):
        """`load_cookie` 会往 cfg 上挂 `cookie_source` ⇒ replace 不能被它带崩。"""
        self.write_accounts({"id": "a1", "cookie": "UID=X;CID=x;SEID=y"})
        cfg = config.load()
        load_cookie(cfg)
        self.assertTrue(getattr(cfg, "cookie_source", ""))
        self.assertEqual(cfg.replace(root_path="别的").root_path, "别的")

    def test_replace_不认识的键被忽略(self):
        cfg = config.load().replace(不存在的字段=1, series_min=4)
        self.assertEqual(cfg.series_min, 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
