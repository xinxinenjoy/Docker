# Docker

自建 Docker 镜像的源码仓库 —— 一个子目录一个项目，GitHub Actions 自动构建**多架构**镜像并发布到 Docker Hub。

## 包含的镜像

| 目录 | 镜像 | 说明 |
|---|---|---|
| [`115offline/`](115offline/) | [`xinxinenjoy/115offline`](https://hub.docker.com/r/xinxinenjoy/115offline) | 115 网盘离线下载推送工具 —— 手机网页粘贴磁力，一键推给 115 |
| [`atv-tv-power/`](atv-tv-power/) | [`xinxinenjoy/atv-tv-power`](https://hub.docker.com/r/xinxinenjoy/atv-tv-power) | Apple TV 开关机联动（Home Assistant / 推送通知） |

## 支持的平台

所有镜像**同时构建三个平台**，`docker pull` 时 Docker 会自动挑对应架构 —— 使用者不需要做任何事：

| 平台 | 覆盖设备 |
|---|---|
| `linux/amd64` | x86-64 的 PC / NAS / 云服务器 |
| `linux/arm64` | 树莓派 4 / 5 · Apple Silicon · ARM NAS · 各类开发板 |
| `linux/arm/v7` | 树莓派 3 / 部分 32 位 ARM 电视盒子 |

## 怎么用

```bash
docker pull xinxinenjoy/115offline:latest
docker run -d --name 115offline -p 8765:8765 \
  -e ACCESS_TOKEN=换成一个够长的口令 \
  -v /path/to/data:/data \
  --restart unless-stopped \
  xinxinenjoy/115offline:latest
```

各项目的配置项、环境变量与使用步骤见对应目录下的 README。

## 怎么构建

推送到 `main` 即触发 GitHub Actions —— **改哪个目录只构建哪个项目**（`paths` 过滤）。
构建产物直接推送 Docker Hub，不需要本地装 Docker。

也可以在 [Actions](../../actions) 页面手动触发（`workflow_dispatch`）。

| Workflow | 触发路径 |
|---|---|
| `115offline.yml` | `115offline/**` |
| `atv-tv-power.yml` | `atv-tv-power/**` |

> 构建需要仓库 secret `DOCKERHUB_TOKEN`（Docker Hub Access Token，权限 Read & Write）。
