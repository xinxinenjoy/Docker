"""115 操作封装 —— 所有请求都从这里出去，**必须**经过节流器。

为什么集中到一层（照 115organize 的成熟做法）：
  ① 每个请求前统一 `throttle.before_request()`，不会有「漏掉某个接口没节流」；
  ② 分页、错误归一化只写一遍；
  ③ 单测可以拿一个假 client 顶掉全部网络行为。

⚠️ **你的网盘内容，这个模块一个字节都不改** —— 视频、目录、你自己的文件，全都不碰。
   它的写操作**只有两处，且都只作用于它自己为导目录树而造的那份临时件**：
     · `fs_export_dir`（`POST webapi.115.com/files/export_dir`）—— 造那份临时件
     · `fs_delete`（`POST webapi.115.com/rb/delete`）—— 删掉它，**进回收站、非永久删除**
   ⛔ 别把这里写成「纯粹的只读客户端」—— 那是不成立的（造/删临时件本身就是写）。
   2026-10-08 删掉事件流后，**唯一一个会碰你网盘设置的**写接口（「打开最近记录」）
   也随之删了 ⇒ 现在写操作就上面那两处，都在 `export_tree_bytes` 里面，且有 `keep=True` 兜底。

⚠️ 两条来自 p115client 文档的硬约束：
  · `fs_files` —— 「**此接口被风控**」 ⇒ 只在真要动的目录调，单次最多回 200 条，必须翻页
  · `fs_dir_getid` / `fs_dir_getid2` —— **id 位置不一样**（见 `_dir_id`）

## ⭐ 本模块还负责「让工具自己跟上网盘」的两件事

  ① **路径解析**（`dir_path`）—— 把目录 `cid` 还原成云端相对路径。
     ⭐ 关键：`/files` 的响应**本来就带**从根到该目录的祖先链（字段 `path`），
        所以列表过哪个目录、就顺手记住了它的路径，**零额外请求**。
  ② **自动导出目录树**（`export_tree_bytes`）—— 把「人工去网页点导出、再把 txt 丢进来」
     变成工具内部动作。**这是主路径**（见 `pipeline` 顶部）：每一轮都靠它拿「此刻最新」
     的结构，而不再需要你手动导树。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import EXPORT_LAYER_LIMIT, Config
from .throttle import Throttle

__all__ = ["V115", "Node", "V115Error", "CookieMissing", "load_cookie",
           "read_accounts", "account_of", "MAX_PAGES", "ROOT_DIR_NAME"]

# 单层目录最多翻多少页（200 条/页 ⇒ 10 万项）。防「接口不认 offset」把工具转死。
MAX_PAGES = 500

# 115 目录树里代表**网盘根**的内部节点名。拼路径时要滤掉它，
# 否则事件解析出来的路径会多一层 `根目录/`，跟 `remote_root` 对不上。
ROOT_DIR_NAME = "根目录"


class V115Error(RuntimeError):
    pass


class CookieMissing(V115Error):
    pass


@dataclass
class Node:
    """115 列表里的一项（目录的 id 在 cid，文件的在 fid —— 统一成 `id`）。"""
    id: str
    name: str
    is_dir: bool
    size: int = 0

    @property
    def ext(self) -> str:
        if "." not in self.name:
            return ""
        return self.name.rsplit(".", 1)[-1].lower()


# --------------------------------------------------------------------------- cookie
def read_accounts(path: Path) -> list[dict]:
    """读账号列表。**只读**，文件不在/坏了都返回空表（⛔ 不抛）。"""
    if not path.exists():
        return []
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(items, dict):
        items = items.get("items") or items.get("accounts") or []
    return [it for it in (items or []) if isinstance(it, dict)]


def account_of(cfg: Config) -> tuple[str, str]:
    """从账号文件里挑一个 → `(cookie, 账号名)`。

    ⚠️ 挑选口径：`ACCOUNT_ID` 指定了就**只**用它（指了却找不到 ⇒ 抛错，⛔ 不静默换人）；
       没指定 ⇒ 按文件顺序取**第一个 cookie 长得像样**的。
    """
    accs = read_accounts(Path(cfg.accounts_file))
    want = (cfg.account_id or "").strip()
    if want:
        for it in accs:
            if str(it.get("id") or "") == want or str(it.get("name") or "") == want:
                ck = str(it.get("cookie") or "").strip()
                if ck:
                    return ck, str(it.get("name") or it.get("id") or want)
        raise CookieMissing(
            f"账号文件里有 {len(accs)} 个账号，但没有 id/名字等于 {want!r} 的那个：\n"
            f"  {Path(cfg.accounts_file)}\n"
            f"  ⇒ 去网页「115 账号」里重选，或把「使用哪个账号」清空（= 用第一个能用的）"
        )
    for it in accs:
        ck = str(it.get("cookie") or "").strip()
        if _looks_like_cookie(ck):
            return ck, str(it.get("name") or it.get("id") or "(未命名)")
    return "", ""


def _looks_like_cookie(text: str) -> bool:
    """粗判一段文本是不是 115 cookie。⛔ 只看有没有 `UID=`，不校签名。"""
    return bool(text) and "UID=" in text.upper()


def load_cookie(cfg: Config) -> str:
    """按优先级找 cookie，并把「从哪来」记进 `cfg.cookie_source`（网页要显示）。

        ① 环境变量 `P115_COOKIE`  —— 显式指定，最高优先级（推荐留空）
        ② 账号文件 `ACCOUNTS_FILE` —— 🔗 在 115offline 扫码登录一次就能用（默认共用）
        ③ `DATA_DIR/cookie.txt`   —— 兜底（手抓的死凭据）
    """
    if cfg.cookie:
        return _remember(cfg, cfg.cookie.strip(), "环境变量 P115_COOKIE")

    ck, who = account_of(cfg)
    if ck:
        return _remember(cfg, ck, f"账号文件 {who}（{Path(cfg.accounts_file)}）")

    f = Path(cfg.data_dir) / "cookie.txt"
    if f.exists():
        text = f.read_text(encoding="utf-8", errors="replace").strip()
        if _looks_like_cookie(text):
            return _remember(cfg, text, f"cookie.txt（{f}）")

    raise CookieMissing(
        "没找到 115 cookie。推荐第 ① 种 —— 只维护一份，还能自动续期：\n"
        f"  ① 在 115offline 里扫码登录一次（它的账号文件：{Path(cfg.accounts_file)}）\n"
        f"     ⚠️ 两个容器要挂同一个数据卷，或用 ACCOUNTS_FILE 把这份文件指过来\n"
        f"  ② 环境变量 P115_COOKIE=…（临时用；过期不会自己更新）\n"
        f"  ③ 手抓一份写进 {f}（兜底路子）"
    )


def _remember(cfg: Config, cookie: str, source: str) -> str:
    try:
        object.__setattr__(cfg, "cookie_source", source)
    except Exception:
        pass
    return cookie


# --------------------------------------------------------------------------- 响应判定
def _ok(resp: Any) -> bool:
    """判定 115 响应成功。`check_response` 只在明确失败时抛，但有的接口是软失败。"""
    if not isinstance(resp, dict):
        return bool(resp)
    if "state" in resp:
        return resp.get("state") in (True, 1, "1")
    return not resp.get("error")


def _msg(resp: Any) -> str:
    if isinstance(resp, dict):
        return str(resp.get("error_msg") or resp.get("message") or resp.get("error") or "")
    return str(resp)


def _dir_id(resp: Any) -> str:
    """从目录类接口的返回里取目录 id。

    ⚠️ **两个接口把 id 放在不同地方**（115organize 真机实测，不是猜的）：

        fs_dir_getid   GET /files/getid       → {"state":true, "id":"<目录 id>"}
                                                ⇒ 顶层 `id`
        fs_dir_getid2  GET /files/get_path_id → {"state":true, "data":{"file_id":"3533…"}}
                                                ⇒ **`data.file_id`**，顶层没有 `id`

    ⛔ 曾经只按顶层 `id`/`cid` 取 ⇒ `ensure_dir()` 两级都拿不到 id ⇒ 全量 1,183 条 mkdir 全失败。
    """
    if not isinstance(resp, dict):
        return ""
    data = resp.get("data")
    if isinstance(data, dict):
        got = data.get("file_id") or data.get("cid") or data.get("id")
        if got:
            return str(got)
    return str(resp.get("id") or resp.get("cid") or "")


def _export_id_of(resp: Any) -> str:
    """从「提交导出任务」的响应里取任务 id。

    ⚠️ 115 有时把它放顶层、有时塞进 `data` —— 两个都认，别只认一个。
    """
    if not isinstance(resp, dict):
        return ""
    for src in (resp, resp.get("data")):
        if isinstance(src, dict):
            got = src.get("export_id") or src.get("exportId")
            if got:
                return str(got)
    return ""


# --------------------------------------------------------------------------- 主体
class V115:
    """115 只读客户端。⚠️ 本类**不含任何写接口**。"""

    def __init__(self, client: Any, cfg: Config, throttle: Throttle, log: Any = None):
        self.client = client
        self.cfg = cfg
        self.throttle = throttle
        self.log = log or (lambda *a, **k: None)
        self.calls = 0
        self._dir_ids: dict[str, str] = {}       # 相对路径 → 目录 id
        self._paths: dict[str, str] = {}         # 目录 id → 相对**网盘根**的路径（含自身名）

    # ---------------------------------------------------------------- 请求出口
    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """**唯一**的请求出口。所有方法都必须走这里，保证节流不被绕过。"""
        self.throttle.before_request()
        self.calls += 1
        fn = getattr(self.client, name)
        try:
            resp = fn(*args, **kwargs)
        except Exception as exc:
            self.throttle.fail(f"{name}: {exc}")
            raise V115Error(f"{name} 失败：{exc}") from exc
        if isinstance(resp, dict) and resp.get("error"):
            self.throttle.fail(f"{name}: {_msg(resp)}")
            raise V115Error(f"{name} 被 115 拒绝：{_msg(resp)}")
        self.throttle.ok()
        return resp

    # ---------------------------------------------------------------- 读
    def list_dir(self, cid: str, dirs_only: bool = False) -> list[Node]:
        """列一层目录（**自动翻页**）。

        ⚠️ 翻页必须做：`fs_files` 单次最多回 200 条（传 500 也只给 200）。
           本盘 `云下载` 一层就有 11,206 项 ⇒ 不翻页会**静默丢掉 98%**。
        """
        out: list[Node] = []
        offset = 0
        limit = 200
        pages = 0
        while True:
            payload: dict[str, Any] = {"cid": cid, "limit": limit, "offset": offset, "show_dir": 1}
            if dirs_only:
                payload["nf"] = 1        # 115 原生「只要目录」，文件根本不会被拉回来
            data = self._call("fs_files", payload)
            if offset == 0:
                # ⭐ 顺手记住这个目录的祖先链 —— 响应里本来就带（字段 `path`），
                #    零额外请求。事件流要靠它把 cid 还原成路径。
                self._remember_path(cid, data)
            raw = [n for n in ((data or {}).get("data") or []) if isinstance(n, dict)]
            for node in raw:
                is_dir = not node.get("fid")
                out.append(Node(
                    id=str(node.get("cid") if is_dir else node.get("fid") or ""),
                    name=node.get("n") or node.get("name") or "",
                    is_dir=is_dir,
                    size=int(node.get("s") or 0),
                ))
            total = (data or {}).get("count")
            offset += len(raw)
            pages += 1
            if not raw:
                break                    # 回空了 = 到头了
            if total is not None and offset >= int(total):
                break                    # `count` 是总数，有它就不猜
            if pages >= MAX_PAGES:
                self.log("warning", f"列目录 {cid} 已翻 {pages} 页还没到底，先收手"
                                    f"（已取 {len(out)} 项，结果可能不全）")
                break
        return out

    def dir_id(self, path: str) -> str:
        """把「相对网盘根的路径」解析成目录 id（逐级走，带缓存）。

        ⚠️ id 在 `fs_dir_getid` 的**顶层 `id`**；`fs_dir_getid2` 在 **`data.file_id`**
           —— `_dir_id()` 两个都认。
        """
        path = (path or "").strip("/")
        if not path:
            return "0"                                  # 网盘根
        if path in self._dir_ids:
            return self._dir_ids[path]

        parent = "0"
        cur = ""
        for part in path.split("/"):
            cur = f"{cur}/{part}" if cur else part
            if cur in self._dir_ids:
                parent = self._dir_ids[cur]
                continue
            resp = self._call("fs_dir_getid", {"path": part, "cid": parent})
            cid = _dir_id(resp)
            if not cid:
                # 兜底：从父目录列举里按名字找
                for node in self.list_dir(parent, dirs_only=True):
                    if node.name == part:
                        cid = node.id
                        break
            if not cid:
                raise V115Error(f"115 上找不到目录：{path}（卡在 {part}）")
            self._dir_ids[cur] = cid
            parent = cid
        return parent

    def root_id(self) -> str:
        """同步根目录的 id（解析一次就缓存，`dir_id` 自己带 memo）。"""
        return self.dir_id(self.cfg.remote_root)

    def stat_by_id(self, fid: str) -> dict | None:
        """单查一个文件/目录（`fs_file`）。

        ⚠️ 为什么不用列表：115 的**目录列表带缓存**（响应里有 `use_cache`），
           而且删除是**异步**的 ⇒ 「列表里还在」不等于「没删掉」。
           要判真值就单查。
        """
        resp = self._call("fs_file", {"fid": fid})
        if isinstance(resp, dict) and resp.get("data"):
            return resp["data"]
        return None

# --------------------------------------------------------------------------- 路径解析
    def _remember_path(self, cid: str, resp: Any) -> None:
        """把 `/files` 响应顶层的 `path`（祖先链）记进缓存。

        ⭐ **这是「零额外请求解析路径」的关键**：115 的列表接口**本来就回**一份
           「从根到该目录」的完整目录树 —— 字段 `path`，元素 `{cid,pid,name}`，
           **最后一项就是被列的那个目录自己**。
           （p115client 的 `tool/attr.update_resp_ancestors` 读的也是这个字段。）

        ⚠️ 要滤掉 `根目录` 这个 115 的内部节点：不滤的话事件解析出来的路径会多一层
           `根目录/`，跟配置里的 `remote_root` 对不上，范围判断会全部落空。
        """
        if not isinstance(resp, dict):
            return
        nodes = resp.get("path")
        cid_s = str(cid or "").strip()
        if not cid_s:
            return
        if not isinstance(nodes, list) or not nodes:
            return
        names: list[str] = []
        for it in nodes:
            if not isinstance(it, dict):
                continue
            nm = str(it.get("name") or "").strip()
            if not nm or nm == ROOT_DIR_NAME:
                continue           # 空名 / 「根目录」不是真实目录层
            names.append(nm)
        self._paths[cid_s] = "/".join(names)

    def dir_path(self, cid: str) -> str | None:
        """目录 id → 它相对**网盘根**的完整路径（**含目录自身名**）。

        返回值三态（调用方必须分清）：
            `""`    —— 网盘根
            `"a/b"` —— 正常
            `None`  —— **拿不到**（cid 无效 / 指向的是文件 / 接口拒绝了）

        ⚠️ 为什么单发一次 `limit=1` 的请求、而不是复用 `list_dir`：
           只要响应里那个 `path` 字段，一页一条就够；`list_dir` 会把整个目录翻完，
           大目录（本盘 `云下载` 一层 11,206 项）白烧几十个请求。
        """
        cid_s = str(cid or "").strip()
        if not cid_s or cid_s == "0":
            return ""
        if cid_s in self._paths:
            return self._paths[cid_s]
        try:
            resp = self._call("fs_files",
                              {"cid": cid_s, "limit": 1, "offset": 0, "show_dir": 1})
        except V115Error as exc:
            self.log("debug", f"解析目录路径失败 cid={cid_s}：{exc}")
            return None
        self._remember_path(cid_s, resp)
        return self._paths.get(cid_s)

    def to_sync_rel(self, full: str) -> str | None:
        """网盘路径（相对网盘根）→ **相对同步根**的相对路径。

        `None` 表示**这条变化不在同步范围内**（比如你往 `云下载` 传了个片）。
        返回 `""` 表示就是同步根本身。
        """
        full = (full or "").strip("/")
        root = (self.cfg.remote_root or "").strip("/")
        if not root:
            return full
        if full == root:
            return ""
        if full.startswith(root + "/"):
            return full[len(root) + 1:]
        return None

    def in_scope(self, rel: str) -> bool:
        """这条相对路径在不在「只处理这些子目录」的白名单里（空白名单 = 全要）。"""
        incs = self.cfg.includes
        if not incs:
            return True
        rel = (rel or "").strip("/")
        return any(rel == inc or rel.startswith(inc + "/") for inc in incs)

    # ---------------------------------------------------------------- 自动导出目录树
    def export_tree_bytes(self, *, layer_limit: int = EXPORT_LAYER_LIMIT, timeout: float = 900.0,
                          poll: float = 2.0, keep: bool = False) -> bytes:
        """让 115 **自己**导出一份目录树，取回原始字节。

        ⭐ 这一步把「人工去网页点导出、再把 txt 丢进 `tree/`」变成**工具内部动作**。
           这正是「目录树只用于首次全量」这个定位能成立的前提 ——
           否则每次全量都要你手动导一份，工具就只是个树转换器。

        🔴 `layer_limit` **必须给足** —— 红领巾 2026-10-08 的实测教训：
           他手动导那份树时层级选太少，很多子目录压根没被导出，在上层表现为
           「既没子项、又没扩展名」的叶子条目，被上一版**误判成空目录**。
           ⇒ 这里默认取 115 取值域的上限（`config.EXPORT_LAYER_LIMIT = 25`），
             不再依赖 115 那个没有公开说明的服务端默认值。详见该常量的注释。

        ⚠️ 115 的硬约束（p115client 原文照抄，不是推测）：
           · 【导出目录树】任务**不可并发、不可中止**
           · **空目录不会被导出**
        ⚠️ 导出会在**网盘根**留一个临时件（`根目录<时间戳>_目录树.txt`）。
           `keep=False`（默认）时工具取回后**顺手把它删掉**，不会在盘上堆积。

        ⚠️ 返回的是**原始字节**，不是 str：115 导出的是 UTF-16，
           交给 `treefile.parse_file` 那套按 BOM 自动识别的读法去解，
           在这里 decode 等于把那一层的编码判断绕过去了。
        """
        payload: dict[str, Any] = {"file_ids": str(self.root_id()), "target": "U_1_0"}
        if 0 < layer_limit <= 25:
            payload["layer_limit"] = layer_limit
        resp = self._call("fs_export_dir", payload)
        eid = _export_id_of(resp)
        if not eid:
            raise V115Error(f"提交「导出目录树」任务失败，115 回了：{resp!r}")
        self.log("info", f"已让 115 自己导出目录树（任务 #{eid}）—— 等它跑完，大库要几分钟")

        info: dict = {}
        t0 = time.time()
        while True:
            st = self._call("fs_export_dir_status", {"export_id": eid})
            data = (st or {}).get("data") if isinstance(st, dict) else None
            if isinstance(data, dict) and (data.get("pick_code") or data.get("file_id")):
                info = data
                break
            code = int((data or {}).get("status") or 0) if isinstance(data, dict) else 0
            if code < 0:
                raise V115Error(f"「导出目录树」任务 #{eid} 被 115 判为失败：{st!r}")
            if time.time() - t0 > timeout:
                raise V115Error(
                    f"「导出目录树」任务 #{eid} 等了 {timeout:.0f} 秒还没好。\n"
                    f"  ⚠️ 115 的导出任务**不可并发、不可中止**；若它卡住了，"
                    f"要么等 115 自己超时，要么去网页版手动取消。")
            time.sleep(poll)

        pick = str(info.get("pick_code") or "")
        fid = str(info.get("file_id") or "")
        if not pick and fid:
            got = self.stat_by_id(fid) or {}
            pick = str(got.get("pc") or got.get("pick_code") or "")
        if not (pick or fid):
            raise V115Error(f"导出完成了，但拿不到 file_id / pick_code：{info!r}")

        # 🔴 必须把 `download_url` 返回的 **P115URL 对象原样**交给 `_read_export` ——
        #    它身上挂着直链要求的请求头（尤其那把 CDN 临时 cookie）；
        #    转成 str 就全丢了，而**那正是 403 的根源**（2026-10-08 实测）。
        # ⚠️ 用 file_id 取链（上游 `export_dir_parse_iter` 的写法）；
        #    `download_url` 的参数本来就「id 或提取码都可以」，两个都有时优先 id。
        url = self._call("download_url", fid or pick, app="web")
        raw = self._read_export(url)
        self.log("info", f"已取回目录树（{len(raw):,} 字节）")

        if not keep and fid:
            try:
                self._call("fs_delete", [fid])
                self.log("info", "已顺手删掉 115 上那份临时导出件")
            except Exception as exc:
                self.log("warning", f"删临时导出件失败（不影响结果）：{exc}")
        return raw

    def _read_export(self, url: Any) -> bytes:
        """把「导出目录树」产生的文件读回来。

        🔴 2026-10-08 22:54 实跑踩到 403，当晚用**只读探针**做三组对照才定的因
           —— 改这里之前先读完这段，别凭直觉猜（我自己就先猜错过一次）：

        115 给导出件的直链带 `f=3`。这个参数的含义（p115client 文档原文）：
          · `f=1` ⇒ 下载时要带 **user-agent**，且**必须与「请求直链时」那个一致**
          · `f=3` ⇒ 还要带 **Cookie —— 但指的是「请求直链时的响应所返回的 Set-Cookie」**，
                    **不是账号 cookie**。那是 CDN 现场签发的临时 cookie（约 15 分钟过期）。

        它随直链一起挂在 `P115URL.headers` 上，实测长这样：:

            {"user-agent": "",
             "cookie": "aca176a…=4b0fbf…; expires=…; path=/6ac7…/; domain=115.com"}

        ⚠️ 注意那个 cookie **不是** `UID=…` 那串账号 cookie。

        ⛔ 下面两种写法**都必然 403**（探针实测，A / C 两组）：
           · 裸 urllib + **账号** cookie + 自选浏览器 UA
           · 裸 urllib + **账号** cookie + 「与取链一致」的 UA
             ⚠️ 后一组是**特意验的**：UA 对齐也救不了 —— 病因是 **cookie**，不是 UA。
                （本函数旧注释写「空 UA 直接 403」，那是**只看到了表象**，已订正。）

        ✅ 正解 = 照上游 `export_dir_parse_iter` 的写法交给 `client.open(P115URL)`——
           它会把 `P115URL.headers`（含那把 CDN cookie）自动附到请求上。

        ⚠️ 这里**故意不走 `self._call`**：`open` 打的是 115 的 **CDN 直链**，
           不是 webapi 接口 ⇒ 不该占用（也不该受）节流配额。
        """
        if not url:
            raise V115Error("导出件的下载地址是空的")
        try:
            resp = self.client.open(url)
            try:
                return resp.read()
            finally:
                resp.close()
        except Exception as exc:
            raise V115Error(f"下载导出件失败：{type(exc).__name__}: {exc}") from exc

# --------------------------------------------------------------------------- 工厂
def build_client(cookie: str) -> Any:
    """造一个 P115Client。延迟 import —— 单测不装 p115client 也能跑其余逻辑。"""
    from p115client import P115Client
    return P115Client(cookie)
