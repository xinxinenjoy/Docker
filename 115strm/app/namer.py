"""strm 内容与路径映射 —— 本工具的**核心口径**，所有规则集中在这里。

## 实测依据（2026-10-08，对着 alist 真机跑的，不是猜的）

    测试 URL: https://alist.example.com:5244/d/媒体/影音/电影/功夫女足（2026）/功夫女足.xxx.mkv
    GET 结果: 302 → https://cdnfhnfile.115cdn.net/…（115 CDN 直链）
    ✅ 免认证  ✅ 内网外网同 URL  ✅ 不占 NAS 带宽  ✅ 编码/不编码都通

    对照: /dav/ 端点 → **401**（WebDAV 要认证）⇒ ⛔ 绝不能写进 strm

⇒ **strm 内容 = `<prefix>/<相对路径>`，prefix 用 alist 的 `/d` 端点。**

⚠️ 上面那个域名是**占位示例**，不是任何人的真实地址 ——
   真实前缀由用户在网页上填、存在 `data/config.json`（见 `config.strm_prefix`）。

## 🔴 两条最容易错的口径（都实测踩过，务必对齐）

### ① 前缀必须**覆盖到 alist 挂载点的完整路径**

alist 是**按挂载路径**路由的。115 挂在 alist 的 **`/媒体/影音`**，
所以前缀必须是 `https://你的域名:5244/d/媒体/影音` —— **不能只写到 `/d`**。

⛔ 出错时的症状极具欺骗性：alist 回

    {"code":500,"message":"storage not found; rawPath: /影音/…"}

**而 HTTP 状态码是 200** —— 看着不像错。所以本工具有 `health.check_prefix()`
主动自检（`doctor --check` / 网页「自检」按钮）。

### ② 目录树的**根** = 同步根 = **alist 挂载点本身**

前缀已经含了挂载路径，所以树里的相对路径**不能再带挂载路径那一层**。

    前缀  = https://你的域名:5244/d/媒体/影音
    相对  = 电影/功夫女足（2026）/xxx.mkv          ← 不含挂点最后一层（这里是 `影音`）
    结果  = …/d/媒体/影音/电影/功夫女足（2026）/xxx.mkv   ✅

⛔ 若相对路径又多带了那一层（写成 `影音/电影/…`），拼出来会是
   `…/d/媒体/影音/影音/电影/…` —— 重复一层，alist 找不到。

## 两条路径口径

    remote_rel   相对「同步根」的路径，如 `电影/功夫女足（2026）/xxx.mkv`
    alist_rel    alist 里挂载点之后的路径 —— 本工具**让两者相等**，
                 这样前缀与相对路径天然对齐、拼接零歧义。

想少配一截也行（`alist_base` 参数），两条路都支持。
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit, urlunsplit

from .config import Config

__all__ = ["StrmBuilder", "local_path_of", "safe_name", "is_video", "WIN_BAD_CHARS"]

# 本地文件系统不认的字符（群晖是 ext4/btrfs，但 strm 可能被同步到别处 ⇒ 一并挡掉）
WIN_BAD_CHARS = '<>:"\\|?*'
# 控制字符
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
# 目录名末尾的 `.` / 空格在 Windows 上会被吃掉
_TRAIL_RE = re.compile(r"[. ]+$")


def safe_name(name: str) -> str:
    """把网盘上的名字转成**本地文件系统能接受**的名字。

    ⚠️ 只做「不合法字符替换」，**不做任何美化改名** —— 本工具的定位是
       「如实镜像网盘结构」，改名会让「对比看缺失」这件事失真。

    替换规则：控制字符去掉，非法字符换全角同形（保持可读、且不再被当分隔符）。
    """
    out = _CTRL_RE.sub("", name)
    table = {"<": "＜", ">": "＞", ":": "：", '"': "＂", "\\": "＼", "|": "｜", "?": "？", "*": "＊"}
    for bad, good in table.items():
        out = out.replace(bad, good)
    out = _TRAIL_RE.sub("", out)
    return out or "_"


def is_video(name: str, exts: frozenset[str]) -> bool:
    """名字是不是要生成 strm 的类型。"""
    if "." not in name:
        return False
    return name.rsplit(".", 1)[-1].lower() in exts


def url_of_path(path: str, encode: bool = True) -> str:
    """相对路径 → URL 路径段。

    ⚠️ **逐段编码、保留 `/`** —— 不能整体 `quote()`（那样 `/` 也会被编成 `%2F`，
       变成一个巨大的单段路径，alist 解析不到）。
       实测整体比过：编码与不编码 alist 都接受，编码后更稳（代理不会二次误解）。
    """
    parts = [p for p in path.split("/") if p]
    if encode:
        return "/".join(quote(p, safe="") for p in parts)
    return "/".join(parts)


def local_path_of(root: str, rel: str) -> str:
    """拼本地路径（POSIX 语义，群晖是 Linux）。"""
    rel = rel.strip("/")
    return posixpath.join(root.rstrip("/"), rel) if rel else root.rstrip("/")


@dataclass
class StrmBuilder:
    """strm 内容生成器 —— 把 prefix 与相对路径拼成最终 URL。"""

    prefix: str                 # 如 `https://你的域名:5244/d/媒体/影音`
    alist_base: str = ""        # 如 `媒体/影音`；留空 = 相对路径直接接在 prefix 后
    encode: bool = True

    @classmethod
    def from_cfg(cls, cfg: Config) -> "StrmBuilder":
        return cls(prefix=cfg.strm_prefix, alist_base="", encode=cfg.url_encode)

    def url(self, rel: str) -> str:
        """相对路径 → strm 里写的完整 URL。

        🔴 **整个路径都编码，包括 prefix 里那部分中文**。

        为什么必须连前缀一起编：前缀通常写成 `…/d/媒体/影音` —— 里面**本来就带中文**。
        只编相对路径的话，产出的是一串**混合形态**（前缀原始中文 + 后面 %XX），
        例如 `…/d/媒体/影音/%E7%94%B5%E5%BD%B1/…`。

        curl 和浏览器能忍这种（它们会自己补编码），但**严格的 HTTP 客户端编不了** ——
        Python 的 `urllib` 直接抛 `'ascii' codec can't encode characters`。
        播放器用的 HTTP 库五花八门，赌不起 ⇒ 干脆全编，做成**纯 ASCII** 的 URL。
        （实测：全编码的形态 alist 一样 302 到 115 CDN，见 `tools/smoke.sh`。）

        ⛔ **前缀为空时直接报错** —— 不许拼出一个相对路径糊过去。
           空前缀拼出来的是 `/电影/xxx.mkv` 这种「看着像 URL、其实播不了」的东西，
           写进 strm 就是**静默产出坏数据**。宁可当场炸。
           （正常流程里 `pipeline.run_once` 会在开跑前就拦住，这里是最后一道兜底。）
        """
        if not (self.prefix or "").strip():
            raise ValueError(
                "strm 前缀是空的 —— 先去网页「设置 › 播放地址」填上你的 alist 地址")
        rel = rel.strip("/")
        base = self.alist_base.strip("/")
        full = f"{base}/{rel}" if base else rel

        if "://" in self.prefix:
            sp = urlsplit(self.prefix)
            prefix_path = sp.path.rstrip("/")
            path = f"{prefix_path}/{full}" if full else prefix_path
            if self.encode:
                path = "/".join(quote(p, safe="") for p in path.split("/"))
            return urlunsplit((sp.scheme, sp.netloc, path, sp.query, sp.fragment))

        # 兜底：前缀没写 scheme（手抖输成 `alist.example.com:5244/d/…`）⇒ 当纯路径处理，
        # 至少不产出畸形 URL；真有问题会在自检里被看出来。
        merged = f"{self.prefix.rstrip('/')}/{full}" if full else self.prefix.rstrip("/")
        return url_of_path(merged, self.encode) if self.encode else merged


@dataclass
class StrmFile:
    """一个待落盘的 strm。"""
    rel: str                    # 相对同步根的路径（含 .strm 后缀前的主体）
    url: str                    # 写入 strm 的内容
    fid: str = ""               # 115 文件 id（删除判定要用）
    size: int = 0

    @property
    def local_rel(self) -> str:
        return self.rel + ".strm"


@dataclass
class DiffResult:
    """一次对比的结果 —— 给人看的就是它。"""
    to_add: list[StrmFile] = field(default_factory=list)
    to_update: list[StrmFile] = field(default_factory=list)
    to_delete: list[str] = field(default_factory=list)      # 本地相对路径
    # ⭐ 「同一个网盘文件，本地 strm 被改过名」—— 内容一致，**不动它**。
    #    元素 = (本地实际路径, 对应的网盘条目)。详见 `Syncer.diff()` 的说明。
    renamed: list[tuple[str, StrmFile]] = field(default_factory=list)
    missing_dirs: list[str] = field(default_factory=list)   # 网盘有、本地没建的目录
    empty_local: list[str] = field(default_factory=list)    # 本地有、网盘没有的目录
    skipped: int = 0                                        # 非媒体扩展名，跳过不生成

    def counts(self) -> dict:
        return {
            "add": len(self.to_add),
            "update": len(self.to_update),
            "delete": len(self.to_delete),
            "renamed": len(self.renamed),
            "missing_dirs": len(self.missing_dirs),
            "empty_local": len(self.empty_local),
            "skipped": self.skipped,
            "total_write": len(self.to_add) + len(self.to_update),
        }
