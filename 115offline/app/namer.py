"""落地对象的「整理命名」—— 纯本地，不联网、不查库

命名规则由红领巾 2026-10-05 用自己库里的截图定下，并用 115 接口返回的真实目录名反证：

  顶层目录   `片名（年份）`        例：护肝人（2026）· 商海通牒（2011）· 寒战1994（2026）
  无年份时   `片名`                例：（原名里查不到年份，且未给年份提示）
  系列/多部  `片名（系列）`        例：小黄人（系列）· 惊声尖笑（系列）· 年会不能停！（系列）
  多季剧     在 `片名（年份）` 下再分 `第1季` / `第2季` …
  分集文件   `剧名.SxxExx.第 N 集.集标题.ext`   例：驻院医生.S01E01.第 1 集.Pilot.mkv
  无集标题   `剧名.SxxExx.ext`                 例：我的媳妇.S01E30.mkv

⚠️ 两处必须由实测而非猜测决定的事实：

1. **括号是全角** —— 接口 `fs_file` 返回过 `"n": "护仁人（2026）"` 这样的全角写��。
   但截图缩放后分辨不清，所以做成可切换（`paren='full'|'half'`），由界面上的开关决定。

2. **年份从哪来** —— ① 原名里能查到时用它（`Demon.Agent.2026.…` ⇒ 2026）；
   ② 查不到时用**离线任务的添加年份**兜底（`护肝人` 原文无年份，库里却是 2026）。
   `寒战1994` 这类「年份紧贴片名」的**不算**年份（靠 \b 边界区分），避免把片名切掉。

⚠️ 只做「删噪声 + 套格式」，**不做片名纠错**；所有结果都必须经预览页人工确认后才写回。
"""
from __future__ import annotations

import re

__all__ = [
    "clean",
    "clean_file",
    "title_of",
    "extract_year",
    "dir_name",
    "episode_name",
    "is_episode_file",
    "suggest",
    "suggest_episode",
    "parse_media",
    "sanitize_name",
    "ensure_ext",
    "paren_wrap",
    "looks_like_ad",
    "ADS",
]

# --------------------------------------------------------------------- 去广告
ADS = (
    "地址发布页",
    "地址發布頁",
    "发布页",
    "收藏不迷路",
    "请牢记",
    "請牢記",
    "防走失",
    "永久域名",
    "永久地址",
    "备用网址",
    "最新地址",
    "点击进入",
    "点击查看",
    "更多精彩",
    "免费观看",
    "在线观看",
    "高清影视之家",
    "最新电影",
    "迅雷下载",
    "磁力链接",
    "种子下载",
    "高清下载",
    "无水印",
    "请访问",
    "請訪問",
    "本站",
    "官网",
)

_BARE_URL_RE = re.compile(r"(?:https?://|www\.)[^\s，。、；;,）)】\]】]+", re.I)
_BRACKET_RE = re.compile(r"【[^】]*】|\[[^\]]*\]|（[^）]*）|\([^)]*\)")
_HOST_HINT_RE = re.compile(
    r"(?:https?://|www\.|[0-9a-zA-Z\-]+\.(?:com|net|org|cn|cc|tv|me|xyz|top|vip|info|io|la|pw|biz)\b)",
    re.I,
)
_BRACKET_AD_WORDS = ("发布", "发佈", "首发", "地址", "网址", "域名", "收藏", "不迷路",
                     "牢记", "牢記", "广告", "廣告", "最新电影", "访问", "訪問", "水印")

_DUP_SEP_RE = re.compile(r"([.\-_·])\1+")
_EDGE_RE = re.compile(r"^[\s.·\-_/|]+|[\s.·\-_/|]+$")
_SPACE_RE = re.compile(r"[\s\u3000]+")

# 技术标签：不在目录名里出现，也不该被当成片名的一部分
TECH_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"\d{3,4}[piPI]|4K|8K|UHD|"
    r"REMUX|BluRay|Blu-?ray|BDRip|BRRip|WEB-?DL|WEB-?Rip|HDTV|DVDRip|HDRip|"
    r"H\.?26[45]|HEVC|AVC|x26[45]|X26[45]|"
    r"TrueHD|Atmos|DTS[\s\-]?(?:HD|X)?|AC3|EAC3|FLAC|AAC|DDP?5\.1|"
    r"HDR\d*|HDR10\+?|DoVi|DV|SDR|IMAX|REPACK|PROPER|EXTENDED|REMASTERED|"
    r"\d{2,3}fps|HQ|HD|SD|MA|COMPLETE|"
    r"\d+声道|国语|國語|粤语|粵語|双语|雙語|中字|中文字幕|简繁|簡繁|特效字幕|内嵌|內嵌|外挂|外掛"
    r")(?![A-Za-z0-9])",
    re.I,
)
# 「年份」的严格判定：前后不能紧贴中文或字母数字 —— 这样 `寒战1994` 里的 1994 不会被当成年份
_YEAR_RE = re.compile(r"(?<![\w\u4e00-\u9fff])((?:19|20)\d{2})(?![\w\u4e00-\u9fff])")
# 集号
_SXXEXX_RE = re.compile(r"[Ss](\d{1,2})[\s._-]?[Ee](?:[Pp])?(\d{1,4})")
_EP_LABEL_RE = re.compile(r"^\s*第\s*\d{1,4}\s*[集话話]\s*[.\-_·]?")
# 尾部压组名
_GROUP_RE = re.compile(r"[-_]([A-Za-z][A-Za-z0-9]{2,15})$")
# 站点标签尾巴
_SITE_TAIL_RE = re.compile(
    r"[.\-_·\s]?[0-9a-zA-Z]{0,8}(?:电影|電影|影视|影視|影院|资源|資源|下载|下載|网盘|網盤|社区|社區|论坛|論壇)$"
)

_PAIRS = {"full": ("（", "）"), "half": ("(", ")")}


def paren_wrap(inner: str, paren: str = "full") -> str:
    left, right = _PAIRS.get(paren, _PAIRS["full"])
    return f"{left}{inner}{right}"


# --------------------------------------------------------------------- 基础清洗
def _has_ad(text: str) -> bool:
    if _HOST_HINT_RE.search(text):
        return True
    return any(w in text for w in _BRACKET_AD_WORDS)


def _strip_bracketed_ad(name: str) -> str:
    """反复剥掉「含网址或广告词」的括号段；不含广告的括号段原样保留。"""
    prev = None
    out = name
    while prev != out:
        prev = out
        out = _BRACKET_RE.sub(lambda m: "" if _has_ad(m.group(0)) else m.group(0), out)
    return out


def _tidy(name: str) -> str:
    s = _SPACE_RE.sub(" ", name)
    s = re.sub(r"\s*([.·])\s*", r"\1", s)
    s = re.sub(r"\s*([\-_])\s*", r"\1", s)
    s = _DUP_SEP_RE.sub(r"\1", s)
    s = _EDGE_RE.sub("", s)
    s = _SPACE_RE.sub(" ", s).strip()
    return s


def clean(name: str) -> str:
    """保守清洗：只删可判定的广告噪声，其余字符语义不动。结果为空则返回原名。"""
    if not name:
        return name
    s = _strip_bracketed_ad(name)
    s = _BARE_URL_RE.sub(" ", s)
    for word in sorted(ADS, key=len, reverse=True):
        s = s.replace(word, " ")
    s = _tidy(s)
    if len(re.sub(r"[.\-_·\s]", "", s)) < 2:
        return name
    return s


def clean_file(filename: str) -> str | None:
    """文件名版的「保守清洗」：**只洗主名，扩展名原样保留**；无变化返回 None。

    为什么不连扩展名一起洗 —— 实测踩过：
      `【更多无水印高品质资源请访问 www.Butailing.com】.MKV`
      整体交给 clean() 会得到 `MKV`（因为它把整段广告当噪声删光了），
      这是个毫无意义的候选名。改成「先拆扩展名、只洗主名」后，主名洗成空 ⇒
      判为**无变化**、不出候选 —— 正是我们要的：这种文件该由人直接删掉，
      而不是被改成怪名字。
    """
    stem, ext = _split_ext(filename)
    cleaned = clean(stem)
    if cleaned == stem:
        return None
    out = f"{cleaned}.{ext}" if ext else cleaned
    return out if out != filename else None


def looks_like_ad(name: str) -> bool:
    """⚠️ **只是提示，不做任何自动动作** —— 名字里有没有广告特征。

    判据就是 `clean()` 用的那两条：① 出现域名/网址 ② 出现广告词（发布页 / 收藏不迷路 / …）。
    红领巾对广告文件的定位很明确（2026-10-05）：
      「很多资源里边会带广告文件，也不好判定是不是广告，**手动删除就可以**」
    ⇒ 这里只用来做界面筛选，绝不自动删。
    """
    return bool(name) and _has_ad(name)


def extract_year(name: str) -> str | None:
    """从名字里挑年份。多个时取**第一个**（影视命名里首个年份通常是发行年）。"""
    m = _YEAR_RE.search(name or "")
    return m.group(1) if m else None


# --------------------------------------------------------------------- 片名
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_SEP_SPLIT_RE = re.compile(r"[.\-_·\s]+")


def title_of(name: str) -> str:
    """从原始名里抽出「片名」—— 这是整理成 `片名（年份）` 的核心。

    步骤：去广告 → 剥掉**所有**括号段（版本/字幕说明不进目录名）→ 去技术标签
    → 去年份 → 去尾部压组与站点标签 → 只留含中文的段（丢弃英文原名）。
    一个中文段都没有时（纯英文片名）保留到第一个技术标签之前。
    """
    s = clean(name)
    if not s:
        return ""
    s = _BRACKET_RE.sub(" ", s)
    s = TECH_RE.sub(" ", s)
    s = _YEAR_RE.sub(" ", s)
    s = _GROUP_RE.sub("", s)
    s = _tidy(s)

    # 反复剥站点标签尾巴
    prev = None
    while prev != s:
        prev = s
        s = _tidy(_SITE_TAIL_RE.sub("", s))

    segs = [seg for seg in _SEP_SPLIT_RE.split(s) if seg]
    if not segs:
        return ""

    cjk_segs = [seg for seg in segs if _CJK_RE.search(seg)]
    keep = cjk_segs if cjk_segs else segs
    # 纯英文片名时，丢掉纯数字段（多半是年份/分辨率残留）
    if not cjk_segs:
        cleaned = [seg for seg in keep if not re.fullmatch(r"\d{1,4}", seg)]
        keep = cleaned or keep

    title = "".join(keep) if cjk_segs else " ".join(keep)
    return _tidy(title) or ""


def dir_name(title: str, year: str | None = None, series: bool = False, paren: str = "full") -> str:
    """拼出目标目录名：`片名（年份）` / `片名（系列）` / `片名`。"""
    title = sanitize_name(title)
    if not title:
        return ""
    if series:
        return f"{title}{paren_wrap('系列', paren)}"
    if year:
        return f"{title}{paren_wrap(year, paren)}"
    return title


def suggest(
    name: str,
    add_year: str | None = None,
    paren: str = "full",
    series: bool = False,
    title_override: str | None = None,
) -> list[dict]:
    """给出目录名候选（供预览页挑选或直接编辑）。

    `add_year` 是「原名里查不到年份」时的兜底（用任务的添加年份）。
    """
    title = (title_override or title_of(name) or clean(name)).strip()
    year_in_name = extract_year(clean(name))
    out: list[dict] = []

    def push(mode: str, label: str, value: str, hint: str = "") -> None:
        if value and all(o["name"] != value for o in out):
            out.append({"mode": mode, "label": label, "name": value, "hint": hint})

    if series:
        push("series", "系列", f"{sanitize_name(title)}{paren_wrap('系列', paren)}", "多部/多季共用片名")
    if year_in_name:
        push("title_year", "片名+年份", dir_name(title, year_in_name, paren=paren), "年份取自原名")
    if add_year and add_year != year_in_name:
        push("title_addyear", "片名+添加年份", dir_name(title, add_year, paren=paren), "原名没有年份，用任务添加年")
    push("title", "只留片名", dir_name(title, None, paren=paren))
    push("keep", "保持原名", name)
    return out


# --------------------------------------------------------------------- 分集文件
def is_episode_file(filename: str) -> bool:
    return bool(_SXXEXX_RE.search(filename or ""))


def _split_ext(filename: str) -> tuple[str, str]:
    """拆出 (主名, 扩展名)。⚠️ 不用 os.path.splitext：像
    `护肝人.6v电影 地址发布页 www.6v123.net` 这种名字会被它切成 `.net 收藏…`。"""
    m = re.search(r"\.([A-Za-z0-9]{1,6})$", filename or "")
    if not m:
        return filename or "", ""
    return filename[: m.start()], m.group(1)


def episode_name(filename: str, series_title: str | None = None) -> str | None:
    """把分集文件名规范成 `剧名.SxxExx.第 N 集.集标题.ext`。

    不是分集文件、或已经是目标形式时返回 None（表示「不需要改」）。
    """
    if not filename:
        return None
    stem, ext = _split_ext(filename)
    m = _SXXEXX_RE.search(stem)
    if not m:
        return None

    season, episode = int(m.group(1)), int(m.group(2))
    head = stem[: m.start()].strip(" .·-_")
    tail = stem[m.end():].strip(" .·-_")

    # 已经带过「第 N 集」就先去重，保证幂等
    tail = _EP_LABEL_RE.sub("", tail).strip(" .·-_")
    # 集标题里若混着发布组 / 站点噪声 / 技术标签，清一下
    #   ⚠️ 技术标签必须在这里也清：`S01E30.1080p.WEB-DL.mkv` 的尾巴全是技术标签，
    #   清完就变空 ⇒ 自动落回「无集标题」形式 `剧名.SxxExx.ext`（与截图真值一致）
    if tail:
        tail = _tidy(
            _SITE_TAIL_RE.sub("", TECH_RE.sub(" ", _strip_bracketed_ad(tail)))
        )
        tail = tail.strip(" .·-_")

    title = (series_title or "").strip() or head
    title = sanitize_name(title)
    if not title:
        title = head

    parts = [f"{title}.S{season:02d}E{episode:02d}"]
    if tail:
        parts.append(f"第 {episode} 集")
        parts.append(tail)
    new = ".".join(parts)
    if ext:
        new = f"{new}.{ext}"
    return None if new == filename else new


def suggest_episode(filename: str, series_title: str | None = None) -> list[dict]:
    """分集文件名的候选（目前只有一档 + 保持原名）。"""
    new = episode_name(filename, series_title)
    out = []
    if new:
        out.append({"mode": "episode", "label": "剧名.SxxExx.第 N 集.集标题", "name": new})
    out.append({"mode": "keep", "label": "保持原名", "name": filename})
    return out


# --------------------------------------------------------------------- 展示信息
_RES_RE = re.compile(r"\b(4320p|2160p|1080p|720p|576p|480p|4K|8K)\b", re.I)
_SRC_RE = re.compile(r"\b(REMUX|BluRay|Blu-?ray|WEB-?DL|WEB-?Rip|HDTV|DVDRip|HDRip)\b", re.I)
_CODEC_RE = re.compile(r"\b(H\.?26[45]|HEVC|AVC|x26[45]|X26[45])\b", re.I)
_AUDIO_RE = re.compile(r"\b(DTS[\s\-]?HD|DTS[\s\-]?X|DTS|TrueHD|Atmos|AC3|EAC3|FLAC|AAC)\b", re.I)


def parse_media(name: str) -> dict:
    """抽出可读的媒体信息，**只用于展示**，不参与改名。"""
    info: dict = {}
    year = extract_year(name)
    if year:
        info["year"] = year
    m = _SXXEXX_RE.search(name or "")
    if m:
        info["season"] = int(m.group(1))
        info["episode"] = int(m.group(2))
    for key, rx in (("resolution", _RES_RE), ("source", _SRC_RE), ("codec", _CODEC_RE), ("audio", _AUDIO_RE)):
        mm = rx.search(name or "")
        if mm:
            info[key] = mm.group(1)
    mm = _GROUP_RE.search(name or "")
    if mm:
        info["group"] = mm.group(1)
    return info


# --------------------------------------------------------------------- 落地校验
# ⚠️ 115 的 fs_mkdir 文档明写：目录名**不能包含** `<` `>` `，`（含中文逗号）。
_FORBIDDEN = str.maketrans({"<": "", ">": "", "，": ","})


def sanitize_name(name: str, limit: int = 200) -> str:
    """清掉 115 不允许的字符与首尾空白/点，并截断到安全长度。"""
    s = (name or "").translate(_FORBIDDEN).strip().strip(" .")
    if len(s) > limit:
        s = s[:limit].rstrip(" .")
    return s


def ensure_ext(new_name: str, ext: str | None) -> str:
    """给**文件**补回扩展名。

    ⚠️ p115client 的 fs_rename 文档原文：「改名时虽然不能修改扩展名，
    但是一定要带上扩展名（无论是啥），不然会把最后一个句点及其之后文字截断」。
    目录没有扩展名 ⇒ 传 ext=None 跳过。
    """
    if not ext:
        return new_name
    suffix = "." + str(ext).lstrip(".")
    if new_name.lower().endswith(suffix.lower()):
        return new_name
    return new_name + suffix
