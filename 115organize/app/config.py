"""配置 —— 参数有**三层**，改参数不用碰代码、也不用重建容器。

    ① 显式环境变量            —— 临时覆盖 / CI 用
    ② `DATA_DIR/config.json`  —— **网页上改的就是它**（`settings.py` 负责读写）
    ③ 代码里的默认值          —— 出厂

⇒ `.env` 因此可以只剩引导项（`DATA_DIR` / `PORT` / `ORGANIZE_AUTH` / `ACCOUNTS_FILE`），
   业务参数一个都不用写在那儿。

⚠️ 每个默认值都写了「为什么是这个数」，改之前先看注释。
⛔ 本模块**不做任何网络副作用**；唯一的文件读取是「首次用到参数时读一次 config.json」。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from dataclasses import fields as df_fields
from pathlib import Path

from . import settings

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent


# `config.json` 灌进来的覆盖值，键是**环境变量名**（⛔ 不是字段名，见 settings.load_json）。
_OVERRIDES: dict[str, str] = {}
_OVERRIDES_LOADED = False


def _env_raw(name: str, default: str = "") -> str:
    """只看环境变量 —— 给「引导项」用，绕过 config.json。"""
    return (os.environ.get(name) or default).strip()


def bootstrap_data_dir() -> Path:
    """`DATA_DIR` —— 引导项，**只认环境变量**。

    ⚠️ 不能让 `data_dir` 也去读 `config.json`：`config.json` 就住在 `DATA_DIR` 里，
       自我引用会绕不出来。所以它是唯一一个不进 `settings.FIELDS` 的路径参数。
    """
    return Path(_env_raw("DATA_DIR") or (PROJECT_DIR / "data"))


def reload_overrides() -> dict[str, str]:
    """重新读一遍 `config.json`。网页保存完、或测试里改了配置时要调。"""
    global _OVERRIDES, _OVERRIDES_LOADED
    _OVERRIDES = settings.load_json(bootstrap_data_dir())
    _OVERRIDES_LOADED = True
    return _OVERRIDES


def _env(name: str, default: str = "") -> str:
    raw = (os.environ.get(name) or "").strip()
    if raw:
        return raw                       # ① 显式环境变量优先
    if not _OVERRIDES_LOADED:
        reload_overrides()               # ② 懒加载：第一次用到参数时才碰磁盘
    got = _OVERRIDES.get(name)
    if got is not None and str(got).strip():
        return str(got).strip()
    return default                       # ③ 出厂默认


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
    # ⚠️ 引导项：只认环境变量（见 `bootstrap_data_dir` 的注释）。
    data_dir: Path = field(default_factory=lambda: Path(_env_raw("DATA_DIR") or (PROJECT_DIR / "data")))
    # 目录树导出的落点。工具**不扫全盘**，靠这份树知道「盘上该有什么」。
    # ⚠️ 默认必须是 **`DATA_DIR/tree`**，不是「项目目录下的 data/tree」——
    #    容器里 `/app/data/tree` 跟 `/data` 根本不是一个地方（`/data` 才是挂载卷），
    #    症状是「树明明丢进去了，页面却说没有」。以前靠 compose 显式设 TREE_DIR 兜住，
    #    2026-10-06 把业务参数从 .env 撤出时这个隐雷才露出来。
    #    留空 ⇒ `__post_init__` 里解析成 `data_dir / "tree"`。
    tree_dir: Path = field(default_factory=lambda: Path(_env("TREE_DIR")) if _env("TREE_DIR") else None)
    tree_file: str = field(default_factory=lambda: _env("TREE_FILE"))   # 留空 ⇒ 取 tree_dir 里最新的

    # ---------------------------------------------------------------- 115 账号
    # `P115_COOKIE` 仍是最高优先级 —— 但**推荐留空**，让 cookie 走下面的共用路径。
    cookie: str = field(default_factory=lambda: _env("P115_COOKIE") or _env("COOKIE"))
    # 🔗 与 115offline 共用的账号文件。默认就是 `DATA_DIR/accounts.json`
    #    ⇔ 两个容器挂同一个数据卷时，在 115offline 里扫码登录一次，这边直接能用。
    accounts_file: Path = field(default_factory=lambda: Path(
        _env_raw("ACCOUNTS_FILE") or (bootstrap_data_dir() / "accounts.json")))
    # 115offline 里可能有多个账号；留空 = 用第一个能用的那个。
    account_id: str = field(default_factory=lambda: _env("ACCOUNT_ID"))

    # ---------------------------------------------------------------- 整理范围
    # ⚠️ 相对 115 根目录的路径。目录树导出里的一级目录就是它。
    root_path: str = field(default_factory=lambda: _env("ROOT_PATH") or "云下载")
    # tidy 模式的目标根 —— 与「整理根」平级，同在 115 根目录下。
    # 红领巾 2026-10-06 定：按女优细化时，整理好的往「涩涩存档」里放。
    tidy_target: str = field(default_factory=lambda: _env("TIDY_TARGET") or "涩涩存档")

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
    # 清理时**允许动目录**吗？默认允许。
    #
    # 背景：115 的目录树导出**没有类型标记**，而本盘大量广告是**目录**（`文宣`、`原创文宣`、
    # `看片资源`、`18禁成人游戏八款附带礼包码`），名字又没扩展名 —— 离线阶段分不清它是
    # 「广告目录」还是「空目录」。两条路只能选一条：
    #
    #   1（默认）允许：判据是**名字**，目录照样适用；而且默认动作是 `quarantine`
    #                  （移进 `_待清理/` 并保留原名），**整目录可撤销** ⇒ 最坏是多移了几个空目录。
    #                  ⛔ 不放开就达不成「把广告清干净」这个诉求（广告外壳会一直留在原地）。
    #   0      保守：清理**只对文件**生效。广告目录会被跳过，并在日志/回执里报出条数，
    #                  你自己看过再决定要不要放开。适合：`JUNK_ACTION=delete`（不可逆）时。
    junk_allow_dir: bool = field(default_factory=lambda: _flag("JUNK_ALLOW_DIR", True))

    # ---------------------------------------------------------------- 节流（防风控）
    # 红领巾 2026-10-06：「3-5 秒的单任务间隔 + 处理一部分之后暂停」——两级。
    throttle_min: float = field(default_factory=lambda: _float("THROTTLE_MIN", 3.0))
    throttle_max: float = field(default_factory=lambda: _float("THROTTLE_MAX", 5.0))
    throttle_batch: int = field(default_factory=lambda: _int("THROTTLE_BATCH", 200))
    throttle_rest: float = field(default_factory=lambda: _float("THROTTLE_REST", 60.0))
    # tidy 批处理参数（红领巾 2026-10-06 定：一次 3-5 个、批间长休、别太快）
    tidy_batch: int = field(default_factory=lambda: _int("TIDY_BATCH", 3))
    tidy_batch_rest: float = field(default_factory=lambda: _float("TIDY_BATCH_REST", 30.0))
    # 运行时间窗（如 `02:00-06:00`；留空 = 不限）。窗外不发起请求、就地等。
    window: str = field(default_factory=lambda: _env("WINDOW"))
    # 连续失败到这个数就停手冷却（风控/网络抖动的共同症状都是连续失败）
    fail_limit: int = field(default_factory=lambda: _int("FAIL_LIMIT", 8))
    fail_cooldown: float = field(default_factory=lambda: _float("FAIL_COOLDOWN", 900.0))

    # ---------------------------------------------------------------- 入站口（自动整理）
    # 自动整理只监控这一个目录（相对整理根）。你只把要整理的东西丢进来，
    # 工具处理完就移走 —— 所以不需要快照 diff，也不会碰盘上其它地方。
    # 默认「待整理」；想用别的名字（如「新下载」）改这里 + 网页设置即可。
    inbox_dir: str = field(default_factory=lambda: _env("INBOX_DIR") or "待整理")
    # 入站口轮询间隔（秒）。115 没有 webhook，只能轮询 ——
    # 每次轮询 = 列一次入站目录（1 个请求）。间隔别设太短：建议 ≥30 秒。
    inbox_poll_interval: float = field(default_factory=lambda: _float("INBOX_POLL_INTERVAL", 60.0))
    # 单次入站整理最大请求数（试跑/防风控用）。0 = 不限制。
    inbox_max_requests: int = field(default_factory=lambda: _int("INBOX_MAX_REQUESTS", 0))

    # ---------------------------------------------------------------- 扫描节流（功能 2）
    # 「选定文件夹」的**接口扫描**专用节流 —— 只扫用户勾的目录，量小、影响小，
    # 可以比整理快一些。⚠️ 只影响扫描（列目录），**不影响执行**（执行仍用 THROTTLE_*）。
    scan_throttle_min: float = field(default_factory=lambda: _float("SCAN_THROTTLE_MIN", 0.5))
    scan_throttle_max: float = field(default_factory=lambda: _float("SCAN_THROTTLE_MAX", 1.5))

    # ---------------------------------------------------------------- 安全阀
    # ⛔ 出厂默认 1 = 只出方案不动手。第一次全量要动 2000+ 个目录，不可逆 ——
    #    看过 `plan` 报告、确认没问题，把 DRY_RUN 改 0 再真跑。
    dry_run: bool = field(default_factory=lambda: _flag("DRY_RUN", True))
    log_dir: Path = field(default_factory=lambda: Path(_env("LOG_DIR") or "."))
    log_level: str = field(default_factory=lambda: (_env("LOG_LEVEL") or "INFO").upper())

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        # 留空 ⇒ 跟着数据目录走（见字段定义处的说明）
        self.tree_dir = Path(self.tree_dir) if self.tree_dir else self.data_dir / "tree"
        self.accounts_file = Path(self.accounts_file)
        # ⚠️ 不能写 `if not str(self.log_dir)` —— `Path("")` 的字符串是 `"."`，
        #    恒为真，于是日志会落到当前工作目录（`doctor` 里那个「✅ 可写：.」）。
        raw_log = _env("LOG_DIR")
        self.log_dir = Path(raw_log) if raw_log else self.data_dir / "logs"

    # ------------------------------------------------------------------ 便利方法
    def rel(self, *parts: str) -> str:
        """拼出相对 root_path 的路径（用 115 的 `/` 分隔）。"""
        return "/".join(p.strip("/") for p in (self.root_path, *parts) if p and p.strip("/"))

    def replace(self, **changes) -> "Config":
        """拿一份改了若干参数的副本 —— 给「不改配置试算一下」用（如网页上改完参数先预览计划）。

        ⚠️ 只挑**认识的**键：外部可能挂过临时属性（`load_cookie` 会挂 `cookie_source`），
           直接 `Config(**vars(self))` 会因为多出关键字参数而 TypeError。
        """
        known = {f.name for f in df_fields(self)}
        data = {k: v for k, v in vars(self).items() if k in known}
        data.update({k: v for k, v in changes.items() if k in known})
        return Config(**data)

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
    """每次调用都重读一遍 `config.json`。

    为什么不做缓存：本工具不是高频服务，但**参数会被网页随时改**；
    缓存反而要维护「什么时候失效」，得不偿失（实测读一次 JSON 是微秒级）。
    """
    reload_overrides()
    return Config()
