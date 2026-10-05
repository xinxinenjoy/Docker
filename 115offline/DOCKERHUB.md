# 115 离线推送（Docker Hub 页面用这份）

自托管的 **115 网盘离线下载推送工具**。手机浏览器打开网页，粘贴磁力 / ed2k / HTTP 链接，一键推给 115 离线下载。
跑在常年开机的机器上（NAS / 小主机 / 云服务器都行），手机随时可用。

## 功能

**推送**
- 多账号：可保存多套 115 cookie，界面下拉切换；**扫码登录**（推荐「电视端」，最不易挤掉在用的登录）或 cookie 登录
- 批量粘贴：一次贴一大段，自动挑出链接；粘连 / 无换行 / 夹杂文字都能正确拆开
- 中文暗号还原：百家姓 / 核心价值观 / 与佛论禅（佛曰）直接还原成链接，可与链接混着粘
- 自动去重；32 位磁力自动转 40 位（115 认不出 32 位）
- 选保存目录：多级下探，可设为本账号默认

**任务与整理**
- 任务记录：按状态筛选、看进度与落地目录，一键跳去整理
- 网盘整理：**任意目录**都能进 —— 目录改名（`片名（年份）` / `片名（系列）`）、分集命名（`剧名.SxxExx.第 N 集.集标题.ext`）、批量改名 / 批量删除、排序（名称 / 时间 / 修改）、广告筛选
- 回收站：最近删除倒序 + 多选还原
- 目录列表带缓存（存在 NAS 数据目录，容器重启不丢）；批量操作不并发（遵守 115 接口要求）

## 快速开始

### 方式一：`docker run`

```bash
docker run -d --name 115offline \
  -p 8765:8765 \
  -e ACCESS_TOKEN=换成一个够长的口令 \
  -v /path/to/data:/data \
  --restart unless-stopped \
  xinxinenjoy/115offline:latest
```

### 方式二：`docker compose`

```yaml
services:
  115offline:
    image: xinxinenjoy/115offline:latest
    container_name: 115offline
    restart: unless-stopped
    ports:
      - "8765:8765"
    environment:
      - ACCESS_TOKEN=换成一个够长的口令
      - TZ=Asia/Shanghai
    volumes:
      - ./data:/data
```

```bash
docker compose up -d
```

打开 `http://部署机器的IP:8765`，先输入 `ACCESS_TOKEN`。

## 环境变量

| 变量 | 默认 | 说明 |
|:---|:---|:---|
| `ACCESS_TOKEN` | 空 | 访问口令，**映射到公网务必设置** |
| `DATA_DIR` | `/data` | 数据目录（存 115 cookie 与目录缓存），**必须挂到宿主机** |
| `FOLDER_CACHE_TTL` | `300` | 目录缓存有效秒数，`0` = 不过期只能手动刷新 |
| `TZ` | 容器系统 | 时区，如 `Asia/Shanghai` |

端口：容器内固定 `8765`，宿主机映射随意。

## 怎么拿 115 cookie

1. 电脑浏览器登录 [115.com](https://115.com)，按 `F12` 打开开发者工具 → **网络 / Network**
2. 刷新页面，点任意一个发往 `115.com` 的请求
3. 在请求头里找到 `Cookie:`，整行复制
4. 其中的 `UID=` `CID=` `SEID=` 三项是必需的

粘进工具的「账号管理」页即可（也可以直接用扫码登录，不用手抓 cookie）。

## 安全提醒

115 cookie 等同于账号登录态。工具只把它存在你自己的 `DATA_DIR`，不上传任何第三方；
但**端口一旦映射到公网，务必设置足够长的 `ACCESS_TOKEN`**，并建议走 HTTPS（反向代理 + 证书）。
