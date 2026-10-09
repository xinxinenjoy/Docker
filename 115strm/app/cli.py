"""命令行入口。

```
python -m app serve     # ★ 网页界面（容器默认动作）
python -m app sync      # ★ 同步一轮：工具自己导目录树 + 全量对账 + 只写差异
python -m app export    # 只让 115 导一次目录树并存档（诊断 / 手工核对）
python -m app preview   # 只预览差异，什么都不写
python -m app doctor    # 环境自检：cookie / 目录树 / 输出目录 / 依赖
python -m app pending   # 看挂起删除
python -m app apply     # 确认删除挂起的那些
```

⭐ **只有一条路**（2026-10-08 定的）：工具**自己导目录树 → 对照本地 → 只写差异**。
   在网页里设上 `SCHEDULE_CRON`，它就每天自己跑；想立刻生效就点网页上的「立即同步」。

⚠️ 原来还有 `auto` / `events` / `tree` / `api` / `life` 五个命令 —— 全删了。
   `sync` 与网页那个按钮跑的是**完全相同的一轮**（共用 `pipeline.run_once`），
   连「模式」这个参数都不再存在。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import pipeline as pipeline_mod
from . import report as report_mod
from . import treefile
from .config import Config, load
from .namer import StrmBuilder
from .sync import Syncer
from .throttle import Throttle
from .v115 import CookieMissing, V115, build_client, load_cookie

__all__ = ["main", "setup_log"]

_LOG_FMT = "%(asctime)s %(levelname)-7s %(message)s"
_DATE_FMT = "%H:%M:%S"


def setup_log(cfg: Config) -> tuple[logging.Logger, object]:
    logger = logging.getLogger("115strm")
    if not logger.handlers:
        logger.setLevel(getattr(logging, cfg.log_level, logging.INFO))
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter(_LOG_FMT, _DATE_FMT))
        logger.addHandler(sh)
        try:
            cfg.log_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(cfg.log_dir / "115strm.log", encoding="utf-8")
            fh.setFormatter(logging.Formatter(_LOG_FMT, _DATE_FMT))
            logger.addHandler(fh)
        except Exception:
            pass

    def log(level: str, msg: str) -> None:
        logger.log(getattr(logging, level.upper(), logging.INFO), msg)

    return logger, log


def _write_receipt(cfg: Config, res) -> str:
    out = Path(cfg.data_dir) / "reports"
    out.mkdir(parents=True, exist_ok=True)
    stamp = report_mod.now_stamp()
    d = res.as_dict()
    (out / f"sync-{stamp}.json").write_text(json.dumps(d, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
    (out / f"sync-{stamp}.md").write_text(report_mod.render_sync_markdown(d, cfg.all()),
                                          encoding="utf-8")
    return stamp


# --------------------------------------------------------------------------- 命令
def cmd_serve(cfg: Config, log, args) -> int:
    from .web import serve
    serve(port=args.port)
    return 0


def _run_sync(cfg: Config, log) -> int:
    """跑一轮同步。

    ⚠️ 与网页**共用** `pipeline.run_once` —— 不再各写一份。
       v0.1 就是两处各写一遍，结果分叉：网页点「事件流」一直返回 400，
       而命令行「能跑」（跑的根本不是事件流）。两处各写一份，必然分叉。
       现在连 `mode` 参数都取消了 —— 只有一条路，没什么可分的。
    """
    out = pipeline_mod.run_once(cfg, log)
    res = out.res
    log("info", f"网盘侧：{res.scanned_remote:,} 项 / 本地 {res.scanned_local:,} 个 strm")
    stamp = _write_receipt(cfg, res)
    log("info", f"写了 {res.written} 个 strm、删了 {res.deleted} 个"
                f"、挂起 {res.pending_delete} 个；回执：reports/sync-{stamp}.md")
    if res.guard_tripped:
        log("error", res.guard_reason)
    return 0


def cmd_preview(cfg: Config, log, args) -> int:
    """只预览，什么都不写。

    ⚠️ 用**本地**目录树（零请求）—— 「先看再动」的第一原则，不该为了预览去调 115。
       本地没有就先 `python -m app export` 导一份。
    """
    if not cfg.prefix_ready:
        log("error", "还没配置 strm 内容前缀 —— 空着的话预览里的地址是残缺的，看了也没用")
        log("info", "  ⇒ 网页「设置 › 播放地址」填你的 alist 地址"
                    "（`https://域名:端口/d/挂载路径`，必须用 /d 端点）")
        return 1
    try:
        tp = Path(treefile.pick_tree_file(cfg.tree_dir))
    except Exception as exc:
        log("error", f"本地没有可用的目录树：{exc}")
        log("info", "  ⇒ 先 `python -m app export` 让 115 导一份；"
                    "或直接跑 `python -m app sync`（它会自己导）")
        return 1
    dry = cfg.replace(dry_run=True)
    syncer = Syncer(None, dry, log)
    tree = treefile.parse_file(tp)
    _dirs, files = syncer.walk_from_tree(tree)
    local = syncer.scan_local()
    d = syncer.diff(files, local)
    c = d.counts()
    log("info", f"目录树：{tp.name}")
    log("info", f"网盘 {len(files):,} 项 / 本地已有 {len(local):,} 个 strm")
    log("info", f"新增 {c['add']:,} · 更新 {c['update']:,} · 删除 {c['delete']:,}")
    if files:
        log("info", f"strm 内容样例：{files[0].url}")
    return 0


def cmd_export(cfg: Config, log, args) -> int:
    """只让 115 自己导一次目录树并落盘（诊断 / 手工核对用）。

    ⭐ 走 `pipeline.save_tree` —— 与自动同步**同一套命名与清理规则**，
       不再另外产出 `manual-*.txt`（那批文件在老版本里会永久堆积，
       而且会被 `pick_tree_file` 当成「最新的树」选中，见 `save_tree` 的注释）。
    """
    cookie = load_cookie(cfg)
    log("info", f"账号来源：{getattr(cfg, 'cookie_source', '(未知)')}")
    v = V115(build_client(cookie), cfg, Throttle(cfg=cfg, log=log), log)
    raw = v.export_tree_bytes(keep=bool(args.keep))
    p = pipeline_mod.save_tree(cfg, raw)
    tree = treefile.parse_file(p)
    st = tree.stats()
    log("info", f"✅ 已存到 {p}（{len(raw):,} 字节）")
    log("info", f"   树里：{st['dirs']:,} 目录 / {st['files']:,} 文件 / 顶层 {st['top_level']} 项")
    # ⚠️ 这里以前是**无条件**打印「⚠️ 核对一下目录数：比预期少 ⇒ 多半是同步根没对上」——
    #    不管实际有没有问题都喊，属恒定噪音 + 误导（2026-10-08 实跑发现）。
    #    改成只看 `extensionless`（判据详见 `treefile.Tree.extensionless`）。
    #    ⚠️ 注意**不是**「为 0 才正常」：工具自导的 25 层树实测也有 3 个 ——
    #       那 3 个是盘上**真实的**无扩展名文件（发布者塞的推广文件），不是截断。
    n = st["extensionless"]
    if n >= 20:
        sample = "；例如：" + "，".join(st["extensionless_sample"][:3])
        log("warning",
            f"   ⚠️ 有 {n:,} 个条目既没有子项、也没有扩展名 —— "
            f"这个数量基本可断定是**导出层级不足**（子树被截断），这些条目一律不建{sample}")
    elif n:
        log("info", f"   另有 {n} 个「无子项也无扩展名」的条目"
                    f"（多半是盘上真实存在的无扩展名文件，如发布者的推广文件）—— 一律不建")
    else:
        log("info", "   ✓ 没有「无子项也无扩展名」的条目")
    return 0


def cmd_doctor(cfg: Config, log, args) -> int:
    ok = True
    print("=== 115strm 自检 ===")
    print(f"数据目录   : {cfg.data_dir}  "
          f"{'✅' if Path(cfg.data_dir).exists() else '⚠️ 不存在（会自动建）'}")
    print(f"输出目录   : {cfg.output_dir}  "
          f"{'✅' if Path(cfg.output_dir).exists() else '⚠️ 不存在（首次同步会建）'}")
    print(f"strm 前缀  : {cfg.strm_prefix}")
    print(f"网盘根     : {cfg.remote_root or '(网盘根)'}")
    print(f"扩展名     : {len(cfg.exts)} 种")
    print("同步方式   : 自己导出目录树 + 全量对账（✅ 不用你手动导任何东西）")

    try:
        tp = Path(treefile.pick_tree_file(cfg.tree_dir))
        tree = treefile.parse_file(tp)
        st = tree.stats()
        print(f"目录树     : ✅ {tp.name}（{st['dirs']:,} 目录 / {st['files']:,} 文件）")
    except Exception as exc:
        # ⚠️ 这里**不再是 ❌**：同步时会自己导一份，本地没有照样能跑。
        print(f"目录树     : ⚠️ 本地没有（{exc}）—— 同步时会自己导，不影响使用")

    try:
        ck = load_cookie(cfg)
        print(f"115 账号   : ✅ {getattr(cfg, 'cookie_source', '')}（…{ck[-8:]}）")
    except CookieMissing as exc:
        print(f"115 账号   : ⚠️ 未配置\n{exc}")
        ok = False

    try:
        import p115client, fastapi, uvicorn  # noqa: F401
        print("依赖       : ✅ p115client / fastapi / uvicorn")
    except Exception as exc:
        print(f"依赖       : ❌ {exc}")
        ok = False

    # ⚠️ 前缀空着时**不能**硬算样例 URL —— `StrmBuilder.url()` 会抛 ValueError，
    #    而 `doctor` 恰恰是「出问题时跑来诊断」的命令，它在空前缀下崩掉最没道理。
    #    ⇒ 空着就只报告状态，把「去哪填」写清楚。
    if cfg.prefix_ready:
        b = StrmBuilder.from_cfg(cfg)
        print(f"URL 样例   : {b.url('115电影/示例片（2026）/示例.mkv')}")
    else:
        print("URL 样例   : ⚠️ 还没配 strm 内容前缀（空着就生成不出可播的地址）")
        print("  ⇒ 网页「设置 › 播放地址」填你的 alist 地址"
              "（`https://域名:端口/d/挂载路径`，必须用 /d 端点）")
        ok = False

    # ⭐ 前缀自检（真发一次请求）—— 这是最容易配错又最难查的一项
    from .health import check_prefix
    if args.check:
        print("前缀自检   : 请求中…")
        r = check_prefix(cfg)
        mark = "✅" if r.ok else "❌"
        print(f"前缀自检   : {mark} {r.message}")
        if r.tip:
            print(f"             建议：{r.tip}")
        if not r.ok:
            ok = False

    print("=" * 22)
    print("✅ 全部就绪" if ok else "⚠️ 有项目需要注意（见上）")
    return 0 if ok else 1


def cmd_pending(cfg: Config, log, args) -> int:
    s = Syncer(None, cfg)
    p = s.load_pending()
    if not p.paths:
        print("没有挂起的删除")
        return 0
    print(f"挂起 {len(p.paths)} 条，已等 {p.age_minutes():.1f} 分钟"
          f"（阈值 {cfg.delete_defer_minutes:.0f} 分钟）")
    for x in p.paths[:200]:
        print("  -", x)
    return 0


def cmd_apply(cfg: Config, log, args) -> int:
    s = Syncer(None, cfg)
    p = s.load_pending()
    if not p.paths:
        print("没有挂起的删除")
        return 0
    if not args.yes:
        print(f"将删除 {len(p.paths)} 个本地 strm。加 --yes 确认。")
        return 1
    n = s.remove(p.paths)
    s.clear_pending()
    print(f"已删除 {n} 个")
    return 0


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="115strm", description="115 网盘 → 本地 strm")
    p.add_argument("--port", type=int, default=None, help="serve 的端口")
    p.add_argument("--keep", action="store_true",
                   help="export 时保留 115 上那份临时导出件")
    p.add_argument("cmd", nargs="?", default="serve",
                   choices=["serve", "sync", "export",
                            "preview", "doctor", "pending", "apply",
                            # ⚠️ 以下是**废弃别名** —— 2026-10-08 去掉模式概念后它们全跑同一条路。
                            #    留着只为不让老脚本 / 老文档里的命令**突然炸**（会打印一行提示）。
                            #    ⛔ 别在新文档里用这几个名字。
                            "auto", "events", "tree", "api"])
    p.add_argument("--yes", action="store_true", help="apply 时确认")
    p.add_argument("--check", action="store_true",
                   help="doctor 时真发一次请求，自检 strm 前缀是否配对正确")
    args = p.parse_args(argv)

    cfg = load()
    _, log = setup_log(cfg)
    # 🔴 启动时一次性准备：把 `.env` 里遗留的「网页专属」参数（STRM_PREFIX）搬进
    #    `data/config.json` —— 不然升级后前缀会静默变空、跑起来才发现。
    from . import config as config_mod
    config_mod.bootstrap(log)
    cfg = load()

    if args.cmd == "serve":
        return cmd_serve(cfg, log, args)
    if args.cmd == "doctor":
        return cmd_doctor(cfg, log, args)
    if args.cmd == "preview":
        return cmd_preview(cfg, log, args)
    if args.cmd == "export":
        return cmd_export(cfg, log, args)
    if args.cmd == "pending":
        return cmd_pending(cfg, log, args)
    if args.cmd == "apply":
        return cmd_apply(cfg, log, args)
    if args.cmd in ("auto", "events", "tree", "api"):
        log("warning", f"⚠️ `{args.cmd}` 已经废弃了（去掉模式后只有一条路）——"
                       f"请改用 `python -m app sync`；本次照 sync 跑")
        return _run_sync(cfg, log)
    return _run_sync(cfg, log)
