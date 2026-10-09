"""配置 —— 三层覆盖（显式环境变量 > DATA_DIR/config.json > 出厂默认）。

改参数不用碰代码、不用重建容器：网页「设置」页改的就是 `config.json`。

⭐ 2026-10-08 起有一类**例外**：「网页专属」字段（`STRM_PREFIX`）**不读环境变量**，
   只由网页填、只落 `config.json`（定义见 `settings.WEB_ONLY_ENVS`）。

⚠️ 与 115organize / 115offline 保持同一套写法 —— 一处学一次，三处都认。
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

# `config.json` 灌进来的覆盖值，键是**环境变量名**（⛔ 不是字段名）。
_OVERRIDES: dict[str, str] = {}
_OVERRIDES_LOADED = False


def _env_raw(name: str, default: str = "") -> str:
    """只看环境变量 —— 给「引导项」用，绕过 config.json。"""
    return (os.environ.get(name) or default).strip()


def bootstrap_data_dir() -> Path:
    """`DATA_DIR` —— 引导项，**只认环境变量**。

    ⚠️ 不能让 `data_dir` 也去读 `config.json`：它就住在 `DATA_DIR` 里，自我引用绕不出来。
    """
    return Path(_env_raw("DATA_DIR") or (PROJECT_DIR / "data"))


def reload_overrides() -> dict[str, str]:
    """重新读一遍 `config.json`。网页保存完、或测试里改了配置时要调。"""
    global _OVERRIDES, _OVERRIDES_LOADED
    _OVERRIDES = settings.load_json(bootstrap_data_dir())
    _OVERRIDES_LOADED = True
    return _OVERRIDES


def _env(name: str, default: str = "") -> str:
    """三层覆盖：显式环境变量 > config.json > 出厂默认。

    ⚠️ 「网页专属」字段（`settings.WEB_ONLY_ENVS`，如 `STRM_PREFIX`）**跳过第一步** ——
       `os.environ` 里就算有也当没看见。这是红领巾 2026-10-08 定的：
       「strm 的前缀应该从启动参数里去掉 … 编辑并保存之后存储在 data 目录下」。
       遗留的旧环境变量由 `settings.migrate_web_only_from_env()` 搬进 config.json，
       并在网页上报 `stale_env`，**绝不静默忽略**。
    """
    if name not in settings.WEB_ONLY_ENVS:
        raw = (os.environ.get(name) or "").strip()
        if raw:
            return raw                   # ① 显式环境变量优先（网页专属字段除外）
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


# --------------------------------------------------------------- 媒体扩展名
# ⚠️ **只放视频扩展名** —— 这是 2026-10-08 真机部署后纠正的一处**设计缺陷**。
#
#    原来把字幕 / 元数据 / 图片也放了进来（`ass,srt,ssa,sub,idx,vtt,sup` + `nfo,jpg,png,webp`），
#    注释写着「部分播放器认外挂字幕」「配合刮削」—— **那是错的**：
#
#      媒体服务器读字幕 / nfo / 海报，靠的是**与视频同目录同名的真实文件**
#      （`movie.mkv` 旁边的 `movie.zh.ass`、`movie.nfo`、`movie-poster.jpg`），
#      它**不认** `movie.zh.ass.strm`。生成出来的 strm 纯属摆设，
#      还让媒体库里多出一堆垃圾条目。
#
#    实测：本盘首轮跑出 867 个 strm，其中 **42 个是这类无用件**
#          （ass 32 / srt 6 / sup 2 / png 2），真正的视频只有 825 个。
#
#    ⇒ 字幕 / nfo / 海报的正确做法是「**下载到本地**」（同仓库的 OpenStrm 就是这么做的），
#      不是生成 strm。那是另一个功能，不该塞进这个白名单里。
#    ⚠️ 想收别的格式（如 `.iso` 蓝光原盘）自己在设置里加。
VIDEO_EXT_DEFAULT = (
    "mp4,mkv,avi,ts,m2ts,mov,wmv,flv,rmvb,rm,webm,mpg,mpeg,vob,3gp,m4v"
)

# --------------------------------------------------------------- 导出目录树的层级
# 🔴 2026-10-08 红领巾二轮定调：「每次导出的目录树一定要选足够的层级」。
#
#    背景事实（他实测出来的，也是上一版一个判断错误的根源）：
#      他手动在网页版导那份树时**层级选得太少**，很多子目录根本没被导出来，
#      于是它们在上层表现为「既没有子项、又没有扩展名」的**叶子条目**。
#      上一版把这 386 个条目当「系列下还没放片的空壳」建成了空目录 —— **错了**，
#      它们不是空的，多半是**被截断的子树**（也可能真是无扩展名文件，判据见 `treefile`）。
#
#    取值依据（p115client 源码原文，不是推测）：
#      · `P115Client.fs_export_dir` 的文档：`layer_limit: int = <default> 💡 层级深度，自然数`
#      · `p115client/tool/export_dir.py:401`：`层级深度，取值范围 0~25`
#      · 同文件 `:426` 的写法是 `if 0 < layer_limit <= 25: payload["layer_limit"] = layer_limit`
#        ⇒ 传 0 等于**不下发这个字段**，VALUE 由 115 的服务端默认值决定（没公开说明）。
#      · 同文件 `:752`（高层封装）写的是 `层级深度，小于等于 0 时不限`。
#
#    ⇒ 两处口径有出入，而「服务端默认到底多深」**没有公开文档**。
#      所以这里**不赌服务端默认** —— 显式传取值域上限 25，把「足够深」这件事钉死：
#      任何媒体库也不可能超过 25 层（25 层够放 `电影/系列/季/集/…` 反复套十几轮）。
EXPORT_LAYER_LIMIT = 25


@dataclass
class Config:
    # ---------------------------------------------------------------- 数据 / 输入
    data_dir: Path = field(default_factory=lambda: Path(_env_raw("DATA_DIR") or (PROJECT_DIR / "data")))
    # 目录树导出件落点 —— 工具每轮导出的树存这儿，只留最近 5 份（见 `pipeline._save_tree`）。
    tree_dir: Path = field(default_factory=lambda: Path(_env("TREE_DIR")) if _env("TREE_DIR") else None)

    # ---------------------------------------------------------------- 115 账号
    cookie: str = field(default_factory=lambda: _env("P115_COOKIE"))
    # 独立账号文件 —— 红领巾 2026-10-08 定：**不套用 115offline**，界面上可配。
    accounts_file: Path = field(default_factory=lambda: Path(
        _env("ACCOUNTS_FILE") or (bootstrap_data_dir() / "accounts.json")))
    account_id: str = field(default_factory=lambda: _env("ACCOUNT_ID"))

    # ---------------------------------------------------------------- 网盘范围
    # ⚠️ 以下两个是**树内相对路径**，不是绝对路径。
    #
    # `remote_root` —— 同步根，相对**目录树导出件的根**。
    #   例：树根是 `根目录`，115 影音库在它下面的 `影音`
    #       ⇒ 这里填 `影音`（不是 `根目录/影音`，也不用写绝对路径）。
    #   留空 = 用树根本身。
    #
    #   🔴 **它必须与 `strm_prefix` 配对**：prefix 的末段应当等于这里的末段。
    #      因为 alist 是**按挂载路径**路由的 —— 115 上的 `影音` 挂在 alist 的
    #      `/媒体/影音`，所以 prefix 写 `…/d/媒体/影音`、同步根写 `影音`，
    #      两者的「最后一层」是同一个东西。配错了会用 `_scope_warn()` 报出来。
    #   ⚠️ **出厂为空**（2026-10-09 定）—— 每个用户的网盘目录名都不一样，
    #      默认值不该替用户预设一个；空着就是「整盘都要」，由网页上填。
    remote_root: str = field(default_factory=lambda: _env("REMOTE_ROOT") or "")

    # ★ 只处理这些子目录（**相对 `remote_root`**，逗号或换行分隔；空 = 全都要）。
    #
    # 红领巾 2026-10-08 定：**只处理电影和电视剧，短剧不要**；而且这个范围要在网页上能选。
    # 例：`电影,电视剧` ⇒ 只处理这两棵子树，`短剧` 完全不碰。
    #
    # ⚠️ 勾一个目录 = 处理它**整棵子树**（含所有下级目录）。
    # ⚠️ 白名单里的路径若在树里不存在，会进报告（不静默忽略 —— 否则「明明勾了却没同步」
    #    这种问题极难查）。
    include_dirs: str = field(default_factory=lambda: _env("INCLUDE_DIRS") or "")

    # 🔴 2026-10-08 红领巾二轮定调 —— 撤掉上一版的「无扩展名当目录」开关：
    #
    #    「后续应该不会存在无拓展名的目录。没有具体文件的目录直接抛弃不生成」
    #
    #   背景：上一版把「没有子项、又没有扩展名」的条目**当空目录建出来**（本盘 386 个），
    #   理由是「能一眼看出哪个系列还缺片」。**那个判断是错的** ——
    #   红领巾在网页上导出那份树时**层级选得太少**，很多子目录根本没被导出来，
    #   于是它们在上层表现为「没有子项的叶子条目」。也就是说：
    #
    #       那些「空壳」不是空的，多半是**被截断的子树**（判据见 `treefile.extensionless`）。
    #
    #   ⇒ 正确做法有三条，分别在别处落地：
    #     ① 自动导出时**按足够深的层级导**（见 `v115.export_tree_bytes`）
    #     ② 解析时把「无扩展名的叶子条目」当**疑似截断**报出来（见 `treefile`）
    #     ③ 本地**只建「子树里至少有一个待生成 strm」的目录**，其余一律不建
    #        （见 `sync.Syncer`）—— 于是「空目录也建」这个特性整体作废

    # ---------------------------------------------------------------- strm 产出
    # strm 落点（容器内路径）。宿主机映射到一个你自己的目录（会给媒体服务器扫），
    # 例如 `-v /你的目录/115strm:/out`。
    output_dir: Path = field(default_factory=lambda: Path(_env("OUTPUT_DIR") or "/out"))
    # ★ strm 内容前缀 —— **网页专属字段，出厂为空**（2026-10-08 红领巾拍板）。
    #
    #   旧版这里内置了一个作者自己的 alist 地址当初值，两个毛病：
    #     ① 别人不配就用上了作者的地址（既不合逻辑、也不该把真域名写进代码）
    #     ② 它同时还能被 `.env` 覆盖 ⇒ 值有两个家，出问题不知道看哪
    #   ⇒ 现在：**不读环境变量、出厂空串**，只由网页填、只落 `data/config.json`。
    #     空着时 `pipeline` 会在开跑前**直接拦住**并告诉你去哪填（不会静默产出坏 URL）。
    #
    #   🔴 填的时候**必须覆盖到「alist 挂载点的完整路径」**，这是最易错的一项：
    #      alist 的路由是**按挂载路径**匹配的。115 挂在 `/媒体/影音`，
    #      所以必须是 `<域名>/d/媒体/影音`；少写一层（`/d`）alist 会回
    #      `{"code":500,"message":"storage not found; rawPath: …"}` —— **播不了**，
    #      而 HTTP 状态码仍是 **200**，看着不像错。
    #   ⛔ 必须用 `/d`（alist 原生下载端点）；`/dav` 实测 401（WebDAV 端点要认证）。
    strm_prefix: str = field(default_factory=lambda: _env("STRM_PREFIX").rstrip("/"))
    # 生成哪些扩展名（小写、逗号分隔）
    video_ext: str = field(default_factory=lambda: _env("VIDEO_EXT") or VIDEO_EXT_DEFAULT)
    # 🔒 内部旋钮（**不出现在网页上**，只能用环境变量改）：路径要不要逐段 URL 编码。
    #    实测两种都能播，编码后更稳（代理不会二次误解）；留着是为了测试能比对明文路径。
    url_encode: bool = field(default_factory=lambda: _flag("URL_ENCODE", True))
    # ---------------------------------------------------------------- 同步策略
    # ⭐ **只有一条路**（2026-10-08 红领巾定）：
    #
    #   「**事件流、实例都去掉**，因为日常的定时处理，**通过系统自带的导出目录树
    #     已经完全足够了**。」
    #
    #   ⇒ 工具**自己导出目录树 → 解析 → 对照本地 → 只写差异**。
    #     原来那个「默认走哪条路」的 `sync_mode`，连同 `events` / `api` / `tree`
    #     三个模式一起删掉了。既然没有可选的，留个开关只会让人以为还有别条路 ——
    #     而且那个开关以前真的误导过人（网页和命令行各写一遍判断，分叉出 400 错误）。
    #
    # 定架构时比较过两条路（这才是判据）：
    #
    #    路 A 逐层实时列目录：请求量 ≈ **目录数** —— 本盘 1,183 个目录
    #                        ⇒ 上千次请求 + 节流 ⇒ 十几分钟。**不能做常规路径**。
    #    路 B 自动导出目录树：115 那边**一个导出任务**跑完，
    #                        我们只发「提交 + 轮询 + 下载 + 删临时件」≈ **十几次**请求，
    #                        且是**任务级**而非**目录级**，与库的大小基本无关。
    #                        ⭐ 实测本盘 727 目录 / 6,071 文件：**13 秒**。
    #
    # ⚠️ 导出失败时仍会**退回 `tree/` 里已有的那份**（见 `pipeline.tree_source`）——
    #    那是容错，不是「模式」，界面上不暴露。

    # 自动扫描的 cron（空 = 不自动；给网页上的「定时」用）
    schedule_cron: str = field(default_factory=lambda: _env("SCHEDULE_CRON"))
    # ⭐ 删除判定：本轮发现 X 处删除 ⇒ 等 N 分钟 ⇒ 二次确认才真删。
    #    借鉴 OpenStrm 的做法（一轮里消失超过三成先不删）—— 这里的阈值是**绝对条数**。
    delete_defer_minutes: float = field(default_factory=lambda: _float("DELETE_DEFER_MINUTES", 10.0))
    # 一轮里消失的比例超过这个数 ⇒ 判定为「异常」（可能 cookie 失效/权限变化，不是真删）⇒ 本轮不删
    delete_ratio_guard: float = field(default_factory=lambda: _float("DELETE_RATIO_GUARD", 0.3))
    # 单轮最大请求数（0 = 不限制）。给试跑用。
    max_requests: int = field(default_factory=lambda: _int("MAX_REQUESTS", 0))

    # ---------------------------------------------------------------- 节流（防风控）
    throttle_min: float = field(default_factory=lambda: _float("THROTTLE_MIN", 1.0))
    throttle_max: float = field(default_factory=lambda: _float("THROTTLE_MAX", 3.0))
    throttle_batch: int = field(default_factory=lambda: _int("THROTTLE_BATCH", 200))
    throttle_rest: float = field(default_factory=lambda: _float("THROTTLE_REST", 60.0))
    # 运行时间窗（如 `02:00-06:00`；留空 = 不限）
    window: str = field(default_factory=lambda: _env("WINDOW"))
    fail_limit: int = field(default_factory=lambda: _int("FAIL_LIMIT", 8))
    fail_cooldown: float = field(default_factory=lambda: _float("FAIL_COOLDOWN", 900.0))

    # ---------------------------------------------------------------- 安全阀
    # ⛔ 出厂 1 = 只出方案、一个文件都不写。看过报告确认后再关。
    dry_run: bool = field(default_factory=lambda: _flag("DRY_RUN", True))
    log_dir: Path = field(default_factory=lambda: Path(_env("LOG_DIR") or "."))
    # 🔒 内部旋钮（**不出现在网页上**）：日志级别，用环境变量 `LOG_LEVEL` 调。
    log_level: str = field(default_factory=lambda: (_env("LOG_LEVEL") or "INFO").upper())

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.tree_dir = Path(self.tree_dir) if self.tree_dir else self.data_dir / "tree"
        self.accounts_file = Path(self.accounts_file)
        self.output_dir = Path(self.output_dir)
        raw_log = _env("LOG_DIR")
        self.log_dir = Path(raw_log) if raw_log else self.data_dir / "logs"

    # ------------------------------------------------------------------ 便利方法
    @property
    def exts(self) -> frozenset[str]:
        return frozenset(e.strip().lower().lstrip(".") for e in self.video_ext.split(",") if e.strip())

    @property
    def prefix_ready(self) -> bool:
        """strm 前缀填了没 —— 空着的话**不该产出任何 strm**（见 `pipeline.run_once` 的守卫）。

        ⚠️ 判据只看「非空」：前缀对不对（挂载路径配没配对）只能靠真发一次请求验，
           那是 `health.check_prefix()` 的活，这里不做猜测。
        """
        return bool((self.strm_prefix or "").strip())

    @property
    def includes(self) -> tuple[str, ...]:
        """白名单解析成规整的元组（去空白、去首尾斜杠、去重、保持顺序）。

        用逗号**或换行**分隔 —— 免得要求用户记住只能用逗号。
        """
        raw = (self.include_dirs or "").replace("\r", "\n").replace(",", "\n")
        out: list[str] = []
        for part in raw.split("\n"):
            p = part.strip().strip("/")
            # 规整内部多余斜杠，并做一次「去掉 remote_root 前缀」的容错：
            # ⚠️ 用户在 UI 上可能选到 `影音/电影`（带同步根）而不是 `电影`，
            #    两种都应该认，否则会「勾了却同步不到」。
            while "//" in p:
                p = p.replace("//", "/")
            if not p:
                continue
            root = (self.remote_root or "").strip("/")
            if root and p.startswith(root + "/"):
                p = p[len(root) + 1:]
            if p and p not in out:
                out.append(p)
        return tuple(out)

    def replace(self, **changes) -> "Config":
        """拿一份改了若干参数的副本（给「不改配置试算一下」用）。"""
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
    """每次调用都重读一遍 `config.json` —— 参数会被网页随时改，缓存得不偿失。"""
    reload_overrides()
    return Config()


def bootstrap(log: Any = None) -> dict:
    """**进程启动时调一次**的一次性准备。

    干的唯一一件事：把「网页专属」字段（`settings.WEB_ONLY_ENVS`）遗留在环境变量里的
    旧值**搬进 `config.json`** —— 否则从上一版升上来时，写在 `.env` / compose 里的
    `STRM_PREFIX` 会被直接忽略、前缀静默变空，下一轮就跑不起来了。
    详见 `settings.migrate_web_only_from_env()`。

    ⚠️ **别放进 `load()`** —— 那会让「读配置」带上磁盘写。
    """
    data_dir = bootstrap_data_dir()
    res = settings.migrate_web_only_from_env(data_dir)
    reload_overrides()
    emit = log or (lambda *a, **k: None)
    if res.get("migrated"):
        emit("info", f"已把环境变量里遗留的 {', '.join(res['migrated'])} 搬进 "
                     f"{settings.config_path(data_dir)} —— "
                     f"这些参数从现在起由网页管、不再读环境变量")
    for s in res.get("stale") or []:
        emit("warning", f"⚠️ 环境变量里还留着 {s['env']}（`{s['env_value']}`），"
                        f"但**它已经不生效了** —— 以网页里保存的 {s['key']} 为准。"
                        f"建议从 .env / compose 里删掉这一行，免得下次又绕进来。")
    return res
