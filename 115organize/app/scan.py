"""在线目录扫描（功能 2 的「接口扫描」来源）。

## 为什么要有它

功能 2 让用户「选定几个文件夹」再整理。选定方式有两种：
  ① 目录树导出（离线，`build_plan_for_dirs` 直接吃树）
  ② **接口扫描** —— 没有树文件 / 树过期时，直接在 115 上逐层列目录给用户勾选。

## 防风控设计

⚠️ 扫描就是列目录，本质是「读」请求，但 `fs_files` 是 115 文档明说**被风控**的接口。
所以：
  · **懒加载** —— 一次只列用户展开的那一层（绝不递归全盘）；
  · **独立节流** —— `SCAN_THROTTLE_MIN/MAX`（默认 0.5~1.5s），比执行快，
    但也不会密集到触发风控。⚠️ 只影响扫描，不影响执行（执行仍走 `THROTTLE_*`）。
  · **单飞** —— 扫描与执行不能同时跑（都走节流器的同一份 rhythm）。

返回结构给前端树形组件用：
  { "id", "name", "is_dir", "has_children" }  —— 前端只对 is_dir 且 has_children 的展开。
"""
from __future__ import annotations

from typing import Any

from .config import Config
from .plan import RESERVED_DIRS
from .throttle import Throttle
from .v115 import V115, Node

__all__ = ["Scanner", "scan_level", "build_scan_throttle"]

MAX_PAGES = 200          # 一层最多翻这么多页（200 条/页 ⇒ 4 万项），防接口不认 offset


def build_scan_throttle(cfg: Config, log: Any = None) -> Throttle:
    """扫描专用节流器 —— 用一份「节流档 = 扫描档」的 cfg 副本，不跟执行共用 rhythm。

    ⚠️ `Throttle` 是 dataclass，节流值**全部从 cfg 读**（`throttle_min/max/batch/rest`），
       没有 `min_seconds` 这类参数。所以这里用 `cfg.replace()` 换掉节流档：
       扫描量小、影响小，`0.5~1.5s / 无长休 / 无时间窗`，比执行快但不密集。
       执行仍用原 `THROTTLE_*`（那份 cfg 不动）。
    """
    scan_cfg = cfg.replace(
        throttle_min=cfg.scan_throttle_min,
        throttle_max=cfg.scan_throttle_max,
        throttle_batch=10_000,       # 极大 ⇒ 永不触发长休（扫描量小，不需要）
        throttle_rest=0.0,
        window="",                   # 扫描不受运行时段限制
    )
    return Throttle(cfg=scan_cfg, log=log)


class Scanner:
    """在 115 上逐层列目录，供前端树形勾选。"""

    def __init__(self, v: V115, cfg: Config, log: Any = None):
        self.v = v
        self.cfg = cfg
        self.log = log or (lambda *a, **k: None)
        # 缓存的「目录 id → 子项」，同一层只列一次（翻页去重）
        self._children: dict[str, list[Node]] = {}

    def root(self) -> dict:
        """整理根的 id + 名字 —— 前端树的根节点。"""
        cid = self.v.root_cid()
        return {"id": cid, "name": self.cfg.root_path, "is_dir": True,
                "has_children": True, "path": ""}

    def children(self, cid: str, *, path: str = "") -> list[dict]:
        """列一层子项（含翻页），返回树节点。

        ⚠️ `path` 是相对整理根的路径（前端勾选后直接交给 `build_plan_for_dirs`）。
        """
        if cid not in self._children:
            self._children[cid] = self._list_all(cid)
        out: list[dict] = []
        for n in self._children[cid]:
            if n.name in RESERVED_DIRS:
                continue
            child_path = f"{path}/{n.name}" if path else n.name
            out.append({
                "id": n.id,
                "name": n.name,
                "is_dir": n.is_dir,
                "size": n.size,
                # 目录是否可展开：有子目录就算（前端懒加载，点了才真列）
                "has_children": n.is_dir,
                "path": child_path,
            })
        return out

    def cid_of(self, path: str) -> str:
        """相对整理根的路径 → 目录 id（逐级走，带缓存）。

        ⚠️ 前端懒加载只带 `path` 不带 `cid` —— 这里从根一路列下来定位，
           每层都是缓存命中的话零额外请求。
        """
        path = (path or "").strip("/")
        if not path:
            return self.root()["id"]
        cur = ""
        cid = self.root()["id"]
        for part in path.split("/"):
            cur = f"{cur}/{part}" if cur else part
            # 找 part 在 cid 下的子项
            hit = next((n for n in self._children.get(cid, []) if n.name == part), None)
            if hit is None:
                self._children[cid] = self._list_all(cid)
                hit = next((n for n in self._children[cid] if n.name == part), None)
            if hit is None or not hit.is_dir:
                raise ValueError(f"路径 {path!r} 在 115 上找不到：{part}")
            cid = hit.id
        return cid

    def _list_all(self, cid: str) -> list[Node]:
        out: list[Node] = []
        offset = 0
        limit = 200
        pages = 0
        while True:
            payload = {"cid": cid, "limit": limit, "offset": offset, "show_dir": 1}
            data = self.v._call("fs_files", payload)
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
                break
            if total is not None and offset >= int(total):
                break
            if pages >= MAX_PAGES:
                self.log("warning", f"列目录 {cid} 已翻 {pages} 页还没到底，先收手"
                                    f"（已取 {len(out)} 项，结果可能不全）")
                break
        return out


def scan_level(v: V115, cfg: Config, cid: str, *, path: str = "", log: Any = None) -> list[dict]:
    """函数式入口：列一层。"""
    return Scanner(v, cfg, log).children(cid, path=path)
