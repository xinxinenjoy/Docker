"""磁力 / 链接解析的回归测试

跑法（在项目根目录）：
    python tests/test_parse.py

⚠️ 这些逻辑现在住在 app/links.py（纯标准库）里 —— 所以本测试**不需要**
fastapi / p115client 就能跑，改完随手就能验。

覆盖点：
  - 40 位 hex / 32 位 base32 的磁力 hash 归一化（含大小写）
  - 32 位 base32 就地替换成 40 位 hex，且不动其余参数
  - 多条拆分、按 info_hash 去重、不同 hash 不误合并
  - 多条【紧挨着】粘贴（直接拼接 / 逗号 / 顿号 / 零宽字符）也能拆开（2026-10-05 修）
  - ed2k / http 识别，115 自身链接排除
  - 空输入 / 纯文字返回空
  - **中文暗号还原后能正常进入解析**（百家姓 / 核心价值观 / 佛曰，2026-10-05 加）
"""
import base64
import binascii
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from links import fix_magnet, normalize_hash, parse_links  # noqa: E402

fail = []


def check(name, got, want):
    ok = got == want
    print(("  [OK] " if ok else "  [!!] ") + name)
    if not ok:
        print("       got :", got)
        print("       want:", want)
        fail.append(name)


H40 = "0123456789abcdef0123456789abcdef01234567"
B32 = base64.b32encode(binascii.unhexlify(H40)).decode()
# 另一个完全不同的种子：用来证明「不该合并的没被合并」
H40_B = "89abcdef0123456789abcdef0123456789abcdef"
B32_B = base64.b32encode(binascii.unhexlify(H40_B)).decode()

print("=== 正反例：hash 归一化 ===")
print("  40位 hex :", H40)
print("  32位 b32 :", B32, f"(len={len(B32)})")
check("40 位 hex 原样保留", normalize_hash(H40), H40)
check("40 位 hex 大写转小写", normalize_hash(H40.upper()), H40)
check("32 位 base32 -> 40 位 hex", normalize_hash(B32), H40)
check("32 位 base32 小写也认", normalize_hash(B32.lower()), H40)
check("非法长度原样返回(小写)", normalize_hash("ZZZZ"), "zzzz")

print("\n=== fix_magnet 就地替换 ===")
m32 = f"magnet:?xt=urn:btih:{B32}&dn=abc&tr=udp%3A%2F%2Ftr.example"
fixed = fix_magnet(m32)
check("32 位被替换", B32 in fixed, False)
check("40 位已写入", H40 in fixed, True)
check("其余参数保留", "dn=abc" in fixed and "tr=udp" in fixed, True)

print("\n=== parse_links 拆分 + 去重 ===")
text = f"""
随便一段文字
magnet:?xt=urn:btih:{B32}&dn=一&tr=udp://tr.a
magnet:?xt=urn:btih:{H40}&dn=二
重复的 magnet:?xt=urn:btih:{H40}&dn=三
magnet:?xt=urn:btih:{B32_B}&dn=四
ed2k://|file|测试.mkv|1234567890|0123456789abcdef0123456789abcdef|/
https://example.com/a.zip
https://115.com/s/xxx   <- 应被忽略
"""
items = parse_links(text)
for i in items:
    print(f"  {i['type']:7} changed={str(i['changed']):5} {i['url'][:62]}")

kinds = [i["type"] for i in items]
check("magnet=2（同 hash 的 32/40 位写法合并成 1，另一不同 hash 各留 1）", kinds.count("magnet"), 2)
check("ed2k=1", kinds.count("ed2k"), 1)
check("http=1（115 自身链接被忽略）", kinds.count("http"), 1)
check("总条数=4", len(items), 4)

magnets = [i for i in items if i["type"] == "magnet"]
check("32 位那条被标记 changed", magnets[0]["changed"], True)
check("32 位那条 url 已是 40 位", H40 in magnets[0]["url"], True)
check("不同 hash 没被误合并", H40_B in magnets[1]["url"], True)

print("\n=== 多条挨着粘贴（2026-10-05 修的 bug）===")
# 旧实现 `magnet:\?[^\s"'<>]+` 是「吃到空白为止」：两条之间没有真正的空白
# （直接拼接 / 逗号 / 顿号 / 零宽字符）就吞成一整条，BTIH 只取第一个 ⇒ 其余消失。
# 现改为负向前瞻切分，与分隔符无关。
M_A = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=AAA"
M_B = "magnet:?xt=urn:btih:89abcdef0123456789abcdef0123456789abcdef&dn=BBB"
M_C = "magnet:?xt=urn:btih:fedcba9876543210fedcba9876543210fedcba98&dn=CCC"
E_A = "ed2k://|file|a.mkv|123|0123456789abcdef0123456789abcdef|/"
E_B = "ed2k://|file|b.mkv|456|89abcdef0123456789abcdef0123456789|/"

STICKY = [
    ("换行分隔",       f"{M_A}\n{M_B}", 2),
    ("空格分隔",       f"{M_A} {M_B}", 2),
    ("直接拼接",       f"{M_A}{M_B}", 2),
    ("逗号分隔",       f"{M_A},{M_B}", 2),
    ("中文逗号分隔",    f"{M_A}，{M_B}", 2),
    ("顿号分隔",       f"{M_A}、{M_B}", 2),
    ("分号分隔",       f"{M_A};{M_B}", 2),
    ("零宽空格U+200B", f"{M_A}\u200b{M_B}", 2),
    ("全角空格U+3000", f"{M_A}\u3000{M_B}", 2),
    ("BOM U+FEFF",    f"{M_A}\ufeff{M_B}", 2),
    ("三条全粘连",     f"{M_A}{M_B}{M_C}", 3),
    ("文字夹带粘连",    f"看这个 {M_A}{M_B} 还有 {M_C}", 3),
    ("ed2k 挨着",      f"{E_A}{E_B}", 2),
    ("磁力挨着ed2k",    f"{M_A}{E_A}", 2),
]
for name, txt, want in STICKY:
    got = [i for i in parse_links(txt) if i["type"] in ("magnet", "ed2k")]
    check(f"{name} -> {want} 条", len(got), want)
    # 尾部不能残留分隔符，否则推给 115 会失败
    check(f"{name} -> 尾部干净", [i["url"][-1] in ",，、;；|" for i in got], [False] * want)

print("\n=== 空输入 ===")
check("空串返回空列表", parse_links(""), [])
check("纯文字返回空列表", parse_links("今天天气不错"), [])

# =====================================================================
print("\n=== 中文暗号：还原后进入解析（2026-10-05 加）===")
# 三种暗号 + 一条裸链接混着粘 —— 这是真实使用姿势（每行一条）
from cipher import CORE_WORDS  # noqa: E402

LINK = "magnet:?xt=urn:btih:947f7f0b7b7c0090d2252846760fd60eb9a1e75b"

# 百家姓暗号（用官方映射表的反表造）
_SRC = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-_+=/?#%&*:|"
_DST = ("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻"
        "福水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳唐罗薛伍余米贝姚孟顾尹江钟竺赖")
B = "".join(dict(zip(_SRC, _DST))[ch] for ch in LINK)

# 核心价值观暗号
C = "".join(
    CORE_WORDS[int(n, 16)] if int(n, 16) < 10 else CORE_WORDS[10] + CORE_WORDS[int(n, 16) - 10]
    for byte in LINK.encode()
    for n in format(byte, "02x")
)

MIX = f"{B}\n{C}\nmagnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
items2 = parse_links(MIX)
print("  识别到：", [(i["type"], i["source"], i["url"][:34]) for i in items2])
check("混粘共 2 条（百家姓 + 核心价值观 指向同一 hash，合并）", len(items2), 2)
check("来源被标注为百家姓", items2[0]["source"], "百家姓")
check("来源被标注为链接", items2[1]["source"], "链接")
check("暗号解出的 hash 正确", LOWER_OK := (LINK in [i["url"] for i in items2]), True)

only_bjx = parse_links(B)
check("只粘一条百家姓暗号", len(only_bjx), 1)
check("只粘一条百家姓暗号 · 来源", only_bjx[0]["source"], "百家姓")
check("只粘一条百家姓暗号 · url", only_bjx[0]["url"], LINK)

check("裸链接来源标注为「链接」", parse_links("https://example.com/a.zip")[0]["source"], "链接")

print("\n" + ("全部通过" if not fail else f"失败 {len(fail)} 项: {fail}"))
sys.exit(1 if fail else 0)
