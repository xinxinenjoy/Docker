"""参数配置层 —— 让「改参数」不用碰 `.env`、也不用重建容器。

## 落点与优先级

落点：`DATA_DIR/config.json`（网页上改的就是它）。

优先级（高 → 低）：

    ① 显式环境变量          —— 临时覆盖 / CI 用；`.env` 里**留空** ⇒ 这一层不生效
    ② `DATA_DIR/config.json` —— 网页上改的
    ③ 代码里的默认值        —— 出厂

⇒ 因此 `.env` 可以只剩**引导项**：`DATA_DIR` / `PORT` / `ORGANIZE_AUTH` / `ACCOUNTS_FILE`。
   业务参数一个都不用写在那儿。

## 为什么要有 `FIELDS` 这张表

网页需要知道「有哪些参数、什么类型、归哪一组、当前这个值是从哪来的」。
⛔ **不能在 HTML 里再抄一份** —— 两处各写一份必然分叉（本项目已经踩过同类坑：
`115offline/tests/test_namer.py` 与 `app/namer.py` 各写一遍规则，改了一边另一边静默失效）。

⭐ 所以表里**只写元信息**（环境变量名 / 类型 / 分组 / 取值域 / 文案），
   **默认值一律从 `Config` 实例上取** ⇒ 「出厂值」全局只有一个真相源。

## ⚠️ 这个模块不 import `config`

`config` 要 import 本模块来读覆盖值；本模块若反过来 import `config` 就成环。
`describe()` 因此把 `Config` 实例**当参数收进来**，而不是自己去造一个。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

__all__ = [
    "CONFIG_NAME", "FIELDS", "GROUPS", "FIELD_BY_KEY",
    "config_path", "load_json", "save_json", "reset_json", "describe", "coerce",
]

CONFIG_NAME = "config.json"

_CRON_RE = re.compile(r"^\S+(\s+\S+){4}$")
_WINDOW_RE = re.compile(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$")


# --------------------------------------------------------------------------- 字段表
def _f(key: str, env: str, kind: str, group: str, label: str, help_: str = "", **kw) -> dict:
    d = {"key": key, "env": env, "type": kind, "group": group, "label": label, "help": help_}
    d.update(kw)
    return d


FIELDS: list[dict] = [
    # ---------------------------------------------------------------- 账号
    _f("account_id", "ACCOUNT_ID", "account", "115 账号", "使用哪个 115 账号",
       "从 115offline 扫码登录过的账号里挑。留空 = 用第一个能用的那个。"
       "cookie 只维护一份 —— 这里不存 cookie，只存「用哪个账号」。"),

    # ---------------------------------------------------------------- 整理范围
    _f("root_path", "ROOT_PATH", "str", "整理范围", "整理根目录",
       "相对 115 网盘根目录的路径，目录树导出件的一级目录就是它。只有这个目录下面的东西会被动。",
       placeholder="云下载"),
    _f("tidy_target", "TIDY_TARGET", "str", "整理范围", "整理目标根",
       "tidy（整理当前目录）模式把整理好的往这个目录里放。与整理根平级，同在 115 根目录下。",
       placeholder="涩涩存档"),
    _f("tree_dir", "TREE_DIR", "path", "整理范围", "目录树目录",
       "把导出的 .txt 放进这个目录，本工具会自己挑最新的那份。", advanced=True),
    _f("tree_file", "TREE_FILE", "str", "整理范围", "指定树文件名",
       "留空 = 取上面目录里最新的那份。", advanced=True, placeholder="(自动取最新)"),

    # ---------------------------------------------------------------- 番号系列
    _f("series_enable", "SERIES_ENABLE", "bool", "番号系列", "按系列聚合",
       "把散落的番号目录收进 系列名/番号/ 两层。"),
    _f("series_min", "SERIES_MIN", "int", "番号系列", "系列聚合阈值",
       "一个系列至少有这么多部才建目录。调 1 = 全都聚合（会制造大量单部目录）。",
       min=1, max=99, unit="部"),
    _f("series_rename", "SERIES_RENAME", "bool", "番号系列", "番号改名规范化",
       "把目录名统一成 DLDSS-532 这种形态，顺带去掉 -ch / -U / [4K]@站点 这类尾巴。"),

    # ---------------------------------------------------------------- 影视
    _f("movie_enable", "MOVIE_ENABLE", "bool", "影视", "电影命名规范化",
       "把片名规整成 片名（年份）。⚠️ 只动「名字里有中文且有年份」的，纯英文名一律不改（会掉信息）。"),
    _f("episode_group_enable", "EPISODE_GROUP_ENABLE", "bool", "影视", "剧集收拢",
       "把散在根目录的集文件收进 剧名（年份）/第N季/。"),
    _f("episode_group_min", "EPISODE_GROUP_MIN", "int", "影视", "剧集收拢阈值",
       "同一部剧至少散落这么多集才收拢。", min=1, max=99, unit="集"),
    _f("paren", "PAREN", "enum", "影视", "年份括号",
       "统一成全角（）还是半角()。",
       options=[{"value": "full", "label": "全角（）"}, {"value": "half", "label": "半角()"}]),

    # ---------------------------------------------------------------- 垃圾清理
    _f("junk_action", "JUNK_ACTION", "enum", "垃圾清理", "清理动作",
       "report = 一个文件都不动，只出报告；quarantine = 移进隔离目录（可整目录撤销）；"
       "delete = 直接删（进 115 回收站，但和还原操作互斥）。",
       options=[{"value": "report", "label": "只报告"},
                {"value": "quarantine", "label": "移进隔离目录（可撤销）"},
                {"value": "delete", "label": "删除（进回收站）"}],
       warn="改成 delete 之前先想清楚 —— 它进的是 115 回收站，而 115 的「删除」和「还原」互斥，"
            "同一时刻只能有一个在跑。"),
    _f("quarantine_dir", "QUARANTINE_DIR", "str", "垃圾清理", "隔离目录名",
       "相对整理根目录。默认动作下，垃圾会按来源目录分桶移进来，保留原名。", placeholder="_待清理"),
    _f("junk_dup_min", "JUNK_DUP_MIN", "int", "垃圾清理", "重复次数门槛",
       "强特征垃圾还要「跨目录重复 ≥N 次」才自动处理；只出现一两次的只进报告。"
       "调 1 = 出现即处理（激进）。", min=1, max=999, unit="次"),
    _f("junk_allow_dir", "JUNK_ALLOW_DIR", "bool", "垃圾清理", "清理时允许动目录",
       "本盘大量广告外壳本身就是目录（文宣 / 看片资源 / …），名字没扩展名 —— "
       "离线阶段和空目录长得一样。关掉 ⇒ 清理只对文件生效，广告目录会留下来（跳过条数会写进回执）。"),

    # ---------------------------------------------------------------- 节流
    _f("throttle_min", "THROTTLE_MIN", "float", "节流防风控", "单请求间隔下限",
       "和下限之间随机取值 —— 随机比固定节奏更像人。", min=0, max=120, step=0.5, unit="秒"),
    _f("throttle_max", "THROTTLE_MAX", "float", "节流防风控", "单请求间隔上限",
       "", min=0, max=300, step=0.5, unit="秒"),
    _f("throttle_batch", "THROTTLE_BATCH", "int", "节流防风控", "长休前请求数",
       "每发这么多个请求就长休一次（「处理一部分之后暂停」）。", min=1, max=10000, unit="个"),
    _f("throttle_rest", "THROTTLE_REST", "float", "节流防风控", "长休时长",
       "", min=0, max=7200, step=5, unit="秒"),
    _f("tidy_batch", "TIDY_BATCH", "int", "节流防风控", "tidy 每批处理数",
       "tidy 模式一次处理多少个目录（3-5 为宜）。处理完一批长休一次，防反爬。",
       min=1, max=50, unit="个"),
    _f("tidy_batch_rest", "TIDY_BATCH_REST", "float", "节流防风控", "tidy 批间长休",
       "处理完一批目录后休息多久。别设太短：建议 ≥30 秒。",
       min=0, max=3600, step=5, unit="秒"),
    _f("window", "WINDOW", "window", "节流防风控", "只在这个时段跑",
       "留空 = 不限。窗外就地等，不退出。", placeholder="02:00-06:00"),
    _f("fail_limit", "FAIL_LIMIT", "int", "节流防风控", "连续失败多少次停手",
       "风控和网络抖动的症状一样，一律按风控处理。", min=1, max=99, unit="次"),
    _f("fail_cooldown", "FAIL_COOLDOWN", "float", "节流防风控", "失败后冷却",
       "", min=0, max=86400, step=60, unit="秒", advanced=True),

    # ---------------------------------------------------------------- 入站口（自动整理）
    _f("inbox_dir", "INBOX_DIR", "str", "入站口", "入站目录名",
       "自动整理只监控这一个目录（相对整理根）。你只把要整理的东西丢进来，"
       "工具处理完就移走 —— 不碰盘上其它地方。", placeholder="待整理"),
    _f("inbox_poll_interval", "INBOX_POLL_INTERVAL", "float", "入站口", "轮询间隔",
       "115 没有 webhook，只能轮询。每次轮询 = 列一次入站目录（1 个请求）。"
       "间隔别设太短：建议 ≥30 秒。", min=5, max=3600, step=5, unit="秒"),
    _f("inbox_max_requests", "INBOX_MAX_REQUESTS", "int", "入站口", "单次整理请求上限",
       "0 = 不限制。设个值可以在试跑时避免一次发太多请求（防风控）。",
       min=0, max=10000, unit="个"),

    # ---------------------------------------------------------------- 扫描节流（功能 2）
    _f("scan_throttle_min", "SCAN_THROTTLE_MIN", "float", "扫描", "扫描单请求间隔下限",
       "只影响「选定文件夹」的接口扫描（列目录），不影响执行。量小、影响小，可比整理快。",
       min=0, max=30, step=0.1, unit="秒"),
    _f("scan_throttle_max", "SCAN_THROTTLE_MAX", "float", "扫描", "扫描单请求间隔上限",
       "", min=0, max=60, step=0.1, unit="秒"),

    # ---------------------------------------------------------------- 安全阀
    _f("dry_run", "DRY_RUN", "bool", "安全阀", "只演练不动手",
       "打开时一个请求都不发（连根目录 id 都不查），只在日志里把要做的事说出来。",
       warn="关掉它 = 真的开始改网盘上的东西。第一次请用「整理」页勾一小批先试。"),
    _f("log_level", "LOG_LEVEL", "enum", "安全阀", "日志级别",
       "", advanced=True,
       options=[{"value": "DEBUG", "label": "DEBUG"}, {"value": "INFO", "label": "INFO"},
                {"value": "WARNING", "label": "WARNING"}, {"value": "ERROR", "label": "ERROR"}]),
]

FIELD_BY_KEY: dict[str, dict] = {f["key"]: f for f in FIELDS}
# 分组顺序 = 上表首现顺序（⛔ 别再手写一份组名列表，两处会分叉）
GROUPS: list[str] = list(dict.fromkeys(f["group"] for f in FIELDS))


# --------------------------------------------------------------------------- 读写
def config_path(data_dir: Any) -> Path:
    return Path(data_dir) / CONFIG_NAME


def load_json(data_dir: Any) -> dict[str, str]:
    """读 `config.json` → `{环境变量名: 字符串值}`。

    ⚠️ 键刻意用**环境变量名**（不是 Config 的字段名）—— 这样 `config.py` 里的
       `_env()` 一个函数就能把「env / json / 默认」三层优先级一次处理完，
       以后加字段不用改两处。
    ⛔ 读不出来一律当成空（配置坏了不该让工具起不来），但会返回空 dict 让调用方看得见。
    """
    path = config_path(data_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for f in FIELDS:
        if f["key"] not in data:
            continue
        raw = data[f["key"]]
        if raw is None:
            continue
        if isinstance(raw, bool):
            out[f["env"]] = "1" if raw else "0"          # 与 `_flag` 的取值口径一致
        else:
            out[f["env"]] = str(raw)
    return out


def save_json(data_dir: Any, values: dict[str, Any]) -> dict:
    """把 UI 传来的 `{字段名: 值}` 合并进 `config.json`（原子写）。

    返回 `{"saved": [...], "rejected": [...], "path": "..."}`。
    ⛔ 单个字段不合法只**拒绝那一条**，其余照存 —— 别让一个笔误把整页改动丢掉。
    """
    path = config_path(data_dir)
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(current, dict):
            current = {}
    except Exception:
        current = {}

    saved, rejected = [], []
    for key, raw in (values or {}).items():
        f = FIELD_BY_KEY.get(key)
        if f is None:
            rejected.append({"key": key, "error": "不认识的参数名"})
            continue
        try:
            current[key] = coerce(f, raw)
            saved.append(key)
        except ValueError as exc:
            rejected.append({"key": key, "error": str(exc)})

    if saved:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(path)                                  # 原子替换：写到一半断电也不会留半份
    return {"saved": saved, "rejected": rejected, "path": str(path)}


def reset_json(data_dir: Any, keys: list[str] | None = None) -> dict:
    """恢复默认：`keys` 为空则整个删掉 `config.json`。"""
    path = config_path(data_dir)
    if not keys:
        if path.exists():
            path.unlink()
        return {"removed": "*", "path": str(path)}
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(current, dict):
            current = {}
    except Exception:
        current = {}
    removed = [k for k in keys if current.pop(k, None) is not None]
    if removed:
        path.write_text(json.dumps(current, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return {"removed": removed, "path": str(path)}


# --------------------------------------------------------------------------- 校验
def coerce(field: dict, raw: Any) -> Any:
    """把 UI 送来的值转成合适的 JSON 类型，顺带校验。不合法直接抛 `ValueError`（消息给人看）。"""
    kind = field["type"]

    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("1", "true", "yes", "on", "y"):
            return True
        if s in ("0", "false", "no", "off", "n", ""):
            return False
        raise ValueError(f"「{field['label']}」只能是开/关")

    if kind in ("int", "float"):
        try:
            num = int(raw) if kind == "int" else float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"「{field['label']}」要是数字") from None
        lo, hi = field.get("min"), field.get("max")
        if lo is not None and num < lo:
            raise ValueError(f"「{field['label']}」不能小于 {lo}")
        if hi is not None and num > hi:
            raise ValueError(f"「{field['label']}」不能大于 {hi}")
        if kind == "float" and field.get("step") == 1:
            num = float(int(num))
        return num

    text = "" if raw is None else str(raw).strip()

    if kind == "enum":
        allowed = {o["value"] for o in field.get("options", [])}
        if text not in allowed:
            raise ValueError(f"「{field['label']}」只能是 {'/'.join(sorted(allowed))}")
        return text

    if kind == "cron":
        if not text:
            raise ValueError(f"「{field['label']}」不能为空")
        if not _CRON_RE.match(text):
            raise ValueError(f"「{field['label']}」要 5 段（分 时 日 月 周），现在是 {text!r}")
        return text

    if kind == "window":
        if text and not _WINDOW_RE.match(text):
            raise ValueError(f"「{field['label']}」要写成 02:00-06:00 这种")
        return text

    return text


# --------------------------------------------------------------------------- 给 UI
def describe(cfg: Any, data_dir: Any) -> dict:
    """字段元信息 + 当前生效值 + 来源。`cfg` 是已经应用过覆盖的 `Config` 实例。

    `source` 三选一（与 `config.py` 的优先级一一对应）：
      · `env`     —— 环境变量显式设了（`.env` 里写了东西）⇒ 网页改不动它，UI 要提示
      · `json`    —— 来自 `config.json`（网页改的）
      · `default` —— 出厂默认
    """
    stored = json.loads(config_path(data_dir).read_text(encoding="utf-8")) \
        if config_path(data_dir).exists() else {}
    if not isinstance(stored, dict):
        stored = {}

    fields: list[dict] = []
    for f in FIELDS:
        item = dict(f)
        item["value"] = _jsonable(getattr(cfg, f["key"], None))
        if os.environ.get(f["env"], "").strip():
            item["source"] = "env"
        elif f["key"] in stored:
            item["source"] = "json"
        else:
            item["source"] = "default"
        fields.append(item)

    return {
        "groups": GROUPS,
        "fields": fields,
        "path": str(config_path(data_dir)),
        "env_locked": sorted({f["env"] for f in FIELDS if os.environ.get(f["env"], "").strip()}),
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return value
