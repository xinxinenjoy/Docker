"""配置 —— 全部走环境变量，容器里改 env 即可，不用动代码。

⚠️ 每个默认值都写了「为什么是这个数」，改之前先看注释。
⛔ 本模块**不做任何网络/文件副作用**，只读环境变量（配置文件读取放在 `v115`/`cli` 里）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _flag(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on", "y")


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# --------------------------------------------------------------- 分级垃圾判定
# 三级分类（红领巾 2026-10-06 拍板「分级处理」）：
#   · JUNK_STRONG   —— 强特征，可自动处理
#   · JUNK_KEEP     —— 白名单，**永不碰**（正规封面 / 字幕 / nfo 全在这）
#   · 其余疑似      —— 只进报告，等人看
#
# ⚠️ 白名单为什么必须存在：2026-10-06 拿真实目录树跑过 —— 用「图片名像广告」这种粗规则
#    会把 `screens.jpg`(120 次) / `poster.jpg`(119 次) / `1.jpg`(117 次) 一起判成垃圾，
#    而这些是**正规封面**。所以本工具是「白名单优先」：命中白名单直接放行，不再看黑名单。
KEEP_EXT = frozenset((
    "nfo", "srt", "ass", "ssa", "sub", "idx", "vtt", "sup",   # 元数据 / 字幕
    "jpg", "jpeg", "png", "webp", "bmp", "gif",               # 图片（默认全保留，见 JUNK_KEEP_IMAGES）
    "iso", "vob",                                             # 整盘镜像
))

# 图片要不要一起保护 —— 默认保护。理由：本盘实测 684 个 jpg 命中粗垃圾规则，
# 其中大半是正规封面；为了不误删，图片一律进白名单，疑似广告图只在报告里列出来。
KEEP_IMAGES = True


@dataclass
class Config:
    # ---------------------------------------------------------------- 数据 / 输入
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR") or (PROJECT_DIR / "data")))
    # 目录树导出的落点。工具**不扫全盘**，靠这份树知道「盘上该有什么」。
    tree_dir: Path = field(default_factory=lambda: Path(_env("TREE_DIR") or (PROJECT_DIR / "data" / "tree")))
    tree_file: str = field(default_factory=lambda: _env("TREE_FILE"))   # 留空 ⇒ 取 tree_dir 里最新的

    # ---------------------------------------------------------------- 115 账号
    cookie: str = field(default_factory=lambda: _env("P115_COOKIE") or _env("COOKIE"))

    # ---------------------------------------------------------------- 整理范围
    # ⚠️ 相对 115 根目录的路径。目录树导出里的一级目录就是它。
    root_path: str = field(default_factory=lambda: _env("ROOT_PATH") or "云下载")

    # ---------------------------------------------------------------- 番号系列
    series_enable: bool = field(default_factory=lambda: _flag("SERIES_ENABLE", True))
    # 系列内 ≥N 部才聚合。为什么默认 3：实测 548 个系列里 262 个（48%）**只出现 1 次**，
    # 给这些建目录纯属制造碎片 —— 阈值把长尾挡在外面。
    series_min: int = field(default_factory=lambda: _int("SERIES_MIN", 3))
    # 番号目录名要不要一并规范成 `DLDSS-532`（顺带去掉 -ch / -U / [4K]@站点 等尾巴）
    series_rename: bool = field(default_factory=lambda: _flag("SERIES_RENAME", True))

    # ---------------------------------------------------------------- 影视
    movie_enable: bool = field(default_factory=lambda: _flag("MOVIE_ENABLE", True))
    # 剧集：把散落的集文件收进 `剧名（年份）/第N季/`
    episode_group_enable: bool = field(default_factory=lambda: _flag("EPISODE_GROUP_ENABLE", True))
    # 同一部剧至少有这么多集**散落在根目录**才收拢（1 集不值得动）
    episode_group_min: int = field(default_factory=lambda: _int("EPISODE_GROUP_MIN", 2))
    # 括号全角（红领巾 2026-10-06：全角是他习惯的写法）
    paren: str = field(default_factory=lambda: _env("PAREN") or "full")

    # ---------------------------------------------------------------- 垃圾清理
    # report=只报告 | quarantine=移到 QUARANTINE_DIR | delete=删（进 115 回收站）
    junk_action: str = field(default_factory=lambda: (_env("JUNK_ACTION") or "quarantine").lower())
    # 强特征候选还要满足「跨目录重复 ≥N 次」才自动处理（1 次性的疑似件只报告）。
    # 为什么：本盘实测 1853 个疑似垃圾里，重复 ≥20 次的头部全是铁证（'游戏大全' 180 次、
    # '聚合全網H直播' 119 次），而只出现一两次的里面混着**正经作品**（如 `madoubt.com 982886.xyz HMN-870`）。
    junk_dup_min: int = field(default_factory=lambda: _int("JUNK_DUP_MIN", 5))
    # 隔离目录名（相对 root_path）。选它而不是直接删：**可撤销**是这里的第一原则。
    quarantine_dir: str = field(default_factory=lambda: _env("QUARANTINE_DIR") or "_待清理")

    # ---------------------------------------------------------------- 节流（防风控）
    # 红领巾 2026-10-06：「3-5 秒的单任务间隔 + 处理一部分之后暂停」——两级。
    throttle_min: float = field(default_factory=lambda: _float("THROTTLE_MIN", 3.0))
    throttle_max: float = field(default_factory=lambda: _float("THROTTLE_MAX", 5.0))
    throttle_batch: int = field(default_factory=lambda: _int("THROTTLE_BATCH", 200))
    throttle_rest: float = field(default_factory=lambda: _float("THROTTLE_REST", 60.0))
    # 运行时间窗（如 `02:00-06:00`；留空 = 不限）。窗外不发起请求、就地等。
    window: str = field(default_factory=lambda: _env("WINDOW"))
    # 连续失败到这个数就停手冷却（风控/网络抖动的共同症状都是连续失败）
    fail_limit: int = field(default_factory=lambda: _int("FAIL_LIMIT", 8))
    fail_cooldown: float = field(default_factory=lambda: _float("FAIL_COOLDOWN", 900.0))

    # ---------------------------------------------------------------- 自动化
    # `0 4 * * *` = 每天 04:00（低峰）。⚠️ 改频率前先想风控 —— 这是**全盘/增量**级别任务。
    schedule_cron: str = field(default_factory=lambda: _env("SCHEDULE_CRON") or "0 4 * * *")
    # 每次巡检只处理「最近接收」里最近 N 天出现过的目录（0 = 不按时间过滤）
    recent_days: int = field(default_factory=lambda: _int("RECENT_DAYS", 3))
    # 「最近接收」轮询本身也要节流，别每秒一次
    recent_poll_interval: float = field(default_factory=lambda: _float("RECENT_POLL_INTERVAL", 0.0))

    # ---------------------------------------------------------------- 安全阀
    # ⛔ 出厂默认 1 = 只出方案不动手。第一次全量要动 2000+ 个目录，不可逆 ——
    #    看过 `plan` 报告、确认没问题，把 DRY_RUN 改 0 再真跑。
    dry_run: bool = field(default_factory=lambda: _flag("DRY_RUN", True))
    log_dir: Path = field(default_factory=lambda: Path(_env("LOG_DIR") or "."))
    log_level: str = field(default_factory=lambda: (_env("LOG_LEVEL") or "INFO").upper())

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.tree_dir = Path(self.tree_dir)
        # ⚠️ 不能写 `if not str(self.log_dir)` —— `Path("")` 的字符串是 `"."`，
        #    恒为真，于是日志会落到当前工作目录（`doctor` 里那个「✅ 可写：.」）。
        raw_log = _env("LOG_DIR")
        self.log_dir = Path(raw_log) if raw_log else self.data_dir / "logs"

    # ------------------------------------------------------------------ 便利方法
    def rel(self, *parts: str) -> str:
        """拼出相对 root_path 的路径（用 115 的 `/` 分隔）。"""
        return "/".join(p.strip("/") for p in (self.root_path, *parts) if p and p.strip("/"))

    def all(self) -> dict:
        """给报告/日志用的一份可打印快照（cookie 只留尾巴）。"""
        out: dict = {}
        for key, value in vars(self).items():
            if key == "cookie":
                out[key] = f"…{value[-8:]}" if value else "(未配置)"
                continue
            out[key] = str(value) if isinstance(value, Path) else value
        return out


def load() -> Config:
    return Config()
