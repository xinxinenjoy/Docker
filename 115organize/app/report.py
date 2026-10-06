"""报告渲染 —— 人看的 Markdown + 机器看的 JSON。

红领巾 2026-10-06 定的流程是「**先出方案清单，确认后再跑**」（`DRY_RUN` 默认 1），
所以报告的定位不是「日志」，而是**他要过目的那份方案**：
  · 概要必须能在**一屏内**看完（有多少动作、最慢要多久、有没有冲突）
  · 冲突 / 灰区要**单独成段**，因为这两类是「需要他裁决」的
  · 明细动辄几千条 ⇒ 不进 Markdown，另写 `plan-ops.txt`
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime

from .plan import Plan

__all__ = ["render_markdown", "render_ops_txt", "render_run_markdown"]

_KIND_CN = {
    "mkdir": "建目录",
    "move_dir": "移动目录",
    "move_file": "移动文件",
    "rename": "改名",
    "trash": "清理垃圾",
}
_GROUP_CN = {
    "series": "番号→系列",
    "movie": "电影命名",
    "tv": "剧集收拢",
    "junk": "垃圾清理",
    "mkdir": "建目录",
}


def _table(rows: list[tuple], header: tuple) -> str:
    out = ["| " + " | ".join(str(h) for h in header) + " |",
           "|" + "|".join("---" for _ in header) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def render_markdown(plan: Plan, estimate: dict | None = None, extra: dict | None = None) -> str:
    c = plan.counts()
    t = plan.tree_stats
    L: list[str] = []
    L.append("# 115 整理方案（dry-run）")
    L.append("")
    L.append(f"- 生成时间：**{plan.created}**")
    L.append(f"- 整理根目录：`{plan.root}`")
    L.append(f"- 目录树来源：`{t.get('source', '')}`")
    L.append(f"- 树规模：**{t.get('dirs', 0):,} 个目录 / {t.get('files', 0):,} 个文件**")
    L.append("")

    L.append("## 一、要动多少")
    L.append("")
    L.append(_table([
        ("总动作", f"**{c['actions']:,}**"),
        ("可自动执行", f"{c['auto']:,}"),
        ("需人工裁决", f"**{c['manual']:,}**"),
        ("冲突（不自动执行）", f"{c['conflicts']:,}"),
        ("灰区待看（不动）", f"{c['suspects']:,}"),
        ("阈值内跳过", f"{c['skipped']:,}"),
    ], ("项", "值")))
    L.append("")
    L.append(_table(
        [(k, _KIND_CN.get(k, k), f"{v:,}") for k, v in
         sorted(c["by_kind"].items(), key=lambda x: -x[1])],
        ("动作类型", "说明", "数量")))
    L.append("")
    L.append(_table(
        [(k, _GROUP_CN.get(k, k), f"{v:,}") for k, v in
         sorted(c["by_group"].items(), key=lambda x: -x[1])],
        ("分组", "说明", "数量")))
    L.append("")

    if estimate:
        L.append("## 二、按当前节流档位，跑完要多久")
        L.append("")
        L.append(_table([
            ("预计请求数", f"{estimate.get('requests', 0):,}"),
            ("单请求间隔", f"{estimate.get('avg_gap')}s（均值，实际 3–5s 随机）"),
            ("长休次数", f"{estimate.get('rests', 0):,}"),
            ("**预计总时长**", f"**{estimate.get('human')}**"),
        ], ("项", "值")))
        L.append("")
        L.append("> 这只是**执行层**的估算；生成计划本身不联网，不占时间。")
        L.append("")

    n = 3 if estimate else 2
    L.append(f"## {_cn(n)}、冲突 —— 需你裁决（不自动执行）")
    L.append("")
    if plan.conflicts:
        L.append(f"共 **{len(plan.conflicts)}** 组。这些是「两个源归一到同一目标」或「目标名打架」，")
        L.append("本工具**绝不覆盖**，一律留给你决定。")
        L.append("")
        L.append(_table(
            [(i + 1, "`" + c_["target"] + "`", " / ".join(f"`{s}`" for s in c_["sources"][:4]))
             for i, c_ in enumerate(plan.conflicts[:40])],
            ("#", "目标", "来源")))
        if len(plan.conflicts) > 40:
            L.append("")
            L.append(f"（只列前 40 组，完整见 `plan.json`）")
    else:
        L.append("无。 ✅")
    L.append("")

    n += 1
    L.append(f"## {_cn(n)}、灰区 —— 不动，只是让你看一眼")
    L.append("")
    L.append(f"共 **{len(plan.suspects):,}** 条。判据不够硬的一律只报告（白名单优先、判不准不动）。")
    L.append("")
    if plan.suspects:
        L.append(_table(
            [(i + 1, "`" + s_["path"][:90] + "`", s_["reason"], s_.get("dup", 1))
             for i, s_ in enumerate(plan.suspects[:25])],
            ("#", "路径", "原因", "全盘出现")))
        if len(plan.suspects) > 25:
            L.append("")
            L.append(f"（只列前 25 条，完整见 `plan.json` 的 `suspects`）")
    L.append("")

    n += 1
    L.append(f"## {_cn(n)}、番号系列分布")
    L.append("")
    grouped = [s for s in plan.series_stats if s["grouped"]]
    L.append(f"- 识别出系列 **{len(plan.series_stats)}** 个，其中达到聚合阈值"
             f"（≥{plan.config.get('series_min')} 部）的 **{len(grouped)}** 个")
    L.append(f"- 未达阈值的 **{len(plan.series_stats) - len(grouped)}** 个：只规范命名、不单独建系列目录")
    L.append("")
    if plan.series_stats:
        L.append(_table(
            [(s["series"], s["count"], "✅ 聚合" if s["grouped"] else "— 仅规范")
             for s in plan.series_stats[:25]],
            ("系列", "部数", "处理")))
        L.append("")
        L.append("（只列前 25，完整见 `plan.json`）")
    L.append("")

    if plan.skipped:
        n += 1
        L.append(f"## {_cn(n)}、被阈值挡下 / 跳过")
        L.append("")
        L.append(_table(
            [("`" + s_["path"][:80] + "`", s_["reason"]) for s_ in plan.skipped[:15]],
            ("路径", "原因")))
        L.append("")

    if extra:
        n += 1
        L.append(f"## {_cn(n)}、其它")
        L.append("")
        for k, v in extra.items():
            L.append(f"- {k}：{v}")
        L.append("")

    L.append("---")
    L.append("")
    L.append("**下一步**：确认没问题后，把 `DRY_RUN` 改成 `0` 再跑同一条命令（或等定时任务到点）。")
    L.append("")
    L.append("| 文件 | 内容 |")
    L.append("|---|---|")
    L.append("| `plan.json` | 完整计划（含全部动作、冲突、灰区），执行器就吃它 |")
    L.append("| `plan-ops.txt` | 逐条动作明细，纯文本好搜索 |")
    L.append("")
    L.append(f"_报告由 115organize 生成 · {datetime.now():%Y-%m-%d %H:%M:%S}_")
    return "\n".join(L)


def _cn(n: int) -> str:
    return {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}.get(n, str(n))


def render_ops_txt(plan: Plan) -> str:
    """逐条明细（纯文本，方便 grep）。"""
    L: list[str] = []
    L.append(f"# 动作明细 · {plan.created} · 共 {len(plan.ops)} 条")
    L.append(f"# 格式：[序号] 类型 | 分组 | 自动? | 描述")
    L.append("")
    for i, op in enumerate(plan.ops, 1):
        flag = "自动" if op.auto else "人工"
        L.append(f"[{i}] {op.kind} | {op.group} | {flag} | {op.describe()}")
        if op.reason or op.note:
            L.append(f"      └─ {op.reason}{' · ' + op.note if op.note else ''}")
    return "\n".join(L)


def render_run_markdown(report: dict) -> str:
    """执行后的回执。"""
    L: list[str] = []
    L.append("# 115 整理执行回执")
    L.append("")
    L.append(_table([
        ("开始", report.get("started", "")),
        ("结束", report.get("finished", "")),
        ("模式", "dry-run（没动任何东西）" if report.get("dry_run") else "真跑"),
        ("计划动作", f"{report.get('planned', 0):,}"),
        ("已执行", f"**{report.get('done', 0):,}**"),
        ("失败", f"{report.get('failed', 0):,}"),
        ("实际请求数", f"{report.get('requests', 0):,}"),
        ("耗时", f"{report.get('elapsed_human', '')}"),
    ], ("项", "值")))
    L.append("")
    if report.get("by_kind"):
        L.append(_table([(k, f"{v:,}") for k, v in report["by_kind"].items()],
                        ("动作类型", "已执行")))
        L.append("")
    thr = report.get("throttle") or {}
    if thr:
        L.append("## 节流实况")
        L.append("")
        L.append(_table([
            ("请求数", f"{thr.get('requests', 0):,}"),
            ("累计 sleep", f"{thr.get('slept_seconds', 0):,.1f} 秒"),
            ("长休次数", f"{thr.get('rests', 0):,}"),
            ("时间窗等待", f"{thr.get('window_waits', 0):,}"),
            ("失败次数", f"{thr.get('failures', 0):,}"),
            ("冷却次数", f"{thr.get('cooldowns', 0):,}"),
        ], ("项", "值")))
        L.append("")
    errs = report.get("errors") or []
    if errs:
        L.append(f"## 失败明细（{len(errs)} 条，最多列 30）")
        L.append("")
        L.append(_table([(i + 1, "`" + str(e.get("op", ""))[:70] + "`", str(e.get("error", ""))[:90])
                         for i, e in enumerate(errs[:30])], ("#", "动作", "错误")))
        L.append("")
    if report.get("skipped_manual"):
        L.append(f"## 略过的人工项（{report['skipped_manual']} 条）")
        L.append("")
        L.append("冲突 / 需裁决的动作没动 —— 见 `plan.json` 的 `conflicts`。")
        L.append("")
    L.append("---")
    L.append("")
    L.append("**没有回执里没提到的副作用**：没动 115 以外的任何东西；")
    L.append("清理动作是**移到 `_待清理/`**（除非显式设了 `JUNK_ACTION=delete`）。")
    return "\n".join(L)


def kind_counter(ops) -> Counter:
    return Counter(op.kind for op in ops)
