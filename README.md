# Docker

自建 Docker 镜像的源码仓库 —— **一个子目录一个项目**，由 GitHub Actions 自动构建**多架构**镜像并发布到 Docker Hub。

## 包含的镜像

| 镜像 | 说明 |
|:---|:---|
| [`115offline`](https://hub.docker.com/r/xinxinenjoy/115offline) | 115 网盘离线下载推送工具：手机网页粘贴磁力，一键推给 115 |
| [`atv-tv-power`](https://hub.docker.com/r/xinxinenjoy/atv-tv-power) | Apple TV 开关机联动（Home Assistant / 推送通知） |

镜像全名为 `xinxinenjoy/<项目名>`，源码在仓库内同名目录下。

## 支持的平台

`docker pull` 时 Docker 会自动挑选匹配的架构 —— 使用者无需做任何事。

| 平台 | 覆盖设备 | 覆盖情况 |
|:---|:---|:---|
| `linux/amd64` | x86-64 的 PC / NAS / 云服务器 | 全部镜像 |
| `linux/arm64` | 树莓派 4 / 5、Apple Silicon、ARM NAS、各类开发板 | 全部镜像 |
| `linux/arm/v7` | 树莓派 3、部分 32 位 ARM 电视盒子 | 视项目而定（见各项目 README） |

## 怎么用

```bash
docker pull xinxinenjoy/115offline:latest

docker run -d --name 115offline \
  -p 8765:8765 \
  -e ACCESS_TOKEN=换成一个足够长的口令 \
  -v /path/to/data:/data \
  --restart unless-stopped \
  xinxinenjoy/115offline:latest
```

各项目的环境变量与完整步骤见对应目录下的说明：

- [`115offline/README.md`](115offline/README.md)
- [`atv-tv-power/README.md`](atv-tv-power/README.md)

## 怎么构建

推送代码到 `main` 即触发 GitHub Actions —— **改哪个目录只构建哪个项目**（`paths` 过滤），产物直接推送 Docker Hub，本地无需安装 Docker。

| Workflow | 触发路径 |
|:---|:---|
| [`115offline.yml`](.github/workflows/115offline.yml) | `115offline/**` |
| [`atv-tv-power.yml`](.github/workflows/atv-tv-power.yml) | `atv-tv-power/**` |

也可在 [Actions](../../actions) 页面手动触发（`workflow_dispatch`）。

> 构建依赖仓库 secret `DOCKERHUB_TOKEN`（Docker Hub Access Token，权限 **Read & Write**）。

## 维护者流程

1. 改 `115offline/` 或 `atv-tv-power/` 下的任何文件 → 推 `main`，对应 workflow 自动构建并推送 Docker Hub。
2. **镜像推送成功后，必须同步更新 Docker Hub 仓库页的说明与使用方法。**
   Hub 页面是别人看到的第一眼 —— 镜像更新了、说明还停在旧版，等于误导。
   **以各目录下的 `README.md` 为准**同步（Hub 的 Overview 与短描述都从它来）。
3. 验收以 `docker manifest inspect <镜像>` 为准，别只看 CI 是不是绿的。
