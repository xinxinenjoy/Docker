"""strm 前缀自检 —— 把「最容易配错、又最难查」的那件事变成工具主动报告的。

## 为什么需要它

alist 的路由是**按挂载路径**匹配的。所以 strm 里的 URL 必须写成：

    https://<域名>:5244/d/<alist 挂载路径>/<文件相对路径>

你的 115 挂在 alist 的 **`/媒体/影音`**（`root_folder_id=<该目录在 115 上的 id>`），
所以整串必须是 `/d/媒体/影音/...`。

⛔ 少写一层（比如 `/d/影音/...`）时 alist 会回：

    {"code":500,"message":"storage not found; rawPath: /影音/电影/…"}

—— 而且**返回 200 状态码**，看着不像错，是**最容易蒙混过关的错**。
（2026-10-08 实测踩到，所以专门做这个自检。）

## 自检做什么

拿配置拼一个 URL，**真去请求一次**，然后判定：
  · 302 + Location 含 `115cdn` / `115.com`  ⇒ ✅ 配对正确
  · 200 + `Content-Type: application/json`  ⇒ ❌ alist 报错了，把 message 原文带回来
  · 401                                        ⇒ ❌ 用错端点了（多半写成了 `/dav`）
  · 连不上                                     ⇒ ⚠️ 域名 / 网络问题
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import Config

__all__ = ["PrefixCheck", "check_prefix", "sample_url", "scope_warn"]


def scope_warn(cfg: Config) -> str:
    """**离线**检查「同步根 ↔ strm 前缀」是否配对 —— 不发请求。

    🔴 为什么需要它：alist 按**挂载路径**路由，而同步根是**树内路径**，
       两者必须指向 115 上的同一个目录。它们的共同点是「最后一层名字」：

           115 盘上：  `影音`            ← 同步根（树内填 `影音`）
           alist 挂载： `/媒体/影音`      ← 前缀写 `…/d/媒体/影音`

        ⇒ **prefix 的末段应当等于 remote_root 的末段**。

       配错时的症状（实测）：alist 回 `storage not found`，
       而 **HTTP 状态码是 200** —— 看着不像错，极难查。所以做成本地可判的告警。
    """
    prefix = (cfg.strm_prefix or "").rstrip("/")
    root = (cfg.remote_root or "").strip("/")
    if not prefix:
        return "还没配置 strm 前缀。"
    if "/d/" not in prefix + "/":
        return "前缀里没有 `/d/` 这一段 —— alist 的下载端点必须带它。"

    tail = prefix.split("/d/", 1)[-1].strip("/") if "/d/" in prefix else ""
    if not tail:
        return ("前缀只写到 `/d`，**没有带上 alist 的挂载路径**。"
                "115 挂在 alist 的哪一级，前缀就得写到那一级（如 `/d/媒体/影音`）。")

    tail_last = tail.rsplit("/", 1)[-1]
    root_last = root.rsplit("/", 1)[-1] if root else ""
    if root_last and tail_last != root_last:
        return (f"**同步根与前缀可能没配对**：同步根末段是 `{root_last}`，"
                f"前缀末段是 `{tail_last}`。"
                f"两者应指向 115 上同一个目录（如都写 `影音`）—— "
                f"配错时 alist 回 `storage not found`，但状态码是 200，很难察觉。")
    return ""

# 判定用的空目录（alist 对「目录」也会走 302 / 报错，所以能用来探路）
_PROBE_TIMEOUT = 15


@dataclass
class PrefixCheck:
    ok: bool = False
    kind: str = ""             # ok / storage_not_found / auth / http / network / no_sample
    message: str = ""
    tip: str = ""
    url: str = ""
    status: int = 0
    location: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "kind": self.kind, "message": self.message,
            "tip": self.tip, "url": self.url, "status": self.status,
            "location": self.location[:300],
        }


def sample_url(cfg: Config, rel: str = "") -> str:
    """拿配置拼一个探针 URL。

    `rel` 留空时用一个中性的目录名 —— 目录也能触发 alist 的路由判定，
    不需要真的存在某个文件。
    """
    from .namer import url_of_path
    prefix = (cfg.strm_prefix or "").rstrip("/")
    if not rel:
        rel = "_probe_"
    return f"{prefix}/{url_of_path(rel, cfg.url_encode)}"


def check_prefix(cfg: Config, *, sample_rel: str = "") -> PrefixCheck:
    """真发一次请求，判定 strm 前缀是否配对正确。"""
    url = sample_url(cfg, sample_rel)
    res = PrefixCheck(url=url)

    # 🔴 前缀是空的（出厂状态）时**先拦下来** —— 不拦的话下面会拿一个 `/xxx`
    #    这种相对路径去发请求，`urllib` 抛 `unknown url type` ⇒ 被兜底 except
    #    吞成「连不上：…」，把「你还没填」误报成「网络有问题」，方向全错。
    if not (cfg.strm_prefix or "").strip():
        res.kind = "empty"
        res.message = "还没配置 strm 内容前缀 —— 它现在是空的，自检没有意义。"
        res.tip = ("去网页「设置 › 播放地址」填 `https://你的域名:端口/d/挂载路径`"
                   "（如 `https://alist.example.com:5244/d/媒体/影音`）。")
        return res

    if "/dav" in url.split("?")[0]:
        res.kind = "auth"
        res.message = "前缀里含 `/dav` —— 那是 WebDAV 端点，实测要认证。"
        res.tip = "把前缀改成 alist 的 `/d` 端点，例如 https://域名:5244/d/媒体/影音"
        return res

    # 🔴 URL 里可能含**未编码的中文**（`url_encode=0` 时，或前缀本身就带中文挂载路径）。
    #    urllib 往 header 写 URL 时按 **ascii** 编 ⇒ 直接抛
    #    `'ascii' codec can't encode characters`。
    #    所以请求前把 path 部分逐段编码一次（**保留 `/`**）。
    from urllib.parse import quote, urlsplit, urlunsplit
    sp = urlsplit(url)
    safe_path = "/".join(quote(p, safe="") for p in sp.path.split("/"))
    url = urlunsplit((sp.scheme, sp.netloc, safe_path, sp.query, sp.fragment))
    res.url = url

    req = urllib.request.Request(url, method="GET",
                                 headers={"User-Agent": "115strm/1.0 (prefix-check)"})
    try:
        # ⛔ 不跟随重定向 —— 我们要看的就是那个 302 本身
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req, timeout=_PROBE_TIMEOUT) as r:
                status, headers, body = r.status, r.headers, r.read(600)
        except urllib.error.HTTPError as e:
            status, headers, body = e.code, e.headers, e.read(600)
    except Exception as exc:
        res.kind = "network"
        res.message = f"连不上：{exc}"
        res.tip = "检查域名 / 端口 / 网络；内网可先用 IP:5244 试。"
        return res

    res.status = status
    res.location = headers.get("Location") or ""
    ctype = (headers.get("Content-Type") or "").lower()

    if status in (301, 302, 303, 307, 308):
        if "115cdn" in res.location or "115.com" in res.location:
            res.ok = True
            res.kind = "ok"
            res.message = "前缀配对正确 —— 已 302 到 115 CDN，播放不占本机带宽。"
        else:
            res.kind = "http"
            res.message = f"302 了，但目标不是 115 CDN：{res.location[:120]}"
            res.tip = "多半是挂载的存储不是 115，或该文件不在 115 上。"
        return res

    if status == 401:
        res.kind = "auth"
        res.message = "401 —— 端点要认证（多半写成了 /dav）。"
        res.tip = "改成 alist 的 `/d` 端点。"
        return res

    if "json" in ctype:
        msg = ""
        try:
            msg = str(json.loads(body.decode("utf-8", "replace")).get("message") or "")
        except Exception:
            msg = body.decode("utf-8", "replace")[:200]

        # 🔴 关键判据：`storage not found` 才是「前缀配错」。
        #    而 `object not found` / `failed to get file` 说明 **alist 认出了这个存储、
        #    只是探针路径下没东西** —— 探针本来就是个假路径，这恰恰证明配对是对的。
        #    曾经把这两种混为一谈 ⇒ 配对正确也报 ❌，把真正的问题淹掉。
        if "storage not found" in msg:
            res.kind = "storage_not_found"
            res.message = f"alist 找不到这个存储：{msg}"
            res.tip = ("**前缀少了挂载路径的那一层**。alist 按挂载路径路由 —— "
                       "115 挂在 `/媒体/影音`，前缀就得写到 `/d/媒体/影音`，"
                       "不能只写到 `/d` 或 `/d/影音`。")
        elif "object not found" in msg or "failed to get file" in msg or "failed get" in msg:
            res.ok = True
            res.kind = "ok"
            res.message = ("前缀配对正确 —— alist 认得这个存储，"
                           "探针是假路径所以报「找不到文件」，这是预期结果。")
            res.tip = ("想更彻底地验证，就去「同步」页预览差异，"
                       "拿真实文件的 URL 用播放器试一下。")
        else:
            res.kind = "http"
            res.message = f"HTTP {status}：{msg}"
        return res

    if status == 200:
        res.kind = "http"
        res.message = "200（不是 302）—— 可能是目录，或内容被代理回来了。"
        res.tip = "换成探一个真实文件的相对路径再试。"
        return res

    res.kind = "http"
    res.message = f"HTTP {status}"
    return res
