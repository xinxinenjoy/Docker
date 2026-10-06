"""命令行入口。

```
python -m app serve     # ★ 网页界面（改参数 / 看计划 / 勾一部分先跑）
python -m app plan      # 用目录树算出方案（不联网），出报告 + plan.json
python -m app run       # 执行（默认读 plan.json；DRY_RUN=1 时一个请求都不发）
python -m app once      # 入站口整理跑一轮（处理「待整理」目录里的东西）
python -m app watch     # 常驻，轮询「待整理」目录，有货就处理
python -m app stats     # 只看目录树的规模统计
python -m app doctor    # 环境自检：cookie / 树文件 / 目录写权限 / 依赖
python -m app clear-state  # 清断点（重跑整轮时用）
```

⚠️ 网页是**后加**的，命令行的每一条都还在、语义也没变 ——
   定时任务和排障仍然走 CLI（`watch` 不依赖网页在不在跑）。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import report as report_mod
from . import treefile
from .config import Config, load
from .executor import Executor
from .plan import Plan, build_plan, estimate
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


def cmd_plan(cfg: Config, log, args) -> int:
    tree, path = _load_tree(cfg)
    log("info", f"目录树：{path}（{tree.stats()['dirs']:,} 目录 / {tree.stats()['files']:,} 文件）")
    plan = build_plan(tree, cfg, max_depth=args.depth)
    est = estimate(plan, cfg)
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
    res = run_once(cfg, log, execute=(False if args.dry else None))
    log("info", f"入站口一轮结束：{json.dumps({k: res.get(k) for k in ('targets', 'done', 'requests')}, ensure_ascii=False)}")
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
    from . import junk, number, settings as settings_mod
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

    # ---- 参数：网页改的那份 ----
    cfg_path = settings_mod.config_path(cfg.data_dir)
    if cfg_path.exists():
        try:
            import json as _json
            n = len(_json.loads(cfg_path.read_text(encoding="utf-8")))
            log("info", f"  ✅ 参数文件：{cfg_path}（改了 {n} 项）")
        except Exception as exc:
            problems += 1
            log("error", f"  ❌ 参数文件坏了：{cfg_path} —— {exc}（删掉它就回到出厂默认）")
    else:
        log("info", f"  ✅ 参数文件：{cfg_path}（还没建，全用出厂默认）")
    locked = sorted({f["env"] for f in settings_mod.FIELDS if os.environ.get(f["env"], "").strip()})
    if locked:
        log("warning", f"  ⚠️ 这些参数被**环境变量**钉住了，网页上改不动：{', '.join(locked)}"
                       f"（想全交给网页，就在 .env 里把它们留空）")

    try:
        tree, path = _load_tree(cfg)
        log("info", f"  ✅ 目录树：{path}（{tree.stats()['dirs']:,} 目录）")
        if cfg.root_path not in {p.split('/')[-1] for p in tree.dirs}:
            log("warning", f"  ⚠️ 目录树里没有 `{cfg.root_path}` —— 检查 ROOT_PATH")
    except Exception as exc:
        problems += 1
        log("error", f"  ❌ 目录树不可用：{exc}")

    # ---- 账号：优先说清 cookie 是从哪来的 ----
    from .v115 import CookieMissing, load_cookie, read_accounts
    accs = read_accounts(Path(cfg.accounts_file))
    log("info", f"  ℹ️ 账号文件：{cfg.accounts_file}"
                f"（{'有 ' + str(len(accs)) + ' 个账号' if accs else '没有/读不出来'}）")
    try:
        ck = load_cookie(cfg)
        log("info", f"  ✅ cookie：{getattr(cfg, 'cookie_source', '来源未知')}（…{ck[-8:]}）")
    except CookieMissing as exc:
        problems += 1
        log("error", f"  ❌ {str(exc).splitlines()[0]}")
        log("error", f"     ⇒ 推荐在 115offline 里扫码登录一次（两个容器挂同一个 /data）")

    try:
        import p115client  # noqa: F401
        log("info", "  ✅ p115client 已安装")
    except ImportError:
        problems += 1
        log("error", "  ❌ 缺 p115client（pip install p115client）")

    try:
        import fastapi  # noqa: F401
        log("info", f"  ✅ 网页依赖已装（端口 {os.environ.get('PORT') or 8766}）")
        if not (os.environ.get("ACCESS_TOKEN") or "").strip():
            log("warning", "  ⚠️ 没设 ACCESS_TOKEN —— 网页不加口令。内网自用可以，映射到公网必须设")
    except ImportError:
        log("warning", "  ⚠️ 没装 fastapi/uvicorn —— `serve`（网页界面）起不来；"
                       "命令行功能不受影响（pip install fastapi uvicorn）")

    if cfg.dry_run:
        log("warning", "  ⚠️ 当前 DRY_RUN=1 —— 不会真动 115 上的东西")
    log("info", f"  ℹ️ 入站口：`{cfg.inbox_dir}`（每 {cfg.inbox_poll_interval:.0f} 秒轮询一次；"
                f"单次请求上限 {cfg.inbox_max_requests or '不限'}）")
    log("info", f"结论：{'全部通过' if not problems else f'{problems} 个问题待修'}")
    return 1 if problems else 0


def cmd_serve(cfg: Config, log, args) -> int:
    """起网页界面。

    网页管「手工整理 + 调参 + 入站口状态」；容器默认动作是 `serve`（见 Dockerfile）。
    `watch` 是另一条常驻的路 —— 想要自动整理，再开一个容器跑它、共用 `/data`。
    """
    from .web import serve
    log("info", "启动网页界面（Ctrl-C 或停容器即退出）")
    serve(host=args.host, port=args.port)
    return 0


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

    sv = sub.add_parser("serve", help="★ 起网页界面（改参数 / 看计划 / 勾一部分先跑）")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=None, help="默认读 PORT（8766）")
    sv.set_defaults(func=cmd_serve)

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

    so = sub.add_parser("once", help="入站口整理跑一轮（处理「待整理」目录）")
    so.add_argument("--dry", action="store_true", help="只演练")
    so.set_defaults(func=cmd_once)

    sw = sub.add_parser("watch", help="常驻，轮询「待整理」目录，有货就处理")
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
