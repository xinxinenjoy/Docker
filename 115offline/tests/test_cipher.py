"""暗号还原的回归测试

跑法（项目根目录）：
    python tests/test_cipher.py

覆盖：
  - AES-256 对 **FIPS-197 官方向量**（这是纯 Python 实现的正确性根基）
  - S 盒抽样（防止生成算法写错）
  - 佛曰对社区测试向量（『test测试测试测试』）
  - 百家姓对 **参考图里暗号↔磁力的交叉验证**（前 14 字符 947F7F0B7B7C00 吻合）
  - 核心价值观对 CTF 实测向量 + 自建编解码往返
  - restore_any 的识别与**误判防护**（反例必须拒）
"""
import binascii
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from cipher import (  # noqa: E402
    BAIJIAXING,
    CORE_WORDS,
    _SBOX,
    _xtime,
    aes256_cbc_decrypt,
    decode_baijiaxing,
    decode_core_values,
    decode_tudou,
    restore_any,
)

fail = []


def check(name, got, want):
    ok = got == want
    print(("  [OK] " if ok else "  [!!] ") + name)
    if not ok:
        print("       got :", repr(got))
        print("       want:", repr(want))
        fail.append(name)
    return ok


def check_true(name, cond, extra=""):
    return check(name + ((" " + extra) if extra else ""), bool(cond), True)


# =====================================================================
print("=== AES-256：FIPS-197 官方向量（Appendix C.3）===")
# 这是纯 Python AES 的命门：通了才敢用在佛曰上
KEY = bytes(range(32))  # 000102...1f
CIPHERTEXT = binascii.unhexlify("8ea2b7ca516745bfeafc49904b496089")
EXPECTED_PLAIN = binascii.unhexlify("00112233445566778899aabbccddeeff")
check("AES-256-CBC 解密官方密文", aes256_cbc_decrypt(KEY, b"\x00" * 16, CIPHERTEXT), EXPECTED_PLAIN)

print("\n=== S 盒抽样（打错一个字符全盘失效，必须卡住）===")
for i, want in ((0x00, 0x63), (0x01, 0x7C), (0x10, 0xCA), (0x53, 0xED), (0xFF, 0x16)):
    check("sbox[0x%02x]" % i, _SBOX[i], want)

# =====================================================================
print("\n=== 佛曰：社区测试向量 ===")
# 向量来自 p115client 作者整理的 TudouCode Python 实现示例
TUDOU_SAMPLE = (
    "佛曰：梵依沙若罰怖諳羅怯大罰波除諳室若若奢伽涅怯究皤苦冥逝冥曳麼梵咒離除奢咒梵夷有怯神倒所滅俱孕盡等呐佛"
)
check("佛曰密文 → 原文", decode_tudou(TUDOU_SAMPLE), "test测试测试测试")
check("容忍半角冒号", decode_tudou(TUDOU_SAMPLE.replace("佛曰：", "佛曰:")), "test测试测试测试")
check("容忍换行/空格（聊天软件粘贴）", decode_tudou(TUDOU_SAMPLE[:12] + "\n" + TUDOU_SAMPLE[12:]), "test测试测试测试")

# =====================================================================
print("\n=== 百家姓：用参考图的暗号 × 磁力 交叉验证 ===")
# 参考图（115 离线助手）里同时给出了暗号与还原后的磁力：
#   暗号  陈周王苏王苏赵水王水王窦赵赵陈陈章孙孙吴孙冯周郑王郑赵苏章郑赵云水陈福钱云王吴水
#   磁力  magnet:?xt=urn:btih:947F7F0B7B7C0090D2252846760FD60EB9A1E75B
# 截图分辨率有限，逐字辨认有风险；但**前 14 个字符完全吻合**，
# 足以锁定映射表正确 —— 这里就锁这一段（不锁全长，避免把截图的辨认误差当成算法错）。
REF_CIPHER = "陈周王苏王苏赵水王水王窦赵赵陈陈章孙孙吴孙冯周郑王郑赵苏章郑赵云水陈福钱云王吴水"
check("百家姓前 14 字符 → 947F7F0B7B7C00", decode_baijiaxing(REF_CIPHER)[:14], "947F7F0B7B7C00")

# 用反表构造一条完整暗号，做往返
def encode_baijiaxing(link: str) -> str:
    src = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-_+=/?#%&*:|"
    dst = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻"
    dst += "福水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳唐罗薛伍余米贝姚孟顾尹江钟竺赖"
    fwd = dict(zip(src, dst))
    return "".join(fwd[ch] for ch in link)


MAGNET = "magnet:?xt=urn:btih:947F7F0B7B7C0090D2252846760FD60EB9A1E75B"
bjx = encode_baijiaxing(MAGNET)
print("  构造的暗号：", bjx)
check("百家姓往返", decode_baijiaxing(bjx), MAGNET)
check("旧版竖线 卜 兼容", decode_baijiaxing("卜"), "|")
check("赖 / 卜 都映射到竖线", BAIJIAXING["赖"], BAIJIAXING["卜"])
check("表大小 76", len(BAIJIAXING), 76)

# =====================================================================
print("\n=== 核心价值观 ===")
# CTF 实测向量（前缀段，逐词可人工核对）：
#   公正=6 公正=6 公正=6 诚信文明=10+2=0xc 公正=6 民主=1 公正=6 法治=7 法治=7 友善平等=5+6=0xb
check("CTF 向量 → flag{", decode_core_values("公正公正公正诚信文明公正民主公正法治法治友善平等"), "flag{")


def encode_core_values(text: str) -> str:
    """测试用编码器（与解码器互为反函数）。"""
    out = []
    for byte in text.encode("utf-8"):
        for nibble in format(byte, "02x"):
            v = int(nibble, 16)
            if v < 10:
                out.append(CORE_WORDS[v])
            else:
                out.append(CORE_WORDS[10] + CORE_WORDS[v - 10])
    return "".join(out)


core = encode_core_values(MAGNET)
print("  构造的暗号前 40 字：", core[:40])
check("核心价值观往返", decode_core_values(core), MAGNET)

# =====================================================================
print("\n=== restore_any：识别（正例）===")
got = restore_any(bjx)
check("百家姓被识别", got, (MAGNET, "百家姓"))
got = restore_any(core)
check("核心价值观被识别", got, (MAGNET, "核心价值观"))
# ⚠️ 手头的佛曰测试向量解出来是「test测试测试测试」，**不是链接** ⇒ restore_any 必须拒它。
# （这正好顺带验证了「结果必须是链接」这道闸门确实在起作用）
check("佛曰样例解出非链接 ⇒ 必须拒", restore_any(TUDOU_SAMPLE), None)

# 一条真正指向链接的佛曰：用解码器反推不方便，改为直接构造
# （用「与佛论禅」加密需要 AES 加密，本模块只做解密 ⇒ 这里用已知可解密的密文不现实，
#   故佛曰的 restore_any 正例在真实使用时由人工粘一条验证，见 README 的实测记录）

print("\n=== restore_any：误判防护（反例必须拒）===")
check("普通中文句子", restore_any("今天天气不错，适合出去玩"), None)
check("纯数字/英文", restore_any("hello world 12345"), None)
check("太短的百家姓串", restore_any("陈王张李"), None)
check("百家姓串但解出来不是链接", restore_any("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨"), None)
check("核心价值观词但解出来不是链接", restore_any("富强民主文明和谐自由平等公正法治"), None)
check("佛曰前缀但内容非法", restore_any("佛曰:你好世界"), None)
check("空串", restore_any(""), None)
check("带中文标签前缀也能识别", restore_any("百家姓：" + bjx), (MAGNET, "百家姓"))

print("\n=== 佛曰 → 链接：往返（自建编码器，它本身先被 FIPS 向量验过）===")
# 上面只验了「向量能解出 test测试测试测试」；但业务上真正要走的是「佛曰 → **链接**」。
# 由于本模块只做解密（不需要加密能力），这里在测试内自建一个编码器 ——
# 先用 FIPS 的**加密**方向验它本身，再用它造一条「佛曰版磁力」来打通生产路径。


def _shift_rows(s):
    out = [0] * 16
    for c in range(4):
        for r in range(4):
            out[4 * c + r] = s[4 * ((c + r) % 4) + r]
    return out


def _mix_columns(s):
    out = [0] * 16
    for c in range(4):
        a0, a1, a2, a3 = s[4 * c:4 * c + 4]
        out[4 * c + 0] = _xtime(a0) ^ (_xtime(a1) ^ a1) ^ a2 ^ a3
        out[4 * c + 1] = a0 ^ _xtime(a1) ^ (_xtime(a2) ^ a2) ^ a3
        out[4 * c + 2] = a0 ^ a1 ^ _xtime(a2) ^ (_xtime(a3) ^ a3)
        out[4 * c + 3] = (_xtime(a0) ^ a0) ^ a1 ^ a2 ^ _xtime(a3)
    return out


def _encrypt_block(block, rk):
    s = [block[i] ^ rk[0][i] for i in range(16)]
    for rnd in range(1, len(rk) - 1):
        s = [_SBOX[b] for b in s]
        s = _shift_rows(s)
        s = _mix_columns(s)
        s = [s[i] ^ rk[rnd][i] for i in range(16)]
    s = [_SBOX[b] for b in s]
    s = _shift_rows(s)
    return bytes(s[i] ^ rk[-1][i] for i in range(16))


def aes256_cbc_encrypt(key, iv, data):
    from cipher import _expand_key

    rk = _expand_key(key)
    out = bytearray()
    prev = iv
    for off in range(0, len(data), 16):
        blk = bytes(a ^ b for a, b in zip(data[off:off + 16], prev))
        enc = _encrypt_block(blk, rk)
        out += enc
        prev = enc
    return bytes(out)


check("编码器本身正确（FIPS 加密方向）", aes256_cbc_encrypt(KEY, b"\x00" * 16, EXPECTED_PLAIN), CIPHERTEXT)


def tudou_encode(text):
    from cipher import _TUDOU_IV, _TUDOU_KEY, _TUDOU_MARK, _TUDOU_TABLE

    data = text.encode("utf-16le")
    pads = (-len(data)) % 16
    data += bytes([pads]) * pads
    raw = aes256_cbc_encrypt(_TUDOU_KEY, _TUDOU_IV, data)
    body = "".join(_TUDOU_TABLE[b] if b < 128 else _TUDOU_MARK[0] + _TUDOU_TABLE[b - 128] for b in raw)
    return "佛曰：" + body


TUDOU_LINK = tudou_encode(MAGNET)
print("  构造的佛曰（前 30 字）：", TUDOU_LINK[:33], "…")
check("佛曰 → 链接 被识别", restore_any(TUDOU_LINK), (MAGNET, "佛曰"))
check("佛曰 → 链接（含高位字节）", decode_tudou(TUDOU_LINK), MAGNET)

# 混合粘贴：三条暗号 + 一条裸链接，各自都要认出来
MIXED = bjx + "\n" + core + "\n" + TUDOU_LINK + "\n" + "https://example.com/a.zip"
lines = [ln for ln in MIXED.split("\n")]
kinds = [restore_any(ln) for ln in lines]
check("混合粘贴逐行识别：百家姓", kinds[0], (MAGNET, "百家姓"))
check("混合粘贴逐行识别：核心价值观", kinds[1], (MAGNET, "核心价值观"))
check("混合粘贴逐行识别：佛曰", kinds[2], (MAGNET, "佛曰"))
check("混合粘贴逐行识别：裸链接不误判", kinds[3], None)

print("\n" + ("全部通过" if not fail else "失败 %d 项: %s" % (len(fail), fail)))
sys.exit(1 if fail else 0)
