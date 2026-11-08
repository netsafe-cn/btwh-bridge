# btwh-bridge · 燃热桥

[**English**](README.md) | **简体中文**

> **非官方项目。** 面向博世 **Therm 4800i** 燃气热水器的 BLE 互操作实现，
> 提供 Python 参考实现与 ESP32 常驻网关，最终以 **Home Assistant** 为主要接入目标。
>
> **实机验证机型：Bosch Therm 4800i**。其蓝牙模块广播名为 `BTWH-001-xxxx`
> （代码里匹配的就是这个名字，不是商品型号）。
>
> 本项目与 Robert Bosch GmbH 及其关联公司**无任何关系**，未获其授权、认可或支持。

官方 App 一次只能连一台手机、没有本地 API、数据全在云端。这个项目把热水器变成
**可编程的本地设备**：读水温、看燃烧状态、调温度，全流程不经过任何云。

**协议实现的全部成果都沉淀为可测试的代码**：Python 参考实现、跨端黄金向量、
ESP32 C++ 实现，三者用同一组十六进制向量对齐，防止两端实现漂移。

---

## 项目状态

| 模块 | 状态 | 说明 |
|---|---|---|
| Python 协议库 `bosch_api` | ✅ 可用 | **145 项**回归全绿，含签名冻结契约与黄金向量 |
| 桌面控制台 / 米家桥接 | ✅ 可用 | tkinter GUI + 巴法云 |
| ESP32-S3 网关固件 v2 | ✅ 已验证 | 实机连通通过：SMP 加密 → `A2/07` → `A2/08=01` → `A0` 稳定刷帧；断电重启自动恢复 |
| MQTT / Home Assistant 自动发现 | ⏳ 计划中 | 见 [架构与路线图](docs/ARCHITECTURE.md) |
| 配网（Improv WiFi） | 📐 契约已定 | 以 `python/device_fsm.py` + [API.md §9](docs/API.md) 为准 |

---

## 适合谁

三个层次，是叠加关系而不是三选一：第 3 层建立第 1 层的成果之上，第 2 层是调试第 3 层时不用
焊台的手段。

| 层次 | 你想要 | 拿走 | 前提 |
|---|---|---|---|
| **1** | 协议本身，有文档 | [`docs/BLE_PROTOCOL.md`](docs/BLE_PROTOCOL.md) + [`spec/protocol_vectors.json`](spec/protocol_vectors.json) | 不需要别的。用 Rust/Go/JS 重写，拿向量自证正确 |
| **2** | 现在就能用的客户端 | [`python/`](python/)：`pip install btwh-bridge`，`import bosch_api` | Python 3.9+ 与蓝牙。笔记本、树莓派、NAS 都行 |
| **3** | 刷进去就能用的设备 | [`firmware/bridge/`](firmware/bridge/)：烧一次 | 一块 ESP32-S3。配对码输一次，断电重启自动恢复 |

**第 1 层不只是文档。** 协议笔记里每个字段偏移，在 `spec/protocol_vectors.json` 里都有对应
的字节串，所以别的语言实现能**自证正确**，而不是靠猜。这也正是 Python 和 C++ 两份实现能
互相盯住的原因。

**第 2 层带了一整套调试工具**：桌面控制台、抓帧采集、日志诊断器。第 3 层就是靠它们做出来
的，你第一次遇到异常时也会需要。

**第 3 层是目标状态**：热水器出现在 Home Assistant 里，之后不再需要任何一台电脑。

---
|---|---|
| 硬件 | 笔记本 / 树莓派 / NAS | 微雪 ESP32-S3-DEV-KIT-N8R8 或同类 |
| 适合 | 试验、抓包、临时控制 | 7×24 常驻，接 HA / 米家 |
| 配对码 | 每次重新输入 | 烧一次，运行时输码，NVS 持久化 |
| 断线重连 | 进程内自动重连 | 固件内自动重连 + 断电重启自恢复 |
| 现状 | ✅ 可用 | ✅ 已验证 |

---

## 功能

| 功能 | 说明 |
|---|---|
| 实时水温 | 出水 / 进水 / 换热器 / 机身四路温度，0.1℃ 精度，4~8 秒刷新 |
| 燃烧状态 | 燃烧功率百分比、风机转速、电离火焰、水流量 |
| 设定温度 | 读取 + 写入（35~60℃），写入后约 4 秒生效并被设备确认 |
| 历史与故障 | B0 运行统计 / 洗浴记录，D7 故障列表 |
| 桌面控制台 | 单窗口 GUI，四路温度 + 曲线 + 历史弹窗 |
| 米家小爱 | 经巴法云同步，语音查询与调温（可选） |
| 通用 API | `python/bosch_api.py` 可被任意 Python 程序复用，自带回归测试 |

---

## 安装

需要 **Python 3.9+**。

```bash
pip install -r requirements.txt
```

Windows 上 `bleak` 会自动安装 WinRT 依赖；Linux 需要 BlueZ（多数发行版已自带）。

---

## 配置

首次运行会生成 `config.json`，也可以直接从示例复制：

```bash
cp config.example.json config.json
```

| 字段 | 说明 | 必填 |
|---|---|---|
| `device_address` | 热水器的蓝牙地址，如 `AA:BB:CC:DD:EE:FF` | ✅ |
| `login_mac` | **本机**蓝牙地址（登录身份，见下方说明） | ✅ |
| `pair_code` | 热水器面板显示的 6 位配对码 | ✅ |
| `bemfa_uid` | 巴法云私钥，留空则不同步米家 | ❌ |
| `topic_set` / `topic_up` / `topic_down` | 巴法云主题名 | ❌ |

**环境变量优先级高于配置文件**，适合容器部署或不想把配对码写进磁盘：

```bash
export BOSCH_PAIR_CODE=123456
export BOSCH_LOGIN_MAC=AABBCCDDEEFF
```

### 三个必填项怎么来

**① 设备蓝牙地址** —— 扫描得到，通常形如 `BTWH-001-XXXX`，设备名后四位是 MAC 末两字节。

**② 本机蓝牙地址** —— 设备维护精确的 MAC 白名单，登录帧里必须带它认识的地址。

- Windows：GUI 的「设置」里有「读取本机蓝牙地址」按钮，一键填入
- Linux：`cat /sys/class/bluetooth/hci0/address`

**③ 配对码** —— 在热水器面板上激活配对，屏幕会显示 6 位数字。这是**面板码，不是铭牌上的 S/N**，
且**短时效、每次不同**，同时是 BLE 链路层的 SMP passkey 与应用层 `A2/08` 密码。

---

## 使用

```bash
python tools/bosch_ui.py        # 桌面控制台（Windows 双击 tools/start_ui.bat）
python tools/bosch_bridge.py    # 无界面后台桥接（双击 tools/start_bridge.bat）
python tools/bosch_read.py      # 单次读取
```

### 作为库使用

```python
from bosch_api import Config, BoschService

svc = BoschService(Config.load())
svc.start()                      # 自动配对、登录、重连、收数据

svc.state                        # DeviceState(temp_out=45.0, fan_speed=800, ...)
svc.authorized                   # True 才是真的登进去了（唯一判据）
svc.pair_kind                    # 'first_pair' / 'rebind' / ''
svc.set_temperature(42)

svc.stop()
```

接口契约见 [`API.md`](docs/API.md)。**原则是只增不改**：已发布签名冻结，由
`tests/test_contract.py` + `api_contract.json` 在 CI 上强制校验，任何 breaking change 会让回归变红。

---

## ESP32 网关

固件在 [`firmware/`](firmware/) 下，一个应用一个目录：

| 目录 | 是什么 |
|---|---|
| [`firmware/bridge/`](firmware/bridge/) | ESP32-S3 无屏版，装在热水器旁边。BLE 进，MQTT 出。 |
| [`firmware/panel/`](firmware/panel/) | 预留：ESP32-S3 + 触摸屏，装在任何楼层。只走 MQTT，**不碰 BLE，也不链接桥的任何代码**。 |
| [`firmware/components/`](firmware/components/) | 多个固件共享的 C，不依赖框架，能在 PC 上单测。 |

桥的操作步骤见 [`firmware/bridge/README.md`](firmware/bridge/README.md)；
板卡手册与工具链说明在 [`docs/hardware/`](docs/hardware/)。

v2 相对早期可行性固件修掉了三个关键点：

1. **补上 BLE SMP PIN**：面板 6 位码同时是链路层 passkey 和应用层 `A2/08` 密码，
   早期固件用 Just Works 必然失败。
2. **配对码改为运行时输入**：串口/桌面面板输码，NVS 持久化，烧一次可反复重试
   （配对窗口只有几十秒，重新编译根本来不及）。
3. **断电重启自动恢复**：启动时若 NVS 里有码，直接发起一次连接；失败才退回等待输入。

排查失败时先跑日志诊断器，它把失败对照表变成了可执行规则：

```bash
python firmware/bridge/tools/log_diag.py            # 自动取最新 logs/*.log
python firmware/bridge/tools/log_diag.py <日志路径>
```

---

## 米家 / 小爱接入

依赖[巴法云](https://cloud.bemfa.com)（免费）。在控制台创建三个主题：

| 主题名 | 类型（由后三位决定） | 用途 |
|---|---|---|
| `boschset004` | `004` = 传感器 | 设定温度，上报 `#38` |
| `boschup006` | `006` = 开关 | 升温 +1℃，触发后自动复位 |
| `boschdown006` | `006` = 开关 | 降温 -1℃ |

然后在米家 App →「我的」→「其他平台设备」→ 搜索「巴法」→ 登录巴法云账号即可。

> **注意**：主题名后三位不是 `001`~`009` 的设备**不会同步到米家**。
> `010`（温控器）和 `011`（热水器）名字最像，但恰恰不同步，别用。

米家对第三方设备的固有限制：不能设置房间、不能批量控制、传感器只读、设备昵称在小爱里不生效。

---

## 测试与质量

```bash
python run_tests.py          # 一条命令跑全部（全离线：不连蓝牙、不连网络）
python run_tests.py -v       # 逐条列出
```

| 测试模块 | 项数 | 覆盖 |
|---|---|---|
| `test_protocol` | 13 | 组帧、填充、逐片异或、A0/B0/D7 解析 |
| `test_session` | 21 | 登录状态机、单飞收发、重发换 msgId、坏帧过滤 |
| `test_api` | 25 | 配置读写、环境变量覆盖、状态属性、巴法云解析、系统配对 |
| `test_service` | 22 | 服务层队列语义、配置热更新、幂等启动、失败原因透出 |
| `test_pair_reasons` | 20 | 配对失败原因码、认证状态、`A2/07=00` 语义 |
| `test_contract` | 7 | **签名冻结**、协议常量冻结、**跨端黄金向量** |
| `test_fsm` | 28 | 配网/配对正交性、超时、恢复、安全约束 |

三条纪律：

- 新增公开 API → 必须同时更新 `API.md`，再 `python tests/test_contract.py --update`
- 改协议层 → 必须同步 `protocol_vectors.json`，并让 ESP32 侧向量测试同样通过
- 连硬件的探针脚本 → 一律 `probe_` 前缀（`test_` 前缀会被自动纳入回归，CI 会挂）

CI 覆盖 Linux + Windows × Python 3.11/3.12，另有一个 job 检查 `config.json`
与硬编码密钥是否被误提交，见 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)。

---

### 版本发布

版本遵循[语义化版本](https://semver.org/lang/zh-CN/)。面向使用者的变更记录在
[CHANGELOG.md](CHANGELOG.md)；判定一次改动算 patch、minor 还是 major 的规则见
[CONTRIBUTING.md 第 6 节](CONTRIBUTING.md#6-versioning-and-releases)。是否"破坏兼容"
以 API 契约为准，不看改动的行数。

## 项目结构

```
.
├── python/                     参考实现
│   ├── bosch_api.py            ★ 协议编解码 / 设备会话 / 常驻服务
│   ├── device_fsm.py           ★ 配网 / 配对状态机
│   ├── bemfa.py                巴法云客户端（米家通道）
│   ├── boschlog.py             日志：轮转文件 + 内存环形缓冲
│   ├── singlelock.py           后台桥接的单实例锁
│   ├── tests/                  ★ 145 项离线测试，自动发现
│   └── tools/                  控制台、无界面桥接、采集、启动器
│
├── firmware/                   一个应用一个目录，互不共享源码
│   ├── components/             可复用的 C，无框架依赖，能在 PC 上单测
│   ├── bridge/                 ESP32-S3 桥：热水器 BLE -> MQTT（无屏）
│   └── panel/                  预留：触摸屏面板，只走 MQTT，不碰 BLE
│
├── spec/                       ★ 跨语言契约，CI 机械校验
│   ├── api_contract.json       签名冻结快照
│   ├── protocol_vectors.json   跨端黄金向量（Python <-> ESP32 逐字节）
│   └── mqtt.json               桥 <-> 面板契约：主题、字段、单位
│
├── docs/                       API.md、BLE_PROTOCOL.md、ARCHITECTURE.md、hardware/
├── integrations/               预留：Home Assistant、Node-RED、ESPHome
├── run_tests.py                一条命令跑 145 项，全离线
├── pyproject.toml              打包元数据（`pip install .`）
├── config.example.json         配置模板（不含任何真实信息）
├── CHANGELOG.md  CONTRIBUTING.md  SECURITY.md  CODE_OF_CONDUCT.md  LICENSE
```

三条边界是刻意划的，且由 CI 守住：

1. **`python/` 不 import 固件。** `import bosch_api` 通过 `pyproject.toml` 里的
   `package-dir` 保持扁平名字，所以搬目录不会变成 breaking change。
2. **`firmware/panel` 不链接 `firmware/bridge` 或 `components/`。** 面板是桥**之上**的
   应用：只说 MQTT，完全不知道 BLE 的存在。两个楼层的面板是平等的消费者，不是分支。
3. **`spec/` 是两侧唯一共享的东西。** `mqtt.json` 是桥 <-> 消费者的契约，桥按它发布，
   面板和任何集成按它渲染。任何一方改名，CI 会一直红到所有消费者跟上。

---

## 常见问题

**连不上，界面一直「等待重连」**

1. **是不是开了两个实例？** 两个程序抢同一台设备会互相踢下线
2. **手机上的官方 App 是不是连着？** 设备同一时刻只能有一个客户端
3. **设备是不是休眠了？** 长时间无连接后 BLE 广播会关闭 —— 按一下热水器面板，或放一下热水即可唤醒
4. **刚强杀过进程？** 系统蓝牙栈需要 10~15 秒释放连接，等一下再启动

**配对码错误 / 设备拒绝**

- 确认用的是**面板上显示的 6 位数字**，不是铭牌 S/N
- 面板重新配对后码会变，需要重新输入（ESP32 固件支持运行时输码，无需重烧）
- `login_mac` 必须与设备白名单一致，换了电脑要重新配对

**运行一段时间后设备不响应了**

这类设备有**连接频率保护**：密集连接会逐级降级（先停止推数据，再拒绝连接），
只能断电重启设备复位。日常使用请避免频繁重启程序。

**Windows 报 GATT 未就绪**

蓝牙栈的缓存问题，程序会自动重连。持续出现的话，关掉其他蓝牙软件、或重启系统蓝牙服务。

---

## 已知限制

- 只在 **Bosch Therm 4800i**（蓝牙模块 `BTWH-001-xxxx`）上验证过，其他型号/固件
  可能字段偏移不同，换机型先核对解析器
- 面板码**首次配对**必须在窗口内完成；一旦成功配对过，设备会长期保留该授权，
  之后可用同一码重连（已验证 11 分钟后仍有效，上限未知，以天/周计需长期观察）
- 连续连接失败 3 次会触发设备侧保护，约 3 分钟才恢复，不要连续重试
- 设备空闲后可能主动断开（早期在 Python 上观察到约 45~50 秒；ESP32 实测连续 8 分钟未断开）
- Windows 的 MTU 固定为 23，长帧会被拆分后自行重组
- 巴法云是第三方服务，长期可用性无保证（其天猫精灵通道已于 2024 年底停止）
- 厂商固件升级可能使私有协议失效，本项目不承诺长期可用

---

## 参与贡献

见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。最需要帮助的方向：更多型号的字段验证、
Home Assistant 接入、ESP32 配网实现。

参与本项目即表示同意遵守[行为准则](CODE_OF_CONDUCT.md)。

---

## 合规与免责

本项目基于对**自有设备**的 BLE 互操作研究，目的是让用户能用自己的设备做自动化。

- 仓库内**不包含**厂商 APK、固件、反编译产物或提取的密钥，也不接受此类贡献
- 不绕过任何付费、授权或安全机制；配对仍需物理接触面板并由人输入当天有效的码
- `Bosch` 等商标归其权利人所有，仅用于说明兼容对象，不表示任何关联或背书
- 互操作研究的法律边界因司法辖区而异，使用者需自行确认所在地区的合规性
- 协议细节仅供技术交流，**不得用于破解、篡改他人设备或商业分发**
- 软件按「现状」提供，不附带任何担保；因使用造成的设备损坏或数据丢失由使用者自负

---

## 许可

[MIT](LICENSE)
