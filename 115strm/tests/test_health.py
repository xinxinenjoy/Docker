"""strm 前缀自检的回归 —— **不联网**，全部用假 opener。

⭐ 这一组用例存在的理由：alist 配错时**状态码是 200**、响应体是 JSON 错误，
   看着不像错。所以要给每种错法都留一个用例，把判据钉死。
"""
from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from app.config import Config
from app.health import PrefixCheck, check_prefix, sample_url


def cfg(prefix: str) -> Config:
    return Config(strm_prefix=prefix, url_encode=True)


class FakeResp:
    def __init__(self, status, headers, body=b"{}"):
        self.status = status
        self.headers = headers
        self._body = body

    def read(self, n=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeHeaders(dict):
    def get(self, k, d=None):
        for kk, vv in self.items():
            if kk.lower() == k.lower():
                return vv
        return d


class TestSampleUrl(unittest.TestCase):
    def test_拼接(self):
        u = sample_url(cfg("https://x/d/媒体/影音"))
        self.assertTrue(u.startswith("https://x/d/媒体/影音/"))

    def test_尾斜杠规整(self):
        self.assertNotIn("//", sample_url(cfg("https://x/d/")).replace("https://", ""))


class TestPrefixGuard(unittest.TestCase):
    def test_dav前缀直接判错_不发请求(self):
        with mock.patch("urllib.request.build_opener") as op:
            r = check_prefix(cfg("https://x/dav/媒体/影音"))
        op.assert_not_called()
        self.assertFalse(r.ok)
        self.assertEqual(r.kind, "auth")
        self.assertIn("/dav", r.message)
        self.assertIn("/d", r.tip)

    def test_空前缀直接判empty_不发请求(self):
        """🔴 出厂状态（前缀是空的）**不能**被误报成「连不上」。

        不拦的话 `sample_url` 会拼出一个 `/…` 相对路径，urllib 抛
        `unknown url type`，被兜底的 `except Exception` 吞成 `kind="network"` ——
        把「你还没填」说成「网络有问题」，排查方向直接跑偏。
        """
        with mock.patch("urllib.request.build_opener") as op:
            r = check_prefix(cfg(""))
        op.assert_not_called()
        self.assertFalse(r.ok)
        self.assertEqual(r.kind, "empty")
        self.assertIn("播放地址", r.tip)

    def test_纯空白前缀也判empty(self):
        with mock.patch("urllib.request.build_opener") as op:
            r = check_prefix(cfg("   "))
        op.assert_not_called()
        self.assertEqual(r.kind, "empty")


class TestPrefixCheck(unittest.TestCase):
    def _run(self, resp=None, exc=None, prefix="https://x/d/媒体/影音"):
        def fake_open(req, timeout=None):
            if exc:
                raise exc
            return resp
        with mock.patch("urllib.request.build_opener") as bo:
            bo.return_value.open.side_effect = fake_open
            return check_prefix(cfg(prefix))

    def test_302到115cdn判成功(self):
        r = self._run(FakeResp(302, FakeHeaders({
            "Location": "https://cdnfhnfile.115cdn.net/abc/x.mkv?t=1"})))
        self.assertTrue(r.ok)
        self.assertEqual(r.kind, "ok")
        self.assertIn("115 CDN", r.message)

    def test_302到115com也判成功(self):
        r = self._run(FakeResp(302, FakeHeaders({
            "Location": "https://cdn.115.com/abc"})))
        self.assertTrue(r.ok)

    def test_302到别处不算成功(self):
        r = self._run(FakeResp(302, FakeHeaders({"Location": "https://别的站/x"})))
        self.assertFalse(r.ok)
        self.assertEqual(r.kind, "http")

    def test_storage_not_found_给出挂载路径提示(self):
        """🔴 最关键的用例 —— 这是最容易犯的错，必须给出可执行的指引。"""
        body = json.dumps({
            "code": 500,
            "message": "storage not found; rawPath: /影音/电影/x.mkv",
        }).encode()
        r = self._run(FakeResp(200, FakeHeaders({
            "Content-Type": "application/json; charset=utf-8"}), body))
        self.assertFalse(r.ok)
        self.assertEqual(r.kind, "storage_not_found")
        self.assertIn("挂载", r.tip)
        self.assertIn("/d/媒体/影音", r.tip)

    def test_object_not_found_判配对正确(self):
        """🔴 `object not found` ≠ 配错 —— 它说明 alist **认得这个存储**，
        只是探针那个假路径下没东西。混为一谈会报假警、淹掉真问题。"""
        body = json.dumps({
            "code": 500,
            "message": "failed link: failed to get file: object not found",
        }).encode()
        r = self._run(FakeResp(200, FakeHeaders({
            "Content-Type": "application/json; charset=utf-8"}), body))
        self.assertTrue(r.ok)
        self.assertEqual(r.kind, "ok")
        self.assertIn("配对正确", r.message)

    def test_failed_get_dir也算配对正确(self):
        body = json.dumps({
            "code": 500,
            "message": "failed link: failed to get file: failed get parent list",
        }).encode()
        r = self._run(FakeResp(200, FakeHeaders({
            "Content-Type": "application/json; charset=utf-8"}), body))
        self.assertTrue(r.ok)

    def test_200纯json但非storage错误(self):
        body = json.dumps({"code": 500, "message": "别的错"}).encode()
        r = self._run(FakeResp(200, FakeHeaders({
            "Content-Type": "application/json"}), body))
        self.assertFalse(r.ok)
        self.assertEqual(r.kind, "http")
        self.assertIn("别的错", r.message)

    def test_401判端点写错(self):
        r = self._run(FakeResp(401, FakeHeaders({})))
        self.assertFalse(r.ok)
        self.assertEqual(r.kind, "auth")
        self.assertIn("/dav", r.tip + r.message)

    def test_200非json视为可疑(self):
        r = self._run(FakeResp(200, FakeHeaders({"Content-Type": "video/x-matroska"})))
        self.assertFalse(r.ok)
        self.assertIn("302", r.message)

    def test_网络异常被兜住(self):
        r = self._run(exc=OSError("连不上"))
        self.assertFalse(r.ok)
        self.assertEqual(r.kind, "network")
        self.assertIn("连不上", r.message)

    def test_http404(self):
        r = self._run(FakeResp(404, FakeHeaders({})))
        self.assertFalse(r.ok)
        self.assertEqual(r.status, 404)

    def test_as_dict结构(self):
        r = self._run(FakeResp(302, FakeHeaders({"Location": "https://x.115cdn.net/y"})))
        d = r.as_dict()
        for k in ("ok", "kind", "message", "tip", "url", "status", "location"):
            self.assertIn(k, d)

    def test_http_error被捕获不抛(self):
        err = urllib.error.HTTPError("u", 403, "forbidden", FakeHeaders({}), io.BytesIO(b""))
        r = self._run(exc=err)
        self.assertFalse(r.ok)
        self.assertEqual(r.status, 403)

    def test_中文路径被编码后再请求(self):
        """🔴 回归：URL 带未编码中文时 urllib 会抛
        `'ascii' codec can't encode characters` —— 必须在请求前逐段 quote。"""
        seen = {}

        def fake_open(req, timeout=None):
            seen['url'] = req.full_url
            return FakeResp(302, FakeHeaders({"Location": "https://x.115cdn.net/y"}))

        with mock.patch("urllib.request.build_opener") as bo:
            bo.return_value.open.side_effect = fake_open
            # 前缀本身带中文 + 样本也不用编码 ⇒ 请求 URL 里必然有中文
            check_prefix(cfg("https://x/d/媒体/影音"))
        u = seen['url']
        self.assertAsciiEncodable(u)
        self.assertIn("%E5%BD%B1%E9%9F%B3", u)     # 「影音」
        self.assertNotIn("影音", u)

    def assertAsciiEncodable(self, s: str) -> None:
        try:
            s.encode("ascii")
        except UnicodeEncodeError:
            self.fail(f"URL 没被编码，urllib 会抛 ascii 错：{s}")

    def test_斜杠在编码后仍保留(self):
        seen = {}

        def fake_open(req, timeout=None):
            seen['url'] = req.full_url
            return FakeResp(302, FakeHeaders({"Location": "https://x.115cdn.net/y"}))

        with mock.patch("urllib.request.build_opener") as bo:
            bo.return_value.open.side_effect = fake_open
            check_prefix(cfg("https://x/d/媒体/影音"))
        self.assertNotIn("%2F", seen['url'])
        self.assertIn("/d/", seen['url'])


class TestOfflineWarn(unittest.TestCase):
    """⚠️ 这一组现在测的是 `scope_warn`（同步根 ↔ 前缀配对）。

    原先是 `web._prefix_warn`（只看前缀形态），2026-10-08 换成 `scope_warn` ——
    因为它能发现更致命的错：**同步根与挂载路径不配对**。
    详细的配对用例在 `test_scope.TestScopeWarn`，这里只留基本形态判定。
    """

    def _warn(self, p: str, root: str = "影音") -> str:
        from app.health import scope_warn
        return scope_warn(Config(strm_prefix=p, remote_root=root))

    def test_空前缀(self):
        self.assertIn("还没配置", self._warn(""))

    def test_缺d段告警(self):
        self.assertIn("/d/", self._warn("https://x/媒体/影音"))

    def test_只到d时提示带上挂载路径(self):
        w = self._warn("https://x/d")
        self.assertIn("挂载路径", w)

    def test_写全了不告警(self):
        self.assertEqual(self._warn("https://x/d/媒体/影音"), "")

    def test_dav前缀被当末段不一致(self):
        # `/dav/...` 不会含 `/d/`（因为 `/dav` 里的 `d` 后面是 `av` 不是 `/`），
        # 所以会先命中「缺 /d 段」这条 —— 无论哪条，都必须报出来。
        w = self._warn("https://x/dav/115strm")
        self.assertTrue(w, "dav 前缀必须告警")


if __name__ == "__main__":
    unittest.main()
