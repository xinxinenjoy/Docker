"""115 操作封装 —— 所有请求都从这里出去，**必须**经过节流器。

为什么集中到一层：
  ① 每个请求前统一 `throttle.before_request()`，不会有「漏掉某个接口没节流」这种事；
  ② 分页、错误归一化、冲突策略只写一遍；
  ③ 单测可以拿一个假 client 顶掉全部网络行为。

⚠️ 三条来自 p115client 文档原文的硬约束（照做，不是保守）：
  · `fs_files`  —— 「**此接口被风控**，此域名下的大量接口都会被风控」
                   ⇒ 只对**真要动的目录**调用，且单次最多回 200 条，必须翻页
  · `fs_move`   —— 「**请不要并发执行**」
  · `fs_delete` —— 「请不要并发执行，但不限制文件数」「删除和还原互斥」
  · `fs_move` 的 `conflict_policy` —— 文件默认 `replace`（**覆盖、不可恢复**）
                   ⇒ 本工具**永远显式传 `keep_both`**（重名就加 (1) 后缀），绝不覆盖
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .throttle import Throttle

__all__ = ["V115", "Node", "V115Error", "CookieMissing", "load_cookie",
           "read_accounts", "account_of", "MAX_PAGES"]

# 单层目录最多翻多少页（200 条/页 ⇒ 10 万项）。
# 本盘最厚的一层是 11,206 项（56 页），留足余量；设它只是防「接口不认 offset」把工具转死。
MAX_PAGES = 500


class V115Error(RuntimeError):
    pass


class CookieMissing(V115Error):
    pass


@dataclass
class Node:
    """115 列表里的一项（目录的 id 在 cid，文件的在 fid —— 统一成 `id`）。"""
    id: str
    name: str
    is_dir: bool
    size: int = 0

    @property
    def ext(self) -> str:
        if "." not in self.name:
            return ""
        return self.name.rsplit(".", 1)[-1].lower()


# --------------------------------------------------------------------------- cookie
# ── 与 115offline 共用的账号文件（那份是它扫码登录后写的）─────────────────────
# 结构（只取我们认识的字段，其余原样不管）：
#     [{"id": "…", "name": "主号", "cookie": "UID=…;CID=…;SEID=…", …}, …]
# 也容忍 `{"items": [...]}` / `{"accounts": [...]}` 这两种包一层的写法。
def read_accounts(path: Path) -> list[dict]:
    """读账号列表。**只读**，文件不在/坏了都返回空表（⛔ 不抛 —— 调用方要能区分"没有"和"读失败"）。"""
    if not path.exists():
        return []
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(items, dict):
        items = items.get("items") or items.get("accounts") or []
    return [it for it in (items or []) if isinstance(it, dict)]


def account_of(cfg: Config) -> tuple[str, str]:
    """从共用账号文件里挑一个 → `(cookie, 账号名)`。

    ⚠️ 挑选口径：`ACCOUNT_ID` 指定了就**只**用它（指了却找不到 ⇒ 抛错，⛔ 不静默换人）；
       没指定 ⇒ 按文件顺序取**第一个 cookie 长得像样**的。
    """
    accs = read_accounts(Path(cfg.accounts_file))
    want = (cfg.account_id or "").strip()
    if want:
        for it in accs:
            if str(it.get("id") or "") == want or str(it.get("name") or "") == want:
                ck = str(it.get("cookie") or "").strip()
                if ck:
                    return ck, str(it.get("name") or it.get("id") or want)
        raise CookieMissing(
            f"账号文件里有 {len(accs)} 个账号，但没有 id/名字等于 {want!r} 的那个：\n"
            f"  {Path(cfg.accounts_file)}\n"
            f"  ⇒ 去网页「115 账号」里重选，或把 ACCOUNT_ID 清空（清空 = 用第一个能用的）"
        )
    for it in accs:
        ck = str(it.get("cookie") or "").strip()
        if _looks_like_cookie(ck):
            return ck, str(it.get("name") or it.get("id") or "(未命名)")
    return "", ""


def _looks_like_cookie(text: str) -> bool:
    """粗判一段文本是不是 115 的 cookie。⛔ 只看有没有 `UID=`，不校签名 —— 过期与否由接口说了算。"""
    return bool(text) and "UID=" in text.upper()


def load_cookie(cfg: Config) -> str:
    """按优先级找 cookie，并把「这一份是从哪来的」记进 `cfg.cookie_source`（网页上要显示）。

        ① `P115_COOKIE` 环境变量  —— 显式指定，最高优先级（但**推荐留空**）
        ② 共用账号文件 `accounts.json` —— 🔗 **在 115offline 扫码登录一次，这边就能用**
        ③ `DATA_DIR/cookie.txt`   —— 老路子，**已降级为兜底**（当年手抓 cookie 用的）

    🔴 为什么 ② 要排在 ③ 前面（2026-10-06 改的）：`cookie.txt` 是人手贴进去的**死凭据**，
       过期了不会自己更新；而 `accounts.json` 是 115offline **扫码登录写入的活凭据**。
       老顺序下，只要盘上残留一份 `cookie.txt`，就会把刷新过的账号盖掉 ——
       症状是「在 115offline 里明明登录好了，整理工具却说 cookie 失效」，极难查。
    """
    if cfg.cookie:
        return _remember(cfg, cfg.cookie.strip(), "环境变量 P115_COOKIE")

    ck, who = account_of(cfg)
    if ck:
        return _remember(cfg, ck, f"共用账号 {who}（{Path(cfg.accounts_file)}）")

    f = Path(cfg.data_dir) / "cookie.txt"
    if f.exists():
        text = f.read_text(encoding="utf-8", errors="replace").strip()
        if _looks_like_cookie(text):
            return _remember(cfg, text, f"cookie.txt（{f}）")

    raise CookieMissing(
        "没找到 115 cookie。推荐第 ① 种 —— 只维护一份，还能自动续期：\n"
        f"  ① 在 115offline 里扫码登录一次（它的账号文件：{Path(cfg.accounts_file)}）\n"
        f"     ⚠️ 两个容器要挂同一个数据卷，或用 ACCOUNTS_FILE 把这份文件指过来\n"
        f"  ② 环境变量 P115_COOKIE=…（临时用；过期不会自己更新）\n"
        f"  ③ 手抓一份写进 {f}（兜底路子）"
    )


def _remember(cfg: Config, cookie: str, source: str) -> str:
    """把 cookie 来源挂到 cfg 上 —— 报错和网页都要说清「这份是从哪来的」。"""
    try:
        object.__setattr__(cfg, "cookie_source", source)
    except Exception:
        pass
    return cookie



def _ok(resp: Any) -> bool:
    """判定 115 响应成功。`check_response` 只在明确失败时抛，但有的接口是软失败。"""
    if not isinstance(resp, dict):
        return bool(resp)
    if "state" in resp:
        return resp.get("state") in (True, 1, "1")
    return not resp.get("error")


def _msg(resp: Any) -> str:
    if isinstance(resp, dict):
        return str(resp.get("error_msg") or resp.get("message") or resp.get("error") or "")
    return str(resp)


def _dir_id(resp: Any) -> str:
    """从目录类接口的返回里取目录 id。

    ⚠️ **两个接口把 id 放在不同地方**（2026-10-06 真机实测，不是猜的）：

        fs_dir_getid   GET /files/getid       → {"state":true, "id":"<目录 id>"}
                                                ⇒ 顶层 `id`
        fs_dir_getid2  GET /files/get_path_id → {"state":true, "data":{"file_id":"3533…", "is_private":"0"}}
                                                ⇒ **`data.file_id`**，顶层没有 `id`

    `fs_makedirs` 在 p115client 里就是 `fs_dir_getid2(is_create=1)` 的封装 ⇒ 同一形态。

    ⛔ 曾经只按顶层 `id`/`cid` 取 ⇒ `ensure_dir()` 两级都拿不到 id ⇒ 抛「建目录失败」。
       在全量计划里那是 **1,183 条 mkdir 全部失败**（第一步就崩）。
    """
    if not isinstance(resp, dict):
        return ""
    data = resp.get("data")
    if isinstance(data, dict):
        got = data.get("file_id") or data.get("cid") or data.get("id")
        if got:
            return str(got)
    return str(resp.get("id") or resp.get("cid") or "")


# --------------------------------------------------------------------------- 主体
class V115:
    def __init__(self, client: Any, cfg: Config, throttle: Throttle, log: Any = None):
        self.client = client
        self.cfg = cfg
        self.throttle = throttle
        self.log = log or (lambda *a, **k: None)
        self.calls = 0
        self._targets: dict[str, str] = {}      # 相对路径 → 目录 id（建过的目标不再重复建）

    # ---------------------------------------------------------------- 请求出口
    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """**唯一**的请求出口。所有方法都必须走这里，保证节流不被绕过。"""
        self.throttle.before_request()
        self.calls += 1
        fn = getattr(self.client, name)
        try:
            resp = fn(*args, **kwargs)
        except Exception as exc:
            self.throttle.fail(f"{name}: {exc}")
            raise V115Error(f"{name} 失败：{exc}") from exc
        if isinstance(resp, dict) and resp.get("error"):
            self.throttle.fail(f"{name}: {_msg(resp)}")
            raise V115Error(f"{name} 被 115 拒绝：{_msg(resp)}")
        self.throttle.ok()
        return resp

    # ---------------------------------------------------------------- 读
    def list_dir(self, cid: str, dirs_only: bool = False) -> list[Node]:
        """列一层目录（**自动翻页**）。

        ⚠️ 翻页必须做：`fs_files` 单次最多回 200 条（传 500 也只给 200）。
           实测本盘 `云下载` 一层就有 11,206 项 ⇒ 不翻页会**静默丢掉 98%**。
        """
        out: list[Node] = []
        offset = 0
        limit = 200
        pages = 0
        while True:
            payload: dict[str, Any] = {"cid": cid, "limit": limit, "offset": offset,
                                       "show_dir": 1}
            if dirs_only:
                payload["nf"] = 1        # 115 原生「只要目录」，文件根本不会被拉回来
            data = self._call("fs_files", payload)
            raw = [n for n in ((data or {}).get("data") or []) if isinstance(n, dict)]
            for node in raw:
                is_dir = not node.get("fid")
                out.append(Node(
                    id=str(node.get("cid") if is_dir else node.get("fid") or ""),
                    name=node.get("n") or node.get("name") or "",
                    is_dir=is_dir,
                    size=int(node.get("s") or 0),
                ))
            total = (data or {}).get("count")
            offset += len(raw)
            pages += 1
            if not raw:
                break                    # 回空了 = 到头了
            if total is not None and offset >= int(total):
                break                    # `count` 是总数，有它就不猜
            if pages >= MAX_PAGES:
                # 兜底：`count` 缺失 + 接口不认 offset 的极端情况下，别把自己转死
                self.log("warning", f"列目录 {cid} 已翻 {pages} 页还没到底，先收手"
                                    f"（已取 {len(out)} 项，结果可能不全）")
                break
        return out

    def root_cid(self) -> str:
        """拿到整理根（如 `云下载`）的目录 id。"""
        if self._targets.get("") :
            return self._targets[""]
        resp = self._call("fs_dir_getid", {"path": self.cfg.root_path})
        cid = _dir_id(resp)
        if not cid:
            # 兜底：从根目录列举里按名字找
            for node in self.list_dir("0", dirs_only=True):
                if node.name == self.cfg.root_path:
                    cid = node.id
                    break
        if not cid:
            raise V115Error(f"115 上找不到目录「{self.cfg.root_path}」")
        self._targets[""] = cid
        return cid

    def receive_list(self, limit: int = 100) -> list[dict]:
        """「最近接收」列表 —— 自动化巡检靠它发现新目录（115 没有 webhook，只能轮询）。

        ⚠️ 这套返回**跟 `fs_files` 完全是两套字段**，别混用（2026-10-06 真机实测）：

            {"state": true, "data": {"total": 32, "list": [ {...}, ... ]}}

          · `data` 是 **dict**（不是 list），条目在 `data.list` 里 ⇒
            按 `isinstance(data, list)` 去解析会**静默拿到空表**，自动化那半直接失效。
          · `id`         —— 这条**接收记录**的 id，⛔ 不是文件 id（曾经当 fid 用，是错的）
          · `file_id`    —— 文件 id；**空串 = 多文件聚合项**（如 `xxx.srt等2个文件`）
                            ⛔ 空串**不代表是目录** —— 「not file_id 就是目录」这个判据是错的
          · `file_name`  —— 接到的条目名
          · `parent_id` / `parent_name` —— 它落在**哪个目录**下 ★ 发现新目录的关键
          · `create_time` / `update_time` —— unix 秒

        实测样本：
            id=<文件 id> file_id=<文件 id> fname='美剧【示例剧名】1-4季 4K中字【255G】' pname='电视剧'
            id=<文件 id> file_id='' fname='示例剧名_The Show.srt等2个文件' pname='示例剧名（2025）'
        """
        resp = self._call("fs_history_receive_list", {"limit": limit})
        data = (resp or {}).get("data")
        if isinstance(data, dict):
            items = data.get("list") or []
        elif isinstance(data, list):         # 兜底：万一哪天改成直接给 list
            items = data
        else:
            items = (resp or {}).get("list") or []
        out: list[dict] = []
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            out.append({
                "record_id": str(it.get("id") or ""),
                "file_id": str(it.get("file_id") or ""),
                "file_name": it.get("file_name") or it.get("n") or "",
                "parent_id": str(it.get("parent_id") or ""),
                "parent_name": it.get("parent_name") or "",
                "time": it.get("create_time") or it.get("update_time") or "",
                "raw": it,
            })
        return out

    # ---------------------------------------------------------------- 写
    def ensure_dir(self, rel_path: str) -> str:
        """确保 `root/rel_path` 存在（不存在则**逐级创建**），返回其 id。

        `fs_mkdir` 不建中间层，所以用 `fs_makedirs`
        （p115client 里它就是对 `fs_dir_getid2(is_create=1)` 的封装）。

        ⚠️ id 在 **`data.file_id`** 里而不是顶层 `id` —— 取法统一走 `_dir_id()`。
        """
        rel = rel_path.strip("/")
        if not rel:
            return self.root_cid()
        if rel in self._targets:
            return self._targets[rel]
        parent = self.root_cid()
        cur = ""
        for part in rel.split("/"):
            cur = f"{cur}/{part}" if cur else part
            if cur in self._targets:
                parent = self._targets[cur]
                continue
            resp = self._call("fs_makedirs", {"path": part, "parent_id": parent})
            cid = _dir_id(resp)
            if not cid:
                # 兜底：万一某个版本新建时只回 state，按路径再问一次
                r2 = self._call("fs_dir_getid2", {"path": part, "parent_id": parent})
                cid = _dir_id(r2)
            if not cid:
                raise V115Error(f"建目录失败：{cur}（返回里没有 id：{resp}）")
            self._targets[cur] = cid
            parent = cid
        return parent

    def _conflict_policy(self, fids: Iterable[str]) -> str:
        """⛔ 必须显式传 —— 不传的话**文件默认 replace（覆盖，不可恢复）**。
        本工具统一 `keep_both`：重名就自动加 (1)/(2) 后缀，绝不覆盖任何东西。"""
        return json.dumps({str(f): {"action": "keep_both"} for f in fids}, ensure_ascii=False)

    def move(self, fids: list[str], pid: str) -> bool:
        """移动（可一次多个，115 限制 5 万个以内）。⚠️ 不并发、不覆盖。

        ⚠️ 载荷键用 `fid[0]`/`fid[1]`… 而不是 `fid[]` 后面挂一个 list：
           p115client 自己对「传 id 序列」的内部实现就是 `{f"fid[{i}]": fid}`；
           而 `fid[]` 挂 list 会走 requests 的序列化，编成什么样随版本变 ——
           照库自己的写法走，就不用赌那一段（这是照抄，不是猜）。
        """
        if not fids:
            return True
        payload: dict[str, Any] = {f"fid[{i}]": f for i, f in enumerate(fids)}
        payload["pid"] = pid
        payload["conflict_policy"] = self._conflict_policy(fids)
        resp = self._call("fs_move", payload)
        return _ok(resp)

    def rename(self, pairs: list[tuple[str, str]]) -> bool:
        """批量改名。`pairs` = [(file_id, 新名), ...]，一次请求改多个。"""
        if not pairs:
            return True
        payload = {f"files_new_name[{fid}]": name for fid, name in pairs}
        resp = self._call("fs_rename", payload)
        return _ok(resp)

    def delete(self, fids: list[str]) -> bool:
        """删除（**进 115 回收站**，可还原）。⚠️ 不并发、与还原互斥。

        载荷键用 `fid[i]`，理由同 `move`（照 p115client 自己的实现走）。
        """
        if not fids:
            return True
        payload = {f"fid[{i}]": f for i, f in enumerate(fids)}
        resp = self._call("fs_delete", payload)
        return _ok(resp)

    def move_check_conflict(self, fids: list[str], pid: str) -> dict:
        resp = self._call("fs_move_check_conflict", {"move_fids[]": list(fids), "pid": pid})
        return resp if isinstance(resp, dict) else {}


# --------------------------------------------------------------------------- 工厂
def build_client(cookie: str) -> Any:
    """造一个 P115Client。延迟 import —— 单测不装 p115client 也能跑除这层外的全部逻辑。"""
    from p115client import P115Client, check_response  # noqa: F401
    return P115Client(cookie)
