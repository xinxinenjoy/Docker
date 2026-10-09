"""报告渲染 —— 人看的 Markdown + 机器看的 JSON。

⭐ 定位不是「日志」，而是**他要过目的那份东西**：
  · 概要必须能**一屏看完**（多少条要写、多少要删、有没有触发异常闸门）
  · 删除项要**单独成段** —— 那是唯一不可逆的动作，必须显眼
  · 明细动辄几千条 ⇒ 不进 Markdown，另写一份 txt
"""
from __future__ import annotations

from datetime import datetime

__all__ = ["render_sync_markdown", "render_diff_txt"]


def _table(rows: list[tuple], header: tuple) -> str:
    out = ["| " + " | ".join(str(h) for h in header) + " |",
           "|" + "|".join("---" for _ in header) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def render_sync_markdown(res: dict, cfg_all: dict | None = None) -> str:
    """一轮同步的回执（Markdown）。"""
    L: list[str] = []
    mode = "演练（未落盘）" if res.get("dry_run") else "执行"
    L.append(f"# 115strm 同步回执 · {mode}")
    L.append("")
    L.append(_table([
        ("开始", res.get("started", "")),
        ("结束", res.get("finished", "")),
        ("耗时", res.get("elapsed", "")),
        ("发现方式", _source_cn(res.get("source", ""))),
        ("115 请求数", f"{res.get('requests', 0):,}"),
    ], ("项", "值")))
    L.append("")

    L.append("## 一、这一轮做了什么")
    L.append("")
    L.append(_table([
        ("网盘条目", f"{res.get('scanned_remote', 0):,}"),
        ("本地已有 strm", f"{res.get('scanned_local', 0):,}"),
        ("**新写 strm**", f"**{res.get('written', 0):,}**"),
        ("内容未变（跳过）", f"{res.get('unchanged', 0):,}"),
        ("本地改过名（原样保留）", f"{res.get('renamed', 0):,}"),
        ("新建目录", f"{res.get('dirs_made', 0):,}"),
        ("**删除 strm**", f"**{res.get('deleted', 0):,}**"),
        ("挂起待确认", f"{res.get('pending_delete', 0):,}"),
    ], ("项", "数量")))
    L.append("")

    if res.get("looks_like_move"):
        L.append("> 🔀 本轮判定为**改名 / 移动**（消失的条数 ≈ 新增的条数），不是异常。")
        L.append("")

    if res.get("guard_tripped"):
        L.append("## ⚠️ 异常闸门已触发 —— 本轮没有删除")
        L.append("")
        L.append(f"> {res.get('guard_reason', '')}")
        L.append("")

    if res.get("errors"):
        L.append("## 二、错误")
        L.append("")
        L.append(_table([(e.get("kind", ""), e.get("msg", "")[:200]) for e in res["errors"][:50]],
                        ("类型", "说明")))
        L.append("")

    if cfg_all:
        L.append("## 三、这轮用的口径")
        L.append("")
        L.append(_table([
            ("strm 前缀", f"`{cfg_all.get('strm_prefix', '')}`"),
            ("网盘根", cfg_all.get("remote_root") or "(网络根)"),
            ("输出目录", f"`{cfg_all.get('output_dir', '')}`"),
            ("扩展名", cfg_all.get("video_ext", "")),
            ("删除等待", f"{cfg_all.get('delete_defer_minutes', 0)} 分钟"),
            ("异常阈值", f"{cfg_all.get('delete_ratio_guard', 0):.0%}"),
        ], ("项", "值")))
        L.append("")

    return "\n".join(L)


def render_diff_txt(res: dict) -> str:
    """明细清单（几千条时给他另存一份看）。"""
    d = res.get("diff") or {}
    L: list[str] = []
    for key, label in (("add", "新增"), ("update", "更新"), ("delete", "删除")):
        items = d.get(key) or []
        if not items:
            continue
        L.append(f"===== {label}（{len(items)} 条）=====")
        L.extend(items)
        L.append("")
    more = d.get("more") or {}
    if more:
        L.append("（明细已截断，完整数量见回执）")
        for k, v in more.items():
            if v:
                L.append(f"  {k}: 还有 {v} 条未列出")
    return "\n".join(L)


def _source_cn(s: str) -> str:
    """数据来源的中文名。⚠️ 现在**只有一条路**（自己导出目录树 + 全量对账），
    这里保留映射是为了老回执还能读懂、以及 `manual` 那条手工路径。"""
    return {"tree": "自己导出目录树（全量对账）", "manual": "手动"}.get(s, s or "—")


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")
