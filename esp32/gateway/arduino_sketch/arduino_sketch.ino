/*
 * ============================================================================
 * 博世热水器 BLE 网关  v2.0  ——  微雪 ESP32-S3-DEV-KIT-N8R8
 * ----------------------------------------------------------------------------
 * 阶段一目标（可行性验证，全部通过再上 WiFi/MQTT）：
 *   ① 连上热水器 + 完成 SMP 配对（面板码就是 BLE PIN）
 *   ② A2/07 提交 MAC + A2/08 提交配对码，拿到授权
 *   ③ 收到 A0 广播并正确解析字段
 *
 * v2.0 相比 v1.0 的两个关键修正：
 *   1) 【致命】v1 用 Just Works + NoInputNoOutput，完全没有处理 SMP PIN。
 *      实测面板码同时充当 "BLE passkey" 和 "应用层密码"，
 *      配对窗口打开时设备请求 PROVIDE_PIN。v2 实现 onPassKeyRequest() 返回面板码。
 *   2) 【效率】配对码不再写死在固件里，改为运行时从串口输入。
 *      面板码只有几分钟寿命，v1 每换一次码都要重新编译+烧录（2 分钟以上），
 *      v2 烧录一次即可反复输码、反复重试。
 *
 * 硬件：微雪 ESP32-S3-DEV-KIT-N8R8（8MB Flash / 8MB PSRAM）
 * 日志：COM3（CH343 → UART0），115200。烧录也走 COM3。
 * 依赖：NimBLE-Arduino 1.4.3（platformio.ini 已锁定，勿升 2.x）
 * 协议依据：BLE_PROTOCOL.md / bosch_api.py（逐字节对齐，136 项测试全绿）
 * ============================================================================
 */

#include <Arduino.h>
#include <NimBLEDevice.h>
#include <Preferences.h>
#include <ctype.h>
#include <string.h>

// ---------------------------------------------------------------- 默认配置
// 占位值。首次使用：scan 找到热水器 → `addr <热水器MAC>` → `mac <本机蓝牙MAC>`
static const char* DEF_DEVICE_MAC = "AA:BB:CC:DD:EE:FF";   // 热水器 BLE 地址（占位）
static const char* DEF_LOGIN_MAC  = "11:22:33:44:55:66";   // 已授权主机蓝牙 MAC（占位）
static const char* DEF_PIN        = "";                     // 面板码：必须运行时输入

static const char* SVC_UUID  = "ff4396a6-50a8-4fc0-bcf6-228e82dfedb0";
static const char* FIFO_UUID = "ff4396a6-50a8-4fc0-bcf6-228e82dfedb2";
static const char* CRED_UUID = "ff4396a6-50a8-4fc0-bcf6-228e82dfedb3";

static const uint8_t XOR_KEY[9] = {'1','a','g','3','.','-','f','s','7'};
static const uint8_t SENDER     = 0x21;
static const uint8_t RECV_LOGIN = 0xAB;
static const uint8_t CMD_LOGIN  = 0xA2;
static const uint8_t CMD_STATE  = 0xA0;
static const size_t  CHUNK      = 20;

// ---------------------------------------------------------------- 全局状态
static Preferences gPrefs;

static String  gDevMac, gLoginMac, gPin;
static uint32_t gPinNum = 0;
static bool     gNeedRebond = false;

static NimBLEClient*               gClient = nullptr;
static NimBLERemoteCharacteristic* gFifo   = nullptr;

static uint8_t gRxBuf[1024];
static size_t  gRxLen = 0;
static uint8_t gMid   = 0;

static volatile int      gAuthResp   = -1;   // A2/07 的 data[1]
static volatile int      gPassResp   = -1;   // A2/08 的 data[1]
static volatile uint32_t gA0Count    = 0;
static volatile uint32_t gLastA0Ms   = 0;
static volatile bool     gEncrypted  = false;

static bool     gRawDump   = false;
static bool     gInSession = false;
static uint32_t gSessionStart = 0;
static String   gSerialLine;

enum Phase { PH_WAIT_CODE, PH_RUNNING, PH_FAILED };
static Phase gPhase = PH_WAIT_CODE;

// ---------------------------------------------------------------- 小工具
static void banner(const char* s) { Serial.printf("\n===== %s =====\n", s); }

static bool parseMac(const String& s, uint8_t out[6]) {
  int v[6] = {0}, n = 0;
  const char* p = s.c_str();
  while (*p && n < 6) {
    if (*p == ':' || *p == '-') { p++; continue; }
    if (!isxdigit((unsigned char)p[0]) || !isxdigit((unsigned char)p[1])) return false;
    char t[3] = {p[0], p[1], 0};
    v[n++] = (int)strtol(t, nullptr, 16);
    p += 2;
  }
  if (n != 6) return false;
  for (int i = 0; i < 6; i++) out[i] = (uint8_t)v[i];
  return true;
}

static String macToStr(const uint8_t m[6]) {
  char b[18];
  snprintf(b, sizeof(b), "%02X:%02X:%02X:%02X:%02X:%02X",
           m[0], m[1], m[2], m[3], m[4], m[5]);
  return String(b);
}

// ---------------------------------------------------------------- 协议编解码
static void xorBuf(uint8_t* p, size_t n) {
  for (size_t i = 0; i < n; i++) p[i] ^= XOR_KEY[i % 9];
}

// 逻辑帧：7B7B | sender recv mid cmd data... | chk | 7D7D
static size_t buildFrame(uint8_t* out, uint8_t recv, uint8_t mid,
                         uint8_t cmd, const uint8_t* data, size_t dlen) {
  size_t i = 0;
  out[i++] = 0x7B; out[i++] = 0x7B;
  out[i++] = SENDER; out[i++] = recv; out[i++] = mid; out[i++] = cmd;
  for (size_t k = 0; k < dlen; k++) out[i++] = data[k];
  uint8_t sum = 0;
  for (size_t k = 2; k < i; k++) sum += out[k];
  out[i++] = sum;
  out[i++] = 0x7D; out[i++] = 0x7D;
  return i;
}

// 插填充：frame[2 .. len-3] 内出现 7B/7D 时后补 00（含 chk 字节，对齐 Python stuff()）
static size_t stuffFrame(const uint8_t* in, size_t n, uint8_t* out) {
  if (n <= 4) { memcpy(out, in, n); return n; }
  size_t o = 0;
  out[o++] = in[0]; out[o++] = in[1];
  for (size_t i = 2; i < n - 2; i++) {
    out[o++] = in[i];
    if (in[i] == 0x7B || in[i] == 0x7D) out[o++] = 0x00;
  }
  out[o++] = in[n-2]; out[o++] = in[n-1];
  return o;
}

static size_t unstuffFrame(const uint8_t* in, size_t n, uint8_t* out) {
  if (n <= 2) { memcpy(out, in, n); return n; }
  size_t o = 0;
  out[o++] = in[0]; out[o++] = in[1];
  bool skip = false;
  for (size_t i = 2; i < n - 2; i++) {
    if (skip && in[i] == 0x00) { skip = false; continue; }
    out[o++] = in[i];
    skip = (in[i] == 0x7B || in[i] == 0x7D);
  }
  out[o++] = in[n-2]; out[o++] = in[n-1];
  return o;
}

// ---------------------------------------------------------------- A0 状态解析
// 字段偏移依据 2026-11-01 148 帧实测校准（BLE_PROTOCOL.md §6）
static void dumpA0(const uint8_t* d, size_t n) {
  auto b = [&](int i) -> int { return (i < (int)n) ? d[i] : 0; };
  int  ftype = b(0);
  bool t1    = (ftype == 1);
  auto temp  = [&](int i) -> float { return b(i) + b(i+1) / 100.0f; };

  float tIn   = temp(1);
  float tChm  = temp(3);
  float tBox  = temp(6);
  int   setp  = b(5);
  int   fault = b(11);
  bool  ion   = (b(10) & 0x10) != 0;

  // 实测偏移仅对 Type1 验证过；Type0 沿用反编译表
  int   outI  = t1 ? 28 : 21;
  float tOut  = (float)b(outI) + b(outI+1) / 100.0f;
  float flow  = t1 ? b(21) / 10.0f : b(8) / 10.0f;
  int   power = t1 ? b(13) : b(9);
  int   fan   = t1 ? (b(15) * 100 + b(16)) : b(14);

  Serial.printf("[A0] T%d 出水%.1f 进水%.1f 炉腔%.1f 机身%.1f 设定%d "
                "功率%d%% 水流%.1fL/min 风机%drpm 电离%s 故障%s\n",
                ftype, tOut, tIn, tChm, tBox, setp, power, flow, fan,
                ion ? "有" : "无", fault ? String(fault, HEX).c_str() : "无");

  if (t1) {  // 反编译推测字段，标注以便后续核对
    Serial.printf("     [推测] 环境%.1f 环境设定%.1f 燃气设定%.1f "
                  "压力%dPa 模式[%d]=%02X 浴室[%d]=%02X\n",
                  temp(16), temp(18), temp(22), b(27) * 10, 32, b(32), 33, b(33));
  }
  if (gRawDump) {
    Serial.print("     raw=");
    for (size_t i = 0; i < n; i++) Serial.printf("%02x ", d[i]);
    Serial.println();
  }
}

// ---------------------------------------------------------------- 收帧分发
static void onFrame(const uint8_t* f, size_t n) {
  if (n < 9) return;
  uint8_t sum = 0;
  for (size_t i = 2; i < n - 3; i++) sum += f[i];
  if (sum != f[n-3]) {
    Serial.printf("[RX] 丢弃坏帧 chk=%02X expect=%02X len=%d\n", f[n-3], sum, (int)n);
    return;
  }
  uint8_t mid = f[4], cmd = f[5];
  const uint8_t* p = f + 6;
  size_t plen = n - 9;

  if (cmd == CMD_STATE) {
    gA0Count++;
    gLastA0Ms = millis();
    dumpA0(p, plen);
  } else if (cmd == CMD_LOGIN && plen >= 2) {
    if (p[0] == 0x07) {
      gAuthResp = p[1];
      Serial.printf("[AUTH] ★ A2/07 应答 = %02X  (%s)\n", p[1],
        p[1] == 0x01 ? "免密授权" : p[1] == 0x02 ? "要求配对码" :
        p[1] == 0x00 ? "MAC 未授权(走配对码)" :
        p[1] == 0x3C ? "设备冷却中(30s)" :
        p[1] == 0x5A ? "设备要求等 90s" : "其它");
    } else if (p[0] == 0x08) {
      gPassResp = p[1];
      Serial.printf("[AUTH] ★ A2/08 应答 = %02X  (%s)\n", p[1],
        p[1] == 0x01 ? "授权成功" : p[1] == 0x02 ? "配对码错误" :
        p[1] == 0x00 ? "被拒绝(码过期/窗口未开)" : "其它");
    } else {
      Serial.printf("[AUTH] A2 未知子命令 %02X: ", p[0]);
      for (size_t i = 0; i < plen; i++) Serial.printf("%02x ", p[i]);
      Serial.println();
    }
  } else {
    Serial.printf("[RX ] cmd=%02X mid=%d len=%d : ", cmd, mid, (int)plen);
    for (size_t i = 0; i < plen; i++) Serial.printf("%02x ", p[i]);
    Serial.println();
  }
}

static void drainRx() {
  while (gRxLen >= 4) {
    size_t start = 0;
    while (start + 1 < gRxLen &&
           !(gRxBuf[start] == 0x7B && gRxBuf[start+1] == 0x7B)) start++;
    if (start + 1 >= gRxLen) { gRxLen = 0; return; }
    if (start > 0) {
      memmove(gRxBuf, gRxBuf + start, gRxLen - start);
      gRxLen -= start;
    }
    size_t end = 0;
    for (size_t i = 2; i + 1 < gRxLen; i++) {
      if (gRxBuf[i] == 0x7D && gRxBuf[i+1] == 0x7D) { end = i + 2; break; }
    }
    if (!end) return;

    uint8_t plain[256];
    size_t plen = unstuffFrame(gRxBuf, end, plain);
    onFrame(plain, plen);
    memmove(gRxBuf, gRxBuf + end, gRxLen - end);
    gRxLen -= end;
  }
}

// FIFO 与 CREDITS 两个 CCCD 共用此回调：与 Python _on_notify 完全一致
static void onNotify(NimBLERemoteCharacteristic* ch, uint8_t* data,
                     size_t len, bool isNotify) {
  (void)ch; (void)isNotify;
  if (len == 0 || len > 64) return;
  uint8_t tmp[64];
  memcpy(tmp, data, len);
  xorBuf(tmp, len);                       // 每个分片独立从 KEY[0] 开始
  if (gRxLen + len > sizeof(gRxBuf)) {
    Serial.println("[RX ] 缓冲溢出，清空重同步");
    gRxLen = 0;
  }
  memcpy(gRxBuf + gRxLen, tmp, len);
  gRxLen += len;
  drainRx();
}

// ---------------------------------------------------------------- 发送
static bool sendFrame(uint8_t recv, uint8_t cmd, const uint8_t* data, size_t dlen) {
  if (!gFifo || !gClient || !gClient->isConnected()) {
    Serial.println("[TX ] FIFO 未就绪");
    return false;
  }
  uint8_t frame[160];
  size_t flen = buildFrame(frame, recv, gMid++, cmd, data, dlen);
  uint8_t wire[256];
  size_t wlen = stuffFrame(frame, flen, wire);

  Serial.printf("[TX ] mid=%d cmd=%02X : ", (uint8_t)(gMid - 1), cmd);
  for (size_t i = 0; i < flen; i++) Serial.printf("%02x ", frame[i]);
  Serial.println();

  for (size_t off = 0; off < wlen; off += CHUNK) {
    size_t n = (wlen - off < CHUNK) ? (wlen - off) : CHUNK;
    uint8_t chunk[CHUNK];
    memcpy(chunk, wire + off, n);
    xorBuf(chunk, n);
    if (!gFifo->writeValue(chunk, n, true)) {
      Serial.println("[TX ] 写失败");
      return false;
    }
    delay(20);
  }
  return true;
}

static bool waitResp(volatile int& which, uint32_t timeoutMs) {
  uint32_t t0 = millis();
  while (millis() - t0 < timeoutMs) {
    if (which >= 0) return true;
    delay(50);
  }
  return false;
}

// 对齐 bosch_api.py::_send_login_wait：6s 超时 × 5 次重发，每次换 msgId
static bool loginStep(const uint8_t* data, size_t len, volatile int& resp,
                      const char* tag) {
  for (int attempt = 0; attempt < 5; attempt++) {
    resp = -1;
    if (!sendFrame(RECV_LOGIN, CMD_LOGIN, data, len)) return false;
    if (waitResp(resp, 6000)) {
      Serial.printf("[AUTH] %s 应答 = %d\n", tag, (int)resp);
      return true;
    }
    Serial.printf("[AUTH] %s 无应答，重发 %d/5（新 msgId）\n", tag, attempt + 1);
  }
  return false;
}

// ---------------------------------------------------------------- SMP 安全回调
// 【关键】面板码同时是 BLE passkey：配对窗口打开时设备请求 PROVIDE_PIN，
// 必须由这里返回 6 位数字，否则 SMP 配对直接失败，后续 A2/08 永远回 00。
class BoschSecurity : public NimBLESecurityCallbacks {
  uint32_t onPassKeyRequest() override {
    Serial.printf("[SMP] 设备请求 PIN → 返回 %06u\n", (unsigned)gPinNum);
    return gPinNum;
  }
  void onPassKeyNotify(uint32_t pass_key) override {
    Serial.printf("[SMP] 设备显示 PIN：%06u（请确认与面板一致）\n", (unsigned)pass_key);
  }
  bool onSecurityRequest() override {
    Serial.println("[SMP] 设备请求加密 → 接受");
    return true;
  }
  bool onConfirmPIN(uint32_t pin) override {
    Serial.printf("[SMP] 数值比较 PIN=%06u → 接受\n", (unsigned)pin);
    return true;
  }
  void onAuthenticationComplete(ble_gap_conn_desc* desc) override {
    bool ok = desc && desc->sec_state.encrypted;
    gEncrypted = ok;
    Serial.printf("[SMP] 认证完成：%s%s\n",
                  ok ? "链路已加密" : "未加密/失败",
                  (desc && desc->sec_state.bonded) ? "，已绑定" : "");
  }
};

// ---------------------------------------------------------------- 会话
static void disconnectClient() {
  if (gClient) {
    if (gClient->isConnected()) gClient->disconnect();
    NimBLEDevice::deleteClient(gClient);
    gClient = nullptr;
  }
  gFifo = nullptr;
  gRxLen = 0;
}

static bool connectAndPair() {
  uint8_t dummy[6];
  if (!parseMac(gDevMac, dummy)) {
    Serial.println("[CONN] 热水器地址格式错误，用 addr AA:BB:CC:DD:EE:FF 修正");
    return false;
  }

  disconnectClient();
  NimBLEAddress addr((std::string)gDevMac.c_str(), BLE_ADDR_PUBLIC);
  gClient = NimBLEDevice::createClient();
  gClient->setConnectTimeout(15);

  Serial.printf("[CONN] 连接 %s ...\n", gDevMac.c_str());
  if (!gClient->connect(addr)) {
    Serial.println("[CONN] ✗ 连不上（检查距离 / 面板是否开着 / 手机 App 是否占用）");
    return false;
  }
  Serial.println("[CONN] ✓ 链路已建立 [1/5]");

  // 设备可能主动要求加密；显式触发一次确保 SMP 走完
  Serial.printf("[SMP ] 发起配对（IOCap=KeyboardOnly，PIN=%06u）... [2/5]\n",
                (unsigned)gPinNum);
  bool sec = gClient->secureConnection();
  Serial.printf("[SMP ] secureConnection() = %s，encrypted=%s\n",
                sec ? "true" : "false", gEncrypted ? "true" : "false");

  NimBLERemoteService* svc = gClient->getService(SVC_UUID);
  if (!svc) {
    Serial.println("[CONN] ✗ 找不到服务（配对未生效时常见）");
    return false;
  }
  gFifo = svc->getCharacteristic(FIFO_UUID);
  NimBLERemoteCharacteristic* cred = svc->getCharacteristic(CRED_UUID);
  if (!gFifo || !cred) {
    Serial.println("[CONN] ✗ 找不到 FIFO/CREDITS 特征");
    return false;
  }
  // 顺序对齐 App：FIFO → 200ms → CREDITS，之后 sleep(1000)
  if (!gFifo->subscribe(true, onNotify)) {
    Serial.println("[CONN] ✗ 订阅 FIFO 失败");
    return false;
  }
  delay(200);
  if (!cred->subscribe(true, onNotify)) {
    Serial.println("[CONN] ✗ 订阅 CREDITS 失败");
    return false;
  }
  Serial.println("[CONN] ✓ 已订阅 FIFO + CREDITS [3/5]");
  delay(1000);
  return true;
}

static bool doLogin() {
  uint8_t mac[6];
  if (!parseMac(gLoginMac, mac)) {
    Serial.println("[AUTH] 登录 MAC 格式错误，用 mac AA:BB:CC:DD:EE:FF 修正");
    return false;
  }
  if (gPin.length() != 6) {
    Serial.println("[AUTH] 面板码为空或不是 6 位。请先输入 6 位码。");
    return false;
  }

  Serial.printf("[AUTH] 提交 MAC = %s（设备白名单）\n", macToStr(mac).c_str());
  uint8_t b1[7] = {0x07, 0,0,0,0,0,0};
  memcpy(b1 + 1, mac, 6);
  if (!loginStep(b1, 7, gAuthResp, "A2/07")) return false;
  Serial.println("[AUTH] ✓ MAC 应答已收到 [4/5]");

  int kind = gAuthResp;
  if (kind == 0x3C || kind == 0x5A) {
    Serial.printf("[AUTH] 设备要求冷却（%02X），%s 后重试\n",
                  kind, kind == 0x3C ? "30 秒" : "90 秒");
    return false;
  }

  delay(1000);   // App: sendLoginPass 开头固定 sleep(1000)
  uint8_t b2[7];
  b2[0] = 0x08;
  for (int i = 0; i < 6; i++) b2[i+1] = gPin[i] - '0';   // 每位数字转 1 字节
  Serial.printf("[AUTH] 提交配对码 %s（nibble 编码）\n", gPin.c_str());
  if (!loginStep(b2, 7, gPassResp, "A2/08")) return false;

  if (gPassResp == 0x01) {
    Serial.println("[AUTH] ✓ 授权成功 [5/5]，等待 A0 广播…");
    return true;
  }
  Serial.println("[AUTH] ✗ 授权失败。多半是「配对窗口没开」或「码已过期」：");
  Serial.println("       面板执行『清除配对』→ 重新取码 → 直接输入新码（不用重烧）");
  return false;
}

static void startSession(bool forceRebond) {
  if (gInSession) { Serial.println("[BUSY] 会话进行中，输入 reboot 可中断"); return; }
  if (gPin.length() != 6) {
    Serial.println("请先输入 6 位面板码（例如：311522），我立刻开始连接。");
    return;
  }
  gInSession = true;
  gSessionStart = millis();
  gPhase = PH_RUNNING;
  gAuthResp = gPassResp = -1;
  gA0Count = 0;
  gLastA0Ms = millis();
  gEncrypted = false;

  NimBLEDevice::setSecurityPasskey(gPinNum);
  if (forceRebond || gNeedRebond) {
    Serial.println("[SMP ] 清除本机旧绑定（准备重新配对）");
    NimBLEDevice::deleteAllBonds();
    gNeedRebond = false;
    delay(200);
  }

  banner("开始会话");
  Serial.printf("[CFG ] 热水器=%s  登录MAC=%s  PIN=%s\n",
                gDevMac.c_str(), gLoginMac.c_str(), gPin.c_str());

  bool ok = connectAndPair();
  if (ok) ok = doLogin();
  if (!ok) {
    Serial.println("\n[结果] ✗ 未通过。修好之后直接再输一次码即可，无需重新烧录。");
    disconnectClient();
    gPhase = PH_FAILED;
  } else {
    Serial.println("\n[结果] ✓ 三步全通，持续监听 A0（每 4 秒一帧）");
  }
  gInSession = false;
}

// ---------------------------------------------------------------- 命令
static void doScan() {
  if (gClient && gClient->isConnected()) {
    Serial.println("[SCAN] 已连接，先断开再扫描");
    disconnectClient();
    delay(300);
  }
  banner("BLE 扫描 5 秒");
  NimBLEScan* s = NimBLEDevice::getScan();
  s->setActiveScan(true);
  s->setInterval(100);
  s->setWindow(99);
  NimBLEScanResults r = s->start(5, false);
  Serial.printf("[SCAN] 发现 %d 个设备：\n", r.getCount());
  for (int i = 0; i < r.getCount(); i++) {
    NimBLEAdvertisedDevice d = r.getDevice(i);   // 1.4.x 返回对象，不是指针
    Serial.printf("  %-20s RSSI=%4d  %s\n",
                  d.getAddress().toString().c_str(), d.getRSSI(),
                  d.haveName() ? d.getName().c_str() : "(无名)");
  }
  s->clearResults();
  banner("扫描结束");
}

static void setPin(const String& code) {
  bool changed = (code != gPin);
  gPin = code;
  gPinNum = (uint32_t)strtoul(code.c_str(), nullptr, 10);
  gPrefs.putString("pin", gPin);
  NimBLEDevice::setSecurityPasskey(gPinNum);
  if (changed) gNeedRebond = true;    // 码变了 = 面板开过新窗口，旧 bond 作废
  Serial.printf("[CFG ] 面板码 = %s（同时作为 BLE PIN 和应用层密码）%s\n",
                gPin.c_str(), changed ? " [码已变→下次自动清绑定]" : "");
  startSession(false);
}

static void printHelp() {
  banner("串口命令（直接输入后回车，不需要重新烧录）");
  Serial.println("  311522            6 位面板码 → 立即开始连接+配对+登录");
  Serial.println("  pin 311522        同上");
  Serial.println("  go                用当前码重连（不重新配对）");
  Serial.println("  pair              清本机绑定，强制重新 SMP 配对");
  Serial.println("  scan              扫描 5 秒，列出附近 BLE 设备");
  Serial.println("  mac AA:BB:...     改登录 MAC（设备白名单里的那台）");
  Serial.println("  addr AA:BB:...    改热水器 BLE 地址");
  Serial.println("  status            打印配置与运行状态");
  Serial.println("  raw on / raw off  显示/隐藏 A0 原始字节");
  Serial.println("  reboot            重启");
  Serial.println("  help              本帮助");
}

static void printStatus() {
  banner("状态");
  Serial.printf("  热水器地址 : %s\n", gDevMac.c_str());
  Serial.printf("  登录 MAC   : %s\n", gLoginMac.c_str());
  Serial.printf("  面板码     : %s (PIN=%06u)\n", gPin.c_str(), (unsigned)gPinNum);
  Serial.printf("  相位       : %s\n", gPhase == PH_WAIT_CODE ? "等待输码" :
                                     gPhase == PH_RUNNING  ? "已连接监听" : "上次失败");
  Serial.printf("  链路       : %s / 加密=%s\n",
                (gClient && gClient->isConnected()) ? "已连接" : "未连接",
                gEncrypted ? "是" : "否");
  Serial.printf("  A2/07=%d  A2/08=%d  A0帧数=%u  原始帧=%s\n",
                (int)gAuthResp, (int)gPassResp, (unsigned)gA0Count,
                gRawDump ? "开" : "关");
}

static void handleCommand(String line) {
  line.trim();
  if (!line.length()) return;
  String low = line; low.toLowerCase();

  // 纯 6 位数字 = 面板码
  if (line.length() == 6) {
    bool digits = true;
    for (int i = 0; i < 6; i++) if (!isdigit((unsigned char)line[i])) digits = false;
    if (digits) { setPin(line); return; }
  }
  if (low.startsWith("pin "))  { setPin(line.substring(4)); return; }
  if (low == "help" || low == "?")   { printHelp(); return; }
  if (low == "status")               { printStatus(); return; }
  if (low == "scan")                 { doScan(); return; }
  if (low == "go")                   { gNeedRebond = false; startSession(false); return; }
  if (low == "pair")                 { startSession(true); return; }
  if (low == "reboot")               { Serial.println("重启…"); delay(200); ESP.restart(); }
  if (low == "raw on")               { gRawDump = true;  Serial.println("[CFG] raw=on");  return; }
  if (low == "raw off")              { gRawDump = false; Serial.println("[CFG] raw=off"); return; }
  if (low.startsWith("mac ")) {
    uint8_t m[6];
    if (!parseMac(line.substring(4), m)) { Serial.println("MAC 格式不对"); return; }
    gLoginMac = macToStr(m);
    gPrefs.putString("loginmac", gLoginMac);
    Serial.printf("[CFG] 登录 MAC = %s\n", gLoginMac.c_str());
    return;
  }
  if (low.startsWith("addr ")) {
    gDevMac = line.substring(5); gDevMac.trim(); gDevMac.toLowerCase();
    gPrefs.putString("devmac", gDevMac);
    Serial.printf("[CFG] 热水器地址 = %s\n", gDevMac.c_str());
    return;
  }
  Serial.printf("[?] 不认识的命令：%s（输入 help 看列表）\n", line.c_str());
}

static void pollSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (gSerialLine.length()) { handleCommand(gSerialLine); gSerialLine = ""; }
    } else if (gSerialLine.length() < 120) {
      gSerialLine += c;
    }
  }
}

// ---------------------------------------------------------------- Arduino
void setup() {
  Serial.begin(115200);
  delay(1200);
  banner("博世热水器 ESP32 网关 v2.0  微雪 ESP32-S3-DEV-KIT-N8R8");

  gPrefs.begin("bosch", false);
  gDevMac   = gPrefs.getString("devmac",   DEF_DEVICE_MAC);
  gLoginMac = gPrefs.getString("loginmac", DEF_LOGIN_MAC);
  gPin      = gPrefs.getString("pin",      DEF_PIN);
  gPinNum   = (uint32_t)strtoul(gPin.c_str(), nullptr, 10);

  Serial.printf("[BOOT] Chip=%s  Flash=%uMB  PSRAM=%u bytes\n",
                ESP.getChipModel(), ESP.getFlashChipSize() / (1024*1024), ESP.getPsramSize());
  Serial.printf("[BOOT] 热水器=%s  登录MAC=%s  已存面板码=%s\n",
                gDevMac.c_str(), gLoginMac.c_str(), gPin.length() ? gPin.c_str() : "(无)");

  NimBLEDevice::init("BoschGW");
  // KeyboardOnly：设备 display PIN 时我们输入；设备 NoIO 时自动回落 Just Works
  NimBLEDevice::setSecurityIOCap(BLE_HS_IO_KEYBOARD_ONLY);
  NimBLEDevice::setSecurityAuth(true, false, true);   // bonding, no MITM 强制, Secure Connections
  NimBLEDevice::setSecurityPasskey(gPinNum);
  NimBLEDevice::setSecurityCallbacks(new BoschSecurity());
  // 发射功率用默认值：setPower() 在 core/NimBLE 各版本签名不一致，不冒险

  banner("准备就绪");
  Serial.println("操作顺序：");
  Serial.println("  1) 确认电脑上的 Python 界面/桥接已关闭（避免抢设备）");
  Serial.println("  2) 热水器面板 →『清除配对』→ 取新的 6 位码");
  Serial.println("  3) 在串口直接输入这 6 位码并回车（几秒内完成，不用重烧）");
  Serial.println("  4) 看 [SMP]/[AUTH]/[A0] 三段输出；失败就重取码再输一次");
  Serial.println();
  printHelp();

  // 断电/重启自动恢复：NVS 里有 6 位码就直接发起一次连接。
  // 依据（2026-11-07 实测）：配对成功过的码会被设备长期保留，配对窗口关闭后
  // 仍可用于重连（A2/07=01 → A2/08=01）。失败则退回等待输入，不影响手动输码。
  if (gPin.length() == 6) {
    Serial.printf("\n[BOOT] 已存面板码 %s，自动尝试重连…（失败可直接输入新码）\n",
                  gPin.c_str());
    startSession(false);
    Serial.println("[BOOT] 若连不上，等待输入新码");
  } else {
    Serial.println("\n等待输入…");
  }
}

void loop() {
  pollSerial();

  if (gPhase == PH_RUNNING && gClient) {
    if (!gClient->isConnected()) {
      Serial.println("[CONN] 连接已断，5 秒后自动重连（用当前码）");
      gPhase = PH_WAIT_CODE;
      disconnectClient();
      delay(5000);
      startSession(false);
      return;
    }
    // 30 秒没收到 A0 = 登录其实没生效，提示但不自动打断（便于观察）
    static uint32_t lastWarn = 0;
    if (gA0Count == 0 && millis() - gSessionStart > 30000 && millis() - lastWarn > 15000) {
      lastWarn = millis();
      Serial.println("[WARN] 已连接 30s 仍无 A0：多半是 A2/08 未通过，请重取码再输一次");
    }
  }
  delay(10);
}
