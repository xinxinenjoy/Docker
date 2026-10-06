"""目录树导出文件解析 —— **本工具的「免扫描」入口**。

背景（红领巾 2026-10-06）：他已经拿到 115 盘的准确结构，导成一份文本目录树。
所以本工具**不做全盘递归扫描**（那是风控最大来源），只：

    ① 读这份树 ⇒ 离线算出「该动什么」（零请求、零风险）
    ② 只对**真要动的目录**发请求拿 id 再执行

⇒ 发现层请求数从「目录数」（本盘 7,697）降到 0。

支持的格式（自动识别，不要求他改导出方式）：

    1. **`| |-` 竖线缩进树**（他这份导出用的格式，实测）：
           |——根目录
           | |-云下载
           | | |-DLDSS-532
           | | | |-4k688.com@DLDSS-532.mp4
    2. **`├──` / `└──` 制表符树**（Linux `tree` 命令风格）
    3. **纯空格 / Tab 缩进**（`ls -R` 之类）
    4. **每行一个完整路径**（`云下载/DLDSS-532/xxx.mp4`）

编码自动识别：BOM（UTF-16LE/BE、UTF-8）→ UTF-8 → GBK → 拉丁兜底。
（⚠️ 实测他导出的就是 **UTF-16LE + BOM**，直接当 UTF-8 读会「binary file」读不出来。）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["TreeEntry", "Tree", "parse_text", "parse_file", "TreeParseError"]


class TreeParseError(Exception):
    pass


# `| |-名字` / `| | | |-名字`；根行 `|——根目录`
_BAR_RE = re.compile(r"^(?P<indent>(?:\| )*)\|-\s?(?P<name>.*)$")
_BAR_ROOT_RE = re.compile(r"^\|[—\-–]{1,3}\s?(?P<name>.*)$")
# `│   ├── 名字` / `|   └── 名字`
_TREE_RE = re.compile(r"^(?P<indent>(?:[│|]?[ \t]{2,}|[│|])*)[├└]─{1,2}\s?(?P<name>.*)$")
# 纯缩进
_INDENT_RE = re.compile(r"^(?P<indent>[ \t]+)(?P<name>\S.*)$")
# 完整路径
_PATHLIKE_RE = re.compile(r"^(?P<path>[^/\\]+(?:[/\\][^/\\]+)+)$")
# 行尾的体积/大小标注，如 ` 1.2GB`
_SIZE_TAIL_RE = re.compile(r"[ \t]+\(?\d[\d.,]*\s*(?:[KMGTP]i?B)\)?$", re.I)

_FULLWIDTH_SPACE = "\u3000"
_FILE_EXT_RE = re.compile(r"\.([A-Za-z0-9]{1,6})$")


def _detect_encoding(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe"):
        return "utf-16"
    if raw.startswith(b"\xfe\xff"):
        return "utf-16"
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    for enc in ("utf-8", "gbk", "big5", "latin-1"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8"


def _clean(name: str) -> str:
    name = _SIZE_TAIL_RE.sub("", name)
    name = name.replace(_FULLWIDTH_SPACE, " ").strip()
    return name.strip()


def _parse_line(line: str) -> tuple[int, str] | None:
    """一行 → `(depth, name)`；认不出来返回 None。

    ⚠️ depth 口径：**根 = 0，它的直接子项 = 1**。
       曾经写成 `len(indent) // 2 + 1`，第一层成了 2 —— 于是 `top_level`（depth==1）恒为空，
       `stats()` 报 `top_level=0`，看起来像「什么都没解析出来」。
    """
    line = line.rstrip()
    m = _BAR_RE.match(line)                      # `| |-名字`
    if m:
        return len(m.group("indent")) // 2, _clean(m.group("name"))
    m = _BAR_ROOT_RE.match(line)                 # `|——根目录`
    if m:
        return 0, _clean(m.group("name"))
    m = _TREE_RE.match(line)                     # `│   ├── 名字`
    if m:
        # ⚠️ 这个格式的缩进是**树符号**（`tree` 命令风格，每级 4 个字符），最外层没有缩进 ⇒
        #    层数 = 缩进宽度 // 4 + 1。用 `//2` 会把最外层算成 0（当成根）：
        #    `├── 云下载` 无缩进 ⇒ 0//2=0 ⇒ 被当成根，整棵树就断了。
        return len(m.group("indent")) // 4 + 1, _clean(m.group("name"))
    m = _INDENT_RE.match(line)                   # 纯空格/Tab 缩进
    if m:
        return len(m.group("indent").replace("\t", "    ")) // 2, _clean(m.group("name"))
    return 0, _clean(line)                       # 顶格行


@dataclass
class TreeEntry:
    name: str
    path: str                        # 相对导出根的路径，`/` 分隔
    depth: int                       # 0 = 导出根
    is_dir: bool
    parent: str = ""


@dataclass
class Tree:
    root_name: str = ""
    entries: dict[str, TreeEntry] = field(default_factory=dict)   # path → entry
    files: dict[str, list[str]] = field(default_factory=dict)     # 目录 path → 直接子文件名
    dirs: list[str] = field(default_factory=list)                 # 全部目录 path（按出现顺序）
    source: str = ""
    _rebased_from: list[str] = field(default_factory=list, repr=False)   # 被裁掉的前缀（rebase 用）
    _children: dict[str, list[str]] | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ 查询
    def is_dir(self, path: str) -> bool:
        return path in self.entries and self.entries[path].is_dir

    def files_of(self, path: str) -> list[str]:
        return self.files.get(path, [])

    def children_dirs(self, path: str) -> list[str]:
        """直接子目录（只返回一层）。走缓存索引 —— 计划生成里会调很多次。"""
        if self._children is None:
            idx: dict[str, list[str]] = {}
            for p in self.dirs:
                idx.setdefault(self.entries[p].parent, []).append(p)
            self._children = idx
        return list(self._children.get(path, []))

    def dirs_at_depth(self, depth: int) -> list[str]:
        return [p for p in self.dirs if self.entries[p].depth == depth]

    def depth_histogram(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for p in self.dirs:
            d = self.entries[p].depth
            out[d] = out.get(d, 0) + 1
        return dict(sorted(out.items()))

    def rel(self, path: str) -> str:
        """把导出根里的路径裁成「相对整理根」—— 供和 115 上的路径对齐。

        例：导出根是 `根目录`，`path=根目录/云下载/DLDSS-532`，
        整理根是 `云下载` ⇒ 返回 `DLDSS-532`。
        """
        for top in self._rebased_from:
            if path == top:
                return ""
            if path.startswith(top + "/"):
                return path[len(top) + 1:]
        return path

    def rebase(self, top: str) -> "Tree":
        """把树**重挂到 `top` 这个目录上**，之后所有 path 都相对它。

        为什么需要：导出是从**盘根**开始的，而他给本工具的范围是 `云下载` —— 不重挂的话
        每条路径都得手动剥一层，容易漏。
        """
        if top and top == self.root_name:
            self._rebased_from = [self.root_name] + self._rebased_from
            return self
        target = None
        for p, e in self.entries.items():
            if e.name == top and e.is_dir:
                # 优先取最浅的那个（避免同名子目录抢先）
                if target is None or e.depth < self.entries[target].depth:
                    target = p
        if target is None:
            raise TreeParseError(
                f"目录树里找不到整理根「{top}」—— 检查 ROOT_PATH 和导出文件是否对得上"
            )
        pref = target + "/"
        new = Tree(root_name=top, source=self.source)
        new._rebased_from = [target] + self._rebased_from
        new.entries[top] = TreeEntry(top, top, 0, True, "")
        new.dirs.append(top)
        for p, e in self.entries.items():
            if not p.startswith(pref):
                continue
            np = p[len(pref):]
            nd = e.depth - self.entries[target].depth
            new.entries[np] = TreeEntry(
                e.name, np, nd, e.is_dir,
                top if nd <= 1 else np.rsplit("/", 1)[0],
            )
            if e.is_dir:
                new.dirs.append(np)
        for p, fs in self.files.items():
            if p == target:
                new.files[top] = list(fs)
            elif p.startswith(pref):
                new.files[p[len(pref):]] = list(fs)
        new.dirs.sort(key=lambda x: (new.entries[x].depth, x))
        return new

    @property
    def top_level(self) -> list[str]:
        return [p for p in self.dirs if self.entries[p].depth == 1]

    def stats(self) -> dict:
        files = sum(len(v) for v in self.files.values())
        return {
            "root": self.root_name,
            "dirs": len(self.dirs),
            "files": files,
            "top_level": len(self.top_level),
            "source": self.source,
        }


def _split_paths(lines: list[str]) -> Tree | None:
    """格式 4：每行一个完整路径（没有缩进信息时兜底）。"""
    paths = []
    for ln in lines:
        s = _clean(ln)
        if not s:
            continue
        m = _PATHLIKE_RE.match(s)
        if not m:
            return None
        paths.append(m.group("path").replace("\\", "/"))
    if not paths:
        return None
    tree = Tree(root_name=paths[0].split("/")[0])
    tree.entries[tree.root_name] = TreeEntry(tree.root_name, tree.root_name, 0, True, "")
    tree.dirs.append(tree.root_name)
    for p in paths:
        parts = [x for x in p.split("/") if x]
        # 最后一段若已经出现过（作为目录）就当目录，否则当文件 —— 用「有没有子路径」判定
        for i in range(1, len(parts)):
            sub = "/".join(parts[: i + 1])
            parent = "/".join(parts[:i])
            if sub in tree.entries:
                continue
            is_dir = any(q.startswith(sub + "/") for q in paths)
            tree.entries[sub] = TreeEntry(parts[i], sub, i, is_dir, parent)
            if is_dir:
                tree.dirs.append(sub)
            else:
                tree.files.setdefault(parent, []).append(parts[i])
    return tree


def parse_text(text: str, source: str = "") -> Tree:
    """把目录树文本解析成 `Tree`。三种缩进格式任选其一，自动识别。"""
    raw_lines = text.splitlines()
    lines = [ln for ln in raw_lines if _clean(ln)]
    if not lines:
        raise TreeParseError("目录树文件是空的")

    # 先看是不是「完整路径」格式（没有任何树形符号）
    if not any("|-" in ln or "├" in ln or "└" in ln for ln in lines[:200]):
        if all(ln.startswith(" ") or ln.startswith("\t") for ln in lines[1:]):
            pass  # 纯缩进，交给下面
        else:
            maybe = _split_paths(lines)
            if maybe is not None and len(maybe.dirs) > 1:
                maybe.source = source
                return maybe

    tree = Tree(source=source)
    rows = [r for r in (_parse_line(ln) for ln in lines) if r and r[1]]
    if not rows:
        raise TreeParseError("没解析出任何条目 —— 格式不认识，把前 20 行发我看看")

    # (depth, path) 栈，用于挂父节点
    stack: list[tuple[int, str]] = []

    for i, (depth, name) in enumerate(rows):
        if depth == 0:
            tree.root_name = name
            tree.entries[name] = TreeEntry(name, name, 0, True, "")
            tree.dirs.append(name)
            stack = [(0, name)]
            continue

        # ★ 判「目录 or 文件」的唯一可靠判据：**下一行的缩进比它更深**。
        #   ⛔ 绝不能按「名字带扩展名」判 —— 本盘大量目录名本身就是个视频文件名
        #      （`MIDA-753.mp4` 是**目录**，里面装着一个同名的 `MIDA-753.mp4` 文件）。
        #      误判成「文件」会连锁出两个后果：
        #        ① 它不进目录栈 ⇒ 它的子项被挂到**上一层**（实测：同名条目在 `files` 里
        #           出现两次，`云下载` 下凭空多出 3,600+ 个「文件」，真机只有 154 个）
        #        ② 计划里对它生成 `move_file` / `trash` 动作 ⇒ 执行时拿**文件的规则去动目录**。
        is_dir = i + 1 < len(rows) and rows[i + 1][0] > depth

        # 挂到最近的、depth 更小的祖先上
        while stack and stack[-1][0] >= depth:
            stack.pop()
        parent = stack[-1][1] if stack else (tree.root_name or "")
        path = f"{parent}/{name}" if parent else name
        # 同名同层重复（少见）时加后缀，别让 key 冲突把数据吃掉
        if path in tree.entries:
            suffix = 2
            while f"{path}#{suffix}" in tree.entries:
                suffix += 1
            path = f"{path}#{suffix}"

        tree.entries[path] = TreeEntry(name, path, depth, is_dir, parent)
        if is_dir:
            tree.dirs.append(path)
            stack.append((depth, path))
        else:
            tree.files.setdefault(parent, []).append(name)

    _fixup(tree)
    if not tree.dirs:
        raise TreeParseError("没解析出任何目录 —— 格式不认识，把前 20 行发我看看")
    return tree


def _fixup(tree: Tree) -> None:
    """修正「目录 / 文件」误判：任何**有子节点**的条目必定是目录（哪怕名字带扩展名）。

    ⚠️ 必须 O(n)：本盘一份树就有 7 万条，写成「逐条扫全表」是 45 亿次比较 —— 实测会直接卡死。
    """
    parents = {e.parent for e in tree.entries.values() if e.parent}
    for p in list(parents):
        entry = tree.entries.get(p)
        if entry is None or entry.is_dir or entry.depth == 0:
            continue
        # 被判成文件、其实有子节点 ⇒ 升格为目录
        entry.is_dir = True
        parent_files = tree.files.get(entry.parent)
        if parent_files and entry.name in parent_files:
            parent_files.remove(entry.name)
        if p not in tree.dirs:
            tree.dirs.append(p)
    tree.dirs.sort(key=lambda p: (tree.entries[p].depth, p))


def parse_file(path: str | Path) -> Tree:
    """读文件并解析（编码自适应，含 UTF-16 BOM）。"""
    p = Path(path)
    raw = p.read_bytes()
    if not raw.strip():
        raise TreeParseError(f"文件是空的：{p}")
    enc = _detect_encoding(raw)
    text = raw.decode(enc, errors="replace")
    return parse_text(text, source=str(p))


def pick_tree_file(tree_dir: str | Path, explicit: str = "") -> Path:
    """挑要用的目录树文件：显式指定优先，否则取目录里**最新修改**的 `.txt`。"""
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = Path(tree_dir) / explicit
        if not p.exists():
            raise TreeParseError(f"指定的目录树文件不存在：{p}")
        return p
    d = Path(tree_dir)
    if not d.exists():
        raise TreeParseError(
            f"目录树目录不存在：{d}\n"
            f"把你的目录树导出文件放进这个目录（或设 TREE_FILE 指到具体文件）"
        )
    cands = [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in (".txt", ".tree", ".list")]
    if not cands:
        raise TreeParseError(f"{d} 里没有 .txt 目录树文件")
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0]
