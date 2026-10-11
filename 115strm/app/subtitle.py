"""外挂字幕 —— 把网盘上**已经有的**字幕真下载到本地，落在 strm 旁边。

## 🔴 先说边界：这一步**不能凭空造出字幕**

`.strm` 里只有一行地址 —— 播放器/媒体服务器**根本不知道网盘上还有字幕这个文件存在**
（Emby 官方答复的原话：*"When you use the STRM file you are pointing ONLY to the media
file so there is no concept of an external sub… download the sub and store that locally
next to the STRM file. It should then work."*）。

所以只有两条路：
  · **网盘上有字幕** ⇒ 本模块把它搬到本地（这就是本模块干的事）
  · **网盘上没有** ⇒ 本模块无能为力，只能靠播放器自带的在线字幕（VidHub 的在线字幕搜索等）

## 🔴 本地该叫什么名 —— 2026-10-11 真机实测定的口径

**本地名 = 视频全名（含它自己的扩展名）+ [语言尾] + 字幕扩展名**

    网盘视频  示例影片 Example Movie.mkv
    网盘字幕  示例影片 Example Movie.srt
    ⇒ 本地落  示例影片 Example Movie.mkv.srt      ← VidHub **默认加载的就是这一份** ✅

实测对照（同一个字幕、同一目录，一次落三份，看 VidHub 认哪个）：

    ✅ `示例影片 Example Movie.mkv.srt`        = 视频全名 + 字幕扩展名
    ❌ `示例影片 Example Movie.srt`            = 照网盘原名（不带视频扩展名）
    ❌ `示例影片 Example Movie.mkv.strm.srt`   = 把 `.strm` 也算进视频名

⚠️ 为什么不是「照网盘原名原样落」：媒体服务器把 `xxx.mkv.strm` 这个条目**当成 `xxx.mkv`**，
   所以字幕必须挂到 `xxx.mkv` 这个名字上，而不是 `xxx` 或 `xxx.mkv.strm`。
   这条与 Emby / Jellyfin 的官方命名规范一致（Jellyfin：`Movie.mkv.zh.srt`）。

## 配对规则（只下能配上的；配不上的**只报告、不猜**）

    ① 同干          `a.mkv`   ↔ `a.srt`          ⇒ 本地 `a.mkv.srt`
    ② 同干+语言尾    `a.mkv`   ↔ `a.zh.srt`       ⇒ 本地 `a.mkv.zh.srt`
    ③ 带视频扩展名   `a.mkv`   ↔ `a.mkv.zh.ass`   ⇒ 本地 `a.mkv.zh.ass`（本来就合规）
    ④ 仅前缀匹配     `a.mkv`   ↔ `a 精修简体中字.sup` ⇒ **不下**，进报告
    ⑤ 配不上        `Some.Film.2.2025….mkv` ↔ `完全不同的名字 精修简体中字 V2.sup` ⇒ **不下**，进报告

⛔ ④⑤ 一律不猜：**配错字幕比没字幕更糟**（时间轴对不上、甚至根本不是这部片）。
   它们进回执，由人决定要不要手工改名。

## 请求量

下载走的是**和视频播放同一条链**（alist `/d` → 302 → 115 CDN），1 请求 / 文件。
⭐ **只有首次付这个成本**：下载完就存在本地，之后每轮只做「文件在不在」判别，**零请求**
（和 strm 的「内容没变就不写」同一套口径）。
"""
from __future__ import annotations

import hashlib
import time
import urllib.request
from dataclasses import dataclass

__all__ = [
    "SUB_EXT_DEFAULT", "LANG_TAGS", "SubFile", "parse_exts", "is_subtitle",
    "classify", "plan_dir", "fetch_bytes", "sha1_of", "UA",
]

# 字幕类扩展名（出厂白名单，可在设置里改）
SUB_EXT_DEFAULT = "ass,srt,ssa,sup,sub,idx,vtt"

# ------------------------------------------------------------------ 语言尾
# 只影响**归类**（决定本地名里要不要保留这一段），不影响下载与否。
#
# ⚠️ 判据是「字幕干 = 视频干 + `.` + 语言」**且两边完全相等**，所以表**宁滥勿缺** ——
#    多列一个词的代价只是「本来配不上的也算配得上」，而那个方向是安全的
#    （本地名依然是 `视频全名.该词.扩展名`，播放器照样认）。
#
# 🔴 2026-10-11 真机踩到：原来只列了十几个常见语言，结果本盘 14 个「仅前缀」的字幕里
#    **12 个其实是语言尾**，只是语种冷门 —— `asm`(阿萨姆语) `nld`(荷兰语) `ind`(印尼语)
#    `glv`(曼岛语) `mya`(缅甸语) `pan`(旁遮普语) `bos`(波斯尼亚语)……
#    ⇒ 补全 **ISO 639-1（2 字母）+ ISO 639-2/B（3 字母）**（与 Jellyfin/Plex 同一套口径），
#      再加中文圈常见的写法。
_ISO = """
aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu cv cy
da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu
hy hz ia id ie ig ii ik io is it iu ja jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb
lg li ln lo lt lu lv mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om
or os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw
ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo za zh zu
aar abk ace ach ada ady afr ain akk alb sqi ale alt amh ang anp ara arc arg arm hye arn arp
asm ast ava ave awa aym aze bak bal bam ban baq eus bas bel bem ben bho bik bin bis bos bra
bre bua bug bul bur mya byn cad car cat ceb ces cze cha chb che chg chi zho chk chm chn cho
chr chu chv chy cop cor cos cpe cpf cpp cre crh csb cus cym wel dak dan dar day del den dgr
din div doi dra dsb dua dum dut nld dyu dzo efi egy eka ell gre elx enm epo est eus ewe ewo
fan fao fat fij fil fin fon fra fre frm fro frr frs fry ful fur gaa gay gba geo kat ger deu
gez gil gla gle glg glv gmh goh gon gor got grb grc grn gsw guj gwi hai hat hau haw heb her
hil him hin hit hmn hmo hrv hsb hun hup iba ibo ice isl ido iii ijo iku ile ilo ina inc ind
ine inh ipk ira iro ita jav jbo jpn jpr jrb kaa kab kac kal kam kan kar kas kau kaw kaz kbd
kha khi khm kho kik kin kir kmb kok kom kon kor kos kpe krc krl kro kru kua kum kur kut lad
lah lam lao lat lav lez lim lin lit lol loz ltz lua lub lug lui lun luo lus mac mkd mad mag
mah mai mak mal man mao mri map mar mas may msa mdf mdr men mga mic min mis mkh mln mno moh
mon mos mul mun mus mwl mwr myn myv nah nai nap nau nav nbl nde ndo nds nep new nia nic niu
nno nob nog non nor nqo nso nub nwc nya nym nyn nyo nzi oci oji ori orm osa oss ota oto paa
pag pal pam pan pap pau peo per fas phi phn pli pol pon por pra pro pus que raj rap rar roa
roh rom ron rum run rup rus sad sag sah sai sal sam san sas sat scn sco sel sem sga sgn shn
sid sin sio sit sla slo slk slv sma sme smi smj smn sms sna snd snk sog som son sot spa srd
srn srp srr ssa ssw suk sun sus sux swa swe syc syr tah tai tam tat tel tem ter tet tgk tgl
tha tib bod tig tir tiv tkl tlh tli tmh tog ton tpi tsi tsn tso tuk tum tup tur tut tvl twi
tyv udm uga uig ukr umb und urd uzb vai ven vie vol vot wak wal war was wel wen wln wol xal
xho yao yap yid yor ypk zaa zap zbl zen zha znd zul zun zxx zza
# 🔴 2026-10-11：上面那串是手敲的，**漏掉过 `eng`**（英语的 639-2 码）——
#    症状极隐蔽：本盘 28 个 `.eng.sub` / `.eng.idx` 全掉进「配不上」，而英语字幕是最常见的。
#    ⇒ 这里再补一份「字幕名里最常出现的写法」，与上面是**并集**（重复无害），
#      并配一条单测钉住关键码（`tests/test_subtitle.py::test_关键语言码没漏`）。
eng chi zho chs cht zhcn zhhans zh-hans zh-hant zh-tw zh-hk zh-cn sc tc gb big5 jp kr
jpn kor fre fra ger deu spa ita rus por tha vie ara hin tur pol dut nld swe nor dan fin
cze ces hun gre ell heb ind may msa ukr ron rum bul srp hrv slv slk cat glg eus baq
cym wel gle gla isl ice lav lit est fil tgl ben tam tel mal kan mar guj pan nep sin
mya bur khm lao mon amh swa som zul xho afr epo lat sqi alb hye arm kat geo aze kaz uzb
kir tgk mkd mac bos hbs nob nno
简 繁 简体 繁体 中文 中字 中英 中字 双语 粤语 粤语官中 官中 国语 国粤 台配 港配 简中 繁中
英文 英字 日文 日字 韩文 特效
"""
LANG_TAGS = frozenset(_ISO.split())

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def parse_exts(raw: str) -> frozenset[str]:
    """`"ass,srt"` → `{"ass","srt"}`（小写、去点、去空白）。"""
    return frozenset(e.strip().lower().lstrip(".") for e in (raw or "").split(",") if e.strip())


def is_subtitle(name: str, exts: frozenset[str]) -> bool:
    if "." not in name:
        return False
    return name.rsplit(".", 1)[-1].lower() in exts


def sha1_of(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


@dataclass(frozen=True)
class SubFile:
    """一个待下载的字幕。"""
    rel: str                 # 网盘上字幕的相对路径（用来算下载 URL）
    local_rel: str           # 本地落点的相对路径（**与 strm 同目录**，名字按上面的口径）
    url: str                 # 下载地址（与视频同一条链）
    video: str               # 对应的视频全名（含扩展名）
    kind: str                # 配对类别（①同干 / ②语言尾 / ③带视频扩展名）
    lang: str = ""           # 语言尾（没有就是空）


def classify(video: str, sub: str, lang_tags: frozenset[str] = LANG_TAGS):
    """把「一个字幕」配到「一个视频」上。

    返回 `(本地应落的名字, 类别, 语言尾)`；配不上返回 `(None, "⑤配不上", "")`。
    ⚠️ **纯函数** —— 不碰磁盘、不发请求，测试直接调它。
    """
    if "." not in video or "." not in sub:
        return None, "⑤配不上", ""
    v_stem = video.rsplit(".", 1)[0]
    s_stem, s_ext = sub.rsplit(".", 1)

    # ① 同干：`a.mkv` ↔ `a.srt`
    if s_stem == v_stem:
        return f"{video}.{s_ext}", "①同干", ""

    # ③ 无语言尾、但字幕干就是**视频全名**：`a.mkv` ↔ `a.mkv.ass`
    #    ⚠️ **必须排在语言判定之前**：视频扩展名本身就是 2/3 字母 ISO 码的情形真实存在
    #       （`ts` 是聪加语、`as` 是阿萨姆语……）。先走语言判定的话，
    #       `a.ts` 配 `a.ts.srt` 会被当成「语言尾 ts」⇒ 拼出 `a.ts.ts.srt`（重复一层）。
    if s_stem == video:
        return sub, "③带视频扩展名", ""

    # ② / ③ 带语言尾：`a.zh.srt`、`a.mkv.zh.ass`
    parts = s_stem.split(".")
    if len(parts) >= 2 and parts[-1].lower() in lang_tags:
        lang = parts[-1]
        base = ".".join(parts[:-1])
        if base == v_stem:
            return f"{video}.{lang}.{s_ext}", "②语言尾", lang
        if base == video:
            # → 字幕本来就叫 `视频全名.语言.扩展名`，已经合规，原样落
            return sub, "③带视频扩展名", lang

    return None, "⑤配不上", ""


def plan_dir(rel_dir: str, videos: list[str], subs: list[str], *,
             builder, exts: frozenset[str], lang_tags=frozenset(LANG_TAGS),
             skip_exts: frozenset[str] = frozenset()):
    """把一个目录里的字幕配到视频上。

    返回 `(matched, unmatched, skipped)`：
      · `matched`   —— `list[SubFile]`，**可以下载**的
      · `unmatched` —— `list[(视频名或空, 字幕名, 类别)]`，**只进报告、不下载**
      · `skipped`   —— `list[(字幕名, 原因)]`，被设置挡掉的（如 `.sup` 体积大）

    ⚠️ 同一目录里**多个视频**时，同写字幕只认「名字完全对得上」的那一个 ——
       前缀匹配（④）一律不猜，宁可漏配也不错配。
    """
    matched: list[SubFile] = []
    unmatched: list[tuple[str, str, str]] = []
    skipped: list[tuple[str, str]] = []
    if not videos:
        # 这个目录里一个视频都没有 ⇒ 没有 strm 可挂，怎么配都没意义
        return matched, unmatched, skipped

    # 按扩展名分组做「同干」快查表
    v_by_stem = {v.rsplit(".", 1)[0]: v for v in videos}
    # ⚠️ 从**名字最长**的视频开始试：同目录里若有 `a.mkv` 与 `a.4k.mkv` 这种
    #    「一个名字正好是另一个的前缀」的情况，先匹配**更具体**的那个才不会配错。
    ordered = sorted(videos, key=lambda v: (-len(v.rsplit(".", 1)[0]), v))

    for s in subs:
        if not is_subtitle(s, exts):
            continue
        s_ext = s.rsplit(".", 1)[-1].lower()

        # ⚠️ 配对**先做、跳过后做**：`skip_exts`（默认 `.sup`）只挡「**配上了**但不值得下」的，
        #    挡不住「本来就配不上」的 —— 后者要照样进回执，否则那批会**静默消失**
        #    （本盘电影库 3 个配不上的恰好全是 `.sup`，正是这种情形）。

        # 拿目录里每个视频都试一遍 classify（大多数目录只有一个视频，成本可忽略）
        hit = None
        for v in ordered:
            local, kind, lang = classify(v, s, lang_tags)
            if local:
                hit = (v, local, kind, lang)
                break
        # 同干快查（覆盖 video 无扩展名/异常命名的情况）
        if hit is None:
            v = v_by_stem.get(s.rsplit(".", 1)[0])
            if v:
                hit = (v, s, "①同干", "")

        if hit is None:
            # ④/⑤ 区分：能前缀对上就算 ④（有希望，人工看一眼），否则 ⑤
            s_stem = s.rsplit(".", 1)[0]
            near = [v for v in ordered
                    if s_stem.startswith(v.rsplit(".", 1)[0]) or v.rsplit(".", 1)[0].startswith(s_stem)]
            unmatched.append((near[0] if near else "", s, "④仅前缀" if near else "⑤配不上"))
            continue

        if s_ext in skip_exts:
            skipped.append((s, f".{s_ext} 已在设置里跳过（体积大）"))
            continue

        v, local, kind, lang = hit
        rel = f"{rel_dir}/{s}" if rel_dir else s
        matched.append(SubFile(
            rel=rel,
            local_rel=f"{rel_dir}/{local}" if rel_dir else local,
            url=builder.url(rel),
            video=v, kind=kind, lang=lang,
        ))

    return matched, unmatched, skipped


def fetch_bytes(url: str, *, timeout: float = 60.0, retries: int = 2,
                ua: str = UA, sleeper=time.sleep) -> bytes:
    """下载一个文件 —— 走的是和视频播放**同一条链**（alist `/d` → 302 → CDN）。

    ⚠️ 必须带一个正常的 `User-Agent`：115 CDN 会把请求用的 UA **签进直链**，
       换个 UA 去取同一个链会被拒。播放器用的就是浏览器的 UA，这里照抄。
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as exc:                      # noqa: BLE001
            last = exc
            if attempt < retries:
                sleeper(1.5 * (attempt + 1))
    raise last if last else RuntimeError("下载失败")
