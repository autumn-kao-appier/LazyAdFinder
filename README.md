# LazyAdFinder 🎯

Automates the tedious part of iOS ad QA: keep tapping into an ad placement until an Appier ad shows up, then stop and get out of your way.

## How it works

```
mitmproxy (detector.py)   ←── watches network traffic
        +
Appium  (run.py)          ←── taps "basic" → waits 2s → backs out → repeat
        ↓
Appier request detected?  → script stops, you inspect in mitmweb
```

Tested against **appierAdSwift** (`com.appier.Random`) — AdMob Mediation / Native ad flow.

---

## Prerequisites

```bash
pip install mitmproxy Appium-Python-Client
npm install -g appium
appium driver install xcuitest
```

Install the mitmproxy CA cert on your iOS device:
→ https://docs.mitmproxy.org/stable/concepts-certificates/

---

## Setup (one-time)

1. Connect iPhone to the same Wi-Fi as your Mac.
2. On iPhone: **Settings → Wi-Fi → (your network) → Configure Proxy → Manual**
   - Server: your Mac's local IP
   - Port: `8080`
3. Build & install **appierAdSwift** on the device.

---

## Run

Open three terminals:

```bash
# 1. mitmproxy (also opens web UI at http://127.0.0.1:8081)
mitmweb -s ~/appier_qa/detector.py --listen-port 8080

# 2. Appium
appium

# 3. The script (optional: pass max rounds, default 30)
python ~/appier_qa/run.py 50
```

When an Appier request is detected, the script prints the matched URL and stops. Open **http://127.0.0.1:8081** to inspect the full request in mitmweb.

---

## Multiple devices

Uncomment and fill in the `udid` line in `run.py`:

```python
options.udid = "YOUR_DEVICE_UDID"
```

Get UDID via: `xcrun xctrace list devices`
