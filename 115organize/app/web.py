"""网页界面 —— 改参数 / 看计划 / **勾一部分先跑**。

## 这个模块的定位

⛔ 它不是「第二个实现」。业务规则（怎么识别番号、什么算垃圾、怎么节流）全都在别的模块里，
本模块只做三件事：**解析请求 → 调库 → 回话**。所以这里看不到任何业务判据。

## 为什么值得有个网页

`115organize` 原本只有 CLI，参数走环境变量。但：
  · 参数有快 30 个，改一个要动 `.env` + 重建容器 —— 试错成本高到让人不想试；
  · 第一次全量要动约 4,900 个对象、**不可逆** ⇒ 必须能「先勾一小批跑跑看」。

⇒ 于是有「参数落 `config.json`、网页上改」和「执行页勾选」这两件事。

## 三条安全约定

1. **口令**：`ACCESS_TOKEN` 为空则不启用（适合内网/本机）；设了就要求 `Bearer`。
   与 `115offline` 同名同语义 —— 一个口令管两个服务。
2. **勾选执行永远 `resume=False`**：网页跑的是子集，pass 计数跟整份计划不是一回事，
   断点文件也**另存一份**（`web-state.json`）⇒ 绝不会污染命令行的断点。
3. **停得掉**：节流器的 `sleeper` 被换成「分小段睡 + 查停止标志」，
   按下停止最多 0.25 秒就跳出 —— 否则一次长休 60 秒、一次冷却 900 秒，
   按钮看着像没反应（这类「点了没反应」最伤人）。
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

from . import report as report_mod
from . import settings as settings_mod
from . import treefile
from .config import Config, load
from .executor import Executor
from .inbox import build_inbox_plan, list_inbox
from .plan import Plan, build_plan, build_plan_for_dirs, estimate
from .scan import Scanner, build_scan_throttle
from .throttle import Throttle
from .v115 import CookieMissing, V115, build_client, load_cookie, read_accounts

__all__ = ["app", "serve"]

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# ⚠️ 与 115offline 用**同一个环境变量名** —— 一个口令管两个服务，不用记两套。
ACCESS_TOKEN = (os.environ.get("ACCESS_TOKEN") or "").strip()


class Cancelled(Exception):
    """用户按了停止（⛔ 不是错误，回执里要说成「已停止」而不是「失败」）。"""


# --------------------------------------------------------------------------- 鉴权
def auth(authorization: str | None = Header(default=None)) -> None:
    """`ACCESS_TOKEN` 为空则不启用口令（适合内网 / 本机）。"""
    if not ACCESS_TOKEN:
        return
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if token != ACCESS_TOKEN:
        raise HTTPException(401, "访问口令错误")


app = FastAPI(title="115 网盘整理", docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------- 日志环
class LogBuffer:
    """网页上要能看见「正在干什么」。

    用**带序号的环形缓冲**而不是直读日志文件：前端只要带上次的 `seq` 来问增量，
    就既能实时刷新、又不会每次重传几百行。
    """

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

    def tail_lines(self, n: int = 60) -> list[str]:
        with self._lock:
            return [f"{x['t']} {x['level'].upper():7} {x['msg']}" for x in list(self._items)[-n:]]


# --------------------------------------------------------------------------- 执行器
class Runner:
    """后台跑一轮执行 —— 网页不能同步等 2–3 小时。

    只允许**一个**在跑：115 的 `fs_move` / `fs_delete` 文档原文就是「请不要并发执行」，
    而且两边并发会让节流器的节奏失效。
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.log = LogBuffer()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._cancel_flag = {"hit": False}
        self._max_requests: int | None = None
        self.ex: Executor | None = None
        self.status: dict = self._blank()

    @staticmethod
    def _blank() -> dict:
        return {
            "running": False, "stage": "", "stage_label": "",
            "planned": 0, "selected": 0, "total_in_plan": 0, "max_requests": None,
            "done": 0, "failed": 0, "requests": 0, "source": "",
            "dry_run": True, "started": "", "finished": "",
            "result": None, "error": "", "stopping": False, "cancelled": False,
        }

    # ---- 供 Executor 注入的两个钩子 -------------------------------------
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
                self._cancel_flag["hit"] = True
                raise Cancelled()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.25, left))

    # ---- 启停 ------------------------------------------------------------
    def start(self, cfg: Config, sub: Plan, *, total_in_plan: int,
              max_requests: int | None = None, source: str = "plan") -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise HTTPException(409, "已经有一轮在跑了 —— 等它结束或先按停止")
            self._stop.clear()
            self._cancel_flag["hit"] = False
            self._max_requests = max_requests
            self.log = LogBuffer()
            self.status = self._blank()
            self.status.update({
                "running": True, "dry_run": bool(cfg.dry_run),
                "selected": len(sub.ops), "total_in_plan": total_in_plan,
                "max_requests": max_requests, "source": source,
                "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            self._thread = threading.Thread(target=self._work, args=(cfg, sub, source), daemon=True)
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
    def _work(self, cfg: Config, sub: Plan, source: str = "plan") -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            label = {"plan": "勾选执行", "inbox": "入站口整理", "for_dirs": "整理当前目录"}.get(
                source, source)
            self.log.add("info", f"开始{label}：{len(sub.ops)} 条动作"
                                 f"（整份计划 {self.status['total_in_plan']} 条）"
                                 f"；DRY_RUN={'开' if cfg.dry_run else '关'}")

            # ⭐ 演练**不查 cookie、不建 client** —— 演练的用处正是「还没配好账号时先看看
            #    这份计划长什么样」。为了演练去要一个 cookie 是本末倒置。
            client = None
            if cfg.dry_run:
                self.log.add("info", "演练模式 —— 一个请求都不会发出去（也就不需要账号）")
            else:
                cookie = load_cookie(cfg)
                self.log.add("info", f"账号来源：{getattr(cfg, 'cookie_source', '(未知)')}")
                client = build_client(cookie)

            throttle = Throttle(cfg=cfg, sleeper=self._sleeper, log=self._log)
            v = V115(client, cfg, throttle, self._log)
            ex = _WatchingExecutor(v, cfg, self._log, on_pass=self._on_pass,
                                   state_name="web-state.json")
            with self._lock:
                self.ex = ex

            result = ex.run(sub, resume=False, max_requests=self._max_requests)
            self._finish(result, stamp, source=source)
        except Cancelled:
            self._finish(None, stamp, cancelled=True, source=source)
        except CookieMissing as exc:
            self._fail("没找到 115 cookie", str(exc))
        except Exception as exc:                      # 兜底：别让线程静默死掉
            self._fail(f"{type(exc).__name__}", str(exc))

    def _on_pass(self, name: str, count: int) -> None:
        label = {"mkdir": "建目录", "rename": "改名", "move": "移动", "trash": "清理"}.get(name, name)
        with self._lock:
            self.status["stage"] = name
            self.status["stage_label"] = label
            self.status["planned"] = count

    def _finish(self, result: dict | None, stamp: str, *, cancelled: bool = False,
                source: str = "plan") -> None:
        """收尾。

        🔴 **顺序不能换**：先把回执和最后几行日志都落地，**最后**才把 `running` 翻成 False。
           反过来的话，前端一看到 `running=False` 就停止轮询，
           而「回执已写出」「本轮结束」这两行是在那之后才写进缓冲的
           ⇒ 页面上永远看不到它们（看着像「跑完了但没写回执」，其实写了）。
        """
        if result:
            try:
                out = Path(self.data_dir) / "reports"
                out.mkdir(parents=True, exist_ok=True)
                prefix = {"inbox": "inbox", "for_dirs": "dirs"}.get(source, "run")
                (out / f"{prefix}-{stamp}.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
                (out / f"{prefix}-{stamp}.md").write_text(
                    report_mod.render_run_markdown(result), encoding="utf-8")
                self.log.add("info", f"回执已写出：reports/{prefix}-{stamp}.md")
            except Exception as exc:
                self.log.add("warning", f"回执写入失败（不影响结果）：{exc}")
        self.log.add("info", "本轮结束" + ("（已停止）" if cancelled else ""))

        with self._lock:
            if result:
                self.status.update({
                    "done": result.get("done", 0), "failed": result.get("failed", 0),
                    "requests": result.get("requests", 0), "result": result,
                })
            self.status.update({
                "running": False, "stage": "", "stage_label": "",
                "stopping": False, "cancelled": cancelled, "source": source,
                "finished": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            if cancelled:
                self.status["error"] = "已按你的要求停止（已经发出去的那些改完了，剩下的没动）"

    def _fail(self, kind: str, msg: str) -> None:
        with self._lock:
            self.status.update({"running": False, "stage": "", "stage_label": "",
                                "stopping": False, "error": f"{kind}：{msg}",
                                "finished": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        self.log.add("error", f"{kind}：{msg}")

    def snapshot(self, after: int = 0) -> dict:
        with self._lock:
            st = dict(self.status)
            ex = self.ex
        if ex is not None and st["running"]:
            # 活进度：`done` 与 `requests` 是执行器自己累加的，读一下就有
            st["done"] = max(st.get("done", 0), ex.done)
            st["failed"] = max(st.get("failed", 0), len(ex.errors))
            st["requests"] = max(st.get("requests", 0), getattr(ex.v, "calls", 0))
        logs = self.log.since(after)
        st["log_seq"] = logs["seq"]
        st["log"] = logs["items"]
        return st


class _WatchingExecutor(Executor):
    """只加一件事：告诉外面「现在跑到哪个 pass 了」。

    ⚠️ 靠解析日志字符串来判阶段是不行的 —— 文案一改就静默失效（本项目已经因为
       「复检字符串跟实际输出不一致」踩过一次）。这里用回调，改文案不影响它。
    """

    def __init__(self, *args, on_pass=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_pass = on_pass or (lambda *_: None)

    def _run_pass(self, name: str, ops: list, max_requests) -> None:
        self._on_pass(name, len(ops))
        return super()._run_pass(name, ops, max_requests)


# --------------------------------------------------------------------------- 计划缓存
class PlanStore:
    """计划解析一次要几秒（真实目录树 7 万条），缓存在内存里，键 = 树文件 + 参数。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.plan: Plan | None = None
        self.meta: dict = {}
        self._key: tuple | None = None

    def tree_file(self, cfg: Config) -> Path:
        return Path(treefile.pick_tree_file(cfg.tree_dir, cfg.tree_file))

    def build(self, cfg: Config, *, depth: int = 2, force: bool = False) -> dict:
        path = self.tree_file(cfg)
        try:
            stat = path.stat()
            key = (str(path), stat.st_mtime_ns, stat.st_size, depth,
                   cfg.root_path, cfg.series_min, cfg.series_enable, cfg.series_rename,
                   cfg.movie_enable, cfg.episode_group_enable, cfg.episode_group_min,
                   cfg.paren, cfg.junk_dup_min)
        except OSError as exc:
            raise HTTPException(400, f"树文件读不到：{path} —— {exc}") from exc

        with self._lock:
            if not force and self._key == key and self.plan is not None:
                return self.meta
            tree = treefile.parse_file(path)
            plan = build_plan(tree, cfg, max_depth=depth)
            self.plan, self._key = plan, key
            st = plan.tree_stats
            self.meta = {
                "built": True,
                "created": plan.created,
                "root": plan.root,
                "tree_file": str(path),
                "tree_file_time": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "counts": plan.counts(),
                "estimate": estimate(plan, cfg),
                # 这份计划是**按哪套参数**算出来的 —— 参数改了之后要能一眼看出来
                # 「现在这堆动作不是按我新调的值算的，得重新生成」。
                "settings": plan.config,
                "tree_stats": {"dirs": st.get("dirs", 0), "files": st.get("files", 0),
                               "root_entries": st.get("root_entries", 0)},
            }
            return self.meta

    def get(self) -> Plan | None:
        with self._lock:
            return self.plan

    def build_for_dirs(self, cfg: Config, dirs: list[str], *, depth: int = 3,
                       force: bool = False) -> dict:
        """选定文件夹计划（功能 2）—— 只处理勾选的目录。

        缓存键 = 树文件 + 参数 + **勾选的目录集合**（换一批文件夹就是另一份计划）。
        """
        path = self.tree_file(cfg)
        try:
            stat = path.stat()
        except OSError as exc:
            raise HTTPException(400, f"树文件读不到：{path} —— {exc}") from exc

        dirs_key = tuple(sorted(set(dirs or [])))
        key = (str(path), stat.st_mtime_ns, stat.st_size, depth,
               "for_dirs", dirs_key,
               cfg.root_path, cfg.series_min, cfg.series_enable, cfg.series_rename,
               cfg.movie_enable, cfg.episode_group_enable, cfg.episode_group_min,
               cfg.paren, cfg.junk_dup_min, cfg.junk_allow_dir)

        with self._lock:
            if not force and self._key == key and self.plan is not None:
                return self.meta
            tree = treefile.parse_file(path)
            plan = build_plan_for_dirs(tree, cfg, dirs, max_depth=depth)
            self.plan, self._key = plan, key
            st = plan.tree_stats
            self.meta = {
                "built": True,
                "created": plan.created,
                "root": plan.root,
                "tree_file": str(path),
                "tree_file_time": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "mode": "for_dirs",
                "dirs": list(dirs_key),
                "counts": plan.counts(),
                "estimate": estimate(plan, cfg),
                "settings": plan.config,
                "tree_stats": {"dirs": st.get("dirs", 0), "files": st.get("files", 0),
                               "root_entries": st.get("root_entries", 0)},
            }
            return self.meta

    def reset(self) -> None:
        """丢掉缓存 —— 单测换目录时用；正常运行中不需要（key 变了会自己重建）。"""
        with self._lock:
            self.plan, self.meta, self._key = None, {}, None


PLANS = PlanStore()
RUNNER: Runner | None = None


def _runner() -> Runner:
    """取（必要时重建）执行器。

    ⚠️ `DATA_DIR` 变了就重建 —— 否则单测里换一个临时目录之后，
       日志和回执会写到上一个目录去（症状是「东西不知道去哪了」）。
    """
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


class PlanIn(BaseModel):
    depth: int = 2
    force: bool = False


class ForDirsIn(BaseModel):
    dirs: list[str] = Field(default_factory=list)
    depth: int = 3
    force: bool = False


class ScanIn(BaseModel):
    cid: str = ""
    path: str = ""


class InboxActionIn(BaseModel):
    dry_run: bool | None = None
    max_requests: int | None = None


class RunIn(BaseModel):
    # `None` = 整份计划全跑；给了数组 = 只跑勾中的那几条（索引按 `/api/plan/ops` 的顺序）
    indices: list[int] | None = None
    # `None` = 跟随配置里的 DRY_RUN；显式给 True/False 只影响**这一次**
    dry_run: bool | None = None
    max_requests: int | None = None


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


# --------------------------------------------------------------------------- 设置
@app.get("/api/settings", dependencies=[Depends(auth)])
def get_settings() -> dict:
    cfg = load()
    return settings_mod.describe(cfg, Path(cfg.data_dir))


@app.put("/api/settings", dependencies=[Depends(auth)])
def put_settings(body: SettingsIn) -> dict:
    cfg = load()
    res = settings_mod.save_json(Path(cfg.data_dir), body.values)
    # ⚠️ 保存后必须重读：`config._env()` 有惰性缓存，不重读这次改动要等下次进程重启才生效。
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
@app.get("/api/accounts", dependencies=[Depends(auth)])
def get_accounts() -> dict:
    cfg = load()
    path = Path(cfg.accounts_file)
    items = read_accounts(path)

    # 每个账号探一下「用的这个 cookie 还活着吗」是**要发请求**的 —— 页面一进来就探头像
    # 会平白多几个请求。所以这里只读不算，真有效性等执行时由接口自己说话。
    active = ""
    for it in items:
        ck = str(it.get("cookie") or "")
        if ck and "UID=" in ck.upper():
            active = str(it.get("id") or "")
            break

    return {
        "file": str(path),
        "exists": path.exists(),
        "active_id": (cfg.account_id or "").strip() or active,
        "picked_id": (cfg.account_id or "").strip(),
        "items": [{
            "id": str(it.get("id") or ""),
            "name": str(it.get("name") or it.get("id") or "(未命名)"),
            "cookie_tail": ("…" + str(it.get("cookie") or "")[-8:]) if it.get("cookie") else "",
            "usable": bool(str(it.get("cookie") or "")) and
                      "UID=" in str(it.get("cookie") or "").upper(),
        } for it in items],
    }


# --------------------------------------------------------------------------- 目录树
@app.get("/api/tree", dependencies=[Depends(auth)])
def get_tree() -> dict:
    cfg = load()
    try:
        path = Path(treefile.pick_tree_file(cfg.tree_dir, cfg.tree_file))
        stat = path.stat()
    except Exception as exc:
        return {"ok": False, "error": f"树文件读不到：{exc}", "dir": str(cfg.tree_dir)}
    return {
        "ok": True, "file": str(path), "dir": str(cfg.tree_dir),
        "name": path.name, "size_mb": round(stat.st_size / 1048576, 1),
        "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "root_path": cfg.root_path,
    }


@app.get("/api/tree/level", dependencies=[Depends(auth)])
def tree_level(path: str = "") -> dict:
    """从**目录树文件**离线取一层的子项 —— 「选定文件夹」的离线来源。

    ⚠️ 与 `/api/scan` 的区别：这里读树文件、**零请求**；`/api/scan` 在线列目录。
       `path` 相对整理根；空 = 整理根的一级子项。
    """
    cfg = load()
    try:
        tfile = treefile.pick_tree_file(cfg.tree_dir, cfg.tree_file)
        tree = treefile.parse_file(tfile).rebase(cfg.root_path)
    except Exception as exc:
        raise HTTPException(400, f"树文件读不到：{exc}") from exc

    base = (path or "").strip("/")
    # ⚠️ 树 rebase 后根节点路径是 `root_path` 本身 —— 空 path = 根的子项，
    #    要传 `cfg.root_path` 而不是 `""`（`children_dirs("")` 会返回根自己）。
    root = cfg.root_path
    if not base:
        base = root
    if base and base not in tree.entries:
        raise HTTPException(400, f"树里没有这个目录：{base}")
    items = []
    for d in tree.children_dirs(base):
        rel = d if d != root else ""
        items.append({"id": "", "name": tree.entries[d].name, "is_dir": True,
                      "has_children": True, "path": rel, "size": 0})
    for f in tree.files_of(base):
        items.append({"id": "", "name": f, "is_dir": False,
                      "has_children": False,
                      "path": f"{base}/{f}" if base != root else f, "size": 0})
    return {"node": {"id": "", "name": cfg.root_path,
                     "is_dir": True, "has_children": True, "path": path or ""},
            "items": items}


# --------------------------------------------------------------------------- 计划
@app.post("/api/plan", dependencies=[Depends(auth)])
def make_plan(body: PlanIn) -> dict:
    cfg = load()
    if _runner().status.get("running"):
        raise HTTPException(409, "正在执行中 —— 生成新计划会改掉正在跑的那份，先等它结束")
    return PLANS.build(cfg, depth=max(1, min(4, body.depth)), force=body.force)


@app.get("/api/plan", dependencies=[Depends(auth)])
def get_plan() -> dict:
    if PLANS.plan is None:
        return {"built": False, "hint": "还没生成 —— 点「生成计划」"}
    return PLANS.meta


@app.get("/api/plan/ops", dependencies=[Depends(auth)])
def plan_ops(
    offset: int = 0,
    limit: int = Query(default=100, le=500),
    kind: str = "",
    group: str = "",
    auto: str = "",
    q: str = "",
    target: str = "",
    only_indices: bool = False,
) -> dict:
    """计划条目（分页 + 过滤）—— 前端勾选就靠它。

    ⚠️ 过滤在**后端**做：整份计划约 5,000 条，一次全推到浏览器再筛
       （还带搜索）会让页面卡顿，而且每次翻页都要重传。

    `only_indices=1` ⇒ 只回「匹配当前过滤的全部索引」，不回明细。
    给「选中全部筛选结果」用 —— 否则前端只能勾到当前这一页，而用户以为是全部。
    """
    plan = PLANS.get()
    if plan is None:
        raise HTTPException(400, "还没生成计划")

    kinds = {k for k in kind.split(",") if k}
    groups = {g for g in group.split(",") if g}
    needle = q.strip().lower()

    picked: list[tuple[int, Any]] = []
    for i, op in enumerate(plan.ops):
        if kinds and op.kind not in kinds:
            continue
        if groups and op.group not in groups:
            continue
        if auto == "1" and not op.auto:
            continue
        if auto == "0" and op.auto:
            continue
        if target and target not in (op.target or ""):
            continue
        if needle and needle not in f"{op.path} {op.target} {op.new_name}".lower():
            continue
        picked.append((i, op))

    head = {
        "total": len(picked),
        "all": len(plan.ops),
        "offset": offset,
        "limit": limit,
        "kinds": plan.by_kind(),
        "groups": plan.by_group(),
    }
    if only_indices:
        return {**head, "indices": [i for i, _op in picked]}

    page = picked[offset:offset + limit]
    return {
        **head,
        "items": [{
            "i": i, "kind": op.kind, "group": op.group, "auto": op.auto,
            "path": op.path, "target": op.target, "new_name": op.new_name,
            "reason": op.reason, "note": op.note, "text": op.describe(),
        } for i, op in page],
    }


@app.post("/api/plan/estimate", dependencies=[Depends(auth)])
def plan_estimate(body: RunIn) -> dict:
    """勾选之后重新估一次 —— 勾 50 条和勾 5,000 条是完全两回事。"""
    plan = PLANS.get()
    if plan is None:
        raise HTTPException(400, "还没生成计划")
    cfg = _cfg_for_run(body)
    sub = _select(plan, body.indices)
    out = estimate(sub, cfg)
    out["selected"] = len(sub.ops)
    out["auto"] = sum(1 for op in sub.ops if op.auto)
    out["manual"] = len(sub.ops) - out["auto"]
    return out


# --------------------------------------------------------------------------- 扫描（功能 2）
@app.get("/api/scan", dependencies=[Depends(auth)])
def scan_dir(cid: str = "", path: str = "") -> dict:
    """懒加载列一层 —— 前端树形勾选「选定文件夹」用。

    ⚠️ 扫描走**独立节流**（`SCAN_THROTTLE_*`，默认 0.5~1.5s）—— 量小、影响小，
       但也不会密集到触发风控。`cid` 空 = 按 `path` 解析（前端懒加载只带 path）。
    """
    if _runner().status.get("running"):
        raise HTTPException(409, "正在执行中 —— 扫描会跟执行抢节奏，先等它结束")
    cfg = load()
    try:
        cookie = load_cookie(cfg)
    except CookieMissing as exc:
        raise HTTPException(400, f"需要 cookie 才能扫描：{exc}") from exc

    throttle = build_scan_throttle(cfg)
    v = V115(build_client(cookie), cfg, throttle, lambda lvl, msg: None)
    scanner = Scanner(v, cfg)
    try:
        if not cid:
            cid = scanner.cid_of(path or "")
        items = scanner.children(cid, path=path or "")
        name = (path or "").rsplit("/", 1)[-1] or cfg.root_path
        return {"node": {"id": cid, "name": name,
                         "is_dir": True, "has_children": True, "path": path or ""},
                "items": items}
    except Exception as exc:
        raise HTTPException(502, f"扫描失败：{exc}") from exc


# --------------------------------------------------------------------------- 入站口
@app.get("/api/inbox", dependencies=[Depends(auth)])
def inbox_status() -> dict:
    """入站口当前状态：目录名 / 里面有多少项 / 轮询间隔。"""
    cfg = load()
    return {
        "dir": cfg.inbox_dir,
        "root": cfg.root_path,
        "poll_interval": cfg.inbox_poll_interval,
        "max_requests": cfg.inbox_max_requests or None,
        "dry_run": cfg.dry_run,
        "running": _runner().status.get("running", False),
    }


@app.post("/api/inbox/run", dependencies=[Depends(auth)])
def inbox_run(body: InboxActionIn) -> dict:
    """手动「整理当前目录」—— 处理入站口里的全部内容。

    ⚠️ 演练（DRY_RUN）时**仍要列一次入站口**（列目录是读请求）：
       「整理当前目录」的价值就是看清「会怎么处理」，不列就什么都看不到。
       但演练**绝不做任何写操作**（移动/清理/建目录都不发）。
    """
    if _runner().status.get("running"):
        raise HTTPException(409, "已经有一轮在跑了 —— 等它结束或先按停止")
    cfg = load()
    if body.dry_run is not None:
        cfg = cfg.replace(dry_run=bool(body.dry_run))
    runner = _runner()

    # 演练时也要 cookie（要列目录），但只读不写
    try:
        cookie = load_cookie(cfg)
    except CookieMissing as exc:
        raise HTTPException(400, f"需要 cookie 才能读入站口：{exc}") from exc
    throttle = Throttle(cfg=cfg, sleeper=runner._sleeper, log=runner._log)
    v = V115(build_client(cookie), cfg, throttle, runner._log)
    plan = build_inbox_plan(v, cfg)
    if not plan.ops and not plan.skipped:
        raise HTTPException(400, f"入站口 `{cfg.inbox_dir}` 里没有可处理的内容")
    return runner.start(cfg, plan, total_in_plan=len(plan.ops),
                        max_requests=body.max_requests, source="inbox")


# --------------------------------------------------------------------------- 选定文件夹
@app.post("/api/plan/for-dirs", dependencies=[Depends(auth)])
def plan_for_dirs(body: ForDirsIn) -> dict:
    """选定文件夹 → 生成计划（功能 2）。目录来自目录树（离线）。"""
    if _runner().status.get("running"):
        raise HTTPException(409, "正在执行中 —— 生成新计划会改掉正在跑的那份，先等它结束")
    if not body.dirs:
        raise HTTPException(400, "没选文件夹 —— 先在左侧勾选要整理的目录")
    cfg = load()
    return PLANS.build_for_dirs(cfg, body.dirs, depth=body.depth, force=body.force)


@app.post("/api/run/for-dirs", dependencies=[Depends(auth)])
def run_for_dirs(body: ForDirsIn) -> dict:
    """「整理当前目录」—— 对勾选的文件夹生成计划并执行。

    ⚠️ 一键语义：没生成计划时**自动生成**（不用先手动点「生成计划」）。
    """
    if _runner().status.get("running"):
        raise HTTPException(409, "已经有一轮在跑了 —— 等它结束或先按停止")
    cfg = load()
    if not body.dirs:
        # 前端没传 dirs：用上次生成的 for_dirs 计划（勾选状态保持）
        if PLANS.plan is not None and PLANS.meta.get("mode") == "for_dirs":
            plan = PLANS.plan
        else:
            raise HTTPException(400, "没选文件夹 —— 先在左侧勾选要整理的目录")
    else:
        meta = PLANS.build_for_dirs(cfg, body.dirs, depth=body.depth or 3)
        plan = PLANS.plan
    runner = _runner()
    return runner.start(cfg, plan, total_in_plan=len(plan.ops),
                        max_requests=None, source="for_dirs")


# --------------------------------------------------------------------------- 执行
@app.post("/api/run", dependencies=[Depends(auth)])
def run_now(body: RunIn) -> dict:
    plan = PLANS.get()
    if plan is None:
        raise HTTPException(400, "还没生成计划 —— 先点「生成计划」")
    cfg = _cfg_for_run(body)
    sub = _select(plan, body.indices)
    if not sub.ops:
        raise HTTPException(400, "一条都没勾 —— 先在列表里勾要处理的条目")
    runner = _runner()
    return runner.start(cfg, sub, total_in_plan=len(plan.ops), max_requests=body.max_requests)


@app.get("/api/run", dependencies=[Depends(auth)])
def run_status(after: int = 0) -> dict:
    return _runner().snapshot(after=after)


@app.post("/api/run/stop", dependencies=[Depends(auth)])
def run_stop() -> dict:
    return _runner().stop()


# --------------------------------------------------------------------------- 回执
@app.get("/api/runs", dependencies=[Depends(auth)])
def list_runs() -> dict:
    cfg = load()
    out: list[dict] = []
    for p in sorted((Path(cfg.data_dir) / "reports").glob("run-*.json"), reverse=True)[:60]:
        item: dict = {"name": p.name, "stem": p.stem}
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            item.update({
                "started": d.get("started", ""), "elapsed": d.get("elapsed_human", ""),
                "dry_run": d.get("dry_run"), "planned": d.get("planned", 0),
                "done": d.get("done", 0), "failed": d.get("failed", 0),
                "requests": d.get("requests", 0),
            })
        except Exception:
            item["broken"] = True
        out.append(item)
    return {"items": out}


@app.get("/api/runs/{stem}", dependencies=[Depends(auth)])
def get_run(stem: str) -> dict:
    cfg = load()
    # ⛔ 文件名只允许 `run-<时间戳>` 这一种形态 —— 这里是唯一一处拿用户输入拼路径的地方，
    #    不锁死就等于开了个任意文件读取的口子（`../` 直接越出去）。
    if not stem.startswith("run-") or "/" in stem or "\\" in stem or ".." in stem:
        raise HTTPException(400, "回执名不合法")
    base = Path(cfg.data_dir) / "reports"
    js, md = base / f"{stem}.json", base / f"{stem}.md"
    out: dict = {"stem": stem}
    if js.exists():
        out["json"] = json.loads(js.read_text(encoding="utf-8"))
    if md.exists():
        out["markdown"] = md.read_text(encoding="utf-8")
    if not out.get("json") and not out.get("markdown"):
        raise HTTPException(404, "没有这份回执")
    return out


# --------------------------------------------------------------------------- 总览
@app.get("/api/state", dependencies=[Depends(auth)])
def state() -> dict:
    """页面首屏一次拿齐 —— ⛔ 别让前端进来就并发打 6 个接口（首屏会明显发顿）。"""
    cfg = load()
    out: dict = {
        "auth": bool(ACCESS_TOKEN),
        "port": int(os.environ.get("PORT") or 8766),
        "root_path": cfg.root_path,
        "data_dir": str(cfg.data_dir),
        "config_file": str(settings_mod.config_path(cfg.data_dir)),
        "dry_run": cfg.dry_run,
        "accounts_file": str(cfg.accounts_file),
    }

    try:
        ck = load_cookie(cfg)
        out["cookie"] = {"ok": True, "source": getattr(cfg, "cookie_source", ""),
                         "tail": "…" + ck[-8:]}
    except CookieMissing as exc:
        out["cookie"] = {"ok": False, "error": str(exc)}

    out["tree"] = get_tree()
    out["plan"] = PLANS.meta if PLANS.plan is not None else {"built": False}
    out["run"] = _runner().snapshot(after=0)
    return out


# --------------------------------------------------------------------------- 内部
def _cfg_for_run(body: RunIn) -> Config:
    """这一次执行用的配置。

    ⛔ `dry_run=False` 只对**这一次**生效 —— 不写回 `config.json`。
       否则「我明明只是试跑一次真跑，怎么以后每次都是真跑」这种事迟早发生。
    """
    cfg = load()
    if body.dry_run is not None:
        cfg = cfg.replace(dry_run=bool(body.dry_run))
    return cfg


def _select(plan: Plan, indices: list[int] | None) -> Plan:
    return plan if indices is None else plan.subset(indices)


# --------------------------------------------------------------------------- 启动
def serve(host: str = "0.0.0.0", port: int | None = None) -> None:
    import uvicorn
    port = int(port or os.environ.get("PORT") or 8766)
    if not ACCESS_TOKEN:
        print("⚠️ 没设 ACCESS_TOKEN —— 不加口令。仅建议在内网/本机这样用。")
    print(f"网页界面：http://127.0.0.1:{port}/")
    uvicorn.run(app, host=host, port=port)
