# Waveshare ESP32-S3-DEV-KIT-N8R8 — vendor documentation

Retrieved **2026-11-07** from the vendor's documentation site and wiki. The board was identified
from the USB devices it enumerates: `VID_303A:PID_1001` (ESP32-S3 native USB) plus
`VID_1A86:PID_55D3` (CH343), which matches the vendor's description of "CH343 + CH334, one
USB-C port exposing two COM ports".

---

## 1. Files to download yourself (not shipped in this repository)

| File | Size | Official URL |
|---|---|---|
| `ESP32-S3-DEV-KIT-N8R8-schematic.pdf` | 429 KB | https://www.waveshare.net/w/upload/e/e1/ESP32-S3-DEV-KIT-N8R8-schematic.pdf |
| `ESP32-S3-WROOM-1_datasheet_cn.pdf` | 1.3 MB | https://www.waveshare.net/w/upload/c/cd/Esp32-s3-wroom-1_wroom-1u_datasheet_cn.pdf |
| `ESP32-S3_technical_reference_manual_cn.pdf` | 15 MB | https://www.waveshare.net/w/upload/8/88/Esp32-s3_technical_reference_manual_cn.pdf |
| `CH343SER_driver.7z` | 559 KB | https://www.waveshare.net/w/upload/f/f1/CH343SER.7z |
| `MicroPython_examples.zip` | 3.0 MB | https://www.waveshare.net/w/upload/6/6f/ESP32-S3-DEV-KIT-MPY-Example.zip |

> These are vendor copyright materials. This project **does not redistribute** them; download
> them from the URLs above. The driver is normally installed automatically (the CH343 appears
> correctly in Device Manager); the archive is only a spare.

## 2. Official pages (always current)

| Page | URL |
|---|---|
| Product home (Chinese) | https://docs.waveshare.net/ESP32-S3-DEV-KIT-N8R8/ |
| Resources and documents | https://docs.waveshare.net/ESP32-S3-DEV-KIT-N8R8/Resources-And-Documents |
| Arduino environment | https://docs.waveshare.net/ESP32-S3-DEV-KIT-N8R8/Arduino |
| ESP-IDF | https://docs.waveshare.net/ESP32-S3-DEV-KIT-N8R8/ESP-IDF |
| MicroPython | https://docs.waveshare.net/ESP32-S3-DEV-KIT-N8R8/MicroPython |
| FAQ | https://docs.waveshare.net/ESP32-S3-DEV-KIT-N8R8/FAQ |
| Arduino IDE setup | https://docs.waveshare.net/ESP32-Arduino-Tutorials/Arduino-IDE-Setup |
| Legacy wiki (English, full walkthrough) | https://www.waveshare.com/wiki/ESP32-S3-DEV-KIT-N8R8 |

## 3. Key hardware parameters (N8R8)

| Item | Value |
|---|---|
| Module | ESP32-S3-WROOM-1-N8R8 |
| CPU | Xtensa LX7 dual-core, up to 240 MHz |
| SRAM / ROM | 512 KB / 384 KB |
| **Flash** | **8 MB** (Quad SPI, DIO/QIO) |
| **PSRAM** | **8 MB** |
| Wireless | 2.4 GHz Wi-Fi + Bluetooth 5 (LE) |
| USB | one USB-C port, on-board **CH343** (USB-UART) + **CH334** (USB hub) |
| Pin-compatible with | ESP32-S3-DevKitC-1 |
| SKU | 24363 (N8R8) / 24363-M (with headers) |
| Native USB D+/D- | GPIO20 / GPIO19 |
| UART0 TX/RX | GPIO43 / GPIO44 (through the CH343) |
| Note | GPIO28/29/30 (GPIO35/36/37) are not connected on this module; IO47/IO48 output 3.3 V |

Sibling variants: N16R8 = 16 MB flash; N32R16V = 32 MB Octal flash + 16 MB PSRAM (its IO47/48 run
at 1.8 V and are not interchangeable); N16R8-U is the IPEX external-antenna version.

**Measured discrepancy**: `esptool flash_id` on the unit used here reports **16 MB** flash
(manufacturer 0x46, device 0x4018), despite the "N8" in the part number. Trust the measurement,
not the label.

## 4. The two COM ports (the trap this project hit)

A single USB-C port enumerates **two independent USB devices and two COM ports**:

| | ESP32-S3 native USB | CH343 USB-UART |
|---|---|---|
| Connection | built-in USB controller, GPIO20/19 | CH343 to UART0, GPIO43/44 |
| Purpose | flashing, USB CDC/HID/MSC | **flashing + Serial0 debug output** |
| Device Manager name | contains `Espressif` / `USB JTAG/serial debug unit` | contains `USB-Enhanced-SERIAL CH343` |
| On RESET | port briefly disappears, then returns | port stays |
| **On the test machine** | **COM4** | **COM3** |

This project standardises on **COM3**. **Do not enable `USB CDC On Boot`** — `Serial` moves to
COM4 and the logs appear to vanish.

## 5. Vendor-recommended Arduino settings

- Board: `ESP32S3 Dev Module`
- Flash: 8 MB; PSRAM: OPI / 8 MB
- Board support package: arduino-esp32, board manager URL
  `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`
- On-board auto-download circuit: **no need to hold BOOT**

This project drives the build from the command line rather than the Arduino IDE, so compilation,
flashing and log capture can be scripted. See [`../README.md`](../README.md) for the exact
arduino-cli commands and pinned versions.
