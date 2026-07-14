# LazyAdFinder 🎯

Ad QA 自動化工具組，兩個用途：

1. **找廣告**（`run_ios.py` / `run_android.py`）— 自動反覆點擊 ad placement，直到 Appier 廣告出現（bid 200）才停，把 bid request/response 留給你檢查。
2. **SSP SDK Signal QA**（`run_ssp.py` + `bid_inspector.py`）— 觸發 bid、抓 request、驗 data-signal 欄位（AND-xx TC），自動整包 evidence 按 test round 歸檔。

## How it works

```
Phone → Charles (8888) → mitmdump/detector.py (8081) → internet
                ↓                    ↓
        you inspect in Charles   bid 偵測 + capture
```

**偵測語意**（對照 appier-ads-android SDK source）：

- Bid = `POST *.apx.appier.net/v2/sdk/aos/ad`（prod `ad3` / staging `adx-stg`），response **200 = 有廣告、204 = no-bid**
- 其他 Appier 流量（imp/click tracker、`signal.appier.com` data-signal key）只記錄、不觸發停止
- 攔到的東西寫在：`/tmp/appier_bid.json`（request）、`/tmp/appier_bid_status`（200/204）、`/tmp/appier_bid_response.json`（贏標的 response）

---

## One-time setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
npm install -g appium
appium driver install uiautomator2   # Android
appium driver install xcuitest       # iOS
```

### 2. Charles + 手機 proxy

手機 Wi-Fi proxy 設為 Mac 的 IP（`ipconfig getifaddr en0`）port `8888`，
並在手機上裝 Charles CA cert（`chls.pro/ssl`）。

Charles → Proxy → External Proxy Settings：
- 勾選 **Use external proxy servers**
- HTTP / HTTPS Proxy 都設 `127.0.0.1` port `8081`（轉給 mitmdump 做偵測）

> Android 也可以不動 Wi-Fi 設定，用 adb 直接設：
> `adb shell settings put global http_proxy <MAC_IP>:8888`
> 測完記得清掉：`adb shell settings delete global http_proxy`

### 3. Android 裝置

開 USB 偵錯、裝好 sample app（`com.appier.android.sample`）即可。

### 4. iOS 裝置 — 簽 WebDriverAgent（只需一次）

有 Apple Developer Team ID 的話：

```bash
export XCODE_ORG_ID=XXXXXXXXXX   # Xcode → Settings → Accounts 裡看
python ~/LazyAdFinder/run_ios.py
```

沒有的話手動簽：

1. `open ~/.appium/node_modules/appium-xcuitest-driver/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj`
2. **WebDriverAgentLib** 和 **WebDriverAgentRunner** 兩個 target 都勾 Automatically manage signing、選你的 Team
3. Bundle Identifier 衝突就在後面加自己的字尾
4. 接手機、選 device、target 選 WebDriverAgentRunner、`Cmd + U`
5. 手機上信任開發者（Settings → General → VPN & Device Management）

---

## Run — 找廣告（Android）

三個 terminal：

```bash
# T1 — 偵測
mitmdump -s ~/LazyAdFinder/detector.py --listen-port 8081

# T2 — Appium
appium

# T3 — 自動點擊（參數：最多幾輪，預設 30）
python ~/LazyAdFinder/run_android.py 50
```

環境變數：

| 變數 | 預設 | 說明 |
|---|---|---|
| `TAB` | `Appier SDK` | sample app 分頁：`Appier SDK` / `AdMob Mediation` / `AppLovin Mediation` |
| `AD_LABEL` | `Native - basic format` | 要點的清單項目（各 tab 清單見 run_android.py docstring；AppLovin 的 Native 大小寫不同） |
| `STOP_ON` | `win` | `win` = bid 200 才停；`bid` = 看到 bid request 就停 |

ViewPager 會預載相鄰分頁（同名 label 會重複出現在 hierarchy），script 用元素座標
過濾畫面外的重複項，不用擔心點到隔壁 tab 的。

iOS 版：`python ~/LazyAdFinder/run_ios.py 50`（流程相同）。

## Run — SSP Signal QA（TC 驗證）

```bash
# T1 / T2 同上

# T3 — baseline：一次 capture 驗全部 checks
export APP_PACKAGE=com.appier.android.sample
export APP_ACTIVITY=com.appier.android.sample.MainActivity
export TEST_ROUND=R1                 # TC 表上的 round 標籤，不設就是 adhoc
python ~/LazyAdFinder/run_ssp.py

# 狀態類 TC：把裝置調成目標狀態後單獨 capture（支援逗號多選）
python ~/LazyAdFinder/run_ssp.py AND-04
python ~/LazyAdFinder/run_ssp.py AND-06,AND-08
```

### Evidence 結構（按 test round 分）

```
evidence/
  R1_20260709_180000/            # <TEST_ROUND>_<首次執行時間戳>，同標籤自動歸入
    round_report.txt             # 彙總：每條 check 取最新 capture 的結果 + 未跑清單
    baseline_20260709_180000/
      phone.png                  # bid 當下截圖
      bid_request.json           # 原始 bid request（req + ext data-signal payload）
      bid_response.json          # bid 200 才有
      device_state.txt           # adb 抓的裝置狀態（darkmode/電量/亮度/前景 activity/app 版本…）
      logcat.txt                 # app 啟動 → capture 全程 logcat
      logcat_appier.txt          # 只留 appier/argus/datasignal 相關行
      report.txt                 # 這次 capture 的欄位驗證表
      results.json               # 結構化結果（round 彙總用）
    AND-04_20260709_183000/      # 狀態類 TC 的單獨 capture，會覆蓋 baseline 同 TC 結果
```

手動重算彙總：`python bid_inspector.py --round evidence/R1_20260709_180000`
離線驗任一份 bid：`python bid_inspector.py --file /tmp/appier_bid.json [AND-04 ...]`

---

## Notes

- run script 啟動時會自動把 app 導回列表頁再開始。
- `bid_inspector.py` 的期望值已對照 SDK source 校正（charging int、conntype string enum、
  mem/disk bytes、duration ms）；SDK 尚未實作的欄位（vpn/ip/gyroscope/impression_history…）
  會 FAIL 並在 note 標明是 RD 未實作。
- data-signal 加密目前在 SDK 端是關閉的（ext 為明文）；若哪天打開，inspector 會直接報
  "ext is a string — encryption re-enabled"。
