"""tidy —— 整理当前目录（红领巾 2026-10-06 定：「先做整理当前目录的工具」）。

## 目标

把 `云下载` 里的**一级文件夹**逐个处理：
  1. 识别/纠错番号（`manipzz-137` → `IPZZ-137`）
  2. 查女优（离线表 + 在线兜底，缓存不重复请求）
  3. 归类（有女优 → `涩涩存档/<女优>/<番号>/`；查不到 → `涩涩存档/<系列>/<番号>/`）
  4. 批处理：**每次 3–5 个目录**，批间长休，防反爬
  5. 一个目录 = 「建目录 + 移动 + 改名」一次完成，不分步

## 与 executor 的差别

executor 是「全盘计划 → 四个 pass 批量执行」；tidy 是「**当前目录 → 小批量 → 一步到位**」。
tidy 复用 V115（节流/keep_both/断点），但**不建大 plan** —— 每批现列现处理。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .metadb import MetaDB
from .metascan import MetaScan
from . import number

__all__ = ["Tidy", "TidyResult", "classify", "plan_one"]

# 识别不出的名字（连番号都没有）—— 不硬动，留给人工
_NO_ID = "无法识别"


@dataclass
class TidyResult:
    """一批 tidy 处理的回执。"""
    started: str = ""
    finished: str = ""
    batch_size: int = 0
    planned: int = 0
    done: int = 0
    failed: int = 0
    requests: int = 0
    skipped: list[str] = field(default_factory=list)   # 没被处理的（含无法识别）
    entries: list[dict] = field(default_factory=list)  # 每个目录的处理明细

    def as_dict(self) -> dict:
        return {k: (list(v) if isinstance(v, list) else v)
                for k, v in vars(self).items()}


def classify(avid: str, actress: str, *, fallback: str = _NO_ID) -> str:
    """决定一个番号归到哪：
    · 有女优 → `<女优>/<番号>`
    · 没女优 → `<系列>/<番号>`
    ⛔ 空番号 → `_无法识别`（不硬归类）
    """
    avid = (avid or "").strip().upper()
    if not avid:
        return f"{fallback}"
    actress = (actress or "").strip()
    if actress:
        return f"{actress}/{avid}"
    series = number.series_of(avid) or "未知系列"
    return f"{series}/{avid}"


def plan_one(name: str, *, db: MetaDB | None = None,
             scan: MetaScan | None = None) -> dict:
    """单个目录的整理方案（不执行）。

    返回：
        {"name": 原名, "avid": 番号, "actress": 女优, "maker": 片商,
         "target": 相对目标根的子路径, "action": "move_and_rename"|"skip"}
    """
    avid = number.get_id(name)
    if not avid:
        return {"name": name, "avid": "", "actress": "", "maker": "",
                "target": _NO_ID, "action": "skip", "reason": "番号识别不出"}

    actress, maker = "", ""
    if db is not None:
        got = db.lookup(avid)
        if got:
            actress = got.get("actress", "")
            maker = got.get("maker", "")
    if not actress and scan is not None:
        # 离线没给女优 → 在线兜底（内部会缓存）
        meta = scan.resolve(avid)
        actress = meta.get("actress", "")
        maker = meta.get("maker", "") or maker

    rel = classify(avid, actress)
    return {"name": name, "avid": avid, "actress": actress, "maker": maker,
            "target": rel, "action": "move_and_rename" if rel != _NO_ID else "skip",
            "reason": ""}


class Tidy:
    """tidy 引擎。小批量、一步到位、批间长休。"""

    def __init__(self, v: Any, cfg: Config, *, log: Any = None,
                 db: MetaDB | None = None, scan: MetaScan | None = None):
        self.v = v
        self.cfg = cfg
        self.log = log or (lambda *a, **k: None)
        self.db = db or MetaDB(cfg.data_dir, log)
        self.scan = scan or MetaScan(self.db, log=log)
        self._listing: dict[str, dict[str, Any]] = {}
        self._dircache: dict[str, str] = {}
        self.done = 0
        self.failed = 0

    # ------------------------------------------------------------ 读 115
    def _list_of(self, cid: str) -> dict[str, Any]:
        if cid not in self._listing:
            self._listing[cid] = {n.name: n for n in self.v.list_dir(cid)}
        return self._listing[cid]

    def _resolve_dir(self, rel: str) -> str:
        rel = rel.strip("/")
        if not rel:
            return self.v.root_cid()
        if rel in self._dircache:
            return self._dircache[rel]
        cid = self.v.ensure_dir(rel)          # 不存在则逐级建
        self._dircache[rel] = cid
        return cid

    def _find(self, rel_dir: str, name: str, *, want_dir: bool | None = None) -> Any:
        cid = self._resolve_dir(rel_dir)
        node = self._list_of(cid).get(name)
        if node is None or (want_dir is not None and node.is_dir != want_dir):
            return None
        return node

    # ------------------------------------------------------------ 批处理
    def run_batch(self, names: list[str], *, dry_run: bool | None = None) -> TidyResult:
        """处理一批目录（3–5 个）。`dry_run=None` ⇒ 用 cfg.dry_run。"""
        dry = self.cfg.dry_run if dry_run is None else dry_run
        res = TidyResult(started=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         batch_size=len(names))
        src_root = self.cfg.root_path
        tgt_root = self.cfg.tidy_target
        if not dry:
            self.v.root_cid()                    # 提前问一次根 id（dry 时不发）

        for name in names:
            if name in (self.cfg.inbox_dir, "_待清理", "_整理日志", "_整理报告"):
                res.skipped.append(f"{name}（保留目录）")
                continue
            node = self._find(src_root, name, want_dir=True)
            if node is None:
                res.skipped.append(f"{name}（源不存在或不是目录）")
                continue

            plan = plan_one(name, db=self.db, scan=self.scan)
            res.planned += 1
            res.entries.append(plan)

            if plan["action"] == "skip":
                res.skipped.append(f"{name}（{plan['reason']}）")
                continue

            avid = plan["avid"]
            # 一步到位：目标子路径 = 女优/番号 或 系列/番号
            target_rel = f"{tgt_root}/{plan['target']}"
            new_name = f"{avid}"
            target_dir = plan["target"].rsplit("/", 1)[0]   # 女优 或 系列

            if dry:
                self.log("info", f"[dry-run] {name} → {tgt_root}/{plan['target']}（改名 {new_name}）")
                self.done += 1
                continue

            try:
                self._apply_one(node, name, target_rel, target_dir, new_name)
                self.done += 1
            except Exception as exc:
                self.failed += 1
                self.log("error", f"处理失败 {name}: {exc}")
                res.failed += 1

        res.finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        res.requests = self.v.calls
        self.db.flush()
        return res

    def _apply_one(self, node: Any, name: str, target_rel: str,
                   target_dir: str, new_name: str) -> None:
        """建目录 + 移动 + 改名 —— 一次完成（不分步）。

        ⛔ 改名和移动要分开发请求（115 没有「移动同时改名」的原生操作），
           但逻辑上属于一个目录的一次处理：先建目标目录，再移过去，再改名。
        """
        # ① 确保目标目录存在（女优/系列层）
        tgt_cid = self._resolve_dir(target_rel)
        # ② 把源目录移进目标（keep_both，重名并存）
        self.v.move([node.id], tgt_cid)
        self._listing.pop(tgt_cid, None)
        # ③ 移动后改名成规范番号（目标内重名自动加后缀，由 115 处理）
        self.v.rename([(node.id, new_name)])
