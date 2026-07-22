# LazyAdFinder — Claude 專案設定

## 關鍵字「刷」＝啟動整條自刷 QA 流程

使用者在對話中打「**刷**」（或「刷 aos」/「刷 ios」）時，啟動 SSP Signal QA 自刷流程：

1. 用 **AskUserQuestion** 依序問參數（能從「刷 aos」等推斷的就跳過該題）：
   - **平台**：AOS / iOS
   - **投放類型**：aibid / reen-static / reen-dynamic
   - **整合模式**：standalone / admob-mediation / applovin-mediation
   - **Test CID**：指定 campaign 或留空
   - **範圍**：預設為該平台目前實作的「完整 Signal 範圍」（AOS：84 個 Signal TC + 15 個 E2E TC；iOS：82 個 Signal TC + 15 個 E2E TC）；也可指定較小範圍或狀態 TC（如 AND-04 / IOS-04，可逗號多選）
2. 把答案設成環境變數，**非互動**執行。**先依「範圍」選對工具**——工具能力必須對得上承諾的範圍，否則狀態類 TC 會落成「缺證據」：
   - **完整範圍（含狀態類 TC）**：
     - AOS → **`manual_wizard.py`**（需 `APP_PACKAGE` / `APP_ACTIVITY`）。它依序佈狀態並逐批 capture
       （M1/M2/M3/SC/AUTO：深色模式／電量／充電／省電／時區／亮度／字級／音量／語系／定位／GAID
       opt-out／session…），對 fail 自動 retry，全部合併成一份 round。設不起來的狀態（VPN/GAID/SIM/
       root/AVD）走 fallback；`ALLOW_MANUAL_FALLBACK=0`（預設）時**跳過該批、不卡**。可用 `START_AT`/
       `STOP_AFTER` 補跑單一批次。env：`TEST_TYPE`/`TEST_MODE`/`TEST_CID`/`TEST_ROUND`。
     - iOS → 目前**沒有**狀態循環 runner；`run_ssp_ios.py` 只做單次 capture，故 iOS「完整範圍」暫時只
       覆蓋 AUTO 欄位＋逐條手動佈的狀態 TC（**待補 iOS wizard**）。
   - **單一 baseline / 指定單條或數條狀態 TC**（如 `AND-04,AND-06`）：
     - AOS → `run_ssp.py`（需 `APP_PACKAGE` / `APP_ACTIVITY`；`python run_ssp.py AND-04,AND-06`）
     - iOS → `run_ssp_ios.py`（`BUNDLE_ID=com.appier.Random`）
   > ⚠️ `run_ssp.py` 只做**一次 baseline、不會循環佈狀態**——別拿它跑「完整範圍」，那 ~34 個狀態類 TC
   > 會全變「缺證據」。完整範圍（AOS）一律用 `manual_wizard.py`。**跑之前先核對「工具能力 vs 承諾範圍」**。
   > 我從工具端執行，無法回答腳本的互動 stdin；所以改由我在對話問參數、再帶環境變數跑
   > （含 `TEST_CID`——`manual_wizard.py` 的 CID 未設時仍會 `input()`）。
3. **刷完自動執行 `./deploy_pages.sh`** —— 重產平台並部署到 GitHub Pages
   （gh-pages 分支），線上網址即時更新。無需另發 claude.ai artifact。

> ⚠️ GitHub Pages 目前是**公開**的（repo public），平台內嵌 IDFA/IDFV/裝置 MAC/
> GPS/截圖等敏感資料 —— 使用者已知悉並選擇維持公開自動部署。

## 判定原則：報告只由「本次 scope」決定 pass / fail / block

平台報告永遠對照**完整 TC 目錄**呈現，但每條 TC 的狀態由本次 run 實際做了什麼決定。
**判定狀態機（`build_artifact.classify()`）**：

1. **有 eligible capture（這輪有做）**：值符合 → **PASS**；值不符 → **FAIL**。
   FAIL **失敗一次就算**，不看次數（別再把單次 mismatch 降級成 block，否則真失敗如
   AND-67 `sdk_version=None` 會被藏掉）。
2. **無 capture（這輪沒做）** → **BLOCKED**。

**BLOCKED 的定位非常窄——只有「清楚的限制」或「這輪根本沒做」：**
- **RD/硬體限制**（`BLOCKED` ∪ `RD_GAP`，恆 block，即使抓到空值也不算 FAIL）：
  - 本輪 RD 沒做：SDK 未實作、值恆 null/[]（AND-42/43 感應器、AND-51 impression_history、
    AND-38 Not in this Release、AND-61 latency、AND-14 vpn…）
  - 硬體不可得：沒 SIM（AND-39/41/64）、需 AVD（AND-12）、需非 root 機（AND-10）
- **本輪未執行**：這輪沒佈該狀態／沒跑該情境（狀態類 TC 在 baseline 輪；AND-47 session 未跑 SC）。
  **這不是缺證據**——是這輪沒做。用 `manual_wizard.py` 佈狀態跑，它們就會變 PASS/FAIL。
- **整合模式/投放目的不適用**：E2E 依 `TEST_MODE`/`TEST_TYPE` 自動判 `na_mode`/`na_type` → BLOCKED
  （例：standalone 輪的 mediation-only TC-02/03/08/16）。權威在 **`e2e_catalog.py`** 的 `modes`/`types`
  欄位 + `evaluate()`；**不要**在 `build_artifact.py` 另立硬編 standalone 清單。REEN 輪 opt-out
  signal TC 走 `TYPE_NA_REEN`（標 N/A，計入 Blocked tile）。

> 平台「總覽卡」層級：**完全沒 round 的 cell** 標「未執行 / No run」（`build_platform.render_card`），
> 不可顯示成 0/0/84 Blocked——那會跟真的 blocked 混淆。

> 對應契約：run_ssp 的 BASELINE capture 存進 `baseline_<ts>/`，`build_artifact.capture_candidates()`
> 必須認得此資料夾去對應 AUTO_TCS（靠 `declared()` 的 BASELINE→AUTO_TCS 判定，勿只認 `AUTO_` 前綴），
> 否則 scope 內的 signal TC 會被誤判全 BLOCKED。

## 專案結構速查

- **找廣告**：`run_ios.py` / `run_android.py`
- **SSP Signal QA（AOS）**：`run_ssp.py`（單次 baseline／指定 TC）+ `bid_inspector.py` + `build_artifact.py`（AND-xx）
  - **完整範圍 runner**：`manual_wizard.py` —— 無人值守，逐批佈狀態(M1/M2/M3/SC/AUTO)再 capture、合併成一份 round；「完整範圍」用它，不是 `run_ssp.py`
- **SSP Signal QA（iOS）**：`run_ssp_ios.py` + `ios_bid_inspector.py` + `build_artifact_ios.py`（IOS-xx）
  - iOS bid 的 `ext_enc` / `req_enc` 用 `apr_xorenc.py` 解碼後才驗證
- **偵測**：`detector.py`（mitmdump addon，攔 `/v2/sdk/{aos,ios}/ad`）
- **報告平台**：`build_platform.py` → AOS/iOS × Standalone/Mediation × 三分類；
  部署在 GitHub Pages（`gh-pages` 分支根目錄，https://autumn-kao-appier.github.io/LazyAdFinder/）
- **授權清單**：`PERMISSIONS.md`（Mac Full Disk Access、iPhone 信任設定…）

## 慣例

- `artifact-*.html` 與 `evidence/` 是生成物／測試資料，已 gitignore，不進 repo。
- 開發直接在 `main`；平台部署在 `gh-pages`。
- 平台 artifact 發佈網址固定，同一 file path 重發即更新。
