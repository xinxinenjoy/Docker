"""离线映射 + 查询缓存 —— tidy（整理当前目录）的「先离线、后在线」第一层。

## 设计

红领巾 2026-10-06 定：按女优细化目录 + 核对番号用「离线表 + 在线兜底」。

这里管**两件事**，都跟网络无关：

1. **内置映射表**（硬编码，离线可判）：
   · 番号 → 片商（IPZZ/IPX/IPTD → Idea Pocket）
   · 女优别名（罗马音 / 日文名 → 中文名，中文为辅）
   · 系列 → 片商
2. **查询缓存**（json，落 `DATA_DIR/meta_cache.json`）：
   在线核对一次之后，结果落本地 —— 同一番号绝不重复请求。

## 为什么缓存要单独成模块

在线核对（`metascan.py`）要走 javbus/javdb 这些反爬强的站，请求是稀缺资源。
缓存做得**扁**（dict[avid] → {actress, maker, title, source, at}），
命中直接返回，绝不二次请求。

## 命名口径（红领巾 2026-10-06 定）

· 女优名**以日文名或罗马音为基础，中文为辅** ⇒ 目录名用「日文/罗马音」，中文放别名。
· 有明确主演 ⇒ 归到「相同名下」；查不到 ⇒ 按番号系列分。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

__all__ = ["MetaDB", "load_cache", "save_cache", "BUILTIN_MAKERS", "BUILTIN_SERIES",
           "MAKER_OF", "ACTRESS_ALIASES"]

# ---------------------------------------------------------------------------
# 内置映射表（离线可判的第一层）
# ---------------------------------------------------------------------------

# 番号前缀 → 片商名（英文/罗马音为准，中文作辅助说明）
BUILTIN_MAKERS: dict[str, str] = {
    # Idea Pocket（IPZZ 是它的新系列，IPX/IPTD/IPSD 是老系列）
    "IPZZ": "Idea Pocket",
    "IPX": "Idea Pocket",
    "IPTD": "Idea Pocket",
    "IPSD": "Idea Pocket",
    "IPZ": "Idea Pocket",
    "IPBZ": "Idea Pocket",
    "IPVR": "Idea Pocket",
    # S1（SSNI / SSIS）
    "SSNI": "S1",
    "SSIS": "S1",
    "SNIS": "S1",
    "SIVR": "S1",
    # Moodyz（MIDV / MUKD / MIAD 等）
    "MIDV": "Moodyz",
    "MUKD": "Moodyz",
    "MIAD": "Moodyz",
    "MIDE": "Moodyz",
    "MIAA": "Moodyz",
    "MIMK": "Moodyz",
    "MIGD": "Moodyz",
    "MISM": "Moodyz",
    # SOD Create（STAR / START / SDAB 等）
    "STAR": "SOD Create",
    "START": "SOD Create",
    "SDAB": "SOD Create",
    "SDDE": "SOD Create",
    "SDMF": "SOD Create",
    "SDMM": "SOD Create",
    "SDMU": "SOD Create",
    "SDMT": "SOD Create",
    "SDPD": "SOD Create",
    "SDJS": "SOD Create",
    # FALENO（FLNS / FLKS）
    "FLNS": "FALENO",
    "FLKS": "FALENO",
    # PREMIUM（PRED / PGD）
    "PRED": "Premium",
    "PGD": "Premium",
    # KMP / Million / ケイ・エム・プロデュース
    "MISM": "KMP",
    "MILD": "KMP",
    "MILK": "KMP",
    "RCT": "KMP",
    "TAMA": "KMP",
    "KTB": "KMP",
    "GVH": "KMP",
    # Madonna（JUX / JUL）
    "JUX": "Madonna",
    "JUL": "Madonna",
    "JUY": "Madonna",
    # 无码站
    "FC2": "FC2",
    "HEYDOUGA": "Heydouga",
    "CARIB": "Caribbeancom",
    "PONDO": "1Pondo",
    "PACO": "Pacopacomama",
    "10MUCH": "10musume",
    "SIRO": "Siro",
    "LUXU": "Luxu",
    "MGMQ": "MGS",
    "300MIUM": "MGStage",
    "230OREC": "MGStage",
    "250OREC": "MGStage",
    "260OREC": "MGStage",
    "GENT": "MGStage",
    # 素人 / 其它
    "SUKE": "Sukebomb",
    "EBOD": "Ebody",
    "BKK": "Bkk",
    "BF": "Befree",
    "WANZ": "Wanz Factory",
    "MEYD": "Tameike Goro",
    "MIYO": "Tameike Goro",
    "NACR": "Nadeshiko",
    "HMN": "Honnaka",
    "MDTM": "M's Video Group",
}

# 系列 → 片商（与 BUILTIN_MAKERS 互补，覆盖「同一片商多系列」）
BUILTIN_SERIES: dict[str, str] = {
    # 补充上面没列到的（可按真实盘再扩）
}

# 女优别名：罗马音 → 中文（中文为辅；目录用哪个名由「命名口径」决定）
ACTRESS_ALIASES: dict[str, str] = {
    "aizawa minami": "相泽南",
    "minami aizawa": "相泽南",
    "aizawaminami": "相泽南",
    "kawakita saika": "河北彩花",
    "saika kawakita": "河北彩花",
    "sakura miu": "樱美羽",
    "miu sakura": "樱美羽",
    "ichinose ao": "一之濑青",
    "ao ichinose": "一之濑青",
    "hikari azusa": "小野寺光",
    "azusa hikari": "小野寺光",
    "toda makoto": "户田真琴",
    "makoto toda": "户田真琴",
    "yoshine yuria": "吉根由里亚",
    "yuria yoshine": "吉根由里亚",
    "sakamichi mira": "坂道美空",
    "mira sakamichi": "坂道美空",
    "mikami yua": "三上悠亚",
    "yua mikami": "三上悠亚",
    "sono saki": "园咲",
    "saki sono": "园咲",
}

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def MAKER_OF(avid: str) -> str:
    """从番号前缀推出片商（离线、零请求）。查不到返回空串。"""
    if not avid:
        return ""
    prefix = _prefix_of(avid)
    if prefix in BUILTIN_MAKERS:
        return BUILTIN_MAKERS[prefix]
    if prefix in BUILTIN_SERIES:
        return BUILTIN_SERIES[prefix]
    return ""


def _prefix_of(avid: str) -> str:
    """`IPZZ-137` → `IPZZ`；`FC2-1567975` → `FC2`；`300MIUM-1446` → `300MIUM`。"""
    s = (avid or "").strip().upper()
    if not s:
        return ""
    for sep in ("-", "_"):
        if sep in s:
            return s.split(sep)[0]
    # 无分隔符：数字前的字母段
    i = 0
    while i < len(s) and not s[i].isdigit():
        i += 1
    return s[:i]


# ---------------------------------------------------------------------------
# 缓存（DATA_DIR/meta_cache.json）
# ---------------------------------------------------------------------------

def _cache_path(data_dir: Path) -> Path:
    return Path(data_dir) / "meta_cache.json"


def load_cache(data_dir: Path) -> dict[str, dict]:
    """读查询缓存。坏了/没有都返回空 —— 缓存是可再生的，不为此抛错。"""
    p = _cache_path(data_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cache(data_dir: Path, cache: dict[str, dict]) -> None:
    """原子写缓存（tmp + replace，防写一半断电留半份）。"""
    if not cache:
        return
    p = _cache_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# MetaDB —— tidy 引擎用的查询门面
# ---------------------------------------------------------------------------

class MetaDB:
    """离线表 + 缓存的一站式查询。在线核对（metascan）只负责**填**缓存，这里只**读**。

    `lookup(avid)` 返回：
        {"avid": "IPZZ-137", "actress": "…", "maker": "…", "title": "…",
         "source": "offline|online", "at": epoch}
    查不到关键字段（女优/片商都没有）⇒ 返回 None（调用方决定归哪）。
    """

    def __init__(self, data_dir: Path, log: Any = None):
        self.data_dir = Path(data_dir)
        self.cache = load_cache(self.data_dir)
        self.log = log or (lambda *a, **k: None)

    # ------------------------------------------------------------ 读
    def lookup(self, avid: str) -> dict | None:
        """优先缓存，其次离线表。两样都没有 ⇒ None（交给在线层）。"""
        avid = (avid or "").strip().upper()
        if not avid:
            return None
        got = self.cache.get(avid)
        if got:
            return dict(got)
        # 离线表：片商必给（女优没有时片商也有用，驱动「按系列分」）
        maker = MAKER_OF(avid)
        if maker:
            return {"avid": avid, "actress": "", "maker": maker,
                    "title": "", "source": "offline", "at": 0}
        return None

    def actress_display(self, actress: str) -> str:
        """女优名规范化：优先用**日文/罗马音**；中文别名只在没有日文/罗马音时兜底。

        红领巾 2026-10-06 定：日文名或罗马音为基础，中文为辅。
        """
        if not actress:
            return ""
        key = actress.strip().lower()
        if key in ACTRESS_ALIASES:          # 反向：给罗马音，返回中文（辅助）
            return ACTRESS_ALIASES[key]
        return actress                        # 已经够规范，原样返回

    # ------------------------------------------------------------ 写（在线层调用）
    def remember(self, avid: str, **meta) -> None:
        """把在线核对结果写进缓存。`meta` 可带 actress/maker/title/source。"""
        avid = (avid or "").strip().upper()
        if not avid:
            return
        entry = {
            "avid": avid,
            "actress": meta.get("actress", ""),
            "maker": meta.get("maker", "") or MAKER_OF(avid),
            "title": meta.get("title", ""),
            "source": meta.get("source", "online"),
            "at": int(time.time()),
        }
        self.cache[avid] = entry

    def flush(self) -> None:
        """把内存缓存落盘（tidy 每处理完一批调一次）。"""
        save_cache(self.data_dir, self.cache)
