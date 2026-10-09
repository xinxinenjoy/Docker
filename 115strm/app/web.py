"""网页服务（FastAPI）—— UI 后端。

## 定位

⛔ 它不是「第二个实现」。业务规则全在 `sync.py` / `namer.py` / `treefile.py` 里，
本模块只做三件事：**解析请求 → 调库 → 回话**。

## 三条安全约定

1. **口令**：`ACCESS_TOKEN` 为空则不启用（适合内网）；设了就要求 `Bearer`。
   ⚠️ 与 115offline / 115organize 同名同语义 —— 一个口令管三个服务。
2. **同一时刻只允许一轮**：本地写盘虽无 115 的并发限制，但两轮并发会互相
   覆盖挂起清单与断点 ⇒ Runner 单飞。
3. **停得掉**：节流器的 `sleeper` 被换成「分小段睡 + 查停止标志」，
   按下停止最多 0.25 秒就跳出 —— 否则一次长休 60 秒看着像没反应。
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from . import accounts as accounts_mod
from . import pipeline as pipeline_mod
from . import report as report_mod
from . import settings as settings_mod
from . import treefile
from .config import Config, load
from .health import scope_warn
from .namer import StrmBuilder, is_video
from .sync import Pending, SyncResult, Syncer
from .throttle import Throttle
from .v115 import CookieMissing, V115, build_client, load_cookie

__all__ = ["app", "serve"]

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

ACCESS_TOKEN = (os.environ.get("ACCESS_TOKEN") or "").strip()


class Cancelled(Exception):
    """用户按了停止（⛔ 不是错误，回执里要说「已停止」而不是「失败」）。"""


# --------------------------------------------------------------------------- 鉴权
def auth(authorization: str | None = Header(default=None)) -> None:
    if not ACCESS_TOKEN:
        return
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if token != ACCESS_TOKEN:
        raise HTTPException(401, "访问口令错误")


app = FastAPI(title="115 STRM", docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------- 日志环
class LogBuffer:
    """带序号的环形缓冲 —— 前端带 `seq` 来问增量，既不重传又有实时感。"""

    def __init__(self, size: int = 600):
        self._items: deque[dict] = deque(maxlen=size)
        self._seq = 0
        self._lock = threading.Lock()

    def add(self, level: str, msg: str) -> None:
        with self._lock:
            self._seq += 1
            self._items.append({
                "seq": self._seq,
                "t": datetime.now().strftime("%H:%M:%S"),
                "level": (level or "info").lower(),
                "msg": msg,
            })

    def since(self, after: int = 0, limit: int = 400) -> dict:
        with self._lock:
            items = [x for x in self._items if x["seq"] > after]
            return {"seq": self._seq, "items": items[-limit:]}


# --------------------------------------------------------------------------- 执行器
class Runner:
    """后台跑一轮同步 —— 不能同步等（扫几万文件 + 节流可能十几分钟）。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.log = LogBuffer()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.status: dict = self._blank()

    @staticmethod
    def _blank() -> dict:
        return {
            "running": False, "source": "",
            "stage": "", "stage_label": "",
            "written": 0, "deleted": 0, "pending_delete": 0, "requests": 0,
            "dry_run": True, "started": "", "finished": "",
            "result": None, "error": "", "stopping": False, "cancelled": False,
            "detail": {},
        }

    def _set_stage(self, label: str) -> None:
        with self._lock:
            self.status["stage_label"] = label

    # ---- 供节流器注入 ---------------------------------------------------
    def _log(self, level: str, msg: str) -> None:
        self.log.add(level, msg)

    def _sleeper(self, seconds: float) -> None:
        """把长睡切碎，好让「停止」立刻生效。

        ⚠️ 别改成一句 `time.sleep(seconds)` —— 节流器会睡 60 秒（长休）
           甚至 900 秒（失败冷却），那样按停止就是「点了没反应」。
        """
        end = time.monotonic() + max(0.0, float(seconds))
        while True:
            if self._stop.is_set():
                raise Cancelled()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.25, left))

    # ---- 启停 -----------------------------------------------------------
    def start(self, cfg: Config, *, force_delete: bool = False) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise HTTPException(409, "已经有一轮在跑了 —— 等它结束或先按停止")
            self._stop.clear()
            self.log = LogBuffer()
            self.status = self._blank()
            self.status.update({
                "running": True, "dry_run": bool(cfg.dry_run),
                "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            self._thread = threading.Thread(
                target=self._work, args=(cfg, force_delete), daemon=True)
            self._thread.start()
            return dict(self.status)

    def stop(self) -> dict:
        if not (self._thread and self._thread.is_alive()):
            return {"ok": False, "msg": "当前没有在跑的任务"}
        self._stop.set()
        self.status["stopping"] = True
        self.log.add("warning", "收到停止请求 —— 等当前这一步做完就收工（最多 0.25 秒）")
        return {"ok": True}

    # ---- 真正干活 --------------------------------------------------------
    def _work(self, cfg: Config, force_delete: bool) -> None:
        """跑一轮。

        🔴 **执行逻辑全部下沉到 `pipeline.run_once`** —— web 与 cli 共用同一份。
           原来 web 和 cli 各写了一遍「按模式组装网盘现状」，结果分叉
           （网页点「事件流」永远 400，命令行却说「能跑」）。两处各写一份，必然分叉。
           现在连 **`mode` 这个参数都取消了** —— 只有一条路，没什么可传的。
        """
        stamp = report_mod.now_stamp()
        try:
            self.log.add("info",
                         f"开始同步（自己导目录树 → 全量对账）；"
                         f"DRY_RUN={'开' if cfg.dry_run else '关'}")
            out = pipeline_mod.run_once(
                cfg, self._log, sleeper=self._sleeper,
                force_delete=force_delete, on_stage=self._set_stage)
            res = out.res
            with self._lock:
                self.status["source"] = out.source
                self.status["detail"] = out.detail
            self._finish(res, stamp)
        except Cancelled:
            self._finish(None, stamp, cancelled=True)
        except CookieMissing as exc:
            self._fail("没找到 115 cookie", str(exc))
        except ValueError as exc:
            self._fail("参数", str(exc))
        except Exception as exc:
            self._fail(type(exc).__name__, str(exc))

    def _finish(self, res: SyncResult | None, stamp: str, *, cancelled: bool = False) -> None:
        """收尾。

        🔴 **顺序不能换**：先把回执落地、**最后**才把 `running` 翻 False。
           反过来的话，前端一看到 running=False 就停止轮询，
           而「回执已写出」那行是在之后才进缓冲的 ⇒ 页面上永远看不到它。
        """
        if res:
            try:
                out = Path(self.data_dir) / "reports"
                out.mkdir(parents=True, exist_ok=True)
                d = res.as_dict()
                (out / f"sync-{stamp}.json").write_text(
                    json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
                (out / f"sync-{stamp}.md").write_text(
                    report_mod.render_sync_markdown(d, load().all()), encoding="utf-8")
                detail = report_mod.render_diff_txt(d)
                if detail.strip():
                    (out / f"sync-{stamp}.detail.txt").write_text(detail, encoding="utf-8")
                self.log.add("info", f"回执已写出：reports/sync-{stamp}.md")
            except Exception as exc:
                self.log.add("warning", f"回执写入失败（不影响结果）：{exc}")
        self.log.add("info", "本轮结束" + ("（已停止）" if cancelled else ""))

        with self._lock:
            if res:
                self.status.update({
                    "written": res.written, "deleted": res.deleted,
                    "pending_delete": res.pending_delete, "requests": res.requests,
                    "result": res.as_dict(),
                })
            self.status.update({
                "running": False, "stage": "", "stage_label": "",
                "stopping": False, "cancelled": cancelled,
                "finished": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            if cancelled:
                self.status["error"] = "已按你的要求停止（已经写出去的保留，剩下的没动）"

    def _fail(self, kind: str, msg: str) -> None:
        with self._lock:
            self.status.update({
                "running": False, "stage": "", "stage_label": "", "stopping": False,
                "error": f"{kind}：{msg}",
                "finished": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        self.log.add("error", f"{kind}：{msg}")

    def snapshot(self, after: int = 0) -> dict:
        logs = self.log.since(after)
        with self._lock:
            st = dict(self.status)
        st["log_seq"] = logs["seq"]
        st["log"] = logs["items"]
        return st


RUNNER: Runner | None = None


def _runner() -> Runner:
    global RUNNER
    cfg = load()
    if RUNNER is None or Path(RUNNER.data_dir) != Path(cfg.data_dir):
        RUNNER = Runner(cfg.data_dir)
    return RUNNER


# --------------------------------------------------------------------------- 请求体
class SettingsIn(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class ResetIn(BaseModel):
    keys: list[str] = Field(default_factory=list)
    all: bool = False


class RunIn(BaseModel):
    dry_run: bool | None = None        # None = 跟随配置；显式给只影响这一次
    force_delete: bool = False         # 跳过挂起等待，立刻删


class PreviewIn(BaseModel):
    limit: int = 200


# --------------------------------------------------------------------------- 静态页
@app.get("/", response_class=HTMLResponse)
def index() -> Any:
    page = STATIC_DIR / "index.html"
    if not page.exists():
        return HTMLResponse("<h1>界面文件缺失</h1><p>app/static/index.html 不在镜像里。</p>",
                            status_code=500)
    return FileResponse(page, media_type="text/html; charset=utf-8")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "auth": bool(ACCESS_TOKEN),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# --------------------------------------------------------------------------- 总览
@app.get("/api/state", dependencies=[Depends(auth)])
def state() -> dict:
    """首屏一次拿齐 —— ⛔ 别让前端进来就并发打 6 个接口。"""
    cfg = load()
    out: dict = {
        "auth": bool(ACCESS_TOKEN),
        "data_dir": str(cfg.data_dir),
        "config_file": str(settings_mod.config_path(cfg.data_dir)),
        "output_dir": str(cfg.output_dir),
        "output_exists": Path(cfg.output_dir).exists(),
        "remote_root": cfg.remote_root or "(网盘根)",
        "strm_prefix": cfg.strm_prefix,
        "prefix_ready": cfg.prefix_ready,
        "dry_run": cfg.dry_run,
        "schedule_cron": cfg.schedule_cron,
        "delete_defer_minutes": cfg.delete_defer_minutes,
        "delete_ratio_guard": cfg.delete_ratio_guard,
        "accounts_file": str(cfg.accounts_file),
    }

    try:
        ck = load_cookie(cfg)
        out["cookie"] = {"ok": True, "source": getattr(cfg, "cookie_source", ""),
                         "tail": "…" + ck[-8:]}
    except CookieMissing as exc:
        out["cookie"] = {"ok": False, "error": str(exc)}

    # 目录树
    try:
        tp = Path(treefile.pick_tree_file(cfg.tree_dir))
        st = tp.stat()
        out["tree"] = {"ok": True, "file": str(tp), "name": tp.name,
                       "size_kb": round(st.st_size / 1024, 1),
                       "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")}
    except Exception as exc:
        out["tree"] = {"ok": False, "dir": str(cfg.tree_dir), "error": str(exc)}

    out["pending"] = _pending_info(cfg)
    out["run"] = _runner().snapshot(after=0)
    # ⭐ 前缀形态的**离线**快速判定（不发请求，首屏不能卡）——
    #    真连通性由「自检」按钮触发，见 /api/check-prefix。
    out["prefix_warn"] = scope_warn(cfg)
    # 同步范围（给首屏显示「只处理哪几个目录」）
    out["scope"] = {"remote_root": cfg.remote_root or "",
                    "includes": list(cfg.includes)}
    # ⭐ 「这一轮会做什么」的步骤表 —— 后端是**唯一事实源**，前端别另抄一份
    #    （本项目吃过「两处各写一遍必然分叉」的亏，见 `pipeline` 顶部）
    out["steps"] = [{"key": k, "title": t, "help": h}
                    for k, t, h in pipeline_mod.SYNC_STEPS]
    return out


# --------------------------------------------------------------------------- 同步范围
@app.get("/api/scope", dependencies=[Depends(auth)])
def get_scope(path: str = "") -> dict:
    """目录选择器：列**某一层**的子目录（从目录树读，**零请求**）。

    `path` 相对**树根**；**留空 = 同步根的下一层**（那才是用户要选的东西 ——
    同步根自己不用选，它是基准）。

    返回里带每个子目录的**规模**（多少子目录 / 多少直接文件），
    让用户勾之前知道代价 —— 这比让人猜有用得多。
    """
    cfg = load()
    try:
        tp = Path(treefile.pick_tree_file(cfg.tree_dir))
        tree = treefile.parse_file(tp)
    except Exception as exc:
        raise HTTPException(400, f"目录树读不到：{exc}") from exc

    rel_root = (cfg.remote_root or "").strip("/")
    # ⭐ 留空 ⇒ 落到**同步根**（列它的子项）；这样前端一进来就能勾「115电影」，
    #    而不是先看到同步根自己（它没得选）。
    cur = (path or "").strip("/")
    if not cur:
        cur = rel_root

    try:
        node_rel = tree.resolve(cur)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc

    # 列子项时用**树内完整路径**（`list_level` 的入参口径）
    items = tree.list_level(node_rel)

    # 面包屑：同步根 › …（树根那段是噪音，不显示）
    #
    # ⚠️ 这里**不能用长度切片**去前缀 —— 曾经写成 `cur[len(rel_root):]`，
    #    当 `cur` 不以 `rel_root` 开头时（如 rel_root=`云下载`、cur=`115电影`）
    #    切出来是 `电影`，面包屑直接错。改成「先判断在不在下面，再切」。
    crumbs: list[dict] = []
    root_name = tree.entries[tree.root_path].name
    under_root = bool(rel_root) and (cur == rel_root or cur.startswith(rel_root + "/"))
    if under_root:
        try:
            rn = tree.entries[tree.resolve(rel_root)].name
        except Exception:
            rn = rel_root.rsplit("/", 1)[-1]
        crumbs.append({"name": rn, "path": rel_root})
        rest = cur[len(rel_root):].strip("/")
        acc = rel_root
    else:
        crumbs.append({"name": root_name, "path": ""})
        rest = cur
        acc = ""
    for part in (rest.split("/") if rest else []):
        acc = f"{acc}/{part}" if acc else part
        crumbs.append({"name": part, "path": acc})

    e = tree.entries[node_rel]
    return {
        "node": {"name": e.name, "path": cur, "is_dir": True,
                 "files": len(tree.files_of(node_rel)),
                 "subtree": tree.subtree_stats(node_rel)},
        "crumbs": crumbs,
        "items": items,
        "tree_root": tree.entries[tree.root_path].name,
        "at_root": cur == rel_root,
        "scope": {"remote_root": cfg.remote_root or "", "includes": list(cfg.includes)},
    }


class ScopeIn(BaseModel):
    remote_root: str = ""
    include_dirs: list[str] = Field(default_factory=list)


@app.post("/api/scope", dependencies=[Depends(auth)])
def set_scope(body: ScopeIn) -> dict:
    """保存同步范围（同步根 + 白名单），并**立刻回一份试算**。

    试算 = 按新范围算「会写多少 / 会删多少」，**一个文件都不动** ——
    改范围是大事（会改变本地目录结构），必须让人当场看到后果。
    """
    cfg = load()
    values: dict = {}
    if body.remote_root.strip():
        values["remote_root"] = body.remote_root.strip().strip("/")
    # 白名单：把 `remote_root/xxx` 形态归一成 `xxx`
    root = (body.remote_root or cfg.remote_root or "").strip("/")
    incs = []
    for x in body.include_dirs:
        p = str(x).strip().strip("/")
        if root and p.startswith(root + "/"):
            p = p[len(root) + 1:]
        if p and p not in incs:
            incs.append(p)
    values["include_dirs"] = ",".join(incs)

    res = settings_mod.save_json(Path(cfg.data_dir), values)
    from . import config as config_mod
    config_mod.reload_overrides()
    cfg = load()

    out = {"saved": res["saved"], "rejected": res["rejected"],
           "scope": {"remote_root": cfg.remote_root, "includes": list(cfg.includes)},
           "warn": scope_warn(cfg)}
    # 试算（零请求，只读目录树 + 本地磁盘）
    try:
        tp = Path(treefile.pick_tree_file(cfg.tree_dir))
        tree = treefile.parse_file(tp)
        syncer = Syncer(None, cfg.replace(dry_run=True))
        dirs, files = syncer.walk_from_tree(tree)
        local = syncer.scan_local()
        d = syncer.diff(files, local)
        out["estimate"] = {
            "remote_dirs": len(dirs), "remote_files": len(files),
            "local_strm": len(local), "counts": d.counts(),
            "sample": ([{"rel": f.local_rel, "url": f.url} for f in files[:3]] if files else []),
        }
    except Exception as exc:
        out["estimate"] = {"error": str(exc)}
    return out


def _pending_info(cfg: Config) -> dict:
    """挂起删除现状 —— 页面上要能一眼看到「有 N 条等着确认」。"""
    s = Syncer(None, cfg)
    p = s.load_pending()
    if not p.paths:
        return {"count": 0}
    return {
        "count": len(p.paths),
        "age_minutes": round(p.age_minutes(), 1),
        "wait_minutes": cfg.delete_defer_minutes,
        "ready": p.age_minutes() >= cfg.delete_defer_minutes,
        "sample": p.paths[:50],
    }


# --------------------------------------------------------------------------- 设置
@app.get("/api/settings", dependencies=[Depends(auth)])
def get_settings() -> dict:
    cfg = load()
    return settings_mod.describe(cfg, Path(cfg.data_dir))


@app.put("/api/settings", dependencies=[Depends(auth)])
def put_settings(body: SettingsIn) -> dict:
    cfg = load()
    res = settings_mod.save_json(Path(cfg.data_dir), body.values)
    from . import config as config_mod
    config_mod.reload_overrides()
    cfg = load()
    out = settings_mod.describe(cfg, Path(cfg.data_dir))
    out["saved"] = res["saved"]
    out["rejected"] = res["rejected"]
    return out


@app.post("/api/settings/reset", dependencies=[Depends(auth)])
def reset_settings(body: ResetIn) -> dict:
    cfg = load()
    res = settings_mod.reset_json(Path(cfg.data_dir), None if body.all else body.keys)
    from . import config as config_mod
    config_mod.reload_overrides()
    cfg = load()
    out = settings_mod.describe(cfg, Path(cfg.data_dir))
    out["removed"] = res["removed"]
    return out


# --------------------------------------------------------------------------- 账号
# ⚠️ 账号文件与 115offline **共用格式**（一个 list），可以挂同一个文件互为备份。
#    详见 `accounts.py` 顶部说明。

class AccountIn(BaseModel):
    name: str = ""
    cookie: str = ""


class AccountEditIn(BaseModel):
    name: str | None = None
    cookie: str | None = None


class QrResultIn(BaseModel):
    uid: str
    name: str = ""
    app: str = "tv"


@app.get("/api/accounts", dependencies=[Depends(auth)])
def get_accounts() -> dict:
    """账号列表 + 当前选中项 + 账号文件现状。"""
    cfg = load()
    path = accounts_mod.accounts_file(cfg)
    items = accounts_mod.read_accounts(path)

    picked = (cfg.account_id or "").strip()
    active = picked
    if not active:
        for it in items:
            if accounts_mod.looks_like_cookie(str(it.get("cookie") or "")):
                active = str(it.get("id") or "")
                break

    return {
        "file": str(path),
        "exists": path.exists(),
        "shared": "/shared" in str(path),        # 是不是挂的 115offline 那份
        "active_id": active,
        "picked_id": picked,
        "count": len(items),
        "items": [accounts_mod.public(a) for a in items],
    }


@app.post("/api/accounts", dependencies=[Depends(auth)])
def create_account(body: AccountIn) -> dict:
    """粘贴 cookie 添加账号。顺手校验一次（失败也存下来，cookie 可能只是过期）。"""
    cfg = load()
    cookie = (body.cookie or "").strip()
    if not accounts_mod.looks_like_cookie(cookie):
        raise HTTPException(400, "cookie 格式不对，至少要包含 UID=（一般还有 CID / SEID）")
    path = accounts_mod.accounts_file(cfg)

    items = accounts_mod.read_accounts(path)
    # 同 cookie 已存在 ⇒ 不重复添加（否则共享文件会越来越乱）
    for a in items:
        if str(a.get("cookie") or "").strip() == cookie:
            raise HTTPException(409, f"这个 cookie 已经在账号「{a.get('name')}」里了")

    acc = accounts_mod.make_account(body.name, cookie)
    items.append(acc)
    accounts_mod.write_accounts(path, items)

    probe = accounts_mod.probe_cookie(cookie)
    accounts_mod.touch(path, acc["id"], probe)
    cur = accounts_mod.find_account(path, acc["id"]) or acc
    return {"ok": True, "item": accounts_mod.public(cur),
            "probe": {"ok": probe["ok"], "nickname": probe["nickname"],
                      "error": probe["error"]}}


@app.put("/api/accounts/{account_id}", dependencies=[Depends(auth)])
def edit_account(account_id: str, body: AccountEditIn) -> dict:
    """改名 / 换 cookie（换 cookie **不必**删了重加）。"""
    cfg = load()
    path = accounts_mod.accounts_file(cfg)
    items = accounts_mod.read_accounts(path)
    target = next((a for a in items if str(a.get("id")) == account_id), None)
    if target is None:
        raise HTTPException(404, "账号不存在")

    if body.name is not None and body.name.strip():
        target["name"] = body.name.strip()
    replaced = False
    if body.cookie is not None and body.cookie.strip():
        ck = body.cookie.strip()
        if not accounts_mod.looks_like_cookie(ck):
            raise HTTPException(400, "cookie 格式不对，至少要包含 UID=")
        target["cookie"] = ck
        target["checked"] = None
        target["nickname"] = ""
        target["valid"] = None
        replaced = True
    accounts_mod.write_accounts(path, items)

    probe = None
    if replaced:
        probe = accounts_mod.probe_cookie(target["cookie"])
        accounts_mod.touch(path, account_id, probe)
    return {"ok": True, "item": accounts_mod.public(
        accounts_mod.find_account(path, account_id) or target),
        "probe": ({"ok": probe["ok"], "nickname": probe["nickname"],
                   "error": probe["error"]} if probe else None)}


@app.delete("/api/accounts/{account_id}", dependencies=[Depends(auth)])
def delete_account(account_id: str) -> dict:
    cfg = load()
    path = accounts_mod.accounts_file(cfg)
    items = accounts_mod.read_accounts(path)
    left = [a for a in items if str(a.get("id")) != account_id]
    if len(left) == len(items):
        raise HTTPException(404, "账号不存在")
    accounts_mod.write_accounts(path, left)
    # 被删的正好是当前选中的 ⇒ 顺手清掉选择，免得一直报「找不到账号」
    cfg2 = load()
    if (cfg2.account_id or "").strip() == account_id:
        settings_mod.save_json(Path(cfg2.data_dir), {"account_id": ""})
        from . import config as config_mod
        config_mod.reload_overrides()
    return {"ok": True, "left": len(left)}


@app.post("/api/accounts/{account_id}/check", dependencies=[Depends(auth)])
def check_account(account_id: str) -> dict:
    """校验 cookie 还活着吗，并刷新昵称。"""
    cfg = load()
    path = accounts_mod.accounts_file(cfg)
    acc = accounts_mod.find_account(path, account_id)
    if acc is None:
        raise HTTPException(404, "账号不存在")
    probe = accounts_mod.probe_cookie(str(acc.get("cookie") or ""))
    accounts_mod.touch(path, account_id, probe)
    return {"ok": probe["ok"], "nickname": probe["nickname"], "error": probe["error"]}


# --------------------------------------------------------------------------- 扫码登录
@app.post("/api/qrcode/token", dependencies=[Depends(auth)])
def qrcode_token() -> dict:
    """生成登录二维码（**不需要 cookie** —— 这一步的目的就是拿 cookie）。

    红领巾 2026-10-08：工具以后要分享给别人，手抓 cookie 门槛太高 ⇒ 内置扫码。
    """
    try:
        return {"ok": True, **accounts_mod.qr_new()}
    except Exception as exc:
        raise HTTPException(502, f"取二维码失败：{exc}") from exc


@app.get("/api/qrcode/status", dependencies=[Depends(auth)])
def qrcode_status(uid: str = Query(...)) -> dict:
    try:
        return {"ok": True, **accounts_mod.qr_status(uid)}
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/qrcode/result", dependencies=[Depends(auth)])
def qrcode_result(body: QrResultIn) -> dict:
    """扫码确认后换 cookie 并保存成账号。"""
    cfg = load()
    path = accounts_mod.accounts_file(cfg)
    try:
        got = accounts_mod.qr_exchange(body.uid, body.app)
    except Exception as exc:
        raise HTTPException(502, f"换取登录凭据失败：{exc}") from exc

    items = accounts_mod.read_accounts(path)
    for a in items:
        if str(a.get("cookie") or "").strip() == got["cookie"]:
            raise HTTPException(409, f"这个账号已经在列表里了（「{a.get('name')}」）")

    name = (body.name or "").strip() or f"{accounts_mod.QR_APPS.get(got['app'], got['app'])}扫码"
    acc = accounts_mod.make_account(name, got["cookie"], login_app=got["app"])
    items.append(acc)
    accounts_mod.write_accounts(path, items)

    probe = accounts_mod.probe_cookie(got["cookie"])
    accounts_mod.touch(path, acc["id"], probe)
    cur = accounts_mod.find_account(path, acc["id"]) or acc
    return {"ok": True, "item": accounts_mod.public(cur), "nickname": probe["nickname"]}


# --------------------------------------------------------------------------- 预览
def _need_prefix(cfg: Config) -> None:
    """前置守卫：strm 内容前缀空着 ⇒ 直接 400，并说清楚去哪填。

    🔴 为什么必须有：`strm_prefix` 出厂是空的（网页专属字段，见 `config.strm_prefix`）。
       不拦的话 `/api/preview` 会一路走到 `builder.url(...)` 抛 `ValueError`
       ⇒ FastAPI 回 **500 Internal Server Error** ——
       用户只看到一句「服务器内部错误」，根本猜不到是「没填播放地址」。
       （真跑那条路由 `pipeline.run_once` 的 ⓪ 守卫管，这里补的是**只读接口**。）
    """
    if cfg.prefix_ready:
        return
    raise HTTPException(
        400,
        "还没配置 strm 内容前缀 —— 去「设置 › 播放地址」填上你的 alist 地址"
        "（格式 `https://你的域名:端口/d/挂载路径`），再点「自检 strm 前缀」验一下。")


@app.post("/api/preview", dependencies=[Depends(auth)])
def preview(body: PreviewIn) -> dict:
    """只读预览：拿网盘结构 + 本地现状，算出「会写多少 / 会删多少」，**一个文件都不动**。

    ⚠️ 与 `/api/run` 的区别：这里**强制 dry_run**，而且**只走目录树**（零请求）
       —— 「先看再动」是这个工具的第一原则。
    """
    if _runner().status.get("running"):
        raise HTTPException(409, "正在执行中 —— 先等它结束")
    cfg = load()
    _need_prefix(cfg)
    limit = max(1, min(2000, body.limit))
    try:
        tp = Path(treefile.pick_tree_file(cfg.tree_dir))
        tree = treefile.parse_file(tp)
    except Exception as exc:
        raise HTTPException(400, f"目录树读不到：{exc}") from exc

    syncer = Syncer(None, cfg.replace(dry_run=True))
    remote_dirs, remote_files = syncer.walk_from_tree(tree)
    local = syncer.scan_local()
    d = syncer.diff(remote_files, local)

    builder = StrmBuilder.from_cfg(cfg)
    return {
        "tree_file": str(tp),
        "tree_stats": tree.stats(),
        "remote_dirs": len(remote_dirs),
        "remote_files": len(remote_files),
        "local_strm": len(local),
        "counts": d.counts(),
        # ⭐ 「本地改过名」的条目 —— 它们**不会**被重生成/删除，得让他看得见，
        #    否则他会以为「我改的名没生效」或「工具不认识我改的名」。
        "renamed": [{"local": r, "remote": f.rel} for r, f in d.renamed[:limit]],
        "looks_like_move": bool(
            d.to_delete and len(d.to_add) > 0 and len(d.to_add) >= len(d.to_delete) * 0.5),
        "samples": {
            "add": [{"rel": f.local_rel, "url": f.url} for f in d.to_add[:limit]],
            "update": [{"rel": f.local_rel, "url": f.url,
                        "was": syncer.read_existing(f.local_rel)[:200]} for f in d.to_update[:limit]],
            "delete": d.to_delete[:limit],
        },
        "prefix_sample": builder.url(remote_files[0].rel) if remote_files else "",
        "more": {
            "add": max(0, len(d.to_add) - limit),
            "update": max(0, len(d.to_update) - limit),
            "delete": max(0, len(d.to_delete) - limit),
            "renamed": max(0, len(d.renamed) - limit),
        },
    }


# --------------------------------------------------------------------------- 健康检查
@app.get("/api/check-prefix", dependencies=[Depends(auth)])
def check_prefix_api(sample: str = "") -> dict:
    """⭐ strm 前缀自检 —— 真发一次请求，确认配对正确。

    这是**最容易配错又最难查**的一项：alist 按挂载路径路由，前缀少写一层时
    它回 `storage not found` 但**状态码是 200**，看着不像错。所以做成主动自检。

    ⚠️ 优先拿**本地已生成的真实 strm 内容**去探 —— 那是最贴近实际的验证。
       没有本地 strm 时退回用假探针路径（也能判定配对，见 `health.py` 的说明）。
    """
    from .health import check_prefix
    cfg = load()
    rel = sample
    if not rel:
        rel = _first_local_rel(cfg)
    return check_prefix(cfg, sample_rel=rel).as_dict()


def _first_local_rel(cfg: Config) -> str:
    """从本地已有的 strm 里挑一条，取出它的相对路径当探针。"""
    try:
        out = Path(cfg.output_dir)
        if not out.exists():
            return ""
        for p in out.rglob("*.strm"):
            rel = str(p.relative_to(out))
            if rel.endswith(".strm"):
                rel = rel[:-5]
            return rel
    except Exception:
        pass
    return ""


@app.get("/api/detect-alist", dependencies=[Depends(auth)])
def detect_alist() -> dict:
    """从 alist 的公开接口探一下挂载结构，帮他填对前缀。

    只用 `/api/fs/list`（实测免认证可读）—— **不依赖 alist 管理员凭据**。
    """
    cfg = load()
    base = cfg.strm_prefix
    # 从 strm_prefix 反推 alist 根地址（砍掉 /d 之后的部分）
    root = base.split("/d/")[0] if "/d/" in base else base
    root = root.rstrip("/")

    import urllib.request
    out: dict = {"alist": root, "mounts": [], "hint": ""}

    def post(path: str, payload: dict) -> Any:
        req = urllib.request.Request(
            root + path, method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    try:
        d = post("/api/fs/list", {"path": "/", "password": "", "page": 1,
                                  "per_page": 0, "refresh": False})
    except Exception as exc:
        out["hint"] = f"连不上 alist：{exc}"
        return out

    if d.get("code") != 200:
        out["hint"] = f"alist 回了 {d.get('code')}：{d.get('message')}"
        return out

    for item in (d.get("data") or {}).get("content") or []:
        node: dict = {"path": "/" + item["name"], "name": item["name"], "is_dir": item.get("is_dir")}
        out["mounts"].append(node)
        # 下探一层，找 115 存储（影音/115影音 这种两级挂载很常见）
        if item.get("is_dir") and len(out["mounts"]) < 40:
            try:
                sub = post("/api/fs/list", {"path": node["path"], "password": "",
                                            "page": 1, "per_page": 0, "refresh": False})
                for s in (sub.get("data") or {}).get("content") or []:
                    out["mounts"].append({"path": node["path"] + "/" + s["name"],
                                          "name": s["name"], "is_dir": s.get("is_dir")})
            except Exception:
                pass
    out["hint"] = ("下面这些是 alist 根下能看到的路径。"
                   "★ strm 前缀应当是 `<alist 地址>/d/<115 存储的挂载路径>`，"
                   "例如 115 挂在 `/影音/115影音` ⇒ 前缀写 `"
                   + root + "/d/影音/115影音`。")
    return out


# --------------------------------------------------------------------------- 运行
@app.post("/api/run", dependencies=[Depends(auth)])
def run_now(body: RunIn) -> dict:
    """手动立即同步 —— 和定时任务跑的是**完全相同的一轮**。"""
    cfg = _cfg_for_run(body)
    runner = _runner()
    return runner.start(cfg, force_delete=bool(body.force_delete))


@app.get("/api/run", dependencies=[Depends(auth)])
def run_status(after: int = 0) -> dict:
    return _runner().snapshot(after=after)


@app.post("/api/run/stop", dependencies=[Depends(auth)])
def run_stop() -> dict:
    return _runner().stop()


# --------------------------------------------------------------------------- 挂起删除
@app.get("/api/pending", dependencies=[Depends(auth)])
def get_pending() -> dict:
    return _pending_info(load())


@app.post("/api/pending/apply", dependencies=[Depends(auth)])
def apply_pending() -> dict:
    """手动确认删除挂起的那些 —— 跳过等待时间，立刻删。"""
    if _runner().status.get("running"):
        raise HTTPException(409, "正在执行中 —— 先等它结束")
    cfg = load()
    s = Syncer(None, cfg)
    p = s.load_pending()
    if not p.paths:
        raise HTTPException(400, "没有挂起的删除")
    n = s.remove(p.paths)
    s.clear_pending()
    return {"ok": True, "deleted": n}


@app.post("/api/pending/cancel", dependencies=[Depends(auth)])
def cancel_pending() -> dict:
    """撤销挂起 —— 不删，并把清单清空。"""
    s = Syncer(None, load())
    p = s.load_pending()
    n = len(p.paths)
    s.clear_pending()
    return {"ok": True, "cancelled": n}


# --------------------------------------------------------------------------- 回执
@app.get("/api/runs", dependencies=[Depends(auth)])
def list_runs() -> dict:
    cfg = load()
    out: list[dict] = []
    for p in sorted((Path(cfg.data_dir) / "reports").glob("sync-*.json"), reverse=True)[:60]:
        item: dict = {"name": p.name, "stem": p.stem}
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            item.update({
                "started": d.get("started", ""), "elapsed": d.get("elapsed", ""),
                "dry_run": d.get("dry_run"), "written": d.get("written", 0),
                "deleted": d.get("deleted", 0), "source": d.get("source", ""),
                "requests": d.get("requests", 0),
            })
        except Exception:
            item["broken"] = True
        out.append(item)
    return {"items": out}


@app.get("/api/runs/{stem}", dependencies=[Depends(auth)])
def get_run(stem: str) -> dict:
    cfg = load()
    # ⛔ 文件名只允许 `sync-<时间戳>` 这一种形态 —— 这是唯一拿用户输入拼路径的地方，
    #    不锁死就等于开了个任意文件读取的口子（`../` 直接越出去）。
    if not stem.startswith("sync-") or "/" in stem or "\\" in stem or ".." in stem:
        raise HTTPException(400, "回执名不合法")
    base = Path(cfg.data_dir) / "reports"
    out: dict = {"stem": stem}
    for ext, key in (("json", "json"), ("md", "markdown"), ("detail.txt", "detail")):
        f = base / f"{stem}.{ext}"
        if f.exists():
            out[key] = (json.loads(f.read_text(encoding="utf-8")) if ext == "json"
                        else f.read_text(encoding="utf-8"))
    if len(out) == 1:
        raise HTTPException(404, "没有这份回执")
    return out


# --------------------------------------------------------------------------- 内部
def _cfg_for_run(body: RunIn) -> Config:
    """这一次执行用的配置。

    ⛔ `dry_run=False` 只对**这一次**生效 —— 不写回 `config.json`。
       否则「我明明只是试跑一次真跑，怎么以后每次都是真跑」迟早发生。
    """
    cfg = load()
    if body.dry_run is not None:
        cfg = cfg.replace(dry_run=bool(body.dry_run))
    return cfg


# --------------------------------------------------------------------------- 定时
_SCHED: dict = {"thread": None, "cron": "", "stop": threading.Event()}


def _cron_next(cron: str, now: datetime | None = None) -> datetime | None:
    """极简 cron 解析 —— 只要「分 时 日 月 周」五段，够用就好。

    ⚠️ 刻意不引 `croniter`（多一个依赖，而本工具只做「每天几点跑一次」这类需求）。
       支持 `*` 与具体数值、以及 `*/N`；不支持列表 / 区间（要那些就上 croniter）。
    """
    now = now or datetime.now()
    parts = cron.split()
    if len(parts) != 5:
        return None
    try:
        mins = _cron_field(parts[0], 0, 59)
        hours = _cron_field(parts[1], 0, 23)
        days = _cron_field(parts[2], 1, 31)
        months = _cron_field(parts[3], 1, 12)
        dows = _cron_field(parts[4], 0, 6)
    except ValueError:
        return None

    from datetime import timedelta
    t = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 * 366):
        if (t.minute in mins and t.hour in hours and t.day in days
                and t.month in months and t.weekday() in dows):
            return t
        t += timedelta(minutes=1)
    return None


def _cron_field(part: str, lo: int, hi: int) -> set[int]:
    if part == "*":
        return set(range(lo, hi + 1))
    if part.startswith("*/"):
        step = int(part[2:])
        if step <= 0:
            raise ValueError("step")
        return set(range(lo, hi + 1, step))
    v = int(part)
    if not (lo <= v <= hi):
        raise ValueError("range")
    return {v}


def start_scheduler() -> None:
    """起一个后台线程，按 `schedule_cron` 定时跑一轮。

    ✅ **跑的就是「立即同步」那一轮**：工具自己导目录树、自己对账、自己落盘。
       **你不需要每次手动导树，也不需要选任何模式。**

    ⚠️ （v0.1 这里写死了 `"tree"`，而当时的 tree 读的是你手动导入的那份文件 ⇒
        「目录树不更新、定时结果就不会变」，定时形同虚设。这是被改掉的那处。）
    ⚠️ （v0.2 这里读 `sync_mode` 配置；2026-10-08 去掉模式概念后一并删了 ——
        现在**只有一条路**，定时和手动不可能再走出两种行为。）
    """
    def loop() -> None:
        while not _SCHED["stop"].is_set():
            cfg = load()
            cron = (cfg.schedule_cron or "").strip()
            if not cron:
                _SCHED["stop"].wait(30)
                continue
            nxt = _cron_next(cron)
            if nxt is None:
                _SCHED["stop"].wait(300)
                continue
            wait = (nxt - datetime.now()).total_seconds()
            if wait > 0 and _SCHED["stop"].wait(min(wait, 3600)):
                continue
            if _SCHED["stop"].is_set():
                break
            try:
                if not _runner().status.get("running"):
                    _runner().log.add("info", f"定时触发（{cron}）⇒ 开始同步")
                    _runner().start(load())
            except Exception as exc:
                _runner().log.add("warning", f"定时触发失败：{exc}")
            _SCHED["stop"].wait(61)

    t = threading.Thread(target=loop, daemon=True)
    _SCHED["thread"] = t
    t.start()


# --------------------------------------------------------------------------- 启动
def serve(host: str = "0.0.0.0", port: int | None = None) -> None:
    import uvicorn
    port = int(port or os.environ.get("PORT") or 8767)
    if not ACCESS_TOKEN:
        print("⚠️ 没设 ACCESS_TOKEN —— 不加口令。仅建议在内网/本机这样用。")
    # 🔴 启动时一次性准备：把 `.env` 里遗留的「网页专属」参数（STRM_PREFIX）搬进
    #    `data/config.json`。不做的话升级后前缀会静默变空、下一轮直接跑不起来。
    from . import config as config_mod
    config_mod.bootstrap(print)
    start_scheduler()
    print(f"网页界面：http://127.0.0.1:{port}/")
    uvicorn.run(app, host=host, port=port)
