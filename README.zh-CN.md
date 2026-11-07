# btwh-bridge · 燃热桥

[**English**](README.md) | **简体中文**

> **非官方项目。** 面向博世 `BTWH-001` 系列燃气热水器的 BLE 互操作实现，
> 提供 Python 参考实现与 ESP32 常驻网关，最终以 **Home Assistant** 为主要接入目标。
> 本项目与 Robert Bosch GmbH 及其关联公司**无任何关系**，未获其授权、认可或支持。

官方 App 一次只能连一台手机、没有本地 API、数据全在云端。这个项目把热水器变成
**可编程的本地设备**：读水温、看燃烧状态、调温度，全流程不经过任何云。

**协议实现的全部成果都沉淀为可测试的代码**：Python 参考实现、跨端黄金向量、
ESP32 C++ 实现，三者用同一组十六进制向量对齐，防止两端实现漂移。

---

## 项目状态

| 模块 | 状态 | 说明 |
|---|---|---|
| Python 协议库 `bosch_api` | ✅ 可用 | **136 项**回归全绿，含签名冻结契约与黄金向量 |
| 桌面控制台 / 米家桥接 | ✅ 可用 | tkinter GUI + 巴法云 |
| ESP32-S3 网关固件 v2 | ✅ 已验证 | 实机连通通过：SMP 加密 → `A2/07` → `A2/08=01` → `A0` 稳定刷帧；断电重启自动恢复 |
| MQTT / Home Assistant 自动发现 | ⏳ 计划中 | 见 [架构与路线图](docs/ARCHITECTURE.md) |
| 配网（Improv WiFi） | 📐 契约已定 | 以 `device_fsm.py` + [API.md §9](API.md) 为准 |

---

## 两种用法

| | Python 直连 | ESP32 网关（推荐长期） |
|---|---|---|
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
| 通用 API | `bosch_api.py` 可被任意 Python 程序复用，自带回归测试 |

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
python bosch_ui.py        # 桌面控制台（Windows 双击 start_ui.bat）
python bosch_bridge.py    # 无界面后台桥接（双击 start_bridge.bat）
python bosch_read.py      # 单次读取
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

接口契约见 [`API.md`](API.md)。**原则是只增不改**：已发布签名冻结，由
`test_contract.py` + `api_contract.json` 在 CI 上强制校验，任何 breaking change 会让回归变红。

---

## ESP32 网关

固件、工具链、板卡手册都在 [`esp32/`](esp32/)，操作步骤见
[`esp32/README.md`](esp32/README.md)。

v2 相对早期可行性固件修掉了三个关键点：

1. **补上 BLE SMP PIN**：面板 6 位码同时是链路层 passkey 和应用层 `A2/08` 密码，
   早期固件用 Just Works 必然失败。
2. **配对码改为运行时输入**：串口/桌面面板输码，NVS 持久化，烧一次可反复重试
   （配对窗口只有几十秒，重新编译根本来不及）。
3. **断电重启自动恢复**：启动时若 NVS 里有码，直接发起一次连接；失败才退回等待输入。

排查失败时先跑日志诊断器，它把失败对照表变成了可执行规则：

```bash
python esp32/gateway/log_diag.py            # 自动取最新 logs/*.log
python esp32/gateway/log_diag.py <日志路径>
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

- 新增公开 API → 必须同时更新 `API.md`，再 `python test_contract.py --update`
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
├── bosch_api.py            ★ 核心库：协议 / 设备会话 / 常驻服务
├── boschlog.py             统一日志（轮转文件 + 内存环形缓冲）
├── bemfa.py                巴法云 TCP 客户端（米家通道）
├── device_fsm.py           ★ 配网 / 配对状态机（框架无关的可执行规范）
├── bosch_ui.py             桌面控制台
├── bosch_bridge.py         无界面后台桥接
├── collector.py            数据采集（逐帧写 CSV，用于协议分析）
├── test_*.py               ★ 单元测试，自动纳入回归
├── api_contract.json       ★ 签名冻结快照（由 test_contract.py 生成）
├── protocol_vectors.json   ★ 跨端黄金向量（Python ↔ ESP32 逐字节对照）
├── API.md                  接口契约（只增不改）
├── BLE_PROTOCOL.md         协议笔记：帧格式、字段表、待挖掘能力
├── docs/                   架构与决策记录
├── esp32/                  ESP32 网关：固件 / 工具链 / 手册下载指引
├── config.example.json     配置模板（不含任何真实信息）
├── pyproject.toml          打包元数据（`pip install .`）
├── CHANGELOG.md            版本记录
├── CODE_OF_CONDUCT.md      社区行为准则
└── LICENSE                 MIT
```

界面只负责显示，蓝牙逻辑全在 `bosch_api`，云同步全在 `bemfa`。
换界面（Qt / Web / 命令行）不需要动协议代码。

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

- 只在 `BTWH-001` 系列上验证过，其他型号/固件可能字段不同
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
