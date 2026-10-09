"""目录树导出件解析 —— **零请求的发现方式**。

115 网页版能导出整个目录树，把那份 `.txt` 丢进 `DATA_DIR/tree/` 即可。
本工具用它一次算出全盘结构（**零请求、零风控**），是最省的首次全量方式。

支持的格式（自动识别，不要求改导出方式 —— 同 `115organize`，已实测）：

    1. **`| |-` 竖线缩进树**（115 实际导出用的格式，实测 UTF-16LE）
           |——根目录
           | |-云下载
           | | |-DLDLS-532
    2. **`├──` / `└──` 制表符树**（Linux `tree` 风格）
    3. **纯空格 / Tab 缩进**（`ls -R` 之类）
    4. **每行一个完整路径**（`云下载/x/xxx.mp4`）

编码自动识别：BOM（UTF-16LE/BE、UTF-8）→ UTF-8 → GBK → 兜底。
⚠️ 实测 115 导出的是 **UTF-16LE + BOM**，直接当 UTF-8 读会「binary file」读不出来。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["TreeEntry", "Tree", "parse_text", "parse_file", "pick_tree_file", "TreeParseError"]


class TreeParseError(Exception):
    pass


_BAR_RE = re.compile(r"^(?P<indent>(?:\| )*)\|-\s?(?P<name>.*)$")
_BAR_ROOT_RE = re.compile(r"^\|[—\-–]{1,3}\s?(?P<name>.*)$")
_TREE_RE = re.compile(r"^(?P<indent>(?:[│|]?[ \t]{2,}|[│|])*)[├└]─{1,2}\s?(?P<name>.*)$")
_INDENT_RE = re.compile(r"^(?P<indent>[ \t]+)(?P<name>\S.*)$")
_PATHLIKE_RE = re.compile(r"^(?P<path>[^/\\]+(?:[/\\][^/\\]+)+)$")
_SIZE_TAIL_RE = re.compile(r"[ \t]+\(?\d[\d.,]*\s*(?:[KMGTP]i?B)\)?$", re.I)

_FULLWIDTH_SPACE = "\u3000"


def _detect_encoding(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
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
       曾经写成 `len(indent)//2 + 1`，第一层成了 2 —— 于是 `top_level` 恒为空。
    """
    line = line.rstrip()
    m = _BAR_RE.match(line)
    if m:
        return len(m.group("indent")) // 2, _clean(m.group("name"))
    m = _BAR_ROOT_RE.match(line)
    if m:
        return 0, _clean(m.group("name"))
    m = _TREE_RE.match(line)
    if m:
        # ⚠️ 这个格式缩进是**树符号**（每级 4 字符），最外层无缩进 ⇒ 层数 = 宽度//4 + 1
        return len(m.group("indent")) // 4 + 1, _clean(m.group("name"))
    m = _INDENT_RE.match(line)
    if m:
        return len(m.group("indent").replace("\t", "    ")) // 2, _clean(m.group("name"))
    return 0, _clean(line)


@dataclass
class TreeEntry:
    path: str
    name: str
    depth: int
    is_dir: bool
    parent: str = ""


@dataclass
class Tree:
    entries: dict[str, TreeEntry] = field(default_factory=dict)
    root_path: str = ""
    _children: dict[str, list[str]] = field(default_factory=dict)
    _files: dict[str, list[str]] = field(default_factory=dict)

    def children_dirs(self, path: str) -> list[str]:
        return sorted(d for d in self._children.get(path, []) if self.entries[d].is_dir)

    def files_of(self, path: str) -> list[str]:
        return sorted(self._files.get(path, []))

    # ------------------------------------------------------------- 相对路径口径
    def resolve(self, rel: str) -> str:
        """把「相对树根的路径」解析成树内的完整路径。

        ⚠️ **这是本工具路径口径的关键一环**：用户在配置里填的 `remote_root`
           是**相对树根**的（`影音`），而树内部用的键是完整的（`根目录/影音`）。
           两个口径混用就会拼出重复层级的路径。
        """
        rel = (rel or "").strip("/")
        if not rel:
            return self.root_path
        if rel in self.entries:
            return rel
        cand = f"{self.root_path}/{rel}"
        if cand in self.entries:
            return cand
        raise TreeParseError(f"树里找不到目录：{rel}（也试过 {cand}）")

    def has(self, rel: str) -> bool:
        try:
            self.resolve(rel)
            return True
        except TreeParseError:
            return False

    def list_level(self, rel: str = "") -> list[dict]:
        """列**某一层的子目录**—— 给网页的目录选择器用。

        ⚠️ `rel` 既可以是**相对树根**的（`影音`），也可以是**树内完整**的
           （`根目录/影音`）—— `resolve()` 两个都认。这样调用方不用纠结口径。

        返回 `[{name, path, is_dir, has_children, files}]`：
          · `path` **相对树根**（前端勾选后直接交给白名单）
          · `files` 该目录下**直接**有多少个文件（让人判断「这目录值不值得勾」）
        """
        node = self.resolve(rel)
        out: list[dict] = []
        for d in self.children_dirs(node):
            e = self.entries[d]
            out.append({
                "name": e.name,
                "path": d[len(self.root_path):].lstrip("/") if d != self.root_path else "",
                "is_dir": True,
                "has_children": bool(self.children_dirs(d)),
                "subdirs": len(self.children_dirs(d)),
                "files": len(self.files_of(d)),
            })
        return out

    def subtree_stats(self, rel: str) -> dict:
        """某个目录（相对树根）**整棵子树**的规模 —— 让用户勾之前知道代价。"""
        node = self.resolve(rel)
        dirs = files = 0
        stack = [node]
        seen: set[str] = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for d in self.children_dirs(cur):
                dirs += 1
                stack.append(d)
            files += len(self.files_of(cur))
        return {"dirs": dirs, "files": files}

    def rebase(self, new_root: str) -> "Tree":
        """把整棵树的根换到 `new_root`（115organize 同款，供「只同步某个子目录」用）。"""
        if not new_root or new_root == self.root_path:
            return self
        if new_root not in self.entries:
            raise TreeParseError(f"树里没有这个目录：{new_root}")
        t = Tree(root_path=new_root)
        t.entries = self.entries
        t._children, t._files = self._children, self._files
        return t

    def extensionless(self) -> list[str]:
        """**没有扩展名的叶子条目** —— 可疑条目（可能是截断，也可能真是文件）。

        🔴 判据与依据（2026-10-08 红领巾实测后定，当晚又用两棵真树校准过一次）：

            「之前目录树有空的是因为**层级选得太少**，很多子目录没生成」

        115 侧两条已确认的事实：
          1. 导出件里，**只有「当过别人的父节点」才会被判成目录**（见 `_build`）
             ⇒ 「没有子项」的条目在这棵树里就是**叶子**
          2. p115client 的 caution 明写：**【导出目录树】空目录不会被导出**
             ⇒ 真正的空目录**根本不会出现在树里**

        ⇒ 「叶子 + 没扩展名」有两种可能，**单看一棵树区分不了**：

           | 情形 | 长什么样 | 实测样本 |
           |---|---|---|
           | **A. 子树被截断** | 树是**手工导的**、`layer_limit` 给少了 ⇒ 目录的子项一层都没导出来 | 他手工那份树：**386 个**，形态全是 `电影/007（系列）/007：大战皇家赌场 (2006)` |
           | **B. 真实的无扩展名文件** | 树是**工具自导的**（固定 25 层）⇒ 层级有保证，那它就真是文件 | 同一盘：**只有 3 个**，形态是发布者塞在片目录里的推广文件（`6v电影APP 有他不迷路`） |

        ⇒ **只能拿数量旁证**：几十上百 ⇒ 基本可断定是 A；个位数 ⇒ 基本是 B。
          但**两种都一律不建**（安全侧），所以这个区分只影响**告警文案**，不影响行为。

        ⚠️ 已知假阳性：目录名本身带点号（`功夫女足(2026).4K`）时 `"." not in name` 会漏掉它 ——
           只能靠「它有没有子项」来翻正成目录（见 `_build` 的二次判定）。

        返回路径列表（排序，便于打印前几条）。
        """
        return sorted(e.path for e in self.entries.values()
                      if not e.is_dir and "." not in e.name)

    def stats(self) -> dict:
        dirs = sum(1 for e in self.entries.values() if e.is_dir)
        files = sum(1 for e in self.entries.values() if not e.is_dir)
        top = len(self._children.get(self.root_path, []))
        ext_less = self.extensionless()
        return {"dirs": dirs, "files": files, "top_level": top,
                "extensionless": len(ext_less), "extensionless_sample": ext_less[:5]}


def _build(rows: list[tuple[int, str]]) -> Tree:
    """把 `(depth, name)` 序列还原成树。

    ⚠️ 用**栈**按 depth 归位，而不是「名字里有 / 就分层」—— 目录名本身可以带
       斜杠之外的点号（`S01E01.mkv`），按名字猜层级必然错。
    """
    tree = Tree()
    # 栈里存 (depth, path)；path 为空表示「还没定根」
    stack: list[tuple[int, str]] = []

    for depth, name in rows:
        if not name:
            continue
        # 弹出不比当前浅的
        while stack and stack[-1][0] >= depth:
            stack.pop()
        parent = stack[-1][1] if stack else ""

        if not parent and depth == 0:
            # 第一条 depth=0 的行 = 根
            path = name
            tree.root_path = tree.root_path or name
        else:
            path = f"{parent}/{name}" if parent else name

        # 判目录：有子项才是目录 —— 先都当文件，后面有子项时翻正
        is_dir = name.endswith("/")
        name_clean = name.rstrip("/") or name

        e = TreeEntry(path=path.rstrip("/"), name=name_clean, depth=depth,
                      is_dir=is_dir, parent=parent.rstrip("/"))
        tree.entries[e.path] = e
        stack.append((depth, e.path))

    # 二次判定：凡「当过别人的 parent」的，一定是目录
    for e in tree.entries.values():
        if e.parent and e.parent in tree.entries:
            tree.entries[e.parent].is_dir = True

    # 建索引
    for e in tree.entries.values():
        if e.parent:
            tree._children.setdefault(e.parent, []).append(e.path)
        if not e.is_dir:
            tree._files.setdefault(e.parent, []).append(e.name)

    if not tree.root_path and tree.entries:
        # 没有显式根 ⇒ 取 depth 最小的那个
        tree.root_path = min(tree.entries.values(), key=lambda x: x.depth).path
    return tree


def parse_text(text: str) -> Tree:
    rows: list[tuple[int, str]] = []
    for line in text.splitlines():
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        got = _parse_line(line)
        if got:
            rows.append(got)
    if not rows:
        raise TreeParseError("目录树是空的 —— 检查导出文件有没有内容")
    return _build(rows)


def parse_file(path: str | Path) -> Tree:
    p = Path(path)
    raw = p.read_bytes()
    enc = _detect_encoding(raw)
    return parse_text(raw.decode(enc, errors="replace"))


def pick_tree_file(tree_dir: str | Path, name: str = "") -> Path:
    """挑一份树文件：指定了就用它，否则取目录里**最新**的。"""
    d = Path(tree_dir)
    if name:
        f = d / name
        if not f.exists():
            raise TreeParseError(f"指定的树文件不存在：{f}")
        return f
    if not d.exists():
        raise TreeParseError(f"树目录不存在：{d}（把导出的 .txt 丢进去即可）")
    cands = [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in (".txt", ".tree", ".list")]
    if not cands:
        raise TreeParseError(f"树目录里没有 .txt 文件：{d}")
    return max(cands, key=lambda f: f.stat().st_mtime)
