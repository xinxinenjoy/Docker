"""同步引擎 —— 把 115 的目录结构镜像成本地 strm 树。

## 一轮同步做什么

    ① 拿「网盘现状」  —— 来源见下（目录树 / 事件流 / API 实时列）
    ② 拿「本地现状」  —— 扫 output_dir 下所有 .strm
    ③ 算差异          —— 新增 / 内容变了 / 本地多余的
    ④ 落盘            —— 建目录 + 写 strm + （确认后）删多余的
    ⑤ 出回执          —— 人看的 markdown + 机器看的 json

## 🔴 「网盘现状」只有**一个**来源（2026-10-08 红领巾定）

    **自动导出目录树**（`v115.export_tree_bytes`）

    「导出目录树」本身就是一个 115 接口 ⇒ 让工具自己调它。
    成本是**任务级**的（提交 + 轮询 + 下载 + 删临时件 ≈ 十几次请求，与库大小无关），
    对比「逐层列目录」的**目录级**成本（本盘 1,183 个目录 ⇒ 上千请求 + 十几分钟）——
    差几个数量级。**而且你一次都不用手动导。**

    ~~生活事件流~~ / ~~API 逐层列目录~~ —— 2026-10-08 一并退役（理由见 `pipeline` 顶部）。
    所以本模块原先那套「增量落盘」（`apply_incremental` / `_expand_deletes` / fid 索引）
    也一起删了：**只剩全量对账这一条路**。

> 一句话：**导出目录树不是「人工步骤」，它是工具内部的一个动作。**

## ⭐ 「内容变了才重写」这条判据，**本来就只依赖目录树**

红领巾问过：换成只看目录树之后，strm 被改过的判断还成立吗？—— **成立，一个字都不用改。**

判据在本文件 `Differ.plan()`：

    if self.read_existing(key) != f.url:   # 读**本地文件真实内容**（不是 mtime）去比
        res.to_update.append(f)

`f.url` 是按**目录树 + 当前设置**算出来的地址。于是：

    · 你改了 strm 前缀        ⇒ 全部 URL 变了       ⇒ 全进 to_update、被重写 ✅
    · 你手改了某个 strm 内容  ⇒ 跟算出来的不一样     ⇒ 被改回 ✅
    · 你删了某个 strm         ⇒ 不在本地键里         ⇒ 进 to_add、被补回 ✅

⇒ **完全不看 115 的任何变更标记**，所以跟「数据来源」无关，去掉事件流对它零影响。

## 为什么「本地现状」必须实扫

不能拿一份「上次写出了什么」的账本当依据 —— 账本会和磁盘漂移
（他手动删了几个、同步盘冲突件、容器重建丢了 data 卷……）。
**磁盘是唯一真相源**，账本只用来加速。

## ⛔ 安全线

    · `dry_run` 打开时**一个文件都不写、一个都不删**
    · 删除**永远**要过「延迟二次确认」（`delete_defer_minutes`）——
      本轮发现有消失 ⇒ 记进 pending，等 N 分钟后再扫一次，确认了才真删
    · 一轮里消失比例 > `delete_ratio_guard` ⇒ 判「异常」（多半是 cookie 失效）
      ⇒ 本轮**一律不删**，只报告
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .namer import DiffResult, StrmBuilder, StrmFile, is_video, local_path_of, safe_name
from .throttle import Throttle
from .v115 import V115, V115Error, Node

__all__ = ["Syncer", "SyncResult", "Pending"]

# 本地账本：记「上一轮看到的网盘结构」的指纹，用来判断要不要重算
STATE_NAME = "sync-state.json"
PENDING_NAME = "pending-delete.json"


def _safe_rel(rel: str) -> str:
    """相对路径 → 本地安全相对路径（**每一段**都过 `safe_name`）。

    ⛔ 必须逐段做：`a:b/c.mkv` 里的冒号在 Windows 上会被当盘符，
       整条路径 `mkdir` 直接失败。目录段和文件段都不能漏。
    """
    return "/".join(safe_name(p) for p in rel.split("/") if p)


def keep_only_file_dirs(dirs: Iterable[str], files: Iterable[Any]) -> list[str]:
    """**只保留「至少是一个 strm 的祖先」的目录**（保持原顺序）。

    🔴 红领巾 2026-10-08：

        「后续应该不会存在无拓展名的目录。**没有具体文件的目录直接抛弃不生成**」

    于是「空目录也建出来（方便对照缺失）」这个特性整体退役 —— 它带来的是
    一堆媒体库里点进去什么都没有的空文件夹，收益远小于噪音。

    做法刻意选「**从文件反推祖先**」而不是「递归时边下边判断」：
    递归是自上而下的，走到某一层时还不知道它底下有没有文件；
    反过来从结果集合算祖先，**一次就算准**，也不用改递归结构。

    ⚠️ 判据是「**要生成的 strm**」，不是「有没有任何文件」——
       一个目录里只有 `.srt` / `.nfo` 时同样不建（本工具只镜像视频）。
    """
    keep: set[str] = set()
    for f in files:
        rel = str(getattr(f, "rel", f) or "")
        parts = rel.split("/")[:-1]          # 去掉文件名那一段
        for i in range(1, len(parts) + 1):
            keep.add("/".join(parts[:i]))
    return [d for d in dirs if d in keep]


@dataclass
class SyncResult:
    """一轮同步的回执（就是网页上要显示的东西）。"""
    started: str = ""
    finished: str = ""
    elapsed: str = ""
    source: str = ""                       # 数据来源（现在恒为 tree）
    dry_run: bool = True
    scanned_remote: int = 0                # 网盘上看到的条目数
    scanned_local: int = 0                 # 本地已有 .strm 数
    written: int = 0
    unchanged: int = 0
    dirs_made: int = 0
    # ---- 「目录只跟文件走」的两项计数（2026-10-08 红领巾二轮）----
    dirs_skipped: int = 0                  # 整棵子树都没有 strm ⇒ 没建的目录数
    dirs_pruned: int = 0                   # 收掉的本地空目录数
    extensionless: int = 0                 # 树里「既无子项又无扩展名」的条目数（疑似截断）
    deleted: int = 0
    pending_delete: int = 0                # 挂起等二次确认的
    renamed: int = 0                       # 本地改过名、被认出来的（原样保留）
    requests: int = 0
    errors: list[dict] = field(default_factory=list)
    guard_tripped: bool = False            # 异常比例触发
    guard_reason: str = ""
    looks_like_move: bool = False          # 判定为「改名/移动」而非「异常」
    diff: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = dict(vars(self))
        return d


@dataclass
class Pending:
    """挂起删除的候选 —— 等二次确认。

    ⚠️ 为什么要它：115 的删除是**异步**的，而且列表**带缓存** ——
       某一轮「没看到某文件」完全可能是暂时的（接口抖动 / 缓存 / 权限瞬时问题）。
       直接删本地 strm 会让播放器丢条目；等一轮再确认，代价小得多。
    """
    paths: list[str] = field(default_factory=list)
    first_seen: float = 0.0                # unix 秒，第一次发现它们消失的时刻
    seen_times: int = 0                    # 第几次确认了（用于排查）

    def age_minutes(self, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        return (now - self.first_seen) / 60.0 if self.first_seen else 0.0


class Syncer:
    """一轮同步的执行者。"""

    def __init__(self, v: V115 | None, cfg: Config, log: Any = None):
        self.v = v
        self.cfg = cfg
        self.log = log or (lambda *a, **k: None)
        self.out = Path(cfg.output_dir)
        self.builder = StrmBuilder.from_cfg(cfg)
        self.state_path = Path(cfg.data_dir) / STATE_NAME
        self.pending_path = Path(cfg.data_dir) / PENDING_NAME
        # 走目录树时攒下的一轮备注（跳过多少空目录、多少条目疑似被截断）
        # —— 给回执和日志用；`walk_from_tree` 填、调用方读。
        self.tree_notes: dict[str, Any] = {}

    # ------------------------------------------------------------------ 本地现状
    def scan_local(self) -> dict[str, float]:
        """扫本地已有 strm → `{相对路径: mtime}`。

        ⚠️ 用 `os.walk` 而不是 `Path.rglob` —— 大目录（几万文件）下前者快得多，
           而且我们要跳过隐藏目录（`.@` / `@eaDir` 这类群晖同步垃圾）。

        🔴 **返回值里的分隔符**统一成 `/`（POSIX 风格），**必须显式转**：
           `os.walk` 在 Windows 上给的是反斜杠 —— 不转的话本地键是
           `115电影\\21座桥…`、网盘侧是 `115电影/21座桥…`，**一个都对不上**，
           于是每一条本地文件都被判成「网盘上没有了」⇒ 全库被误标删除。
           （2026-10-08 在 Windows 上实测踩到；群晖 Linux 上因为 `os.sep` 就是 `/`
             不会触发 —— 属于会「换台机器才爆」的隐蔽 bug。当时靠异常比例闸门拦下了，
             但那个闸门是最后一道防线，不能当常态依赖。）
        """
        found: dict[str, float] = {}
        if not self.out.exists():
            return found
        root_str = str(self.out).rstrip("/\\")
        root_len = len(root_str) + 1
        for dirpath, dirnames, filenames in os.walk(self.out):
            # 跳过群晖特有的垃圾目录与隐藏目录
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in ("@eaDir", "#recycle")]
            for fn in filenames:
                if not fn.endswith(".strm"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = full[root_len:].replace("\\", "/")     # ← 🔴 统一分隔符
                try:
                    found[rel] = os.path.getmtime(full)
                except OSError:
                    found[rel] = 0.0
        return found

    def read_existing(self, rel: str) -> str:
        """读一个已存在的 strm 内容（用于判断要不要更新）。"""
        try:
            return (self.out / _safe_rel(rel)).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    # ------------------------------------------------------------------ 网盘现状
    def walk_remote(self, cid: str = "", rel: str = "") -> tuple[list[str], list[StrmFile]]:
        """递归列网盘 → `(目录相对路径列表, 要生成的 strm 列表)`。

        ⚠️ **这是请求最重的一条路**（请求量 ≈ 目录数）⇒ 只在没有目录树、
           也没法用事件流时才走。日常增量**不要**调它。

        ⚠️ 本函数**递归**地建 `dirs`，所以过滤「没有文件的目录」放在最外层的
           `walk_remote_filtered()` 里做（递归中段还不知道子树底下有没有文件）。
        """
        dirs: list[str] = []
        files: list[StrmFile] = []
        if cid == "":
            cid = self.v.root_id()

        for node in self.v.list_dir(cid):
            child_rel = f"{rel}/{node.name}" if rel else node.name
            if node.is_dir:
                dirs.append(child_rel)
                d2, f2 = self.walk_remote(node.id, child_rel)
                dirs.extend(d2)
                files.extend(f2)
            elif is_video(node.name, self.cfg.exts):
                # strm 路径：把扩展名换掉（`xxx.mkv` → `xxx.mkv.strm`）
                # ⚠️ 为什么保留原扩展名：Emby 这类媒体服务器靠后缀识别媒体类型，
                #    `xxx.mkv.strm` 比 `xxx.strm` 更容易被正确归类（社区通行做法）。
                files.append(StrmFile(
                    rel=child_rel,
                    url=self.builder.url(child_rel),
                    fid=node.id,
                    size=node.size,
                ))
        return dirs, files

    def walk_remote_filtered(self) -> tuple[list[str], list[StrmFile]]:
        """`walk_remote()` + **丢掉「整棵子树都没有视频」的目录**。

        与走目录树那条路（`walk_from_tree`）**同一条规则** —— 红领巾 2026-10-08：
        「没有具体文件的目录直接抛弃不生成」。两条路的口径必须一致，
        否则「同一个库、换个数据来源，本地目录就不一样」会非常难查。
        """
        dirs, files = self.walk_remote()
        return keep_only_file_dirs(dirs, files), files

    def walk_from_tree(self, tree: Any, base: str = "") -> tuple[list[str], list[StrmFile]]:
        """从**目录树导出件**离线算出网盘结构 —— 零请求。

        ⚠️ **基准路径是 `cfg.remote_root`，不是 `tree.root_path`** —— 这是本工具
           最容易错的路径口径，2026-10-08 用真实树（根是 `根目录`、影音库在 `115影音`）
           实测踩到过：

               prefix  = https://域名:5244/d/影音/115影音   ← 已经含了挂载路径
               相对路径 = 115电影/功夫女足（2026）/x.mkv      ← 必须**不含** `115影音`

           若拿 `tree.root_path`（`根目录`）当基准，相对路径会变成
           `115影音/115电影/…`，拼出来是 `…/d/影音/115影音/115影音/115电影/…`
           —— **重复一层**，alist 找不到存储。

        ⭐ 白名单（`cfg.includes`）：非空时**只走列出的子树**，其余完全不碰。
           这是红领巾 2026-10-08 要的「只处理电影和电视剧」。

        🔴 2026-10-08 二轮定调 —— **两条结构性规则**（红领巾原话）：

            「后续应该不会存在无拓展名的目录。没有具体文件的目录直接抛弃不生成」
            「之前目录树有空的是因为层级选得太少，很多子目录没生成」

          · **无扩展名的叶子条目不再当目录建**（上一版的 `noext_as_dir` 整个作废）：
            那种条目是**被截断的子树**，不是空目录 ⇒ 只统计、只告警，不建任何东西。
            真正的截断由 `v115.export_tree_bytes` 按 25 层上限重导来避免。
          · **整棵子树一个视频都没有的目录，不建**：
            目录列表改为「**从文件反推**」—— 只有作为某个 strm 的祖先目录才留下。
            于是「空目录也建」这个特性整体退役。

        ⚠️ 树里没有文件 id（导出件不含 id）⇒ 这类生成的 strm `fid` 为空，
           删除判定只能靠「路径是否存在」。不影响正确性，只是少一层交叉验证。
        """
        dirs: list[str] = []
        files: list[StrmFile] = []
        missing: list[str] = []          # 白名单里填了、但树里没有的路径

        # ① 找基准节点（相对树根的路径，如 `115影音`）
        rel_root = (base or self.cfg.remote_root or "").strip("/")
        try:
            root_node = tree.resolve(rel_root)
        except Exception as exc:
            raise V115Error(
                f"目录树里找不到同步根 `{rel_root}`：{exc}\n"
                f"  ⇒ 去「设置 › 同步范围」里选一个树里真实存在的目录"
            ) from exc

        # ② 决定起点：白名单非空 ⇒ 从白名单的每个目录开始；否则从同步根开始
        includes = self.cfg.includes
        starts: list[tuple[str, str]] = []      # (树内路径, 相对同步根的路径)
        if includes:
            for inc in includes:
                try:
                    node = tree.resolve(f"{rel_root}/{inc}")
                except Exception:
                    missing.append(inc)
                    continue
                starts.append((node, inc))
        else:
            starts.append((root_node, ""))

        # ③ 逐棵子树走
        def walk(node_path: str, rel_prefix: str) -> None:
            for child in tree.children_dirs(node_path):
                name = tree.entries[child].name
                rel = f"{rel_prefix}/{name}" if rel_prefix else name
                dirs.append(rel)
                walk(child, rel)
            for fname in tree.files_of(node_path):
                rel = f"{rel_prefix}/{fname}" if rel_prefix else fname
                if is_video(fname, self.cfg.exts):
                    files.append(StrmFile(rel=rel, url=self.builder.url(rel)))

        for node, rel_prefix in starts:
            # ⚠️ 白名单命中的目录**本身不建**（它已经是同步根下的层级），只走它下面的
            walk(node, rel_prefix)

        # ④ ⭐ 目录列表**从文件反推**：只保留「至少一个 strm 的祖先」。
        #
        #    红领巾 2026-10-08：「没有具体文件的目录直接抛弃不生成」。
        #    规则与 `walk_remote_filtered()` **同一份实现**（`keep_only_file_dirs`），
        #    否则「同一个库换个数据来源、本地目录就不一样」会非常难查。
        dirs_all = len(dirs)
        dirs = keep_only_file_dirs(dirs, files)
        skipped_dirs = dirs_all - len(dirs)

        # ⑤ 「既没有子项、也没有扩展名」的叶子条目 ⇒ 只统计、只提示，**不建任何东西**。
        #    ⚠️ 上一版把这 386 个当成「空目录」建了出来，判断错了。
        #    🔴 2026-10-08 晚用**两棵真树对照**才把它说清：
        #       · 手工导的树（层级没拉满）—— 386 个，全是被**截断的子目录**
        #       · 工具自导的树（固定 25 层）—— **只有 3 个**，且都是**真实存在的
        #         无扩展名文件**（发布者塞在片目录里的推广文件，如 `6v电影APP 有他不迷路`）
        #    ⇒ 所以这话**不能说死**是截断：单看一棵树区分不了这两种，
        #       只能拿「数量」旁证（几十上百基本就是截断）。无论哪种都**一律不建**。
        ext_less = tree.extensionless()
        self.tree_notes = {
            "skipped_dirs": skipped_dirs,
            "extensionless": len(ext_less),
            "extensionless_sample": ext_less[:5],
        }
        if ext_less:
            many = len(ext_less) >= 20
            self.log("warning" if many else "info",
                     f"{'⚠️ ' if many else ''}目录树里有 {len(ext_less)} 个条目"
                     f"**既没有子项、也没有扩展名**（例如：{ext_less[0]}）")
            if many:
                self.log("warning",
                         "   这个数量基本可以断定是**导出时层级选少了**（子树被截断）——"
                         "重导时记得把层级拉到最大。工具自己导的不受影响（固定 25 层）。")
            else:
                self.log("info",
                         "   这么少，多半是盘上**真实存在的无扩展名文件**"
                         "（发布者塞的推广文件之类），不是截断。")
            self.log("info", "   这两种都**一律不建**（免得建出一堆假空目录）。")
        if skipped_dirs:
            self.log("info",
                     f"{skipped_dirs} 个目录**整棵子树都没有要生成的视频** ⇒ 一个都不建"
                     f"（只处理有具体文件的目录）")

        if missing:
            raise V115Error(
                "白名单里有目录在树里不存在：\n  " + "\n  ".join(missing) +
                f"\n  ⇒ 检查「设置 › 同步范围」；同步根是 `{rel_root}`，"
                f"白名单要填**它下面**的名字（如 `115电影`）"
            )
        return dirs, files

    # ------------------------------------------------------------------ 差异
    def diff(self, remote_files: list[StrmFile], local: dict[str, float]) -> DiffResult:
        """算差异 —— **两级匹配**。

        ## 🔴 第一级：路径直配（覆盖 99% 的情况）

        本地键 = `_safe_rel(f.local_rel)`（磁盘上的名字是安全化之后的）。
        ⚠️ 用网盘原名去比会**永远判定为「新增」**、每轮全量重写一遍。

        ⚠️ **这一级不是「零 IO」**（2026-10-09 订正了一处措辞）：
           **键的查找**确实是内存里的字典比对（零 IO），但**命中之后要读一次文件内容**
           —— 见下面循环里的 `read_existing(key)`，那是用来判「这个文件**要不要重写**」的
           （URL 变了才写，避免每轮全刷 mtime 让媒体库反复重扫）。
        ⇒ 所以每轮的**固定成本** = `os.walk` 一遍 + **读全部已存在的 strm 内容**
           （实测 825 个文件：0.006 s + 0.016 s ≈ **0.022 s**，见 pipeline.py 的实测表）。
        ⛔ 别想着「省掉读内容、改用 mtime 判」：mtime 判不出内容变化，
           网盘地址变了（如换了 strm 前缀）就检测不出来，文件会**停在旧地址上** ⇒ 得不偿失。

        ## ⭐ 第二级：内容反查（给「本地改过名」兜底）

        ⭐ 触发条件很窄：**「有路径配不上的网盘条目」且「有没被认领的本地文件」**
           —— 两个都非空才进。名字全规矩的一轮里，`unmatched` 是空的 ⇒ **整段不执行**。

        红领巾 2026-10-08 的需求：

        > 「修改 strm 的名称不应该被重新生成 —— 因为内部的数据并没有变化，
        >   而是有特殊需求需要修改 strm 的名字。」

        原来「本地路径 == 网盘路径」这个**隐式约定**在当索引，本地一改名就断了。
        现在加一层：对**路径配不上的**网盘条目，去本地那些「没被认领」的 strm 里
        **按内容（URL）反查**：

            strm 的内容天然唯一指向一个网盘文件 ⇒ **内容本身就是对应关系**。

        ⇒ 你把 `a.mkv.strm` 改名成 `我改的名字.mkv.strm`：
          路径对不上，但内容对得上 ⇒ 认出「同一个文件」⇒ **内容一致、就地不动**。

        **为什么不做成一本显式的「账本」文件**：账本会**漂移** ——
        用户直接改文件系统、账本丢了/坏了、Seagate/Seafile 冲突件……
        都会让它和现实对不上，而账本一旦错就是**误删**。
        内容当键是**自校验**的：对不上就是真的对不上。

        ⭐ 顺带的好处：**用户手动新增的、指向真实网盘文件的 strm 也会被保留**
        （内容对得上 ⇒ 被认领），不再一律当孤儿删掉。

        ⚠️ 性能：只对「路径没配上的」条目 + 「没被认领的本地文件」做内容读取，
           通常各只有几条，不会因为库大而变慢。
        """
        res = DiffResult()

        # ---- 第一级：路径直配 ----
        used_local: set[str] = set()
        unmatched: list[tuple[str, StrmFile]] = []       # (期望本地键, 条目)
        for f in remote_files:
            key = _safe_rel(f.local_rel)
            if key in local:
                used_local.add(key)
                # 内容变了才重写（避免每次全量刷 mtime，让媒体库反复重扫）
                if self.read_existing(key) != f.url:
                    res.to_update.append(f)
            else:
                unmatched.append((key, f))

        # ---- 第二级：内容反查（只在真有配不上的条目时才做）----
        orphans = sorted(set(local) - used_local)
        if unmatched and orphans:
            by_url: dict[str, list[str]] = {}
            for rel in orphans:
                u = self.read_existing(rel)
                if u:
                    by_url.setdefault(u, []).append(rel)

            still_unmatched: list[StrmFile] = []
            for key, f in unmatched:
                hit = by_url.get(f.url)
                if hit:
                    rel = hit.pop(0)          # 同一个 URL 有多份时，一份认领一个
                    used_local.add(rel)
                    # ⭐ 内容一致 ⇒ 什么都不用做，只记下来给他看
                    res.renamed.append((rel, f))
                else:
                    still_unmatched.append(f)
            res.to_add = still_unmatched
        else:
            res.to_add = [f for _k, f in unmatched]

        # ---- 剩下来的本地文件 = 网盘上确实没有了 ----
        res.to_delete = sorted(set(local) - used_local)
        return res

    # ------------------------------------------------------------------ 落盘
    def write(self, files: Iterable[StrmFile], res_holder: SyncResult) -> None:
        """写 strm（含建目录）。

        ⚠️ **每一段路径都要过 `safe_name`** —— 网盘上可以叫 `a:b.mkv`，
           本地文件系统不行（Windows 直接建不出目录，Linux 上则是个带冒号的名字，
           同步到别的机器就炸）。⛔ 曾经只对目录段做了安全化，文件名漏了，
           症状是 `mkdir` 抛 `FileNotFoundError: 'a:'`（Windows 把冒号前当盘符）。
        """
        made: set[str] = set()
        for f in files:
            target = self.out / _safe_rel(f.local_rel)
            parent = target.parent
            if str(parent) not in made:
                parent.mkdir(parents=True, exist_ok=True)
                made.add(str(parent))
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(f.url + "\n", encoding="utf-8")
            os.replace(tmp, target)          # 原子写：别让媒体库读到半个文件
            res_holder.written += 1
        res_holder.dirs_made = len(made)

    def mkdirs(self, dirs: Iterable[str]) -> int:
        """只建目录。

        🔴 2026-10-08 起传进来的 `dirs` **已经过滤过**（`keep_only_file_dirs`）：
           只包含「至少是一个 strm 的祖先」的目录 ⇒ **不会再有空目录被建出来**。
           上一版这里是「空目录也要建 —— 才好对照缺失」，那个定位已被红领巾否掉，
           见 `keep_only_file_dirs` 的说明。
        """
        made = 0
        for rel in dirs:
            parts = [safe_name(p) for p in rel.split("/") if p]
            if not parts:
                continue
            p = self.out.joinpath(*parts)
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)
                made += 1
        return made

    def prune_empty_dirs(self, keep: Iterable[str], *, dry: bool = False) -> list[str]:
        """收掉**本地那些空掉的、且这一轮也不需要存在的**目录（自下而上）。

        为什么要有它：上一版按「空目录也建」跑过（本版那盘假空壳留下的），
        新规则不再生成空目录了，但**已经躺在那儿的得能收掉** ——
        否则媒体库里永远点进去一片空白，看着像工具坏了。

        ⛔ 边界（只删**空目录**，比删 strm 更保守）：
           · 里面有**任何**文件（`.strm` / `.srt` / `.nfo` / 你手放的任何东西）⇒ 跳过
           · 还有子目录没删掉（非空）⇒ 跳过
           · `output_dir` 自己**永不删**
           · 隐藏目录 / 群晖垃圾（`.@`、`@eaDir`、`#recycle`）不碰
           · 这一轮本该存在的目录（在 `keep` 里）不删
           · `dry=True` 只算不删（`dry_run` 打开时走这条）

        返回**被删掉的**相对路径列表（`dry` 时是「本来要删的」）。
        """
        root = self.out.resolve()
        if not root.is_dir():
            return []
        keep_norm = {_safe_rel(k) for k in keep if str(k or "").strip()}
        victims: list[str] = []
        # `topdown=False` ⇒ 先给最深的目录 —— 删掉之后父目录才可能跟着变空
        for dirpath, dirnames, filenames in os.walk(self.out, topdown=False):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in ("@eaDir", "#recycle")]
            p = Path(dirpath)
            if filenames:
                continue
            try:
                rp = p.resolve()
                if rp == root:
                    continue
                if any(p.iterdir()):        # 非空（还有子目录）⇒ 留着
                    continue
                rel = str(rp.relative_to(root)).replace("\\", "/")
            except (OSError, ValueError):
                continue
            if rel in keep_norm:
                continue
            if dry:
                victims.append(rel)
                continue
            try:
                p.rmdir()
                victims.append(rel)
            except OSError:
                continue
        return victims

    def remove(self, rels: Iterable[str]) -> int:
        """删本地 strm（**只删 .strm 文件**，绝不删目录、绝不删非 strm）。

        ⚠️ 删完顺手清掉空目录（网盘上目录没了，本地留个空壳会让媒体库报错）。
           只往上清到 output_dir 为止，**绝不越界**。

        🔴 越界判据用「解析后必须是 output_dir 的子孙」而不是 `relative_to` ——
           后者对 `out/../danger` 这种**通过了**（它确实在 out 的相对范围内），
           但实际路径已经跑到外面去了。必须先 `resolve()` 再比。
        """
        n = 0
        touched: set[Path] = set()
        root = self.out.resolve()
        for rel in rels:
            rel = str(rel).replace("\\", "/")     # ⚠️ 兼容反斜杠（老挂起清单 / Windows 产物）
            # 双保险 ①：必须是 .strm
            if not rel.endswith(".strm"):
                continue
            # 双保险 ②：解析后必须真在 output_dir 里面
            try:
                p = (self.out / _safe_rel(rel)).resolve()
            except (OSError, ValueError):
                continue
            if p != root and root not in p.parents:
                self.log("warning", f"拒绝删除越界路径：{rel}")
                continue
            try:
                p.unlink()
                n += 1
                touched.add(p.parent)
            except OSError as exc:
                self.log("warning", f"删不掉 {rel}：{exc}")
        # 清理空目录（自下而上）
        for d in sorted(touched, key=lambda x: -len(str(x))):
            cur = d
            while cur != root and cur.is_dir():
                try:
                    if any(cur.iterdir()):
                        break
                    cur.rmdir()
                    cur = cur.parent
                except OSError:
                    break
        return n

    # ------------------------------------------------------------------ 挂起删除
    def load_pending(self) -> Pending:
        if not self.pending_path.exists():
            return Pending()
        try:
            d = json.loads(self.pending_path.read_text(encoding="utf-8"))
            return Pending(paths=list(d.get("paths") or []),
                           first_seen=float(d.get("first_seen") or 0),
                           seen_times=int(d.get("seen_times") or 0))
        except Exception:
            return Pending()

    def save_pending(self, p: Pending) -> None:
        self.pending_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.pending_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "paths": p.paths, "first_seen": p.first_seen, "seen_times": p.seen_times,
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.pending_path)

    def clear_pending(self) -> None:
        try:
            self.pending_path.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------ 总流程
    def run(self, *, remote_files: list[StrmFile], remote_dirs: list[str],
            source: str = "api", force_delete: bool = False) -> SyncResult:
        """跑一轮。调用方负责把「网盘现状」拿到并传进来（三条路各自组装）。

        `force_delete=True` ⇒ 跳过挂起等待，立刻删（界面上「确认删除」按这个）。
        """
        t0 = time.time()
        res = SyncResult(
            started=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            source=source, dry_run=bool(self.cfg.dry_run),
            scanned_remote=len(remote_files),
        )

        local = self.scan_local()
        res.scanned_local = len(local)
        res.diff = {}
        # 走目录树时攒下的备注（跳过多少目录、多少条目疑似被截断）——
        # 由 `walk_from_tree` 填；走别的路时是空的，计数保持 0。
        notes = self.tree_notes or {}
        res.dirs_skipped = int(notes.get("skipped_dirs") or 0)
        res.extensionless = int(notes.get("extensionless") or 0)

        if self.cfg.dry_run:
            # 演练：一个文件都不动，只把「会做什么」算出来
            d = self.diff(remote_files, local)
            res.unchanged = len(remote_files) - len(d.to_add) - len(d.to_update)
            # ⚠️ 演练时只**数**空目录、不删（`dry=True`）
            res.dirs_pruned = len(self.prune_empty_dirs(remote_dirs, dry=True))
            res.diff = {
                "add": [f.local_rel for f in d.to_add[:500]],
                "update": [f.local_rel for f in d.to_update[:500]],
                "delete": d.to_delete[:500],
                "renamed": [{"local": r, "remote": f.rel} for r, f in d.renamed[:500]],
                "more": {"add": max(0, len(d.to_add) - 500),
                         "update": max(0, len(d.to_update) - 500),
                         "delete": max(0, len(d.to_delete) - 500)},
            }
            res.diff["counts"] = d.counts()
            # ⭐ 演练时也把「是不是改名/移动」判出来 —— 他要**动手之前**就知道
            #    「这一大批跟那一大批其实是同一批文件换了个名」。
            #    ⚠️ 只看 `to_add`（新路径），不算 `to_update`（只是内容变）。
            res.looks_like_move = bool(
                d.to_delete and len(d.to_add) > 0 and len(d.to_add) >= len(d.to_delete) * 0.5)
            res.finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            res.elapsed = _human(time.time() - t0)
            return res

        # ---- 真干 ----
        # ① 目录先建齐 —— ⚠️ 传进来的 `remote_dirs` 已经过滤过，
        #    **只含「子树里有 strm」的目录**，不会再建出空目录（见 keep_only_file_dirs）
        res.dirs_made = self.mkdirs(remote_dirs)

        # ② 写 strm
        d = self.diff(remote_files, local)
        res.unchanged = len(remote_files) - len(d.to_add) - len(d.to_update)
        self.write(list(d.to_add) + list(d.to_update), res)
        if d.renamed:
            res.renamed = len(d.renamed)
            self.log("info", f"有 {len(d.renamed)} 条本地 strm **被改过名**"
                             f"（内容与网盘一致）⇒ 原样保留、不重生成。"
                             f" 例如：{d.renamed[0][0]}")

        # ②′ 收掉本地空目录 —— 「没有具体文件的目录直接抛弃」的另一半：
        #     光「不再新建」不够，上一版按「空目录也建」跑过的那批还躺在那儿。
        #     ⛔ 只删**空目录**（里面有你自己放的任何文件就绝不碰），
        #        而且 `output_dir` 自己永不删。详见 `prune_empty_dirs`。
        pruned = self.prune_empty_dirs(remote_dirs)
        if pruned:
            res.dirs_pruned = len(pruned)
            self.log("info", f"收掉 {len(pruned)} 个空目录（只删空的；"
                             f"里面有东西的一律不碰）例：{pruned[0]}")

        # ③ 删除（带二次确认）
        if d.to_delete:
            # ⚠️ 传的是 **`to_add`**（新路径）而**不是** `to_add + to_update`。
            #    判「改名/移动」要看的是「有没有一批**新路径**冒出来」；
            #    `to_update` 只是内容变了（如同一批文件 URL 前缀换了），
            #    把它算进来会让「只少了一个、其余内容全变」这种误判成改名。
            res = self._handle_delete(d.to_delete, local, res, force=force_delete,
                                      added=len(d.to_add))
        elif not force_delete:
            # 这轮没发现删除 ⇒ 挂起清单作废（说明刚才是抖动，现在恢复了）
            self.clear_pending()

        res.finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        res.elapsed = _human(time.time() - t0)
        res.diff = {"counts": d.counts()}
        return res

    def _handle_delete(self, gone: list[str], local: dict[str, float],
                       res: SyncResult, *, force: bool, added: int = 0) -> SyncResult:
        """删除判定 —— 本工具最需要小心的那一段。

        ## 🔴 区分「改名/移动」与「异常」—— 这是本方法的关键

        光看「消失比例」会把**整目录改名**误判成异常（实测口径）：

            867 个 strm，把 `115电影` 改个名 ⇒ 消失 283 条 = 32.6% > 30% 阈值
            ⇒ 闸门触发 ⇒ 旧的删不掉 ⇒ **本地新旧两套并存**

        但两者有个**决定性区别**：

            cookie 失效 / 接口抽风 / 分页失败  ⇒ 网盘**返回空或不全**
                                              ⇒ **只消失、没有新增**
            改名 / 移动                        ⇒ 文件还在、路径变了
                                              ⇒ **消失的同时有等量新增**

        ⇒ 判据加上 `added`：**新路径能顶上消失的大部分时，就不是异常**。

        ⚠️ `added` 必须只数 **`to_add`（新路径）**，**不能**把 `to_update`（内容变了）算进来。
           反例（本项目的测试抓到过）：本地 10 个 strm 内容都是占位文本、网盘 9 个 ——
           于是 9 个进 `to_update`、1 个进 `to_delete`；若把 update 也算「新增」，
           就会得出「消失 1、新增 9」⇒ 误判成改名。
        """
        total = max(1, len(local))

        # ① 异常比例闸门 + 改名判据
        ratio = len(gone) / total
        # 「新增能顶上」= 新增数 ≥ 消失数的一半。用一半而不是相等，是因为
        # 改名常常夹着少量真删除（如顺手把广告件删了），相等太严会漏判。
        looks_like_move = added > 0 and added >= len(gone) * 0.5
        if ratio > self.cfg.delete_ratio_guard and not looks_like_move:
            res.guard_tripped = True
            res.guard_reason = (
                f"本轮消失 {len(gone)} 条 / 本地共 {len(local)} 条"
                f"（{ratio:.1%}）超过阈值 {self.cfg.delete_ratio_guard:.0%}，"
                f"而且**几乎没有新增**（只有 {added} 条）"
                f" ⇒ 判定异常，本轮不删。"
                f" 常见原因：cookie 失效、网盘权限变化、接口返回不全。")
            self.log("error", res.guard_reason)
            # ⚠️ 清单**照样存下来** —— 万一是「真删了一大批」，他能在网页上
            #    看到清单并手动确认；不存的话他连发生了什么都看不到。
            self.save_pending(Pending(paths=gone, first_seen=time.time(), seen_times=1))
            res.pending_delete = len(gone)
            return res

        if looks_like_move:
            res.looks_like_move = True
            self.log("info", f"消失 {len(gone)} 条、同时新增 {added} 条"
                             f" ⇒ 判定为**改名/移动**（不是异常），按正常流程处理")

        if force:
            n = self.remove(gone)
            res.deleted = n
            self.clear_pending()
            self.log("info", f"已确认删除 {n} 个本地 strm")
            return res

        # ② 延迟二次确认
        p = self.load_pending()
        if not p.first_seen:
            # 第一次发现 ⇒ 挂起，等 N 分钟
            self.save_pending(Pending(paths=gone, first_seen=time.time(), seen_times=1))
            res.pending_delete = len(gone)
            self.log("info", f"发现 {len(gone)} 条本地多余，**先挂起**，"
                             f"等 {self.cfg.delete_defer_minutes:.0f} 分钟后二次扫描确认")
            return res

        age = p.age_minutes()
        if age < self.cfg.delete_defer_minutes:
            # 还没到时间 ⇒ 继续等，并更新清单（可能又多了几条）
            self.save_pending(Pending(paths=gone, first_seen=p.first_seen,
                                      seen_times=p.seen_times + 1))
            res.pending_delete = len(gone)
            self.log("info", f"挂起中（已等 {age:.1f}/{self.cfg.delete_defer_minutes:.0f} 分钟），"
                             f"本轮仍不删")
            return res

        # ③ 到点了 ⇒ 再核对一次「它们真的不在网盘上了吗」，然后删
        still_gone = self._recheck(gone)
        n = self.remove(still_gone)
        res.deleted = n
        self.clear_pending()
        self.log("info", f"二次确认通过（等了 {age:.0f} 分钟），删除 {n} 个本地 strm")
        return res

    def _recheck(self, rels: list[str]) -> list[str]:
        """对候选删除再核一次 —— 能查网盘就查，查不了就用传入的清单。

        ⚠️ 这里刻意**保守**：`V115` 不可用（没 cookie / 演练）时，
           不擅自认为「删得对」，而是照用清单（清单本身已经是二次扫描的产物）。
        """
        if self.v is None:
            return rels
        out: list[str] = []
        for rel in rels:
            # strm 相对路径 → 网盘上的目录（去掉 .strm 后缀）
            remote_rel = rel[:-5] if rel.endswith(".strm") else rel
            parent = remote_rel.rsplit("/", 1)[0] if "/" in remote_rel else ""
            name = remote_rel.rsplit("/", 1)[-1]
            try:
                cid = self.v.dir_id(parent) if parent else self.v.root_id()
                found = any(n.name == name for n in self.v.list_dir(cid))
            except Exception as exc:
                self.log("warning", f"复核 {rel} 失败（{exc}）⇒ 按「还在」处理，不删")
                found = True
            if not found:
                out.append(rel)
        return out


def _human(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s} 秒"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m} 分 {sec} 秒"
    h, m = divmod(m, 60)
    return f"{h} 小时 {m} 分"
