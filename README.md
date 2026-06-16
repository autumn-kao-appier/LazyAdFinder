# LazyAdFinder 🎯

Automates the tedious part of iOS ad QA: keep tapping into an ad placement until an Appier ad shows up, then stop and get out of your way.

## How it works

```
Phone → Charles (8080) → mitmdump/detector.py (8081) → internet
                ↓
        you inspect in Charles
                +
        Appier request detected? → script stops
```

Tested against **appierAdSwift** (`com.appier.Random`) — AdMob Mediation / Native ad flow.

---

## One-time setup

### 1. Install dependencies

```bash
pip install mitmproxy Appium-Python-Client
npm install -g appium
appium driver install xcuitest
```

### 2. Sign WebDriverAgent (required for real device)

```bash
open ~/.appium/node_modules/appium-xcuitest-driver/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj
```

In Xcode: select **WebDriverAgentLib** and **WebDriverAgentRunner** targets → Signing & Capabilities → set your Apple Developer Team.

### 3. Connect your iPhone

UDID is auto-detected when only one iPhone is connected. If you have multiple devices, pass it explicitly:

```bash
# find UDIDs
xcrun xctrace list devices

# run with specific device
python ~/appier_qa/run.py 30 YOUR_DEVICE_UDID
```

### 4. Install Charles CA cert on iPhone

Settings → Wi-Fi → (your network) → Configure Proxy → Manual
- Server: your Mac's local IP (`ipconfig getifaddr en0`)
- Port: `8080`

Then open `chls.pro/ssl` in Safari on the iPhone → install the profile → Settings → General → About → Certificate Trust Settings → enable Charles.

### 5. Charles upstream proxy (one-time)

Proxy → External Proxy Settings → enable → set HTTP + HTTPS to `127.0.0.1:8081`

---

## Run

Open three terminals:

```bash
# Terminal 1 — detection (silent, traffic visible in Charles)
mitmdump -s ~/appier_qa/detector.py --listen-port 8081

# Terminal 2 — Appium
appium

# Terminal 3 — the script (optional: max rounds, default 30)
python ~/appier_qa/run.py 50
```

When an Appier request is detected, the script prints `[STOP]` and halts. Inspect the full request in Charles.

To stop early: `Ctrl+C` in Terminal 3. Terminals 1 and 2 stay open for the next run.

---

## Notes

- The script handles the case where the app launches already on the ad page — it navigates back to the list automatically before starting.
- Detection matches any request to `appier.net` or `appier.com`.
