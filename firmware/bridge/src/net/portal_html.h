// The provisioning web page, as a single PROGMEM string.
//
// Constraints that shaped it:
//   * served by an ESP32 in SoftAP mode - one file, no assets, no JS framework, works in the
//     browsers that come preinstalled on phones (the captive-portal popup is often a stripped
//     WebView with no fetch(), so the form posts the old-fashioned way)
//   * the heater list and the Wi-Fi list are injected server-side into the two <select>s, so
//     the page renders fully before any script runs; the script only upgrades the experience
//   * every label matches the words printed on the heater panel and in the manual

#include <pgmspace.h>

namespace net {
namespace provision_html {

// Placeholder tokens, replaced by the server before sending:
//   @HEATER_OPTIONS@   <option> rows from the BLE scan
//   @WIFI_OPTIONS@     <option> rows from the Wi-Fi scan
//   @STATE@            current provisioning state, e.g. "unpaired"
//   @ERROR@            validation error from the previous submit, or ""
static const char PAGE[] PROGMEM = R"HTML(<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Water heater bridge</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; background: #f5f6f8; color: #222; }
  .card { max-width: 420px; margin: 24px auto; background: #fff; border-radius: 12px;
          padding: 20px 22px; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
  h1 { font-size: 18px; margin: 0 0 2px; }
  .state { color: #666; font-size: 13px; margin-bottom: 16px; }
  .step { font-size: 12px; color: #1a56a0; text-transform: uppercase; letter-spacing: .06em;
          margin: 14px 0 6px; }
  label { display: block; font-size: 14px; margin: 10px 0 4px; }
  select, input { width: 100%; box-sizing: border-box; padding: 10px; font-size: 16px;
                  border: 1px solid #ccc; border-radius: 8px; background: #fff; }
  input[type=submit] { margin-top: 18px; background: #1a56a0; color: #fff; border: 0;
                       font-weight: 600; padding: 12px; }
  .hint { font-size: 12px; color: #888; margin-top: 4px; }
  .error { background: #fdecea; color: #c0392b; border-radius: 8px; padding: 10px 12px;
           font-size: 14px; margin-bottom: 12px; }
  .code { letter-spacing: .35em; font-size: 20px; text-align: center; }
</style>
</head>
<body>
<form class="card" method="POST" action="/save">
  <h1>Water heater bridge</h1>
  <div class="state">Status: @STATE@</div>

  <div class="error" id="err" style="display:@ERROR_DISPLAY@">@ERROR@</div>

  <div class="step">1 &middot; Your heater</div>
  <label for="heater">Heaters in range</label>
  <select name="heater" id="heater">@HEATER_OPTIONS@</select>
  <div class="hint">Look for the name shown on the heater panel. Nothing listed? Move closer
    and reload.</div>

  <div class="step">2 &middot; Wi-Fi</div>
  <label for="ssid">Network</label>
  <select name="ssid" id="ssid" onchange="document.getElementById('manual').style.display =
    this.value==='__other__' ? 'block' : 'none'">@WIFI_OPTIONS@</select>
  <input name="ssidmanual" id="manual" placeholder="Network name (hidden networks)"
         style="display:none; margin-top:6px">
  <label for="wifipass">Password</label>
  <input name="wifipass" id="wifipass" type="password" autocomplete="off">
  <div class="hint">Leave empty to keep the current settings when only re-pairing.</div>

  <div class="step">3 &middot; Pairing code</div>
  <label for="code">The six digits on the heater panel</label>
  <input name="code" id="code" class="code" type="text" inputmode="numeric" pattern="[0-9]{6}"
         maxlength="6" placeholder="000000" autocomplete="off">
  <div class="hint">Heater panel &rarr; clear pairing &rarr; a new six-digit code appears.
    Enter it while it is still on the panel. Leave empty to keep the existing pairing.</div>

  <div class="step">4 &middot; MQTT broker (optional)</div>
  <label for="broker">Broker address</label>
  <input name="broker" id="broker" placeholder="192.168.9.206 or 192.168.9.206:1883"
         autocomplete="off">
  <label for="mqttuser">Username</label>
  <input name="mqttuser" id="mqttuser" autocomplete="off">
  <label for="mqttpass">Password</label>
  <input name="mqttpass" id="mqttpass" type="password" autocomplete="off">
  <div class="hint">Where the telemetry goes. Leave everything empty to keep the current
    settings; the console command <b>broker -</b> removes the broker.</div>

  <input type="submit" value="Save and connect">
</form>
</body>
</html>)HTML";

}  // namespace provision_html
}  // namespace net
