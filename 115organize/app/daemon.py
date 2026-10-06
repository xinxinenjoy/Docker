"""自动化调度 —— 每天定时 + 「最近接收」轮询。

红领巾 2026-10-06 选的档：**轮询最近接收 + 每天一次**（`SCHEDULE_CRON` 默认 `0 4 * * *`）。

一轮做什么：
    ① 列根目录（分页）→ 跟快照 diff，找出**新增目录**
    ② 拉「最近接收」→ 取最近 `RECENT_DAYS` 天的条目
    ③ ①∪② 就是这一轮要处理的目录；对它们生成**增量计划**（只碰这几个目录）
    ④ 执行（受 `DRY_RUN` 管）→ 更新快照 → 写回执

⚠️ 为什么不用 `APScheduler` / `croniter`：
   只需要「分 时 日 月 周」的最小子集，为此多装一个依赖不划算。
   自己写 30 行的 matcher，行为可读、可测、无第三方。
"""
from __future__ import annotations

import time
from datetime import datetime

from . import report as report_mod
from .config import Config
from .executor import Executor
from .live import build_live_plan, list_root
from .snapshot import Snapshot, recent_filter
from .throttle import Throttle
from .v115 import V115, load_cookie, build_client

__all__ = ["cron_match", "validate_cron", "Daemon", "run_once"]

# 常见 cron 英文缩写。支持它成本很低，而不支持的代价是 `0 4 * * MON` 这种**静默永不触发**
# （用户以为配好了，实际一年都不会跑）。
_NAME_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    "SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6,
}
_FIELDS = (("分", 0, 59), ("时", 0, 23), ("日", 1, 31), ("月", 1, 12), ("周", 0, 7))


def _as_int(text: str) -> int | None:
    t = (text or "").strip().upper()
    if t in _NAME_MAP:
        return _NAME_MAP[t]
    try:
        return int(t)
    except ValueError:
        return None


def _field_match(field: str, value: int, lo: int, hi: int) -> bool:
    """支持 `*` / `5` / `1,2,3` / `1-5` / `*/6` / 英文缩写（`MON`、`JAN`）。

    ⚠️ **看不懂的写法一律判「不匹配」**（不是「匹配」）。曾经想反着来（宁可多跑），
       但「多跑」的真实代价是**每分钟打一次 115 全盘** —— 那不是多跑一轮，那是风控自杀。
       所以选择：看不懂就不跑，但 `validate_cron()` 会在**启动时**把它喊出来，
       不给你「静默永不触发」的机会。
    """
    field = (field or "*").strip()
    if field == "*":
        return True
    if field.startswith("*/"):
        step = _as_int(field[2:])
        if step is None or step <= 0:
            return False
        return (value - lo) % step == 0
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _sep, b = part.partition("-")
            na, nb = _as_int(a), _as_int(b)
            if na is None or nb is None:
                return False
            if na <= value <= nb:
                return True
        else:
            nv = _as_int(part)
            if nv is None:
                return False
            if nv == value:
                return True
    return False


def validate_cron(expr: str) -> list[str]:
    """检查表达式能不能被理解。返回问题清单（空 = 没问题）。

    给 `doctor` 和守护进程启动时用 —— 「配了个永远不会触发的计划」必须当场喊出来。
    """
    parts = (expr or "").split()
    if len(parts) != 5:
        return [f"`{expr or '(空)'}` 不是 5 段（分 时 日 月 周）"]
    problems: list[str] = []
    for (name, lo, hi), part in zip(_FIELDS, parts):
        if part == "*":
            continue
        if part.startswith("*/"):
            step = _as_int(part[2:])
            if step is None or step <= 0:
                problems.append(f"{name}字段步长看不懂：`{part}`")
            continue
        for seg in part.split(","):
            seg = seg.strip()
            if not seg:
                problems.append(f"{name}字段有空段")
                continue
            if "-" in seg:
                a, _sep, b = seg.partition("-")
                na, nb = _as_int(a), _as_int(b)
                if na is None or nb is None:
                    problems.append(f"{name}字段区间看不懂：`{seg}`")
                elif not (lo <= na <= hi and lo <= nb <= hi):
                    problems.append(f"{name}字段超出 {lo}-{hi}：`{seg}`")
            else:
                nv = _as_int(seg)
                if nv is None:
                    problems.append(f"{name}字段看不懂：`{seg}`")
                elif not lo <= nv <= hi:
                    problems.append(f"{name}字段超出 {lo}-{hi}：`{seg}`")
    return problems


def cron_match(expr: str, now: datetime | None = None) -> bool:
    """`0 4 * * *` 形式的匹配。段数不对 ⇒ False；段内看不懂 ⇒ 该段不匹配。

    ⚠️ 星期口径：cron 里 `0` 和 `7` **都是周日**，Python 的 `weekday()` 是周一=0。
       换算漏了 7 的话，写成 `0 4 * * 7` 的人会得到一个永不触发的任务。
    """
    now = now or datetime.now()
    parts = (expr or "").split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    # cron 的星期：0/7 = 周日；Python 的 weekday() 是周一=0
    py_dow = (now.weekday() + 1) % 7
    dow_ok = _field_match(dow, py_dow, 0, 6) or (py_dow == 0 and _field_match(dow, 7, 0, 7))
    return (_field_match(minute, now.minute, 0, 59)
            and _field_match(hour, now.hour, 0, 23)
            and _field_match(dom, now.day, 1, 31)
            and _field_match(month, now.month, 1, 12)
            and dow_ok)


class Daemon:
    def __init__(self, cfg: Config, log):
        self.cfg = cfg
        self.log = log
        self.throttle = Throttle(cfg=cfg, log=log)
        self.v: V115 | None = None

    def _client(self) -> V115:
        if self.v is None:
            self.v = V115(build_client(load_cookie(self.cfg)), self.cfg, self.throttle, self.log)
        return self.v

    # ------------------------------------------------------------------ 检测
    def detect_new(self) -> list[str]:
        """找出这一轮要处理的目录：快照 diff（新增）+ 最近接收（时间窗内）。"""
        v = self._client()
        root_dirs, _files = list_root(v, self.cfg)
        current = {n.name: n.id for n in root_dirs}

        snap = Snapshot.load(self.cfg)
        if not snap.dirs:
            self.log("info", f"首次运行：建立快照基线（{len(current)} 个目录），本轮不处理")
            snap.dirs = current
            snap.save(self.cfg)
            return []

        fresh = snap.new_dirs(current)
        received: list[str] = []
        try:
            items = recent_filter(v.receive_list(limit=200), self.cfg.recent_days)
            received = [it["name"] for it in items
                        if it.get("is_dir") and it.get("name") in current]
        except Exception as exc:
            self.log("warning", f"「最近接收」拉取失败（不影响快照 diff）：{exc}")

        targets = sorted(set(fresh) | set(received))
        self.log("info", f"新增 {len(fresh)} · 最近接收 {len(received)} ⇒ 本轮处理 {len(targets)} 个目录")
        return targets

    # ------------------------------------------------------------------ 一轮
    def once(self, execute: bool | None = None, targets: list[str] | None = None) -> dict:
        cfg = self.cfg
        do_exec = (not cfg.dry_run) if execute is None else execute
        v = self._client()
        ts = targets if targets is not None else self.detect_new()
        if not ts:
            self.log("info", "没有新目录，本轮无事可做")
            self.snapshot_now()
            return {"targets": 0, "actions": 0, "requests": v.calls}

        plan = build_live_plan(v, cfg, ts)
        self.log("info", f"增量计划：{len(plan.ops)} 条动作（{plan.counts()['auto']} 自动）")

        ex = Executor(v, cfg, self.log)
        result = ex.run(plan, resume=False)
        self.snapshot_now()

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = cfg.data_dir / "reports"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"run-{stamp}.md").write_text(report_mod.render_run_markdown(result), encoding="utf-8")
        (out / f"run-{stamp}.json").write_text(
            __import__("json").dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"targets": len(ts), **result}

    def snapshot_now(self) -> None:
        v = self._client()
        root_dirs, root_files = list_root(v, self.cfg)
        snap = Snapshot(dirs={n.name: n.id for n in root_dirs},
                        files=[n.name for n in root_files])
        snap.save(self.cfg)

    # ------------------------------------------------------------------ 常驻
    def loop(self, poll_seconds: float = 30.0, max_rounds: int | None = None) -> None:
        expr = self.cfg.schedule_cron
        problems = validate_cron(expr)
        if problems:
            # ⛔ 表达式看不懂 ⇒ **拒绝常驻**。否则就是「容器在跑、任务永不动」——
            #    比直接报错难查得多（他只会觉得「怎么一直没整理」）。
            self.log("error", f"SCHEDULE_CRON=`{expr}` 有问题：{'；'.join(problems)}")
            self.log("error", "⛔ 拒绝启动常驻 —— 这个计划永远触发不了。"
                              "改好 SCHEDULE_CRON（例：`0 4 * * *` = 每天 04:00）再启动；"
                              "想立刻跑一轮用 `once`。")
            return
        self.log("info", f"常驻启动：计划 `{expr}`（每分钟检查一次；"
                         f"DRY_RUN={'1' if self.cfg.dry_run else '0'}）")
        last_fired = ""
        rounds = 0
        while True:
            now = datetime.now()
            stamp = now.strftime("%Y-%m-%d %H:%M")
            if stamp != last_fired and cron_match(expr, now):
                last_fired = stamp
                rounds += 1
                self.log("info", f"── 到点（{stamp}），开始第 {rounds} 轮")
                try:
                    self.once()
                except Exception as exc:            # 单轮失败不能弄死常驻
                    self.log("error", f"本轮异常，等下一轮：{exc}")
                if max_rounds and rounds >= max_rounds:
                    self.log("info", f"已达到 max_rounds={max_rounds}，退出")
                    return
            time.sleep(max(1.0, poll_seconds))


def run_once(cfg: Config, log, targets: list[str] | None = None,
             execute: bool | None = None) -> dict:
    return Daemon(cfg, log).once(execute=execute, targets=targets)
