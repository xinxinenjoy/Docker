"""计划生成 —— **纯离线**，只读目录树，一个网络请求都不发。

这是「慢速」的第一层保障：**先算清楚要动什么，再决定要不要动**。
算的时候不碰 115，所以怎么算都不存在风控问题。

规则（红领巾 2026-10-06 拍板）：
  · 番号片 → 按系列聚合到 `系列/番号/`，**系列内 ≥SERIES_MIN 部才建目录**
  · 电影   → 目录/文件规范成 `片名（年份）`（规则来自 `namer.py`）
  · 电视剧 → 散落的集文件收进 `剧名（年份）/第N季/`
  · 垃圾   → 分级（`junk.py`）：强特征才产出动作，白名单永不碰，灰区只进报告

⚠️ 冲突即停车：两个不同的源指向同一个目标（本盘真实存在 ——
   `JUR-777.[4K]@R90s` 与 `JUR-777_4K60FPS` 归一化后都是 `JUR-777`），
   这类**一律不自动执行**，只进报告的「冲突」段，等人裁决。
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime

from . import junk as junk_mod
from . import namer, number
from .config import Config
from .treefile import Tree

__all__ = ["Op", "Plan", "build_plan", "PLAN_VERSION", "RESERVED_DIRS"]

PLAN_VERSION = 1

# 目录名，绝不参与扫描 / 清理（工具自己的产物）
RESERVED_DIRS = frozenset(("_待清理", "_整理日志", "_整理报告"))

_ORDER = {"mkdir": 0, "move_dir": 1, "move_file": 2, "rename": 3, "trash": 4}
_EP_RE = re.compile(r"[Ss](\d{1,2})[\s._-]?[Ee](\d{1,3})")


@dataclass
class Op:
    """一个待执行动作。

    `kind` 取值：
      · mkdir     建目录（target 是目标目录）
      · move_dir  移动目录（可同时改名：new_name 非空）
      · move_file 移动文件到 target 目录（可同时改名）
      · rename    原地改名（target 为空）
      · trash     清理垃圾文件（隔离 / 删除，由 JUNK_ACTION 决定）
    """
    kind: str
    path: str = ""                 # 源（相对整理根，`/` 分隔）；mkdir 时为空
    target: str = ""               # 目标目录（相对整理根）
    new_name: str = ""
    reason: str = ""
    group: str = ""                # series / movie / tv / junk / mkdir
    auto: bool = True              # 是否允许自动执行
    note: str = ""

    @property
    def src_dir(self) -> str:
        return self.path.rsplit("/", 1)[0] if "/" in self.path else ""

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1] if self.path else ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Op":
        out = dict(d)
        out.setdefault("auto", True)
        return cls(**{k: out.get(k) for k in
                      ("kind", "path", "target", "new_name", "reason", "group", "auto", "note")})

    def describe(self) -> str:
        if self.kind == "mkdir":
            return f"建目录   {self.target}/"
        if self.kind == "trash":
            extra = f"（{self.reason}{'，' + self.note if self.note else ''}）"
            return f"清理     {self.path}  {extra}"
        newname = self.new_name or self.name
        dest = f"{self.target}/{newname}" if self.target else newname
        verb = {"move_dir": "移动", "move_file": "移动文件", "rename": "改名"}.get(self.kind, self.kind)
        return f"{verb}   {self.path}  →  {dest}"


@dataclass
class Plan:
    ops: list[Op] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    suspects: list[dict] = field(default_factory=list)      # 灰区（只报告）
    skipped: list[dict] = field(default_factory=list)       # 因阈值/规则没动的
    series_stats: list[dict] = field(default_factory=list)
    tree_stats: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    created: str = ""
    root: str = ""

    # ------------------------------------------------------------------ 统计
    def by_kind(self) -> dict[str, int]:
        return dict(Counter(op.kind for op in self.ops))

    def by_group(self) -> dict[str, int]:
        return dict(Counter(op.group for op in self.ops))

    def counts(self) -> dict:
        return {
            "actions": len(self.ops),
            "auto": sum(1 for op in self.ops if op.auto),
            "manual": sum(1 for op in self.ops if not op.auto),
            "by_kind": self.by_kind(),
            "by_group": self.by_group(),
            "conflicts": len(self.conflicts),
            "suspects": len(self.suspects),
            "skipped": len(self.skipped),
        }

    def to_dict(self) -> dict:
        return {
            "version": PLAN_VERSION,
            "created": self.created,
            "root": self.root,
            "config": self.config,
            "tree_stats": self.tree_stats,
            "ops": [op.to_dict() for op in self.ops],
            "conflicts": self.conflicts,
            "suspects": self.suspects,
            "skipped": self.skipped,
            "series_stats": self.series_stats,
        }

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=1)

    @classmethod
    def load(cls, data: dict | str) -> "Plan":
        if isinstance(data, str):
            data = json.loads(data)
        return cls(
            ops=[Op.from_dict(d) for d in data.get("ops", [])],
            conflicts=data.get("conflicts", []),
            suspects=data.get("suspects", []),
            skipped=data.get("skipped", []),
            series_stats=data.get("series_stats", []),
            tree_stats=data.get("tree_stats", {}),
            config=data.get("config", {}),
            created=data.get("created", ""),
            root=data.get("root", ""),
        )


# --------------------------------------------------------------------------- 内部
def _dup_counts(tree: Tree) -> Counter:
    """统计文件名跨目录重复次数 —— 「跨目录重复」是推广文件的头号特征。"""
    c: Counter = Counter()
    for files in tree.files.values():
        for f in files:
            c[f] += 1
    return c


def _scan_dirs(tree: Tree, base: str, max_depth: int) -> list[tuple[str, str]]:
    """产出 (目录路径, 目录名)，含 base 自身，向下最多 `max_depth` 层。BFS 实现。"""
    out = [(base, tree.entries[base].name if base in tree.entries else base)]
    frontier = [base]
    for _ in range(max(0, max_depth - 1)):
        nxt: list[str] = []
        for p in frontier:
            for c in tree.children_dirs(p):
                if tree.entries[c].name in RESERVED_DIRS:
                    continue
                out.append((c, tree.entries[c].name))
                nxt.append(c)
        frontier = nxt
        if not frontier:
            break
    return out


def _tv_title_season(fname: str) -> tuple[str, int]:
    """集文件名 →（剧名目录名, 季号）。`清道夫.Ray.Donovan.S03E01.中英字幕.mp4` → (`清道夫`, 3)。"""
    stem = junk_mod.split_ext(fname)[0]
    m = _EP_RE.search(stem)
    head = stem[: m.start()] if m else stem
    season = int(m.group(1)) if m else 0
    head = head.strip(" .-_")
    title = namer.title_of(head) or (namer.clean(head) if head else "")
    title = namer.sanitize_name(title) or namer.sanitize_name(head)
    return title, season


# --------------------------------------------------------------------------- 主流程
def build_plan(tree: Tree, cfg: Config, *, max_depth: int = 2) -> Plan:
    """由目录树生成计划。**全程不联网。**"""
    root = cfg.root_path
    tree = tree.rebase(root)
    dup = _dup_counts(tree)

    plan = Plan(
        created=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        root=root,
        tree_stats=tree.stats(),
        config={k: v for k, v in cfg.all().items() if k != "cookie"},
    )

    top_dirs = [d for d in tree.children_dirs(root)
                if tree.entries[d].name not in RESERVED_DIRS]
    # ⚠️ 去重：解析器对「同名同层」会补 `#2` 后缀，但 files 列表可能仍带重复项，
    #    不去重会让同一个文件产出两条动作（实测出现过）。
    top_files = list(dict.fromkeys(
        f for f in tree.files_of(root) if f not in RESERVED_DIRS))

    # ------------------------------------------------------------ ① 番号索引
    dir_id: dict[str, str] = {}
    for d in top_dirs:
        pid = number.get_id(tree.entries[d].name)
        if pid:
            dir_id[d] = pid
    loose_id: list[tuple[str, str]] = []
    for f in top_files:
        pid = number.get_id(f)
        if pid:
            loose_id.append((f, pid))

    series_count: Counter = Counter()
    for pid in dir_id.values():
        series_count[number.series_of(pid)] += 1
    for _, pid in loose_id:
        series_count[number.series_of(pid)] += 1
    plan.series_stats = [
        {"series": s, "count": n,
         "grouped": bool(cfg.series_enable and n >= cfg.series_min)}
        for s, n in series_count.most_common()
    ]

    move_ops: list[Op] = []
    other_ops: list[Op] = []
    claimed: dict[str, str] = {}

    # ------------------------------------------------------------ ② 番号目录 → 系列
    for d in top_dirs:
        pid = dir_id.get(d)
        if not pid:
            continue
        name = tree.entries[d].name
        avid = number.format_id(pid)
        series = number.series_of(pid)
        n = series_count[series]
        grouped = bool(cfg.series_enable and n >= cfg.series_min)
        parent = d.rsplit("/", 1)[0] if "/" in d else ""

        if grouped:
            target, want = series, (avid if cfg.series_rename else name)
            reason = f"系列 {series} 共 {n} 部"
        elif cfg.series_rename and avid != name:
            target, want = parent, avid
            reason = f"系列 {series} 仅 {n} 部（<阈值 {cfg.series_min}），只规范命名"
        else:
            plan.skipped.append({"path": d, "reason": f"系列 {series} 仅 {n} 部，低于阈值"})
            continue

        # 目标重名的处理顺序：① 规范名 ② 撞了 ⇒ 退成「移动但不改名」（保原名不会撞）
        #   ③ 连原名都撞 ⇒ 记冲突、转人工。⛔ 绝不覆盖，也绝不静默丢弃。
        note = ""
        key = f"{target}/{want}"
        if key in claimed and claimed[key] != d:
            alt = f"{target}/{name}"
            if alt not in claimed:
                claimed[alt] = d
                want = name
                note = "目标重名，保留原名移动"
            else:
                plan.conflicts.append({"target": key, "sources": [claimed[key], d],
                                       "reason": "两个源归一化后指向同一目标"})
        else:
            claimed[key] = d

        if target == parent and want == name:
            continue
        move_ops.append(Op(kind="move_dir", path=d, target=target if target != parent else "",
                           new_name=want if want != name else "",
                           group="series", reason=reason, note=note))

    # ------------------------------------------------------------ ③ 目录内垃圾
    # 对所有一级子目录（含非番号目录）扫 max_depth 层
    for d in top_dirs:
        for sub, sub_name in _scan_dirs(tree, d, max_depth):
            for f in tree.files_of(sub):
                v = junk_mod.judge(f, dup_count=dup[f], dup_min=cfg.junk_dup_min,
                                   parent_name=sub_name)
                if v.level == junk_mod.LEVEL_STRONG:
                    other_ops.append(Op(kind="trash", path=f"{sub}/{f}", reason=v.reason,
                                        group="junk", auto=v.auto,
                                        note=_dup_note(v.reason, dup[f])))
                elif v.level == junk_mod.LEVEL_SUSPECT:
                    plan.suspects.append({"path": f"{sub}/{f}", "reason": v.reason, "dup": dup[f]})

    # ------------------------------------------------------------ ④ 根目录散文件
    tv_buckets: dict[tuple[str, int], list[str]] = defaultdict(list)
    id_by_file = {f: pid for f, pid in loose_id}
    for f in top_files:
        ext = junk_mod.split_ext(f)[1]
        pid = id_by_file.get(f)
        if pid:
            avid = number.format_id(pid)
            series = number.series_of(pid)
            grouped = bool(cfg.series_enable and series_count[series] >= cfg.series_min)
            target = f"{series}/{avid}" if grouped else root
            # ⚠️ 只有**认得扩展名**时才改文件名：115 的 fs_rename 要求名字必须带扩展名，
            #    扩展名认不出（如 `…@NongPink.CoM` 这种伪后缀）就只移动、不改名。
            new = f"{avid}.{ext}" if ext else ""
            other_ops.append(Op(kind="move_file", path=f, target=target,
                                new_name=new if new != f else "",
                                group="series", reason=f"散落的番号片 → {series} 系列"))
            continue

        kind = number.kind_of(f)
        if kind == number.Kind.TV and ext in junk_mod.VIDEO_EXT:
            title, season = _tv_title_season(f)
            tv_buckets[(title, season)].append(f)
            continue
        # 电影文件改名同样要求「有年份」—— 没年份的纯英文名交给 namer 会掉信息
        if kind == number.Kind.MOVIE and ext in junk_mod.VIDEO_EXT and _movie_renamable(f):
            new = _movie_name(f, cfg)
            if new and new != f:
                other_ops.append(Op(kind="rename", path=f, new_name=new, group="movie",
                                    reason="规范成 片名（年份）.ext"))
            continue

        v = junk_mod.judge(f, dup_count=dup[f], dup_min=cfg.junk_dup_min, parent_name=root)
        if v.level == junk_mod.LEVEL_STRONG:
            other_ops.append(Op(kind="trash", path=f, reason=v.reason, group="junk",
                                auto=v.auto, note=_dup_note(v.reason, dup[f])))
        elif v.level == junk_mod.LEVEL_SUSPECT:
            plan.suspects.append({"path": f, "reason": v.reason, "dup": dup[f]})

    # ------------------------------------------------------------ ⑤ 剧集收拢
    for (title, season), files in sorted(tv_buckets.items()):
        if not cfg.episode_group_enable:
            break
        if len(files) < cfg.episode_group_min or not title:
            plan.skipped.append({"path": f"（剧集）{title or files[0]}",
                                 "reason": f"只找到 {len(files)} 集，低于阈值 {cfg.episode_group_min}"})
            continue
        season_name = f"第{season}季" if season else "第1季"
        season_dir = f"{title}/{season_name}"
        for f in sorted(files):
            new = namer.episode_name(f, series_title=title) or f
            other_ops.append(Op(kind="move_file", path=f, target=season_dir,
                                new_name=new if new != f else "", group="tv",
                                reason=f"{title} 第 {season or 1} 季，共 {len(files)} 集"))

    # ------------------------------------------------------------ ⑥ 电影 / 剧集目录
    for d in top_dirs:
        if d in dir_id:
            continue
        name = tree.entries[d].name
        kind = number.kind_of(name)
        if kind == number.Kind.MOVIE and _movie_renamable(name):
            cands = namer.suggest(name, paren=cfg.paren)
            new = next((c["name"] for c in cands if c["mode"] != "keep"), "")
            if new and new != name:
                other_ops.append(Op(kind="rename", path=d, new_name=new, group="movie",
                                    reason="规范成 片名（年份）"))
        elif kind == number.Kind.TV:
            plan.suspects.append({
                "path": d,
                "reason": "疑似剧集目录 —— 集文件收拢目前只处理根目录散文件，这条留人工",
                "dup": 0,
            })

    # ------------------------------------------------------------ ⑦ 冲突 → 转人工
    conflict_paths = {s for c in plan.conflicts for s in c["sources"]}
    for op in move_ops + other_ops:
        if op.path in conflict_paths:
            op.auto = False
            op.note = " · ".join(x for x in (op.note, "目标冲突，需人工裁决") if x)

    # ------------------------------------------------------------ ⑧ 补建目标目录
    targets: list[str] = []
    for op in move_ops + other_ops:
        if op.kind in ("move_dir", "move_file") and op.target and op.target != root:
            targets.append(op.target)
    for t in sorted(set(targets), key=lambda x: (x.count("/"), x)):
        other_ops.append(Op(kind="mkdir", target=t, group="mkdir", reason="确保目标目录存在"))

    plan.ops = sorted(move_ops + other_ops, key=lambda o: (_ORDER.get(o.kind, 9), o.path, o.target))
    return plan


def _dup_note(reason: str, n: int) -> str:
    """重复次数只在 reason 里没写过时才补 —— 否则报告会出现「…×11 重复×11」这种废话。"""
    return f"全盘出现 {n} 次" if (n > 1 and "重复" not in reason) else ""


def _movie_renamable(name: str) -> bool:
    """能不能按中文影视规则改名。

    ⚠️ 两个硬条件：① 有年份 ② **原名字里有中文**。
       为什么必须有中文 —— `namer.title_of` 的规则是「只留含中文的段」（专为他那个中文影视库调的），
       拿纯英文名去套会掉信息。实测踩过：
         `6.Underground.2019.1080p.NF.WEBRip…` → 被改成 `Underground NF（2019）`（数字 6 没了）
       所以纯英文名**一律不改**，只在报告里列出。
    """
    return bool(namer.extract_year(name)) and number.has_cjk(name)


def _movie_name(fname: str, cfg: Config) -> str:
    for c in namer.suggest_file(fname, paren=cfg.paren):
        if c["mode"] != "keep":
            return c["name"]
    return ""
