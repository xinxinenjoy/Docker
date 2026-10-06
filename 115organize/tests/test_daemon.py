"""调度与增量检测单测。

两块：
  · `cron_match` —— 自己写的 5 字段 matcher（不引 APScheduler）。
    ⚠️ 「漏跑一次」比「多跑一次」危险得多：漏了就是有目录永远没人整理，
       所以设计上**不认识的写法一律当匹配**。这条要有测试盯着。
  · `Snapshot` / `recent_filter` —— 「新增目录」和「最近接收」的判定。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config                       # noqa: E402
from app.daemon import _field_match, cron_match, validate_cron  # noqa: E402
from app.snapshot import Snapshot, _parse_time, recent_filter  # noqa: E402


class TestCronMatch(unittest.TestCase):
    def test_每天凌晨四点(self):
        expr = "0 4 * * *"
        self.assertTrue(cron_match(expr, datetime(2026, 10, 6, 4, 0)))
        self.assertFalse(cron_match(expr, datetime(2026, 10, 6, 4, 1)))
        self.assertFalse(cron_match(expr, datetime(2026, 10, 6, 3, 0)))

    def test_步长(self):
        self.assertTrue(_field_match("*/6", 0, 0, 59))
        self.assertTrue(_field_match("*/6", 6, 0, 59))
        self.assertFalse(_field_match("*/6", 7, 0, 59))
        self.assertFalse(_field_match("*/6", 5, 0, 59))

    def test_列举与区间(self):
        self.assertTrue(_field_match("1,15,28", 15, 1, 31))
        self.assertFalse(_field_match("1,15,28", 16, 1, 31))
        self.assertTrue(_field_match("1-5", 3, 0, 6))
        self.assertFalse(_field_match("1-5", 6, 0, 6))

    def test_星期口径_0和7都是周日(self):
        """cron 里 0 = 周日，Python 的 `weekday()` 是周一=0 —— 换算错了就会在错误的日子跑。

        2026-10-04 是周日，2026-10-05 是周一。
        """
        sunday = datetime(2026, 10, 4, 4, 0)
        monday = datetime(2026, 10, 5, 4, 0)
        self.assertEqual(sunday.weekday(), 6)
        self.assertTrue(cron_match("0 4 * * 0", sunday))
        self.assertTrue(cron_match("0 4 * * 7", sunday))
        self.assertFalse(cron_match("0 4 * * 0", monday))
        self.assertTrue(cron_match("0 4 * * 1", monday))
        # 工作日：周一到周五
        self.assertFalse(cron_match("0 4 * * 1-5", sunday))
        self.assertTrue(cron_match("0 4 * * 1-5", monday))

    def test_写坏了不会静默永不触发(self):
        """⛔ 方向性：字段看不懂时**判不匹配**（不是「当匹配」）。

        曾经想反过来（宁可多跑），但「多跑」的真实代价不是多跑一轮，而是
        **每分钟打一次 115 全盘** —— 那是风控自杀。所以改成：
        不匹配 + `validate_cron()` 在启动时把它喊出来，不给「静默永不触发」的机会。
        """
        now = datetime(2026, 10, 6, 4, 0)
        self.assertFalse(cron_match("0 4 *", now), "段数不对 ⇒ 不跑")
        self.assertFalse(cron_match("", now))
        self.assertFalse(_field_match("乱写", 4, 0, 23), "看不懂 ⇒ 不匹配")
        self.assertFalse(_field_match("*/x", 4, 0, 23))

    def test_validate_cron_能把坏表达式揪出来(self):
        self.assertEqual(validate_cron("0 4 * * *"), [])
        self.assertEqual(validate_cron("*/6 0-23 * * 1-5"), [])
        self.assertEqual(validate_cron("0 4 * * MON"), [], "英文缩写该被接受")

        self.assertTrue(validate_cron("0 4 *"))                  # 段数不够
        self.assertTrue(validate_cron("0 4 * * MONDAY"))         # 名字不认识
        self.assertTrue(validate_cron("0 25 * * *"))             # 小时越界
        self.assertTrue(validate_cron("0 4 32 * *"))             # 日越界
        self.assertTrue(validate_cron("*/0 * * * *"))            # 步长为 0

    def test_英文缩写的月份与星期(self):
        monday = datetime(2026, 10, 5, 4, 0)
        self.assertTrue(cron_match("0 4 * * MON", monday))
        self.assertFalse(cron_match("0 4 * * TUE", monday))
        self.assertTrue(cron_match("0 4 * OCT *", monday))
        self.assertFalse(cron_match("0 4 * NOV *", monday))

    def test_每小时的整点(self):
        for h in (0, 9, 23):
            self.assertTrue(cron_match("0 * * * *", datetime(2026, 10, 6, h, 0)))
        self.assertFalse(cron_match("0 * * * *", datetime(2026, 10, 6, 9, 1)))


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config()
        self.cfg.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_新增与消失(self):
        snap = Snapshot(dirs={"A": "1", "B": "2"}, files=["f1"])
        current = {"A": "1", "B": "2", "C": "3"}          # C 是新增、B 还在
        self.assertEqual(snap.new_dirs(current), ["C"])
        self.assertEqual(snap.gone_dirs(current), [])
        current2 = {"A": "1", "C": "9"}
        self.assertEqual(snap.gone_dirs(current2), ["B"])

    def test_按名字判定不看id(self):
        """115 上「同名目录删了重建」很常见，id 会变 —— 那种不该当新增（我们整理的是目录本身）。"""
        snap = Snapshot(dirs={"A": "old_id"})
        self.assertEqual(snap.new_dirs({"A": "new_id"}), [])

    def test_存取往返(self):
        snap = Snapshot(dirs={"A": "1"}, files=["f1", "f2"])
        snap.save(self.cfg)
        back = Snapshot.load(self.cfg)
        self.assertEqual(back.dirs, {"A": "1"})
        self.assertEqual(back.files, ["f1", "f2"])
        self.assertTrue(back.time)

    def test_没有快照文件时不炸(self):
        self.assertEqual(Snapshot.load(self.cfg).dirs, {})

    def test_快照文件坏了也不炸(self):
        Snapshot.path(self.cfg).write_text("{ 这不是 json", encoding="utf-8")
        self.assertEqual(Snapshot.load(self.cfg).dirs, {})


class TestRecentFilter(unittest.TestCase):
    def test_时间戳三种形态都能读(self):
        """115 的接收时间有 unix 秒、毫秒、也有字符串。"""
        sec = _parse_time(1759700000)
        self.assertIsInstance(sec, datetime)
        ms = _parse_time(1759700000000)
        self.assertEqual(ms, sec, "毫秒该被折算回秒")
        text = _parse_time("2026-10-06 15:44:15")
        self.assertEqual(text, datetime(2026, 10, 6, 15, 44, 15))
        self.assertEqual(_parse_time("2026-10-06"), datetime(2026, 10, 6))
        self.assertIsNone(_parse_time(""))
        self.assertIsNone(_parse_time("乱写"))

    def test_只留最近N天(self):
        now = datetime.now()
        items = [
            {"name": "今天", "time": now.timestamp()},
            {"name": "两天前", "time": (now - timedelta(days=2)).timestamp()},
            {"name": "十天前", "time": (now - timedelta(days=10)).timestamp()},
            {"name": "时间读不出来", "time": ""},      # 读不出来 ⇒ 保留（宁可多看一个）
        ]
        got = [it["name"] for it in recent_filter(items, days=3)]
        self.assertIn("今天", got)
        self.assertIn("两天前", got)
        self.assertNotIn("十天前", got)
        self.assertIn("时间读不出来", got)

    def test_天数为0等于不过滤(self):
        items = [{"name": "很久以前", "time": 1000}, {"name": "现在", "time": 2 ** 31}]
        self.assertEqual(len(recent_filter(items, days=0)), 2)


class TestDetectNew(unittest.TestCase):
    """`detect_new` 的判据 —— 2026-10-06 真机跑过 `receive_list` 之后重写的。

    ⚠️ 这层曾经用 `it["name"]` + `it["is_dir"]` 筛「最近接收」。那两个字段**接口里根本没有**，
       所以它**永远收不到任何东西**，而且不报错 —— 自动化那半会静默失效
       （表现是「容器在跑，但新目录永远不进整理队列」，极难查）。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config()
        self.cfg.data_dir = Path(self._tmp.name)
        self.cfg.recent_days = 3
        self.logs: list[tuple[str, str]] = []

    def tearDown(self):
        self._tmp.cleanup()

    def _daemon(self, dirs: dict[str, str], receive: list[dict] | None = None):
        from app.daemon import Daemon
        from app.v115 import Node
        d = Daemon(self.cfg, lambda lvl, msg: self.logs.append((lvl, msg)))
        now = datetime.now().timestamp()

        class Fake:
            def root_cid(self) -> str:
                return "ROOT"

            def list_dir(self, cid: str, dirs_only: bool = False) -> list:
                return [Node(id=i, name=n, is_dir=True) for n, i in dirs.items()]

            def receive_list(self, limit: int = 100) -> list[dict]:
                # 真机字段：record_id/file_id/file_name/parent_id/parent_name/time
                return [dict(it, time=it.get("time") or now) for it in (receive or [])]

        d.v = Fake()
        return d

    def test_首次运行只建基线不处理(self):
        d = self._daemon({"A": "1"})
        self.assertEqual(d.detect_new(), [])
        self.assertEqual(Snapshot.load(self.cfg).dirs, {"A": "1"})

    def test_快照diff出新增目录(self):
        Snapshot(dirs={"A": "1"}).save(self.cfg)
        d = self._daemon({"A": "1", "B": "2"})
        self.assertEqual(d.detect_new(), ["B"])

    def test_接收项落在根下_那就是新目录(self):
        Snapshot(dirs={"A": "1"}).save(self.cfg)
        d = self._daemon({"A": "1", "B": "2"}, receive=[
            {"record_id": "r1", "file_id": "", "file_name": "B",
             "parent_id": "ROOT", "parent_name": "云下载"}])
        self.assertEqual(d.detect_new(), ["B"])

    def test_接收项落进已有目录_那个目录也要顺带整理(self):
        """接收进来的会夹广告件，所以「落在谁里面」谁就要过一遍。"""
        Snapshot(dirs={"A": "1", "B": "2"}).save(self.cfg)
        d = self._daemon({"A": "1", "B": "2"}, receive=[
            {"record_id": "r1", "file_id": "f9", "file_name": "inside.mp4",
             "parent_id": "1", "parent_name": "A"}])
        self.assertEqual(d.detect_new(), ["A"])

    def test_聚合项不会被误当目录(self):
        """`file_id=''` 是「多文件聚合」而不是目录；名字又不在根下 ⇒ 不该产生候选。"""
        Snapshot(dirs={"A": "1"}).save(self.cfg)
        d = self._daemon({"A": "1"}, receive=[
            {"record_id": "r1", "file_id": "", "file_name": "主谋.srt等2个文件",
             "parent_id": "ROOT", "parent_name": "云下载"}])
        self.assertEqual(d.detect_new(), [])

    def test_接收拉取失败不影响快照diff(self):
        Snapshot(dirs={"A": "1"}).save(self.cfg)
        d = self._daemon({"A": "1", "B": "2"})

        def boom(limit: int = 100):
            raise RuntimeError("115 抽风")

        d.v.receive_list = boom                       # type: ignore[method-assign]
        self.assertEqual(d.detect_new(), ["B"], "快照 diff 是主力，接收只是补充线索")
        self.assertTrue(any("最近接收" in m for _lvl, m in self.logs), "失败要留日志")


if __name__ == "__main__":
    unittest.main(verbosity=2)
