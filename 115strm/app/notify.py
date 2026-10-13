"""推送 —— 一轮跑完，把结果发出去（Bark / Telegram / 自定义 Webhook）。

## 为什么字段名长得像环境变量

三个渠道的**字段名与调用形态照搬青龙面板**（`/ql/data/scripts/sendNotify.js`），
所以你在青龙里配好的值可以**直接抄过来**。下面这张表是**实读**该文件得出的，不是照记忆写的：

| 渠道 | 关键点 |
|---|---|
| Bark | **GET** —— 标题 / 正文是**路径段**（不是 query），各自 URL 编码；`BARK_PUSH` 不以 `http` 开头就补 `https://api.day.app/`（兼容只填设备码）|
| Telegram | POST `{TG_API_HOST}/bot{TOKEN}/sendMessage`，JSON `{chat_id, text, disable_web_page_preview}`；网络出口可用 `TG_PROXY_*` |
| 自定义 | `$title` / `$content` 占位；四种 content-type；**URL 或 BODY 至少要有一个含 `$title`** |

⚠️ 有一处**故意不照搬**：青龙那个「模板里没有 `$title` 就**静默不发**」的行为 ——
   我们照旧要求 `$title`，但**明确报错**（`ok=False` + 说清原因），不复制它的静默。
   本项目最忌「看起来成功的失败」：配了却不发、还不告诉你，是最坏的一种。

## 两条硬约束

⛔ **零第三方依赖** —— HTTP 走 `urllib.request`，与 `health.py` / `accounts.py` 同一套写法。
⛔ **发送失败绝不往上抛** —— 推送是**旁路**，不能因为推送挂了把一整轮同步判成失败。
   失败只落进返回值（网页「发送测试」会显示出来）+ 一行日志。

## 消息口径（红领巾 2026-10-13 定）

「**每次运行都推送**，有变动的报变动，**无变动的报当前剧集、电影的数据**」
⇒ 每轮跑完必发一条，正文分三种：有变动 / 无变动 / 出错。见 `build_summary()`。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CHANNELS", "CHANNEL_NAMES", "PushResult",
    "channels", "send_one", "send_all", "build_summary", "notify_run", "local_stats",
]

CHANNELS: tuple[str, ...] = ("bark", "tg", "webhook")
CHANNEL_NAMES = {"bark": "Bark", "tg": "Telegram", "webhook": "自定义"}

_TIMEOUT = 15
_UA = "115strm/1.0 (+push)"
_SKIP_DIRS = ("@eaDir", "#recycle")


@dataclass
class PushResult:
    """一次发送的结果 —— 网页「发送测试」直接显示这个。"""
    channel: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"channel": self.channel, "name": CHANNEL_NAMES.get(self.channel, self.channel),
                "ok": self.ok, "detail": self.detail}


# --------------------------------------------------------------------------- HTTP 底座
def _enc(s: object) -> str:
    """等价于 JS 的 `encodeURIComponent`。

    ⚠️ 别图省事用 `quote(s)`：它默认 `safe='/'`，会把路径分隔符留下 ——
       Bark 的标题／正文正是**路径段**，标题里带个 `/`（剧名很常见）就会把 URL 切错层。
    """
    return urllib.parse.quote(str(s if s is not None else ""), safe="!'()*-._~")


def _safe_url(url: str) -> str:
    """把 URL 里没法直接放进请求行的字符编码掉。

    ⚠️ 照 `health.py` 的教训：`urllib` 写请求头是按 **ascii** 编的 ——
       URL 里但凡有个中文（自定义渠道的地址很容易带）就直接抛，报错还看不出原因。
    """
    p = urllib.parse.urlsplit(url)
    keep = "/%:@&=+$,;~!*'()"
    return urllib.parse.urlunsplit((
        p.scheme, p.netloc,
        urllib.parse.quote(p.path, safe=keep),
        urllib.parse.quote(p.query, safe=keep + "?"),
        p.fragment,
    ))


def _http(method: str, url: str, *, data: bytes | None = None,
          headers: dict | None = None, proxy: str = "") -> tuple[int, str, str]:
    """发一次请求 → `(状态码, 响应正文, 错误说明)`。**从不抛异常。**

    状态码 `0` 表示连请求都没发出去（DNS / 超时 / 连接被拒），错误说明里带原因。

    ⚠️ `proxy` 为空时用 `ProxyHandler({})` **显式禁用**环境变量里的代理。
       理由：本机（Windows）常年开着系统代理，`urllib` 默认会读 `http_proxy`
       ⇒ 探本机地址也会被劫持。要代理的只有 TG，而且它有自己的 `TG_PROXY_*` 字段。
    """
    req = urllib.request.Request(_safe_url(url), data=data, method=method.upper())
    req.add_header("User-Agent", _UA)
    for k, v in (headers or {}).items():
        req.add_header(str(k), str(v))
    handlers: list = [] if proxy else [urllib.request.ProxyHandler({})]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=_TIMEOUT) as r:
            return int(r.status), r.read().decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return int(e.code), body, f"HTTP {e.code}"
    except Exception as e:                      # noqa: BLE001 —— 旁路，什么都不能让它炸
        return 0, "", f"{type(e).__name__}: {e}"


def _json_or_text(body: str):
    """响应可能是 JSON，也可能是一段裸文本（PushMe 那类返回 `success`）。"""
    try:
        return json.loads(body)
    except Exception:
        return body


def _g(cfg, name: str, default: str = "") -> str:
    """取配置值并去空白。

    ⚠️ 用 `getattr` 兜底而不是 `cfg.x`：推送是旁路，字段万一缺失也**不该炸**，
       只当没配（返回空串 ⇒ 该渠道不启用）。
    """
    v = getattr(cfg, name, None)
    if v is None:
        return default
    return str(v).strip() if isinstance(v, str) else str(v)


def _on(cfg, name: str) -> bool:
    return bool(getattr(cfg, name, False))


# --------------------------------------------------------------------------- Bark
def send_bark(cfg, title: str, content: str) -> PushResult:
    push = _g(cfg, "bark_push")
    if not push:
        return PushResult("bark", False, "没填 BARK_PUSH（地址或设备码）")
    # 兼容只填设备码（青龙的行为）—— 不补的话会拼出一个没法请求的相对地址
    if not push.lower().startswith("http"):
        push = f"https://api.day.app/{push}"
    push = push.rstrip("/")
    q = urllib.parse.urlencode({
        "icon": _g(cfg, "bark_icon"),
        "sound": _g(cfg, "bark_sound"),
        "group": _g(cfg, "bark_group"),
        "isArchive": _g(cfg, "bark_archive"),
        "level": _g(cfg, "bark_level"),
        "url": _g(cfg, "bark_url"),
    })
    url = f"{push}/{_enc(title)}/{_enc(content)}?{q}"
    status, body, err = _http("GET", url)
    if err and status != 200:
        return PushResult("bark", False, f"请求失败：{err}")
    data = _json_or_text(body)
    if isinstance(data, dict) and int(data.get("code") or 0) == 200:
        return PushResult("bark", True, "已发送")
    msg = data.get("message") if isinstance(data, dict) else body
    return PushResult("bark", False, f"接口回：{str(msg)[:200] or status}")


# --------------------------------------------------------------------------- Telegram
def send_tg(cfg, title: str, content: str) -> PushResult:
    token, uid = _g(cfg, "tg_bot_token"), _g(cfg, "tg_user_id")
    if not (token and uid):
        return PushResult("tg", False, "缺 TG_BOT_TOKEN 或 TG_USER_ID")
    host = (_g(cfg, "tg_api_host") or "https://api.telegram.org").rstrip("/")
    payload = json.dumps({
        "chat_id": uid,
        "text": f"{title}\n\n{content}",
        "disable_web_page_preview": True,
    }, ensure_ascii=False).encode("utf-8")
    # 代理：照青龙的拼法 `http://{AUTH}{HOST}:{PORT}`（AUTH 形如 `user:pass@`，自带尾 @）
    proxy = ""
    ph, pp = _g(cfg, "tg_proxy_host"), _g(cfg, "tg_proxy_port")
    if ph and pp:
        proxy = f"http://{_g(cfg, 'tg_proxy_auth')}{ph}:{pp}"
    status, body, err = _http(
        "POST", f"{host}/bot{token}/sendMessage", data=payload,
        headers={"Content-Type": "application/json"}, proxy=proxy,
    )
    if err and status == 0:
        return PushResult("tg", False, f"请求失败：{err}" + ("（配了代理，先确认代理通不通）" if proxy else ""))
    data = _json_or_text(body)
    if isinstance(data, dict) and data.get("ok"):
        return PushResult("tg", True, "已发送")
    code = data.get("error_code") if isinstance(data, dict) else status
    # 照青龙的口径给可执行的提示（它这两个码的文案就是干这个用的）
    if code == 400:
        return PushResult("tg", False, "400：先给这个 bot 发一条消息，再检查 TG_USER_ID 对不对")
    if code == 401:
        return PushResult("tg", False, "401：TG_BOT_TOKEN 填错了")
    desc = data.get("description") if isinstance(data, dict) else body
    return PushResult("tg", False, f"接口回 {code}：{str(desc)[:200]}")


# --------------------------------------------------------------------------- 自定义 Webhook
def _parse_headers(text: str) -> dict:
    """青龙 `parseHeaders`：**每行一条 `键: 值`**，键转**小写**，同名**合并**（逗号连接）。

    ⚠️ 两个「照搬」的点，别顺手改：① 键小写；② 值**不做 JSON 解析**
       （`X-Count: 3` 就该是字符串 `"3"`，转成数字反而可能让对端不认）。
    """
    out: dict = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if not k:
            continue
        out[k] = f"{out[k]}, {v}" if k in out else v
    return out


def _parse_body_kv(text: str) -> dict:
    """青龙 `parseString`：**每行一条 `键: 值`**，**重复键只取第一条**，值会试一次 `JSON.parse`。

    ⚠️ 与 `_parse_headers` 有两处不同，都是照搬青龙：① 键**不转小写**；② 值**会试 JSON**。
    """
    out: dict = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if not k or k in out:
            continue
        try:
            out[k] = json.loads(v)
        except Exception:
            out[k] = v
    return out


def send_webhook(cfg, title: str, content: str) -> PushResult:
    url_tpl, method = _g(cfg, "webhook_url"), _g(cfg, "webhook_method")
    body_tpl = getattr(cfg, "webhook_body", "") or ""
    if not url_tpl:
        return PushResult("webhook", False, "没填 WEBHOOK_URL")
    if not method:
        return PushResult("webhook", False, "没填 WEBHOOK_METHOD（青龙的行为：空则不发）")
    # ⚠️ 这条是青龙的硬要求。它有它的道理（不填占位的模板发出去也没意义），
    #    但它选择**静默不发** —— 我们照旧拦，但要**说出来**。
    if "$title" not in url_tpl and "$title" not in body_tpl:
        return PushResult(
            "webhook", False,
            "URL 和 BODY 里都没有 $title 占位 —— 这样发出去对方拿不到内容。"
            "请至少在一处写上 $title（正文用 $content）",
        )
    ctype = _g(cfg, "webhook_content_type") or "application/json"
    headers = _parse_headers(_g(cfg, "webhook_headers"))
    # ⚠️ URL 里的占位要**编码**（它会成为地址的一部分）；BODY 里的**不编码**（那是内容）。
    url = url_tpl.replace("$title", _enc(title)).replace("$content", _enc(content))
    data: bytes | None = None
    if ctype == "text/plain":
        headers.setdefault("content-type", "text/plain;charset=utf-8")
        data = (body_tpl or f"{title}\n{content}").replace("$title", title).replace("$content", content).encode("utf-8")
    elif body_tpl:
        kv = _parse_body_kv(body_tpl.replace("$title", title).replace("$content", content))
        if ctype == "application/x-www-form-urlencoded":
            headers.setdefault("content-type", ctype)
            data = urllib.parse.urlencode(kv).encode("utf-8")
        elif ctype == "multipart/form-data":
            boundary = "----115strmWebhookBoundary"
            headers.setdefault("content-type", f"multipart/form-data; boundary={boundary}")
            chunks = []
            for k, v in kv.items():
                chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n")
            data = ("".join(chunks) + f"--{boundary}--\r\n").encode("utf-8")
        else:                                   # application/json（默认）
            headers.setdefault("content-type", "application/json")
            data = json.dumps(kv, ensure_ascii=False).encode("utf-8")
    status, body, err = _http(method, url, data=data, headers=headers)
    if status == 0:
        return PushResult("webhook", False, f"请求失败：{err}")
    if 200 <= status < 300:
        return PushResult("webhook", True, f"HTTP {status}")
    return PushResult("webhook", False, f"HTTP {status}：{body[:200]}")


_SENDERS = {"bark": send_bark, "tg": send_tg, "webhook": send_webhook}


# --------------------------------------------------------------------------- 调度
def _enabled(cfg, ch: str) -> bool:
    """这个渠道**真的会发**吗？

    ⭐ 2026-10-13 起是**单选**：先看它是不是「发送渠道」里选中的那一个
       （红领巾：「选择关闭的时候就不启用，选择哪一个就启用哪个」），再看必填项齐不齐。
    ⚠️ 前端 `index.html` 的 `PUSH_CHANNELS[].need` 必须与下面这几条一一对应 ——
       对不上就会出现「界面说已配好、后端压根不发」这种最难查的形态。
    """
    if ch not in CHANNELS or _g(cfg, "push_channel").lower() != ch:
        return False
    if ch == "bark":
        return bool(_g(cfg, "bark_push"))
    if ch == "tg":
        return bool(_g(cfg, "tg_bot_token")) and bool(_g(cfg, "tg_user_id"))
    if ch == "webhook":
        return bool(_g(cfg, "webhook_url")) and bool(_g(cfg, "webhook_method"))
    return False


def channels(cfg) -> list[str]:
    """当前**真的会发**的渠道 —— 总开关开着 + 被选中 + 必填项齐。**最多一个**。"""
    if not _on(cfg, "push_enable"):
        return []
    return [c for c in CHANNELS if _enabled(cfg, c)]


def send_one(cfg, ch: str, title: str, content: str) -> PushResult:
    """单渠道发送 —— 给网页「发送测试」用，**不看开关**（点了就是要发）。"""
    fn = _SENDERS.get(ch)
    if not fn:
        return PushResult(ch, False, f"不认识的渠道：{ch}")
    return fn(cfg, title, content)


def send_all(cfg, title: str, content: str, log=None) -> list[PushResult]:
    """把消息发给所有启用的渠道。**任何一路失败都不影响别的，也不往上抛。**"""
    out: list[PushResult] = []
    for ch in channels(cfg):
        try:
            r = send_one(cfg, ch, title, content)
        except Exception as e:                  # noqa: BLE001 —— 兜底：绝不让推送打断主流程
            r = PushResult(ch, False, f"未预期的错误：{type(e).__name__}: {e}")
        out.append(r)
        if log:
            log("info" if r.ok else "warn",
                f"推送 [{CHANNEL_NAMES.get(ch, ch)}] {'成功' if r.ok else '失败'} —— {r.detail}")
    return out


# --------------------------------------------------------------------------- 消息内容
def _n(v) -> str:
    """数字带千分位（手机上读 4,306 比 4306 快）。"""
    try:
        return f"{int(v):,}"
    except Exception:
        return str(v or 0)


def _field(res, key: str, default=0):
    """兼容两种来源：`SyncResult`（dataclass）与网页回执（dict）。"""
    if isinstance(res, dict):
        return res.get(key, default)
    return getattr(res, key, default)


def local_stats(out_dir) -> list[tuple[str, int]]:
    """按输出目录的**顶层目录**数 strm 条数 → `[("电影", 1234), ("电视剧", 456)]`。

    这是红领巾要的「无变动时报当前剧集、电影的数据」。口径就一条：
    **数本地已落盘的 .strm**（不在网盘侧数 —— 网盘上有、本地没有的不算进你的媒体库）。

    ⚠️ 只在**无变动**时调用 —— 有变动时报变动就够了，不必再扫一遍盘（省一轮 os.walk）。
    """
    out = Path(out_dir) if out_dir else None
    if not out or not out.exists():
        return []
    top: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(out):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS]
        n = sum(1 for fn in filenames if fn.endswith(".strm"))
        if not n:
            continue
        rel = os.path.relpath(dirpath, out).replace("\\", "/")
        key = rel.split("/")[0] if rel != "." else "（根目录）"
        top[key] = top.get(key, 0) + n
    return sorted(top.items(), key=lambda kv: -kv[1])


def build_summary(res, out_dir=None) -> tuple[str, str]:
    """一轮的结果 → `(标题, 正文)`。三种口径：出错 / 有变动 / 无变动。"""
    errors = _field(res, "errors", []) or []
    written = int(_field(res, "written", 0) or 0)
    updated = int(_field(res, "unchanged", 0) or 0)
    deleted = int(_field(res, "deleted", 0) or 0)
    renamed = int(_field(res, "renamed", 0) or 0)
    dirs_made = int(_field(res, "dirs_made", 0) or 0)
    dirs_pruned = int(_field(res, "dirs_pruned", 0) or 0)
    pending = int(_field(res, "pending_delete", 0) or 0)
    subs_down = int(_field(res, "subs_downloaded", 0) or 0)
    subs_del = int(_field(res, "subs_deleted", 0) or 0)
    elapsed = _field(res, "elapsed_total", "") or _field(res, "elapsed", "")
    requests = _field(res, "requests", 0)

    changed = any((written, deleted, renamed, dirs_made, dirs_pruned, subs_down, subs_del))

    if errors:
        title = "115 STRM · 这一轮出错了"
        lines = [f"❌ 有 {_n(len(errors))} 处错误，本轮的结果**不完整**。", ""]
        for e in errors[:5]:
            msg = e.get("msg") if isinstance(e, dict) else str(e)
            where = (e.get("where") if isinstance(e, dict) else "") or ""
            lines.append(f"· {where} {msg}".strip())
        if len(errors) > 5:
            lines.append(f"· …还有 {_n(len(errors) - 5)} 处")
    elif changed:
        title = "115 STRM · 有变动"
        lines = [
            f"📊 新增 {_n(written)} · 改 {_n(updated)} · 删 {_n(deleted)} · 改名 {_n(renamed)}",
        ]
        if dirs_made or dirs_pruned:
            lines.append(f"📁 目录 建 {_n(dirs_made)} · 收 {_n(dirs_pruned)}")
        if subs_down or subs_del:
            lines.append(f"💬 字幕 下 {_n(subs_down)} · 删 {_n(subs_del)}")
    else:
        title = "115 STRM · 无变动"
        lines = ["✅ 这轮没有变化 —— 本地已和网盘一致。"]
        stats = local_stats(out_dir)
        if stats:
            lines.append("")
            lines.append("🎬 " + " · ".join(f"{k} {_n(v)} 部" for k, v in stats))

    # ⚠️ 挂起**必须单独说**：它意味着「有 154 条判定要删、但还等着二次确认」——
    #    不说的话，用户看到「无变动」会以为一切正常，而其实有一批正挂着。
    if pending:
        lines.append("")
        lines.append(f"⚠️ {_n(pending)} 条 strm 在「待确认删除」里挂着（到点后下一轮会自动复核）")

    tail = []
    if elapsed:
        tail.append(f"耗时 {elapsed}")
    if requests:
        tail.append(f"请求 {_n(requests)}")
    if tail:
        lines.append("")
        lines.append("⏱ " + " · ".join(tail))

    return title, "\n".join(lines)


def notify_run(cfg, res, log=None) -> list[PushResult]:
    """一轮跑完调这个。总开关关着就什么都不做（返回空）。"""
    if not _on(cfg, "push_enable"):
        return []
    title, content = build_summary(res, getattr(cfg, "output_dir", None))
    return send_all(cfg, title, content, log=log)
