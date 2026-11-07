# ESP32 网关 v2.0 —— 微雪 ESP32-S3-DEV-KIT-N8R8

> 2026-11-07 到板开工。硬件已识别：COM3 = CH343（烧录 + 日志），COM4 = 原生 USB。
> 官方手册/原理图/数据手册已下载到 [`manual/`](manual/)。

## 一、这次开工修掉的两个坑

| # | 问题 | 后果 | v2 修正 |
|---|---|---|---|
| 1 | **v1 完全没有处理 BLE SMP PIN** | `ble_pair_auto.py` 实测：面板码同时是 **BLE passkey** 和**应用层密码**，配对窗口打开时设备请求 `PROVIDE_PIN`。v1 却设了 `NoInputNoOutput` + Just Works，SMP 会失败，后面 `A2/08` 只能回 `00` | 实现 `onPassKeyRequest()` 返回 6 位面板码，IOCap 改 `KEYBOARD_ONLY`（设备无 IO 时自动回落 Just Works） |
| 2 | **配对码写死在固件里** | 面板码几分钟就过期，换一次码要重编译+重烧 2 分钟以上，实战根本来不及 | 改成**运行时串口输码**，NVS 持久化配置，烧一次可反复输、反复重试 |

另外：协议逐字节对照 `bosch_api.py` 重新核过（stuff 范围含 chk、每分片独立 XOR、FIFO/CREDITS 共用一个回调、A2 六秒×五次重发换 msgId、A0 字段用 11-01 实测偏移）。

## 二、目录

```
esp32/
├── gateway/                       ← 主工程（PlatformIO / arduino-cli 双通道）
│   ├── src/main.cpp               固件源码 v2.0
│   ├── arduino_sketch/            arduino-cli 用的 .ino（由 main.cpp 复制）
│   ├── platformio.ini             PlatformIO 配置（锁 NimBLE 1.4.3）
│   ├── gateway_cli.py             一键 编译/烧录/串口交互/日志
│   ├── esp32_panel.py             置顶控制面板（输入 6 位码）
│   ├── pair_once.py               一条命令完成配对并给出结论
│   └── log_diag.py                串口日志诊断
├── manual/                        板卡资料下载指引（PDF 不随仓库分发）
└── README.md                      本文件
```

## 三、第一次使用（一条命令）

```bash
cd esp32/gateway
python gateway_cli.py --flash
```

它会自动找 CH343 口（本机 COM3）→ 编译 → 烧录 → 打开串口交互。

### 然后按这个顺序操作

```
1. 确认电脑上的 Python 界面/桥接都关了（否则两个 BLE 客户端抢设备）
2. 热水器面板 →「清除配对」                  ← 打开配对窗口，这步不能省
3. 面板 →「连接/配对」→ 记下 6 位码
4. 在脚本窗口直接敲这 6 位码并回车          ← 毫秒级，不用重烧
5. 看 [SMP] / [AUTH] / [A0] 三段输出
```

码过期或被拒了？**重新取码再敲一次就行**，不用碰编译。

### 串口命令

| 输入 | 作用 |
|---|---|
| `311522` | 6 位面板码 → 立即连接+配对+登录 |
| `go` | 用当前码重连（不重新配对） |
| `pair` | 清本机绑定，强制重新 SMP 配对 |
| `scan` | 扫 5 秒，列出附近 BLE 设备（查地址/信号） |
| `mac AA:BB:CC:DD:EE:FF` | 改登录 MAC（设备白名单里的那台） |
| `addr AA:BB:CC:DD:EE:FF` | 改热水器 BLE 地址 |
| `status` | 打印配置与运行状态 |
| `raw on` / `raw off` | 显示/隐藏 A0 原始字节 |
| `reboot` / `help` | 重启 / 帮助 |

## 四、三条成功判据（同时满足才算通）

1. `[SMP] 认证完成：链路已加密`
2. `[AUTH] ★ A2/07 应答 = 01/02/00` 且 `A2/08 应答 = 01`
3. `[A0]` 字段合理：水流 0~11 L/min、功率 0~50%、风机 700~1500 rpm

## 五、失败对照

| 现象 | 原因 | 处理 |
|---|---|---|
| `连不上` | 面板休眠 / 手机 App 占着 / 距离远 | `scan` 看设备在不在；关掉手机蓝牙 |
| `A2/08 应答 = 00` | **配对窗口没开**或码过期 | 面板「清除配对」→ 重取码 → 再敲一次 |
| `[SMP] 设备请求 PIN` 后失败 | 码填错 / 窗口关了 | 同上；`status` 确认 PIN 值 |
| 找不到服务 | 配对没生效 | 看 `[SMP]` 那一段 |
| 有 A2 应答但无 A0 | 登录没真通过 | 确认 `A2/08 = 01` |
| A0 字段离谱 | 收帧不完整 | `raw on` 看长度是否 34 字节 |
| 串口没输出 | 看错口了 | **用 COM3（CH343），不是 COM4**；别开 USB CDC On Boot |

## 六、验证通过后再扩展（顺序建议）

```
1. WiFi 连接（首次配网免改代码）
2. MQTT 上报 + HA 自动发现
3. 巴法云上报（米家通道，沿用现有 topic 约定）
4. 温度控制下行（D3 写参数，sendFrame 可直接用）
5. OTA 空中升级（以后不用拆下来插 USB）
6. 断线重连 + 看门狗（长期运行，建议连跑 2 天）
```

## 七、技术口径

| 项 | 值 |
|---|---|
| 板卡 | 微雪 ESP32-S3-DEV-KIT-N8R8；**esptool 实测 16MB Flash**（Manufacturer 0x46 / Device 0x4018），与型号里的 "N8" 不一致，以实测为准 |
| PSRAM | 8MB **Octal**（R8）。必须 `PSRAM=opi` 才会启用；缺省 Disabled 时 `ESP.getPsramSize()` = 0 |
| 板型 / FQBN（**板载当前**） | `esp32:esp32:esp32s3:FlashSize=8M` —— 已编译、烧录、启动验证通过，与接下来实测用的一致 |
| 目标 FQBN（实测通过后） | `esp32:esp32:esp32s3:FlashSize=16M,PartitionScheme=default_8MB,PSRAM=opi` —— 对齐 16MB Flash 并解锁 8MB PSRAM |
| 分区 | 当前 `FlashSize=8M` 自动带 `default_8MB`（3MB APP × OTA + 1.5MB SPIFFS）；切 16M 时**必须显式写** `PartitionScheme=default_8MB`，否则落回 `default` 只有 1.2MB APP |
| BLE 库 | **NimBLE-Arduino 1.4.3**（2.x 回调签名不兼容，锁死） |
| 日志/烧录口 | COM3（CH343 → UART0），115200 / 上传 460800 |
| 热水器地址 | 首次 `scan` 找到后用 `addr <MAC>` 写入 NVS（示例占位 `AA:BB:CC:DD:EE:FF`） |
| 登录 MAC | 本机蓝牙地址，`mac <MAC>` 可改（示例占位 `11:22:33:44:55:66`） |

协议依据：根目录 `BLE_PROTOCOL.md`（§6 字段实测校准、§16 B0/D7）、`API.md`、
`bosch_api.py`（136 项测试）。SMP PIN 的行为另有实机抓包佐证，见 `API.md` §10。

## 八、编译环境（换电脑 / 重装时用）

本机不装 Arduino IDE，用 **arduino-cli 命令行**：一条 `python gateway_cli.py --flash`
完成编译 + 烧录 + 读日志。

| 组件 | 版本 / 要求 |
|---|---|
| arduino-cli | ≥ 1.0，放在 `PATH` 或 `esp32/toolchain/arduino-cli.exe`（本地工具链不入库） |
| arduino-esp32 core | **2.0.17**（3.x 没有内置 NimBLE，且回调签名不同） |
| NimBLE-Arduino | **1.4.3**（2.x 不兼容，锁死） |
| 板型 FQBN | `esp32:esp32:esp32s3:FlashSize=8M` |

网络正常时的标准装法：

```bash
arduino-cli core install esp32:esp32@2.0.17
arduino-cli lib install NimBLE-Arduino@1.4.3
```

编译工作区默认 `~/esp`（Windows 下即 `C://Users//<你>//esp`），可用环境变量
`BTWH_WORK` 覆盖。**不要放进工程目录**：Windows 的 CreateProcess 有路径长度限制，
工具链里有超长 C++ 头文件名，目录一深 `g++` 就报
`The filename or extension is too long`。源码正本仍在工程里，短路径只是编译副本。

### 国内网络（实测 2026-11-07）

| 通道 | 速度 |
|---|---|
| GitHub 直连 / PlatformIO CDN | 超时 / 18 KB/s |
| arduino-cli 自带下载器 | 78 KB/s |
| GitHub 加速前缀 + curl 并行 | 500~665 KB/s |

拉不动时用任意加速前缀拼出 release 直链，下完按 Arduino 规范解压到
`<工作区>/data/packages/esp32/`。两个坑：解压必须带 `\\?\` 长路径前缀，否则会中断；
判断工具链是否完整要看
`libexec/gcc/xtensa-esp32s3-elf/8.4.0/cc1plus.exe` 是否存在——残留的 `gcc.exe`
会让人误判"已解压完成"。

## 九、当前状态（2026-11-07 收尾）

| 阶段 | 状态 |
|---|---|
| 官网手册/原理图/数据手册/驱动 | ✅ `manual/` |
| 板卡识别（COM3 = CH343，COM4 = 原生 USB） | ✅ |
| arduino-cli 工具链（短路径 + 本地包 + NimBLE 1.4.3） | ✅ |
| v2 固件（运行时输码 + SMP PIN + A2/07/A2/08 + A0 解析） | ✅ 编译 536401 B / 烧录 Hash 校验 / 启动横幅全通过 |
| 硬件实测（16MB Flash、8MB Octal PSRAM 闲置） | ✅ 已查明，配置升级留到实测后 |
| **真实热水器 BLE 连通实测** | ⏳ **唯一待办，需人在热水器旁操作** |

### 现在就差这四步

```
1. 关掉电脑上的 Python 界面 / 桥接（两个 BLE 客户端会抢热水器）
2. 双击 gateway/启动控制面板.bat（置顶窗口；日志自动落盘 logs/panel_*.log）
3. 热水器面板 →「清除配对」→ 取新的 6 位码
4. 窗口里输入 6 位码回车 → 看 [SMP] / [AUTH] / [A0] 三段输出
```

> ⚠️ 实测阶段**不要跑 `gateway_cli.py --flash`**：开过一次 16M+PSRAM 的全量重编译，
> Windows 上 11 分钟仍停在库探测，而且会烧进一个尚未验证启动的固件。
> 只连串口用 `python gateway_cli.py`（不带参数）或直接双击控制面板。
> 窗口关了日志也不丢，把 `logs/panel_*.log` 发回来即可分析。

### 实测通过后的下一刀

把 `gateway_cli.py` 的 FQBN 换成第七节的目标值 → `--flash` → 确认启动日志变成
`Flash=16MB  PSRAM=8388608 bytes` → 再上 WiFi / MQTT / HA 自动发现。
已验证固件的三个 bin 建议备份在编译工作区（默认 `~/esp/backup_v2/`），出问题可用 esptool 直接烧回。
