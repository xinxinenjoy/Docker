"""番号识别 —— 规则**照抄 JavSP**（`javsp/avid.py`，GPL-3.0），噪声词表来自**本盘实测**。

为什么抄而不是自己编：番号形态极其杂乱（FC2 / HEYDOUGA / MGStage 数字前缀 / TMA 的 T28 /
东热的 N、K 系 / 无码纯数字…），JavSP 是 5,179★ 的成熟刮削器，这些边角它全踩过。
自己拍脑袋写正则的结果是「能认出 60%、剩下的静默漏掉」—— 而漏掉的恰恰是最需要人工兜的那批。

⚠️ 但**不能整段照搬**，两处按本项目改了：
  1. `get_id()` 里 JavSP 会**回退到父目录名**再匹配一次（`return get_id(filepath.parent.name)`）。
     我们是在**目录名**上跑，回退会让 `云下载` 自己被打上番号 ⇒ 去掉回退，判不出就返回空。
  2. 补了一层**噪声剥离**：JavSP 靠用户配的 `ignored_id_pattern`，我们这边是从
     7 万条真实目录名里统计出来的（`第一會所新片@` 218 次 / `Thz.la` 144 次 / 后缀 `-C` 502 次 …）。

来源：https://github.com/Yuukiy/JavSP/blob/master/javsp/avid.py
      统计口径：本盘 7 万条真实目录名的**逐条计数**（数值 = 出现次数）
"""
from __future__ import annotations

import re

__all__ = [
    "normalize_name",
    "get_id",
    "format_id",
    "series_of",
    "guess_av_type",
    "kind_of",
    "has_cjk",
    "Kind",
    "dedupe_prefix",
]

# --------------------------------------------------------------- 站点前缀污染纠错
# ⚠️ 红领巾 2026-10-06 定：**核对番号时不能照名字硬认系列** —— `manipzz-137` 这类
#    是站点前缀污染（真实番号是 `IPZZ-137`，属于 IPZZ 系列），不能当 `MANIPZZ` 系列。
#    这里是「污染前缀 → 真实前缀」的映射表。命中后整体替换，再走正常识别。
#    前缀形态可能是「字母串」也可能是「字母串+数字」（如 manipzz 后面直接跟番号数字）。
#    ⚠️ 只做**前缀**替换：`manipzz-137` → `IPZZ-137`；不会碰「名字中间夹着」的。
_PREFIX_FIXES: dict[str, str] = {
    "manipzz": "IPZZ",       # 污染名 → 真实片商 Idea Pocket 的 IPZZ 系
}

# 也要防「污染前缀 + 规范后缀」的混合形态：`manipzz-137ch` / `manipzz-137-C`
# 先把尾巴剥掉再做前缀替换，这样 `manipzz-137ch` 也能干净地纠成 `IPZZ-137`。
_PREFIX_FIX_SPLIT_RE = re.compile(r"(?i)^([a-z0-9]{2,20})[-_\.]?(\d{2,6})(.*)$")


def dedupe_prefix(name: str) -> str:
    """把站点前缀污染名还原成真实番号形态。

    `manipzz-137` → `IPZZ-137`（前缀被映射表替换，数字/尾巴保留）。
    不认识的形态原样返回 —— 宁可少认，不瞎改。
    """
    s = (name or "").strip()
    m = _PREFIX_FIX_SPLIT_RE.match(s)
    if not m:
        return s
    prefix, digits, tail = m.group(1).lower(), m.group(2), m.group(3)
    real = _PREFIX_FIXES.get(prefix)
    if not real:
        return s
    return f"{real}-{digits}{tail}"

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def has_cjk(s: str) -> bool:
    """名字里有没有中文 —— 判断「该不该套中文命名规则」用。"""
    return bool(_CJK_RE.search(s or ""))

# --------------------------------------------------------------- 噪声：站点前缀
# 实测计数来自本盘 7 万条目录名（数值 = 出现次数）
_SITE_PREFIX = (
    "第一會所新片@", "第一会所新片@", "第一會所@", "第一会所@",
    "Thz.la", "ThZu.Cc", "7sht.me", "22sht.me", "44x.me", "168x.me", "dccdom.com@",
    "madoubt.com", "18P2P.COM", "RUNBKK",
)
# 站点 / 推广后缀（`@` 后面跟域名那种，如 `BACJ-079.[4K]@RUNBKK`）
_AT_TAIL_RE = re.compile(r"@[0-9A-Za-z.\-_]{2,30}$")
# 前缀域名（`madoubt.com 982886.xyz HMN-870` 这种一串域名打头）
_HOST_HEAD_RE = re.compile(
    r"^(?:(?:https?://|www\.)?[0-9A-Za-z\-]+\.(?:com|net|org|cn|cc|tv|me|xyz|top|vip|info|io|la|pw|biz|la|fun|la)"
    r"[\s._\-]*)+",
    re.I,
)
# 数字前缀站点（`489155.com@SORA-650`）
_AT_HEAD_RE = re.compile(r"^[0-9A-Za-z.\-_]{2,30}@")
# 「这一截里像不像番号」——**只用于判断剥离会不会带走番号**，不做提取（提取是 `get_id` 的事）。
# 为什么需要它：剥离是按「站点形态」走的，但站点形态和番号形态**会重叠**：
# 实测 `@蜂鳥@FENGNIAO151.VIP-IPX-714_2K` 里，`@FENGNIAO151.VIP-IPX-714_2K` 整体满足
# 「`@` + 一串字母数字点和横线」的站点尾巴形态 ⇒ 被整段剥掉 ⇒ 番号 `IPX-714` 也没了。
_ID_ISH_RE = re.compile(
    r"[A-Z0-9]{2,8}[-_]\d{2,6}|FC2|HEYDOUGA|GETCHU|GYUTTO|LUXU|PONDO", re.I)

# --------------------------------------------------------------- 噪声：后缀标签
# `DLDSS-532-C`(502 次) / `-ch`(66) / `-U`(32) / `-UC`(27) / `-uncensored-HD`(28)
# / `-HD`(65) / `-FHD`(38) / `-2K`(12) / `-AI`(11) / `-C_GG5` / `-C_X1080X` / `-4K60FPS`
# ⚠️ 只在**数字之后**作为整段剥离，所以不会伤到 `JUR`、`C` 这种真系列名。
_TAIL_TAG_RE = re.compile(
    r"(?:[\s._\-]*(?:"
    r"uncensored(?:[\s\-_]*HD)?|无码破解|無碼破解|无码|無碼|中文字幕|中字|"
    r"4K60FPS|X1080X|GG5|FHD|UHD|HD|SD|AI|4K|2K|8K|UC|CH|C|U"
    r"))+$",
    re.I,
)
_BRACKET_RE = re.compile(r"【[^】]*】|\[[^\]]*\]|（[^）]*）|\([^)]*\)")
_EXT_RE = re.compile(r"\.(?:mp4|mkv|avi|wmv|mov|mpg|mpeg|flv|rmvb|rm|asf|ts|m2ts|webm|3gp|m4v|f4v|iso|vob|hd|ktr)$", re.I)
_SEP_TAIL_RE = re.compile(r"[\s._\-]+$")
_LEAD_TAG_RE = re.compile(r"^(?:[\s._\-]*(?:HD|FHD|UHD|4K|8K|AI|无码|無碼|中文字幕))+[\s._\-]*", re.I)

# 不能当系列名的技术词（否则 `WEB-DL`、`H265-1080` 这种会被误判成番号）
_NOT_SERIES = frozenset((
    "WEB", "WEBRIP", "WEBDL", "BLURAY", "BDRIP", "BRRIP", "HDTV", "DVDRIP", "HDRIP",
    "REMUX", "HEVC", "AVC", "X264", "X265", "H264", "H265", "AAC", "AC3", "EAC3",
    "DTS", "FLAC", "TRUEHD", "ATMOS", "HDR", "HDR10", "DOVI", "SDR", "IMAX",
    "MP4", "MKV", "AVI", "WMV", "MOV", "MPG", "MPEG", "RMVB", "WEBM", "TS", "M2TS",
    "FPS", "CHS", "CHT", "ENG", "JPN", "CHI", "GB", "BIG5", "REPACK", "PROPER",
    "EXTENDED", "REMASTERED", "COMPLETE", "V2", "V3", "PART", "CD1", "CD2",
    "S01", "S02", "S03", "E01", "E02", "P2P", "XC", "ALT", "DREAMHD", "ADWEB",
))


def _norm_key(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def normalize_name(name: str) -> str:
    """把原始目录名/文件名洗成「只剩番号本体」的形态（大写、去站点、去后缀标签）。

    不可逆 —— 只用于**识别**，不用作新名字。新名字由 `format_id()` 生成。

    🔴 铁律：**剥离动作只在不把内容掏空的前提下进行**。
       实测踩过两次，都是「越洗越空」：
         · `fc2-ppv-1567975-nyap2p.com` → 域名正则把**整串**吃掉 ⇒ 洗成空串 ⇒
           番号识别失败 ⇒ 正片被当成「纯域名广告」列进待清理。
         · `@蜂鳥@FENGNIAO151.VIP-IPX-714_2K` → 洗成 `蜂鳥` ⇒ 同上。
       所以每次剥离前都检查「**剥完还剩不剩东西**」，不剩就退回上一状态。
    """
    s = (name or "").strip()
    if not s:
        return ""
    s = _EXT_RE.sub("", s)
    # 括号段全部去掉（站点/版本/字幕说明都不进番号）
    s = _BRACKET_RE.sub(" ", s)

    def _has_content(x: str) -> bool:
        return bool(x.strip(" ._-@\u3000"))

    def _keeps_id(cut: str, rest: str) -> bool:
        """切掉 `cut` 这一截，会不会把番号带走？会 ⇒ 不切。

        两个条件同时成立才算「会带走」：
          ① `cut` 里面**有**番号形态（`IPX-714`）；
          ② `rest` 里面**没有**番号形态。
        反过来（番号在 `rest` 里）就必须切 —— 那才是正常的「站点前缀 + 番号」。
        `489155.com@SORA-650` 走的就是这一支：`cut = 489155.com` 无番号形态 ⇒ 切。
        """
        return not (_ID_ISH_RE.search(cut) and not _ID_ISH_RE.search(rest))

    def strip_once(x: str) -> str:
        """剥一层**前缀**站点/域名。**剥完必须还有内容、且没把番号带走**，否则原样返回。"""
        for pat in (_HOST_HEAD_RE, _AT_HEAD_RE):
            m = pat.match(x)
            if m and _has_content(x[m.end():]) and _keeps_id(x[: m.end()], x[m.end():]):
                return x[m.end():].lstrip("@. _-")
        for w in _SITE_PREFIX:
            if x.lower().startswith(w.lower()) and _has_content(x[len(w):]):
                return x[len(w):].lstrip("@. _-")
        for w in ("SIS001", "1024", "2048", "SEX8", "18P2P"):
            if x.upper().startswith(w) and _has_content(x[len(w):]):
                return x[len(w):].lstrip("@. _-")
        return x

    def strip_tail_at(x: str) -> str:
        """剥一层 `@站点` 尾巴。同样过两道闸（够长才剥 + 不带走番号）。"""
        m = _AT_TAIL_RE.search(x)
        if not m:
            return x
        head, tail = x[: m.start()], x[m.start():]
        if not _has_content(head) or not _keeps_id(tail, head):
            return x
        return head

    # 前后交替剥，直到稳定（`第一會所新片@SIS001@300MIUM-1446` 要剥三层）
    for _ in range(8):
        nxt = strip_tail_at(strip_once(s))
        if nxt == s:
            break
        s = nxt

    s = s.rstrip(" ._-")
    s = _TAIL_TAG_RE.sub("", s)
    s = s.rstrip(" ._-")
    s = _TAIL_TAG_RE.sub("", s)
    s = _LEAD_TAG_RE.sub("", s)
    s = s.strip(" ._-")
    return s.upper()


# --------------------------------------------------------------- 番号规则（JavSP）
_FC2_RE = re.compile(r"FC2[^A-Z\d]{0,5}(PPV[^A-Z\d]{0,5})?(\d{5,7})", re.I)
_HEYDOUGA_RE = re.compile(r"(HEYDOUGA)[-_]*(\d{4})[-_]0?(\d{3,5})", re.I)
_HEY_RE = re.compile(r"(?:HEY)[-_]*(\d{4})[-_]0?(\d{3,5})", re.I)
_GETCHU_RE = re.compile(r"GETCHU[-_]*(\d+)", re.I)
_GYUTTO_RE = re.compile(r"GYUTTO-(\d+)", re.I)
_LUXU259_RE = re.compile(r"259LUXU-(\d+)", re.I)
_MUGEN_RE = re.compile(r"(MKB?D)[-_]*(S\d{2,3})|(MK3D2DBD|S2M|S2MBD)[-_]*(\d{2,3})", re.I)
_IBW_RE = re.compile(r"(IBW)[-_](\d{2,5}z)", re.I)
# MGStage 系：`300MIUM-1446` / `230OREC-xxx` —— 必须排在通用规则**之前**，
# 否则通用规则会从 `300MIUM-1446` 里切出 `MIUM-1446`（实测本盘有 `300MIUM-1446`）
_MGSTAGE_RE = re.compile(r"\b(\d{3})([A-Z]{3,6})[-_]?(\d{3,5})\b", re.I)
# 无码站（1Pondo / Carib / Pacoma…）：`系列-日期_序号`
_DATE_SERIES_RE = re.compile(r"\b1?([A-Z]{3,8})[-_](\d{6})[-_](\d{2,4})\b", re.I)
_GENERIC_SEP_RE = re.compile(r"([A-Z]{2,10})[-_](\d{2,5})", re.I)
_TOKYO_RE = re.compile(r"(RED[01]\d\d|SKY[0-3]\d\d|EX00[01]\d)", re.I)
_GENERIC_NOSEP_RE = re.compile(r"([A-Z]{2,})(\d{2,5})", re.I)
_TMA_RE = re.compile(r"(T[23]8[-_]\d{3})")
_NK_RE = re.compile(r"\b([NK]\d{4})\b", re.I)
_R18_RE = re.compile(r"(R18-?\d{3})", re.I)
_DIGITS_RE = re.compile(r"(\d{6}[-_]\d{2,3})")


def get_id(name: str) -> str:
    """从目录名/文件名里提取番号（DVD ID）。判不出返回空串。

    ⚠️ 顺序敏感 —— 每一条「更具体的形态」都必须排在通用规则之前，否则会被通用规则
       切出**看起来对、其实错**的番号（`300MIUM-1446` → `MIUM-1446` 就是典型）。

    ⚠️ 2026-10-06 起**先做污染前缀纠错**（`manipzz-137` → `IPZZ-137`），
       纠错命中则用纠错后的名字走正常识别；没命中才用原名。
    """
    fixed = dedupe_prefix(name)
    if fixed != name:
        got = _raw_get_id(fixed)
        if got:
            return got
    return _raw_get_id(name)


def _raw_get_id(name: str) -> str:
    norm = normalize_name(name)
    if not norm:
        return ""

    # --- 带特殊字样的番号（JavSP 原文顺序）---------------------------------
    if "FC2" in norm:
        m = _FC2_RE.search(norm)
        if m:
            return "FC2-" + m.group(2)
    if "HEYDOUGA" in norm:
        m = _HEYDOUGA_RE.search(norm)
        if m:
            return "-".join(m.groups())
    if "GETCHU" in norm:
        m = _GETCHU_RE.search(norm)
        if m:
            return "GETCHU-" + m.group(1)
    if "GYUTTO" in norm:
        m = _GYUTTO_RE.search(norm)
        if m:
            return "GYUTTO-" + m.group(1)
    if "259LUXU" in norm:
        m = _LUXU259_RE.search(norm)
        if m:
            return "259LUXU-" + m.group(1)

    # --- 去掉域名后再试一次（`madoubt.com HMN-870`）--------------------------
    no_domain = re.sub(r"\w{3,10}\.(?:COM|NET|APP|XYZ|CC|ME|LA|TV|IO)", "", norm, flags=re.I)
    if no_domain != norm:
        got = _raw_get_id(no_domain)
        if got:
            return got

    # --- HEY 缩写 / MUGEN / IBW ------------------------------------------
    m = _HEY_RE.search(norm)
    if m:
        return "heydouga-" + "-".join(m.groups())
    m = _MUGEN_RE.search(norm)
    if m:
        if m.group(1) is not None:
            return f"{m.group(1)}-{m.group(2)}"
        return f"{m.group(3)}-{m.group(4)}"
    m = _IBW_RE.search(norm)
    if m:
        return f"{m.group(1)}-{m.group(2)}"

    # --- MGStage 数字前缀（必须早于通用规则）-------------------------------
    # ⚠️ 返回时**数字前缀要带上**：`300MIUM-1446` 的系列名就是 `300MIUM`，
    #    只回 `MIUM-1446` 会让 `_GENERIC_NOSEP_RE` 之类在别处切出错误的系列。
    m = _MGSTAGE_RE.search(norm)
    if m and _norm_key(m.group(2)) not in _NOT_SERIES:
        return f"{m.group(1)}{m.group(2).upper()}-{m.group(3)}"

    # --- 无码站点的「日期_序号」番号：`1Pondo-041614_791` / `Carib-010112-123` ----
    #     这几个站不按 `系列-数字` 走，硬套通用规则会切出 `PONDO-04161` 这种残号。
    m = _DATE_SERIES_RE.search(norm)
    if m:
        return f"{m.group(1).upper()}-{m.group(2)}_{m.group(3)}"

    # --- 通用：带分隔符 ---------------------------------------------------
    m = _GENERIC_SEP_RE.search(norm)
    if m and _norm_key(m.group(1)) not in _NOT_SERIES:
        return f"{m.group(1)}-{m.group(2)}"

    # --- 东热 red / sky / ex（无分隔，已停更，数字范围收紧以降误判）----------
    m = _TOKYO_RE.search(norm)
    if m:
        return m.group(1)

    # --- 通用：缺分隔符（`SSIS123`）---------------------------------------
    m = _GENERIC_NOSEP_RE.search(norm)
    if m and _norm_key(m.group(1)) not in _NOT_SERIES and len(m.group(1)) <= 8:
        return f"{m.group(1)}-{m.group(2)}"

    # --- TMA / 东热 N、K 系 / R18 / 无码纯数字 ------------------------------
    m = _TMA_RE.search(norm)
    if m:
        return m.group(1)
    m = _NK_RE.search(norm)
    if m:
        return m.group(1).upper()
    m = _R18_RE.search(norm)
    if m:
        return m.group(1).upper()
    m = _DIGITS_RE.search(norm)
    if m:
        return m.group(1)

    # --- `)(` 当分隔符的少数片子 ------------------------------------------
    if ")(" in norm:
        got = _raw_get_id(norm.replace(")(", "-"))
        if got:
            return got
    return ""


def format_id(avid: str) -> str:
    """番号 → 规范目录名。统一 `系列-数字` 大写，数字**不补零**（115 上原始形态就是如此）。"""
    s = (avid or "").strip()
    m = re.match(r"^([A-Za-z0-9]+)[-_](\d+)$", s)
    if not m:
        return s
    return f"{m.group(1).upper()}-{m.group(2)}"


def series_of(avid: str) -> str:
    """番号 → 系列名（字母前缀）。`DLDSS-532` → `DLDSS`。

    这是「按系列整理」的分组键。
    """
    s = (avid or "").strip().upper()
    m = re.match(r"^([A-Z0-9]+?)[-_]\d+", s)
    if m:
        return m.group(1)
    m = re.match(r"^([A-Z]+)\d+", s)
    if m:
        return m.group(1)
    return s


def guess_av_type(avid: str) -> str:
    """番号分类：normal / fc2 / getchu —— 供将来接刮削源时区分。"""
    if re.match(r"^FC2-\d{5,7}$", avid or "", re.I):
        return "fc2"
    if re.match(r"^GETCHU-(\d+)", avid or "", re.I):
        return "getchu"
    return "normal"


# --------------------------------------------------------------- 内容分类
class Kind:
    JAV = "jav"            # 番号片（含无码）
    WESTERN = "western"    # 欧美（XXX 场景）
    TV = "tv"              # 电视剧（分集）
    MOVIE = "movie"        # 电影
    OTHER = "other"        # 分不出/其它


_WESTERN_RE = re.compile(r"\bXXX\b|-\s?P2P\b|\[XC\]", re.I)
_WESTERN_DATE_RE = re.compile(r"\.\d{2}\.\d{2}\.\d{2}\.")
# `2018-05-18` / `2018.05.18` —— 欧美场景片几乎都带发行日期
_WESTERN_ISO_RE = re.compile(r"\b(?:19|20)\d{2}[-.]\d{2}[-.]\d{2}\b")
# 已知欧美片商（方括号里出现就算）
_WESTERN_STUDIOS = (
    "TeensLoveHugeCocks", "Blacked", "Vixen", "Tushy", "Brazzers", "RealityKings",
    "Mofos", "BangBros", "NaughtyAmerica", "Private", "EvilAngel", "DigitalPlayground",
    "PornPros", "TeamSkeet", "FamilyStrokes", "BrattySis", "SisLovesMe", "PassionHD",
    "PureTaboo", "Trickery", "PropertySex", "Deeper", "TushyRaw", "Slayed",
)
_WESTERN_STUDIO_RE = re.compile("|".join(_WESTERN_STUDIOS), re.I)
_TV_MARK_RE = re.compile(r"[Ss]\d{1,2}[\s._-]?[Ee]\d{1,4}|第\s*\d{1,3}\s*[集话話季]|全\s*\d+\s*集")
_YEAR_RE = re.compile(r"(?<![\w\u4e00-\u9fff])((?:19|20)\d{2})(?![\w\u4e00-\u9fff])")
# 清晰度/来源标签 —— 有它 + 有年份，才敢认成影视作品（而不是随便一个带年份的目录）
_TECH_HINT_RE = re.compile(
    r"2160p|1080p|720p|4K|8K|UHD|REMUX|Blu-?Ray|WEB-?DL|WEB-?Rip|HDTV|DVDRip|HDRip"
    r"|H\.?26[45]|HEVC|x26[45]|HDR|DTS|Atmos|DDP",
    re.I,
)


def kind_of(name: str) -> str:
    """判断一个目录名 / 文件名属于哪一类。**顺序即优先级。**

    为什么这个顺序：番号片里也会出现年份（`… 2015) NEW.mp4`）和「第 N 集」这类字样，
    所以必须先判番号、再判剧集、最后才判电影。

    ⚠️ 电影判定**必须同时有年份和清晰度/来源标签**。
       曾经写过「只要有清晰度标签 + 有英文词就算电影」的兜底分支，实测立刻出事：
       `(HD720P)(S-Cute)(no.289)REI` 被判成电影、然后被 `namer` 改名成 `REI`（信息全丢）。
       本盘这类「带技术标签的番号/素人片」数量不小，宁可判不出（走 OTHER，只清垃圾不改名）。
    """
    s = name or ""
    if not s:
        return Kind.OTHER
    if get_id(s):
        return Kind.JAV
    if (_WESTERN_RE.search(s) or _WESTERN_STUDIO_RE.search(s)
            or _WESTERN_ISO_RE.search(s)
            or (_WESTERN_DATE_RE.search(s) and re.search(r"\d{3,4}p", s, re.I))):
        return Kind.WESTERN
    if _TV_MARK_RE.search(s):
        return Kind.TV
    if _YEAR_RE.search(s) and _TECH_HINT_RE.search(s):
        return Kind.MOVIE
    return Kind.OTHER
