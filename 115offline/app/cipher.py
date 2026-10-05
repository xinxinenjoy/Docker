"""中文暗号还原 —— 百家姓 / 核心价值观 / 与佛论禅（佛曰）

社区分享磁力时常把裸链接换成「暗号」以躲屏蔽。本模块**只做还原（解码）**，
不做编码 —— 本工具的场景是「收到暗号 → 还原成链接 → 推给 115」。

三种暗号各自的来源与算法：

1. **百家姓**（福利吧社区流行）—— 单表替换。链接里每个字符换成对应姓氏：
   字符串 `magnet:?xt=urn:btih:…` → `陈周王苏王苏赵水王水…`。
   表共 76 项：数字 10 + 小写 26 + 大写 26 + 符号 13 + 兼容旧版 `卜`。

2. **核心价值观** —— 12 个词的 base-12 编码（本质是十六进制换皮）。
   每个 hex 位（0~15）映射为一个词的下标；≥10 的位拆成两词：
   `诚信`+`(x-10)` 或 `友善`+`(x-6)`。解出的 hex 再按 UTF-8 还原。

3. **与佛论禅（佛曰）** —— 源自 keyfc.net 的「土豆代码」：
   AES-256-CBC（固定 KEY/IV）+ 密文字节映射到 128 个佛经用字
   （≥128 的字节再加一个「高位标记字」前缀）。明文是 UTF-16LE。

⚠️ **只支持第一代「佛曰」**。第二代「如是我闻」额外用了压缩，算法不同，本模块不涉。

正确性由 tests/test_cipher.py 用官方向量与实测样本锁死。
"""
from __future__ import annotations

import re

__all__ = [
    "restore_any",
    "decode_baijiaxing",
    "decode_core_values",
    "decode_tudou",
    "aes256_cbc_decrypt",
    "CIPHER_NAMES",
]

CIPHER_NAMES = ("百家姓", "核心价值观", "佛曰")

# 认得出的链接前缀 —— 还原结果必须命中其一，否则判为「不是有效暗号」
# （这是防误判的主闸门：中文句子凑巧全由暗号字符组成时，解出来的是乱码而非链接）
_LINK_RE = re.compile(r"(magnet:\?|ed2k://|https?://|thunder://|ftp://)", re.I)

_LABEL_RE = re.compile(r"^(?:百家姓|核心价值观|价值观|暗号)\s*[：:]\s*")
_WS_RE = re.compile(r"\s+")


# =====================================================================
# 一、百家姓
# =====================================================================
# 权威对照表：www.bjxah.com/guide.html「百家姓暗号对照表（完整映射）」
# 已用参考图里的「暗号 ↔ 磁力」交叉验证：前 14 个字符 947F7F0B7B7C00 完全吻合。
_BAIJIAXING_TABLE = (
    "赵钱孙李周吴郑王冯陈"  # 0-9
    "褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻"  # a-z
    "福水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳唐罗"  # A-Z
    "薛伍余米贝"  # . - _ + =
    "姚孟顾尹江钟竺赖"  # / ? # % & * : |
    "卜"  # 旧版竖线，官方明确要求兼容
)
_BAIJIAXING_VALUE = (
    "0123456789"
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    ".-_+="
    "/?#%&*:|"
    "|"
)

BAIJIAXING = dict(zip(_BAIJIAXING_TABLE, _BAIJIAXING_VALUE))
assert len(BAIJIAXING) == 76, "百家姓映射表应为 76 项，当前 %d" % len(BAIJIAXING)


def decode_baijiaxing(text: str) -> str:
    """百家姓暗号 → 原始链接。表外字符直接报错（调用方负责先判断）。"""
    out = []
    for ch in text:
        hit = BAIJIAXING.get(ch)
        if hit is None:
            raise ValueError("不是百家姓暗号：出现表外字符 %r" % ch)
        out.append(hit)
    return "".join(out)


# =====================================================================
# 二、核心价值观
# =====================================================================
CORE_WORDS = (
    "富强", "民主", "文明", "和谐", "自由", "平等",
    "公正", "法治", "爱国", "敬业", "诚信", "友善",
)
_CORE_CHARS = frozenset("".join(CORE_WORDS))


def decode_core_values(text: str) -> str:
    """核心价值观暗号 → 原始文本（hex 两两成字节后按 UTF-8 还原）。"""
    if len(text) % 2:
        raise ValueError("核心价值观暗号长度必须是偶数（每词 2 字）")
    pairs = [text[i:i + 2] for i in range(0, len(text), 2)]
    idx = []
    for word in pairs:
        try:
            idx.append(CORE_WORDS.index(word))
        except ValueError as exc:
            raise ValueError("不是核心价值观暗号：未知词组 %r" % word) from exc

    digits: list[str] = []
    i = 0
    while i < len(idx):
        v = idx[i]
        if v < 10:
            digits.append(str(v))
            i += 1
        else:  # 10 -> 低位+10；11 -> 低位+6
            if i + 1 >= len(idx):
                raise ValueError("核心价值观暗号在标记词后截断")
            low = idx[i + 1]
            if low > 9:
                raise ValueError("核心价值观暗号低位非法：%d" % low)
            digits.append(format(low + (10 if v == 10 else 6), "x"))
            i += 2

    hexstr = "".join(digits)
    if len(hexstr) % 2:
        raise ValueError("核心价值观暗号解出的 hex 位数为奇数")
    return bytes.fromhex(hexstr).decode("utf-8")


# =====================================================================
# 三、与佛论禅（佛曰）—— 含极简 AES-256-CBC（仅解密）
# =====================================================================
# 为什么自己写 AES：本项目的既有约定是「依赖里零编译包」（见 requirements.txt），
# 为了解一个固定的玩具密码引入 cryptography / pycryptodome 不划算。
# 正确性由 tests/test_cipher.py 用 FIPS-197 官方向量锁死。
#
# KEY/IV 与 128 字表来自 keyfc.net「土豆代码」的原版实现（社区公开移植版）。

# ⚠️ IV 与主表第 122 项务必照原版逐字抄：搜索摘要/网页转载常把
#    `Potato@Key@_@=_=` 压成 `Potato@Key@@==`（少 2 字节，AES 直接报错），
#    把 `。` 换成 `｡`（半角，字表对不上）。已按 gist 原文校正。
_TUDOU_KEY = b"XDXDtudou@KeyFansClub^_^Encode!!"
_TUDOU_IV = b"Potato@Key@_@=_="
_TUDOU_TABLE = (
    "滅苦婆娑耶陀跋多漫都殿悉夜爍帝吉利阿無南那怛喝羯勝摩伽謹波者穆僧室藝尼瑟地彌菩提"
    "蘇醯盧呼舍佛參沙伊隸麼遮闍度蒙孕薩夷迦他姪豆特逝朋輸楞栗寫數曳諦羅曰咒即密若般故"
    "不實真訶切一除能等是上明大神知三藐耨得依諸世槃涅竟究想夢倒顛離遠怖恐有礙心所以亦"
    "智道。集盡死老至"
)
_TUDOU_MARK = "冥奢梵呐俱哆怯諳罰侄缽皤"

assert len(_TUDOU_KEY) == 32, "AES-256 密钥必须 32 字节"
assert len(_TUDOU_IV) == 16, "CBC 的 IV 必须 16 字节"
assert len(_TUDOU_TABLE) == 128, "佛曰主字符表应为 128 项，当前 %d" % len(_TUDOU_TABLE)
assert len(_TUDOU_MARK) == 12, "佛曰高位标记应为 12 项，当前 %d" % len(_TUDOU_MARK)
assert len(set(_TUDOU_TABLE)) == 128, "佛曰主字符表有重复项"

_TUDOU_INDEX = {ch: i for i, ch in enumerate(_TUDOU_TABLE)}
_TUDOU_MARK_SET = frozenset(_TUDOU_MARK)


def _rotl8(v: int, n: int) -> int:
    return ((v << n) | (v >> (8 - n))) & 0xFF


def _gen_sbox() -> list[int]:
    """按标准算法生成 S 盒（避免手抄 256 个常量出错）。"""
    p = q = 1
    sbox = [0] * 256
    while True:
        p = p ^ (p << 1) ^ (0x1B if p & 0x80 else 0)
        p &= 0xFF
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        xformed = q ^ _rotl8(q, 1) ^ _rotl8(q, 2) ^ _rotl8(q, 3) ^ _rotl8(q, 4)
        sbox[p] = xformed ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


_SBOX = _gen_sbox()
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a & 0xFF


def _gf_mul(a: int, b: int) -> int:
    r = 0
    for _ in range(8):
        if b & 1:
            r ^= a
        b >>= 1
        a = _xtime(a)
    return r


# 逆列混合用到的四个乘数，预先展开成表
_MUL = {n: [_gf_mul(i, n) for i in range(256)] for n in (9, 11, 13, 14)}


def _expand_key(key: bytes) -> list[list[int]]:
    """AES-256 密钥扩展，返回 15 组、每组 16 字节的轮密钥。"""
    nk, nr = 8, 14
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        elif i % nk == 4:
            t = [_SBOX[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return [sum(w[4 * r:4 * r + 4], []) for r in range(nr + 1)]


def _inv_shift_rows(s: list[int]) -> list[int]:
    out = [0] * 16
    for c in range(4):
        for r in range(4):
            out[4 * c + r] = s[4 * ((c - r) % 4) + r]
    return out


def _inv_mix_columns(s: list[int]) -> list[int]:
    out = [0] * 16
    for c in range(4):
        a0, a1, a2, a3 = s[4 * c:4 * c + 4]
        out[4 * c + 0] = _MUL[14][a0] ^ _MUL[11][a1] ^ _MUL[13][a2] ^ _MUL[9][a3]
        out[4 * c + 1] = _MUL[9][a0] ^ _MUL[14][a1] ^ _MUL[11][a2] ^ _MUL[13][a3]
        out[4 * c + 2] = _MUL[13][a0] ^ _MUL[9][a1] ^ _MUL[14][a2] ^ _MUL[11][a3]
        out[4 * c + 3] = _MUL[11][a0] ^ _MUL[13][a1] ^ _MUL[9][a2] ^ _MUL[14][a3]
    return out


def _decrypt_block(block: bytes, rk: list[list[int]]) -> bytes:
    # ⚠️ 顺序易错：解密是「先加【最后】一组轮密钥」，最后才加第 0 组（加密的反向）。
    s = [block[i] ^ rk[-1][i] for i in range(16)]
    for rnd in range(len(rk) - 2, 0, -1):
        s = _inv_shift_rows(s)
        s = [_INV_SBOX[b] for b in s]
        s = [s[i] ^ rk[rnd][i] for i in range(16)]
        s = _inv_mix_columns(s)
    s = _inv_shift_rows(s)
    s = [_INV_SBOX[b] for b in s]
    return bytes(s[i] ^ rk[0][i] for i in range(16))


def aes256_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-256-CBC 解密（不做去填充）。"""
    if len(key) != 32:
        raise ValueError("AES-256 密钥长度必须 32 字节")
    if len(iv) != 16:
        raise ValueError("CBC 的 IV 长度必须 16 字节")
    if len(data) % 16:
        raise ValueError("密文长度必须是 16 的整数倍")
    rk = _expand_key(key)
    out = bytearray()
    prev = iv
    for off in range(0, len(data), 16):
        blk = data[off:off + 16]
        plain = _decrypt_block(blk, rk)
        out += bytes(a ^ b for a, b in zip(plain, prev))
        prev = blk
    return bytes(out)


def _tudou_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def decode_tudou(text: str) -> str:
    """佛曰密文 → 原文。容忍空白、容忍「佛曰：」/「佛曰:」前缀。"""
    s = _WS_RE.sub("", text)
    if s.startswith("佛曰"):
        s = s[2:]
        s = s.lstrip("：:，,、;；")
    if not s:
        raise ValueError("佛曰密文为空")

    data = bytearray()
    i = 0
    while i < len(s):
        ch = s[i]
        if ch in _TUDOU_MARK_SET:
            i += 1
            if i >= len(s):
                raise ValueError("佛曰密文在高位标记字后截断")
            nxt = _TUDOU_INDEX.get(s[i])
            if nxt is None:
                raise ValueError("佛曰密文含表外字符 %r" % s[i])
            data.append(nxt + 128)
        else:
            v = _TUDOU_INDEX.get(ch)
            if v is None:
                raise ValueError("佛曰密文含表外字符 %r" % ch)
            data.append(v)
        i += 1

    plain = _tudou_unpad(aes256_cbc_decrypt(_TUDOU_KEY, _TUDOU_IV, bytes(data)))
    return plain.decode("utf-16le")


# =====================================================================
# 四、统一入口
# =====================================================================
def restore_any(text: str) -> tuple[str, str] | None:
    """尝试把一行文本当暗号还原。

    返回 `(还原出的文本, 来源名)`；不是暗号、或还原结果不像链接时返回 None。

    识别顺序：佛曰（靠前缀）→ 百家姓 → 核心价值观。
    三者的字符集基本互斥，加上「结果必须是链接」这道闸门，误判概率极低。
    """
    if not text:
        return None
    raw = _LABEL_RE.sub("", text.strip())
    compact = _WS_RE.sub("", raw)
    if not compact:
        return None

    # ---- 佛曰：只认前缀，避免与百家姓混判 ----
    if compact.startswith("佛曰"):
        try:
            out = decode_tudou(compact)
        except Exception:
            return None
        return (out, "佛曰") if _LINK_RE.search(out) else None

    # ---- 百家姓：整行都得是表内字，且够长 ----
    if len(compact) >= 16 and all(ch in BAIJIAXING for ch in compact):
        try:
            out = decode_baijiaxing(compact)
        except Exception:
            return None
        if _LINK_RE.search(out):
            return out, "百家姓"

    # ---- 核心价值观：按 2 字切成词，每词都在词表里 ----
    if len(compact) >= 24 and len(compact) % 2 == 0 and set(compact) <= _CORE_CHARS:
        words = [compact[i:i + 2] for i in range(0, len(compact), 2)]
        if all(w in CORE_WORDS for w in words):
            try:
                out = decode_core_values(compact)
            except Exception:
                return None
            if _LINK_RE.search(out):
                return out, "核心价值观"

    return None
