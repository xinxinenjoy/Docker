"""命令行入口。

```
python -m app plan      # 用目录树算出方案（不联网），出报告 + plan.json
python -m app run       # 执行（默认读 plan.json；DRY_RUN=1 时一个请求都不发）
python -m app once      # 在线增量跑一轮（新增目录 / 最近接收）
python -m app watch     # 常驻，按 SCHEDULE_CRON 到点跑 once
python -m app stats     # 只看目录树的规模统计
python -m app doctor    # 环境自检：cookie / 树文件 / 目录写权限 / 依赖
python -m app clear-state  # 清断点（重跑整轮时用）
```
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import report as report_mod
from . import treefile
from .config import Config, load
from .executor import Executor
from .plan import Plan, build_plan
from .throttle import Throttle

__all__ = ["main", "setup_log"]

_LOG_FMT = "%(asctime)s %(levelname)-7s %(message)s"
_DATE_FMT = "%H:%M:%S"


def setup_log(cfg: Config, name: str = "115organize") -> tuple[logging.Logger, callable]:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(getattr(logging, cfg.log_level, logging.INFO))
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter(_LOG_FMT, _DATE_FMT))
        logger.addHandler(sh)
        try:
            cfg.log_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(cfg.log_dir / f"{name}.log", encoding="utf-8")
            fh.setFormatter(logging.Formatter(_LOG_FMT, _DATE_FMT))
            logger.addHandler(fh)
        except Exception:
            pass

    def log(level: str, msg: str) -> None:
        logger.log(getattr(logging, level.upper(), logging.INFO), msg)

    return logger, log


def _reports_dir(cfg: Config) -> Path:
    out = cfg.data_dir / "reports"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _load_tree(cfg: Config):
    path = treefile.pick_tree_file(cfg.tree_dir, cfg.tree_file)
    return treefile.parse_file(path), path


def _estimate_for(plan: Plan, cfg: Config) -> dict:
    """按执行层实际会发的请求数估算耗时（比按动作数估准得多）。

    请求数 ≈ 列根目录分页 + 目标目录创建 + 改名批次 + 移动批次 + 清理批次
    """
    thr = Throttle(cfg=cfg)
    n_mkdir = sum(1 for op in plan.ops if op.kind == "mkdir")
    n_rename = sum(1 for op in plan.ops if op.kind == "rename") + \
        sum(1 for op in plan.ops if op.kind in ("move_dir", "move_file") and op.new_name)
    n_move = sum(1 for op in plan.ops if op.kind in ("move_dir", "move_file"))
    n_trash = sum(1 for op in plan.ops if op.kind == "trash")
    # ⚠️「列根目录」的页数按**根下条目数**算 —— `fs_files` **不递归**，只列一层。
    #    拿整棵树的 dirs+files 会高估一个数量级（实测：根下 7,698 项 ⇒ 39 页；
    #    整棵树 71,944 项算出来是 360 页，白吓人）。
    root_entries = plan.tree_stats.get("root_entries")
    if not root_entries:
        root_entries = sum(1 for op in plan.ops if not op.src_dir) or 1
    root_pages = max(1, (root_entries + 199) // 200)
    # 下面各项都取**上界**（宁可估久，别让人以为很快）：清理那两项尤其粗 ——
    # 实际是「每个源目录只列一次」+「每 500 条才发一次移动请求」。
    est = (root_pages                                   # 列根目录
           + n_mkdir                                    # 建目标目录
           + (n_rename + 99) // 100                     # 改名分批
           + max(1, len({op.target for op in plan.ops if op.kind in ("move_dir", "move_file")}))
           + (n_move + 499) // 500                      # 移动分批
           + n_trash                                    # 清理要逐个目录列一次 + 移动
           + max(1, len({op.src_dir for op in plan.ops if op.kind == "trash"})))
    out = thr.estimate(est)
    out["breakdown"] = {
        "列根目录": root_pages, "建目录": n_mkdir,
        "改名批次": (n_rename + 99) // 100,
        "移动批次": max(1, len({op.target for op in plan.ops if op.kind in ("move_dir", "move_file")})),
        "清理相关": n_trash,
    }
    return out


def cmd_plan(cfg: Config, log, args) -> int:
    tree, path = _load_tree(cfg)
    log("info", f"目录树：{path}（{tree.stats()['dirs']:,} 目录 / {tree.stats()['files']:,} 文件）")
    plan = build_plan(tree, cfg, max_depth=args.depth)
    est = _estimate_for(plan, cfg)
    c = plan.counts()
    log("info", f"计划：{c['actions']:,} 条动作（自动 {c['auto']:,} / 人工 {c['manual']:,}）"
                f"；冲突 {c['conflicts']}，灰区 {c['suspects']:,}；预计耗时 {est['human']}")

    out = Path(args.out) if args.out else _reports_dir(cfg)
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.json").write_text(plan.dumps(), encoding="utf-8")
    (out / "plan-ops.txt").write_text(report_mod.render_ops_txt(plan), encoding="utf-8")
    md = report_mod.render_markdown(plan, est)
    (out / "plan.md").write_text(md, encoding="utf-8")
    log("info", f"已写出：{out / 'plan.md'} / plan.json / plan-ops.txt")
    return 0


def cmd_run(cfg: Config, log, args) -> int:
    src = Path(args.plan) if args.plan else _reports_dir(cfg) / "plan.json"
    if not src.exists():
        log("error", f"找不到计划文件 {src} —— 先跑 `plan`")
        return 2
    plan = Plan.load(src.read_text(encoding="utf-8"))
    log("info", f"载入计划：{len(plan.ops)} 条（{src}）")

    if not args.yes and cfg.dry_run:
        log("info", "DRY_RUN=1 ⇒ 只演练，不发请求（想真跑：设 DRY_RUN=0）")

    from .v115 import V115, build_client, load_cookie
    throttle = Throttle(cfg=cfg, log=log)
    v = V115(build_client(load_cookie(cfg)), cfg, throttle, log)
    ex = Executor(v, cfg, log)
    result = ex.run(plan, resume=not args.no_resume, max_requests=args.max_requests)

    out = _reports_dir(cfg)
    import datetime as _dt
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    (out / f"run-{stamp}.md").write_text(report_mod.render_run_markdown(result), encoding="utf-8")
    (out / f"run-{stamp}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    log("info", f"完成：执行 {result['done']:,} / 失败 {result['failed']}；"
                f"请求 {result['requests']:,}；用时 {result['elapsed_human']}")
    return 0 if not result["failed"] else 1


def cmd_once(cfg: Config, log, args) -> int:
    from .daemon import run_once
    targets = [t.strip() for t in args.targets.split(",") if t.strip()] if args.targets else None
    res = run_once(cfg, log, targets=targets, execute=(False if args.dry else None))
    log("info", f"增量一轮结束：{json.dumps({k: res.get(k) for k in ('targets', 'done', 'requests')}, ensure_ascii=False)}")
    return 0


def cmd_watch(cfg: Config, log, args) -> int:
    from .daemon import Daemon
    Daemon(cfg, log).loop(max_rounds=args.rounds)
    return 0


def cmd_stats(cfg: Config, log, args) -> int:
    tree, path = _load_tree(cfg)
    st = tree.stats()
    log("info", f"来源：{path}")
    for k, v in st.items():
        log("info", f"  {k}: {v}")
    log("info", f"  深度分布: {tree.depth_histogram()}")
    sub = tree.rebase(cfg.root_path)
    log("info", f"  整理根 `{cfg.root_path}` 下：{len(sub.children_dirs(cfg.root_path)):,} 个子目录 / "
                f"{len(sub.files_of(cfg.root_path)):,} 个散文件")
    return 0


def cmd_doctor(cfg: Config, log, args) -> int:
    from . import junk, number
    problems = 0
    log("info", f"数据目录：{cfg.data_dir}")
    for d in (cfg.data_dir, cfg.log_dir, cfg.data_dir / "reports"):
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / ".wtest").write_text("1", encoding="utf-8")
            (d / ".wtest").unlink()
            log("info", f"  ✅ 可写：{d}")
        except Exception as exc:
            problems += 1
            log("error", f"  ❌ 不可写：{d} —— {exc}")

    try:
        tree, path = _load_tree(cfg)
        log("info", f"  ✅ 目录树：{path}（{tree.stats()['dirs']:,} 目录）")
        if cfg.root_path not in {p.split('/')[-1] for p in tree.dirs}:
            log("warning", f"  ⚠️ 目录树里没有 `{cfg.root_path}` —— 检查 ROOT_PATH")
    except Exception as exc:
        problems += 1
        log("error", f"  ❌ 目录树不可用：{exc}")

    if cfg.cookie:
        log("info", f"  ✅ cookie：来自环境变量（…{cfg.cookie[-8:]}）")
    else:
        from .v115 import CookieMissing, load_cookie
        try:
            ck = load_cookie(cfg)
            log("info", f"  ✅ cookie：来自文件（…{ck[-8:]}）")
        except CookieMissing as exc:
            problems += 1
            log("error", f"  ❌ {exc}")

    try:
        import p115client  # noqa: F401
        log("info", f"  ✅ p115client 已安装")
    except ImportError:
        problems += 1
        log("error", "  ❌ 缺 p115client（pip install p115client）")

    from .daemon import validate_cron
    cron_problems = validate_cron(cfg.schedule_cron)
    if cron_problems:
        problems += 1
        log("error", f"  ❌ SCHEDULE_CRON=`{cfg.schedule_cron}`：{'；'.join(cron_problems)}")
        log("error", "     ⇒ `watch` 会拒绝启动（这个计划永远触发不了）")
    else:
        log("info", f"  ✅ SCHEDULE_CRON=`{cfg.schedule_cron}` 可解析")

    log("info", f"  ✅ 垃圾判定自检：{junk.judge('manko.fun.mp4').level} / "
                f"{junk.judge('screens.jpg').level} / {junk.judge('DLDSS-532.mp4').level}")
    log("info", f"  ✅ 番号识别自检：{number.get_id('ipzz-916ch')} / "
                f"{number.get_id('第一會所新片@SIS001@300MIUM-1446')}")
    if cfg.dry_run:
        log("warning", "  ⚠️ 当前 DRY_RUN=1 —— 不会真动 115 上的东西")
    log("info", f"结论：{'全部通过' if not problems else f'{problems} 个问题待修'}")
    return 1 if problems else 0


def cmd_clear_state(cfg: Config, log, args) -> int:
    p = Path(cfg.data_dir) / "run-state.json"
    if p.exists():
        p.unlink()
        log("info", f"已清除断点 {p}")
    else:
        log("info", "没有断点文件")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="115organize", description="115 网盘慢速整理工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="用目录树算出方案（不联网）")
    sp.add_argument("--out", help="输出目录（默认 DATA_DIR/reports）")
    sp.add_argument("--depth", type=int, default=2, help="每个目录往下扫几层（默认 2）")
    sp.set_defaults(func=cmd_plan)

    sr = sub.add_parser("run", help="执行计划")
    sr.add_argument("--plan", help="计划文件（默认 DATA_DIR/reports/plan.json）")
    sr.add_argument("--yes", action="store_true", help="即使 DRY_RUN=1 也问都不问（不改变其语义）")
    sr.add_argument("--no-resume", action="store_true", help="忽略断点、从头跑")
    sr.add_argument("--max-requests", type=int, default=None, help="最多发多少个请求就收工（试跑用）")
    sr.set_defaults(func=cmd_run)

    so = sub.add_parser("once", help="在线增量跑一轮（新增目录 / 最近接收）")
    so.add_argument("--targets", help="只处理这些目录名（逗号分隔）")
    so.add_argument("--dry", action="store_true", help="只演练")
    so.set_defaults(func=cmd_once)

    sw = sub.add_parser("watch", help="常驻，按 SCHEDULE_CRON 到点跑")
    sw.add_argument("--rounds", type=int, default=None, help="只跑这么多轮就退出（测试用）")
    sw.set_defaults(func=cmd_watch)

    ss = sub.add_parser("stats", help="只看目录树统计")
    ss.set_defaults(func=cmd_stats)

    sd = sub.add_parser("doctor", help="环境自检")
    sd.set_defaults(func=cmd_doctor)

    sc = sub.add_parser("clear-state", help="清断点")
    sc.set_defaults(func=cmd_clear_state)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load()
    _, log = setup_log(cfg)
    try:
        return args.func(cfg, log, args)
    except Exception as exc:                     # 顶层兜底：把原因说清楚，别只丢堆栈
        log("error", f"{type(exc).__name__}: {exc}")
        if cfg.log_level == "DEBUG":
            raise
        return 1
