# 權限清單 — 讓自動測試不被系統彈窗卡住

這份清單只列**這台 Mac + 這支測試 iPhone**需要「按一次同意」的地方。全部都是
**一次性**設定——按過一次之後，之後每次跑 `run_ssp.py` / `run_ssp_ios.py` /
`build_platform.py` 都不會再跳出任何系統詢問。

判斷方式：下面每一項都附了「怎麼確認目前狀態」的指令，跑了才知道哪幾項還沒弄。

---

## A. Mac 系統設定

### A1. Full Disk Access（必要 — 目前缺）

**現象**：讀取 `~/Desktop/...` 之類的資料夾時噴
`PermissionError: [Errno 1] Operation not permitted`（`evidence` 若指到
Desktop/Documents 以外的位置就會踩到；這次 `~/Desktop/LazyAdFinder_evidence`
已經踩到）。

**原因**：跑這些腳本的實際 App 是 **Terminal**（process tree：
`Terminal → login → zsh → …`），macOS 對 Desktop/Documents/Downloads/iCloud
等資料夾的存取是**按 App 授權**，不是按使用者；Terminal 目前只有部分資料夾權限，
且這種存取被拒絕時是**靜默失敗**（不會跳出可以點的對話框，因為背景 script 沒有
UI 可以觸發詢問），只能靠這條指令先手動開權限。

**設定步驟**（一次）：
1. 系統設定 → 隱私權與安全性 → **完整磁碟取用權限**（Full Disk Access）
2. 加入 **Terminal**（若清單沒有，點左下角 `+`，路徑
   `/System/Applications/Utilities/Terminal.app`）
3. 打勾啟用 → 完全關閉並重開 Terminal（Full Disk Access 生效需要重啟 App）

**確認指令**：
```bash
ls ~/Desktop >/dev/null 2>&1 && echo OK || echo DENIED
```

> 用的是 iTerm2 / VS Code 內建終端機而不是 Terminal.app？把對應的 App
> （`iTerm.app` / `Visual Studio Code.app` 等）加進同一個 Full Disk Access
> 清單，道理一樣。

---

### A2. Local Network（建議先開，避免未來卡住）

**現象**：手機透過 Wi-Fi 連到 Mac 的 mitmdump（8081）/ Charles（8888）時，
macOS 第一次可能跳「Terminal 想要在區域網路上查找並連接裝置」的詢問。

**設定步驟**：
1. 系統設定 → 隱私權與安全性 → **區域網路**
2. 找到 Terminal，打勾啟用

**確認指令**：目前這次操作沒有實際觸發到（可能已授權，或這次連線模式沒用到
mDNS），但這是 mitmproxy 類工具常見的第一次執行提示，建議先開起來一次解決。

---

### A3. 防火牆允許連入連線（目前不影響 — 防火牆本身是關的）

**現象**：如果之後有人把 Mac 的「應用程式防火牆」打開，`mitmdump` 第一次
監聽 port 收到外部連線（手機打過來）時，會跳「是否允許 mitmdump/python3
接受連入連線」的對話框。

**目前狀態**：
```bash
/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate
# → Firewall is disabled. (State = 0)   ← 目前是關的，不會卡
```

**若之後防火牆被打開**，設定步驟：
1. 系統設定 → 網路 → 防火牆 → 選項
2. 加入 `python3`（`/opt/homebrew/bin/python3`）與 `mitmdump`
   （`~/Library/Python/3.14/bin/mitmdump`），設為「允許連入連線」

---

## B. iPhone 裝置設定（每一支實測手機各自要弄一次）

### B1. Trust This Computer（已完成，此裝置免動作）

**現象**：USB 接上 Mac 第一次會在手機螢幕跳「信任這台電腦？」，要輸入
裝置密碼確認。沒信任的話 `idevice*` 系列工具會整個連不上。

**確認指令**：
```bash
idevicepair -u <UDID> validate
# → SUCCESS: Validated pairing with device <UDID>   ← 已通過
```

若換一支新手機測試，記得先用資料線接上、螢幕解鎖後點「信任」再開始跑腳本。

### B2. 信任開發者憑證 / WebDriverAgent（已完成，此裝置免動作）

**現象**：第一次裝 Appium 自動簽的 WebDriverAgentRunner，手機會顯示
「未信任的開發者」，要去 設定 → 一般 → VPN 與裝置管理 手動信任。

**確認方式**：這支手機上已經裝著
`com.facebook.WebDriverAgentRunner.xctrunner`（跑過的痕跡），代表已經信任過。
換新裝置或新的簽名憑證（`XCODE_ORG_ID` 換人/換 Team）時要重新走一次
`README.md`「iOS 裝置」章節的手動簽名流程。

### B3. Charles 憑證完全信任（已完成，此裝置免動作）

**現象**：裝了 Charles CA（`chls.pro/ssl`）後，iOS 15+ 還要多一步手動開關，
不然裝了憑證也不會生效：

設定 → 一般 → 關於本機 → 憑證信任設定 → 針對 Charles Proxy CA 開啟「完全信任」

**確認方式**：這次 Charles 接上正確 proxy 後有成功解密一般流量（非 pinned
主機），代表這支手機已經完成這步。

### B4. App Tracking Transparency 授權彈窗（已在程式碼處理，非權限設定）

**現象**：全新安裝 / 重置過的 app 第一次要 IDFA 時，系統會跳「允許『追蹤』
您的活動嗎？」的彈窗——這**不是**要你去系統設定裡預先開，而是每個 app
第一次要 IDFA 時都會問一次（跟前面幾項「設定裡打勾一次就好」不同類）。

**這次改法**：`run_ssp_ios.py` 已加上 Appium 的 `autoAcceptAlerts` capability，
啟動 session 時如果跳出任何系統彈窗（含 ATT）會自動接受，不需要人在旁邊點。
✅ 這項不需要你做任何操作，已經是程式碼層面解決。

---

## 目前狀態總結（2026-07-20 這次盤點）

| 項目 | 狀態 | 需要你做的事 |
|---|---|---|
| A1 Full Disk Access | ❌ 缺 | 系統設定加 Terminal，重開 Terminal |
| A2 Local Network | 未確認會不會卡 | 建議順手開一次 |
| A3 防火牆例外 | ✅ 不影響（防火牆本來就關） | 無 |
| B1 Trust This Computer | ✅ 已配對 | 無（新手機才要重做） |
| B2 信任開發者憑證 | ✅ 已信任 | 無（新手機才要重做） |
| B3 Charles 憑證完全信任 | ✅ 已生效 | 無（新手機才要重做） |
| B4 ATT 彈窗 | ✅ 程式碼已自動處理 | 無 |

**只剩 A1 需要你手動點一次**，弄完之後整條 `run_ssp_ios.py` →
`build_artifact_ios.py` → `build_platform.py` 流程就不會再被任何系統對話框
擋住。
