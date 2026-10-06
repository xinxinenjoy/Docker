"""执行器 —— 把计划落到 115 上，全程走节流器。

## 为什么要「分批归并」而不是一条条发

计划的动作数是 6000+，但**115 的接口大多支持一次带多个对象**：
  · `fs_move`   一次可以带 5 万个 fid，但**不能并发**
  · `fs_rename` 一次可以带多组 (id, 新名)
  · `fs_delete` 「不限制文件数」，但**不能并发**
所以按「同类 + 同目标」归并后，请求数能压到原来的 1/6 左右 ——
**这是最直接的风控减负**：请求越少越不像脚本，也越省时间。

⚠️ 但归并有个前提：**顺序不能乱**。所以固定四个 pass：
    ① mkdir  先把目标目录建齐（否则后面移进去会落到不存在的 id，115 会静默丢节点）
    ② rename 改名（`fs_move` 不会顺带改名，得单独做）
    ③ move   按目标目录分组搬
    ④ trash  清理（隔离或删除）
每个 pass 独立断点，中途挂了重跑不会重复动手。

## 三条安全线

1. **`dry_run` 打开时，一次请求都不发** —— 只在日志里把要做的说出来。
2. **任何移动都显式 `keep_both`** —— 115 对文件的默认策略是 `replace`（覆盖、不可恢复）。
3. **`auto=False` 的动作一律不执行**（冲突项），并在回执里说明数量。
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Config
from .plan import Op, Plan
from .v115 import V115, V115Error

__all__ = ["Executor"]

# 单次 `fs_rename` 带多少组。115 文档没说上限，这里保守取 100 —— 改名请求比移动更重。
RENAME_CHUNK = 100
# 单次 `fs_move` / `fs_delete` 带多少 fid
MOVE_CHUNK = 500
PASSES = ("mkdir", "rename", "move", "trash")

# 🔴 动作 → pass 的**白名单映射**。必须逐个列全，**不许写 `else "trash"` 这种兜底**：
#    曾经就是那么写的，结果 `move_dir` / `move_file` 没被认出来、整批掉进 `trash` 桶 ——
#    真跑时 2,782 个番号目录会被当垃圾搬进 `_待清理/`，`JUNK_ACTION=delete` 档下就是**直接删**。
#    这类错误不可逆，所以宁可「不认识就不执行」。
PASS_OF = {
    "mkdir": "mkdir",
    "rename": "rename",
    "move_dir": "move",
    "move_file": "move",
    "trash": "trash",
}


class Executor:
    def __init__(self, v: V115, cfg: Config, log: Any = None):
        self.v = v
        self.cfg = cfg
        self.log = log or (lambda *a, **k: None)
        self.root_cid = ""
        self._listing: dict[str, dict[str, Any]] = {}     # cid → {name: Node}
        self._dircache: dict[str, str] = {}               # 相对路径 → cid
        self.errors: list[dict] = []
        self.done = 0
        self.state_path = Path(cfg.data_dir) / "run-state.json"
        self.state: dict = {"passes": {p: 0 for p in PASSES}, "updated": ""}

    # ------------------------------------------------------------------ 基础设施
    def _load_state(self) -> None:
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data.get("passes"), dict):
                    self.state = data
                    self.log("info", f"发现断点：{data.get('updated')} → {data['passes']}")
            except Exception:
                pass

    def _save_state(self) -> None:
        self.state["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
        except Exception as exc:
            self.log("warning", f"断点写入失败（不影响执行）：{exc}")

    def _listing_of(self, cid: str) -> dict[str, Any]:
        """列目录并按名字索引（**按 cid 缓存**，同一目录只列一次）。"""
        if cid not in self._listing:
            nodes = self.v.list_dir(cid)
            self._listing[cid] = {n.name: n for n in nodes}
        return self._listing[cid]

    def _resolve_dir(self, rel: str) -> str:
        """相对路径 → 目录 id（不存在就建）。"""
        rel = rel.strip("/")
        if not rel:
            return self.root_cid
        if rel in self._dircache:
            return self._dircache[rel]
        # 逐级确保存在（ensure_dir 内部有缓存，重复调用零请求）
        cid = self.v.ensure_dir(rel)
        self._dircache[rel] = cid
        return cid

    def _find(self, rel_dir: str, name: str, *, want_dir: bool | None = None) -> Any:
        """在 `root/rel_dir` 里按名字找一项。"""
        cid = self._resolve_dir(rel_dir)
        node = self._listing_of(cid).get(name)
        if node is None or (want_dir is not None and node.is_dir != want_dir):
            return None
        return node

    # ------------------------------------------------------------------ 主流程
    def run(self, plan: Plan, *, resume: bool = True, max_requests: int | None = None) -> dict:
        started = time.time()
        started_at = datetime.now()
        if self.cfg.dry_run:
            # ⛔ dry-run **一个请求都不发**（对账口径：`v.calls` 必须保持 0）。
            #    连 `root_cid()` 都不问 —— 它能少发一个就少发一个。
            self.log("info", "DRY_RUN=1 —— 只记录、不发请求（连根目录 id 都不查）")
        else:
            self.root_cid = self.v.root_cid()
        if resume:
            self._load_state()
        else:
            self.state = {"passes": {p: 0 for p in PASSES}, "updated": ""}

        ops = plan.ops
        auto_ops = [op for op in ops if op.auto]
        manual = len(ops) - len(auto_ops)

        self.log("info", f"根目录 id={self.root_cid or '(dry-run 未查)'}；计划 {len(ops)} 条，"
                         f"其中可自动 {len(auto_ops)} 条、人工 {manual} 条")

        groups: dict[str, list[Op]] = {p: [] for p in PASSES}
        unknown: list[Op] = []
        for op in auto_ops:
            bucket = PASS_OF.get(op.kind)
            if bucket is None:
                unknown.append(op)
                continue
            groups[bucket].append(op)
        if unknown:
            self.log("error", f"{len(unknown)} 条动作的 kind 不认识（{sorted({o.kind for o in unknown})}）"
                              f" —— ⛔ **不予执行**（不认识就不动手，比猜错强）")
        try:
            for p in PASSES:
                self._run_pass(p, groups[p], max_requests)
                if max_requests and self.v.calls >= max_requests:
                    self.log("warning", f"达到请求上限 {max_requests}，提前收工")
                    break
        except _StopRun:
            pass                      # 已达上限：正常收工，断点已存
        finally:
            self._save_state()

        finished_at = datetime.now()
        elapsed = time.time() - started
        return {
            "started": started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "finished": finished_at.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_human": str(timedelta(seconds=int(elapsed))),
            "dry_run": bool(self.cfg.dry_run),
            "planned": len(auto_ops),
            "done": self.done,
            "failed": len(self.errors),
            "requests": self.v.calls,
            "by_kind": self._by_kind(groups),
            "errors": self.errors,
            "throttle": self.v.throttle.stats.as_dict(),
            "skipped_manual": manual,
            "unroutable": len(unknown),
        }

    def _pass_of(self, op: Op) -> str:
        """留着给外部按 pass 归类用（如报告）。⚠️ 未知 kind 返回 `""`，**不是** "trash"。"""
        return PASS_OF.get(op.kind, "")

    def _by_kind(self, groups: dict[str, list[Op]]) -> dict[str, int]:
        out = defaultdict(int)
        for p, lst in groups.items():
            out[p] += min(len(lst), self.state["passes"].get(p, 0))
        return dict(out)

    # ------------------------------------------------------------------ 各 pass
    def _run_pass(self, name: str, ops: list[Op], max_requests: int | None) -> None:
        if not ops:
            return
        done_before = self.state["passes"].get(name, 0)
        if done_before >= len(ops):
            self.log("info", f"── pass {name}：断点已完成，跳过")
            return
        self.log("info", f"── pass {name}：{len(ops)} 条（断点 {done_before}）")
        handler = {
            "mkdir": self._pass_mkdir,
            "rename": self._pass_rename,
            "move": self._pass_move,
            "trash": self._pass_trash,
        }[name]
        handler(ops, done_before, max_requests)

    # ---- ① 建目录 ---------------------------------------------------------
    def _pass_mkdir(self, ops: list[Op], done_before: int, max_requests: int | None) -> None:
        for i, op in enumerate(ops):
            if i < done_before:
                continue
            self._emit("mkdir", op.target)
            if self.cfg.dry_run:
                self.done += 1
                continue
            try:
                self._resolve_dir(op.target)
                self.done += 1
            except V115Error as exc:
                self._fail(op, exc)
            self._checkpoint("mkdir", i + 1, max_requests)

    # ---- ② 改名 -----------------------------------------------------------
    def _pass_rename(self, ops: list[Op], done_before: int, max_requests: int | None) -> None:
        if self.cfg.dry_run:
            # ⛔ dry-run 里**不查源** —— `_find` 会经 `ensure_dir` 出去写请求。
            n = 0
            for i, op in enumerate(ops):
                if i < done_before:
                    continue
                n += 1
                self._emit("rename", f"{op.path} → {op.new_name or op.name}")
            self.done += n
            self._checkpoint("rename", len(ops), max_requests)
            return

        pairs: list[tuple[Op, str, str]] = []     # (op, fid, new_name)
        skipped = 0
        for op in ops:
            if skipped < done_before:
                skipped += 1
                continue
            node = self._find(op.src_dir, op.name)
            if node is None:
                self.log("warning", f"改名：源不存在，跳过 {op.path}")
                self._fail(op, "源不存在（可能已处理）")
                continue
            pairs.append((op, node.id, op.new_name or op.name))
        for start in range(0, len(pairs), RENAME_CHUNK):
            chunk = pairs[start:start + RENAME_CHUNK]
            try:
                self.v.rename([(fid, new) for _op, fid, new in chunk])
                self.done += len(chunk)
            except V115Error as exc:
                for op, _fid, _new in chunk:
                    self._fail(op, exc)
            self._checkpoint("rename", min(done_before + start + len(chunk), len(ops)), max_requests)

    # ---- ③ 移动 -----------------------------------------------------------
    def _pass_move(self, ops: list[Op], done_before: int, max_requests: int | None) -> None:
        if self.cfg.dry_run:
            n = 0
            for i, op in enumerate(ops):
                if i < done_before:
                    continue
                n += 1
                self._emit("move", f"{op.path} → {op.target or self.cfg.root_path}")
            self.done += n
            self._checkpoint("move", len(ops), max_requests)
            return

        by_target: dict[str, list[tuple[Op, Any]]] = defaultdict(list)
        for op in ops:
            node = self._find(op.src_dir, op.name)
            if node is None:
                self.log("warning", f"移动：源不存在，跳过 {op.path}")
                self._fail(op, "源不存在（可能已处理）")
                continue
            target = op.target or self.cfg.root_path
            by_target[target].append((op, node))
        total = 0
        for target, items in by_target.items():
            total += len(items)
            if total <= done_before:
                continue
            try:
                pid = self._resolve_dir(target)
            except V115Error as exc:
                for op, _node in items:
                    self._fail(op, exc)
                continue
            for start in range(0, len(items), MOVE_CHUNK):
                chunk = items[start:start + MOVE_CHUNK]
                fids = [node.id for _op, node in chunk]
                try:
                    self.v.move(fids, pid)
                    self.done += len(chunk)
                    # 移走后缓存失效，防止后续按旧位置误判
                    self._listing.pop(pid, None)
                except V115Error as exc:
                    for op, _node in chunk:
                        self._fail(op, exc)
                self._checkpoint("move", min(total, done_before + start + len(chunk)), max_requests)

    # ---- ④ 清理 -----------------------------------------------------------
    def _pass_trash(self, ops: list[Op], done_before: int, max_requests: int | None) -> None:
        mode = (self.cfg.junk_action or "quarantine").lower()
        qdir = self.cfg.quarantine_dir
        if self.cfg.dry_run:
            n = 0
            for i, op in enumerate(ops):
                if i < done_before:
                    continue
                n += 1
                self._emit("trash", f"{op.path} （{op.reason}）")
            self.done += n
            self._checkpoint("trash", len(ops), max_requests)
            return

        pool: list[tuple[Op, Any]] = []
        for i, op in enumerate(ops):
            if i < done_before:
                continue
            node = self._find(op.src_dir, op.name)
            if node is None:
                self.log("warning", f"清理：文件不存在，跳过 {op.path}")
                continue
            pool.append((op, node))
        if not pool:
            self._checkpoint("trash", len(ops), max_requests)
            return

        if mode == "report":
            self.done += len(pool)
            for op, _node in pool:
                self._emit("trash", f"{op.path} （{op.reason}）")
            self._checkpoint("trash", len(ops), max_requests)
            return

        if mode == "delete":
            for start in range(0, len(pool), MOVE_CHUNK):
                chunk = pool[start:start + MOVE_CHUNK]
                try:
                    self.v.delete([node.id for _op, node in chunk])
                    self.done += len(chunk)
                except V115Error as exc:
                    for op, _node in chunk:
                        self._fail(op, exc)
                self._checkpoint("trash", min(len(ops), done_before + start + len(chunk)), max_requests)
            return

        # quarantine：移到 `root/_待清理/{源目录名}/`，**保留原名、按来源分桶**，
        # 这样既不重名、又能一眼看出它是从哪来的（可整目录撤销）。
        try:
            buckets: dict[str, list[tuple[Op, Any]]] = defaultdict(list)
            for op, node in pool:
                bucket = f"{qdir}/{op.src_dir}" if op.src_dir else qdir
                buckets[bucket].append((op, node))
            for bucket, items in buckets.items():
                pid = self._resolve_dir(bucket)
                for start in range(0, len(items), MOVE_CHUNK):
                    chunk = items[start:start + MOVE_CHUNK]
                    try:
                        self.v.move([node.id for _op, node in chunk], pid)
                        self.done += len(chunk)
                        self._listing.pop(pid, None)
                    except V115Error as exc:
                        for op, _node in chunk:
                            self._fail(op, exc)
                    self._checkpoint("trash", done_before + start + len(chunk), max_requests)
        except V115Error as exc:
            for op, _node in pool:
                self._fail(op, exc)

    # ------------------------------------------------------------------ 工具
    def _emit(self, kind: str, detail: str) -> None:
        self.log("info", f"[dry-run] {kind}: {detail}")

    def _fail(self, op: Op, err: Any) -> None:
        self.errors.append({"op": op.path, "kind": op.kind, "error": str(err)[:300]})

    def _checkpoint(self, name: str, value: int, max_requests: int | None) -> None:
        self.state["passes"][name] = max(self.state["passes"].get(name, 0), value)
        if value % 20 == 0:
            self._save_state()
        if max_requests and self.v.calls >= max_requests:
            self.log("warning", f"请求数达上限 {max_requests}，保存断点并收工")
            self._save_state()
            raise _StopRun()

    def clear_state(self) -> None:
        if self.state_path.exists():
            self.state_path.unlink()


class _StopRun(Exception):
    """内部信号：达到请求上限，正常收工（不是错误）。"""
