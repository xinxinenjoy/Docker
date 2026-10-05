"""链接与暗号的抽取 —— 纯标准库，**不依赖 fastapi / p115client**

单独成模块的原因：这是本工具最容易出 bug、也最该有回归测试的一块，
拆出来后 tests/ 里的脚本可以脱离 Web 框架与 115 SDK 直接跑。
"""
from __future__ import annotations

import base64
import binascii
import re

from cipher import restore_any

__all__ = [
    "parse_links",
    "normalize_hash",
    "fix_magnet",
    "MAX_BATCH",
    "SOURCE_LINK",
]

MAX_BATCH = 100
SOURCE_LINK = "链接"

# 粘贴来源五花八门（网页 / 微信 / 记事本），常夹带零宽字符。它们【不是】\s，
# 会被正则当成链接的一部分 ⇒ 先统一换成空格。
INVISIBLE_RE = re.compile("[\u200b-\u200f\u2060\ufeff\u00ad]")

# ⚠️ 不能用 `magnet:\?[^\s"'<>]+`：那是「吃到空白为止」。多条磁力挨着粘贴时
#    （中间没有换行/空格，或用逗号、顿号、零宽字符相隔）会被吞成一整条，
#    而 BTIH 只取第一个 ⇒ 其余磁力凭空消失（2026-10-05 实测复现）。
#    改为【负向前瞻】：在下一个 `magnet:?` 出现之前停下 —— 与分隔符无关，
#    任何粘法都能正确切开。ed2k 同理（它内部也不会再出现裸 `ed2k://`）。
MAGNET_RE = re.compile(r"magnet:\?(?:(?!magnet:\?)[^\s\"'<>])+", re.I)
ED2K_RE = re.compile(r"ed2k://(?:(?!ed2k://)[^\s\"'<>])+", re.I)
# ⚠️ http 故意【不加】前瞻：URL 的 query 里可能内嵌未编码的 URL
#    （如 ?next=https://…），加了会把一条链接切成两条。保持原样。
HTTP_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
BTIH_RE = re.compile(r"xt=urn:btih:([0-9A-Za-z]{32,40})", re.I)

# 前瞻切分时，两条链接【之间】的分隔符会被算进前一条的尾部 ⇒ 剥掉。
# ⚠️ ed2k 不能剥 `|`：它的合法结尾是 `ed2k://|file|…|/`。
MAGNET_TAIL = ",，、;；|"
ED2K_TAIL = ",，、;；"


def normalize_hash(raw_hash: str) -> str:
    """把磁力链里的 btih 归一化成 40 位 hex。

    32 位是 base32（A-Z2-7）编码，115 侧常识别不了，必须转 40 位 hex。
    """
    h = (raw_hash or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}", h):
        return h.lower()
    if re.fullmatch(r"[A-Za-z2-7]{32}", h):
        try:
            return binascii.hexlify(base64.b32decode(h.upper())).decode()
        except Exception:
            return h.lower()
    return h.lower()


def fix_magnet(magnet: str) -> str:
    """把磁力链中的 32 位 hash 就地替换成 40 位。"""
    m = BTIH_RE.search(magnet)
    if not m:
        return magnet
    old = m.group(1)
    new = normalize_hash(old)
    if new == old:
        return magnet
    return magnet[: m.start(1)] + new + magnet[m.end(1):]


def _add_matches(text: str, add, source: str = SOURCE_LINK) -> None:
    for m in MAGNET_RE.finditer(text):
        raw = m.group(0).rstrip(MAGNET_TAIL)
        btih = BTIH_RE.search(raw)
        if not btih:
            continue
        add("magnet", raw, "btih:" + normalize_hash(btih.group(1)), fix_magnet(raw), source)

    for m in ED2K_RE.finditer(text):
        raw = m.group(0).rstrip(ED2K_TAIL)
        add("ed2k", raw, raw, None, source)

    for m in HTTP_RE.finditer(text):
        raw = m.group(0).rstrip(".,;)\u3002\uff0c")
        if "115.com" in raw:
            continue
        add("http", raw, raw, None, source)


def parse_links(text: str) -> list[dict]:
    """从任意文本中提取链接（含中文暗号还原），去重并标注类型与来源。

    `source` 取值：`链接`（直接就是链接）/ `百家姓` / `核心价值观` / `佛曰`。

    顺序：先**按行**试着把整行当暗号还原，再在原文里抽裸链接 ——
    两轮靠 key 去重，所以「暗号行 + 裸链接行」混着粘也各自算得对。
    """
    if not text:
        return []
    text = INVISIBLE_RE.sub(" ", text)  # 零宽字符归一化，避免粘连
    seen: set[str] = set()
    out: list[dict] = []

    def add(kind: str, raw: str, key: str, fixed: str | None = None, source: str = SOURCE_LINK) -> None:
        k = key.lower()
        if k in seen:
            return
        seen.add(k)
        out.append({
            "type": kind,
            "raw": raw,
            "url": fixed or raw,
            "key": k,
            "changed": bool(fixed and fixed != raw),
            "source": source,
        })

    for line in text.splitlines() or [text]:
        hit = restore_any(line)
        if not hit:
            continue
        restored, source = hit
        _add_matches(restored, add, source)

    _add_matches(text, add)
    return out
