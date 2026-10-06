"""115 离线推送工具 —— 后端服务

自托管的 115 网盘离线下载推送工具：
  - 多账号管理（cookie / 扫码两种登录，存本地 JSON，不上传任何第三方）
  - 粘贴磁力 / ed2k / http 链接，**以及中文暗号**（百家姓 / 核心价值观 / 佛曰）
  - 目录多级下探选择保存位置，可设为本账号默认
  - 一键批量推送到 115 离线下载
  - 用量与空间展示（离线配额 / 网盘容量）
  - 对已完成的离线任务做「整理」：浏览目录 → 预览改名 → 确认后执行（可改回）
  - **手动多选删除**（进回收站，可还原）+ 回收站列表 / 还原

115 的签名与接口细节交给 p115client 处理，本文件只做业务编排。
暗号还原见 cipher.py，命名清洗见 namer.py。

⚠️ 所有批量写操作（改名 / 删除 / 还原）都走 `_fs_write_slot` 全局单飞 + 分块 + 块间延时：
   115 明确「删除请不要并发执行」「删除和还原互斥」，且用户要求别并发太快以免风控。
"""
from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from p115client import P115Client, check_response

# 链接 / 暗号解析是纯标准库逻辑，单独放在 links.py，便于脱离框架做回归测试。
# normalize_hash / fix_magnet 在这里再导出一次，保持既有引用不失效。
from links import MAX_BATCH, fix_magnet, normalize_hash, parse_links  # noqa: F401
from namer import (
    clean_file,
    ensure_ext,
    is_episode_file,
    looks_like_ad,
    parse_media,
    sanitize_name,
    suggest,
    suggest_episode,
    suggest_file,
    title_of,
)

# 推送记录（「本体推送」账本）是纯标准库逻辑，单独放在 pushlog.py。
from pushlog import MAX_RECORDS as PUSHLOG_MAX
from pushlog import PushLog

# ----------------------------------------------------------------- 配置
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(os.environ.get("DATA_DIR") or (BASE_DIR.parent / "data"))
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
ACCESS_TOKEN = (os.environ.get("ACCESS_TOKEN") or "").strip()

DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="115 离线推送", docs_url=None, redoc_url=None)
_store_lock = threading.RLock()

# 批量写操作（删除 / 还原 / 改名）的**全局单飞锁**。
# ⚠️ 不是保守起见，是 115 的硬要求 —— p115client 对 fs_delete 的文档原文：
#    「请不要并发执行，但不限制文件数」
#    「删除和（从回收站）还原是互斥的，同时最多只允许执行一个操作」
# 配合红领巾 2026-10-05 的要求（批量别并发太快，避免风控），批量写一律排队跑。
_FS_WRITE_LOCK = threading.Lock()


@contextlib.contextmanager
def _fs_write_slot(action: str):
    """抢不到就 409，不排队等待 —— 让用户知道「有别的批量任务在跑」，而不是静默卡住。"""
    if not _FS_WRITE_LOCK.acquire(blocking=False):
        raise HTTPException(
            409, f"已有批量操作在跑（115 不允许并发删除/还原），请等它结束再{action}"
        )
    try:
        yield
    finally:
        _FS_WRITE_LOCK.release()


def _chunks(seq: list, size: int) -> list[list]:
    """把列表切成小块 —— 批量写分块执行，块间 sleep。"""
    size = max(1, min(int(size), 200))
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def _size_text(n: Any) -> str:
    """字节数 → 人类可读。传字符串也认（回收站返回的 file_size 就是字符串）。"""
    try:
        size = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024 or unit == "PB":
            return ("%.0f %s" % (size, unit)) if unit == "B" else ("%.2f %s" % (size, unit))
        size /= 1024
    return ""


# ------------------------------------------------- 目录列表缓存（内存热层 + 落盘持久层）
# 红领巾 2026-10-05：**不要每次点开都扫一遍**，加「扫描」按钮把当前这一层的结果
# 存到容器里复用。所以：
#   · 只缓存**当前这一层**的返回，**绝不递归拉整棵树**（递归才是真风控 —— 257 个子
#     目录 × 每层一次请求）。层级由用户一层层点出来，每层各缓存一条。
#   · TTL 默认 300 秒（`FOLDER_CACHE_TTL` 可调，设 0 = 永不过期、只能手动刷新）。
#   · 任何**写操作**（改名 / 删除 / 还原）后立刻失效该账号的缓存 —— 否则会基于旧
#     列表误操作，这是缓存必须付的代价。
#
# 🔴 2026-10-06 红领巾问「缓存是存浏览器还是 docker 目录里」⇒ 答案与改法：
#    旧版只存**容器内存**（进程字典）⇒ 容器重建/重启就全丢，重新一层层扫。
#    现在加**落盘持久层**：`DATA_DIR/folder_cache.json`（与 accounts.json 同目录，
#    已经挂到宿主机）⇒ 容器重建不丢。读永远是内存优先（快），写才同步刷盘；
#    启动时把盘里的过期条目清掉再加载回内存。
_FOLDER_TTL = float(os.environ.get("FOLDER_CACHE_TTL") or 300)
_FOLDER_CACHE_MAX = 600
_FOLDER_CACHE_FILE = DATA_DIR / "folder_cache.json"
_folder_cache: dict[tuple, dict] = {}
_folder_cache_lock = threading.RLock()


def _cache_key_str(key: tuple) -> str:
    """tuple 键 → JSON 对象的 key（tuple 不能直接进 JSON）。"""
    return "|".join(str(x) for x in key)


def _key_str_to_cache_key(s: str) -> tuple:
    parts = s.split("|")
    # 键结构固定 6 段：account_id|kind|cid|offset|limit|mode
    try:
        return (parts[0], parts[1], parts[2], int(parts[3]), int(parts[4]), parts[5])
    except (IndexError, ValueError):
        return (s,)  # 认不出的旧结构 ⇒ 原样包一层，等 TTL 自然过期


def _cache_load_disk() -> None:
    """启动时从盘里恢复缓存（过期的直接丢）。失败不致命 —— 大不了重新扫。"""
    global _folder_cache
    if not _FOLDER_CACHE_FILE.exists():
        return
    try:
        raw = json.loads(_FOLDER_CACHE_FILE.read_text("utf-8"))
        now = time.time()
        loaded: dict[tuple, dict] = {}
        for k, rec in (raw or {}).items():
            try:
                at = float(rec.get("at") or 0)
            except (TypeError, ValueError):
                continue
            if _FOLDER_TTL > 0 and now - at > _FOLDER_TTL:
                continue  # 已过期，不恢复
            ck = _key_str_to_cache_key(k)
            if len(ck) == 6:
                loaded[ck] = {"at": at, "payload": rec.get("payload") or {}, "hits": 0}
        _folder_cache = loaded
        logging.info("folder cache: restored %d entries from %s", len(loaded), _FOLDER_CACHE_FILE)
    except Exception as exc:  # 缓存坏了不能影响服务启动
        logging.warning("folder cache restore failed: %s", exc)


def _cache_save_disk_locked() -> None:
    """把内存缓存刷到盘。⚠️ 调用方必须已持有 `_folder_cache_lock`。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _FOLDER_CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {_cache_key_str(k): {"at": v["at"], "payload": v["payload"]}
                 for k, v in _folder_cache.items()},
                ensure_ascii=False,
            ),
            "utf-8",
        )
        tmp.replace(_FOLDER_CACHE_FILE)
    except Exception as exc:
        logging.warning("folder cache save failed: %s", exc)


def _cache_key(account_id: str, kind: str, cid: str, offset: int, limit: int,
               mode: str = "") -> tuple:
    """缓存键。

    ⚠️ `mode`（`dirs` = 只列目录 / `files` = 目录+文件）必须进键：
       同一层在两种视图下返回的**条数都不一样**（`nf=1` 时 115 只回目录），
       共用一份缓存会串味 —— 点「显示文件」却拿到只有目录的旧结果。
    """
    return (str(account_id), kind, str(cid), int(offset), int(limit), str(mode or ""))


def _cache_get(key: tuple):
    now = time.time()
    with _folder_cache_lock:
        rec = _folder_cache.get(key)
        if not rec:
            return None
        if _FOLDER_TTL > 0 and now - rec["at"] > _FOLDER_TTL:
            _folder_cache.pop(key, None)
            return None
        rec["hits"] = rec.get("hits", 0) + 1
        return rec["payload"], rec["at"]


def _cache_put(key: tuple, payload: dict) -> None:
    with _folder_cache_lock:
        _folder_cache[key] = {"at": time.time(), "payload": payload, "hits": 0}
        if len(_folder_cache) > _FOLDER_CACHE_MAX:
            stale = sorted(_folder_cache.items(), key=lambda kv: kv[1]["at"])
            for k, _ in stale[: len(_folder_cache) - _FOLDER_CACHE_MAX]:
                _folder_cache.pop(k, None)
        _cache_save_disk_locked()


def _cache_drop(account_id: str | None = None) -> int:
    """写操作后失效。整个账号一起清 —— 改名/删除会连带影响父子目录的 `fc` 计数。"""
    with _folder_cache_lock:
        keys = [k for k in _folder_cache if account_id is None or k[0] == str(account_id)]
        for k in keys:
            _folder_cache.pop(k, None)
        if keys:
            _cache_save_disk_locked()
    return len(keys)


def _cache_info(account_id: str | None = None) -> dict:
    with _folder_cache_lock:
        rows = [v for k, v in _folder_cache.items() if account_id is None or k[0] == str(account_id)]
    return {
        "entries": len(rows),
        "ttl": _FOLDER_TTL,
        "hits": sum(r.get("hits", 0) for r in rows),
        "newest": _ts_text(max((r["at"] for r in rows), default=0)) if rows else "",
    }


def _ts_text(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else ""


# ----------------------------------------------------------------- 推送记录（本体推送）
# 红领巾 2026-10-06：「在离线里边增加一个本体推送的记录方便查看」。
# 115 的离线任务列表只反映**它那边**的最终状态 —— 看不出「推的时候挑的哪个目录」
# 「有没有被 115 挡回来 / 被本地去重吃掉」。所以本地自己也记一份账：
#   落盘 `DATA_DIR/pushlog.json`（原子写、容器重建不丢），上限 1000 条、最新在前。
#   ⛔ 纯本地账本，不参与任何 115 请求；记账失败只打日志，绝不影响推送本身。
_PUSHLOG = PushLog(DATA_DIR / "pushlog.json", PUSHLOG_MAX)


def _served(kind: str, account_id: str, cid: str, offset: int, limit: int,
            refresh: bool, producer, mode: str = "") -> dict:
    """缓存优先：命中且没过期就直接给；否则现拉一次并写进缓存。"""
    key = _cache_key(account_id, kind, cid, offset, limit, mode)
    if not refresh:
        hit = _cache_get(key)
        if hit:
            payload, at = hit
            out = dict(payload)
            out.update({
                "from_cache": True,
                "cached_at": _ts_text(at),
                "cache_age": round(time.time() - at, 1),
            })
            return out
    payload = producer()
    if payload.get("ok"):
        _cache_put(key, payload)
    out = dict(payload)
    out.update({
        "from_cache": False,
        "cached_at": _ts_text(time.time()) if payload.get("ok") else "",
        "cache_age": 0,
    })
    return out


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
        "cookie_len": len(cookie),
        "default_dir": acc.get("default_dir") or "",
        "default_dir_name": acc.get("default_dir_name") or "",
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


def _touch_account(account_id: str, probe: dict) -> None:
    accounts = _read_accounts()
    for item in accounts:
        if item.get("id") == account_id:
            item["checked"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            item["nickname"] = probe.get("nickname") or item.get("nickname") or ""
            item["valid"] = bool(probe.get("ok"))
    _write_accounts(accounts)


# ----------------------------------------------------------------- 链接解析
# 实现见 links.py（纯标准库 + cipher.py）。这里只做一次转发说明，避免两处维护同一套正则。

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


# ----------------------------------------------------------------- 扫码登录
# 直接用 115 的公开扫码端点（无需 cookie —— 这一步的目的就是拿 cookie）。
# 走标准库 urllib，不依赖 p115client 的内部签名逻辑。
QR_API = "https://qrcodeapi.115.com"
PASSPORT_API = "https://passportapi.115.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
# ⚠️ 选哪个端很关键：同一个端再次扫码会把该端旧登录挤掉。
#    默认给 tv（电视端），最不容易和日常用的网页端/手机端撞车。
QR_APPS = {
    "tv": "电视端（推荐，最不易撞端）",
    "web": "网页端（会挤掉你浏览器里的 115 登录）",
    "android": "安卓端",
    "ios": "iOS 端",
    "mac": "Mac 端",
    "windows": "Windows 端",
    "linux": "Linux 端",
}
_QR_LOCK = threading.RLock()
_QR_STATE: dict[str, dict] = {}


def _http_json(url: str, data: dict | None = None, timeout: float = 12) -> dict:
    if data is None:
        req = Request(url, headers={"User-Agent": UA})
    else:
        req = Request(
            url,
            data=urlencode(data).encode("utf-8"),
            headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 —— 域名是写死的
        return json.loads(resp.read().decode("utf-8", "replace"))


def _http_bytes(url: str, timeout: float = 12) -> bytes:
    with urlopen(Request(url, headers={"User-Agent": UA}), timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def _qr_cleanup() -> None:
    """清掉 10 分钟前的扫码会话（二维码本身几分钟就过期）。"""
    now = time.time()
    with _QR_LOCK:
        for uid in [u for u, v in _QR_STATE.items() if now - v["created"] > 600]:
            _QR_STATE.pop(uid, None)


# ----------------------------------------------------------------- 请求模型
class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    cookie: str = Field(min_length=1)


class AccountEditIn(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    cookie: str | None = None


class DefaultDirIn(BaseModel):
    dir_id: str = ""
    dir_name: str = ""


class PushIn(BaseModel):
    account_id: str
    text: str = ""
    urls: list[str] | None = None
    wp_path_id: str = ""
    # 前端选中的目录名（只用于本地推送记录里回显「存到哪」）——
    # 115 的推送接口只认目录 id，名字它不回，所以让前端顺手带一个。
    dir_name: str = ""


class ParseIn(BaseModel):
    text: str = ""


class QrResultIn(BaseModel):
    uid: str
    app: str = "tv"
    name: str = ""


class RenamePreviewIn(BaseModel):
    account_id: str
    file_id: str
    name: str = ""
    kind: str = "auto"  # auto | dir | file


class RenameItem(BaseModel):
    file_id: str
    new_name: str
    ext: str | None = None


class RenameApplyIn(BaseModel):
    account_id: str
    items: list[RenameItem]
    chunk: int = Field(default=50, ge=1, le=200)
    delay: float = Field(default=0.6, ge=0, le=30)


class SuggestItem(BaseModel):
    name: str
    is_dir: bool = True
    series: bool = False            # 目录：按「系列」给候选（小黄人（系列））
    add_year: str | None = None     # 原名无年份时的兜底（用任务的添加年份）
    title: str | None = None        # 人工指定片名，跳过自动抽取
    series_title: str | None = None  # 分集文件：用父目录片名替换前缀


class SuggestIn(BaseModel):
    items: list[SuggestItem]
    paren: str = "full"             # full=全角（） / half=半角()


class DeleteIn(BaseModel):
    account_id: str
    file_ids: list[str]
    chunk: int = Field(default=20, ge=1, le=100)
    delay: float = Field(default=1.2, ge=0, le=30)
    ignore_warn: bool = True        # 115 对「正在分享中」等会要一次确认，这里直接确认
    dry_run: bool = False           # 只回执「将要删什么、分几批、大概多久」，不动数据


class RevertIn(BaseModel):
    account_id: str
    file_ids: list[str]
    chunk: int = Field(default=20, ge=1, le=100)
    delay: float = Field(default=1.2, ge=0, le=30)


class ScanIn(BaseModel):
    """扫描（把当前这一层的目录列表存进容器缓存）。"""
    cid: str = "0"
    limit: int = Field(default=200, ge=1, le=200)
    # `dirs` = 只要目录（115 端 `nf=1`，文件不拉回来）；默认 `files` = 目录 + 文件
    mode: str = "files"


# ----------------------------------------------------------------- 页面
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config", dependencies=[Depends(auth)])
def get_config() -> dict:
    return {
        "need_auth": bool(ACCESS_TOKEN),
        "max_batch": MAX_BATCH,
        "qr_apps": QR_APPS,
        "folder_cache_ttl": _FOLDER_TTL,
    }


# ----------------------------------------------------------------- 扫描 / 缓存
@app.post("/api/accounts/{account_id}/scan", dependencies=[Depends(auth)])
def scan_folder(account_id: str, body: ScanIn) -> dict:
    """**扫描当前这一层**，把结果存进容器缓存，顺手把这一层有多少目录/文件报回去。

    🔴 红线：**只扫当前层，绝不递归拉整棵树**。递归才是真的风控风险
    （`115电影` 257 个子目录 × 每层一次请求）。要看下一层就点进去再扫一次。
    红领巾 2026-10-05 的原话就是「扫描只针对当前所处文件夹」。
    """
    started = time.time()
    mode = "dirs" if body.mode == "dirs" else "files"
    payload = _served("folder", account_id, body.cid, 0, body.limit, True,
                      lambda: _browse_folder_impl(account_id, body.cid, body.limit, 0, mode),
                      mode=mode)
    items = payload.get("items") or []
    dirs = sum(1 for x in items if x.get("is_dir"))
    return {
        **payload,
        "scanned": bool(payload.get("ok")),
        "dirs": dirs,
        "files": len(items) - dirs,
        "elapsed": round(time.time() - started, 2),
    }


@app.get("/api/accounts/{account_id}/cache", dependencies=[Depends(auth)])
def get_cache_info(account_id: str) -> dict:
    """看看容器里缓存了哪些层（只报数量与最新时间，不吐内容）。"""
    _find_account(account_id)
    return {"ok": True, **_cache_info(account_id)}


@app.delete("/api/accounts/{account_id}/cache", dependencies=[Depends(auth)])
def clear_cache(account_id: str) -> dict:
    """手动清缓存（界面上的「清缓存」）。"""
    _find_account(account_id)
    return {"ok": True, "cleared": _cache_drop(account_id)}


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


@app.put("/api/accounts/{account_id}", dependencies=[Depends(auth)])
def edit_account(account_id: str, body: AccountEditIn) -> dict:
    """编辑账号：改名 / 换 cookie（换 cookie 不必再删了重加）。"""
    accounts = _read_accounts()
    target = None
    for acc in accounts:
        if acc.get("id") == account_id:
            target = acc
    if target is None:
        raise HTTPException(404, "账号不存在")

    if body.name is not None and body.name.strip():
        target["name"] = body.name.strip()
    if body.cookie is not None and body.cookie.strip():
        cookie = body.cookie.strip()
        if "UID=" not in cookie.upper():
            raise HTTPException(400, "cookie 格式不对，至少要包含 UID / CID / SEID")
        target["cookie"] = cookie
        target["checked"] = None
        target["nickname"] = ""
    _write_accounts(accounts)

    if body.cookie is not None and body.cookie.strip():
        try:
            probe = _probe(_client(target))
            _touch_account(account_id, probe)
        except Exception:
            pass
    return {"ok": True, "item": _public(_find_account(account_id))}


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
    _touch_account(account_id, probe)
    return {"ok": probe["ok"], "nickname": probe["nickname"]}


@app.post("/api/accounts/{account_id}/default-dir", dependencies=[Depends(auth)])
def set_default_dir(account_id: str, body: DefaultDirIn) -> dict:
    """记住本账号的默认保存目录（推送时不再每次手选）。"""
    accounts = _read_accounts()
    hit = False
    for acc in accounts:
        if acc.get("id") == account_id:
            acc["default_dir"] = body.dir_id.strip()
            acc["default_dir_name"] = body.dir_name.strip()
            hit = True
    if not hit:
        raise HTTPException(404, "账号不存在")
    _write_accounts(accounts)
    return {"ok": True, "default_dir": body.dir_id.strip(), "default_dir_name": body.dir_name.strip()}


# ----------------------------------------------------------------- 用量 / 空间
@app.get("/api/accounts/{account_id}/usage", dependencies=[Depends(auth)])
def usage(account_id: str) -> dict:
    """离线配额 + 网盘空间 + 账号信息。取不到的项留 None，前端自动隐藏。

    ⚠️ 字段名是 2026-10-05 在真实账号上实测出来的，不是猜的：
       - 离线配额：clouddownload_quota_info() -> {state, quota, total}
         `quota` = **剩余**次数、`total` = 总配额（与 115 界面口径一致）
       - 网盘空间：user_space_info() -> data.all_total / all_use / all_remain
         每项含 {size: 字节, size_format: 人类可读}
    """
    acc = _find_account(account_id)
    client = _client(acc)
    out: dict[str, Any] = {
        "ok": True,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "offline": None,
        "space": None,
        "user": None,
        "errors": [],
    }

    try:
        data = check_response(client.clouddownload_quota_info())
        remain = data.get("quota")
        total = data.get("total")
        if remain is not None and total is not None:
            out["offline"] = {
                "remain": int(remain),
                "total": int(total),
                "used": max(int(total) - int(remain), 0),
            }
    except Exception as exc:
        out["errors"].append("离线配额：%s" % exc)

    try:
        data = check_response(client.user_space_info())
        info = (data.get("data") or {}) if isinstance(data, dict) else {}
        total = info.get("all_total") or {}
        used = info.get("all_use") or {}
        remain = info.get("all_remain") or {}
        if total.get("size"):
            out["space"] = {
                "total": total.get("size_format"),
                "used": used.get("size_format"),
                "remain": remain.get("size_format"),
                "total_size": total.get("size"),
                "used_size": used.get("size"),
                "remain_size": remain.get("size"),
                "percent": round(float(used.get("size") or 0) / float(total.get("size") or 1) * 100, 2),
            }
    except Exception as exc:
        out["errors"].append("网盘空间：%s" % exc)

    try:
        data = check_response(client.user_info())
        info = (data.get("data") or {}) if isinstance(data, dict) else {}
        if info:
            out["user"] = {
                "name": info.get("user_name") or acc.get("name"),
                "uid": str(info.get("user_id") or ""),
                "vip": info.get("is_vip"),
                "face": info.get("user_face") or "",
            }
    except Exception as exc:
        out["errors"].append("账号信息：%s" % exc)

    return out


# ----------------------------------------------------------------- 扫码
@app.post("/api/qrcode/token", dependencies=[Depends(auth)])
def qrcode_token() -> dict:
    """生成登录二维码（返回 base64 PNG，直接内联显示）。"""
    _qr_cleanup()
    try:
        resp = _http_json(f"{QR_API}/api/1.0/web/1.0/token/")
    except Exception as exc:
        raise HTTPException(502, f"取二维码失败：{exc}") from exc

    data = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(data, dict) or not data.get("uid"):
        raise HTTPException(502, f"二维码接口返回异常：{resp}")

    uid = str(data["uid"])
    with _QR_LOCK:
        _QR_STATE[uid] = {
            "time": data.get("time"),
            "sign": data.get("sign"),
            "created": time.time(),
        }

    image = ""
    try:
        png = _http_bytes(f"{QR_API}/api/1.0/mac/1.0/qrcode?uid={uid}")
        image = "data:image/png;base64," + base64.b64encode(png).decode()
    except Exception:
        image = ""

    return {"ok": True, "uid": uid, "image": image, "qrcode": data.get("qrcode") or ""}


@app.get("/api/qrcode/status", dependencies=[Depends(auth)])
def qrcode_status(uid: str = Query(...)) -> dict:
    """轮询扫码状态：0 待扫 / 1 已扫待确认 / 2 已确认 / -1 过期 / -2 取消。"""
    with _QR_LOCK:
        state = _QR_STATE.get(uid)
    if not state:
        raise HTTPException(404, "二维码会话已失效，请重新生成")

    payload = {"uid": uid, "time": state["time"], "sign": state["sign"]}
    try:
        resp = _http_json(f"{QR_API}/get/status/?{urlencode(payload)}")
    except Exception as exc:
        raise HTTPException(502, f"查询扫码状态失败：{exc}") from exc

    data = resp.get("data") if isinstance(resp, dict) else None
    status = data.get("status") if isinstance(data, dict) else None
    return {
        "ok": True,
        "status": status,
        "message": (data or {}).get("msg") or "",
        "label": {0: "等待扫码", 1: "已扫描，请在手机上确认", 2: "已确认"}.get(status, ""),
    }


@app.post("/api/qrcode/result", dependencies=[Depends(auth)])
def qrcode_result(body: QrResultIn) -> dict:
    """扫码确认后换取 cookie 并保存为账号。"""
    app_name = body.app if body.app in QR_APPS else "tv"
    with _QR_LOCK:
        if body.uid not in _QR_STATE:
            raise HTTPException(404, "二维码会话已失效，请重新生成")

    try:
        resp = _http_json(
            f"{PASSPORT_API}/app/1.0/{app_name}/1.0/login/qrcode/",
            {"app": app_name, "account": body.uid},
        )
    except Exception as exc:
        raise HTTPException(502, f"换取登录凭据失败：{exc}") from exc

    data = resp.get("data") if isinstance(resp, dict) else None
    cookies = (data or {}).get("cookie") if isinstance(data, dict) else None
    if not isinstance(cookies, dict) or not cookies.get("UID"):
        raise HTTPException(502, f"登录返回异常：{resp}")

    cookie = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
    accounts = _read_accounts()
    acc = {
        "id": uuid.uuid4().hex[:12],
        "name": (body.name or "").strip() or f"{app_name} 扫码登录",
        "cookie": cookie,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "checked": None,
        "nickname": "",
        "login_app": app_name,
    }
    try:
        probe = _probe(_client(acc))
        acc["checked"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        acc["nickname"] = probe["nickname"]
        acc["valid"] = probe["ok"]
    except Exception:
        pass
    accounts.append(acc)
    _write_accounts(accounts)

    with _QR_LOCK:
        _QR_STATE.pop(body.uid, None)
    return {"ok": True, "item": _public(acc), "nickname": acc.get("nickname") or ""}


# ----------------------------------------------------------------- 目录
def _is_folder(node: dict) -> bool:
    """⚠️ 115 的**文件**节点也带 `cid`（那是它的父目录 id）⇒ 不能拿 cid 判目录。
    判据是：目录没有 `fid`，文件有 `fid`（2026-10-05 实测两边的键集合确认）。"""
    return not node.get("fid")


@app.get("/api/accounts/{account_id}/dirs", dependencies=[Depends(auth)])
def list_dirs(account_id: str, cid: str = "0", offset: int = 0, limit: int = 200,
              refresh: int = 0) -> dict:
    """列出 115 某个目录下的**子目录**（供多级下探）。

    `cid=0` 为根。返回 items（子目录）+ path（面包屑）+ 分页游标。

    ⚠️ 分页是**必须**的，不是优化：`fs_files` 单次最多回 200 条（传 500 也只给 200，
    2026-10-05 实测 `115电影` 有 257 个子目录、只回 200）⇒ 不翻页会**静默丢目录**。
    翻页口径用 `next_offset`（= 本次 offset + 本次**原始**返回条数），因为返回里
    目录/文件是混着的、过滤发生在本地，拿过滤后的条数累加会漏页。

    `refresh=1` 绕过缓存强制重拉（界面上的「扫描 / 刷新」按这个来）。
    """
    return _served("dirs", account_id, cid, offset, limit, bool(refresh),
                   lambda: _list_dirs_impl(account_id, cid, offset, limit))


def _list_dirs_impl(account_id: str, cid: str, offset: int, limit: int) -> dict:
    acc = _find_account(account_id)
    client = _client(acc)
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    try:
        data = check_response(client.fs_files({
            "cid": cid, "limit": limit, "offset": offset, "show_dir": 1,
        }))
    except Exception as exc:
        return {"ok": False, "items": [], "path": [], "error": f"{exc}"}

    raw = [n for n in (data.get("data") or []) if isinstance(n, dict)]
    items = []
    for node in raw:
        if not _is_folder(node):
            continue
        items.append({
            "id": str(node.get("cid")),
            "name": node.get("n") or node.get("name") or "(未命名)",
            "count": node.get("fc") or 0,
        })
    items.sort(key=lambda x: x["name"])

    path = []
    for node in (data.get("path") or []):
        if isinstance(node, dict):
            path.append({
                "id": str(node.get("cid", 0)),
                "name": node.get("name") or node.get("n") or "根目录",
            })
    if not path:
        path = [{"id": "0", "name": "根目录"}]
    # 同 browse_folder：cid 无效时 115 静默回落到根目录，靠 path 反查
    warning = ""
    if str(cid) not in ("", "0") and len(path) <= 1:
        warning = "这个 id 不是有效目录（115 会静默回落到根目录）"

    next_offset = offset + len(raw)
    total = data.get("count")
    has_more = bool(raw) and total is not None and next_offset < int(total)
    return {
        "ok": True, "items": items, "path": path, "cid": str(cid),
        "count": len(items), "total": total,
        "offset": offset, "next_offset": next_offset, "has_more": has_more,
        "warning": warning,
    }


@app.get("/api/accounts/{account_id}/folder", dependencies=[Depends(auth)])
def browse_folder(account_id: str, cid: str = "0", limit: int = 200, offset: int = 0,
                  refresh: int = 0, mode: str = "files") -> dict:
    """列出某目录下的内容（浏览 / 整理面板用）。

    `mode`（红领巾 2026-10-05：原意是「避免每次打开某个目录都拉取内部的所有文件」）
      · `files`（默认）= 目录 + 文件，走 `show_dir=1`
      · `dirs`         = **只要目录**，走 115 的 `nf=1`（服务端过滤，文件**根本没被拉回来**）
    ⚠️ `nf=1` 是 115 自己的参数（p115client 文档：`nf` 「不要显示文件（即仅显示目录）」）
       ⇒ 这不是「拉回来再丢掉」，而是**真的少拉一半数据**。
       两种 mode 各自独立缓存（见 `_cache_key` 的 `mode` 维度）。

    走容器缓存：命中且没过期就直接给（`from_cache=true`），`refresh=1` 强制重拉。
    缓存策略与理由见上方「目录列表缓存」小节。

    ⚠️ 两个实测出来的坑：
      1. `fs_files` 的文档自己标了「**此接口被风控**，此域名下的大量接口都会被风控」
         ⇒ 只在用户点开某个目录时调用，绝不做轮询/预加载；缓存也正是为此。
      2. 传进去的 cid 若不是有效目录，115 **不报错**，而是静默当成根目录处理
         （文档原文：「如果不指定或者指定的 cid 不存在，则会视为 cid=0 进行处理」）
         ⇒ 用返回的 path 反查：**非根目录请求却只回「根目录」一条** = cid 无效。
         实测踩过 —— 有个任务的 file_id 就是这么「看起来正常、其实列的是根目录」。
    """
    return _served("folder", account_id, cid, offset, limit, bool(refresh),
                   lambda: _browse_folder_impl(account_id, cid, limit, offset, mode),
                   mode=("dirs" if mode == "dirs" else "files"))


def _browse_folder_impl(account_id: str, cid: str, limit: int, offset: int,
                        mode: str = "files") -> dict:
    acc = _find_account(account_id)
    client = _client(acc)
    limit = max(1, min(int(limit), 200))
    dirs_only = mode == "dirs"
    try:
        data = check_response(client.fs_files({
            "cid": cid, "limit": limit, "offset": int(offset), "show_dir": 1,
            # ⚠️ `nf=1` = 115 原生的「不要显示文件（仅显示目录）」⇒ 文件根本不会被拉回来。
            #    这是本工具「别一打开目录就拉全部文件」的正解（服务端过滤，不是拉回来再丢）。
            **({"nf": 1} if dirs_only else {}),
        }))
    except Exception as exc:
        raise HTTPException(502, f"读取目录失败：{exc}") from exc

    path: list[dict] = []
    for node in (data.get("path") or []):
        if isinstance(node, dict):
            path.append({
                "id": str(node.get("cid", 0)),
                "name": node.get("name") or node.get("n") or "根目录",
            })
    if not path:
        path = [{"id": "0", "name": "根目录"}]

    warning = ""
    if str(cid) not in ("", "0") and len(path) <= 1:
        warning = "这个 id 不是有效目录（115 会静默回落到根目录）—— 可能已被删除或移动"

    items: list[dict] = []
    for node in (data.get("data") or []):
        if not isinstance(node, dict):
            continue
        is_dir = _is_folder(node)
        items.append({
            # 目录的自有 id 在 cid，文件在 fid —— 都取出来拼成统一的 file_id
            "file_id": str(node.get("cid") if is_dir else node.get("fid") or ""),
            "name": node.get("n") or node.get("name") or "(未命名)",
            "is_dir": is_dir,
            "size": node.get("s") or 0,
            "size_text": "" if is_dir else _size_text(node.get("s")),
            "ext": "" if is_dir else (node.get("ico") or "").strip(),
            "mtime": node.get("t") or "",
            # `pte` = 115 的「修改时间」（`t` 是创建/添加时间）—— 排序用（红领巾
            # 2026-10-06：排序要「时间、名称、修改」都支持）。拿不到就回落到 mtime。
            "ptime": node.get("pte") or node.get("t") or "",
            # ⚠️ 曾经透传 `node["fc"]` 给前端显示「目录 · N 项」——**实测该字段恒为 0**
            #    （2026-10-05 打线上 /folder 实测：根目录 5 个明明有内容的目录全回 0），
            #    显示出来就是个只会说「0 项」的假信息，已从响应里去掉（红领巾要求）。
            #    `_dirs_impl` 里的 `count` 仍保留：目录选择器那处不展示它，无害。
        })
    items.sort(key=lambda x: (not x["is_dir"], x["name"]))
    total = data.get("count")
    next_offset = int(offset) + len(items)
    has_more = bool(items) and total is not None and next_offset < int(total)
    return {
        "ok": True,
        "cid": str(cid),
        "path": path,
        "items": items,
        "count": len(items),
        "total": total,
        "offset": int(offset),
        "next_offset": next_offset,
        "has_more": has_more,
        "warning": warning,
        # 这一层的**真实构成**（115 在 `nf=1` 时会给 folder_count / file_count，
        # 两者都在 `data` 顶层）—— 前端拿它显示「目录 x · 文件 y」，不用再猜。
        "dirs_only": dirs_only,
        "folder_count": data.get("folder_count"),
        "file_count": data.get("file_count"),
    }


# ----------------------------------------------------------------- 任务
@app.get("/api/accounts/{account_id}/tasks", dependencies=[Depends(auth)])
def list_tasks(account_id: str, page: int = 1, stat: int = 0) -> dict:
    """离线任务列表。

    ⚠️ 任务数组在**响应顶层**的 `tasks`（不是 `data`）—— 2026-10-05 实测确认。
    顶层还带 `count` / `quota` 等元信息。
    """
    acc = _find_account(account_id)
    client = _client(acc)
    try:
        data = check_response(client.clouddownload_task_list({
            "page": page, "page_size": 50, "stat": stat,
        }))
    except Exception as exc:
        raise HTTPException(502, f"读取任务失败：{exc}") from exc

    raw_tasks = data.get("tasks") or []
    items = []
    for t in raw_tasks:
        if not isinstance(t, dict):
            continue
        items.append({
            "name": t.get("name") or "",
            "url": t.get("url") or "",
            "status": t.get("status"),
            "status_text": t.get("status_text") or t.get("display_status") or "",
            "percent": t.get("percentDone"),
            "size": t.get("size"),
            "add_time": t.get("add_time"),
            "file_id": str(t.get("file_id") or ""),
            "wp_path_id": str(t.get("wp_path_id") or ""),
            "del_path": t.get("del_path") or "",
            "pick_code": t.get("pick_code") or "",
        })
    return {
        "ok": True,
        "items": items,
        "page": data.get("page") or page,
        "page_count": data.get("page_count") or 1,
        "count": data.get("count") or 0,
        "quota": data.get("quota"),
    }


# ----------------------------------------------------------------- 整理改名
@app.post("/api/rename/preview", dependencies=[Depends(auth)])
def rename_preview(body: RenamePreviewIn) -> dict:
    """给一个已完成任务的落地对象，列出它是什么、里面有什么、以及两档建议名。

    ⚠️ 只做「读 + 给建议」，**不写任何东西** —— 写操作在 /api/rename/apply。
    """
    acc = _find_account(body.account_id)
    client = _client(acc)

    file_id = body.file_id.strip()
    if not file_id:
        raise HTTPException(400, "缺少 file_id（该任务可能没有落地对象）")

    try:
        info = check_response(client.fs_file({"file_id": file_id}))
    except Exception as exc:
        raise HTTPException(502, f"读取对象信息失败：{exc}") from exc

    node = (info.get("data") or [{}])
    if isinstance(node, list):
        node = node[0] if node else {}
    if not isinstance(node, dict):
        node = {}
    name = node.get("n") or node.get("name") or body.name or ""
    is_folder = _is_folder(node) if node else (body.kind != "file")
    ico = (node.get("ico") or "").strip() or None

    children: list[dict] = []
    if is_folder:
        try:
            listing = check_response(client.fs_files({"cid": file_id, "limit": 200, "offset": 0}))
            for child in (listing.get("data") or []):
                if isinstance(child, dict):
                    children.append({
                        "file_id": str(child.get("fid") or child.get("cid") or ""),
                        "name": child.get("n") or child.get("name") or "",
                        "is_dir": _is_folder(child),
                        "size": child.get("s") or 0,
                        "ext": (child.get("ico") or "").strip() or None,
                    })
        except Exception:
            children = []

    return {
        "ok": True,
        "file_id": file_id,
        "name": name,
        "is_dir": bool(is_folder),
        "ext": None if is_folder else ico,
        "children": children,
        "suggestions": suggest(name) if is_folder else suggest_file(name),
        "media": parse_media(name),
    }


@app.post("/api/rename/apply", dependencies=[Depends(auth)])
def rename_apply(body: RenameApplyIn) -> dict:
    """执行改名。传原名即「改回」—— 所以前端只需记住原名就能做撤销。

    ⚠️ 规则（都来自 p115client 文档原文）：
      - 目录名不能含 `<` `>` `，` ⇒ 走 sanitize_name 过滤
      - 文件改名**必须带扩展名**，否则最后一个句点之后会被截断 ⇒ ensure_ext
    ⚠️ 分块 + 延时 + 单飞：批量写不并发（同删除的理由）。
    """
    if not body.items:
        raise HTTPException(400, "没有要改的条目")
    if len(body.items) > 500:
        raise HTTPException(400, "一次最多改 500 条")

    acc = _find_account(body.account_id)
    client = _client(acc)

    pairs: list[tuple[str, str]] = []
    for item in body.items:
        final = sanitize_name(ensure_ext(item.new_name, item.ext))
        if not final:
            raise HTTPException(400, f"新名为空（file_id={item.file_id}）")
        pairs.append((item.file_id, final))

    groups = _chunks(pairs, body.chunk)
    applied: list[dict] = []
    failed: list[dict] = []
    messages: list[str] = []
    started = time.time()

    with _fs_write_slot("改名"):
        for i, group in enumerate(groups):
            if i:
                time.sleep(body.delay)
            try:
                resp_ = client.fs_rename(group)
                ok = True
                msg = ""
                if isinstance(resp_, dict):
                    ok = resp_.get("state") in (True, 1, "1")
                    msg = str(resp_.get("error_msg") or resp_.get("message") or "")
                if ok:
                    applied.extend({"file_id": fid, "new_name": name} for fid, name in group)
                    if msg:
                        messages.append(msg)
                else:
                    failed.append({
                        "file_ids": [fid for fid, _ in group],
                        "error": msg or str(resp_),
                    })
            except Exception as exc:
                failed.append({"file_ids": [fid for fid, _ in group], "error": f"{exc}"})

    # 改名会连带影响父子目录的显示 ⇒ 立刻失效缓存，避免拿旧列表误操作
    if applied:
        _cache_drop(body.account_id)
    return {
        "ok": not failed,
        "applied": applied,
        "failed": failed,
        "changed": len(applied),
        "failed_count": len(failed),
        "seconds": round(time.time() - started, 1),
        "message": "；".join(messages[:3]),
    }


# ----------------------------------------------------------------- 本地改名建议
@app.post("/api/suggest", dependencies=[Depends(auth)])
def suggest_names(body: SuggestIn) -> dict:
    """**纯本地**算候选新名 —— 不碰 115，所以不受风控、可以随便点。

    目录 → `namer.suggest()`：片名（年份）/ 片名（系列）/ 只留片名 / 保持原名
    分集文件 → `namer.suggest_episode()`：剧名.SxxExx.第 N 集.集标题.ext
    其它文件 → `namer.suggest_file()`：视频优先给「片名（年份）.ext」，否则保守清洗
    """
    if len(body.items) > 500:
        raise HTTPException(400, "一次最多算 500 条")
    paren = body.paren if body.paren in ("full", "half") else "full"

    out: list[dict] = []
    for it in body.items:
        name = it.name or ""
        if it.is_dir:
            cands = suggest(
                name, add_year=it.add_year, paren=paren,
                series=it.series, title_override=it.title,
            )
        else:
            cands = suggest_file(
                name, paren=paren, add_year=it.add_year, series_title=it.series_title,
            )
        out.append({
            "name": name,
            "is_dir": it.is_dir,
            "suggestions": cands,
            # 抽出的「片名」单独给一份（不是候选、不进界面按钮）——
            # 前端拿它当分集文件的「剧名前缀」（根目录片名 → 子目录分集改名）。
            # 🔴 2026-10-06 起，已规范的名字（`护肝人（2026）` 等）只回「保持原名」，
            #    suggestions 里不再有 title 档 ⇒ 这个字段就是 rootTitle 的唯一来源。
            "title": title_of(name) if it.is_dir else None,
            "media": parse_media(name),
            # 只是界面筛选用的提示（命中广告词/域名）—— 绝不据此自动删文件
            "ad_hint": looks_like_ad(name),
        })
    return {"ok": True, "items": out, "paren": paren}


# ----------------------------------------------------------------- 删除（进回收站）
@app.post("/api/delete", dependencies=[Depends(auth)])
def delete_items(body: DeleteIn) -> dict:
    """删除对象 —— **进 115 回收站，可还原**。

    ⚠️ 为什么要分块 + 块间延时 + 全局单飞（一条都不是拍脑袋）：
      - p115client 对 `fs_delete` 的文档原文：「**请不要并发执行**，但不限制文件数」
        以及「删除和（从回收站）还原是互斥的，同时最多只允许执行一个操作」
      - 红领巾 2026-10-05 的要求：批量操作别并发太快，避免 115 风控
      ⇒ 默认 20 个一组、组间 sleep 1.2s。要更慢/更快改 `chunk` / `delay` 即可。

    🔴 **重要且反直觉：115 的删除是「异步任务」。** 2026-10-05 实测（errno 990009）：
      - 删完立刻查，对象**还在**（`fs_file` 返回 state:true）⇒ 别拿列表判断成败
      - 再删一次会回 `231011 文件已删除，请勿重复操作`
      - 再删一次会回 `990009 删除[...]操作尚未执行完成，请稍后再试`
      - 回收站列表也要**几十秒~十几分钟**才更新出来
      ⇒ 所以这里对「尚未执行完成」做**有限重试 + 退避**，而不是立刻判失败。
    """
    ids: list[str] = []
    seen: set[str] = set()
    for raw in body.file_ids:
        fid = str(raw).strip()
        if fid and fid not in seen:
            seen.add(fid)
            ids.append(fid)
    if not ids:
        raise HTTPException(400, "没选中任何对象")
    if len(ids) > 500:
        raise HTTPException(400, f"一次最多删 500 个，当前 {len(ids)} 个")

    groups = _chunks(ids, body.chunk)
    plan = {
        "count": len(ids),
        "chunk": body.chunk,
        "delay": body.delay,
        "groups": len(groups),
        "sleep_total": round(body.delay * max(len(groups) - 1, 0), 1),
    }
    if body.dry_run:
        return {"ok": True, "dry_run": True, "plan": plan, "deleted": [], "failed": []}

    acc = _find_account(body.account_id)
    client = _client(acc)
    deleted: list[str] = []
    failed: list[dict] = []
    started = time.time()

    with _fs_write_slot("删除"):
        for i, group in enumerate(groups):
            if i:
                time.sleep(body.delay)
            payload: dict[str, Any] = {"fid": ",".join(group)}
            if body.ignore_warn:
                payload["ignore_warn"] = 1
            # 115 的删除是异步任务，「上一个还没跑完」会导致 990009 ⇒ 退避重试（有限次）
            for attempt in range(3):
                try:
                    check_response(client.fs_delete(payload))
                    deleted.extend(group)
                    break
                except Exception as exc:
                    text = str(exc)
                    busy = "尚未执行完成" in text or "990009" in text
                    if busy and attempt < 2:
                        time.sleep(3.0 * (attempt + 1))
                        continue
                    # 231011 = 「已删除，请勿重复操作」⇒ 其实已经生效了，按成功记
                    already = "请勿重复操作" in text or "231011" in text
                    if already:
                        deleted.extend(group)
                    else:
                        failed.append({"file_ids": group, "error": text})
                    break

    if deleted:
        _cache_drop(body.account_id)
    return {
        "ok": not failed,
        "dry_run": False,
        "plan": plan,
        "deleted": deleted,
        "failed": failed,
        "deleted_count": len(deleted),
        "failed_count": len(failed),
        "seconds": round(time.time() - started, 1),
        "note": "115 的删除是异步任务：已提交，回收站里可能延迟几十秒~几分钟才出现；"
                "要确认是否真删掉，请刷新目录而不是立刻看回收站",
    }


# ----------------------------------------------------------------- 回收站
@app.get("/api/accounts/{account_id}/recyclebin", dependencies=[Depends(auth)])
def recyclebin_list(account_id: str, limit: int = 30, offset: int = 0) -> dict:
    """回收站列表 —— 删除的兜底安全网。

    ⚠️ 字段名是 2026-10-05 实测出来的（不是猜）：
       条目 = `id`(对象 id) / `file_name` / `file_size`(**字节的字符串**) /
              `type` / `dtime`(时间戳字符串) / `cid` + `parent_name`（**原**父目录）/ `ico`
       `type` 实测取值：**`"1"` = 文件、`"2"` = 目录**（拿一个自建的目录条目核对过）。
       ⚠️ 回收站安全密钥若**不是**默认的 `000000`（见列表里的 `rb_pass`），
          彻底删除/清空会**静默失败**（返回 state:true 但什么都没删）
          ⇒ 所以本工具不提供彻底删除。
    """
    acc = _find_account(account_id)
    client = _client(acc)
    limit = max(1, min(int(limit), 100))
    try:
        data = check_response(client.recyclebin_list({
            "aid": 7, "limit": limit, "offset": int(offset),
            # ⚠️ **必须显式倒序**。实测踩过：不传时按 dtime **升序**（最旧的在前），
            #    于是「刚删掉的东西」根本不在第一页里 —— 用户会以为没删成功。
            #    文档里 `asc: 0 | 1 = <default>` 也是这个意思（默认 1 = 升序）。
            "o": "dtime", "asc": 0,
        }))
    except Exception as exc:
        raise HTTPException(502, f"读取回收站失败：{exc}") from exc

    items: list[dict] = []
    for node in (data.get("data") or []):
        if not isinstance(node, dict):
            continue
        items.append({
            "file_id": str(node.get("id") or ""),
            "name": node.get("file_name") or "",
            # 实测：type "1" = 文件、"2" = 目录
            "is_dir": str(node.get("type")) == "2",
            "size_text": _size_text(node.get("file_size")),
            "deleted_at": node.get("dtime") or "",
            "parent_id": str(node.get("cid") or ""),
            "parent_name": node.get("parent_name") or "",
            "ext": (node.get("ico") or "").strip(),
        })
    try:
        total = int(data.get("count") or 0)
    except (TypeError, ValueError):
        total = 0
    return {"ok": True, "items": items, "count": total, "limit": limit, "offset": int(offset)}


@app.post("/api/recyclebin/revert", dependencies=[Depends(auth)])
def recyclebin_revert(body: RevertIn) -> dict:
    """从回收站还原（载荷是 `rid[0..]`，实测确认）。

    🔴 **绝不提供「彻底删除 / 清空回收站」，也不要在任何地方调用 `recyclebin_clean`。**
       2026-10-05 真实踩过（详见 DEPLOY-LOCAL.md「事故记录」）：
       在群晖上用同一个账号调了一次
           `cl.recyclebin_clean({"tid": "<单个 rid>"}, password="000000")`
       ——请求体确实是 `{"tid": "<rid>", "password": "000000"}`（读过 p115client 源码确认），
       接口当场返回 `state: true`，此刻回收站**没变化**（13 条）；
       约 1~2 分钟后**整个回收站变成 0 条**（含一个 5.22 GB 的 ISO，永久丢失）。
       ⇒ 结论：`/rb/secret_del` 在**安全密钥不是默认值**（列表里的 `rb_pass` = 1）时，
         `tid` 会被忽略、退化成「清空回收站」，而且是**异步**生效、当场看不出来。
       这是不可恢复的操作 ⇒ 本工具只做「删除进回收站 + 还原」，永久删除交给 115 官方界面。

    ⚠️ 还原与删除互斥 ⇒ 与删除共用同一把单飞锁。
    """
    ids: list[str] = []
    seen: set[str] = set()
    for raw in body.file_ids:
        rid = str(raw).strip()
        if rid and rid not in seen:
            seen.add(rid)
            ids.append(rid)
    if not ids:
        raise HTTPException(400, "没选中任何条目")
    if len(ids) > 500:
        raise HTTPException(400, f"一次最多还原 500 个，当前 {len(ids)} 个")

    acc = _find_account(body.account_id)
    client = _client(acc)
    groups = _chunks(ids, body.chunk)
    done: list[str] = []
    failed: list[dict] = []
    started = time.time()

    with _fs_write_slot("还原"):
        for i, group in enumerate(groups):
            if i:
                time.sleep(body.delay)
            try:
                check_response(client.recyclebin_revert(group))
                done.extend(group)
            except Exception as exc:
                failed.append({"file_ids": group, "error": f"{exc}"})

    if done:
        _cache_drop(body.account_id)
    return {
        "ok": not failed,
        "reverted": done,
        "failed": failed,
        "reverted_count": len(done),
        "failed_count": len(failed),
        "seconds": round(time.time() - started, 1),
    }


# ----------------------------------------------------------------- 推送
@app.post("/api/parse", dependencies=[Depends(auth)])
def parse(body: ParseIn) -> dict:
    items = parse_links(body.text)
    restored = [i for i in items if i["source"] != "链接"]
    lines = [ln for ln in (body.text or "").splitlines() if ln.strip()]
    return {
        "ok": True,
        "items": items,
        "count": len(items),
        "stats": {
            "lines": len(lines),
            "restored": len(restored),
            "sources": sorted({i["source"] for i in restored}),
        },
    }


@app.post("/api/push", dependencies=[Depends(auth)])
def push(body: PushIn) -> dict:
    acc = _find_account(body.account_id)

    urls: list[str] = []
    src_of: dict[str, str] = {}      # 小写 url → 来源（链接 / 百家姓 / 核心价值观 / 佛曰）
    if body.text:
        for item in parse_links(body.text):
            urls.append(item["url"])
            src_of.setdefault(str(item["url"]).lower(), item.get("source") or "链接")
    if body.urls:
        for u in body.urls:
            if u and u.strip():
                urls.append(u.strip())

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
    if len(deduped) > MAX_BATCH:
        raise HTTPException(400, f"一次最多 {MAX_BATCH} 条，当前 {len(deduped)} 条（分批推吧）")

    client = _client(acc)
    payload: dict[str, Any] = {f"url[{i}]": u for i, u in enumerate(deduped)}
    wp = (body.wp_path_id or "").strip() or str(acc.get("default_dir") or "").strip()
    if wp:
        payload["wp_path_id"] = wp

    # 本地账本的公共部分（成功 / 失败都要记一笔，否则「推了但没下来」查不出原因）
    # ⚠️ `skipped` 多数时候是 0：`parse_links` 自己在解析阶段就按 info_hash 去过重了，
    #    这里那一层只兜住「前端直接把 urls 数组传上来」的情况。
    def _log(ok: bool, message: str) -> None:
        _PUSHLOG.add(
            account_id=str(body.account_id),
            account=str(acc.get("name") or ""),
            dir_id=wp,
            dir=(body.dir_name or "").strip(),
            count=len(deduped),
            skipped=len(urls) - len(deduped),
            links=[{"url": u, "source": src_of.get(u.lower(), "链接")} for u in deduped],
            ok=ok,
            message=message,
        )

    try:
        resp = client.clouddownload_task_add_urls(payload)
    except Exception as exc:
        _log(False, f"推送失败：{exc}")
        raise HTTPException(502, f"推送失败：{exc}") from exc

    ok = True
    message = ""
    if isinstance(resp, dict):
        state = resp.get("state")
        ok = state in (True, 1, "1", None)
        message = str(resp.get("error_msg") or resp.get("message") or "")

    # 新任务落地后目标目录会多东西 ⇒ 丢掉该账号的目录缓存（下一次点开就是新的）
    if ok:
        _cache_drop(body.account_id)

    _log(ok, message)

    return {
        "ok": ok,
        "count": len(deduped),
        "urls": deduped,
        "wp_path_id": wp,
        "message": message,
        "raw": resp,
    }


# ----------------------------------------------------------------- 推送记录
@app.get("/api/pushlog", dependencies=[Depends(auth)])
def pushlog_list(limit: int = Query(default=200, ge=1, le=PUSHLOG_MAX)) -> dict:
    """本地推送记录（最新在前）。与 115 无关，纯本工具自己的账本。"""
    return {
        "ok": True,
        "items": _PUSHLOG.items(limit),
        "count": _PUSHLOG.count(),
        "max": PUSHLOG_MAX,
    }


@app.delete("/api/pushlog", dependencies=[Depends(auth)])
def pushlog_clear() -> dict:
    return {"ok": True, "removed": _PUSHLOG.clear()}


# ----------------------------------------------------------------- 静态
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 启动时把落盘的目录缓存恢复回内存（过期的丢弃）。放在路由都注册完之后、
# 对外服务之前 —— 失败只打日志，绝不影响启动。
_cache_load_disk()
# 本地推送记录同样从盘里恢复（账本，丢了也不影响推送功能）
_PUSHLOG.load()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT") or 8765))
