"""115 操作封装 —— 所有请求都从这里出去，**必须**经过节流器。

为什么集中到一层：
  ① 每个请求前统一 `throttle.before_request()`，不会有「漏掉某个接口没节流」这种事；
  ② 分页、错误归一化、冲突策略只写一遍；
  ③ 单测可以拿一个假 client 顶掉全部网络行为。

⚠️ 三条来自 p115client 文档原文的硬约束（照做，不是保守）：
  · `fs_files`  —— 「**此接口被风控**，此域名下的大量接口都会被风控」
                   ⇒ 只对**真要动的目录**调用，且单次最多回 200 条，必须翻页
  · `fs_move`   —— 「**请不要并发执行**」
  · `fs_delete` —— 「请不要并发执行，但不限制文件数」「删除和还原互斥」
  · `fs_move` 的 `conflict_policy` —— 文件默认 `replace`（**覆盖、不可恢复**）
                   ⇒ 本工具**永远显式传 `keep_both`**（重名就加 (1) 后缀），绝不覆盖
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .throttle import Throttle

__all__ = ["V115", "Node", "V115Error", "CookieMissing", "load_cookie", "MAX_PAGES"]

# 单层目录最多翻多少页（200 条/页 ⇒ 10 万项）。
# 本盘最厚的一层是 11,206 项（56 页），留足余量；设它只是防「接口不认 offset」把工具转死。
MAX_PAGES = 500


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
def load_cookie(cfg: Config) -> str:
    """按优先级找 cookie：环境变量 → `data/cookie.txt` → `data/accounts.json`（115offline 那份）。

    ⚠️ 兼容 `accounts.json` 是有意为之：两个容器可以挂同一个数据卷，
       cookie 只用维护一份（在 `115offline` 里扫码登录后，这里直接就能用）。
    """
    if cfg.cookie:
        return cfg.cookie.strip()
    data = Path(cfg.data_dir)

    f = data / "cookie.txt"
    if f.exists():
        text = f.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            return text

    acc = data / "accounts.json"
    if acc.exists():
        try:
            items = json.loads(acc.read_text(encoding="utf-8"))
        except Exception as exc:
            raise CookieMissing(f"{acc} 解析失败：{exc}") from exc
        if isinstance(items, dict):
            items = items.get("items") or items.get("accounts") or []
        for item in items or []:
            ck = (item or {}).get("cookie")
            if ck and "UID=" in ck.upper():
                return ck.strip()

    raise CookieMissing(
        "没找到 115 cookie。三种给法任选：\n"
        f"  ① 环境变量 P115_COOKIE=...\n"
        f"  ② 写进 {data / 'cookie.txt'}\n"
        f"  ③ 复用 115offline 的 {data / 'accounts.json'}（挂同一个数据卷即可）"
    )


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


# --------------------------------------------------------------------------- 主体
class V115:
    def __init__(self, client: Any, cfg: Config, throttle: Throttle, log: Any = None):
        self.client = client
        self.cfg = cfg
        self.throttle = throttle
        self.log = log or (lambda *a, **k: None)
        self.calls = 0
        self._targets: dict[str, str] = {}      # 相对路径 → 目录 id（建过的目标不再重复建）

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
           实测本盘 `云下载` 一层就有 11,206 项 ⇒ 不翻页会**静默丢掉 98%**。
        """
        out: list[Node] = []
        offset = 0
        limit = 200
        pages = 0
        while True:
            payload: dict[str, Any] = {"cid": cid, "limit": limit, "offset": offset,
                                       "show_dir": 1}
            if dirs_only:
                payload["nf"] = 1        # 115 原生「只要目录」，文件根本不会被拉回来
            data = self._call("fs_files", payload)
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
                # 兜底：`count` 缺失 + 接口不认 offset 的极端情况下，别把自己转死
                self.log("warning", f"列目录 {cid} 已翻 {pages} 页还没到底，先收手"
                                    f"（已取 {len(out)} 项，结果可能不全）")
                break
        return out

    def root_cid(self) -> str:
        """拿到整理根（如 `云下载`）的目录 id。"""
        if self._targets.get("") :
            return self._targets[""]
        resp = self._call("fs_dir_getid", {"path": self.cfg.root_path})
        cid = str((resp or {}).get("id") or (resp or {}).get("cid") or "")
        if not cid:
            # 兜底：从根目录列举里按名字找
            for node in self.list_dir("0", dirs_only=True):
                if node.name == self.cfg.root_path:
                    cid = node.id
                    break
        if not cid:
            raise V115Error(f"115 上找不到目录「{self.cfg.root_path}」")
        self._targets[""] = cid
        return cid

    def receive_list(self, limit: int = 100) -> list[dict]:
        """「最近接收」列表 —— 自动化巡检靠它发现新目录（115 没有 webhook，只能轮询）。

        ⚠️ id 口径跟 `list_dir` **必须一致**：目录取 `cid`、文件取 `fid`。
           115 的这套返回里**也有 `id` 字段**，但它对目录不是 cid ——
           直接取 `id` 会让后面 `fs_move` 拿着一个不存在的 fid 去移。
        """
        resp = self._call("fs_history_receive_list", {"limit": limit})
        items = (resp or {}).get("data") or (resp or {}).get("list") or []
        out = []
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            is_dir = not it.get("fid")
            out.append({
                "id": str((it.get("cid") if is_dir else it.get("fid"))
                          or it.get("id") or ""),
                "name": it.get("n") or it.get("name") or "",
                "time": it.get("t") or it.get("time") or "",
                "is_dir": is_dir,
            })
        return out

    # ---------------------------------------------------------------- 写
    def ensure_dir(self, rel_path: str) -> str:
        """确保 `root/rel_path` 存在（不存在则**逐级创建**），返回其 id。

        `fs_mkdir` 不建中间层，所以用 `fs_makedirs`（它是对 `fs_dir_getid2(is_create=1)` 的封装）。
        """
        rel = rel_path.strip("/")
        if not rel:
            return self.root_cid()
        if rel in self._targets:
            return self._targets[rel]
        parent = self.root_cid()
        cur = ""
        for part in rel.split("/"):
            cur = f"{cur}/{part}" if cur else part
            if cur in self._targets:
                parent = self._targets[cur]
                continue
            resp = self._call("fs_makedirs", {"path": part, "parent_id": parent})
            cid = str((resp or {}).get("id") or (resp or {}).get("cid") or "")
            if not cid:
                # 已存在时有的版本只回 state —— 回退到按路径查 id
                r2 = self._call("fs_dir_getid2", {"path": part, "parent_id": parent})
                cid = str((r2 or {}).get("id") or (r2 or {}).get("cid") or "")
            if not cid:
                raise V115Error(f"建目录失败：{cur}")
            self._targets[cur] = cid
            parent = cid
        return parent

    def _conflict_policy(self, fids: Iterable[str]) -> str:
        """⛔ 必须显式传 —— 不传的话**文件默认 replace（覆盖，不可恢复）**。
        本工具统一 `keep_both`：重名就自动加 (1)/(2) 后缀，绝不覆盖任何东西。"""
        return json.dumps({str(f): {"action": "keep_both"} for f in fids}, ensure_ascii=False)

    def move(self, fids: list[str], pid: str) -> bool:
        """移动（可一次多个，115 限制 5 万个以内）。⚠️ 不并发、不覆盖。"""
        if not fids:
            return True
        resp = self._call("fs_move", {
            "fid[]": list(fids),
            "pid": pid,
            "conflict_policy": self._conflict_policy(fids),
        })
        return _ok(resp)

    def rename(self, pairs: list[tuple[str, str]]) -> bool:
        """批量改名。`pairs` = [(file_id, 新名), ...]，一次请求改多个。"""
        if not pairs:
            return True
        payload = {f"files_new_name[{fid}]": name for fid, name in pairs}
        resp = self._call("fs_rename", payload)
        return _ok(resp)

    def delete(self, fids: list[str]) -> bool:
        """删除（**进 115 回收站**，可还原）。⚠️ 不并发、与还原互斥。"""
        if not fids:
            return True
        resp = self._call("fs_delete", {"fid[]": list(fids)})
        return _ok(resp)

    def move_check_conflict(self, fids: list[str], pid: str) -> dict:
        resp = self._call("fs_move_check_conflict", {"move_fids[]": list(fids), "pid": pid})
        return resp if isinstance(resp, dict) else {}


# --------------------------------------------------------------------------- 工厂
def build_client(cookie: str) -> Any:
    """造一个 P115Client。延迟 import —— 单测不装 p115client 也能跑除这层外的全部逻辑。"""
    from p115client import P115Client, check_response  # noqa: F401
    return P115Client(cookie)
