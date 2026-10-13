"""推送模块的离线单测。

⭐ 这里锁的是**请求形态**（URL / 方法 / 请求体 / 头 / 判据），不是「真能不能发出去」——
   真机可达性只能靠网页「发送测试」或实跑一轮。
   所以断言里拿的是**青龙面板 `sendNotify.js` 的原话**（那份是实读的），
   这样以后谁改了调用形态，这里立刻红。

⚠️ 有一处**故意与青龙不同**，有专门的用例钉住：`$title` 缺失时我们**报错**，青龙**静默不发**。
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import notify


def _cfg(**kw) -> SimpleNamespace:
    """造一份配置 —— 字段名与 `config.Config` 一致（推送那一段）。"""
    base = dict(
        push_enable=False, output_dir="",
        bark_enable=False, bark_push="", bark_group="", bark_sound="",
        bark_icon="", bark_level="", bark_archive="", bark_url="",
        tg_enable=False, tg_bot_token="", tg_user_id="",
        tg_api_host="https://api.telegram.org",
        tg_proxy_host="", tg_proxy_port="", tg_proxy_auth="",
        webhook_enable=False, webhook_url="", webhook_method="",
        webhook_content_type="application/json", webhook_body="", webhook_headers="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


OK200 = (200, '{"code":200}', "")


class TestEnc(unittest.TestCase):
    """`_enc` 必须等价于 JS 的 `encodeURIComponent`（青龙就是用它拼 Bark 的路径）。"""

    def test_斜杠要被编码(self):
        # ⚠️ 这条最要紧：Python 的 `quote()` 默认 `safe='/'`，
        #    而 Bark 的标题/正文是**路径段** —— 标题里带个 `/` 就会把 URL 切错层。
        self.assertIn("%2F", notify._enc("a/b"))
        self.assertNotIn("/", notify._enc("a/b"))

    def test_空格与非ASCII(self):
        self.assertEqual(notify._enc("a b"), "a%20b")
        self.assertTrue(notify._enc("中文").startswith("%E4%B8%AD"))

    def test_encodeURIComponent_不编码的那几个字符(self):
        for ch in "!*'()":
            self.assertEqual(notify._enc(f"a{ch}b"), f"a{ch}b")


class TestBark(unittest.TestCase):
    def test_只填设备码要自动补前缀(self):
        # 青龙的原话：「兼容BARK本地用户只填写设备码的情况」
        cfg = _cfg(bark_push="AbCdEf123")
        with mock.patch.object(notify, "_http", return_value=OK200) as m:
            r = notify.send_bark(cfg, "标题", "正文")
        self.assertTrue(r.ok)
        self.assertTrue(m.call_args[0][1].startswith("https://api.day.app/AbCdEf123/"), m.call_args[0][1])

    def test_填了完整地址就原样用(self):
        cfg = _cfg(bark_push="https://api.day.app/k/")
        with mock.patch.object(notify, "_http", return_value=OK200) as m:
            notify.send_bark(cfg, "标题", "正文")
        url = m.call_args[0][1]
        self.assertTrue(url.startswith("https://api.day.app/k/"), url)
        self.assertNotIn("//k//", url)

    def test_是GET且标题正文在路径里(self):
        cfg = _cfg(bark_push="https://api.day.app/k")
        with mock.patch.object(notify, "_http", return_value=OK200) as m:
            notify.send_bark(cfg, "标题", "正文")
        self.assertEqual(m.call_args[0][0], "GET")

    def test_附加参数照青龙拼进query(self):
        cfg = _cfg(bark_push="https://api.day.app/k", bark_group="115strm",
                   bark_sound="birdsong", bark_level="active")
        with mock.patch.object(notify, "_http", return_value=OK200) as m:
            notify.send_bark(cfg, "标题", "正文")
        url = m.call_args[0][1]
        self.assertIn("group=115strm", url)
        self.assertIn("sound=birdsong", url)
        self.assertIn("level=active", url)

    def test_没填就不发(self):
        r = notify.send_bark(_cfg(), "t", "c")
        self.assertFalse(r.ok)
        self.assertIn("BARK_PUSH", r.detail)

    def test_判据是code200(self):
        cfg = _cfg(bark_push="https://api.day.app/k")
        with mock.patch.object(notify, "_http", return_value=(200, '{"code":400,"message":"bad key"}', "")):
            r = notify.send_bark(cfg, "t", "c")
        self.assertFalse(r.ok)
        self.assertIn("bad key", r.detail)


class TestTelegram(unittest.TestCase):
    def test_缺任一必填项就不发(self):
        for kw in ({}, {"tg_bot_token": "t"}, {"tg_user_id": "1"}):
            r = notify.send_tg(_cfg(**kw), "t", "c")
            self.assertFalse(r.ok)
            self.assertIn("TG_BOT_TOKEN", r.detail)

    def test_请求形态(self):
        cfg = _cfg(tg_bot_token="123:ABC", tg_user_id="999")
        with mock.patch.object(notify, "_http", return_value=(200, '{"ok":true}', "")) as m:
            r = notify.send_tg(cfg, "标题", "正文")
        self.assertTrue(r.ok)
        self.assertEqual(m.call_args[0][0], "POST")
        self.assertEqual(m.call_args[0][1], "https://api.telegram.org/bot123:ABC/sendMessage")
        body = json.loads(m.call_args[1]["data"].decode("utf-8"))
        self.assertEqual(body["chat_id"], "999")
        self.assertEqual(body["text"], "标题\n\n正文")
        self.assertIs(body["disable_web_page_preview"], True)
        self.assertEqual(m.call_args[1]["headers"]["Content-Type"], "application/json")

    def test_api_host_可换成反代(self):
        cfg = _cfg(tg_bot_token="t", tg_user_id="1", tg_api_host="https://tg.example.com/")
        with mock.patch.object(notify, "_http", return_value=(200, '{"ok":true}', "")) as m:
            notify.send_tg(cfg, "t", "c")
        self.assertEqual(m.call_args[0][1], "https://tg.example.com/bott/sendMessage")

    def test_代理按青龙的拼法(self):
        # 青龙：`proxy: 'http://${TG_PROXY_AUTH}${TG_PROXY_HOST}:${TG_PROXY_PORT}'`
        cfg = _cfg(tg_bot_token="t", tg_user_id="1", tg_proxy_host="127.0.0.1",
                   tg_proxy_port="7890", tg_proxy_auth="u:p@")
        with mock.patch.object(notify, "_http", return_value=(200, '{"ok":true}', "")) as m:
            notify.send_tg(cfg, "t", "c")
        self.assertEqual(m.call_args[1]["proxy"], "http://u:p@127.0.0.1:7890")

    def test_没配代理时为空(self):
        cfg = _cfg(tg_bot_token="t", tg_user_id="1")
        with mock.patch.object(notify, "_http", return_value=(200, '{"ok":true}', "")) as m:
            notify.send_tg(cfg, "t", "c")
        self.assertEqual(m.call_args[1]["proxy"], "")

    def test_400与401给可执行提示(self):
        # 照青龙的口径（它这两个码的文案就是干这个用的）
        cfg = _cfg(tg_bot_token="t", tg_user_id="1")
        for code, word in ((400, "先给这个 bot 发一条消息"), (401, "TOKEN 填错")):
            body = json.dumps({"ok": False, "error_code": code, "description": "x"})
            with mock.patch.object(notify, "_http", return_value=(code, body, "")):
                r = notify.send_tg(cfg, "t", "c")
            self.assertFalse(r.ok)
            self.assertIn(word, r.detail)


class TestWebhook(unittest.TestCase):
    def test_缺地址或方法就不发(self):
        r = notify.send_webhook(_cfg(), "t", "c")
        self.assertFalse(r.ok)
        self.assertIn("WEBHOOK_URL", r.detail)
        # 青龙：「空则不发」
        r2 = notify.send_webhook(_cfg(webhook_url="http://x"), "t", "c")
        self.assertFalse(r2.ok)
        self.assertIn("WEBHOOK_METHOD", r2.detail)

    def test_没有_title占位要明确报错(self):
        """⚠️ 这条是**故意偏离青龙**的地方。

        青龙遇到这种情况直接 `resolve()` —— **静默不发、什么都不告诉你**。
        本项目最忌这种「看起来成功的失败」（配了半天、以为在推、其实一条没发）。
        """
        cfg = _cfg(webhook_url="http://x", webhook_method="POST", webhook_body="a: b")
        r = notify.send_webhook(cfg, "t", "c")
        self.assertFalse(r.ok)
        self.assertIn("$title", r.detail)

    def test_URL里的占位要编码_body的占位不编码(self):
        cfg = _cfg(webhook_url="http://x/?t=$title", webhook_method="POST",
                   webhook_body="content: $content")
        with mock.patch.object(notify, "_http", return_value=(200, "ok", "")) as m:
            notify.send_webhook(cfg, "有 空格", "正文 带空格")
        # URL 里它是地址的一部分 ⇒ 必须编码
        self.assertIn("t=%E6%9C%89%20%E7%A9%BA%E6%A0%BC", m.call_args[0][1])
        # BODY 里它是内容 ⇒ 不编码（编了对方收到的就是一串 %XX）
        body = json.loads(m.call_args[1]["data"].decode("utf-8"))
        self.assertEqual(body["content"], "正文 带空格")

    def test_JSON_body(self):
        cfg = _cfg(webhook_url="http://x", webhook_method="POST",
                   webhook_body="title: $title\ncontent: $content")
        with mock.patch.object(notify, "_http", return_value=(200, "ok", "")) as m:
            notify.send_webhook(cfg, "标题", "正文")
        self.assertEqual(json.loads(m.call_args[1]["data"].decode("utf-8")),
                         {"title": "标题", "content": "正文"})
        self.assertEqual(m.call_args[1]["headers"]["content-type"], "application/json")

    def test_urlencoded(self):
        cfg = _cfg(webhook_url="http://x", webhook_method="POST",
                   webhook_content_type="application/x-www-form-urlencoded",
                   webhook_body="title: $title")
        with mock.patch.object(notify, "_http", return_value=(200, "ok", "")) as m:
            notify.send_webhook(cfg, "标题", "正文")
        self.assertEqual(m.call_args[1]["data"].decode("utf-8"), "title=%E6%A0%87%E9%A2%98")

    def test_multipart(self):
        cfg = _cfg(webhook_url="http://x", webhook_method="POST",
                   webhook_content_type="multipart/form-data", webhook_body="title: $title")
        with mock.patch.object(notify, "_http", return_value=(200, "ok", "")) as m:
            notify.send_webhook(cfg, "标题", "正文")
        self.assertIn("multipart/form-data; boundary=", m.call_args[1]["headers"]["content-type"])
        self.assertIn(b'name="title"', m.call_args[1]["data"])

    def test_text_plain整段就是正文(self):
        cfg = _cfg(webhook_url="http://x", webhook_method="POST",
                   webhook_content_type="text/plain", webhook_body="$title\n$content")
        with mock.patch.object(notify, "_http", return_value=(200, "ok", "")) as m:
            notify.send_webhook(cfg, "标题", "正文")
        self.assertEqual(m.call_args[1]["data"].decode("utf-8"), "标题\n正文")

    def test_headers行小写且同名合并(self):
        cfg = _cfg(webhook_url="http://x", webhook_method="POST", webhook_body="title: $title",
                   webhook_headers="X-A: 1\nX-A: 2\nAuthorization: Bearer z")
        with mock.patch.object(notify, "_http", return_value=(200, "ok", "")) as m:
            notify.send_webhook(cfg, "t", "c")
        h = m.call_args[1]["headers"]
        self.assertEqual(h["x-a"], "1, 2")
        self.assertEqual(h["authorization"], "Bearer z")

    def test_body的解析规则照青龙(self):
        # 青龙 `parseString`：重复键**只取第一条**、值**会试 JSON.parse**、键**不转小写**
        self.assertEqual(notify._parse_body_kv("a: 1\na: 2"), {"a": 1})
        self.assertEqual(notify._parse_body_kv("N: 3"), {"N": 3})
        self.assertEqual(notify._parse_body_kv("s: hello"), {"s": "hello"})
        # 而 headers 那份是**同名合并 + 键小写**（两处语义不同，都是照青龙）
        self.assertEqual(notify._parse_headers("X-A: 1\nX-A: 2"), {"x-a": "1, 2"})

    def test_status不是2xx算失败(self):
        cfg = _cfg(webhook_url="http://x", webhook_method="POST", webhook_body="title: $title")
        with mock.patch.object(notify, "_http", return_value=(500, "boom", "HTTP 500")):
            r = notify.send_webhook(cfg, "t", "c")
        self.assertFalse(r.ok)
        self.assertIn("500", r.detail)


class TestChannels(unittest.TestCase):
    def test_总开关关着什么都不发(self):
        self.assertEqual(notify.channels(_cfg(bark_enable=True, bark_push="k")), [])

    def test_开关开了但必填项没齐也不算(self):
        self.assertEqual(notify.channels(_cfg(push_enable=True, bark_enable=True)), [])

    def test_配齐才算启用(self):
        self.assertEqual(
            notify.channels(_cfg(push_enable=True, bark_enable=True, bark_push="k")), ["bark"])
        self.assertEqual(
            notify.channels(_cfg(push_enable=True, tg_enable=True,
                                 tg_bot_token="t", tg_user_id="1")), ["tg"])
        self.assertEqual(
            notify.channels(_cfg(push_enable=True, webhook_enable=True,
                                 webhook_url="http://x", webhook_method="POST")), ["webhook"])


class TestSendAll(unittest.TestCase):
    def test_单渠道抛异常不影响别的(self):
        """推送是旁路 —— 一个渠道炸了不能连累其它渠道，更不能往上抛。"""
        cfg = _cfg(push_enable=True, bark_enable=True, bark_push="k",
                   webhook_enable=True, webhook_url="http://x",
                   webhook_method="POST", webhook_body="title: $title")

        def boom(c, t, x):
            raise RuntimeError("炸了")

        with mock.patch.dict(notify._SENDERS, {"bark": boom}):
            with mock.patch.object(notify, "_http", return_value=(200, "ok", "")):
                res = notify.send_all(cfg, "t", "c")
        by = {r.channel: r for r in res}
        self.assertFalse(by["bark"].ok)
        self.assertIn("炸了", by["bark"].detail)
        self.assertTrue(by["webhook"].ok)

    def test_run_总开关关着不发送(self):
        with mock.patch.object(notify, "send_all") as m:
            notify.notify_run(_cfg(), {"written": 1})
        m.assert_not_called()


class TestSummary(unittest.TestCase):
    def test_有变动时报变动(self):
        t, b = notify.build_summary({"written": 12, "deleted": 3, "dirs_made": 5,
                                     "elapsed_total": "22 秒", "requests": 7})
        self.assertIn("有变动", t)
        self.assertIn("新增 12", b)
        self.assertIn("删 3", b)
        self.assertIn("目录 建 5", b)
        self.assertIn("22 秒", b)

    def test_无变动时报部数(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for rel in ("电影/a.strm", "电影/b.strm", "电视剧/x/c.strm"):
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x", encoding="utf-8")
            t, b = notify.build_summary({"written": 0, "deleted": 0, "unchanged": 9}, out_dir=root)
        self.assertIn("无变动", t)
        self.assertIn("电影 2 部", b)
        self.assertIn("电视剧 1 部", b)

    def test_挂起必须单独说(self):
        # 「无变动」+ 154 条挂起 ⇒ 只报「没变」会让人以为一切正常
        _t, b = notify.build_summary({"written": 0, "deleted": 0, "pending_delete": 154})
        self.assertIn("154", b)
        self.assertIn("待确认删除", b)

    def test_有错误时标题是这一轮出错了(self):
        t, b = notify.build_summary({"errors": [{"where": "导树", "msg": "cookie 失效"}]})
        self.assertIn("出错", t)
        self.assertIn("cookie 失效", b)


class TestLocalStats(unittest.TestCase):
    def test_按顶层目录数部数(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for rel in ("电影/a.strm", "电影/b.strm", "电视剧/x/c.strm", "散.strm"):
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x", encoding="utf-8")
            (root / "电影" / "note.txt").write_text("x", encoding="utf-8")   # 非 strm 不算
            stats = dict(notify.local_stats(root))
        self.assertEqual(stats, {"电影": 2, "电视剧": 1, "（根目录）": 1})

    def test_跳过群晖垃圾目录(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for rel in ("@eaDir/a.strm", "#recycle/b.strm", ".hidden/c.strm", "电影/d.strm"):
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x", encoding="utf-8")
            stats = dict(notify.local_stats(root))
        self.assertEqual(stats, {"电影": 1})

    def test_目录不存在返回空(self):
        self.assertEqual(notify.local_stats(Path("D:/nope/绝对不存在")), [])


if __name__ == "__main__":
    unittest.main()
