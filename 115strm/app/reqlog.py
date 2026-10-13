"""请求流水 —— 「这一轮到底对 115 做了什么」的原始记录。

## 为什么要有它

网页上「请求 60」这个数字以前只是个**计数**（`throttle.stats.requests`），
红领巾 2026-10-13：「我想看看它都具体操作了什么」——

    光看 60 是看不出问题的：导树的 60 次轮询和「扫了 60 个目录」在数字上
    长得一模一样，但前者是**正常**、后者是**接口选错了**（那正是本工具
    从「逐层列目录」改成「让 115 自己导树」的原因，见 `pipeline` 顶部）。
    ⇒ 计数必须能下钻到流水。

## 记什么

每条 = **一次真实发出的 115 请求**（`V115._call` 是唯一出口，所以埋在那里就全覆盖）：
序号 / 时刻 / 接口名 / **当时在干什么** / 这次请求自身耗时 / 请求前的节流等待 / 成败。

⚠️ 「节流等待」单独记，**不并进请求耗时**：
   一次请求可能前面先睡了 3 秒（节流）再花 0.2 秒拿到响应。
   并成一个数的话，「为什么这一轮慢」就永远查不出来 —— 而慢通常全在等待上。

## 上限与「不许说谎」

`_KEEP_ITEMS` 只封顶**流水明细**；归并统计（`by_name`）**随时累加、永不丢**
⇒ 超限时网页上写「只列了前 400 条，另有 N 条」，而那个 N 与各接口的次数仍然是真的。
⛔ 别把超限的条目整个丢掉还照旧报 `total` —— 那就是在编数字。
"""
from __future__ import annotations

from datetime import datetime

__all__ = ["Call", "RequestLog"]

# 流水明细则数上限 —— 超出只丢明细，归并统计仍然完整。
_KEEP_ITEMS = 400


class Call:
    """一次请求 —— 用 `__slots__` 是因为一轮可能几十上百条，不值得给每条配 dict。"""

    __slots__ = ("seq", "at", "name", "note", "ms", "wait_ms", "ok", "err")

    def __init__(self, seq: int, name: str, note: str, ms: int, wait_ms: int,
                 ok: bool, err: str = "") -> None:
        self.seq = seq
        self.at = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.name = name
        self.note = note
        self.ms = ms
        self.wait_ms = wait_ms
        self.ok = ok
        self.err = err

    def as_dict(self) -> dict:
        return {"seq": self.seq, "at": self.at, "name": self.name, "note": self.note,
                "ms": self.ms, "wait_ms": self.wait_ms, "ok": self.ok,
                **({"err": self.err} if self.err else {})}


class RequestLog:
    """一次同步里所有 115 请求的账本。"""

    def __init__(self, limit: int = _KEEP_ITEMS) -> None:
        self.limit = limit
        self.items: list[Call] = []       # 明细（封顶）
        self._agg: dict[str, dict] = {}   # 归并（不封顶）
        self.total = 0                    # 真实总次数（含被截掉明细的）

    def add(self, name: str, *, note: str = "", ms: int = 0, wait_ms: int = 0,
            ok: bool = True, err: str = "") -> None:
        self.total += 1
        a = self._agg.setdefault(name, {"name": name, "count": 0, "ms": 0,
                                        "wait_ms": 0, "failed": 0, "note": ""})
        a["count"] += 1
        a["ms"] += ms
        a["wait_ms"] += wait_ms
        if not ok:
            a["failed"] += 1
        if not a["note"] and note:
            a["note"] = note              # 归并行上留个例子，看得出这接口在干嘛
        if len(self.items) < self.limit:
            self.items.append(Call(self.total, name, note, ms, wait_ms, ok, err))

    @property
    def dropped(self) -> int:
        return self.total - len(self.items)

    def summary(self) -> dict:
        """给网页 / 回执用的汇总。

        ⭐ `by_name` 按**次数降序** —— 「哪个接口被反复调」是这个视图最该先回答的问题，
           按名字排序等于让人自己去数。
        """
        rows = sorted(self._agg.values(), key=lambda x: (-x["count"], x["name"]))
        return {
            "total": self.total,
            "shown": len(self.items),
            "dropped": self.dropped,
            "request_ms": sum(r["ms"] for r in rows),      # 真正花在请求上的
            "wait_ms": sum(r["wait_ms"] for r in rows),    # 花在节流等待上的
            "failed": sum(r["failed"] for r in rows),
            "by_name": rows,
            "items": [c.as_dict() for c in self.items],
        }
