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
from typing import Iterable

from . import junk as junk_mod
from . import namer, number
from .config import Config
from .treefile import Tree

__all__ = ["Op", "Plan", "build_plan", "build_plan_for_dirs", "estimate",
           "PLAN_VERSION", "RESERVED_DIRS"]

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

    # ------------------------------------------------------------------ 取子集
    def subset(self, indices: Iterable[int]) -> "Plan":
        """按**索引**取子集（索引以 `self.ops` 的当前顺序为准），保留全部元信息。

        ⭐ 为什么敢让人「只勾一部分就跑」—— **不做依赖补全**也不会出错：
           `move_dir` / `move_file` 的目标目录在执行器里走 `_resolve_dir()`
           ⇒ 那一刻会 `ensure_dir()` 现建（内部有缓存，重复调用零请求）。
           所以单独勾一条「移动」、不带它的「建目录」，照样能落到位。

        ⛔ 别改成「顺手把相关的 mkdir 也塞进来」：那会让「我明明只勾了 3 条，
           日志里却动了 50 条」这种事发生 —— 勾选的意义就是**所见即所得**。

        ⚠️ 越界/负数一律**丢掉**（不是报错）—— 请求可能在两次轮询之间过期，
           为此把整批拒绝掉太粗暴；丢掉的条数由调用方从 `len()` 差里看得出来。
        """
        total = len(self.ops)
        picked: list[Op] = []
        seen: set[int] = set()
        for i in indices or ():
            try:
                idx = int(i)
            except (TypeError, ValueError):
                continue
            if idx in seen or idx < 0 or idx >= total:
                continue
            seen.add(idx)
            picked.append(self.ops[idx])
        out = Plan(
            ops=picked,
            conflicts=list(self.conflicts),
            suspects=list(self.suspects),
            skipped=list(self.skipped),
            series_stats=list(self.series_stats),
            tree_stats=dict(self.tree_stats),
            config=dict(self.config),
            created=self.created,
            root=self.root,
        )
        return out

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
    # 根下条目数 —— 给耗时估算用。⚠️ `fs_files` **不递归**，列根目录只有这一层的量级，
    #    拿 `tree_stats["dirs"+"files"]`（整棵树）去算页数会高估一个数量级。
    plan.tree_stats["root_entries"] = len(top_dirs) + len(top_files)

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


# --------------------------------------------------------------------------- 耗时估算
def estimate(plan: Plan, cfg: Config) -> dict:
    """按**执行层实际会发的请求数**估算耗时 —— 比按动作数估准得多。

    请求数 ≈ 列根目录分页 + 目标目录创建 + 改名批次 + 移动批次 + 清理批次。

    ⚠️ 放在这里而不是 `cli.py`，是因为**网页上勾一部分之后也要重新估一次**
       （勾 50 条和勾 5000 条是完全两回事）⇒ 它必须是能被复用的库函数，
       不能只长在命令行里。
    """
    from .throttle import Throttle

    thr = Throttle(cfg=cfg)
    n_mkdir = sum(1 for op in plan.ops if op.kind == "mkdir")
    n_rename = sum(1 for op in plan.ops if op.kind == "rename") + \
        sum(1 for op in plan.ops if op.kind in ("move_dir", "move_file") and op.new_name)
    n_move = sum(1 for op in plan.ops if op.kind in ("move_dir", "move_file"))
    n_trash = sum(1 for op in plan.ops if op.kind == "trash")
    # ⚠️「列根目录」的页数按**根下条目数**算 —— `fs_files` **不递归**，只列一层。
    #    拿整棵树的 dirs+files 会高估一个数量级（实测：根下 7,698 项 ⇒ 39 页；
    #    整棵树 71,944 项算出来是 360 页，白吓人）。
    root_entries = plan.tree_stats.get("root_entries")
    if not root_entries:
        root_entries = sum(1 for op in plan.ops if not op.src_dir) or 1
    root_pages = max(1, (root_entries + 199) // 200)
    # 下面各项都取**上界**（宁可估久，别让人以为很快）：清理那两项尤其粗 ——
    # 实际是「每个源目录只列一次」+「每 500 条才发一次移动请求」。
    est = (root_pages                                   # 列根目录
           + n_mkdir                                    # 建目标目录
           + (n_rename + 99) // 100                     # 改名分批
           + max(1, len({op.target for op in plan.ops if op.kind in ("move_dir", "move_file")}))
           + (n_move + 499) // 500                      # 移动分批
           + n_trash                                    # 清理要逐个目录列一次 + 移动
           + max(1, len({op.src_dir for op in plan.ops if op.kind == "trash"})))
    out = thr.estimate(est)
    out["breakdown"] = {
        "列根目录": root_pages, "建目录": n_mkdir,
        "改名批次": (n_rename + 99) // 100,
        "移动批次": max(1, len({op.target for op in plan.ops if op.kind in ("move_dir", "move_file")})),
        "清理相关": n_trash,
    }
    return out


# --------------------------------------------------------------------------- 选定文件夹
def build_plan_for_dirs(tree: Tree, cfg: Config, dirs: list[str], *, max_depth: int = 3) -> Plan:
    """只处理**选中的几个文件夹**（功能 2）。

    `dirs` —— 相对整理根的路径列表（如 `["DLDSS-532", "影视/动作"]`）。
    处理范围 = 这些目录**及其子树**（递归到 `max_depth` 层，不含再往下的子孙）。

    ⚠️ 与 `build_plan` 的区别：
      · **只碰选中范围** —— 别的目录一概不生成动作（哪怕是系列聚合也不会跨出去建目录）。
      · 番号归位仍生效：选中目录本身是番号 → 归位到系列；
        选中目录里的番号子目录 → 也归位（这些目录被选中的范围内）。
      · 系列聚合的「全盘数量」拿不到 ⇒ 规则跟入站口一致：
        系列目录**已存在**（在整理根下）就搬进去；否则看**本批**数量够不够阈值。
      · 目标目录（系列目录）如果**在选中范围之外**，也照建 —— 这是「把东西搬去该去的地方」。

    ⛔ 不动的：选中范围之外的任何文件 / 目录。
    """
    root = cfg.root_path
    tree = tree.rebase(root)

    # 选中目录展开成「要扫的目录集合」（含自身 + 子树到 max_depth 层）
    chosen = _expand_dirs(tree, dirs, max_depth=max_depth)
    if not chosen:
        return Plan(created=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    root=root, config={k: v for k, v in cfg.all().items() if k != "cookie"},
                    tree_stats={"source": "(选定文件夹)", "dirs": 0, "files": 0,
                                "root_entries": 0})

    dup = _dup_counts(tree)

    plan = Plan(
        created=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        root=root,
        tree_stats={"source": "(选定文件夹)", "dirs": len(chosen),
                    "files": sum(len(tree.files_of(p)) for p in chosen),
                    "root_entries": len(chosen)},
        config={k: v for k, v in cfg.all().items() if k != "cookie"},
    )

    # 整理根下已有的一级目录（判断「系列目录已存在」）
    root_dir_names = {tree.entries[p].name for p in tree.top_level if tree.entries[p].is_dir}
    root_dir_names |= {tree.entries[root].name}

    move_ops: list[Op] = []
    other_ops: list[Op] = []
    claimed: dict[str, str] = {}

    # ① 番号目录归位（选中的目录 / 子树里的目录，只要名字是番号）
    dir_id: dict[str, str] = {}
    for p in chosen:
        pid = number.get_id(tree.entries[p].name)
        if pid:
            dir_id[p] = pid

    series_count: Counter = Counter()
    for pid in dir_id.values():
        series_count[number.series_of(pid)] += 1

    for p in chosen:
        pid = dir_id.get(p)
        if not pid:
            continue
        name = tree.entries[p].name
        avid = number.format_id(pid)
        series = number.series_of(pid)
        n = series_count[series]
        series_exists = series in root_dir_names
        grouped = bool(cfg.series_enable and (series_exists or n >= cfg.series_min))
        parent = p.rsplit("/", 1)[0] if "/" in p else ""

        if grouped:
            target, want = series, (avid if cfg.series_rename else name)
            reason = f"系列 {series} 共 {n} 部"
        elif cfg.series_rename and avid != name:
            target, want = parent, avid
            reason = f"系列 {series} 仅 {n} 部（<阈值 {cfg.series_min}），只规范命名"
        else:
            plan.skipped.append({"path": p, "reason": f"系列 {series} 仅 {n} 部，低于阈值"})
            continue

        note = ""
        key = f"{target}/{want}"
        if key in claimed and claimed[key] != p:
            alt = f"{target}/{name}"
            if alt not in claimed:
                claimed[alt] = p
                want = name
                note = "目标重名，保留原名移动"
            else:
                plan.conflicts.append({"target": key, "sources": [claimed[key], p],
                                       "reason": "两个源归一化后指向同一目标"})
        else:
            claimed[key] = p

        if target == parent and want == name:
            continue
        move_ops.append(Op(kind="move_dir", path=p, target=target if target != parent else "",
                           new_name=want if want != name else "",
                           group="series", reason=reason, note=note))

    # ② 选中范围内的垃圾：
    #    a) **选中的目录本身**若命中强特征垃圾（如「文宣」「看片资源」这类广告外壳目录），
    #       整目录进清理 —— 用户选了它就是想要这个。⛔ 只有当它**不是番号目录**才适用
    #       （番号目录已归位处理，不能既归位又清理）。
    #    b) 目录内部的文件垃圾（递归扫到 max_depth）。
    for p in chosen:
        name = tree.entries[p].name
        if p not in dir_id and cfg.junk_allow_dir:      # 非番号目录 且 允许动目录
            vd = junk_mod.judge(name, dup_count=1, dup_min=cfg.junk_dup_min,
                                parent_name=tree.entries[p].parent or root)
            if vd.level == junk_mod.LEVEL_STRONG:
                other_ops.append(Op(kind="trash", path=p, reason=vd.reason,
                                    group="junk", auto=vd.auto,
                                    note="选中目录本身是广告/垃圾"))
                continue                                # 整目录清掉，不再扫内部
        for f in tree.files_of(p):
            v = junk_mod.judge(f, dup_count=dup[f], dup_min=cfg.junk_dup_min,
                               parent_name=name)
            if v.level == junk_mod.LEVEL_STRONG:
                other_ops.append(Op(kind="trash", path=f"{p}/{f}", reason=v.reason,
                                    group="junk", auto=v.auto,
                                    note=_dup_note(v.reason, dup[f])))
            elif v.level == junk_mod.LEVEL_SUSPECT:
                plan.suspects.append({"path": f"{p}/{f}", "reason": v.reason, "dup": dup[f]})

    # ③ 选中范围根上的散文件（只在 chosen 的最浅层 = 用户选的目录本身，不扫深层散文件）
    #    ⚠️ 深层目录的散文件归位已在上面逐目录扫；这里只处理「选中目录的直接子文件」。
    for p in chosen:
        if p not in tree.files:
            continue
        for f in tree.files_of(p):
            ext = junk_mod.split_ext(f)[1]
            pid = number.get_id(f)
            if pid:
                avid = number.format_id(pid)
                series = number.series_of(pid)
                n = series_count[series]
                series_exists = series in root_dir_names
                grouped = bool(cfg.series_enable and (series_exists or n >= cfg.series_min))
                target = f"{series}/{avid}" if grouped else root
                new = f"{avid}.{ext}" if ext else ""
                other_ops.append(Op(kind="move_file", path=f"{p}/{f}", target=target,
                                    new_name=new if new != f else "",
                                    group="series", reason=f"散落的番号片 → {series} 系列"))
                continue

            kind = number.kind_of(f)
            if kind == number.Kind.TV and ext in junk_mod.VIDEO_EXT:
                title, season = _tv_title_season(f)
                season_name = f"第{season}季" if season else "第1季"
                season_dir = f"{title}/{season_name}"
                # ⚠️ 剧集收拢：目标目录可能不在选中范围 —— 照建（东西该去的地方）
                tv_bucket = (title, season)
                if len(tree.files_of(p)) >= cfg.episode_group_min and title:
                    new = namer.episode_name(f, series_title=title) or f
                    other_ops.append(Op(kind="move_file", path=f"{p}/{f}", target=season_dir,
                                        new_name=new if new != f else "", group="tv",
                                        reason=f"{title} 第 {season or 1} 季（选中范围内收拢）"))
                else:
                    plan.skipped.append({"path": f"{p}/{f}",
                                         "reason": f"剧集 {title or f} 集数不足"})
                continue

            if kind == number.Kind.MOVIE and ext in junk_mod.VIDEO_EXT and _movie_renamable(f):
                new = _movie_name(f, cfg)
                if new and new != f:
                    other_ops.append(Op(kind="rename", path=f"{p}/{f}", new_name=new,
                                        group="movie", reason="规范成 片名（年份）.ext"))
                continue

            v = junk_mod.judge(f, dup_count=dup[f], dup_min=cfg.junk_dup_min, parent_name=root)
            if v.level == junk_mod.LEVEL_STRONG:
                other_ops.append(Op(kind="trash", path=f"{p}/{f}", reason=v.reason,
                                    group="junk", auto=v.auto,
                                    note=_dup_note(v.reason, dup[f])))
            elif v.level == junk_mod.LEVEL_SUSPECT:
                plan.suspects.append({"path": f"{p}/{f}", "reason": v.reason, "dup": dup[f]})

    # ④ 冲突 → 转人工
    conflict_paths = {s for c in plan.conflicts for s in c["sources"]}
    for op in move_ops + other_ops:
        if op.path in conflict_paths:
            op.auto = False
            op.note = " · ".join(x for x in (op.note, "目标冲突，需人工裁决") if x)

    # ⑤ 补建目标目录
    targets: list[str] = []
    for op in move_ops + other_ops:
        if op.kind in ("move_dir", "move_file") and op.target and op.target != root:
            targets.append(op.target)
    for t in sorted(set(targets), key=lambda x: (x.count("/"), x)):
        other_ops.append(Op(kind="mkdir", target=t, group="mkdir", reason="确保目标目录存在"))

    plan.ops = sorted(move_ops + other_ops, key=lambda o: (_ORDER.get(o.kind, 9), o.path, o.target))
    return plan


def _expand_dirs(tree: Tree, dirs: list[str], *, max_depth: int = 3) -> list[str]:
    """把选中的目录路径展开成「要处理的目录集合」（自身 + 子树，到 max_depth 层）。

    ⛔ 不存在的路径静默丢掉（网页勾选后 115 上目录可能已变）。
    """
    out: list[str] = []
    for d in dirs or []:
        d = d.strip("/")
        if not d or d not in tree.entries or not tree.entries[d].is_dir:
            continue
        out.append(d)
        # BFS 展开子树
        frontier = [d]
        for _ in range(max(0, max_depth - 1)):
            nxt: list[str] = []
            for p in frontier:
                for c in tree.children_dirs(p):
                    if tree.entries[c].name in RESERVED_DIRS:
                        continue
                    out.append(c)
                    nxt.append(c)
            frontier = nxt
            if not frontier:
                break
    # 去重保序
    return list(dict.fromkeys(out))
