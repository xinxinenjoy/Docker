"""快照与增量 —— 自动化的「新增目录」检测。

115 **没有 webhook**，所谓「有新目录加入就触发」只能靠两种轮询：
  ① `fs_history_receive_list`（115 自己的「最近接收」视图）—— **推荐**，一次请求就够
  ② 跟自己存的快照做 diff —— 兜底：能发现「非接收方式」进来的新目录

⚠️ 快照只存**名字**，不存 id 之外的任何东西。存 id 有意义（同名的目录被删了重建，id 会变，
   靠 id 能区分），但存体积/时间没意义 —— 那些每次列举都不一样，会让 diff 永远「有变化」。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config

__all__ = ["Snapshot", "load_snapshot", "save_snapshot"]


class Snapshot:
    def __init__(self, dirs: dict[str, str] | None = None, files: list[str] | None = None,
                 time: str = ""):
        self.dirs: dict[str, str] = dirs or {}
        self.files: list[str] = files or []
        self.time: str = time

    # ------------------------------------------------------------------ 存取
    @classmethod
    def path(cls, cfg: Config) -> Path:
        return Path(cfg.data_dir) / "snapshot.json"

    @classmethod
    def load(cls, cfg: Config) -> "Snapshot":
        p = cls.path(cfg)
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        return cls(dirs=data.get("dirs") or {}, files=data.get("files") or [],
                   time=data.get("time") or "")

    def save(self, cfg: Config) -> None:
        p = self.path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        p.write_text(json.dumps({"time": self.time, "dirs": self.dirs, "files": self.files},
                                ensure_ascii=False, indent=1), encoding="utf-8")

    # ------------------------------------------------------------------ diff
    def new_dirs(self, current: dict[str, str]) -> list[str]:
        """当前盘上有、快照里没有的目录名（按名字判定）。

        ⚠️ 用「名字」而不是「名字+id」：115 上「同名目录被删了重建」很常见，
           按 id 判定会把这种当「新增」，但按名字判定又会漏掉内容替换。
           这里选**按名字**，因为我们的动作是「整理目录」，名字才是整理的对象。
        """
        return sorted(n for n in current if n not in self.dirs)

    def gone_dirs(self, current: dict[str, str]) -> list[str]:
        return sorted(n for n in self.dirs if n not in current)


def recent_filter(items: list[dict], days: int) -> list[dict]:
    """把「最近接收」列表按天数过滤（`days<=0` = 不过滤）。"""
    if days <= 0:
        return items
    cutoff = datetime.now() - timedelta(days=days)
    out = []
    for it in items:
        ts = _parse_time(it.get("time"))
        if ts is None or ts >= cutoff:
            out.append(it)
    return out


def _parse_time(raw: object) -> datetime | None:
    """115 的接收时间有 unix 秒、毫秒、也有 `2026-10-06 15:44:15` 字符串。"""
    if raw in (None, "", 0, "0"):
        return None
    try:
        num = float(raw)
        if num > 1e11:           # 毫秒
            num /= 1000.0
        return datetime.fromtimestamp(num)
    except (TypeError, ValueError):
        pass
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None
