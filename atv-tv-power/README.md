# Apple TV 待机 → 自动关闭电视

按 Apple TV 遥控器让 ATV 待机时，把它旁边那台「CEC 关不掉」的电视一起关掉。

```
Apple TV ──Companion 协议推送(实时)──▶ 本服务 ──HTTP 6095(局域网)──▶ 电视
```

> ## ⚠️ 设备支持范围（2026-10-05 补）
>
> **本项目目前只在【小米电视】上实测通过**，其余品牌**一律未测试**。
>
> | | 状态 |
> |:---|:---|
> | 小米电视（MiTV-ANSP0 / Android 9 / MIUI TV 22.12.6.2963） | ✅ **已实测**（长时间运行验证过，见「实测结论」） |
> | 其它品牌（索尼 / 三星 / LG / TCL / 海信 / 创维 …） | ❓ **未测试，不确定能不能用** |
>
> **关键不确定点**：本方案依赖电视自带的**局域网 HTTP 控制接口 + 端口 `6095`**。
> 这个 `6095` 是**小米电视**的接口端口 —— **其它品牌几乎不可能一样**（换个品牌、甚至换个机型都可能不同）。
> 若你的电视不是小米，**不要直接照抄 `tv.port: 6095`**，需要自行确认：
>
> 1. 电视是否有等价的**局域网 HTTP 控制接口**（不是 CEC、不是 ADB、不是厂商云 API）；
> 2. 该接口的**真实端口**与**请求路径**（本项目用的是
>    `/request?action=isalive` 判状态、`/controller?action=keyevent&keycode=power` 发电源键）；
> 3. `keyevent` 是否为**切换语义**（本项目据此在发键前先确认电视处于开机态，否则会把已关的电视**打开**）。
>
> 三者只要有一处不同，就需要改 `atv_tv_power.py` 里的 `TvController`（接口很小，见该类的三个方法）。
> **欢迎把你的品牌与实测结果补进「实测结论」一节。**

## 为什么要自己做一个

不少客厅电视的 HDMI-CEC 实现不完整：**能**响应 ATV 的 CEC 开机，但**不响应** CEC Standby —— 遥控器关机后电视仍亮着。

本服务是自持的最小实现：只依赖两条**已实测**的通道，不依赖厂商云、不加任何硬件。

---

## 支持的平台

| 平台 | 覆盖设备 |
|:---|:---|
| `linux/amd64` | x86-64 的 PC / NAS / 云服务器 |

镜像名 `xinxinenjoy/atv-tv-power`。

> ⚠️ **只提供 `linux/amd64`（x86-64）** —— 现在绝大多数 NAS / 小主机 / 云服务器都是这个架构。
> 本项目**明确不支持 ARM**：`pyatv` 依赖的 `miniaudio` 在 PyPI 上**没有**任何 Linux ARM 轮子
> （aarch64 没有、armv7l 只有 cp37 的老轮子），只能现场编译；而 `pyatv/helpers.py` 在 `import`
> 期就要用它（无 `try/except`），绕不开也跳不过 ⇒ 构建成本过高、放弃。
> ARM 设备（树莓派等）请自行在设备上构建，或改用其他方案。

---

## 快速开始

前提：装好 `config.json` 与配对凭据 `keys/atv_credentials.json`（见下）。

```bash
docker run -d --name atv-tv-power \
  -v /path/to/config.json:/app/config.json:ro \
  -v /path/to/keys:/app/keys:ro \
  --restart unless-stopped \
  xinxinenjoy/atv-tv-power:latest
```

或 `docker compose`：

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

> 💡 `config.json` 与 `keys/` **不打进镜像**（含设备地址与配对凭据），一律靠挂载进来。
> 连接设备有问题时，可加 `--network host`（部分环境下容器 NAT 会挡住局域网设备的响应）。
>
> 💡 设备地址也可以走环境变量（`ATV_HOST` / `TV_HOST` / `TV_PORT`），见「配置」一节的
> 「用环境变量覆盖设备地址」——上面 `docker run` 加 `-e TV_HOST=…` 即可。

### 先准备两份东西

1. **配对凭据**：把 Apple TV 的 IP 填进 `config.json` 的 `atv.host`，然后跑配对 —— PIN 会显示在电视上：

   ```bash
   python tools/pair_atv.py --host <你的ATV地址>
   ```

   产物写入 `keys/atv_credentials.json`。

2. **`config.json`**：照 [`config.example.json`](config.example.json) 复制一份，改掉里面的地址与推送设置。

---

## 配置

`config.json`：

| 键 | 默认 | 说明 |
|:---|:---|:---|
| `delay_seconds` | 20 | ATV 待机后等多久才关电视。期间若 ATV 被唤醒则取消（防误触） |
| `poll_seconds` | 30 | **心跳探活间隔**（也是状态对账间隔）—— 每轮真发一次 `fetch_attention_state()`，顺带发现推送漏掉的事件。别调得比 5s 还小（探活本身有 5s 超时） |
| `retries` / `retry_interval` | 3 / 5 | 关电视失败重试次数与间隔 |
| `reconnect_delay` | 30 | ATV 掉线后的重连间隔 |
| `atv.host` | — | Apple TV 地址（可被环境变量 `ATV_HOST` 覆盖） |
| `atv.credentials` | `keys/atv_credentials.json` | 配对产物 |
| `tv.host` / `tv.port` | — / 6095 | 电视地址与局域网 HTTP 接口端口（可被 `TV_HOST` / `TV_PORT` 覆盖）。⚠️ **`6095` 是小米电视的端口，其它品牌未测、未必一致** —— 见文首「设备支持范围」 |
| `tv.timeout` | 4 | 单次 HTTP 超时（判"已待机"靠的就是它超时） |
| `tv.verify_delay` | 4 | 发键后复查状态的等待秒数 |
| `guard.enabled` | true | **防误关判据**（见下节）。关掉则退回「ATV 一待机就关电视」的旧逻辑 |
| `guard.on_unknown` | `skip` | 拿不到 ATV 原始状态时的取舍：`skip` = 不关电视（保守）/ `close` = 按旧逻辑关 |
| `bark.*` | — | 失效告警推送（`enabled` / `key_file` / `group` / `sound` / `icon` / `offline_after`） |

### 用环境变量覆盖设备地址（可选）

设备地址除 `config.json` 外，**也可以用环境变量给** —— 便于在 compose / `.env` 里配、不用改 `config.json`：

| 环境变量 | 覆盖的配置键 | 说明 |
|:---|:---|:---|
| `ATV_HOST` | `atv.host` | Apple TV 地址 |
| `TV_HOST` | `tv.host` | 电视地址 |
| `TV_PORT` | `tv.port` | 电视接口端口（默认 6095） |

**嘴径（很关键）**：环境变量**优先，但只覆盖「非空」的值** —— 不传、或传空串，就用 `config.json` 里的值。
⇒ 不设这几个变量时行为与从前**完全一致**，可放心升级。

compose 里这样写（默认值一律留空，靠 `.env` 覆盖）：

```yaml
    environment:
      - TZ=Asia/Shanghai
      - ATV_HOST=${ATV_HOST:-}
      - TV_HOST=${TV_HOST:-}
      - TV_PORT=${TV_PORT:-}
```

> 💡 **`${VAR:-}` 的写法**：`${VAR:-默认值}` = 「变量没用就用默认值」，`:-` 后面直接写默认值即可。
> 这里默认值是**空**，表示「没传就交给 `config.json`」。
> **如果直接写死字面值**（如 `- TV_HOST=192.168.1.50`），**不需要任何额外符号**，IP 也不用加引号；
> 只有当值里本身含 `$` 时才要写成 `$$`（compose 的转义）—— IP / 端口不含 `$`，不用管。

`config.json` 仍是**默认来源**；环境变量只在这三项上提供一层覆盖。

---

## 它是怎么工作的

1. 容器内一个 Python 进程常驻。
2. 用 pyatv 连 Apple TV 的 Companion 协议，**订阅电源状态推送**（不是轮询 —— ATV 一进待机事件立刻就到）。
3. 收到「待机」→ 等 `delay_seconds`（默认 20s）；期间 ATV 若被唤醒就取消，防误触。
4. **防误关判据**：查「待机前 ATV 的**原始**状态」，只有 `Awake` 才继续；`Screensaver`/`Idle` 说明是 ATV 闲置后自己睡着的 ⇒ **不关电视**。
5. 通过判据 → HTTP 请求电视 `6095` 发一次电源键 → 4 秒后复查确认已关。
6. 关电视重试耗尽 / ATV 连续失联 / 判据降级 → 走 Bark 推一条告警（同一问题不刷屏）。
7. **心跳探活**：每 `poll_seconds` 真发一次 `fetch_attention_state()` —— 这是**整个服务唯一**会走网络的存活检查。连接死了就在 5s 内失败 ⇒ 退出会话 ⇒ 重连。心跳读到的状态同时用于**对账**：与本地记录不符（推送丢了）就按新值补偿，避免漏关。

### 🔴 防误关判据：为什么要看「原始状态」

**问题**：ATV 有自己的屏保 / 睡眠计时器，**和你切没切电视输入源无关**。你正用电视的另一个 HDMI 看别的内容，ATV 到点自己睡着 —— 此时若无条件关电视，就是一次误关。

**根因**：pyatv 把设备上报的原始状态做了**有损压缩**（`companion/__init__.py` 的 `_system_status_to_power_state`）：

| 设备原始状态（`SystemStatus`） | pyatv 交给我们的 `PowerState` |
|:---|:---|
| `Asleep` (0x01) | `Off` |
| `Screensaver` (0x02) | `On` ← **三合一** |
| `Awake` (0x03) | `On` ← **区分被抹掉** |
| `Idle` (0x04) | `On` ← pyatv 自己标注 "Not verified"，实测也从未见过 |

**解决办法**：绕开这层映射，从 `atv._interfaces[Power]._interfaces[Protocol.Companion].api` 拿到 `CompanionAPI`，再 `listen_to("SystemStatus", cb)` + `subscribe_event("SystemStatus")`。

（`listen_to` 走 `MessageDispatcher`，是 **append** ⇒ 不会覆盖 pyatv 自己的电源回调；`subscribe_event` 内部有去重 ⇒ 重复订阅安全。⚠️ 回调会被 `loop.call_soon` 调用，须写成**同步函数**。）

**决策表**：

| 待机前的原始状态 | 推断 | 动作 |
|:---|:---|:---|
| `Awake` | 你正在用 ATV，是按遥控器主动关机 | **关电视** |
| `Screensaver` / `Idle` | ATV 闲置后自己睡着的 | **不关电视** |
| 拿不到（降级） | 判据失效 | 按 `guard.on_unknown`（默认 `skip` 不关）+ Bark 告警 |

**已知取舍**（写在这里免得以后当 bug 查）：在 ATV 主界面停到它进屏保、**之后**再按遥控器关机，会被判成「自己睡着」而不关电视。代价是**偶尔该关没关**（手按一下遥控器即可），换来的是**不误关**。

**为什么不能从电视侧判断**（三条路都堵死了，别再试）：

| 途径 | 结论 |
|:---|:---|
| 电视 6095 | **没有任何「读当前输入源 / 前台 App」的接口** —— 17 个候选全 404（网传的 `getCurrentApp` 是内容农场编的）。开机状态下反复切 App / HDMI 源共 6 次，`url`/`build`/`platform`/`stream` 四个字段零变化 |
| UPnP 49152 | 是纯 DLNA **DMR**，只有三个标准服务，**零厂商扩展状态属性** |
| ADB 5555 | 电视每次关机都会把「ADB 调试」复位 ⇒ 不可持续（已弃用） |

---

## 实测结论（勿凭直觉推翻）

### Apple TV 侧

| # | 结论 | 影响 |
|:---|:---|:---|
| 1 | pyatv 的 `StateProducer.listener` setter 内部是 `weakref.ref(target)` | **listener 必须被强引用持有**；写成 `atv.power.listener = MyListener()` 会因对象当场被 GC 而**永远收不到推送** |
| 2 | ATV 待机后 Companion 长连接**不断**，待机事件实时送达 | 可用长连接 + 推送，无需轮询 |
| 3 | ATV 待机时 `7000`/`49153` 端口**仍然开放** | 不能靠探端口判断 ATV 电源状态 |
| 4 | 单纯 `connect` + 读 `power_state` **不会**唤醒 ATV | 可放心做只读探活（`fetch_attention_state()` 同理，不唤醒） |
| 5 | pyatv 0.18 配对是两段式：`pairing.pin(x)` → `await pairing.finish()` | 老教程的 `finish(pin)` 在 0.18 报 `TypeError` |

### ⚠️ 长连接保活

| 事实 | 影响 |
|:---|:---|
| pyatv 的 Companion 协议**没有任何保活**：协议内无应用层心跳、无周期任务；`tcp_keepalive()` 只被 MRP 协议调用，**Companion 一次都没调** | 长空闲后连接被路由器 / AP **静默回收**（TCP 半开）时，客户端**无从察觉** |
| `atv.power.power_state` 读的是 pyatv 的**内存缓存**（`companion/__init__.py` 的 `return self._power_state`），**不发任何网络请求** | ⚠️ **拿它当探活 = 假探活**：连接死了它既不报错、还一直返回旧值 |
| 真正走网络的只有 `CompanionAPI.fetch_attention_state()`（pyatv 内置 5s 超时 ⇒ 连接死了必然失败） | 本服务每 `poll_seconds` 调它一次作**心跳**，失败即退出会话、重连 |

> 🔴 **事故教训（跨项目通用）**：**事件驱动型长连接服务的探活必须走网络，读内存缓存不算探活。**
> 曾发生过：会话订阅成功后连接静默死亡 ⇒ 推送收不到、读 `power_state` 不报错、循环永不退出 ⇒
> 连告警都没响，服务全哑 60 小时，只能手动重启。修复点就是上面第 7 条的心跳探活。

### 电视侧

| 状态 | TCP 6095 | `/request?action=isalive` |
|:---|:---|:---|
| 开机 | 开放 | `200 OK` |
| 待机 | 超时 | 超时 |
| 关机瞬间 | — | `502`（过渡态） |

| # | 结论 | 影响 |
|:---|:---|:---|
| 6 | 6095 是该类电视自带的局域网 HTTP 控制接口，**不需要 token / ADB / 开发者选项** | 这是官方 App / 语音助手控制电视的本地通道 |
| 7 | `keyevent` 是**切换语义**（`power` = 开/关翻转） | 发键前必须确认电视处于开机态，否则会把已关的电视**打开** |
| 8 | 待机时 6095 完全不响应 | 无法用它开机；开机由 ATV 的 CEC 负责 |
| 9 | UDP 54321 有 miio 响应 | 本方案用不到，备查 |

**为什么没用 ADB**：ADB 曾经可用（`input keyevent 26` 能关电视），但电视在关机后会把「ADB 调试」开关复位，下次开机 5555 不再监听 —— 每次都要人工去设置里重开，不可持续。6095 HTTP 接口不存在这个问题，且同时提供「控制」与「状态判断」，故弃用 ADB。

---

## 本机调试

```bash
pip install -r requirements.txt
python atv_tv_power.py
```

诊断用（全程只读，不唤醒、不按键）：

```bash
python atv_tv_power.py --show-state     # 打印 ATV 电源状态 / 原始状态 / 待机前快照，以及「此刻若待机会怎么判」
python atv_tv_power.py --test-bark      # 手动推一条 Bark，验证告警通道
```

---

## 排障

| 现象 | 原因 / 处理 |
|:---|:---|
| `找不到 Apple TV` | 设备换 IP 或休眠较深。核对 `config.json` 的 host；`atvremote scan` 复查 |
| 日志有 `ATV 进入待机` 但电视没关 | 先看紧随其后的那行：若写「**跳过关电视 —— 待机前 = Screensaver**」，这是**预期**的防误关；若写「关电视：…」再对照「复查状态」，显式 `off` 说明电视本就关着（正常） |
| 电视该关却没关 | 跑 `python atv_tv_power.py --show-state` 看判据怎么判的；再看 ATV 设置里屏保 / 睡眠时长。若判据**降级**（会推 🟡 告警），把 `guard.on_unknown` 改 `close` 可退回旧行为 |
| 电视被**打开**了（而不是关掉） | `keyevent` 是切换语义 —— 说明发键前状态判断出错。检查 `tv.port` / host 是否指到了别的设备 |
| 推送收不到 | 十有八九是 listener 被 GC（见结论 1）。本服务的 `_listener` 是强引用，改动代码请保留 |
| **长期运行后完全收不到 ATV 事件** | 旧缺陷：Companion 长连接无保活 + 兜底轮询读的是缓存 ⇒ 连接静默死亡后无人发现。新版已修（心跳探活） |
| IP 变了 | 路由里给电视和 ATV 都做 **DHCP 静态绑定**，否则会偶发失联 |
| 凭据失效（设备恢复出厂等） | 重跑 `tools/pair_atv.py` |
| **改了配置但行为没变** | `config.json` 是**挂载**进去的 ⇒ 改完要**重启容器**（compose 不会因为挂载文件内容变了而重建） |
| **改了代码但行为没变** | 代码是**打进镜像**的 ⇒ 必须重新构建镜像，`restart` 没用 |

---

## 工具

| 脚本 | 用途 |
|:---|:---|
| `tools/pair_atv.py` | 与 Apple TV 做 Companion 配对，产出 `keys/atv_credentials.json`（PIN 显示在电视上） |
| `tools/test_guard.py` | **防误关判据的回归测试**：离线跑决策表（不连设备、不发请求）。改动判据函数后**务必先跑它** |
| `tools/test_heartbeat.py` | **连接探活的回归测试**：离线验证「连接死了必须被发现」—— 反例（`fetch` 抛异常 → 会话退出并清理干净）、正例（健康时不退出）、状态对账、拿不到 API 时不误判。改动心跳 / 会话循环后**务必先跑它** |

---

## ⚠️ 凭据管理

`keys/atv_credentials.json` 含可控制你 Apple TV 的配对凭据，`keys/bark.json` 含 Bark 推送 key：

- 都已在 `.gitignore` 中排除，不会进 git；
- ⚠️ Bark 的 key **不要**图省事写进 `config.json` —— 那文件在模板形态下虽已脱敏，但你自己那一份很容易被误提交，key 一律放 `keys/bark.json` 并用 `bark.key_file` 指向它。
