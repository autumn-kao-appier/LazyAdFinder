# LazyAdFinder 🎯

Automates the tedious part of iOS ad QA: keep tapping into an ad placement until an Appier ad shows up, then stop and get out of your way.

## How it works

```
Phone → Charles (8888) → mitmdump/detector.py (8081) → internet
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
pip install -r requirements.txt
npm install -g appium
appium driver install xcuitest
```

### 2. Sign WebDriverAgent (required for real device)

> **捷徑**：如果你有 Apple Developer Team ID，可以跳過手動 Xcode 步驟，改用環境變數讓 Appium 自動簽：
> ```bash
> export XCODE_ORG_ID=XXXXXXXXXX   # 你的 Team ID（Xcode → Settings → Accounts 裡看）
> export WDA_BUNDLE_ID=com.yourname.wda  # bundle id 衝突時才需要設
> python ~/appier_qa/run.py
> ```
> 設了 `XCODE_ORG_ID` 之後下面的手動步驟可以略過。

**手動方式（沒有 Team ID 或自動簽失敗時）**

Appium 需要在你的手機上裝一個叫 WebDriverAgent 的輔助 app，裝之前要先用你的 Apple 帳號簽名。只需要做一次。

**Step 1 — 用 Terminal 打開 Xcode 專案**

```bash
open ~/.appium/node_modules/appium-xcuitest-driver/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj
```

**Step 2 — 簽名 WebDriverAgentLib**

1. Xcode 左側 sidebar 最上面點 **WebDriverAgent**（藍色資料夾圖示）
2. 中間面板上方有一排 target，點 **WebDriverAgentLib**
3. 點 **Signing & Capabilities** tab
4. 勾選 **Automatically manage signing**
5. **Team** 下拉選你的 Apple 帳號（沒有的話先去 Xcode → Settings → Accounts 登入）

**Step 3 — 簽名 WebDriverAgentRunner**

同上，把 target 換成 **WebDriverAgentRunner**，重複一樣的步驟。

**Step 4 — 確認沒有紅色錯誤**

Signing 那欄如果有紅色感嘆號，通常是 Bundle Identifier 跟別人重複。解法：把 Bundle Identifier 隨便改一下，例如在後面加你名字縮寫：
- `com.facebook.WebDriverAgentLib` → `com.facebook.WebDriverAgentLib.yourname`
- `com.facebook.WebDriverAgentRunner` → `com.facebook.WebDriverAgentRunner.yourname`

改完紅字應該就消失了。

**Step 5 — 接上手機，Build 到裝置上**

1. 手機接上 Mac
2. Xcode 左上角 device 選你的手機（不要選 Simulator）
3. 選 target **WebDriverAgentRunner**
4. `Cmd + U`（Run Tests）或上方選 **Product → Test**
5. 手機出現「要信任此開發者嗎」的提示 → Settings → General → VPN & Device Management → 信任

成功的話 Xcode 會顯示 build succeeded，手機畫面會短暫出現一個空白 app 然後消失，這是正常的。

> 之後直接跑 `python run.py` 就好，不需要再開 Xcode。

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
- Port: `8888`

Then open `chls.pro/ssl` in Safari on the iPhone → install the profile → Settings → General → About → Certificate Trust Settings → enable Charles.

### 5. Charles upstream proxy（選用，開啟後每輪會印出廣告主）

不設也能跑，只是不會顯示每輪是哪家的廣告。

設了之後每輪會印：
```
[3/30] tapping basic ...
         → Google/AdMob, Smadex
```

**設定方式：** Charles → Proxy → External Proxy Settings
- 勾選 **Use external proxy servers**
- HTTP Proxy: `127.0.0.1` port `8081`
- HTTPS Proxy: `127.0.0.1` port `8081`

---

## Run

Open three terminals:

```bash
# Terminal 1 — detection (silent, traffic visible in Charles)
mitmdump -s ~/appier_qa/detector.py --listen-port 8081 --ignore-hosts '.*\.apple\.com|.*\.mzstatic\.com'

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
