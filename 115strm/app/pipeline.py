"""一轮同步的**调度** —— 按什么顺序做、每步做什么的唯一决策点。

## 只有一条路（2026-10-08 定）

    ⭐ 工具**自己导出目录树** → 解析 → 对照本地 → 只写差异 → 收尾

红领巾定的：「**事件流、实例都去掉**，因为日常的定时处理，
**通过系统自带的导出目录树已经完全足够了**。」
⇒ 原来的三条都删了：

    ~~events~~ 生活事件流增量   —— 「最近接收」只记分享的，依据不牢
    ~~api~~    逐层实时列目录   —— 目录级成本，上千请求 × 节流 ⇒ 十几分钟，太费
    ~~tree~~   只用本地快照     —— 「不需要固定快照」

**没有可选的模式了** ⇒ 界面上不再出现「数据来源」选择器，也不再提「auto」这个词。

## 为什么是「自动导出」而不是「逐层列目录」

    · 逐层实时列目录  = **目录级**成本 —— 本盘 1,183 个目录 ⇒ 上千请求 + 节流 ⇒ 十几分钟
    · 自动导出目录树  = **任务级**成本 —— 提交 + 轮询 + 下载 + 删临时件 ≈ 十几次请求，
                        与库有多大基本无关

⭐ 实测（2026-10-08，本盘 727 目录 / 6,071 文件）：**导出到取回只要 13 秒**。
差几个数量级 ⇒ 只留后者。把 `schedule_cron` 设上（如 `0 4 * * *`），
它就每天自己跑一轮，你只管往网盘里丢片；想立刻生效就在网页上点「立即同步」。

## strm 「内容变了才重写」的判定**不受影响**

判据是 `sync.Differ` 里那两行：

    if self.read_existing(key) != f.url:   # ⭐ 逐个读**本地文件真实内容**去比
        res.to_update.append(f)

也就是「拿本地那个 strm 里现在写着什么」和「按当前设置（含 strm 前缀）算出来该写什么」
逐字比对 —— **纯粹基于目录树算出来的结果**，不依赖 115 的任何变更标记。
⇒ 换数据来源、去掉事件流，这条判据**一点都不变**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import treefile
from .config import Config
from .sync import SyncResult, Syncer
from .throttle import Throttle
from .v115 import V115, build_client, load_cookie

__all__ = ["Outcome", "run_once", "save_tree", "SYNC_STEPS"]

# 「这一轮做了什么」—— 给界面和日志共用的**单一事实源**。
# ⚠️ 别在前端另写一份：文案一改就对不上（本项目吃过「两处各写一遍必然分叉」的亏）。
# 每项 = (阶段键, 标题, 一句说明)
SYNC_STEPS: tuple[tuple[str, str, str], ...] = (
    ("export", "让 115 导出目录树",
     "按最大层级 25 导全（你自己在网页版导时也要拉满，不然深层会被截断）"),
    ("parse", "解析目录与文件清单",
     "读出目录与媒体文件；**整棵子树里都没有视频的目录直接丢掉**，不建"),
    ("diff", "对照本地现状",
     "逐个读已存在的 strm 内容，跟按当前设置算出的地址比对；顺带认得出「你改过名」的文件"),
    ("write", "只写差异",
     "新增的补上、内容变了的重写；判为「盘上没了」的走二次确认才删，异常比例直接不删"),
    ("tidy", "收尾",
     "删掉本轮不再需要的空目录；写出回执"),
)

# 目录树留几份（一份几 MB，留太多白占盘）。⚠️ 与 `pick_tree_file` 的口径必须一致。
_KEEP_TREES = 5


@dataclass
class Outcome:
    """一轮的结果 —— 回执 + 给界面的额外信息。"""
    res: SyncResult
    source: str = ""
    detail: dict = field(default_factory=dict)


def save_tree(cfg: Config, raw: bytes) -> Path:
    """把导出的目录树存进 `DATA_DIR/tree/`，并清掉旧的。

    ⚠️ 存**原始字节**，不要 decode：115 导出的是 UTF-16，
       交给 `treefile.parse_file` 那套按 BOM 识别的读法去解。

    ⭐ 留档的意义：出问题时能拿**同一份输入**复现，而不是「反正每次都重新导」。

    🔴 2026-10-08 改：清理口径从「只清 `auto-*.txt`」扩成「**所有树文件**统一只留最近 N 份」。
       原因：旧命令 `python -m app export` 存下来的叫 `manual-*.txt`，不在原先的清理范围内
       ⇒ 那些文件**永久堆积**；而 `pick_tree_file()` 取的是「目录里最新的一份」，
       会把这些陈年旧树一起算进去，**可能选中一份几个月前的旧树**。
       现在树只有一个来源（工具自己导），统一口径，并顺便把老命名也清掉。
    """
    d = Path(cfg.tree_dir)
    d.mkdir(parents=True, exist_ok=True)

    p = d / f"tree-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    p.write_bytes(raw)

    # 只留最近 N 份（含刚写的这份）—— 筛选口径与 `treefile.pick_tree_file` 保持一致
    try:
        cands = [f for f in d.iterdir()
                 if f.is_file() and f.suffix.lower() in (".txt", ".tree", ".list")]
        survivors = sorted(cands, key=lambda f: f.stat().st_mtime, reverse=True)
        for old in survivors[_KEEP_TREES:]:
            old.unlink()
    except OSError:
        pass
    return p


def tree_source(cfg: Config, v: V115 | None, log: Callable) -> tuple[list[str], list[Any], str]:
    """拿一份目录树 → `(目录列表, 文件列表, 来源说明)`。

    ⭐ **主路径**：让 115 自己导 —— 要的是「此刻最新」。
    ⚠️ **容错**：导出这条路失败时，退回 `tree/` 里**已有的那份**（零请求）。
       宁可拿一份稍旧的树去对账，也不要整轮跑不起来。
       （2026-10-08 实测：导出件下载曾因 cookie 问题报 403，就是靠这条兜住的 ——
         没有它，每轮都会静默失败。）
    """
    def parse(path: Path) -> tuple[list[str], list[Any]]:
        tree = treefile.parse_file(path)
        return Syncer(None, cfg, log).walk_from_tree(tree)

    errors: list[str] = []
    if v is not None:
        try:
            raw = v.export_tree_bytes()
            saved = save_tree(cfg, raw)
            dirs, files = parse(saved)
            log("info", f"已让 115 自己导出目录树 ⇒ {saved.name}"
                        f"（{len(dirs):,} 目录 / {len(files):,} 个媒体项）")
            return dirs, files, f"115 自动导出 {saved.name}"
        except Exception as exc:
            errors.append(f"自动导出：{exc}")
            log("warning", f"自动导出这条路不行（{exc}）⇒ 退回本地已有的目录树")

    # 兜底：tree/ 里最新的那份（老版本存下的叫 manual-*，也一并认得）
    tp = Path(treefile.pick_tree_file(cfg.tree_dir))
    dirs, files = parse(tp)
    log("info", f"用本地已有的目录树（零请求）：{tp.name}")
    return dirs, files, f"本地导出件 {tp.name}"


def run_once(cfg: Config, log: Callable, *,
             sleeper: Callable[[float], None] | None = None,
             force_delete: bool = False,
             on_stage: Callable[[str], None] | None = None) -> Outcome:
    """跑一轮。⚠️ **这是 web / cli 唯一共用的执行入口。**"""
    # ---------------------------------------------------------- ⓪ 前置守卫：前缀
    # 🔴 `strm_prefix` 出厂为空（网页专属字段，见 config.strm_prefix）。
    #    空着还往下跑的话，`StrmBuilder` 只会拼出一个相对路径 ——
    #    **能跑完、能写出一堆文件、但一个都播不了**。这种「看起来成功了」的失败
    #    是本项目最忌讳的形态 ⇒ 干脆**开跑前就拦住**，并直接告诉去哪填。
    if not cfg.prefix_ready:
        raise ValueError(
            "还没配置 strm 内容前缀 —— 生成的 strm 里会写不出完整地址。\n"
            "  请到网页「设置 › 播放地址」填上你的 alist 地址，"
            "格式是 `https://你的域名:端口/d/挂载路径`（如 `https://alist.example.com:5244/d/影音/115影音`）。\n"
            "  ⚠️ 必须用 `/d` 端点（`/dav` 要认证、播不了），"
            "而且**要写到挂载路径的完整一层**（少一层 alist 会回 storage not found，"
            "状态码却还是 200）。填完可以点「自检 strm 前缀」验一下。")

    throttle = Throttle(cfg=cfg, sleeper=sleeper, log=log)
    detail: dict = {"tree_origin": ""}

    def stage(label: str) -> None:
        if on_stage:
            try:
                on_stage(label)
            except Exception:
                pass

    # ---------------------------------------------------------- ① 建客户端
    # 主路径就是导树 ⇒ 一定要 115 客户端。cookie 有问题在这一步就报清楚（别等到用的时候）
    cookie = load_cookie(cfg)
    log("info", f"账号来源：{getattr(cfg, 'cookie_source', '(未知)')}")
    v = V115(build_client(cookie), cfg, throttle, log)

    syncer = Syncer(v, cfg, log)

    # ---------------------------------------------------------- ② 导树 → 解析
    stage(SYNC_STEPS[0][1])
    dirs, files, origin = tree_source(cfg, v, log)
    detail["tree_origin"] = origin

    # ---------------------------------------------------------- ③ 对照本地 + 落盘
    #    ⚠️ 这一步会**逐个读本地已存在的 strm 内容**去比对（`sync.Differ`）——
    #       「内容变了才重写」的判据就在这里，跟数据来源无关。
    stage(SYNC_STEPS[2][1])
    res = syncer.run(remote_files=files, remote_dirs=dirs, source="tree",
                     force_delete=force_delete)

    res.requests = throttle.stats.requests
    return Outcome(res=res, source="tree", detail=detail)
