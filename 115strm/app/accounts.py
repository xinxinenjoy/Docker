"""115 账号管理 —— 读写 + 校验 + 扫码登录。

## 与 115offline 共用同一份账号文件

账号文件格式**刻意与 115offline 完全一致**（`accounts.json`，一个 list），所以
两个容器可以挂同一个文件、互为备份：

    [{"id": "abc123", "name": "主号", "cookie": "UID=…;CID=…;SEID=…",
      "created": "2026-10-08 17:00", "checked": "…", "nickname": "…", "valid": true}, …]

⇒ 在 115offline 里扫码登录一次，115strm 直接能用；反过来也一样。

⚠️ **115strm 不做「扫码登录」以外的写入** —— 它只 追加 / 改名 / 换 cookie / 删除。
   写的时候是**读-改-写整个列表**，不认识的字段（如 115offline 写的
   `default_dir` / `login_app`）**原样保留** —— 否则会把人家记的东西抹掉。

## 为什么要扫码登录

红领巾 2026-10-08：**这个工具以后要分享给别人用**。别人没有 115offline，
让他去 F12 手抓 cookie 门槛太高 ⇒ 内置扫码登录（走 115 公开扫码端点，无需 cookie）。

⚠️ **选端很关键**：同一个端再次扫码会把该端旧登录**挤掉**。
   默认给 `tv`（电视端）—— 最不容易和日常用的网页端 / 手机端撞车。
"""
from __future__ import annotations

import base64
import json
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config

__all__ = [
    "read_accounts", "write_accounts", "find_account", "public", "probe_cookie",
    "make_account", "qr_new", "qr_status", "qr_exchange", "QR_APPS",
]

_LOCK = threading.RLock()

# 扫码端点（公开，不需要 cookie —— 这一步的目的就是拿 cookie）
QR_API = "https://qrcodeapi.115.com"
PASSPORT_API = "https://passportapi.115.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

QR_APPS = {
    "tv": "电视端（推荐，最不易撞端）",
    "web": "网页端（会挤掉你浏览器里的 115 登录）",
    "android": "安卓端",
    "ios": "iOS 端",
    "mac": "Mac 端",
    "linux": "Linux 端",
    "windows": "Windows 端",
}

# 扫码会话（内存）
_QR_STATE: dict[str, dict] = {}
_QR_TTL = 300


# --------------------------------------------------------------------------- 读写
def _atomic_write(path: Path, items: list[dict]) -> None:
    """原子写（tmp + replace）—— 别让另一个容器读到半个文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_accounts(path: Path) -> list[dict]:
    """读账号列表。**只读**，文件不在/坏了都返回空表（⛔ 不抛）。"""
    with _LOCK:
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []


def write_accounts(path: Path, items: list[dict]) -> None:
    with _LOCK:
        _atomic_write(path, items)


def find_account(path: Path, account_id: str) -> dict | None:
    for a in read_accounts(path):
        if str(a.get("id")) == str(account_id):
            return a
    return None


def public(acc: dict) -> dict:
    """对外暴露的信息 —— **绝不返回完整 cookie**。"""
    cookie = str(acc.get("cookie") or "")
    return {
        "id": acc.get("id") or "",
        "name": acc.get("name") or acc.get("id") or "(未命名)",
        "created": acc.get("created") or "",
        "checked": acc.get("checked") or "",
        "nickname": acc.get("nickname") or "",
        "valid": acc.get("valid"),
        "login_app": acc.get("login_app") or "",
        "cookie_tail": ("…" + cookie[-8:]) if cookie else "",
        "usable": bool(cookie) and "UID=" in cookie.upper(),
    }


def looks_like_cookie(text: str) -> bool:
    """粗判一段文本是不是 115 cookie。⛔ 只看有没有 `UID=`，不校签名。"""
    return bool(text) and "UID=" in text.upper()


def make_account(name: str, cookie: str, *, login_app: str = "") -> dict:
    """造一条账号记录（**不自校验**，调用方决定要不要探一次）。"""
    return {
        "id": uuid.uuid4().hex[:12],
        "name": (name or "").strip() or "新账号",
        "cookie": cookie.strip(),
        "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "checked": None,
        "nickname": "",
        "valid": None,
        **({"login_app": login_app} if login_app else {}),
    }


# --------------------------------------------------------------------------- 校验
def probe_cookie(cookie: str, timeout: float = 15) -> dict:
    """校验 cookie 还能不能用，尽力拿昵称。

    ⚠️ 依次试三个接口 —— 115 的接口不是每个都稳定，任何一个通了就算「可用」。
       （照 115offline 的成熟做法，`user_info` → `login_status` → `clouddownload_sign`）
    """
    out: dict[str, Any] = {"ok": False, "nickname": "", "error": ""}
    try:
        from p115client import P115Client, check_response
    except Exception as exc:
        out["error"] = f"没装 p115client：{exc}"
        return out
    try:
        client = P115Client(cookie)
    except Exception as exc:
        out["error"] = f"cookie 解析失败：{exc}"
        return out

    for call in (
        lambda: client.user_info(),
        lambda: client.login_status(),
        lambda: client.clouddownload_sign(),
    ):
        try:
            data = check_response(call())
        except Exception as exc:
            out["error"] = str(exc)[:200]
            continue
        out["ok"] = True
        out["error"] = ""
        payload = data.get("data") if isinstance(data, dict) else None
        if isinstance(payload, dict):
            for key in ("user_name", "uname", "nick_name", "nickname", "name"):
                if payload.get(key):
                    out["nickname"] = str(payload[key])
                    break
        break
    return out


def touch(path: Path, account_id: str, probe: dict) -> None:
    """把校验结果写回账号记录（保留其它字段）。"""
    with _LOCK:
        items = read_accounts(path)
        for it in items:
            if str(it.get("id")) == str(account_id):
                it["checked"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                it["nickname"] = probe.get("nickname") or it.get("nickname") or ""
                it["valid"] = bool(probe.get("ok"))
        _atomic_write(path, items)


# --------------------------------------------------------------------------- HTTP
def _http_json(url: str, data: dict | None = None, timeout: float = 12) -> Any:
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _http_bytes(url: str, timeout: float = 12) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# --------------------------------------------------------------------------- 扫码
def _qr_cleanup() -> None:
    now = time.time()
    for k in [k for k, v in _QR_STATE.items() if now - v.get("created", 0) > _QR_TTL]:
        _QR_STATE.pop(k, None)


def qr_new() -> dict:
    """生成登录二维码（返回 base64 PNG，前端直接内联显示）。"""
    _qr_cleanup()
    resp = _http_json(f"{QR_API}/api/1.0/web/1.0/token/")
    data = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(data, dict) or not data.get("uid"):
        raise RuntimeError(f"二维码接口返回异常：{resp}")
    uid = str(data["uid"])
    with _LOCK:
        _QR_STATE[uid] = {"time": data.get("time"), "sign": data.get("sign"),
                          "created": time.time()}
    image = ""
    try:
        png = _http_bytes(f"{QR_API}/api/1.0/mac/1.0/qrcode?uid={uid}")
        image = "data:image/png;base64," + base64.b64encode(png).decode()
    except Exception:
        image = ""
    return {"uid": uid, "image": image, "qrcode": data.get("qrcode") or "",
            "apps": [{"key": k, "label": v} for k, v in QR_APPS.items()]}


def qr_status(uid: str) -> dict:
    """轮询扫码状态：0 待扫 / 1 已扫待确认 / 2 已确认 / -1 过期 / -2 取消。"""
    with _LOCK:
        state = _QR_STATE.get(uid)
    if not state:
        raise RuntimeError("二维码会话已失效，请重新生成")
    resp = _http_json(f"{QR_API}/get/status/?" + urllib.parse.urlencode(
        {"uid": uid, "time": state["time"], "sign": state["sign"]}))
    data = resp.get("data") if isinstance(resp, dict) else None
    status = data.get("status") if isinstance(data, dict) else None
    return {
        "status": status,
        "message": (data or {}).get("msg") or "",
        "label": {0: "等待扫码", 1: "已扫描，请在手机上确认", 2: "已确认"}.get(status, ""),
    }


def qr_exchange(uid: str, app: str = "tv") -> dict:
    """扫码确认后换取 cookie。"""
    app_name = app if app in QR_APPS else "tv"
    with _LOCK:
        if uid not in _QR_STATE:
            raise RuntimeError("二维码会话已失效，请重新生成")
    resp = _http_json(f"{PASSPORT_API}/app/1.0/{app_name}/1.0/login/qrcode/",
                      {"app": app_name, "account": uid})
    data = resp.get("data") if isinstance(resp, dict) else None
    cookies = (data or {}).get("cookie") if isinstance(data, dict) else None
    if not isinstance(cookies, dict) or not cookies.get("UID"):
        raise RuntimeError(f"登录返回异常：{resp}")
    cookie = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
    with _LOCK:
        _QR_STATE.pop(uid, None)
    return {"cookie": cookie, "app": app_name}


def accounts_file(cfg: Config) -> Path:
    return Path(cfg.accounts_file)
