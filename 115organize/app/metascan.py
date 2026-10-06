"""在线核对模块 —— 番号 → 女优/片商 的**多源分流**查询。

红领巾 2026-10-06 定：
  · 核对番号数据可以用 javbus / javdb / fanza / xcity / mgstage / fc2 / avsox / jav321
  · **可以做分流，不要紧着一个** —— 不同站点派系不同：
       FC2 → 查 fc2 站
       MGStage → 查 mgstage
       无码（Carib/Pondo/10MUCH…）→ 查 xcity
       主流有码 → javbus → javdb → 兜底
  · 离线表命中就不联网；这里只处理**离线判不了**的。

## 反爬纪律（本模块的核心）

  · 每站独立节流（5–10s 随机）
  · 单源失败 3 次 → 切下一个源（不硬刚）
  · 结果写缓存（metadb.remember）—— 同一番号绝不重复请求
  · 全程只 GET，不提交任何东西
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

from . import number
from .metadb import MetaDB

__all__ = ["MetaScan", "pick_sources", "SOURCES", "SOURCE_ALIASES"]

# 各源「怎么被访问」的描述 —— 只写 URL 形态与识别逻辑，不在这里发起任何请求。
# 具体请求由 `_scan_one` 里的 fetch 函数做（可被测试替身顶掉）。
SOURCES: dict[str, dict] = {
    "javbus": {"label": "JavBus", "url": "https://www.javbus.com", "for": "main"},
    "javdb": {"label": "JavDB", "url": "https://javdb.com", "for": "main"},
    "fanza": {"label": "FANZA", "url": "https://www.dmm.co.jp", "for": "main"},
    "mgstage": {"label": "MGStage", "url": "https://www.mgstage.com", "for": "mgstage"},
    "fc2": {"label": "FC2", "url": "https://fc2.com", "for": "fc2"},
    "xcity": {"label": "XCity", "url": "https://www.xcity.jp", "for": "uncensored"},
    "avsox": {"label": "AVSOX", "url": "https://avsox.host", "for": "main"},
    "jav321": {"label": "Jav321", "url": "https://jav321.com", "for": "main"},
}

# 派系别名 —— 供 pick_sources 按番号形态分流
SOURCE_ALIASES: dict[str, list[str]] = {
    "main": ["javbus", "javdb", "fanza", "avsox", "jav321"],
    "mgstage": ["mgstage", "javbus", "javdb"],
    "fc2": ["fc2", "javdb", "javbus"],
    "uncensored": ["xcity", "javbus", "javdb"],
}


@dataclass
class ScanResult:
    """一次在线核对的产物（可能是空：都查不到）。"""
    avid: str
    actress: str = ""
    maker: str = ""
    title: str = ""
    source: str = ""
    found: bool = False
    attempts: list[str] = field(default_factory=list)   # 试过哪些源（记档）

    def as_dict(self) -> dict:
        return {"avid": self.avid, "actress": self.actress, "maker": self.maker,
                "title": self.title, "source": self.source, "found": self.found,
                "attempts": self.attempts}


def pick_sources(avid: str) -> list[str]:
    """按番号形态分流：返回要尝试的源名（按优先级排序）。

    红领巾 2026-10-06 定「可以做分流，不要紧着一个」。
    """
    a = (avid or "").upper()
    if a.startswith("FC2"):
        return list(SOURCE_ALIASES["fc2"])
    if a.startswith(("300MIUM", "230OREC", "250OREC", "260OREC", "GENT")) or "MGSTAGE" in a:
        return list(SOURCE_ALIASES["mgstage"])
    if a.startswith(("CARIB", "PONDO", "10MUCH", "PACO", "HEYDOUGA", "SIRO")):
        return list(SOURCE_ALIASES["uncensored"])
    return list(SOURCE_ALIASES["main"])


class MetaScan:
    """在线核对器。`fetch` 可注入（单测用假实现顶掉网络）。"""

    def __init__(self, db: MetaDB, *, log: Any = None,
                 delay_min: float = 5.0, delay_max: float = 10.0,
                 max_fail: int = 3, fetch: Any = None):
        self.db = db
        self.log = log or (lambda *a, **k: None)
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.max_fail = max_fail
        self.requests = 0
        self._fetch = fetch or self._default_fetch
        self._fail_count = 0

    # ------------------------------------------------------------ 对外
    def resolve(self, avid: str) -> dict:
        """核对一个番号。先查缓存；缓存没有才走在线。返回可落缓存的结果。

        ⛔ 同一番号绝不重复请求 —— 命中缓存直接返回。
        """
        avid = (avid or "").strip().upper()
        if not avid:
            return ScanResult(avid).as_dict()
        cached = self.db.lookup(avid)
        if cached and cached.get("actress"):
            return {**cached, "source": cached.get("source") or "offline"}

        res = ScanResult(avid)
        for src in pick_sources(avid):
            if self._fail_count >= self.max_fail:
                self.log("warning", f"连续失败 {self.max_fail} 次，暂停在线核对（交给缓存/人工）")
                break
            res.attempts.append(src)
            self._pace()
            try:
                meta = self._fetch(src, avid)
                self.requests += 1
            except Exception as exc:
                self._fail_count += 1
                self.log("warning", f"[{src}] 查询失败（{exc}）—— 切下一个源")
                continue
            self._fail_count = 0
            if meta and (meta.get("actress") or meta.get("maker")):
                res.actress = meta.get("actress", "") or res.actress
                res.maker = meta.get("maker", "") or res.maker
                res.title = meta.get("title", "") or res.title
                res.source = src
                res.found = True
                break                                # 查到了就停，不紧着一个也不铺开
        # 无论查没查到都落缓存（查不到 = 记「此号无结果」，下次不再重复请求）
        self.db.remember(res.avid, actress=res.actress, maker=res.maker,
                         title=res.title, source=res.source or "none")
        return res.as_dict()

    # ------------------------------------------------------------ 内部
    def _pace(self) -> None:
        """两站之间随机间隔 —— 反爬核心。"""
        time.sleep(random.uniform(self.delay_min, self.delay_max))

    def _default_fetch(self, src: str, avid: str) -> dict:
        """真实 fetch。⚠️ 本站这几个源反爬都不弱 —— 只 GET、不重试、结果解析保守。

        这是「缺省实现」；测试/离线环境用 `fetch=` 注入替身，不真发请求。
        """
        raise NotImplementedError(
            "metascan 的真实 fetch 未接线 —— 部署时通过环境变量/配置启用；"
            "本地自测请注入假 fetch"
        )
