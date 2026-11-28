# 迁移到 Arduino-ESP32 3.3.x / NimBLE 2.x

状态：**分支 `feature/core3`，源码已改，未编译验证**（工具链下载中）。

## 为什么

| IDF 版本 | EOL | 对应 Arduino core |
| --- | --- | --- |
| 4.4 | **2024-07** | 2.0.x ← 迁移前 |
| 5.0 | 2025-05 | — |
| 5.1 | **2025-12** | 3.0.x |
| 5.2 | 2026-08 | — |
| 5.5 | 受支持 | **3.3.x ← 目标** |

依据：Espressif 公告 AR2024-008（v4.4）、AR2025-007（v5.1）、AR2026-008（v5.2）。
EOL 之后不再有安全修复，而这是一台连 WiFi 的设备。

**不要只升到 3.0/3.1/3.2**：它们对应的 IDF 5.1/5.2 同样已 EOL。唯一正确的目标是 3.3.x。

## 双工具链

两个 core 大版本不能共用一个 data 目录，所以各有一套：

```
2.0.17   toolchain/arduino-cli.yaml       C:\Users\LZY\esp   （默认，保持可回退）
3.3.12   toolchain/arduino-cli-3x.yaml    C:\Users\LZY\esp3
```

`gateway_cli.py` 通过环境变量选择：

```bash
# 2.0.17（默认，无需设置）
python firmware/bridge/tools/gateway_cli.py --build

# 3.3.12
set BTWH_CONFIG=firmware/bridge/toolchain/arduino-cli-3x.yaml
set BTWH_WORK=C:\Users\LZY\esp3
python firmware/bridge/tools/gateway_cli.py --build
```

`BTWH_CONFIG` 是本次迁移新增的；`BTWH_WORK` 原本就有，控制 build 与 libraries 的位置。

## 代码改动：6 处，全在 `ble/link.cpp`

分层在这里帮了忙：NimBLE 只在这一个文件里出现，其余 10 个文件提到它都只是注释。

| # | 1.4.3 | 2.x | 依据 |
| --- | --- | --- | --- |
| 1 | `class Security : public NimBLESecurityCallbacks` | `NimBLEClientCallbacks` | 2.x 移除了 `NimBLESecurityCallbacks`，安全回调并入 client callbacks |
| 2 | `uint32_t onPassKeyRequest()` 返回 PIN | `void onPassKeyEntry(NimBLEConnInfo&)` + `NimBLEDevice::injectPassKey()` | `NimBLEClient.h` 的 `virtual void onPassKeyEntry(NimBLEConnInfo& connInfo);` |
| 3 | `bool onConfirmPIN(uint32_t)` | `void onConfirmPasskey(NimBLEConnInfo&, uint32_t)` + `injectConfirmPasskey()` | 同上，`virtual void onConfirmPasskey(NimBLEConnInfo&, uint32_t pin);` |
| 4 | `onAuthenticationComplete(ble_gap_conn_desc*)` | `onAuthenticationComplete(NimBLEConnInfo&)`，用 `.isEncrypted()` / `.isBonded()` | `NimBLEConnInfo.h` 有 `bool isEncrypted() const; bool isBonded() const;` |
| 5 | `onNotify(chr*, uint8_t*, size_t, bool)` | `onNotify(chr*, NimBLEAttValue)` | `NimBLERemoteCharacteristic.h`：`typedef std::function<...> notify_callback;` |
| 6 | `scan->start(seconds, false)` + `getDevice(i)` 返回对象 | `scan->getResults(ms, false)` + `getDevice(i)` 返回指针 | 迁移指南；`start()` 的时间单位变成毫秒 |

配套两处：
- `begin()` 删掉 `NimBLEDevice::setSecurityCallbacks()`（2.x 没有设备级安全回调）
- `connect()` 加 `gClient->setCallbacks(gSecurity)`，其中 `gSecurity` 是**静态实例**

## 三个陷阱：能编译通过，但会静默失败

**① `setClientCallbacks` 不存在。**
2.x 的实际签名是 `void setCallbacks(NimBLEClientCallbacks& callbacks)` —— **传引用，且 client 不拥有它**。
如果照着旧习惯 `new Security()` 传指针，编译会报错（好）；但如果误以为 client 会释放而用局部对象，**回调会在 client 存活期间悬空**。

**② `secureConnection()` 仍然存在，不要换成 `startSecurity()`。**
`startSecurity(connHandle)` 是 **server 侧**的强制配对入口。在 client 上用它，代码能编译，但**配对永远不会发生**——症状是 `A2/08` 回答 `00`，看起来像配对码错了。

**③ `onNotify` 的 `len` 参数没了。**
长度要从 `NimBLEAttValue::size()` 取。如果误用 `sizeof(value)`，会拿到对象大小而不是数据长度，帧重组会立刻错乱。

## 验证清单（迁移后逐项过）

```
1. 首次配对：面板出码 → 输入 → encrypted=yes
2. A2/07 提交 MAC → 回复 01
3. A2/08 提交码 → 回复 01
4. A0 帧持续到达（status 里 A0 frames 递增）
5. 断开重连，不重新配对
6. 断电重启后 bond 仍在（不需要重新输码）
7. 面板清配对后能重新出码并完成配对
```

第 1、6 项最关键：配对链路是这套固件最难的部分，2.x 改了回调模型，等于要重新证明一遍。

**观测手段**（迁移前专门加的诊断能力，就是为这一步准备的）：

```
guard <℃>     问守卫一个值，确认逻辑没被改坏
inject <hex>  造帧喂解析器，确认 A0 解析不变
log           串口拉最近 60 行日志（含 SMP 每一步）
telnet :23    网络允许时可远程看同样的日志
```

## 回退

```bash
git checkout main                     # 回到 2.0.17 的代码
# 不设 BTWH_CONFIG / BTWH_WORK，默认就是用 ~/esp 里的 2.0.17
python firmware/bridge/tools/gateway_cli.py --build --flash
```

`C:\Users\LZY\esp` 里的 2.0.17 环境和 build 目录全程未被触碰，回退是**编译即可用**，不需要重新下载任何东西。

## 不在这轮范围内的

- OTA 镜像校验（SHA-256 / 签名）—— 见评审第 1 条，改契约，单独一轮
- OTA 期间喂看门狗 —— 同上
- 型号自动识别 —— 需要第二个型号的帧样本
