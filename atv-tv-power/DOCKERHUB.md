# Apple TV 待机 → 自动关电视

按 Apple TV 遥控器让 ATV 待机时，把它旁边那台「HDMI-CEC 关不掉」的电视一起关掉 —— 一个常驻进程，不加硬件。

```
Apple TV ──Companion 协议推送(实时)──▶ 本服务 ──局域网 HTTP──▶ 电视
```

> ⚠️ **致命前提：只在【小米电视】上实测过。**
> 本方案依赖电视**自带的局域网 HTTP 控制接口**（实测端口 `6095`）——这个端口是小米电视的，其它品牌几乎不可能一样。
> **不是小米电视先别照抄配置**，需自行确认：① 有没有等价的局域网 HTTP 接口；② 真实端口与请求路径；
> ③ 电源键是不是「开/关翻转」语义。三者任一不同，就得改 `atv_tv_power.py` 里的 `TvController`（只有三个方法）。

## 使用方法

**① 先准备两份东西**（都不打进镜像，靠挂载进来）

1. **配对凭据**：把 ATV 的 IP 填进 `config.json` 的 `atv.host`，再跑配对（PIN 会显示在电视上）：
   `python tools/pair_atv.py --host <你的ATV地址>` ⇒ 产出 `keys/atv_credentials.json`
2. **`config.json`**：照 `config.example.json` 复制一份，改掉里面的地址与推送设置。

**② 起容器**

```bash
docker run -d --name atv-tv-power \
  -v /path/to/config.json:/app/config.json:ro \
  -v /path/to/keys:/app/keys:ro \
  --restart unless-stopped \
  xinxinenjoy/atv-tv-power:latest
```

等价的 compose：

```yaml
services:
  atv-tv-power:
    image: xinxinenjoy/atv-tv-power:latest
    container_name: atv-tv-power
    restart: unless-stopped
    volumes:
      - ./config.json:/app/config.json:ro
      - ./keys:/app/keys:ro
```

**③ 看日志** —— 连上之后，每次 ATV 待机都会打一行「进入待机 / 关电视 / 跳过（原因）」。

## 配置

`config.json` 里的键（缺的用默认值）：

| 键 | 默认 | 说明 |
|:---|:---|:---|
| `delay_seconds` | 20 | ATV 待机后等多久才关电视；期间 ATV 被唤醒则取消（防误触） |
| `poll_seconds` | 30 | 心跳探活间隔，也是状态对账间隔（别小于 5s —— 探活本身有 5s 超时） |
| `retries` / `retry_interval` | 3 / 5 | 关电视失败的重试次数与间隔 |
| `reconnect_delay` | 30 | ATV 掉线后的重连间隔 |
| `atv.host` / `atv.credentials` | — / `keys/atv_credentials.json` | Apple TV 地址；配对产物 |
| `tv.host` / `tv.port` | — / 6095 | 电视地址；局域网 HTTP 接口端口（⚠️ 见上） |
| `tv.timeout` / `tv.verify_delay` | 4 / 4 | 单次 HTTP 超时；发键后复查状态的等待秒数 |
| `guard.enabled` / `guard.on_unknown` | true / `skip` | **防误关判据**；拿不到 ATV 原始状态时 `skip` = 不关电视（保守）、`close` = 按旧逻辑关 |
| `bark.*` | — | 失效告警推送（`enabled` / `server` / `key_file` / `group` / `offline_after`） |

设备地址也可以走环境变量（便于在 compose / `.env` 里配）：`ATV_HOST` · `TV_HOST` · `TV_PORT` ——
⚠️ 它们**优先，但只覆盖「非空」的值**，不传或传空串就回落 `config.json`。另可用 `TZ`（镜像内默认 `Asia/Shanghai`）。

## 注意要点

- ⚠️ **`tv.port` 别照抄** —— `6095` 是小米电视的接口端口，其它品牌未测、未必一致。
- ⚠️ **防误关判据**：ATV 有自己的屏保 / 睡眠计时器，**跟你切没切电视输入源无关**。所以只在「待机前的**原始**状态是 `Awake`」时才关电视；屏保 / 闲置自动睡着的**不关**。已知取舍：在 ATV 主界面停到进屏保**之后**再按关机，会被判成「自己睡着」而不关 —— 代价是偶尔该关没关，换来的是**不误关**。
- ⚠️ **`keyevent` 是「翻转」语义**（`power` = 开/关切换）⇒ 发键前必须先确认电视处于开机态，否则会把已关的电视**打开**。
- 🔴 **改了配置要重启容器** —— `config.json` 是挂载进去的，compose 不会因为挂载文件内容变了而重建。
- 🔴 **改了代码要重新构建镜像** —— 代码是打进镜像的，`restart` 没用。
- 🔴 **只发布 `linux/amd64`**（ARM 不做：`pyatv` 依赖的 `miniaudio` 在 PyPI 上没有 Linux ARM 轮子）。
- ⚠️ 连不上设备时加 `--network host` —— 部分环境容器 NAT 会挡住局域网设备的响应。
- 🔴 **凭据**：`keys/atv_credentials.json` 能控制你的 Apple TV、`keys/bark.json` 是推送 key ——
  都靠 `:ro` 挂载进来，⛔ 别写进 `config.json`、也别提交进 git（已在 `.gitignore` 里排除）。

## 调试

```bash
python atv_tv_power.py --show-state   # 只读：打印 ATV 状态与「此刻若待机会怎么判」，不唤醒、不按键
python atv_tv_power.py --test-bark    # 手动推一条 Bark，验证告警通道
```

---

源码与完整文档（含实测结论与排障表）：<https://github.com/xinxinenjoy/Docker>
