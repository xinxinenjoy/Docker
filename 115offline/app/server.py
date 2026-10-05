"""115 离线推送工具 —— 后端服务

自托管的 115 网盘离线下载推送工具：
  - 多账号 cookie 管理（存本地 JSON，不上传任何第三方）
  - 粘贴磁力 / ed2k / http 链接，自动拆分、去重、修正 32 位磁力 hash
  - 一键批量推送到 115 离线下载

115 的签名与接口细节交给 p115client 处理，本文件只做业务编排。
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from p115client import P115Client, check_response

# ----------------------------------------------------------------- 配置
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(os.environ.get("DATA_DIR") or (BASE_DIR.parent / "data"))
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
ACCESS_TOKEN = (os.environ.get("ACCESS_TOKEN") or "").strip()

DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="115 离线推送", docs_url=None, redoc_url=None)
_store_lock = threading.RLock()


# ----------------------------------------------------------------- 账号存储
def _read_accounts() -> list[dict]:
    with _store_lock:
        if not ACCOUNTS_FILE.exists():
            return []
        try:
            data = json.loads(ACCOUNTS_FILE.read_text("utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []


def _write_accounts(items: list[dict]) -> None:
    with _store_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = ACCOUNTS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(ACCOUNTS_FILE)


def _find_account(account_id: str) -> dict:
    for acc in _read_accounts():
        if acc.get("id") == account_id:
            return acc
    raise HTTPException(404, "账号不存在")


def _public(acc: dict) -> dict:
    """对外暴露的账号信息 —— 绝不返回完整 cookie。"""
    cookie = acc.get("cookie") or ""
    return {
        "id": acc.get("id"),
        "name": acc.get("name"),
        "created": acc.get("created"),
        "checked": acc.get("checked"),
        "nickname": acc.get("nickname") or "",
        "cookie_tail": cookie[-8:] if cookie else "",
    }


def _client(acc: dict) -> P115Client:
    cookie = (acc.get("cookie") or "").strip()
    if not cookie:
        raise HTTPException(400, f"账号「{acc.get('name')}」还没配置 cookie")
    try:
        return P115Client(cookie)
    except Exception as exc:  # cookie 格式不对
        raise HTTPException(400, f"cookie 解析失败：{exc}") from exc


def _probe(client: P115Client) -> dict:
    """校验 cookie 是否可用，尽力拿到昵称。"""
    result: dict[str, Any] = {"ok": False, "nickname": ""}
    for call in (
        lambda: client.user_info(),
        lambda: client.login_status(),
        lambda: client.clouddownload_sign(),
    ):
        try:
            data = check_response(call())
        except Exception:
            continue
        result["ok"] = True
        payload = data.get("data") if isinstance(data, dict) else None
        if isinstance(payload, dict):
            for key in ("user_name", "uname", "nick_name", "nickname", "name"):
                if payload.get(key):
                    result["nickname"] = str(payload[key])
                    break
        break
    return result


# ----------------------------------------------------------------- 链接解析
# 粘贴来源五花八门（网页 / 微信 / 记事本），常夹带零宽字符。它们【不是】\s，
# 会被正则当成链接的一部分 ⇒ 先统一换成空格。
INVISIBLE_RE = re.compile("[\u200b-\u200f\u2060\ufeff\u00ad]")

# ⚠️ 不能用 `magnet:\?[^\s"'<>]+`：那是「吃到空白为止」。多条磁力挨着粘贴时
#    （中间没有换行/空格，或用逗号、顿号、零宽字符相隔）会被吞成一整条，
#    而 BTIH 只取第一个 ⇒ 其余磁力凭空消失（2026-10-05 实测复现）。
#    改为【负向前瞻】：在下一个 `magnet:?` 出现之前停下 —— 与分隔符无关，
#    任何粘法都能正确切开。ed2k 同理（它内部也不会再出现裸 `ed2k://`）。
MAGNET_RE = re.compile(r"magnet:\?(?:(?!magnet:\?)[^\s\"'<>])+", re.I)
ED2K_RE = re.compile(r"ed2k://(?:(?!ed2k://)[^\s\"'<>])+", re.I)
# ⚠️ http 故意【不加】前瞻：URL 的 query 里可能内嵌未编码的 URL
#    （如 ?next=https://…），加了会把一条链接切成两条。保持原样。
HTTP_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
BTIH_RE = re.compile(r"xt=urn:btih:([0-9A-Za-z]{32,40})", re.I)

# 前瞻切分时，两条链接【之间】的分隔符会被算进前一条的尾部 ⇒ 剥掉。
# ⚠️ ed2k 不能剥 `|`：它的合法结尾是 `ed2k://|file|…|/`。
MAGNET_TAIL = ",，、;；|"
ED2K_TAIL = ",，、;；"


def normalize_hash(raw_hash: str) -> str:
    """把磁力链里的 btih 归一化成 40 位 hex。

    32 位是 base32（A-Z2-7）编码，115 侧常识别不了，必须转 40 位 hex。
    """
    h = (raw_hash or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}", h):
        return h.lower()
    if re.fullmatch(r"[A-Za-z2-7]{32}", h):
        try:
            return binascii.hexlify(base64.b32decode(h.upper())).decode()
        except Exception:
            return h.lower()
    return h.lower()


def fix_magnet(magnet: str) -> str:
    """把磁力链中的 32 位 hash 就地替换成 40 位。"""
    m = BTIH_RE.search(magnet)
    if not m:
        return magnet
    old = m.group(1)
    new = normalize_hash(old)
    if new == old:
        return magnet
    return magnet[: m.start(1)] + new + magnet[m.end(1):]


def parse_links(text: str) -> list[dict]:
    """从任意文本中提取链接，去重并标注类型。"""
    if not text:
        return []
    text = INVISIBLE_RE.sub(" ", text)  # 零宽字符归一化，避免粘连
    seen: set[str] = set()
    out: list[dict] = []

    def add(kind: str, raw: str, key: str, fixed: str | None = None) -> None:
        k = key.lower()
        if k in seen:
            return
        seen.add(k)
        out.append({
            "type": kind,
            "raw": raw,
            "url": fixed or raw,
            "key": k,
            "changed": bool(fixed and fixed != raw),
        })

    for m in MAGNET_RE.finditer(text):
        raw = m.group(0).rstrip(MAGNET_TAIL)
        btih = BTIH_RE.search(raw)
        if not btih:
            continue
        add("magnet", raw, "btih:" + normalize_hash(btih.group(1)), fix_magnet(raw))

    for m in ED2K_RE.finditer(text):
        raw = m.group(0).rstrip(ED2K_TAIL)
        add("ed2k", raw, raw)

    for m in HTTP_RE.finditer(text):
        raw = m.group(0).rstrip(".,;)\u3002\uff0c")
        if "115.com" in raw:
            continue
        add("http", raw, raw)

    return out


# ----------------------------------------------------------------- 鉴权
def auth(authorization: str | None = Header(default=None)) -> None:
    """ACCESS_TOKEN 为空则不启用口令（适合内网 / 本机）。"""
    if not ACCESS_TOKEN:
        return
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if token != ACCESS_TOKEN:
        raise HTTPException(401, "访问口令错误")


# ----------------------------------------------------------------- 请求模型
class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    cookie: str = Field(min_length=1)


class PushIn(BaseModel):
    account_id: str
    text: str = ""
    urls: list[str] | None = None
    wp_path_id: str = ""


class ParseIn(BaseModel):
    text: str = ""


# ----------------------------------------------------------------- 页面
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config", dependencies=[Depends(auth)])
def get_config() -> dict:
    return {"need_auth": bool(ACCESS_TOKEN)}


# ----------------------------------------------------------------- 账号
@app.get("/api/accounts", dependencies=[Depends(auth)])
def list_accounts() -> dict:
    return {"items": [_public(a) for a in _read_accounts()]}


@app.post("/api/accounts", dependencies=[Depends(auth)])
def create_account(body: AccountIn) -> dict:
    cookie = body.cookie.strip()
    if "UID=" not in cookie.upper():
        raise HTTPException(400, "cookie 格式不对，至少要包含 UID / CID / SEID")
    accounts = _read_accounts()
    acc = {
        "id": uuid.uuid4().hex[:12],
        "name": body.name.strip(),
        "cookie": cookie,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "checked": None,
        "nickname": "",
    }
    accounts.append(acc)
    _write_accounts(accounts)

    # 顺手校验一次，失败不影响添加（cookie 可能只是过期，用户想先存着）
    try:
        probe = _probe(_client(acc))
        acc["checked"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        acc["nickname"] = probe["nickname"]
        acc["valid"] = probe["ok"]
        _write_accounts(accounts)
    except Exception:
        pass
    return {"ok": True, "item": _public(acc)}


@app.delete("/api/accounts/{account_id}", dependencies=[Depends(auth)])
def delete_account(account_id: str) -> dict:
    accounts = _read_accounts()
    left = [a for a in accounts if a.get("id") != account_id]
    if len(left) == len(accounts):
        raise HTTPException(404, "账号不存在")
    _write_accounts(left)
    return {"ok": True}


@app.post("/api/accounts/{account_id}/check", dependencies=[Depends(auth)])
def check_account(account_id: str) -> dict:
    acc = _find_account(account_id)
    probe = _probe(_client(acc))
    accounts = _read_accounts()
    for item in accounts:
        if item.get("id") == account_id:
            item["checked"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            item["nickname"] = probe["nickname"]
            item["valid"] = probe["ok"]
    _write_accounts(accounts)
    return {"ok": probe["ok"], "nickname": probe["nickname"]}


# ----------------------------------------------------------------- 目录
@app.get("/api/accounts/{account_id}/dirs", dependencies=[Depends(auth)])
def list_dirs(account_id: str, cid: str = "0") -> dict:
    """列出 115 目录（用于选择保存位置）。失败时降级为手填目录 ID。"""
    acc = _find_account(account_id)
    client = _client(acc)
    try:
        data = check_response(client.fs_files({
            "cid": cid, "limit": 500, "offset": 0, "show_dir": 1,
        }))
    except Exception as exc:
        return {"ok": False, "items": [], "error": f"{exc}"}

    items = []
    for node in (data.get("data") or []):
        if not isinstance(node, dict):
            continue
        folder_id = node.get("cid")
        if folder_id is None:
            continue
        items.append({
            "id": str(folder_id),
            "name": node.get("n") or node.get("name") or "(未命名)",
        })
    path = []
    for node in (data.get("path") or []):
        if isinstance(node, dict):
            path.append({"id": str(node.get("cid", 0)), "name": node.get("name") or "根目录"})
    return {"ok": True, "items": items, "path": path}


# ----------------------------------------------------------------- 任务
@app.get("/api/accounts/{account_id}/tasks", dependencies=[Depends(auth)])
def list_tasks(account_id: str, page: int = 1, stat: int = 0) -> dict:
    acc = _find_account(account_id)
    client = _client(acc)
    try:
        data = check_response(client.clouddownload_task_list({
            "page": page, "page_size": 30, "stat": stat,
        }))
    except Exception as exc:
        raise HTTPException(502, f"读取任务失败：{exc}") from exc
    return {"ok": True, "raw": data}


# ----------------------------------------------------------------- 推送
@app.post("/api/parse", dependencies=[Depends(auth)])
def parse(body: ParseIn) -> dict:
    items = parse_links(body.text)
    return {"ok": True, "items": items, "count": len(items)}


@app.post("/api/push", dependencies=[Depends(auth)])
def push(body: PushIn) -> dict:
    acc = _find_account(body.account_id)

    urls: list[str] = []
    if body.text:
        urls += [item["url"] for item in parse_links(body.text)]
    if body.urls:
        urls += [u.strip() for u in body.urls if u and u.strip()]

    # 去重（保序）
    deduped: list[str] = []
    seen: set[str] = set()
    for item in urls:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    if not deduped:
        raise HTTPException(400, "没有解析到可推送的链接")

    client = _client(acc)
    payload: dict[str, Any] = {f"url[{i}]": u for i, u in enumerate(deduped)}
    if body.wp_path_id.strip():
        payload["wp_path_id"] = body.wp_path_id.strip()

    try:
        resp = client.clouddownload_task_add_urls(payload)
    except Exception as exc:
        raise HTTPException(502, f"推送失败：{exc}") from exc

    ok = True
    message = ""
    if isinstance(resp, dict):
        state = resp.get("state")
        ok = state in (True, 1, "1", None)
        message = str(resp.get("error_msg") or resp.get("message") or "")

    return {
        "ok": ok,
        "count": len(deduped),
        "urls": deduped,
        "message": message,
        "raw": resp,
    }


# ----------------------------------------------------------------- 静态
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT") or 8765))
