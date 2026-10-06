# 115organize

**115 网盘的慢速整理工具** —— 把乱堆的网盘按「番号系列 / 影视作品」归好位，顺手清掉夹带的广告推广件。

两条铁约束贯穿全部设计：

1. **慢**。每一个请求之间 3–5 秒随机间隔，每 200 个请求长休 60 秒，还能限定只在凌晨跑。
   目的只有一个：**不触发 115 的风控**。
2. **不覆盖、不误删**。任何移动都显式声明「重名就并存」；清理默认是**移到隔离目录**而不是删；
   判不准的一律只报告。出厂默认 **`DRY_RUN=1`：只出方案，一个请求都不发**。

---

## 一、它能做什么

分成**两部分**，对应两种用法：

### 第 1 部分：整理已有目录（离线算方案，再慢速执行）

你已经从 115 导出了目录树（`.txt`），所以**不需要扫盘**：

```bash
python -m app plan   # 纯离线，读目录树算方案，出报告 + plan.json（不联网）
python -m app run    # 按方案执行（DRY_RUN=1 时只演练）
```

| 动作 | 做什么 |
|:---|:---|
| **番号 → 系列** | `DLDSS-532` 这类番号目录收进 `DLDSS/`；一个系列满 `SERIES_MIN`(默认3) 部才建目录 |
| **番号目录改名** | `ipzz-916ch` → `IPZZ-916`（去掉 `-ch` / `-U` / `[4K]@站点` 这类尾巴） |
| **电影命名** | 按已有规则规范成 `片名（年份）` |
| **剧集收拢** | 散落的集文件收进 `剧名（年份）/第N季/` |
| **清广告** | 分级判定：强特征才动，白名单（封面/字幕/nfo）永不碰，灰区只报告 |

### 第 2 部分：新增目录自动整理（定时 / 增量）

115 **没有 webhook**，所以「有新目录就触发」只能靠轮询：

```bash
python -m app once    # 跑一轮增量：快照 diff（新增目录）+「最近接收」（最近 N 天）
python -m app watch   # 常驻，按 SCHEDULE_CRON 到点自动跑（容器默认就是它）
```

一轮做什么：列根目录 → 跟上次的快照 diff 找出**新增目录** → 拉「最近接收」按天过滤 →
两者合并成这一轮的目标 → 只对这几个目录生成增量计划 → 执行 → 更新快照、写回执。

> ⚠️ **增量只碰要处理的目录**，绝不递归扫全盘 —— `fs_files` 是 115 明确会风控的接口。

---

## 二、快速开始

### 方式 A：Docker（推荐）

```bash
docker run -d --name 115organize \
  -e P115_COOKIE='你的 115 cookie' \
  -e ROOT_PATH='云下载' \
  -e DRY_RUN=1 \
  -v /path/to/data:/data \
  --restart unless-stopped \
  xinxinenjoy/115organize:latest
```

等价的 compose：

```yaml
services:
  115organize:
    image: xinxinenjoy/115organize:latest
    container_name: 115organize
    restart: unless-stopped
    environment:
      - P115_COOKIE=你的 115 cookie
      - ROOT_PATH=云下载
      - DRY_RUN=1                 # 先只出方案
    volumes:
      - ./data:/data
```

> 本镜像**不映射端口** —— 它是个后台慢速进程，没有网页界面。
> 看进展用 `docker logs -f 115organize`，或看 `data/reports/` 下的报告。

**第一次跑，按这个顺序**：

```bash
# ① 把目录树导出件放进 /data/tree/（115 网页版能导出整个目录树，见下节）
# ② 出方案（离线、不发请求）
docker exec 115organize python -m app plan
# ③ 看报告：data/reports/plan.md
# ④ 确认没问题 → 把 .env 里 DRY_RUN 改成 0 → 重启容器 → 真跑
```

**cookie 从哪来**：三种任选 ——
① 环境变量 `P115_COOKIE`；② 写进 `/data/cookie.txt`；
③ **挂 `115offline` 的数据卷复用它的 `accounts.json`**（推荐，cookie 只维护一份）：

```yaml
    volumes:
      - /path/to/115offline/data:/data
```

### 方式 B：本机直接跑

```bash
pip install -r requirements.txt
export TREE_DIR="目录树所在文件夹"          # 或者把树放进 data/tree/
python -m app doctor                        # 环境自检：目录树 / cookie / 依赖 / cron
python -m app plan
python -m app run
```

### 目录树怎么导出

115 的网页版有「导出目录树」功能，导出的 `.txt` 就是本工具的输入。
放进 `TREE_DIR`，`plan` 会自己挑**最新**的那份；想指定就设 `TREE_FILE`。
读得懂 UTF-16LE（带 BOM）/ UTF-8 / GBK。

---

## 三、命令一览

| 命令 | 说明 |
|:---|:---|
| `python -m app plan` | **离线**算方案，出 `plan.md` / `plan.json` / `plan-ops.txt` |
| `python -m app run` | 执行方案。`DRY_RUN=1` 时零请求；支持断点续跑与请求上限 |
| `python -m app once` | 在线跑**一轮增量**（新增目录 + 最近接收） |
| `python -m app watch` | 常驻，按 `SCHEDULE_CRON` 到点跑 `once` |
| `python -m app stats` | 只看目录树规模统计 |
| `python -m app doctor` | 环境自检：目录树 / cookie / 目录可写 / 依赖 / cron 是否可解析 |
| `python -m app clear-state` | 清断点（想从头重跑时用） |

`run` 的常用参数：`--max-requests 50`（试跑，发够 50 个请求就收工并保存断点）、
`--no-resume`（忽略断点从头跑）、`--plan <路径>`。

---

## 四、配置

全部走环境变量，完整清单与每个值的理由见 [`.env.example`](.env.example)。几个关键的：

| 变量 | 默认 | 说明 |
|:---|:---|:---|
| `ROOT_PATH` | `云下载` | 整理范围的根（相对 115 网盘根目录） |
| `DRY_RUN` | **`1`** | **出厂默认只出方案不动手**。看过报告再改 `0` |
| `JUNK_ACTION` | `quarantine` | `report` 只报告 / `quarantine` 移到隔离目录（可撤销）/ `delete` 删（进回收站） |
| `SERIES_MIN` | `3` | 一个系列满几部才建目录 |
| `THROTTLE_MIN` / `THROTTLE_MAX` | `3` / `5` | 单请求间隔（秒），之间**随机**取 |
| `THROTTLE_BATCH` / `THROTTLE_REST` | `200` / `60` | 每 200 个请求长休 60 秒 |
| `WINDOW` | 空 | 只在此时段跑，如 `02:00-06:00`；窗外就地等 |
| `SCHEDULE_CRON` | `0 4 * * *` | 每天 04:00。支持 `*/6`、`1-5`、`MON` |

**三档节流**（`MIN / MAX / BATCH / REST`）：

| 档 | 值 | 什么时候用 |
|:---|:---|:---|
| 频繁 | `1 / 2 / 500 / 30` | 盘小、想快点看完 |
| **常规（默认）** | `3 / 5 / 200 / 60` | 一般情况 |
| 保守 | `8 / 12 / 100 / 600` | 刚被风控过 / 盘很大 |

---

## 五、安全设计（为什么敢让它动我的盘）

| 措施 | 说明 |
|:---|:---|
| **默认 dry-run** | `DRY_RUN=1` 时**一个请求都不发**（连根目录 id 都不查），只在日志/报告里说它本来要做什么 |
| **绝不覆盖** | 每次移动都显式带 `keep_both`。115 对文件的默认策略是 `replace` —— **覆盖且不可恢复**，所以这个参数一次都不省 |
| **清理不删** | 默认 `quarantine`：移到 `_待清理/<原目录名>/`，**保留原名、按来源分桶**，可整目录撤销 |
| **冲突即停车** | 两个源归一到同一目标（本盘真实存在 5 组）⇒ 标 `auto=False`，**不执行**，只在报告里列出来等人裁决 |
| **不认识就不动手** | 动作类型白名单外的、计划里没有的，一律拒绝执行（宁可不动，不猜着动） |
| **判不准只报告** | 垃圾判定分三级，灰区 1,700+ 条只进报告 |
| **失败即冷却** | 连续失败 8 次（风控与网络抖动的症状一样）就停手冷却 15 分钟 |
| **断点续跑** | 中途挂了重跑不会重复动手（四个 pass 各自记断点） |

---

## 六、它不会做什么

- **不下载、不上传、不刮削元数据**。它只做移动/改名/清理。想要封面、简介、演员表，
  那是 [JavSP](https://github.com/Yuukiy/JavSP) / [MoviePilot](https://github.com/jxxghp/MoviePilot) 的活。
- **不递归扫全盘**。没有目录树就不能算方案；增量模式只碰当轮的目标目录。
- **不认 115 的「分享 / 离线下载推送」**。想推磁力去用同仓库的
  [115offline](https://github.com/xinxinenjoy/Docker/tree/main/115offline)。
- **不并发**。115 明确写了 `fs_move` / `fs_delete` **请不要并发执行** —— 本工具全程串行。

---

## 七、规则是怎么来的

判别规则不是拍脑袋写的：

- **番号识别**照抄 [JavSP](https://github.com/Yuukiy/JavSP)（5,179★，`javsp/avid.py`，GPL-3.0）的规则**顺序**，
  噪声词表则来自你这份真实目录树的**逐条计数**（如 `第一會所新片@` 出现 218 次、后缀 `-C` 502 次）。
- **命名规则**（电影 `片名（年份）`）直接复用本仓库
  [`115offline/app/namer.py`](../115offline/app/namer.py)，两边**同步**（`tests/check_namer.py` 是同一份回归用例）。
- **垃圾判定**的词表拆成「出现即判」与「必须叠加跨目录重复」两级 ——
  实测第一版把 `…撸先生和玲的射4分钟.mp4` 这类**正片**误判成广告（2,194 条误报），
  收紧后降到 1,150、再叠加重复度门槛后为 929。

---

## 八、开发

```bash
python -m unittest discover -s tests        # 74 个用例，约 1 秒，不需要装任何第三方依赖
python tests/check_namer.py                 # 命名规则独立脚本（与 115offline 共用）
```

用例全部离线：节流器的时钟与 sleep 是**可注入**的（否则验「4 小时跑完」要真等 4 小时），
115 那层用**假 client** 顶掉（能断言每次都传了 `keep_both`、翻页没漏、请求没绕过节流）。

## 九、局限 / 已知取舍

- **番号系列阈值一刀切**：`SERIES_MIN=3` 意味着只出现 1–2 次的番号不会被聚合
  （实测 548 个系列里 262 个只出现 1 次，全聚合会制造大量碎片目录）。想要全聚合就设 1。
- **电影只认「有年份 + 有清晰度标签」的**。纯英文名一律不改名 ——
  曾经改过一个 `6.Underground.2019…`，把开头那个 `6` 弄丢了。
- **`count` 缺失时靠翻页试探**，极端情况（接口不认 `offset`）会在 500 页处收手并告警。
- **不动 `115电影` 那块**（如果盘上另有该目录）：`ROOT_PATH` 一次只指一个根，想整理另一块就另跑一份配置。

---

## 十、许可

番号识别规则源自 [JavSP](https://github.com/Yuukiy/JavSP)（GPL-3.0），本项目相应部分同受其约束。
其余代码用 [p115client](https://github.com/ChenyangGao/p115client)（MIT）访问 115 接口。
