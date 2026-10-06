"""垃圾文件**分级**判定 —— 只对「强特征」动手，白名单永不碰，灰区只进报告。

红领巾 2026-10-06 拍板「分级处理」，理由是他 2026-10-05 的原话：
「很多资源里边会带广告文件，**也不好判定是不是广告**，手动删除就可以」

所以这里的设计原则只有一条：**判不准就不动**。三级：

    KEEP     白名单 —— 连报告都不用生成「建议删除」。正规封面 / 字幕 / nfo / 主体文件都在这。
    STRONG   强特征 —— 可以自动处理（隔离或删除，由 JUNK_ACTION 决定）
    SUSPECT  灰区   —— **只报告**，等人看

⚠️ 为什么必须有 KEEP，而不是「命中广告特征就删」：
   拿本盘真实数据实测，粗规则会把下面这些一并判成垃圾 ——
     · `4k688.com@DLDSS-532.mp4`      ← 带域名，但它就是**正片**（番号在名字里）
     · `madoubt.com 982886.xyz HMN-870` ← 一串域名，但它是**番号目录**
     · `screens.jpg`(120 次) / `poster.jpg`(119 次) / `1.jpg`(117 次) ← **正规封面**
   所以判定顺序是 **KEEP → STRONG → SUSPECT**，白名单先短路。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import number
from .config import KEEP_EXT, KEEP_IMAGES

__all__ = ["Verdict", "judge", "split_ext", "LEVEL_KEEP", "LEVEL_STRONG", "LEVEL_SUSPECT", "AD_WORDS"]

LEVEL_KEEP = "keep"
LEVEL_STRONG = "strong"
LEVEL_SUSPECT = "suspect"

# 视频扩展名（正片候选）—— 与 namer.VIDEO_EXT 保持一致，另加本盘实测出现的 `.hd` / `.ktr`
VIDEO_EXT = frozenset((
    "mp4", "mkv", "avi", "ts", "m2ts", "mov", "wmv", "flv", "rmvb", "rm",
    "webm", "mpg", "mpeg", "m4v", "3gp", "iso", "vob", "f4v", "asf", "hd", "ktr",
))
# 压缩包 —— 多半是「字幕包 / 图片包」，不动
ARCHIVE_EXT = frozenset(("zip", "rar", "7z"))


@dataclass
class Verdict:
    level: str
    reason: str = ""
    auto: bool = False          # 是否可以自动处理
    def __bool__(self) -> bool:
        return self.level != LEVEL_KEEP


# --------------------------------------------------------------- 强特征词表
# ⚠️ 词表**只收「推广/导航」词汇，绝不收「内容」词汇** —— 这是本盘实测得出的教训：
#    第一版把 `porn` / `撸` / `脱衣` / `约妹` 也收了进来，结果 2194 条被误判成广告，
#    其中就有 `(大鸟十八Bigbird18)撸先生和玲的射4分钟.mp4`、`Larkin Love BallBustingPornstars.com…`
#    —— 这些是**正片**，标题里出现这些字眼再正常不过。
#    判据：一个词若可能是**作品标题**的一部分，它就不能单独当广告证据。
#
# 分两档（档次决定「要不要再叠加重复次数」）：
#   PROMO_ALWAYS —— 出现即可判（这些话在作品名里不可能出现）
#   PROMO_WEAK   —— **必须叠加「跨目录重复 ≥JUNK_DUP_MIN」**才判（如「直播」，
#                   单独看太危险，但本盘那批 187/119/110 次的推广视频全都能被这一条兜住）
PROMO_ALWAYS = (
    # 站点导航 / 找回
    "收藏不迷路", "不迷路", "最新位址", "最新地址", "最新网址", "最新網址",
    "地址發布", "地址发布", "地址发布页", "防走失", "請牢記", "请牢记",
    "永久域名", "永久地址", "备用网址", "備用網址", "找回密码", "找回網址",
    # 引流 / 拉新
    "注册免费", "註冊免費", "免费体验", "免費體驗", "免费领取", "免費領取",
    "加群", "进群", "進群", "扫码", "掃碼", "公众号", "公眾號", "关注微信",
    "下载app", "下載APP", "下载安装", "点击下载", "立即下载",
    # 博彩 / 游戏推广
    "游戏大全", "遊戲大全", "游戏盒子", "遊戲盒子", "游戏官网", "遊戲官網",
    "礼包", "禮包", "代币", "代幣", "兑换码", "兌換碼", "签到领", "簽到領",
    "体育电竞", "體育電競", "电竞直播", "電競直播", "世足", "英超官方",
    # 成人引流 App（与「成人内容」区分：这些是**拉你去装 App**的话术）
    "成人手游", "成人手遊", "成人app", "成人APP", "成人游戏", "成人遊戲",
    "成人短剧", "成人短劇", "成人ai", "成人AI", "无限免费看", "無限免費看",
    # 站点水印 / 论坛招牌
    "高清影视之家", "影视之家", "影視之家", "不太灵", "村花论坛", "村花論壇",
    "聚合", "社群最新", "社區最新", "社区最新", "文宣",
)
# 危险词（可能是正经标题）—— 只在「跨目录重复」达标时才判
PROMO_WEAK = (
    "直播", "現場", "现场", "無料", "无料", "免費", "免费",
    "颜射", "顏射", "换脸", "換臉", "脱衣", "脫衣",
)
_AD_ALWAYS_RE = re.compile("|".join(re.escape(w) for w in sorted(PROMO_ALWAYS, key=len, reverse=True)), re.I)
_AD_WEAK_RE = re.compile("|".join(re.escape(w) for w in sorted(PROMO_WEAK, key=len, reverse=True)), re.I)
# `水印` 单独当证据会误伤：`【無水印原版】` 是**画质声明**，不是广告。
# 只有「加水印 / 带水印」这种才是。所以用否定后顾，把「无水印」排除掉。
_WATERMARK_RE = re.compile(r"(?<![无無])水印")
AD_WORDS = PROMO_ALWAYS + PROMO_WEAK

# 白名单文件名（正规封面 / 缩略图 / 说明）—— 精确匹配（忽略大小写）
KEEP_NAMES = frozenset((
    "screens.jpg", "screens.jpeg", "screens.png",
    "poster.jpg", "poster.jpeg", "poster.png",
    "cover.jpg", "cover.jpeg", "cover.png",
    "folder.jpg", "folder.jpeg", "folder.png",
    "fanart.jpg", "fanart.jpeg", "fanart.png",
    "banner.jpg", "banner.png", "thumb.jpg", "thumb.png",
    "backdrop.jpg", "landscape.jpg", "logo.png", "clearart.png",
    "readme.txt", "readme.md", "info.txt", "说明.txt", "nfo",
))

_HOST_RE = re.compile(
    r"(?:https?://|www\.)|[0-9A-Za-z\-]{2,}\.(?:com|net|org|cn|cc|tv|me|xyz|top|vip|info|io|la|pw|biz|fun|app|live|online|site)\b",
    re.I,
)
_QUN_RE = re.compile(
    # ⚠️ 不能收「飞机 / 飛機」—— 实测 `1 宅男打飞机推荐…` 被误判成引流件。
    r"(?:加群|进群|進群|扫码|掃碼|公众号|公眾號|QQ群|Telegram|电报|電報|telegram)"
)
_EXT_RE = re.compile(r"\.([A-Za-z0-9]{1,6})$")

# ⚠️ 只认**已知扩展名**，别拿 `.com` / `.1080p` 当扩展名。
#    实测踩过：`! Heyzo-0768 [FHD-1080p] By …@NongPink.CoM` 被拆成主名 `…@NongPink` + 扩展名 `com`，
#    于是「移动并改名」会把它改成 `HEYZO-0768.com` —— 把一个视频文件改成伪域名后缀。
#    更危险的是 115 的 `fs_rename`：**传进去的名字必须带扩展名**，带错等于把原后缀截断。
KNOWN_EXT = frozenset(
    tuple(VIDEO_EXT) + tuple(KEEP_EXT) + tuple(ARCHIVE_EXT)
    + ("txt", "html", "htm", "url", "sfv", "par2", "md5", "cue", "log",
       "doc", "docx", "xls", "xlsx", "pdf", "torrent", "dat", "bin", "md", "json", "xml")
)
_PART_RE = re.compile(r"^(?:[rs]\d{2}|z\d{2}|0\d{2})$", re.I)     # 分卷包 r00 / r01 / 001
# 整个主名就是一个域名（可能被空格拆开，如 `x u u 6 2 . c o m`）—— 这是最干净的广告特征
_PURE_HOST_RE = re.compile(
    r"(?:(?:https?://|www\.)?[0-9A-Za-z\-]{1,}\.(?:com|net|org|cn|cc|tv|me|xyz|top|vip|info|io|la|pw|biz|fun|app|live|online|site)\.?)+",
    re.I,
)


def _split(fname: str) -> tuple[str, str]:
    """拆 (主名, 扩展名小写)。不是已知扩展名 ⇒ **扩展名算空**，整名都是主名。"""
    m = _EXT_RE.search(fname or "")
    if not m:
        return fname or "", ""
    ext = m.group(1).lower()
    if ext in KNOWN_EXT or _PART_RE.match(ext):
        return fname[: m.start()], ext
    return fname or "", ""


def split_ext(fname: str) -> tuple[str, str]:
    """拆 (主名, 扩展名小写)。⚠️ 不用 os.path.splitext：像
    `护肝人.6v电影 地址发布页 www.6v123.net` 这种名字会被它切成 `.net 收藏…`。"""
    return _split(fname)


def _parent_id_matches(stem: str, parent_name: str) -> bool:
    """文件主体名是否就是「父目录的那个番号」—— 同番号的文件一律算主体。"""
    if not parent_name:
        return False
    pid = number.get_id(parent_name)
    if not pid:
        return False
    fid = number.get_id(stem)
    if fid:
        return fid == pid
    # `DLDSS-532-1.mp4` / `DLDSS-532 中文字幕.mp4` —— 番号开头、后面接尾巴
    key = number.format_id(pid)
    norm = number.normalize_name(stem)
    return bool(norm) and (norm.startswith(key.replace("-", "")) or norm.startswith(key))


def judge(fname: str, *, dup_count: int = 1, dup_min: int = 5, parent_name: str = "") -> Verdict:
    """判一个文件名。`dup_count` = 同一文件名在**其它目录**出现过的次数（跨目录重复）。"""
    name = (fname or "").strip()
    if not name:
        return Verdict(LEVEL_KEEP, "空名")
    stem, ext = _split(name)
    low = name.lower()

    # ---------------------------------------------------------------- ① 白名单
    if low in KEEP_NAMES:
        return Verdict(LEVEL_KEEP, "白名单文件名")
    if ext in KEEP_EXT:
        if ext in ("jpg", "jpeg", "png", "webp", "bmp", "gif") and not KEEP_IMAGES:
            pass
        else:
            return Verdict(LEVEL_KEEP, f"白名单扩展名 .{ext}")
    if ext in ("sfv", "par2", "md5", "cue", "log", "torrent") or _PART_RE.match(ext):
        return Verdict(LEVEL_KEEP, f"配套件 .{ext}")
    # 主体：名字里带父目录的番号，或自己能判出番号（多数正片就是 `域名@番号.mp4` 这种）
    if parent_name and number.get_id(parent_name) and _parent_id_matches(stem, parent_name):
        return Verdict(LEVEL_KEEP, "父目录番号主体")
    if number.get_id(stem) and ext in VIDEO_EXT:
        return Verdict(LEVEL_KEEP, "名字即番号的正片")
    if number.kind_of(name) in (number.Kind.MOVIE, number.Kind.TV) and ext in VIDEO_EXT:
        return Verdict(LEVEL_KEEP, "影视正片")
    if ext in ARCHIVE_EXT:
        return Verdict(LEVEL_KEEP, f"压缩包 .{ext}")

    flat = re.sub(r"\s+", "", name)
    flat_stem = re.sub(r"\s+", "", stem)
    has_host = bool(_HOST_RE.search(flat))
    has_always = bool(_AD_ALWAYS_RE.search(name)) or bool(_WATERMARK_RE.search(name))
    has_weak = bool(_AD_WEAK_RE.search(name))
    has_qun = bool(_QUN_RE.search(name))
    repeated = dup_count >= dup_min

    # ---------------------------------------------------------------- ② 强特征
    # `.url` / `.html` —— 本盘实测 177 + 127 个，无一例外是推广页 / 导航
    if ext in ("url", "html", "htm"):
        return Verdict(LEVEL_STRONG, f".{ext} 推广页", auto=True)
    if has_always:
        return Verdict(LEVEL_STRONG, "推广话术", auto=True)
    if has_qun:
        return Verdict(LEVEL_STRONG, "引流话术", auto=True)
    # 整个主名就是个域名 ⇒ `manko.fun.mp4` / `x u u 6 2 . c o m.mp4` / `StraplessDildo.com`
    # ⚠️ 只认「**整名**是域名」，不能靠「剥完变短」—— 实测 `fc2-ppv-1567975-nyap2p.com.mp4`
    #    和 `【每日更新606dvd.com】50老妇.avi` 都会因剥离而「看起来只剩域名」，其实都是正片。
    if flat_stem and _PURE_HOST_RE.fullmatch(flat_stem):
        return Verdict(LEVEL_STRONG, "整名即域名", auto=True)
    if has_host and number.has_cjk(name) is False and not number.normalize_name(stem):
        return Verdict(LEVEL_STRONG, "只剩域名", auto=True)
    if has_host and repeated:
        return Verdict(LEVEL_STRONG, f"域名+跨目录重复×{dup_count}", auto=True)
    if has_weak and repeated:
        return Verdict(LEVEL_STRONG, f"可疑话术+跨目录重复×{dup_count}", auto=True)

    # ---------------------------------------------------------------- ③ 灰区
    if not ext:
        return Verdict(LEVEL_SUSPECT, "无扩展名")
    if ext == "txt":
        return Verdict(LEVEL_SUSPECT, ".txt 但无广告特征")
    if ext in VIDEO_EXT:
        if repeated:
            return Verdict(LEVEL_SUSPECT, f"跨目录重复×{dup_count}（视频，无广告特征）")
        return Verdict(LEVEL_KEEP, "视频（无广告特征）")
    if ext in ("jpg", "jpeg", "png", "webp", "bmp", "gif"):
        if repeated:
            return Verdict(LEVEL_SUSPECT, f"跨目录重复×{dup_count}（图片）")
        return Verdict(LEVEL_KEEP, "图片")
    return Verdict(LEVEL_SUSPECT, f"待定扩展名 .{ext}")
