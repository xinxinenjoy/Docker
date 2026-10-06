# 115 网盘慢速整理

把乱堆的 115 网盘**按番号系列 / 影视作品归好位**，顺手清掉夹带的广告推广件 ——
全程**慢速**（请求间隔 3–5 秒随机 + 定期长休），**默认只出方案不动手**。

## 功能

- **番号按系列归位**：`DLDSS-532` → `DLDSS/DLDSS-532`，系列满 3 部才建目录
- **番号目录改名**：`ipzz-916ch` → `IPZZ-916`（去掉 `-ch` / `-U` / `[4K]@站点` 尾巴）
- **电影命名**：规范成 `片名（年份）`
- **剧集收拢**：散落的集文件收进 `剧名（年份）/第N季/`
- **清广告推广件**：分级判定 —— 强特征才动，正规封面/字幕永不碰，判不准的只报告
- **自动增量**：定时（默认每天 04:00）+「最近接收」轮询，只碰新增/新到的目录，**不扫全盘**

## 使用方法

**① 起容器**

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
      - DRY_RUN=1                 # 先只出方案、不发请求
    volumes:
      - ./data:/data
```

> 不映射端口 —— 它是后台慢速进程，没有网页界面。看进展用 `docker logs -f 115organize`。

**② 放进目录树** —— 115 网页版能导出整个目录树，把那份 `.txt` 丢进 `/data/tree/`。

**③ 先出方案**

```bash
docker exec 115organize python -m app plan     # 纯离线，不发任何请求
# 报告在 data/reports/plan.md —— 看清楚它打算动什么、要多久
```

**④ 确认后再真跑** —— 把 `DRY_RUN` 改成 `0` 重启容器，它就会按计划慢速执行；
之后每天 04:00 自动处理新增目录。

## 命令

| 命令 | 说明 |
|:---|:---|
| `python -m app plan` | 离线算方案（不联网），出报告 |
| `python -m app run` | 按方案执行（`DRY_RUN=1` 时零请求） |
| `python -m app once` | 跑一轮增量（新增目录 + 最近接收） |
| `python -m app watch` | 常驻，按 `SCHEDULE_CRON` 到点跑 |
| `python -m app doctor` | 环境自检 |

## 安全

- **默认 `DRY_RUN=1`**：一个请求都不发，只在报告里说它本来要做什么
- **绝不覆盖**：每次移动都显式带「重名就并存」（115 对文件的默认策略是覆盖，且不可恢复）
- **清理不删**：默认移到 `_待清理/`，**可整目录撤销**
- **判不准只报告**；目标冲突一律**不执行**，列出来等人裁决
- **慢**：单请求 3–5 秒随机间隔，每 200 个请求长休 60 秒，可限定只在凌晨跑

## 变量

完整清单见仓库里的 `.env.example`。常用的：

| 变量 | 默认 | 说明 |
|:---|:---|:---|
| `ROOT_PATH` | `云下载` | 整理哪个目录 |
| `DRY_RUN` | `1` | 改 `0` 才真动手 |
| `JUNK_ACTION` | `quarantine` | `report` / `quarantine` / `delete` |
| `SERIES_MIN` | `3` | 系列满几部才建目录 |
| `THROTTLE_MIN`/`MAX` | `3`/`5` | 单请求间隔（秒） |
| `THROTTLE_BATCH`/`REST` | `200`/`60` | 每 N 个请求长休 M 秒 |
| `WINDOW` | 空 | 只在此时段跑，如 `02:00-06:00` |
| `SCHEDULE_CRON` | `0 4 * * *` | 定时计划 |
