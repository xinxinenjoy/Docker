"""在线增量计划 —— 自动化那一半：处理**目录树里还没有**的新目录。

## 为什么必须有这个模块

全量计划（`plan.build_plan`）吃的是**静态目录树导出**。但自动化的场景是
「新目录刚进来」——它**必然不在**那份树里。所以增量这一半只能**实时列举**：

    列出根目录（分页）→ 跟快照比出新增 → 对每个新增目录列一层（1 请求）→ 判垃圾 / 认番号

⚠️ 请求预算（这是自动化能不能天天跑的关键）：
    · 列根目录：`ceil(根下条目数 / 200)` 次（本盘 11,206 项 ⇒ **57 次**）
    · 每个要处理的目录：**1 次**（列它自己的内容，才能拿到文件的 id）
    ⇒ 所以增量计划只对**新增的那几个目录**发请求，跟盘的大小**无关**。
      这是「每天都跑也不会触发风控」的根据。

⚠️ 系列聚合在增量场景下拿不到「全盘该系列有几部」：
    规则改成 —— 系列目录**已经存在**就搬进去；不存在时，**只看这一批里**同系列的数量，
    够阈值才新建（避免为一部新片建一个系列目录）。
"""
from __future__ import annotations

from datetime import datetime

from . import junk as junk_mod
from . import namer, number
from .config import Config
from .plan import Plan, Op, RESERVED_DIRS
from .v115 import V115, Node

__all__ = ["build_live_plan", "list_root"]


def list_root(v: V115, cfg: Config) -> tuple[list[Node], list[Node]]:
    """列出整理根：返回 (子目录, 子文件)。"""
    cid = v.root_cid()
    nodes = v.list_dir(cid)
    dirs = [n for n in nodes if n.is_dir and n.name not in RESERVED_DIRS]
    files = [n for n in nodes if not n.is_dir and n.name not in RESERVED_DIRS]
    return dirs, files


def build_live_plan(v: V115, cfg: Config, targets: list[str] | None = None,
                    *, with_junk: bool = True) -> Plan:
    """生成**在线增量计划**。

    `targets`：只处理这些目录名（通常来自「最近接收」）；`None` = 处理调用方筛出来的全部。
    """
    root_dirs, root_files = list_root(v, cfg)
    dirs_by_name = {n.name: n for n in root_dirs}
    chosen = list(root_dirs) if targets is None else [dirs_by_name[t] for t in targets
                                                      if t in dirs_by_name]

    plan = Plan(created=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), root=cfg.root_path,
                config={k: val for k, val in cfg.all().items() if k != "cookie"},
                tree_stats={"source": "(实时列举)", "dirs": len(root_dirs),
                            "files": len(root_files),
                            # 根下条目数 = 列根目录要翻几页的依据（`fs_files` 不递归）
                            "root_entries": len(root_dirs) + len(root_files)})

    # 本批次的番号统计（用于决定要不要新建系列目录）
    batch_series: dict[str, list[Node]] = {}
    for d in chosen:
        pid = number.get_id(d.name)
        if pid:
            batch_series.setdefault(number.series_of(pid), []).append(d)

    move_ops: list[Op] = []
    other_ops: list[Op] = []
    claimed: dict[str, str] = {}

    for d in chosen:
        pid = number.get_id(d.name)
        target = ""
        if pid:
            avid = number.format_id(pid)
            series = number.series_of(pid)
            series_exists = series in dirs_by_name
            enough = len(batch_series.get(series, [])) >= cfg.series_min
            if cfg.series_enable and (series_exists or enough):
                target = series
            elif cfg.series_rename and avid != d.name:
                target = ""
            else:
                target = ""
            want = avid if cfg.series_rename else d.name
            key = f"{target}/{want}"
            if key in claimed and claimed[key] != d.name:
                other_ops.append(Op(kind="rename", path=d.name, new_name="", auto=False,
                                    group="series", reason="目标重名，需人工裁决"))
            else:
                claimed[key] = d.name
            if target or want != d.name:
                move_ops.append(Op(
                    kind="move_dir", path=d.name, target=target, new_name=want if want != d.name else "",
                    group="series",
                    reason=(f"{series} 系列" + ("（目录已存在）" if series_exists else
                                                f"（本批 {len(batch_series.get(series, []))} 部）")),
                ))

        # 目录内的垃圾：只列这一层（增量不递归，控制请求数）
        if with_junk:
            for node in v.list_dir(d.id):
                if node.is_dir:
                    continue
                verdict = junk_mod.judge(node.name, dup_count=1, dup_min=cfg.junk_dup_min,
                                         parent_name=d.name)
                if verdict.level == junk_mod.LEVEL_STRONG:
                    other_ops.append(Op(kind="trash", path=f"{d.name}/{node.name}",
                                        reason=verdict.reason, group="junk", auto=verdict.auto))
                elif verdict.level == junk_mod.LEVEL_SUSPECT:
                    plan.suspects.append({"path": f"{d.name}/{node.name}",
                                          "reason": verdict.reason, "dup": 1})

    # 根目录散文件
    for node in root_files:
        pid = number.get_id(node.name)
        if pid:
            avid = number.format_id(pid)
            series = number.series_of(pid)
            if cfg.series_enable and series in dirs_by_name:
                other_ops.append(Op(kind="move_file", path=node.name, target=f"{series}/{avid}",
                                    new_name=f"{avid}.{node.ext}" if node.ext else "",
                                    group="series", reason=f"散落番号片 → {series}"))
            continue
        verdict = junk_mod.judge(node.name, dup_count=1, dup_min=cfg.junk_dup_min)
        if verdict.level == junk_mod.LEVEL_STRONG:
            other_ops.append(Op(kind="trash", path=node.name, reason=verdict.reason,
                                group="junk", auto=verdict.auto))
        elif verdict.level == junk_mod.LEVEL_SUSPECT:
            plan.suspects.append({"path": node.name, "reason": verdict.reason, "dup": 1})

    # 补建目标目录
    for t in sorted({op.target for op in move_ops + other_ops if op.target},
                    key=lambda x: (x.count("/"), x)):
        other_ops.append(Op(kind="mkdir", target=t, group="mkdir", reason="确保目标目录存在"))

    order = {"mkdir": 0, "move_dir": 1, "move_file": 2, "rename": 3, "trash": 4}
    plan.ops = sorted(move_ops + other_ops, key=lambda o: (order.get(o.kind, 9), o.path))
    return plan
