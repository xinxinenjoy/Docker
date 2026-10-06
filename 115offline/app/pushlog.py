# -*- coding: utf-8 -*-
"""本地推送记录 —— 本工具自己发出去的那一笔「本体推送」。

红领巾 2026-10-06：「在离线里边增加一个本体推送的记录方便查看」。

**为什么要单独记一份**：115 的离线任务列表只反映**它那边**的最终状态 ——
看不出「推的时候挑的哪个目录」「有没有被 115 挡回来 / 被本地去重吃掉」。
这份记录是这个工具自己的账本：

  · 落盘 `DATA_DIR/pushlog.json`（与 accounts.json / folder_cache.json 同目录，已挂宿主机）
    ⇒ 容器重建不丢；写盘是**原子写**（tmp + replace）。
  · 上限 1000 条，**最新在前**；满了丢最旧的。
  · 读永远内存优先（快），只有写才刷盘。
  · **纯本地账本**：不参与任何 115 请求；记账失败只打日志，绝不影响推送本身。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

MAX_RECORDS = 1000     # 保留条数（前端的提示文案跟这个值对齐）
MAX_LINKS = 30         # 单条记录最多留这么多链接：够回看，又不至于把 json 撑到几十 MB
LINK_MAX_CHARS = 600   # 单条链接截断长度（磁力一般 100~300 字符，600 足够看全）


def clip_link(u: str) -> str:
    s = str(u or "")
    return s if len(s) <= LINK_MAX_CHARS else s[: LINK_MAX_CHARS - 1] + "…"


class PushLog:
    """推送记录账本（线程安全；所有异常都在内部吞掉）。"""

    def __init__(self, path: Path | str, max_records: int = MAX_RECORDS) -> None:
        self.path = Path(path)
        self.max_records = int(max_records)
        self._lock = threading.RLock()
        self._items: list[dict] = []   # 最新在前
        self._next_id = 1

    # ------------------------------------------------------------------ 读
    def load(self) -> int:
        """启动时恢复。文件坏了就当空的 —— 记账本不配影响服务启动。"""
        if not self.path.exists():
            return 0
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except Exception as exc:
            logging.warning("pushlog restore failed: %s", exc)
            return 0
        rows = raw.get("items") if isinstance(raw, dict) else raw
        restored: list[dict] = []
        for it in (rows or []):
            if not isinstance(it, dict):
                continue
            links = []
            for lk in (it.get("links") or []):
                if isinstance(lk, dict):
                    links.append({"url": str(lk.get("url") or ""),
                                  "source": str(lk.get("source") or "")})
                elif isinstance(lk, str):
                    links.append({"url": lk, "source": ""})
            restored.append({
                "id": int(it.get("id") or 0),
                "ts": int(it.get("ts") or 0),
                "account_id": str(it.get("account_id") or ""),
                "account": str(it.get("account") or ""),
                "dir_id": str(it.get("dir_id") or ""),
                "dir": str(it.get("dir") or ""),
                "count": int(it.get("count") or 0),
                "skipped": int(it.get("skipped") or 0),
                "links": links[:MAX_LINKS],
                "links_more": int(it.get("links_more") or 0),
                "ok": bool(it.get("ok")),
                "message": str(it.get("message") or ""),
            })
        restored.sort(key=lambda r: r["id"])
        with self._lock:
            self._items = list(reversed(restored[-self.max_records:]))
            self._next_id = (self._items[0]["id"] + 1) if self._items else 1
        logging.info("pushlog: restored %d records from %s", len(self._items), self.path)
        return len(self._items)

    def items(self, limit: int = 200) -> list[dict]:
        limit = max(0, int(limit or 0))
        with self._lock:
            return [dict(r) for r in self._items[:limit]]

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    # ------------------------------------------------------------------ 写
    def add(self, *, account_id: str = "", account: str = "", dir_id: str = "",
            dir: str = "", count: int = 0, skipped: int = 0,
            links: list[dict] | None = None, ok: bool = True,
            message: str = "", ts: int | None = None) -> dict:
        """记一笔（最新在前）。返回落库后的记录（便于调用方回显 / 测试断言）。"""
        links = [lk for lk in (links or []) if isinstance(lk, dict)]
        rec = {
            "id": 0,
            "ts": int(ts if ts is not None else time.time()),
            "account_id": str(account_id or ""),
            "account": str(account or ""),
            "dir_id": str(dir_id or ""),
            "dir": str(dir or ""),
            "count": int(count or 0),
            "skipped": int(skipped or 0),
            "links": [{"url": clip_link(lk.get("url")),
                       "source": str(lk.get("source") or "")} for lk in links[:MAX_LINKS]],
            "links_more": max(0, len(links) - MAX_LINKS),
            "ok": bool(ok),
            "message": str(message or ""),
        }
        try:
            with self._lock:
                rec["id"] = self._next_id
                self._next_id += 1
                self._items.insert(0, rec)
                if len(self._items) > self.max_records:
                    del self._items[self.max_records:]
                self._save_locked()
        except Exception as exc:   # 记账失败不影响推送
            logging.warning("pushlog append failed: %s", exc)
        return rec

    def clear(self) -> int:
        """清空本地记录，返回清掉的条数（前端用它显示「已清空 N 条」）。"""
        with self._lock:
            n = len(self._items)
            self._items = []
            self._save_locked()
        return n

    # ------------------------------------------------------------------ 落盘
    def _save_locked(self) -> None:
        """⚠️ 调用方必须已持有 `self._lock`。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"items": list(reversed(self._items))}, ensure_ascii=False),
                "utf-8",
            )
            tmp.replace(self.path)
        except Exception as exc:
            logging.warning("pushlog save failed: %s", exc)
