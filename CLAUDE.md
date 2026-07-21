# LazyAdFinder — Claude 專案設定

## 關鍵字「刷」＝啟動整條自刷 QA 流程

使用者在對話中打「**刷**」（或「刷 aos」/「刷 ios」）時，啟動 SSP Signal QA 自刷流程：

1. 用 **AskUserQuestion** 依序問參數（能從「刷 aos」等推斷的就跳過該題）：
   - **平台**：AOS / iOS
   - **投放類型**：aibid / reen-static / reen-dynamic
   - **整合模式**：standalone / admob-mediation / applovin-mediation
   - **Test CID**：指定 campaign 或留空
   - **範圍**：BASELINE（全跑）或指定狀態 TC（如 AND-04 / IOS-04，可逗號多選）
2. 把答案設成環境變數，**非互動**執行：
   - AOS → `run_ssp.py`（需 `APP_PACKAGE` / `APP_ACTIVITY`）
   - iOS → `run_ssp_ios.py`（`BUNDLE_ID=com.appier.Random`）
   > 我從工具端執行，無法回答腳本的互動 stdin；所以改由我在對話問參數、再帶環境變數跑。
3. 跑完自動 `python build_platform.py --standalone <index>` 重產平台，並視情況部署
   （gh-pages / Artifact）。

## 專案結構速查

- **找廣告**：`run_ios.py` / `run_android.py`
- **SSP Signal QA（AOS）**：`run_ssp.py` + `bid_inspector.py` + `build_artifact.py`（AND-xx）
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
