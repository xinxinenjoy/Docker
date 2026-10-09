"""账号管理回归 —— 重点是「与 115offline 共用同一份文件」这条不变量。

🔴 最容易出的事：115strm 写账号文件时**把 115offline 记的字段抹掉**
   （`default_dir` / `default_dir_name` / `login_app` 等）。
   所以每个写操作都必须「读-改-写整个列表、保留不认识的字段」。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app import accounts as A


def tmp_path(td: str) -> Path:
    return Path(td) / "accounts.json"


def seed(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


# 模拟 115offline 写出来的账号（带它自己的字段）
OFFLINE_ACC = {
    "id": "aaa111",
    "name": "主号",
    "cookie": "UID=123;CID=abc;SEID=def",
    "created": "2026-10-01 10:00",
    "checked": "2026-10-01 10:00",
    "nickname": "红领巾",
    "valid": True,
    "default_dir": "123456",
    "default_dir_name": "云下载",
    "login_app": "tv",
}


class TestReadWrite(unittest.TestCase):
    def test_读不存在的文件返回空(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(A.read_accounts(tmp_path(td)), [])

    def test_读坏文件返回空不抛(self):
        with tempfile.TemporaryDirectory() as td:
            p = tmp_path(td)
            p.write_text("{ 坏 json", encoding="utf-8")
            self.assertEqual(A.read_accounts(p), [])

    def test_读非list返回空(self):
        with tempfile.TemporaryDirectory() as td:
            p = tmp_path(td)
            p.write_text('{"items": []}', encoding="utf-8")
            self.assertEqual(A.read_accounts(p), [])

    def test_往返(self):
        with tempfile.TemporaryDirectory() as td:
            p = tmp_path(td)
            A.write_accounts(p, [OFFLINE_ACC])
            self.assertEqual(A.read_accounts(p)[0]["name"], "主号")

    def test_原子写不留tmp(self):
        with tempfile.TemporaryDirectory() as td:
            p = tmp_path(td)
            A.write_accounts(p, [OFFLINE_ACC])
            self.assertEqual(list(Path(td).glob("*.tmp")), [])


class TestPreserveOfflineFields(unittest.TestCase):
    """🔴 核心不变量：写自己的东西，别动别人的东西。"""

    def test_改名不动其它字段(self):
        with tempfile.TemporaryDirectory() as td:
            p = tmp_path(td)
            seed(p, [OFFLINE_ACC])
            items = A.read_accounts(p)
            items[0]["name"] = "改过的名字"
            A.write_accounts(p, items)
            got = A.read_accounts(p)[0]
            self.assertEqual(got["name"], "改过的名字")
            self.assertEqual(got["default_dir"], "123456")
            self.assertEqual(got["default_dir_name"], "云下载")
            self.assertEqual(got["nickname"], "红领巾")

    def test_touch_只改校验字段(self):
        with tempfile.TemporaryDirectory() as td:
            p = tmp_path(td)
            seed(p, [OFFLINE_ACC])
            A.touch(p, "aaa111", {"ok": False, "nickname": ""})
            got = A.read_accounts(p)[0]
            self.assertFalse(got["valid"])
            self.assertEqual(got["nickname"], "红领巾")      # 拿不到就别清掉
            self.assertEqual(got["default_dir"], "123456")   # ⛔ 不能动

    def test_删除只删指定的(self):
        with tempfile.TemporaryDirectory() as td:
            p = tmp_path(td)
            seed(p, [OFFLINE_ACC, dict(OFFLINE_ACC, id="bbb222", name="小号")])
            left = [a for a in A.read_accounts(p) if a["id"] != "aaa111"]
            A.write_accounts(p, left)
            got = A.read_accounts(p)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["default_dir"], "123456")


class TestPublic(unittest.TestCase):
    def test_只留尾巴不回显完整cookie(self):
        """⚠️ 口径：只回**最后 8 字符**当指纹（与 115offline 一致），
        足够区分「哪个是哪个」，但**不足以还原 cookie**。
        这里用的是真实长度的 SEID，避免「尾巴正好把整个值漏出去」的假阴性。"""
        long = dict(OFFLINE_ACC)
        # ⚠️ 假值，但**长度与结构照真实**（8位_2位_10位 + 32位hex + 128位hex）——
        #    这样「尾巴正好把整个值漏出去」的假阴性仍然测得出来。
        long["cookie"] = ("UID=10000000_D1_1700000000;CID=0123456789abcdef0123456789abcdef;"
                          "SEID=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
                          "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
        p = A.public(long)
        blob = json.dumps(p, ensure_ascii=False)
        self.assertNotIn(long["cookie"], blob)             # 完整 cookie 不能出现
        self.assertNotIn("deadbeefdeadbeef", blob)          # SEID 前段不能出现
        self.assertNotIn("10000000_D1", blob)               # UID 不能出现
        self.assertTrue(p["cookie_tail"].startswith("…"))
        self.assertLessEqual(len(p["cookie_tail"]), 9)

    def test_字段齐全(self):
        p = A.public(OFFLINE_ACC)
        for k in ("id", "name", "nickname", "valid", "cookie_tail", "usable", "checked"):
            self.assertIn(k, p)

    def test_usable_认cookie形态(self):
        self.assertTrue(A.public(OFFLINE_ACC)["usable"])
        self.assertFalse(A.public({"id": "x", "cookie": ""})["usable"])
        self.assertFalse(A.public({"id": "x", "cookie": "随便什么"})["usable"])

    def test_名字兜底(self):
        self.assertEqual(A.public({"id": "zzz"})["name"], "zzz")
        self.assertEqual(A.public({"id": ""})["name"], "(未命名)")


class TestLooksLikeCookie(unittest.TestCase):
    def test_认得(self):
        self.assertTrue(A.looks_like_cookie("UID=1;CID=2;SEID=3"))
        self.assertTrue(A.looks_like_cookie("uid=1"))
        self.assertTrue(A.looks_like_cookie("x; UID=1 ;y"))
        self.assertTrue(A.looks_like_cookie("UID=1; CID=2; SEID=3"))   # 分号后带空格是常见的

    def test_不认(self):
        self.assertFalse(A.looks_like_cookie(""))
        self.assertFalse(A.looks_like_cookie("随便贴一段"))
        self.assertFalse(A.looks_like_cookie("magnet:?xt=urn:btih:..."))
        # ⚠️ `UID = 1`（等号两边带空格）**不认** —— 真实 cookie 不会有这种写法，
        #    而放宽判据容易把「随便贴的一段话里有 UID =」误判成 cookie。
        self.assertFalse(A.looks_like_cookie("UID = 1"))


class TestMakeAccount(unittest.TestCase):
    def test_结构(self):
        a = A.make_account("主号", "UID=1;SEID=x")
        self.assertEqual(a["name"], "主号")
        self.assertEqual(a["cookie"], "UID=1;SEID=x")
        self.assertIsNone(a["valid"])          # 还没校验
        self.assertEqual(a["checked"], None)
        self.assertTrue(a["id"])

    def test_无名给默认(self):
        self.assertEqual(A.make_account("", "UID=1")["name"], "新账号")

    def test_id_不重复(self):
        ids = {A.make_account("x", "UID=1")["id"] for _ in range(200)}
        self.assertEqual(len(ids), 200)

    def test_带login_app(self):
        self.assertEqual(A.make_account("x", "UID=1", login_app="tv")["login_app"], "tv")
        self.assertNotIn("login_app", A.make_account("x", "UID=1"))


class TestFindAccount(unittest.TestCase):
    def test_找到与找不到(self):
        with tempfile.TemporaryDirectory() as td:
            p = tmp_path(td)
            seed(p, [OFFLINE_ACC])
            self.assertEqual(A.find_account(p, "aaa111")["name"], "主号")
            self.assertIsNone(A.find_account(p, "不存在"))


class TestQrApps(unittest.TestCase):
    def test_默认端是tv(self):
        """⚠️ 选端很关键：同一个端再次扫码会把该端旧登录挤掉。
        tv（电视端）最不容易和日常用的网页端/手机端撞车 ⇒ 必须是默认。"""
        self.assertIn("tv", A.QR_APPS)

    def test_清单含电视端(self):
        self.assertIn("电视端", A.QR_APPS["tv"])


class TestQrState(unittest.TestCase):
    def test_过期会话被清(self):
        A._QR_STATE.clear()
        A._QR_STATE["old"] = {"created": 0, "time": 1, "sign": "s"}
        A._qr_cleanup()
        self.assertNotIn("old", A._QR_STATE)

    def test_未会话时status报错(self):
        A._QR_STATE.clear()
        with self.assertRaises(Exception):
            A.qr_status("不存在")

    def test_未会话时exchange报错(self):
        A._QR_STATE.clear()
        with self.assertRaises(Exception):
            A.qr_exchange("不存在")


class TestProbeNoNetwork(unittest.TestCase):
    def test_没有p115client时给出可读错误(self):
        """校验函数在依赖缺失时不能炸 —— 要返回可读的 error。"""
        import sys
        saved = sys.modules.get("p115client")
        sys.modules["p115client"] = None          # 模拟 import 失败
        try:
            r = A.probe_cookie("UID=1;SEID=x")
            self.assertFalse(r["ok"])
            self.assertTrue(r["error"])
        finally:
            if saved is None:
                sys.modules.pop("p115client", None)
            else:
                sys.modules["p115client"] = saved


if __name__ == "__main__":
    unittest.main()
