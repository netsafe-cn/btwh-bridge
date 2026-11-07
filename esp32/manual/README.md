# 微雪 ESP32-S3-DEV-KIT-N8R8 官方资料（本地留存）

下载时间：**2026-11-07**，来源均为微雪官方文档站 / wiki 原始链接。
板卡型号依据：电脑上枚举出的 `VID_303A:PID_1001`（ESP32-S3 原生 USB）+
`VID_1A86:PID_55D3`（CH343），与官方"CH343 + CH334 双芯片、一个 Type-C 出两个 COM 口"
的描述完全吻合。

---

## 一、需要自行下载的官方文件（不随仓库分发）

| 文件 | 大小 | 官方原始 URL |
|---|---|---|
| `ESP32-S3-DEV-KIT-N8R8-schematic.pdf` | 429 KB | https://www.waveshare.net/w/upload/e/e1/ESP32-S3-DEV-KIT-N8R8-schematic.pdf |
| `ESP32-S3-WROOM-1_datasheet_cn.pdf` | 1.3 MB | https://www.waveshare.net/w/upload/c/cd/Esp32-s3-wroom-1_wroom-1u_datasheet_cn.pdf |
| `ESP32-S3_技术参考手册_cn.pdf` | 15 MB | https://www.waveshare.net/w/upload/8/88/Esp32-s3_technical_reference_manual_cn.pdf |
| `CH343SER_driver.7z` | 559 KB | https://www.waveshare.net/w/upload/f/f1/CH343SER.7z |
| `MicroPython_示例程序.zip` | 3.0 MB | https://www.waveshare.net/w/upload/6/6f/ESP32-S3-DEV-KIT-MPY-Example.zip |

本机驱动已自动装好（设备管理器里 CH343 显示正常），`CH343SER_driver.7z` 只是留档备用。

> 上表 PDF / 驱动 / 示例程序均为厂商版权资料，本项目**不重新分发**，按 URL 自行下载。

## 二、官网页面（在线，最新）

| 页面 | URL |
|---|---|
| 产品主页（中文，当前最新文档平台） | https://docs.waveshare.net/ESP32-S3-DEV-KIT-N8R8/ |
| 相关资料 / 手册下载 | https://docs.waveshare.net/ESP32-S3-DEV-KIT-N8R8/Resources-And-Documents |
| Arduino 开发环境 | https://docs.waveshare.net/ESP32-S3-DEV-KIT-N8R8/Arduino |
| ESP-IDF 开发 | https://docs.waveshare.net/ESP32-S3-DEV-KIT-N8R8/ESP-IDF |
| MicroPython 开发 | https://docs.waveshare.net/ESP32-S3-DEV-KIT-N8R8/MicroPython |
| 产品 FAQ | https://docs.waveshare.net/ESP32-S3-DEV-KIT-N8R8/FAQ |
| Arduino IDE 安装教程 | https://docs.waveshare.net/ESP32-Arduino-Tutorials/Arduino-IDE-Setup |
| 旧版 wiki（英文，含完整示例步骤） | https://www.waveshare.com/wiki/ESP32-S3-DEV-KIT-N8R8 |

## 三、硬件关键参数（本型号 N8R8）

| 项目 | 值 |
|---|---|
| 模组 | ESP32-S3-WROOM-1-N8R8 |
| CPU | Xtensa 32 位 LX7 双核，最高 240 MHz |
| SRAM / ROM | 512 KB / 384 KB |
| **Flash** | **8 MB**（Quad SPI，DIO/QIO） |
| **PSRAM** | **8 MB** |
| 无线 | 2.4 GHz Wi-Fi + Bluetooth 5 (LE) |
| USB | 1 × Type-C，板载 **CH343**（USB 转 UART）+ **CH334**（USB HUB） |
| 引脚兼容 | ESP32-S3-DevKitC-1 |
| SKU | 24363（N8R8）/ 24363-M（带排针） |
| 原生 USB D+/D- | GPIO20 / GPIO19 |
| UART0 TX/RX | GPIO43 / GPIO44（经 CH343） |
| 注意 | GPIO28/29/30（GPIO35/36/37）在本模块为 NC；IO47/IO48 输出 3.3 V |

同系列差异：N16R8 = 16MB Flash；N32R16V = 32MB Octal Flash + 16MB PSRAM（其
IO47/48 为 1.8 V 域，不通用）；N16R8-U 为 IPEX 外置天线版本。

## 四、两个 COM 口（本项目的坑，官方特别提示）

同一个 Type-C 会枚举出**两个独立 USB 设备 / 两个 COM 口**：

| | ESP32-S3 原生 USB | CH343 USB 转 UART |
|---|---|---|
| 连接 | 内置 USB 控制器 GPIO20/19 | CH343 转 UART0 GPIO43/44 |
| 用途 | 烧录、USB CDC/HID/MSC | **烧录 + 查看 Serial0 调试日志** |
| 设备管理器名称 | 含 `Espressif` / `USB JTAG/serial debug unit` | 含 `USB-Enhanced-SERIAL CH343` |
| 按 RESET 时 | 端口短暂消失后重新出现 | 端口保持不变 |
| **本机实测** | **COM4** | **COM3** |

本项目统一用 **COM3**：`platformio.ini` 里 `upload_port` / `monitor_port` 都是 COM3。
**不要开 `USB CDC On Boot`**，一开 `Serial` 就跑到 COM4 去了，日志会"看不见"。

## 五、官方 Arduino 配置口径

- 板型：`ESP32S3 Dev Module`
- Flash：8MB；PSRAM：OPI / 8MB
- 板级支持包：arduino-esp32（官方板管理器地址
  `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`）
- 板载自动下载电路，**无需手动按 BOOT 键**

本项目用 PlatformIO 命令行替代 Arduino IDE（便于自动编译/烧录/读日志），
板型选 `esp32-s3-devkitc-1`，其余与上表一致。
