# Docker

> ## 🚧 开发中 —— 暂不对外推荐
>
> 这三个镜像还在迭代，**功能没定稿**，接口与配置都可能变。
> 请先别把它推荐给别人 —— 等定稿后再说。
>
> （另一个原因：**每次推 `main` 都会自动触发一次 Docker Hub 构建**。迭代期频繁推送 =
> 频繁构建等待。已给三个 workflow 配 `concurrency.cancel-in-progress`，连推多个提交只构建最后一次。）

自建 Docker 镜像的源码仓库 —— **一个子目录一个项目**，由 GitHub Actions 自动构建镜像并发布到 Docker Hub。

## 包含的镜像

| 镜像 | 说明 |
|:---|:---|
| [`115offline`](https://hub.docker.com/r/xinxinenjoy/115offline) | 115 网盘离线下载推送工具：手机网页粘贴磁力，一键推给 115 |
| [`115organize`](https://hub.docker.com/r/xinxinenjoy/115organize) | 115 网盘慢速整理：番号按系列归位、影视规范命名、自动清广告，全程节流防风控 |
| [`115strm`](https://hub.docker.com/r/xinxinenjoy/115strm) | 115 网盘结构镜像成本地 strm 树，给 Emby/Jellyfin/Kodi/Infuse/爆米花 扫，播放走 Alist 302 直连 CDN（⚠️ VidHub 不支持 strm） |
| [`atv-tv-power`](https://hub.docker.com/r/xinxinenjoy/atv-tv-power) | Apple TV 开关机联动（Home Assistant / 推送通知） |

镜像全名为 `xinxinenjoy/<项目名>`，源码在仓库内同名目录下。

## 支持的平台

| 平台 | 覆盖设备 | 覆盖情况 |
|:---|:---|:---|
| `linux/amd64` | x86-64 的 PC / NAS / 云服务器 | 全部镜像 |

> ⚠️ **只发布 `linux/amd64`（x86-64）**。ARM（`arm64` / `arm/v7`）**不做** —— 两个项目在 ARM 上
> 都得现场编译依赖（`miniaudio` / `brotli` / `zstandard` 等无预编译轮子），构建耗时远超收益，
> 2026-10-05 拍板砍掉。具体原因见各项目 README。

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
- [`115organize/README.md`](115organize/README.md)
- [`115strm/README.md`](115strm/README.md)
- [`atv-tv-power/README.md`](atv-tv-power/README.md)

## 怎么构建

推送代码到 `main` 即触发 GitHub Actions —— **改哪个目录只构建哪个项目**（`paths` 过滤），产物直接推送 Docker Hub，本地无需安装 Docker。

| Workflow | 触发路径 |
|:---|:---|
| [`115offline.yml`](.github/workflows/115offline.yml) | `115offline/**` |
| [`115organize.yml`](.github/workflows/115organize.yml) | `115organize/**` |
| [`115strm.yml`](.github/workflows/115strm.yml) | `115strm/**` |
| [`atv-tv-power.yml`](.github/workflows/atv-tv-power.yml) | `atv-tv-power/**` |

也可在 [Actions](../../actions) 页面手动触发（`workflow_dispatch`）。

> 构建依赖仓库 secret `DOCKERHUB_TOKEN`（Docker Hub Access Token，权限 **Read & Write**）。
>
> ⚠️ `115organize.yml` / `115strm.yml` 比另两个多一个 `test` job：**先跑离线单测，过了才构建**
> （`115organize` 102 个 / `115strm` 280 个）。这两个项目的风险不在编译不过，而在**规则判错**
> （垃圾判错 = 正片被搬走；路径口径判错 = 整库播不了；删除判定写错 = 本地 strm 被误删）——
> 这类错误只有用例能拦。

## 维护者流程

1. 改 `115offline/` / `115organize/` / `115strm/` / `atv-tv-power/` 下的任何文件 → 推 `main`，对应 workflow 自动构建并推送 Docker Hub。
2. **镜像推送成功后，必须同步更新 Docker Hub 仓库页的说明。**
   Hub 页面是别人看到的第一眼 —— 镜像更新了、说明还停在旧版，等于误导。
   **同步的是各目录下的 `DOCKERHUB.md`**（Hub 的 Overview 与短描述都从它来），
   `README.md` 留在仓库当**完整文档**。`DOCKERHUB.md` 的性质：
   - **简明扼要**：≤ 100 行 / 6 KB；该压缩时压**句子**，别为了省行数漏列命令 / 环境变量
   - **适配所有人**：⛔ 不出现作者自己的目录名 / 文件名 / 文件夹 id / 域名 / 内网地址，举例一律用通用占位
   - **只讲「怎么用」和「怎么不踩坑」**：不写实现推导，一个坑只讲一次
3. 验收以 `docker manifest inspect <镜像>` 为准，别只看 CI 是不是绿的。
