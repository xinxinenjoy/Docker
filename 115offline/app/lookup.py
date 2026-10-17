"""联网核对片名 —— **按需、单条、只读**。

红领巾 2026-10-17 定的口径：
  「不用批量，只针对名字很乱需要改名的文件，**点击进行查询**，然后返回建议名字」
⇒ 这是个「查库」而不是「自动改名」：结果只当建议，要不要用由人点。

数据源（按顺序，前一个没结果才用下一个）：
  · **TMDB**（要有 token；群晖能直连）—— 结构化最全：`title/name` + 年份 + `movie|tv` + 原名，
    中文标题走 `language=zh-CN`。**有 token 就优先用它**。
  · **豆瓣 `subject_suggest`** —— 零配置兜底。直接回 `title` / `year` / `type` / 别名。
  · **Bing 网页搜索** —— 最后兜底，只拿得到**页面标题**（没有年份字段），
    所以标明 `type=web`，界面上要当成「线索」而不是「答案」。

⚠️ 两条实现坑（2026-10-17 实测）：
  ① 豆瓣对**空 UA / 脚本 UA** 直接 403 ⇒ 必须带浏览器 UA + `Accept-Language: zh-CN`。
  ② 豆瓣有**频率限制**（连续猛查会被限流）⇒ 调用方（server.py）带本地缓存，同词 7 天内不再打。
⚠️ 这里**绝不碰 115**，也不产生任何写操作 —— 纯查询。
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 8
_DOUBAN = "https://movie.douban.com/j/subject_suggest?q="
_BING = "https://cn.bing.com/search?q="
_TAG = re.compile(r"<[^>]+>")


def _get(url: str) -> str:
    req = Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 —— 域名写死，不接受外部传入
        return resp.read().decode("utf-8", "ignore")


def _get_json(url: str, headers: dict[str, str]) -> Any:
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json", **headers})
    with urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", "ignore") or "{}")


def tmdb(query: str, token: str, language: str = "zh-CN") -> list[dict]:
    """TMDB `search/multi`（电影 + 剧集一起查）。

    `token` 两种都认：v4 读令牌（`eyJ…` 开头，走 Bearer 头）与 v3 API Key
    （32 位十六进制，走 `api_key` 参数）—— 红领巾 2026-10-17 把两样都给了。
    """
    token = (token or "").strip()
    if not token:
        return []
    params = {"query": query, "language": language, "include_adult": "false"}
    headers: dict[str, str] = {}
    if token.startswith("eyJ"):          # v4 读令牌
        headers["Authorization"] = f"Bearer {token}"
    else:                                 # v3 API Key
        params["api_key"] = token
    data = _get_json("https://api.themoviedb.org/3/search/multi?" + urlencode(params), headers)
    out: list[dict] = []
    for it in (data.get("results") or []):
        if not isinstance(it, dict):
            continue
        media = it.get("media_type")
        if media not in ("movie", "tv"):
            continue
        title = (it.get("title") or it.get("name") or "").strip()
        if not title:
            continue
        date = (it.get("release_date") or it.get("first_air_date") or "").strip()
        out.append({
            "title": title,
            "year": date[:4] if len(date) >= 4 else "",
            "alias": (it.get("original_title") or it.get("original_name") or "").strip(),
            "type": media,               # movie | tv
            "id": str(it.get("id") or ""),
            "url": f"https://www.themoviedb.org/{media}/{it.get('id')}" if it.get("id") else "",
            "source": "tmdb",
        })
    return out


def douban(query: str) -> list[dict]:
    """豆瓣联想（结构化）。拿不到就抛异常，由 search() 兜底。"""
    raw = _get(_DOUBAN + quote(query))
    data = json.loads(raw or "[]")
    out: list[dict] = []
    for it in (data if isinstance(data, list) else []):
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "title": title,
            "year": str(it.get("year") or "").strip(),
            "alias": str(it.get("sub_title") or "").strip(),
            "type": str(it.get("type") or "").strip(),
            "id": str(it.get("id") or ""),
            "url": str(it.get("url") or ""),
            "source": "douban",
        })
    return out


def bing(query: str, limit: int = 6) -> list[dict]:
    """Bing 网页搜索兜底：只抽 <h2> 里的标题（**没有年份**，只能当线索）。"""
    raw = _get(_BING + quote(query + " 电影 豆瓣"))
    out: list[dict] = []
    for chunk in re.findall(r"<h2><a[^>]*>(.*?)</a></h2>", raw, re.S)[:limit]:
        title = _TAG.sub("", chunk).strip()
        if title:
            out.append({"title": title, "year": "", "alias": "", "type": "web",
                        "id": "", "url": "", "source": "bing"})
    return out


def search(query: str, tmdb_token: str = "") -> dict:
    """查一次。返回 `{ok, source, items, error/note}` —— 不抛异常（调用方只管展示）。

    顺序：TMDB（有 token 就先问它）→ 豆瓣 → Bing。前一个有结果就停，
    这样大部分情况拿到的是结构化的「片名 + 年份 + 电影/剧集」。
    """
    q = (query or "").strip()
    if not q:
        return {"ok": False, "source": "", "items": [], "error": "查询词为空"}
    notes: list[str] = []
    if tmdb_token:
        try:
            items = tmdb(q, tmdb_token)
            if items:
                return {"ok": True, "source": "tmdb", "items": items[:8]}
            notes.append("TMDB 没有匹配")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"TMDB 查询失败：{exc}")
    try:
        items = douban(q)
        if items:
            return {"ok": True, "source": "douban", "items": items[:8],
                    "note": "；".join(notes)}
        notes.append("豆瓣没有匹配")
    except Exception as exc:  # noqa: BLE001 —— 网络/解析失败都要如实回报给界面
        notes.append(f"豆瓣查询失败：{exc}")
    try:
        items = bing(q)
        if items:
            return {"ok": True, "source": "bing", "items": items,
                    "note": "；".join(notes) + "；下面是搜索引擎的页面标题（没有年份，只能当线索）"}
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Bing 也失败：{exc}")
    return {"ok": False, "source": "", "items": [], "error": "；".join(notes)}
