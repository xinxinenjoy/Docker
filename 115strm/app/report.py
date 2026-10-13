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
    # ⚠️ 原来这里还有个「· 演练（未落盘）」的后缀 —— 2026-10-11 随 `dry_run` 一起撤了。
    L.append("# 115strm 同步回执")
    L.append("")
    # ⚠️ 「耗时」给**整轮**（含导目录树）—— 老回执只有对账段的 `elapsed`，
    #    退回它但要写明，别让两个口径混着看不出区别（2026-10-13 前的回执就是那种）。
    total = res.get("elapsed_total") or res.get("elapsed", "")
    rows = [
        ("开始", res.get("started", "")),
        ("结束", res.get("finished", "")),
        ("耗时（整轮）", total),
    ]
    if res.get("elapsed_tree"):
        rows.append(("　其中：导目录树", res.get("elapsed_tree", "")))
    if res.get("elapsed"):
        rows.append(("　其中：对账落盘", res.get("elapsed", "")))
    rows += [
        ("发现方式", _source_cn(res.get("source", ""))),
        ("115 请求数", f"{res.get('requests', 0):,}"),
    ]
    L.append(_table(rows, ("项", "值")))
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

    # 「既没有子项、也没有扩展名」的条目 —— 光报个数没法处理，**得说是哪几个**
    # （红领巾 2026-10-11：「这个也不知道说的是哪个」）。
    if res.get("extensionless"):
        n_ext = res["extensionless"]
        sample = res.get("extensionless_sample") or []
        L.append(f"> ⚠️ 目录树里有 **{n_ext:,} 个**条目既没有子项、也没有扩展名 —— "
                 f"这几乎只能是**导出时层级选少了**（子树被截断），不是空目录；工具一律不建。")
        for p in sample:
            L.append(f">   · `{p}`")
        if n_ext > len(sample):
            L.append(f">   · …另有 {n_ext - len(sample):,} 个未列出（回执只存前 {len(sample)} 条）")
        L.append("")

    # ---- 外挂字幕（2026-10-11 新增；口径见 app/subtitle.py）----
    # ⚠️ 只在**有内容可说**时才出这一节 —— 没开这功能的那一轮，回执不该多出一堆 0。
    if res.get("subs_enabled") or res.get("subs_planned"):
        L.append("## 二、外挂字幕")
        L.append("")
        if not res.get("subs_enabled"):
            L.append("> 这功能**没开**（设置 › 字幕 › 下载外挂字幕到本地）。以下是「开了会怎样」。")
            L.append("")
        L.append(_table([
            ("网盘上能配上、该下到本地", f"{res.get('subs_planned', 0):,}"),
            ("**本轮下载**", f"**{res.get('subs_downloaded', 0):,}**"),
            ("本地已有（跳过，零请求）", f"{res.get('subs_skipped', 0):,}"),
            ("下载失败", f"{res.get('subs_failed', 0):,}"),
            ("按设置跳过（.sup 等）", f"{res.get('subs_skipped_ext', 0):,}"),
            ("删除本地字幕", f"{res.get('subs_deleted', 0):,}"),
            ("挂起待确认", f"{res.get('subs_pending_delete', 0):,}"),
        ], ("项", "数量")))
        L.append("")
        if res.get("subs_guard_tripped"):
            L.append(f"> ⚠️ 字幕的异常闸门触发了 —— 本轮没删本地字幕："
                     f"{res.get('subs_guard_reason', '')}")
            L.append("")
        for note in (res.get("subs_notes") or [])[:10]:
            L.append(f"> · {note}")
        if res.get("subs_notes"):
            L.append("")
        um = res.get("subs_unmatched") or []
        if um:
            more = res.get("subs_unmatched_more") or 0
            L.append("### 配不上的字幕（**没有下载**，要的话人工改名）")
            L.append("")
            # ⚠️ **按目录列出**，不是一张列名字的表 —— 2026-10-11 红领巾：
            #    「直接显示文件我根本就不知道需要处理的文件在哪」。
            L.append(f"共 **{len(um) + more:,}** 条，按目录列（去对应目录里对着改名即可）：")
            L.append("")
            by_dir: dict[str, list] = {}
            for r in um[:50]:
                by_dir.setdefault(r.get("dir") or "(同步根)", []).append(r)
            for d in sorted(by_dir):
                L.append(f"**{d}**")
                L.append("")
                for r in by_dir[d]:
                    who = (f"，同目录视频 `{r['video']}`" if r.get("video")
                           else "，同目录没找到相关视频")
                    L.append(f"- `{r.get('sub', '')}` —— {r.get('kind', '')}{who}")
                L.append("")
            if len(um) > 50 or more:
                L.append(f"（本回执按目录只列了前 50 条；同一轮的 json 回执里有前 "
                         f"{len(um):,} 条）")
                L.append("")

    if res.get("errors"):
        L.append("## 三、错误")
        L.append("")
        L.append(_table([(e.get("kind", ""), e.get("msg", "")[:200]) for e in res["errors"][:50]],
                        ("类型", "说明")))
        L.append("")

    if cfg_all:
        L.append("## 四、这轮用的口径")
        L.append("")
        L.append(_table([
            ("strm 前缀", f"`{cfg_all.get('strm_prefix', '')}`"),
            ("网盘根", cfg_all.get("remote_root") or "(网络根)"),
            ("输出目录", f"`{cfg_all.get('output_dir', '')}`"),
            ("扩展名", cfg_all.get("video_ext", "")),
            ("删除等待", f"{cfg_all.get('delete_defer_minutes', 0)} 分钟"),
            ("异常阈值", f"{cfg_all.get('delete_ratio_guard', 0):.0%}"),
            ("外挂字幕", ("开" if _truthy(cfg_all.get("subtitle_enable")) else "关")
                       + (f" · 跳过 .sup" if _truthy(cfg_all.get("subtitle_skip_sup")) else "")),
            ("认的字幕扩展名", cfg_all.get("subtitle_ext", "")),
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


def _truthy(v) -> bool:
    """配置快照里的真假值 —— `cfg.all()` 里大部分是字符串（`"True"`/`"1"`），
    但也可能是原生 bool，两种都要认。"""
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("1", "true", "yes", "on", "y")


def _source_cn(s: str) -> str:
    """数据来源的中文名。⚠️ 现在**只有一条路**（自己导出目录树 + 全量对账），
    这里保留映射是为了老回执还能读懂、以及 `manual` 那条手工路径。"""
    return {"tree": "自己导出目录树（全量对账）", "manual": "手动"}.get(s, s or "—")


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")
