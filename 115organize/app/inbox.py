"""入站口整理 —— 自动整理只处理一个「待整理」目录。

## 为什么有这个模块

旧自动化（daemon + 快照 diff + 最近接收）的问题是：**它要碰整个整理根**。
「每天全盘 diff」既费请求、又容易误碰已经归位好的目录 —— 风控压力全在盘的大小上。

入站口模式反过来：**范围就是一个小目录**。
你只把要整理的东西丢进 `ROOT_PATH/待整理/`，工具只处理这一层，
处理完就移走（归位到系列 / 影视区 / 隔离目录）。于是：

  · 请求量跟盘的大小**无关**，只跟「你丢了几个东西进来」有关
  · 天然增量 —— 处理完目录里就空了，下次列出来有东西 = 新来的
  · 绝不误碰已归位的目录（它们不在入站口里）

⚠️ 115 没有 webhook，只能轮询：每隔 `INBOX_POLL_INTERVAL` 列一次入站目录
（1 个请求）。间隔别设太短（建议 ≥30 秒），防风控。

## 入站口里的东西怎么处理（规则）

  · **番号目录**（如 `DLDSS-532`）→ 移到 `系列名/番号/`（够阈值）
      不够阈值（SERIES_MIN）→ 移到根下保持原名（不建多余系列目录）
  · **影视目录 / 文件**（含中文+年份 / 剧集）→ 移到根下对应系列，保持原名
  · **垃圾**（junk 强特征）→ 移到隔离目录（或按 JUNK_ACTION）
  · **无法识别** → 留原地，进报告的「未处理」段

⛔ 入站口整理**不递归**：只处理入站目录的**一层**子项。递归是「整理一个文件夹」
（功能 2）的事，那是从目录树 / 扫描里选好再单独生成的计划。
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import junk as junk_mod
from . import namer, number
from .config import Config
from .executor import Executor
from .plan import Op, Plan, RESERVED_DIRS
from .v115 import V115, Node

__all__ = ["list_inbox", "build_inbox_plan", "InboxDaemon", "run_once"]

# 入站口里「看不懂」的项进这里 —— 相当于计划里的 suspects
_SUSPECT_LEVELS = (junk_mod.LEVEL_SUSPECT,)


def list_inbox(v: V115, cfg: Config) -> tuple[list[Node], list[Node]]:
    """列出入站目录的 (子目录, 子文件)。入站目录不存在时返回空（不抛）。

    ⚠️ 用 `root_cid()` 拿整理根 id，再往下一层找 `INBOX_DIR`。
       入站目录还没建 ⇒ 返回空，让调用方「首次运行建目录」或「无事可做」。
    """
    root_id = v.root_cid()
    root_nodes = v.list_dir(root_id)
    for n in root_nodes:
        if n.is_dir and n.name == cfg.inbox_dir:
            items = v.list_dir(n.id)
            return [x for x in items if x.is_dir and x.name not in RESERVED_DIRS], \
                   [x for x in items if not x.is_dir and x.name not in RESERVED_DIRS]
    return [], []


def build_inbox_plan(v: V115, cfg: Config) -> Plan:
    """生成入站口计划 —— 只处理 `INBOX_DIR` 下的一层子项。

    ⛔ 不递归：入站口就是一层。深层文件归位是「整理文件夹」（功能 2）的事。
    """
    dirs, files = list_inbox(v, cfg)
    plan = Plan(
        created=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        root=cfg.root_path,
        config={k: val for k, val in cfg.all().items() if k != "cookie"},
        tree_stats={"source": "(入站口实时列举)", "dirs": len(dirs),
                    "files": len(files), "root_entries": len(dirs) + len(files)},
    )

    root_dir_names = _root_dir_names(v, cfg)

    # ⚠️ 系列聚合的「全盘数量」在入站口场景拿不到（不扫全盘）。
    #    规则改成 —— 系列目录**已经存在**就搬进去；不存在时，**只看这一批里**同系列的数量，
    #    够阈值才新建（避免为一部新片建一个系列目录）。
    batch_series: dict[str, list[Node]] = {}
    for d in dirs:
        pid = number.get_id(d.name)
        if pid:
            batch_series.setdefault(number.series_of(pid), []).append(d)

    move_ops: list[Op] = []
    other_ops: list[Op] = []
    claimed: dict[str, str] = {}

    # ① 番号目录 → 系列 / 根下
    for d in dirs:
        pid = number.get_id(d.name)
        target = ""
        if pid:
            avid = number.format_id(pid)
            series = number.series_of(pid)
            series_exists = series in root_dir_names
            enough = len(batch_series.get(series, [])) >= cfg.series_min
            if cfg.series_enable and (series_exists or enough):
                target = series
            want = avid if cfg.series_rename else d.name
            key = f"{target}/{want}"
            if key in claimed and claimed[key] != d.name:
                other_ops.append(Op(kind="rename", path=d.name, new_name="", auto=False,
                                    group="series", reason="目标重名，需人工裁决"))
            else:
                claimed[key] = d.name
            move_ops.append(Op(
                kind="move_dir", path=d.name, target=target, new_name=want if want != d.name else "",
                group="series",
                reason=(f"{series} 系列" + ("（目录已存在）" if series_exists else
                                            f"（本批 {len(batch_series.get(series, []))} 部）")),
            ))
            continue

        # ② 非番号目录：影视目录 → 根下对应系列（保持原名）；垃圾 → 隔离；其余留原地
        # ⚠️ 目录**没有扩展名**，junk 判定对目录一律 suspect（无扩展名）——
        #    所以影视判定必须**先于**垃圾判定：有中文+年份 = 影视，不是垃圾。
        kind = number.kind_of(d.name)
        looks_media = (namer.extract_year(d.name) is not None and number.has_cjk(d.name)) or \
                      (kind == number.Kind.TV)
        if looks_media:
            move_ops.append(Op(kind="move_dir", path=d.name, target="", new_name="",
                               group="series", reason="影视目录 → 整理根下保持原名"))
            continue

        vd = junk_mod.judge(d.name, dup_count=1, dup_min=cfg.junk_dup_min,
                            parent_name=cfg.inbox_dir)
        if vd.level == junk_mod.LEVEL_STRONG:
            other_ops.append(Op(kind="trash", path=d.name, reason=vd.reason,
                                group="junk", auto=vd.auto))
        else:
            plan.skipped.append({"path": d.name,
                                 "reason": "无法识别（不是番号 / 影视 / 垃圾），留原地"})

    # ③ 入站口散文件：番号 → 系列/番号；影视 → 根下对应系列；垃圾 → 隔离
    for f in files:
        fname = f.name if isinstance(f, Node) else f
        pid = number.get_id(fname)
        if pid:
            avid = number.format_id(pid)
            series = number.series_of(pid)
            series_exists = series in root_dir_names
            ext = junk_mod.split_ext(fname)[1]
            if cfg.series_enable and series_exists:
                other_ops.append(Op(kind="move_file", path=fname, target=f"{series}/{avid}",
                                    new_name=f"{avid}.{ext}" if ext else "",
                                    group="series", reason=f"散落番号片 → {series}"))
            else:
                other_ops.append(Op(kind="move_file", path=fname, target="",
                                    new_name=f"{avid}.{ext}" if ext else "",
                                    group="series", reason=f"散落番号片 → 根下"))
            continue

        kind = number.kind_of(fname)
        ext = junk_mod.split_ext(fname)[1]
        if kind == number.Kind.TV and ext in junk_mod.VIDEO_EXT:
            other_ops.append(Op(kind="move_file", path=fname, target="", new_name="",
                                group="tv", reason="剧集文件 → 根下（收拢由整理文件夹做）"))
            continue
        if kind == number.Kind.MOVIE and ext in junk_mod.VIDEO_EXT and namer.extract_year(fname) and number.has_cjk(fname):
            other_ops.append(Op(kind="move_file", path=fname, target="", new_name="",
                                group="movie", reason="电影 → 根下保持原名"))
            continue

        vf = junk_mod.judge(fname, dup_count=1, dup_min=cfg.junk_dup_min, parent_name=cfg.inbox_dir)
        if vf.level == junk_mod.LEVEL_STRONG:
            other_ops.append(Op(kind="trash", path=fname, reason=vf.reason, group="junk",
                                auto=vf.auto))
        elif vf.level in _SUSPECT_LEVELS:
            plan.suspects.append({"path": fname, "reason": vf.reason, "dup": 1})
        else:
            plan.skipped.append({"path": fname,
                                 "reason": "无法识别（不是番号 / 影视 / 垃圾），留原地"})

    # ④ 补建目标目录
    targets: list[str] = []
    for op in move_ops + other_ops:
        if op.kind in ("move_dir", "move_file") and op.target and op.target != cfg.root_path:
            targets.append(op.target)
    for t in sorted(set(targets), key=lambda x: (x.count("/"), x)):
        other_ops.append(Op(kind="mkdir", target=t, group="mkdir", reason="确保目标目录存在"))

    order = {"mkdir": 0, "move_dir": 1, "move_file": 2, "rename": 3, "trash": 4}
    plan.ops = sorted(move_ops + other_ops, key=lambda o: (order.get(o.kind, 9), o.path))
    return plan


def _root_dir_names(v: V115, cfg: Config) -> set[str]:
    """整理根下已有的一级目录名 —— 判断「系列目录已存在」用。"""
    root_id = v.root_cid()
    nodes = v.list_dir(root_id)
    return {n.name for n in nodes if n.is_dir and n.name not in RESERVED_DIRS}


# --------------------------------------------------------------------------- 常驻
class InboxDaemon:
    """入站口轮询守护：每隔 `INBOX_POLL_INTERVAL` 列一次入站目录，有货就处理。"""

    def __init__(self, cfg: Config, log):
        self.cfg = cfg
        self.log = log
        self.throttle = None                      # 延迟创建（首轮才需要）
        self.v: V115 | None = None
        self.rounds = 0

    def _client(self) -> V115:
        if self.v is None:
            from .throttle import Throttle
            from .v115 import build_client, load_cookie
            self.throttle = Throttle(cfg=self.cfg, log=self.log)
            self.v = V115(build_client(load_cookie(self.cfg)), self.cfg, self.throttle, self.log)
        return self.v

    def once(self, execute: bool | None = None) -> dict:
        """处理一轮入站口。返回统计（跟 daemon.once 同构）。"""
        cfg = self.cfg
        do_exec = (not cfg.dry_run) if execute is None else execute
        v = self._client()

        dirs, files = list_inbox(v, cfg)
        if not dirs and not files:
            self.log("info", f"入站口 `{cfg.inbox_dir}` 是空的 —— 本轮无事可做")
            return {"targets": 0, "actions": 0, "requests": v.calls}

        plan = build_inbox_plan(v, cfg)
        self.log("info", f"入站口计划：{len(plan.ops)} 条动作（{plan.counts()['auto']} 自动）"
                         f"；无法识别 {len(plan.skipped)} 条留原地")

        ex = Executor(v, cfg, self.log, state_name="inbox-state.json")
        result = ex.run(plan, resume=False, max_requests=cfg.inbox_max_requests or None)
        self.rounds += 1

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = cfg.data_dir / "reports"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"inbox-{stamp}.md").write_text(_render_inbox(result, plan), encoding="utf-8")
        (out / f"inbox-{stamp}.json").write_text(
            __import__("json").dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"targets": len(dirs) + len(files), **result}

    def loop(self, poll_seconds: float | None = None, max_rounds: int | None = None) -> None:
        interval = poll_seconds or self.cfg.inbox_poll_interval
        interval = max(5.0, float(interval))
        self.log("info", f"入站口常驻启动：监控 `{self.cfg.inbox_dir}`，每 {interval:.0f} 秒轮询一次"
                         f"（DRY_RUN={'1' if self.cfg.dry_run else '0'}）")
        rounds = 0
        while True:
            rounds += 1
            try:
                self.once()
            except Exception as exc:            # 单轮失败不能弄死常驻
                self.log("error", f"本轮异常，等下一轮：{exc}")
            if max_rounds and rounds >= max_rounds:
                self.log("info", f"已达到 max_rounds={max_rounds}，退出")
                return
            # 分段睡，方便测试和 Ctrl-C 快速退出
            end = time.monotonic() + interval
            while time.monotonic() < end:
                time.sleep(min(1.0, end - time.monotonic()))


def run_once(cfg: Config, log, execute: bool | None = None) -> dict:
    return InboxDaemon(cfg, log).once(execute=execute)


def _render_inbox(result: dict, plan: Plan) -> str:
    """入站口回执（md）。"""
    lines = [
        f"# 入站口整理回执",
        f"",
        f"- 时间：{result.get('started', '')} → {result.get('finished', '')}（{result.get('elapsed_human', '')}）",
        f"- DRY_RUN：{'开（只演练）' if result.get('dry_run') else '关（真跑）'}",
        f"- 入站口条目：{result.get('targets', 0)} 个",
        f"- 动作：{result.get('planned', 0)} 条（失败 {result.get('failed', 0)}）",
        f"- 请求：{result.get('requests', 0)}",
        f"",
        f"## 未识别（留在原地）",
        f"",
    ]
    if plan.skipped:
        for s in plan.skipped:
            lines.append(f"- `{s['path']}` — {s['reason']}")
    else:
        lines.append("（无）")
    lines.append("")
    lines.append("## 动作明细")
    for op in plan.ops:
        lines.append(f"- [{op.kind}] {op.describe()}")
    return "\n".join(lines)
