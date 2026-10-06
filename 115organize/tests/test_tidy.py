"""tidy / metadb / metascan 的回归测试。

跑法（项目根目录）：
    python -m unittest discover -s tests
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import number  # noqa: E402
from app.metadb import MetaDB, MAKER_OF  # noqa: E402
from app.metascan import MetaScan, pick_sources  # noqa: E402
from app.tidy import classify, plan_one  # noqa: E402


class TestClassify(unittest.TestCase):
    """归类口径：有女优 → 女优/番号；没女优 → 系列/番号。"""

    def test_有女优(self):
        self.assertEqual(classify("IPZZ-137", "三上悠亚"), "三上悠亚/IPZZ-137")

    def test_没女优_按系列(self):
        self.assertEqual(classify("IPZZ-137", ""), "IPZZ/IPZZ-137")
        self.assertEqual(classify("FC2-1567975", ""), "FC2/FC2-1567975")

    def test_空番号_无法识别(self):
        self.assertEqual(classify("", ""), "无法识别")


class TestPickSources(unittest.TestCase):
    """多源分流：不同派系走不同源。"""

    def test_主流有码(self):
        self.assertEqual(pick_sources("IPZZ-137")[0], "javbus")
        self.assertEqual(pick_sources("DLDSS-532")[0], "javbus")

    def test_fc2(self):
        self.assertEqual(pick_sources("FC2-1567975")[0], "fc2")

    def test_mgstage(self):
        self.assertEqual(pick_sources("300MIUM-1446")[0], "mgstage")

    def test_无码(self):
        self.assertEqual(pick_sources("CARIB-010112-123")[0], "xcity")


class TestMetaDB(unittest.TestCase):
    """离线映射 + 缓存。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = MetaDB(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_离线片商(self):
        self.assertEqual(MAKER_OF("IPZZ-137"), "Idea Pocket")
        self.assertEqual(MAKER_OF("SSNI-123"), "S1")

    def test_缓存命中不再请求(self):
        self.db.remember("IPZZ-137", actress="三上悠亚", maker="Idea Pocket")
        got = self.db.lookup("IPZZ-137")
        self.assertEqual(got["actress"], "三上悠亚")
        self.assertEqual(got["maker"], "Idea Pocket")

    def test_女优名规范化_中文为辅(self):
        # 罗马音 → 中文（当目录名还没有日文时，中文兜底）
        self.assertEqual(self.db.actress_display("yua mikami"), "三上悠亚")


class TestPlanOne(unittest.TestCase):
    """plan_one：识别 → 归类（离线表命中就不联网）。"""

    def test_离线片商驱动_按系列(self):
        # 没配在线 scan ⇒ 离线只给片商，没有女优 ⇒ 归系列
        plan = plan_one("manipzz-137")   # 纠错成 IPZZ-137
        self.assertEqual(plan["avid"], "IPZZ-137")
        self.assertEqual(plan["actress"], "")
        self.assertEqual(plan["target"], "IPZZ/IPZZ-137")
        self.assertEqual(plan["action"], "move_and_rename")

    def test_无法识别(self):
        plan = plan_one("云下载")
        self.assertEqual(plan["action"], "skip")
        self.assertEqual(plan["target"], "无法识别")

    def test_在线兜底_补女优(self):
        # 注入假 scan：离线查不到女优 → 在线补上
        class FakeScan:
            def resolve(self, avid):
                return {"avid": avid, "actress": "三上悠亚", "maker": "Idea Pocket",
                        "source": "javbus", "found": True}

        with tempfile.TemporaryDirectory() as tmp:
            db = MetaDB(Path(tmp))
            plan = plan_one("IPZZ-137", db=db, scan=FakeScan())
            self.assertEqual(plan["actress"], "三上悠亚")
            self.assertEqual(plan["target"], "三上悠亚/IPZZ-137")


class TestMetaScanCache(unittest.TestCase):
    """在线核对必须缓存：同一番号第二次不请求。"""

    def test_重复请求被缓存挡住(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = MetaDB(Path(tmp))
            calls = []

            def fake_fetch(src, avid):
                calls.append((src, avid))
                return {"actress": "三上悠亚", "maker": "Idea Pocket", "title": ""}

            scan = MetaScan(db, fetch=fake_fetch, delay_min=0, delay_max=0)
            r1 = scan.resolve("IPZZ-137")
            r2 = scan.resolve("IPZZ-137")
            self.assertTrue(r1["found"])
            self.assertEqual(len(calls), 1, "第二次命中缓存，不应再请求")
            self.assertEqual(r2["actress"], "三上悠亚")
            # 缓存落盘后重开也能读
            db.flush()
            db2 = MetaDB(Path(tmp))
            self.assertEqual(db2.lookup("IPZZ-137")["actress"], "三上悠亚")

    def test_默认fetch_javbus_解析_不真打网(self):
        """默认 javbus 解析器 —— 用假 opener 顶掉网络，验证 HTML 解析逻辑。"""
        import tempfile as _tf
        from pathlib import Path as _P
        from unittest.mock import patch

        html = """<html><head><title>IPZZ-137 xxx - JavBus</title></head><body>
        <h3>IPZZ-137 真夏の輪 庵ひめか</h3>
        <div class="star-name"><a href="https://www.javbus.com/star/zjy" title="庵ひめか">庵ひめか</a></div>
        <div class="info">メーカー：<a href="#">Idea Pocket</a></div>
        </body></html>"""

        with _tf.TemporaryDirectory() as tmp:
            db = MetaDB(_P(tmp))
            scan = MetaScan(db, delay_min=0, delay_max=0)
            fake = type("FakeResp", (), {"__enter__": lambda s: s,
                                        "__exit__": lambda *a: None,
                                        "read": lambda s: html.encode("utf-8")})()
            fake_opener = type("FakeOpener", (), {
                "open": lambda s, req, timeout=20: fake})()
            with patch.object(scan, "_opener", return_value=fake_opener):
                got = scan._fetch_javbus("IPZZ-137")
            self.assertEqual(got["actress"], "庵ひめか")
            self.assertIn("真夏の輪", got["title"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
