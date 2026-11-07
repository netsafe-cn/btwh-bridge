# bosch_api 接口契约

版本：v1.0（2026-11-01）
原则：**只增不改**。已发布的函数/方法签名与语义冻结；新增功能一律以新函数、新可选参数（带默认值）的形式加入。

---

## 1. 稳定性承诺

| 规则 | 说明 |
|---|---|
| 签名冻结 | 不删除、不改名、不加必填参数；新增参数必须带默认值 |
| 语义冻结 | `True/False` 的意义、异常类型、返回值结构不变 |
| 新增 | 只通过新函数或新可选参数扩展 |
| 回归门槛 | 任何变更后 `python run_tests.py` 必须全绿（当前 59 项） |
| Breaking 变更 | 需要升主版本号，并在本文档记录迁移说明 |

---

## 2. 分层与职责

```
Config          配置读写（含 env 覆盖）
DeviceState     设备状态数据类（19 字段 + 兼容属性）
BoschProtocol   纯编解码/解析（无 IO，可完全单测）
BoschDevice     单次会话（连接、登录、收发、查询）
BoschService    常驻服务（后台线程、自动重连、命令队列、结果缓存）
配对函数         模块级：ensure_paired / paired_state / unpair_device / parse_mac
```

**API 不含任何界面逻辑**。界面层只负责展示与采集用户输入，配对、登录、重连均由 API 完成。

---

## 3. 配对接口（模块级）

| 函数 | 签名 | 说明 |
|---|---|---|
| `paired_state` | `async (address: str) -> str` | 返回 `'paired'` / `'unpaired'` / `'unknown'`（设备不可达或非 Windows） |
| `ensure_paired` | `async (address: str, log=None) -> bool` | 确保系统级配对。未配对则自动 Just Works 绑定。`True`=已配对或无需处理 |
| `unpair_device` | `async (address: str) -> bool` | 删除本机与设备的配对（清残留）。`True`=已解除或本来就没配对 |
| `parse_mac` | `(address: str) -> int` | `'AA:BB:CC:DD:EE:FF'` → int |

### 配对的两个层次（重要）

```
链路层（BLE bonding）  ← ensure_paired 负责，程序可自动完成
    前提：设备处于"配对窗口"。窗口由面板【清除配对】动作打开。

应用层（MAC 授权）     ← BoschDevice.login() 负责，程序自动完成
    前提：config.pair_code 是面板当前显示的 6 位码。
```

**程序无法远程打开配对窗口**（必须人工在面板操作），这是物理限制，不是 API 缺陷。
窗口是否打开的外部判据（API 内部也据此判断）：
- BLE 配对请求为 `PROVIDE_PIN`（而非 `CONFIRM_ONLY`）
- `A2/07` 应答 `02`（而非 `00`）

---

## 4. 会话接口（BoschDevice）

| 方法 | 签名 | 说明 |
|---|---|---|
| `connect` | `async () -> None` | 建立链路 + 订阅 FIFO/CREDITS（不读任何特征） |
| `close` | `async () -> None` | 断开并清理 |
| `login` | `async () -> bool` | `A2/07` →（免密也补发）`A2/08`；成功后设备开始推 A0 |
| `send` | `async (cmd, data=b"", receiver=0x00, tag="") -> None` | 单帧发送（分片、填充、加密） |
| `send_and_wait` | `async (cmd, data=b"", receiver=0x00, tag="", timeout=6.0, retries=5) -> bytes` | 单飞：等应答，超时重发换 msgId；耗尽抛 `TimeoutError` |
| `set_temperature` | `async (value: int) -> None` | 写设定温度（35~60），走 `send_and_wait` |
| `read_history` | `async (kind=2) -> bytes` | B0 历史数据，2=运行统计 3=洗浴记录 |
| `read_errors` | `async (count=10) -> bytes` | D7 故障列表 |
| `idle_seconds` | `async () -> float` | 距上次收到数据的秒数 |

属性：`state: DeviceState`、`login_state: str`、`cooldown_sec: float`

### 异常契约

| 异常 | 场景 |
|---|---|
| `TimeoutError` | `send_and_wait` 重试耗尽；`set_temperature` 未获确认 |
| `RuntimeError("GATT 未就绪")` | 服务枚举失败（通常是配对失效） |
| `RuntimeError("未连接")` | 未 connect 就 send |

---

## 5. 服务接口（BoschService）

同步接口，线程安全，供 GUI / 脚本 / 自动化调用。

| 成员 | 类型 | 说明 |
|---|---|---|
| `start()` / `stop(timeout=3)` | 方法 | 启动/停止后台线程与连接循环 |
| `running` | 属性 | 线程是否存活 |
| `state` | 属性 | 最近一次 `DeviceState`（可能为 None） |
| `status` | 属性 | 连接状态文案（直接展示） |
| `login_state` | 属性 | 登录状态文案（直接展示） |
| `connected` | 属性 | 链路是否连上 |
| `set_temperature(v)` | 方法 | 入队（不阻塞，失败不抛） |
| `query_history(kind=2)` | 方法 | 入队，结果写入 `results['history']` |
| `query_errors(count=10)` | 方法 | 入队，结果写入 `results['errors']` |
| `results` | 属性 | `{'history': bytes, 'history_ts': float, ...}` |
| `take_logs()` | 方法 | 取走并清空日志队列 |
| `update_config(cfg)` | 方法 | 热更新配置并触发重连 |
| `set_pair_code_and_reconnect(code, save=True)` | 方法 | 改配对码并立即重连（配对窗口短，用于快速响应） |

**自动行为**（调用方无需关心）：
- 连接前 `ensure_paired`
- 连续 3 次 GATT 失败 → `unpair_device` 自愈
- 3C/5A 冷却 → 重连间隔至少 30/90 秒
- 稳定会话被踢 → 30 秒后再连
- 登录成功后清空陈旧命令队列
- 配置变更 → 安全退出当前会话后重连

---

## 6. 协议层（BoschProtocol，纯函数）

| 方法 | 说明 |
|---|---|
| `xor` / `encode_chunks` / `build` / `stuff` / `unstuff` / `decrypt_chunk` / `decode_stream` | 编解码 |
| `parse_state(payload) -> DeviceState` | 解析 A0（自动区分 Type0/Type1） |
| `parse_history(payload) -> dict` | 解析 B0：`{type, a(运行分钟), b(点火次数), raw}` |
| `parse_errors(payload) -> List[int]` | 解析 D7：跳过 `0xEA` 状态字节，返回故障码列表 |
| `encode_pair_code(code) -> bytes` | 面板码 → 6 字节（每位一个半字节） |

---

## 7. 调用示例

### 7.1 日常使用（最常见）

```python
from bosch_api import Config, BoschService

svc = BoschService(Config.load())
svc.start()                       # 自动配对、登录、重连、收数据
print(svc.state.temp_out)         # 出水温度
svc.set_temperature(42)
svc.query_errors()
```

### 7.2 首次配对 / 换电脑（需要人工两步）

```python
from bosch_api import (Config, BoschDevice, ensure_paired,
                       paired_state, unpair_device)

cfg = Config.load()
addr = cfg.device_address

# 第 0 步（人工）：面板 → 清除配对（打开窗口）
# 第 1 步（人工）：面板 → 连接/配对 → 读 6 位码
cfg.pair_code = "123456"          # ← 人工读到的码

# 第 2 步（程序）：链路层
if not await ensure_paired(addr, log=print):
    raise SystemExit("配对失败：请到面板执行【清除配对】后重试")

# 第 3 步（程序）：应用层
dev = BoschDevice(cfg)
await dev.connect()
if not await dev.login():
    raise SystemExit("登录失败：%s" % dev.login_state)
await dev.close()
```

### 7.3 排查配对状态

```python
state = await paired_state(addr)   # 'paired' / 'unpaired' / 'unknown'
if state == "unpaired":
    await ensure_paired(addr)      # 自动重配（需窗口打开）
elif state == "paired":            # 但设备不认（链路层失败）→ 清残留再配
    await unpair_device(addr)
```

### 7.4 数据采集

```python
from collector import main      # 见 tools/collector.py，逐帧写 CSV
```

---

## 8. 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-11-01 | v1.0 | 初版契约；配对接口（ensure_paired/paired_state/unpair_device）、B0/D7 解析、A0 十九字段 |
| 2026-11-07 | v1.0.1 | 修正 `set_pair_code_and_reconnect` 对非字符串输入抛 `AttributeError` 的缺陷，统一为 `ValueError`（bugfix，**签名未变**）；新增签名冻结契约 `api_contract.json`、跨端黄金向量 `protocol_vectors.json`、服务层与状态机单测，回归 59 → 112 项 |

---

## 9. 设备侧状态机契约（ESP32 桥接器）

**规范实现**：`device_fsm.py`（框架无关、可单测），回归用例 `tests/test_fsm.py`。
C++ 固件必须与本文同构；改状态机要先改这里的规范并跑回归。

### 9.1 配网与配对正交

| 轴 | 目标 | 状态 | 持久性 |
|---|---|---|---|
| 配网 provisioning | ESP32 拿到 WiFi SSID / 密码 | `unprovisioned → provisioning → connecting → connected / failed` | 一次性，凭证存 NVS |
| 配对 pairing | 热水器接受 ESP32 的 BLE 授权 | `idle → window_wait → pin_ready → bonding → login → online / failed` | 可反复，6 位码短时效 |

**禁止把两者合并成一个「配对模式」开关**：换路由器不应要求重新到面板「清除配对」，
热水器掉线也不应要求重输 WiFi 密码。`reset_wifi_only()` 只清 WiFi，保留 bond 与 pin。

### 9.2 对外组合模式

| 模式 | 进入条件 | 行为 |
|---|---|---|
| `setup` | 无 WiFi 凭证 / 用户主动请求 | 开配网通道；**仍允许同时与热水器配对**（串口调试阶段就这么用） |
| `normal` | 凭证齐全、连接成功 | 正常桥接 BLE ↔ MQTT |
| `recovery` | WiFi 连续失败 ≥ 3 次 | 开 AP 等重配；**不改配对轴状态** |

### 9.3 物理约束

1. 配对窗口只能由人在面板【清除配对】打开，程序无法远程触发。
2. 6 位码短时效且每次不同：运行时输入，不编译进固件、不当长期凭证。
3. 面板码同时是 BLE SMP passkey 与应用层 `A2/08` 密码。
4. 配对窗口默认 60 秒，超时需重取码；`online` 之后不再受窗口约束。

### 9.4 NVS 键空间（C++ / Python 必须一致）

| namespace | keys | 归属 |
|---|---|---|
| `bosch_wifi` | `ssid`, `pass` | 配网流程 |
| `bosch_heater` | `addr`, `login_mac`, `pin` | 配对流程 |
| `nimble_bond` | `*` | **NimBLE 自管，配网流程禁止删除** |

### 9.5 安全约束

`snapshot()` 供 MQTT / HA 上报，**只输出 `has_pin` 布尔，绝不输出 pin 明文或 WiFi 密码**。
开源前必须剔除 `config.json`（含真实 MAC、配对码、巴法云 UID），
改为 `config.example.json` + 环境变量注入；历史探针脚本里也可能硬编码了密钥，需逐一清理。

---

## 10. 配对失败原因与提示契约

**背景**：配对窗口由人在面板打开，时效只有几十秒，程序无法远程打开或延长。
失败时如果只报"连不上"或"MAC 未授权"，用户会去改错的东西（改 `login_mac`、换电脑），
而真正该做的是回面板重新取码。因此把失败原因固化成**原因码**，与中文文案解耦。

### 10.1 原因码（模块级常量）

| 常量 | 值 | 触发条件 | 面向用户的一步动作 |
|---|---|---|---|
| `PAIR_FAIL_WINDOW_CLOSED` | `window_closed` | `A2/07` 应答 `00` | 面板『清除配对』→ 立刻『连接/配对』取码 → 30 秒内输入 |
| `PAIR_FAIL_CODE_REJECTED` | `code_rejected` | `A2/08` 应答 `00`，且窗口开着 | 码已失效，重新取码后立即输入 |
| `PAIR_FAIL_CODE_MISMATCH` | `code_mismatch` | `A2/08` 应答 `02` | 核对是否为当前面板码（不是铭牌 S/N） |
| `PAIR_FAIL_NO_RESPONSE` | `no_response` | 6s × 5 次重发耗尽 | 确认仍停留在配对界面；必要时断电重启热水器 |
| `PAIR_FAIL_COOLDOWN` | `cooldown` | `A2/07` 应答 `3C` / `5A` | 等 30~90 秒，不要连续重连 |
| `PAIR_FAIL_LINK` | `link_failed` | 链路未建立 | 靠近、唤醒面板、关掉手机 App |

读取方式：

```python
dev.pair_fail_reason      # 'window_closed' | None | ...
dev.pair_fail_hint()      # 可直接展示的一句动作建议
```

**不要靠匹配 `login_state` 的中文文案判断原因**——文案会调整，原因码才是契约。

### 10.2 `A2/07` 的应答位是「窗口状态」，不是「MAC 授权状态」

2026-11-07 ESP32-S3 真机实测：

| 应答 | 含义 | 说明 |
|---|---|---|
| `02` | 窗口开着，要求提交码 | 正常路径，随后发 `A2/08` |
| `01` | **MAC 已登记，仍需配对码** | 实测 2026-11-07 出现 `01 → A2/08=00` 失败，故 `01` **不是终态**；仍补发 `A2/08` |
| `00` | **窗口未打开或已超时关闭** | v1.0 文档曾标为「MAC 未授权」，实测证实是窗口状态 |

据此定两条硬规则：

1. `00` 时**不要**让用户去改 `login_mac`。仍补发 `A2/08`（App 同样行为），
   但原因码必须记为 `window_closed`。
2. 后续 `A2/08 = 00` **不得覆盖** `window_closed`。否则会把「没开窗口」
   误报成「码错」，用户去核对码是白折腾。

### 10.3 配对窗口与码的有效期

- **首次配对**：从面板出码到设备校验，通常只有几十秒；程序无法延长
- 推荐动作序列：`清除配对` → `连接/配对` 取码 → **30 秒内**完成提交
- 取码后**不要退出配对界面**，退出即关窗
- 失败后不要连续重试：设备有连接频率保护（`3C` 冷却 30s、`5A` 冷却 90s）

**配对成功之后，该码会被设备长期保留**（2026-11-07 实测）：

> 19:21:55 用码 `123456` 配对成功 → 19:24 断开 → 19:32（11 分钟后、窗口早已关闭）
> 用同一码重连，`A2/07=01`、`A2/08=01`，继续正常收 A0。

推论：

1. 面板码的真正时效分两段——**首次配对时短**，**成功后被绑定为长期凭证**
2. 从未成功配对过的码无效（实测 `333223` 在已认证状态下仍被判 `00`）
3. 因此「断电重启后能否自动恢复」取决于**固件是否主动用 NVS 里的码发起连接**，
   而不是设备侧限制

**当前固件行为**：启动后不主动连接，需人工输码或发 `go`。这是待改进项。
**有效期上限尚未测出**（11 分钟已验证可用，以天/周计需长期观察）。

### 10.4 结构化认证状态

上层（界面、MQTT 上报、诊断工具）**不要解析 `login_state` 的中文文案**，改用这些字段：

| 成员 | 类型 | 含义 |
|---|---|---|
| `mac_registered` | bool | 设备认得本机 MAC（`A2/07` = `01` 或 `02`） |
| `authorized` | bool | 应用层授权通过（`A2/08` = `01`），**这是"能收 A0"的唯一判据** |
| `pair_kind` | str | `first_pair`（`A2/07`=`02`）/ `rebind`（`A2/07`=`01`）/ `""` |
| `pair_code_verified_at` | float \| None | 该码最近一次验证成功的时刻 |
| `pair_fail_reason` | str \| None | 失败原因码，见 10.1 |
| `pair_summary()` | dict | 以上字段汇总，**不含码明文**，可直接上报 |

`first_pair` 与 `rebind` 的区别有实际意义：

- `first_pair`：设备第一次见这个 MAC，必须用**窗口内**的码
- `rebind`：设备已登记该 MAC；若该码曾验证成功过，则长期有效（实测 11 分钟仍可用）

`BoschService` 同样暴露 `pair_fail_reason` 与 `pair_fail_hint()`，并在登录失败时自动写入日志。
