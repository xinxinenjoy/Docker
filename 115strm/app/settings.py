"""参数配置层 —— 让「改参数」不用碰 `.env`、也不用重建容器。

落点 `DATA_DIR/config.json`；优先级：显式环境变量 > config.json > 出厂默认。

⭐ 本表只写**元信息**（环境变量名 / 类型 / 分组 / 取值域 / 文案），
   **默认值一律从 `Config` 实例上取** ⇒ 「出厂值」全局只有一个真相源。
   ⛔ 别在 HTML 里再抄一份（两处各写一份必然分叉，115organize 已踩过）。

## 🔴 「网页专属」字段（`web_only=True`）—— 2026-10-08 红领巾要求

    「strm 的前缀应该从启动参数里去掉，首次配置时为空，编辑并保存之后存储在 data 目录下，
      还有其他的一些参数，都应该存储在这里」

⇒ `strm_prefix` 这类字段：
   · **不读环境变量**（`config._env()` 对 `WEB_ONLY_ENVS` 里的名字直接跳过 `os.environ`）
   · 出厂**空值** —— 首次进网页时它就该是空的，让人当场填，而不是悄悄用某个内置地址
   · 值只存在 `data/config.json` 里 ⇒ 跟着 data 目录走（备份 / 迁移 / 换机都只搬它）

⚠️ 但**不能静默丢弃**环境变量里的旧值 —— 升级到本版时，上一版写在 `.env` / compose 里的
   `STRM_PREFIX` 会被忽略、前缀突然变空 ⇒ 下一轮直接跑不起来。
   ⇒ 所以有 `migrate_web_only_from_env()`：启动时把 `.env` 里遗留的值**搬进 config.json**，
     搬完在网页上给出「`.env` 里还留着、已忽略、建议删掉」的提示（`stale_env`）。

⚠️ 本模块不 import `config`（会成环）⇒ `describe()` 把 Config 实例当参数收进来。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

__all__ = [
    "CONFIG_NAME", "FIELDS", "GROUPS", "FIELD_BY_KEY", "WEB_ONLY_ENVS",
    "REQUIRED_KEYS", "config_path", "load_json", "save_json", "reset_json",
    "describe", "coerce", "migrate_web_only_from_env",
]

CONFIG_NAME = "config.json"

_CRON_RE = re.compile(r"^\S+(\s+\S+){4}$")
_WINDOW_RE = re.compile(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$")


def _f(key: str, env: str, kind: str, group: str, label: str, help_: str = "", **kw) -> dict:
    d = {"key": key, "env": env, "type": kind, "group": group, "label": label, "help": help_}
    d.update(kw)
    return d


FIELDS: list[dict] = [
    # ---------------------------------------------------------------- 账号
    _f("account_id", "ACCOUNT_ID", "account", "115 账号", "使用哪个账号",
       "从账号文件里挑。留空 = 用第一个能用的。"),

    # ---------------------------------------------------------------- 网盘范围
    # ⚠️ 这两个字段在网页上有**专门的目录选择器**（「同步范围」卡片），
    #    不放在普通表单里 —— 但必须登记在这，否则 `save_json` 会把它们当未知键拒掉。
    _f("remote_root", "REMOTE_ROOT", "str", "同步范围", "同步根目录",
       "相对**目录树导出件**的根。例：树根是「根目录」、影音库在「影音」⇒ 填 `影音`。"
       "⚠️ 它必须与 strm 前缀**配对**（末段同名），配错时 alist 回 storage not found 但状态码是 200。",
       placeholder="影音"),
    _f("include_dirs", "INCLUDE_DIRS", "str", "同步范围", "只处理这些子目录",
       "相对同步根，逗号或换行分隔；**留空 = 全部都要**。"
       "勾一个目录 = 处理它整棵子树。", placeholder="电影,电视剧"),

    # ---------------------------------------------------------------- 输出
    # ★ 本字段是「网页专属」—— 见模块顶部说明。
    _f("strm_prefix", "STRM_PREFIX", "str", "输出", "strm 内容前缀",
       "strm 里写的地址开头，**必填**。必须用 alist 的 `/d` 端点（`/dav` 要认证，播不了），"
       "并且要写到**挂载路径的完整一层**（少一层 alist 会回 storage not found，"
       "而状态码还是 200，极难察觉）。内网外网都要能用就填公网域名。"
       "⚠️ 它**只存在 `data/config.json`**（不再认 `.env`），首次使用是空的。",
       placeholder="https://alist.example.com:5244/d/媒体/影音",
       web_only=True, required=True),
    _f("output_dir", "OUTPUT_DIR", "path", "输出", "strm 落点",
       "容器内路径。宿主机上挂一个你自己的目录给它（媒体服务器会扫这个目录）。", advanced=True),
    _f("video_ext", "VIDEO_EXT", "str", "输出", "生成哪些扩展名",
       "小写、逗号分隔。**只放视频** —— 媒体服务器读字幕 / nfo / 海报靠的是"
       "「与视频同目录同名的真实文件」，它不认 `xxx.srt.strm`，加了只会让媒体库多出垃圾条目。",
       advanced=True),

    # ---------------------------------------------------------------- 自动化
    # 🔴 `dry_run` **必须留在**「非进阶」区 —— 它是**安全阀**，出厂就开着，
    #    要让人一眼看见「现在到底写不写文件」。藏进进阶区等于把安全提示藏起来。
    #
    #    而且它**必须登记在这**（2026-10-08 部署后才发现漏过）：
    #    网页上那个「只演练」开关走的是 `PUT /api/settings {values:{dry_run:…}}`，
    #    而 `save_json` 只认**登记过**的键 ⇒ 漏了就会被静默 reject（HTTP 200，
    #    但 `saved` 里没有它）⇒ `DRY_RUN` 永远是 1
    #    ⇒ **定时任务每轮都只演练、一个文件都不写**，表面上还「跑成功了」。
    _f("dry_run", "DRY_RUN", "bool", "自动化", "只演练（不写文件）",
       "出厂默认**开** —— 一个文件都不写、不删，先在预览里看清它要做什么。"
       "⚠️ **这是持久设置，定时任务也受它管**；不开的话定时任务永远只演练。"
       "确认无误后关掉，它才真的写 strm。"),
    _f("schedule_cron", "SCHEDULE_CRON", "cron", "自动化", "定时扫描",
       "5 段 cron（分 时 日 月 周），如 `0 4 * * *` = 每天 04:00。留空 = 不自动跑。"
       "✅ 定时跑的就是**和「立即同步」一模一样的那一轮**：工具自己导目录树、自己对账、"
       "自己落盘。**你不需要手动导任何东西。**"
       "想把请求压到深夜见进阶里的「运行时间窗」。",
       placeholder="(不自动)"),
    # -------- 以下都是「进阶」：默认值就是稳态，不动它也对 --------
    # 判据：**只有真的会因人而异、且改错了要能看出来**的才配放在上面第一屏。
    _f("delete_defer_minutes", "DELETE_DEFER_MINUTES", "float", "自动化", "删除确认等待",
       "本轮发现有文件消失时，不立即删本地 strm，等这么多分钟后再扫一次确认。",
       min=0, max=1440, unit="分钟", advanced=True),
    _f("delete_ratio_guard", "DELETE_RATIO_GUARD", "float", "自动化", "异常比例阈值",
       "一轮里消失比例超过这个数 ⇒ 判定异常（可能 cookie 失效），本轮**一律不删**。"
       "⚠️ 这是**闸门**：强制删除（`force`）也越不过它 —— 只能改这个阈值。",
       min=0, max=1, step=0.05, advanced=True),

    # ---------------------------------------------------------------- 节流
    _f("window", "WINDOW", "window", "节流", "运行时间窗",
       "如 `02:00-06:00`。窗外不发起请求、就地等。留空 = 不限时段。",
       placeholder="(不限时段)", advanced=True),
    _f("throttle_min", "THROTTLE_MIN", "float", "节流", "单请求最小间隔",
       "两次 115 请求之间至少等这么多秒。", min=0, max=60, unit="秒", advanced=True),
    _f("throttle_max", "THROTTLE_MAX", "float", "节流", "单请求最大间隔",
       "在这两个值之间随机，固定节奏更像脚本。", min=0, max=120, unit="秒", advanced=True),
    _f("throttle_batch", "THROTTLE_BATCH", "int", "节流", "每多少请求长休",
       "连续这么多请求后休息一次。", min=1, max=10000, advanced=True),
    _f("throttle_rest", "THROTTLE_REST", "float", "节流", "长休时长",
       "每次长休休息多少秒。", min=0, max=3600, unit="秒", advanced=True),
    _f("fail_limit", "FAIL_LIMIT", "int", "节流", "连续失败上限",
       "连续失败这么多次就停手冷却。", min=1, max=100, advanced=True),
    _f("fail_cooldown", "FAIL_COOLDOWN", "float", "节流", "失败冷却时长",
       "触发上限后冷却多少秒。", min=0, max=7200, unit="秒", advanced=True),
    _f("max_requests", "MAX_REQUESTS", "int", "节流", "单轮请求上限",
       "0 = 不限制。试跑时设小一点。", min=0, max=100000, advanced=True),

]

# ⚠️ 顺序就是网页上「进阶参数」里的分组顺序 —— 改这里等于改界面。
# 🔴 2026-10-08：「发现方式」**整组删掉**（原来是 use_life_events / life_auto_enable / tree_file）。
#    事件流与「指定快照文件」都去掉了，这组没有成员了 ——
#    **分组名也必须从这儿删**，不然界面上会留一个空标题。
GROUPS = ["115 账号", "同步范围", "输出", "自动化", "节流"]
FIELD_BY_KEY = {f["key"]: f for f in FIELDS}

# 「网页专属」字段的环境变量名 —— `config._env()` 见到它们**直接跳过 `os.environ`**。
# 理由与迁移策略见模块顶部说明。
WEB_ONLY_ENVS: frozenset[str] = frozenset(f["env"] for f in FIELDS if f.get("web_only"))
# 必填项 —— 空着就跑不起来（前端用它做「首次配置」清单 + 开跑前的守卫）。
REQUIRED_KEYS: tuple[str, ...] = tuple(f["key"] for f in FIELDS if f.get("required"))


# --------------------------------------------------------------------------- 落盘
def config_path(data_dir: Path) -> Path:
    return Path(data_dir) / CONFIG_NAME


def load_json(data_dir: Path) -> dict[str, str]:
    """读覆盖值。键是**环境变量名**（⛔ 不是字段名），与 `config._env` 的口径一致。

    ⚠️ 存的是 env 名而不是字段名，是为了让 `.env` 与 `config.json` 两套写法
       在 `_env()` 里**走同一条路** —— 否则「网页改了但没生效」会极难查。
    """
    p = config_path(data_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in data.items():
        out[str(k)] = "" if v is None else str(v)
    return out


def save_json(data_dir: Path, values: dict) -> dict:
    """写入覆盖值。只认 `FIELDS` 里登记过的环境变量名，其余进 `rejected`。"""
    p = config_path(data_dir)
    known_env = {f["env"] for f in FIELDS}
    cur = load_json(data_dir)
    saved: list[str] = []
    rejected: list[str] = []

    for raw_key, raw_val in (values or {}).items():
        f = FIELD_BY_KEY.get(raw_key)
        # 允许两种写法：字段名（网页默认）或环境变量名（兼容手写）
        env = f["env"] if f else (raw_key if raw_key in known_env else None)
        if not env:
            rejected.append(raw_key)
            continue
        conv = coerce(f, raw_val) if f else str(raw_val)
        if conv is None:
            rejected.append(raw_key)
            continue
        cur[env] = str(conv)
        saved.append(raw_key)

    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return {"saved": saved, "rejected": rejected, "path": str(p)}


def reset_json(data_dir: Path, keys: list[str] | None = None) -> dict:
    """恢复出厂：删掉指定的覆盖值（`keys=None` = 全清）。"""
    p = config_path(data_dir)
    cur = load_json(data_dir)
    if keys is None:
        removed = list(cur)
        cur = {}
    else:
        removed = []
        for k in keys:
            f = FIELD_BY_KEY.get(k)
            env = f["env"] if f else k
            if env in cur:
                cur.pop(env, None)
                removed.append(k)
    if p.exists():
        p.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"removed": removed, "path": str(p)}


# --------------------------------------------------------------------------- 校验
def coerce(f: dict, value: Any) -> Any:
    """把一个值按字段类型转成合适形态；不合法的返回 None（会被丢进 rejected）。

    ⚠️ 前端的引号 / 空白 / 中文标点很容易带进来，这里统一剥一层。
    """
    kind = f.get("type", "str")
    if kind == "bool":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        return s in ("1", "true", "yes", "on", "y")
    if kind == "int":
        try:
            n = int(float(str(value).strip()))
        except (ValueError, TypeError):
            return None
        if "min" in f and n < f["min"]:
            n = int(f["min"])
        if "max" in f and n > f["max"]:
            n = int(f["max"])
        return n
    if kind == "float":
        try:
            x = float(str(value).strip())
        except (ValueError, TypeError):
            return None
        if "min" in f and x < f["min"]:
            x = float(f["min"])
        if "max" in f and x > f["max"]:
            x = float(f["max"])
        return x
    if kind == "enum":
        opts = {o[0] for o in f.get("options", [])}
        s = str(value).strip()
        return s if s in opts else None
    if kind == "cron":
        s = str(value).strip()
        if not s:
            return ""
        return s if _CRON_RE.match(s) else None
    if kind == "window":
        s = str(value).strip()
        if not s:
            return ""
        return re.sub(r"\s+", "", s) if _WINDOW_RE.match(s) else None
    return str(value).strip()


def describe(cfg: Any, data_dir: Path) -> dict:
    """给网页的一份完整字段说明 + 当前生效值 + 值来自哪里。"""
    overrides = load_json(data_dir)
    items: list[dict] = []
    missing: list[str] = []
    for f in FIELDS:
        key, env = f["key"], f["env"]
        cur = getattr(cfg, key, None)
        if isinstance(cur, Path):
            cur = str(cur)
        web_only = bool(f.get("web_only"))
        env_raw = (os.environ.get(env) or "").strip()
        saved_raw = str(overrides.get(env) or "").strip()
        # ⚠️ 网页专属字段**永远不报「环境变量」** —— 它压根就不读 os.environ。
        #    真在 .env 里留着的话，走 `stale_env` 单独报（见下），别混进 source。
        if env_raw and not web_only:
            source = "环境变量"
        elif saved_raw:
            source = "已保存"
        else:
            source = "默认"
        item = dict(f)
        item.update({
            "value": cur,
            "source": source,
            "default": _default_of(cfg, key, f),
            "web_only": web_only,
            # 🔴 网页专属字段却还在 `.env` / compose 里留着值 ⇒ **显式报出来**。
            #    不报的话，用户会觉得「我明明在 .env 里配了、怎么不生效」，
            #    这正是本项目最忌讳的那类「静默失效」。
            "stale_env": env_raw if (web_only and env_raw) else "",
        })
        if f.get("required") and not str(cur or "").strip():
            missing.append(key)
        items.append(item)
    return {
        "fields": items,
        "groups": GROUPS,
        "config_file": str(config_path(data_dir)),
        "overrides": overrides,
        "required": list(REQUIRED_KEYS),
        "missing": missing,          # 必填却还空着的键 —— 前端「首次配置」清单用它
    }


def migrate_web_only_from_env(data_dir: Path) -> dict:
    """把「网页专属」字段遗留在环境变量里的值**搬进 `config.json`**（一次性）。

    为什么必须有它：`strm_prefix` 由「环境变量 > config.json」改成「**只认 config.json**」
    之后，上一版写在 `.env` / compose 里的值会被忽略 ⇒ 前缀突然变空 ⇒ 下一轮直接跑不起来。
    ⇒ 启动时先搬一次：宁可多写一个键，也不让它静默变空。

    返回 `{"migrated": [...], "stale": [...]}`：
      · `migrated` —— 环境变量里有、config.json 里还没有 ⇒ 已搬进去
      · `stale`    —— 两边都有值且**不同** ⇒ **不覆盖**（以网页里的为准），只提示去删 `.env`
    """
    known = [f for f in FIELDS if f.get("web_only")]
    if not known:
        return {"migrated": [], "stale": []}
    cur = load_json(data_dir)
    migrated: list[str] = []
    stale: list[dict] = []
    for f in known:
        env_val = (os.environ.get(f["env"]) or "").strip()
        if not env_val:
            continue
        saved = str(cur.get(f["env"]) or "").strip()
        if not saved:
            cur[f["env"]] = env_val
            migrated.append(f["env"])
        elif saved != env_val:
            stale.append({"key": f["key"], "env": f["env"], "env_value": env_val})
    if migrated:
        p = config_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    return {"migrated": migrated, "stale": stale}


def _default_of(cfg: Any, key: str, f: dict) -> Any:
    """取「出厂值」—— 用一个不受 config.json 影响的干净实例。

    ⚠️ 直接 `Config()` 也不行：`_env()` 会读全局 `_OVERRIDES`。所以临时清空再读。
    """
    try:
        from . import config as config_mod
        saved, saved_loaded = config_mod._OVERRIDES, config_mod._OVERRIDES_LOADED
        config_mod._OVERRIDES, config_mod._OVERRIDES_LOADED = {}, True
        try:
            raw = getattr(config_mod.Config(), key, None)
        finally:
            config_mod._OVERRIDES, config_mod._OVERRIDES_LOADED = saved, saved_loaded
        return str(raw) if isinstance(raw, Path) else raw
    except Exception:
        return None
