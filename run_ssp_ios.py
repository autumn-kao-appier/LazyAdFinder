#!/usr/bin/env python3
"""run_ssp_ios.py — iOS 版 SSP Signal QA 擷取（移植 run_ssp.py，Phase 1：只擷取）。

對照 run_ssp.py（Android）的 baseline capture 流程，換成 iOS 裝置層：
  - 啟動 app / 觸發廣告 / 截圖 / UI dump  → Appium XCUITest（非 adb + uiautomator）
  - bid request / response / 全流量         → detector.py 既有的 /v2/sdk/ios/ad endpoint
  - 裝置狀態 / 環境                          → ideviceinfo（libimobiledevice）
  - 系統 log 側錄                            → idevicesyslog（取代 adb logcat）

驗證：
  用 ios_bid_inspector.py 的 IOS-xx TC（由 AOS AND-xx 依 iOS 語意改寫，重用同一套
  check 引擎）產出 report.txt / results.json / round_report.txt。約八成 TC 用最合理
  推測值可直接判讀；約兩成標 [待校準]（欄位路徑或期望值需對第一份真實 iOS bid 校準，
  見同資料夾 ios_bid_summary.txt）。路徑猜錯只會顯示 FAIL/missing，不會假 PASS。
  round 資料夾以 IOS_ 前綴命名，build_platform.py 會辨識為 iOS 入口（平台 render 待
  Phase 3 build_artifact 平台感知；在那之前 build_platform 以 IOS_REPORTING_READY 擋著）。

用法：
    export BUNDLE_ID=com.appier.ssp.sample      # 必填
    export TEST_ROUND=R1                         # 不設＝adhoc
    # 不設 TEST_TYPE/TEST_MODE/TEST_CID 時會互動詢問（同 run_ssp.py）
    python run_ssp_ios.py [TC_ID] [UDID]

    # sample app 為分頁結構（Appier Direct / AdMob Mediation / …），頁籤與觸發
    # 版位會依 TEST_MODE 自動推斷（見 TAB_NAME / TAB_TRIGGER_LABEL）；
    # sample app 換版或對應不上時手動覆蓋：
    #   export TAB="AdMob Mediation"
    #   export TRIGGER_LABEL="mediation (AdMob + Appier)"

前置：
    pip install Appium-Python-Client
    brew install libimobiledevice        # 提供 ideviceinfo / idevicesyslog
    appium（另開 terminal）；WebDriverAgent 已簽（見 run_ios.py 說明）
    detector：mitmdump -s detector.py --listen-port 8081
    手機 Wi-Fi proxy → Mac IP:8888（Charles），Charles upstream → 127.0.0.1:8081
"""
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from appium import webdriver
from appium.options.ios.xcuitest.base import XCUITestOptions

# ── detector 產出的檔案（跨平台，與 run_ssp.py 共用協定）─────────────────────────
FLAG_FILE         = "/tmp/appier_hit"
BID_FILE          = "/tmp/appier_bid.json"
FIRST_BID_FILE    = "/tmp/appier_first_bid.json"
BID_STATUS_FILE   = "/tmp/appier_bid_status"
BID_RESPONSE_FILE = "/tmp/appier_bid_response.json"
IMPRESSION_FILE   = "/tmp/appier_impression.json"
TRAFFIC_FILE      = "/tmp/appier_traffic.jsonl"
NETWORK_FILE      = "/tmp/current_networks"
SYSLOG_TMP        = "/tmp/appier_ios_syslog.txt"

SYSLOG_PROC = None


# ── 終端機進度條 ──────────────────────────────────────────────────────────────
def progress(cur, total, label="", width=26):
    """單行原地更新的進度條（\\r）。cur>=total 時收尾換行。"""
    total = max(int(total), 1)
    cur = max(0, min(int(cur), total))
    filled = round(width * cur / total)
    bar = "█" * filled + "░" * (width - filled)
    pct = int(100 * cur / total)
    line = f"\r  ▕{bar}▏ {cur}/{total} {pct:3d}%  {label}"
    sys.stdout.write(line[:110].ljust(112))
    sys.stdout.flush()
    if cur >= total:
        sys.stdout.write("\n")
        sys.stdout.flush()


def wait_with_countdown(deadline, ready, label="等待 bid response"):
    """等到 ready() 為真或 deadline 到；期間顯示倒數進度條。回傳是否 ready。"""
    total = max(deadline - time.monotonic(), 0.01)
    while True:
        remain = deadline - time.monotonic()
        if ready():
            progress(total, total, label + "（已收到）")
            return True
        if remain <= 0:
            progress(total, total, label + "（逾時）")
            return False
        progress(total - remain, total, f"{label} … {remain:0.0f}s")
        time.sleep(0.2)


APPIUM_URL   = "http://127.0.0.1:4723"
BID_TIMEOUT  = 12.0
EVIDENCE_DIR = Path(os.environ.get("EVIDENCE_DIR", Path(__file__).parent / "evidence"))

BUNDLE_ID    = os.environ.get("BUNDLE_ID", "").strip()
# iOS sample app（AppierAdsSwiftSample）為分頁結構，跟 Android 版一樣要先選頁籤
# （2026-07-20 實機 dump 確認）：
#   Tab bar：「Appier Direct」/「AdMob Mediation」
#   可點擊版位文字（accessibility id）＝該頁籤下的副標題文字，不是 "basic"：
#     Appier Direct   → "direct (AppierAds SDK)"
#     AdMob Mediation → "mediation (AdMob + Appier)"
# 若之後 sample app 換版或加 AppLovin 頁籤，文字可能不同——先用
# ios_dump_labels() 或 --dump-labels 重新盤點再改這裡。
TAB_TRIGGER_LABEL = {
    "standalone": "direct (AppierAds SDK)",
    "admob-mediation": "mediation (AdMob + Appier)",
    "applovin-mediation": "mediation (AppLovin + Appier)",  # 待實機確認（尚未 dump 過此頁籤）
}
TAB_NAME = {
    "standalone": "Appier Direct",
    "admob-mediation": "AdMob Mediation",
    "applovin-mediation": "AppLovin Mediation",  # 待實機確認 tab 名稱
}
# 環境變數可覆蓋自動推斷（TEST_MODE 解析前先讀不到 TEST_MODE，故 trigger label
# 在 main() 內、resolve_test_mode() 之後才決定；這裡只放使用者顯式覆蓋值）
TRIGGER_LABEL_OVERRIDE = os.environ.get("TRIGGER_LABEL", os.environ.get("AD_LABEL", "")).strip()
TAB_OVERRIDE = os.environ.get("TAB", "").strip()
TEST_ROUND   = os.environ.get("TEST_ROUND", "adhoc")
VALID_TYPES  = ("aibid", "reen-static", "reen-dynamic")
VALID_MODES  = ("standalone", "admob-mediation", "applovin-mediation")
TEST_TYPE    = os.environ.get("TEST_TYPE", "").strip().lower()
TEST_MODE    = os.environ.get("TEST_MODE", "").strip().lower()
TEST_CID     = os.environ.get("TEST_CID", "").strip()
TEST_EXECUTOR = os.environ.get("TEST_EXECUTOR", "").strip() or getpass.getuser()

# WebDriverAgent 自動簽名（同 run_ios.py）
XCODE_ORG_ID  = os.environ.get("XCODE_ORG_ID")
WDA_BUNDLE_ID = os.environ.get("WDA_BUNDLE_ID")

DWELL_SEC       = float(os.environ.get("DWELL_SEC", "0"))
AD_RETRY_DELAY  = float(os.environ.get("AD_RETRY_DELAY", "2"))
MAX_AD_ATTEMPTS = int(os.environ.get("MAX_AD_ATTEMPTS", "150"))
SAVE_ON_BID     = os.environ.get("SAVE_ON_BID", "0") == "1"
CAPTURE_LABEL   = os.environ.get("CAPTURE_LABEL", "").strip()
STATE_ACTION    = os.environ.get("STATE_ACTION")

TC_ID = sys.argv[1] if len(sys.argv) > 1 else "BASELINE"
UDID  = sys.argv[2] if len(sys.argv) > 2 else (os.environ.get("UDID", "").strip() or None)


# ── 互動詢問（對照 run_ssp.py 的 resolve_*）─────────────────────────────────────
def resolve_test_type():
    global TEST_TYPE
    if TEST_TYPE in VALID_TYPES:
        return TEST_TYPE
    if not sys.stdin.isatty():
        sys.exit(f"TEST_TYPE 必填且須為 {VALID_TYPES}（非互動環境請用環境變數帶入）")
    print("投放目的？ 1) AIBID  2) REEN")
    goal = input("選 [1/2]: ").strip()
    if goal == "1":
        TEST_TYPE = "aibid"
    elif goal == "2":
        creative = input("素材？ 1) Static  2) Dynamic [1/2]: ").strip()
        TEST_TYPE = "reen-static" if creative == "1" else "reen-dynamic"
    else:
        sys.exit("無效選擇。")
    return TEST_TYPE


def resolve_test_mode():
    global TEST_MODE
    if TEST_MODE in VALID_MODES:
        return TEST_MODE
    if not sys.stdin.isatty():
        return "standalone"
    print("SDK 整合模式？ 1) standalone  2) admob-mediation  3) applovin-mediation")
    sel = input("選 [1/2/3]（預設 1）: ").strip() or "1"
    TEST_MODE = {"1": "standalone", "2": "admob-mediation",
                 "3": "applovin-mediation"}.get(sel, "standalone")
    return TEST_MODE


def resolve_test_cid():
    global TEST_CID
    if TEST_CID:
        return TEST_CID
    if not sys.stdin.isatty():
        return ""
    TEST_CID = input("測試 CID（可留空）: ").strip()
    return TEST_CID


def resolve_round_dir():
    """同 run_ssp.py，但 round 名加 IOS_ 前綴 → build_platform 認得是 iOS 入口。"""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    safe_cid = re.sub(r"[^A-Za-z0-9_-]+", "-", TEST_CID).strip("-")
    type_label = TEST_TYPE.upper().replace("-", "_")
    mode_label = TEST_MODE.upper().replace("-", "_")
    prefix = f"IOS_{mode_label}_{type_label}_CID_{safe_cid}_{TEST_ROUND}"
    existing = sorted(d for d in EVIDENCE_DIR.glob(f"{prefix}_*") if d.is_dir())
    if existing:
        return existing[-1]
    return EVIDENCE_DIR / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


# ── iOS 裝置層（取代 adb helpers）───────────────────────────────────────────────
def _have(tool):
    return shutil.which(tool) is not None


def detect_udid():
    """偵測唯一連接的 iPhone（沿用 run_ios.py 的 xctrace 解析）。"""
    if UDID:
        return UDID
    out = subprocess.check_output(["xcrun", "xctrace", "list", "devices"], text=True)
    devices_section = out.split("== Devices ==")[1].split("==")[0]
    udids = re.findall(r'\(([0-9A-Fa-f]{40}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16})\)',
                       devices_section)
    if not udids:
        sys.exit("找不到連接的 iPhone，請接上手機或手動指定 UDID。")
    if len(udids) > 1:
        sys.exit(f"偵測到多台裝置：{udids}\n請執行：python run_ssp_ios.py {TC_ID} <UDID>")
    return udids[0]


def dismiss_system_alert(driver):
    """自動接受系統彈窗（App Tracking Transparency 授權詢問等）。

    全新裝置/app 重置後，SDK 第一次要 IDFA 會觸發 ATT 系統彈窗；headless 自動化
    沒有人可以點，會卡住等到 timeout。沒有彈窗時 accept 會丟例外，直接吞掉即可
    （不影響原本流程）。"""
    try:
        driver.execute_script("mobile: alert", {"action": "accept"})
        print("  [note] 已自動接受系統彈窗（可能是 App Tracking Transparency 授權）")
        return True
    except Exception:
        return False


def select_tab(driver, tab_name):
    """切到 sample app 的指定頁籤（Tab Bar 按鈕的 accessibility id＝頁籤名稱）。
    對照 run_android.py 的 select_tab；iOS 版靠 XCUITest tab bar button 直接命中，
    不需要 Android 那種 ViewPager 預載座標過濾。"""
    if not tab_name:
        return True
    try:
        driver.find_element("accessibility id", tab_name).click()
        time.sleep(0.6)
        return True
    except Exception as exc:
        print(f"  [warn] 切頁籤 '{tab_name}' 失敗：{exc}")
        return False


# group → 從 Settings 根層依序要點的 row（可見文字；_tap_settings_row 會捲動尋找）。
# 對照 ios_bid_inspector.IOS_STATE 的 group。
IOS_SETTINGS_NAV = {
    "darkmode":    ["Display & Brightness"],
    "brightness":  ["Display & Brightness"],
    "textsize":    ["Display & Brightness", "Text Size"],
    "lowpower":    ["Battery"],
    "charging":    ["Battery"],
    "batterylevel": ["Battery"],
    "tracking":    ["Privacy & Security", "Tracking"],
    "geo":         ["Privacy & Security", "Location Services"],
    "tz":          ["General", "Date & Time"],
    "language":    ["General", "Language & Region"],
    "vpn":         ["General", "VPN & Device Management"],
    "deviceinfo":  ["General", "About"],
}


def _tap_settings_row(driver, label):
    """在 Settings 內找一列並點入（先 accessibility id，再可見文字，找不到就捲動）。"""
    for _ in range(8):
        for by, val in (("accessibility id", label),
                        ("-ios predicate string",
                         f'label == "{label}" OR name == "{label}"')):
            try:
                driver.find_element(by, val).click()
                time.sleep(0.8)
                return True
            except Exception:
                pass
        try:
            driver.execute_script("mobile: scroll", {"direction": "down"})
            time.sleep(0.3)
        except Exception:
            break
    return False


def capture_state_proof_ios(driver, folder, groups):
    """狀態 TC：導航到對應 iOS 設定頁截圖 state_proof_<group>.png（設定當下畫面）。"""
    for group in groups:
        nav = IOS_SETTINGS_NAV.get(group)
        if not nav:
            continue
        try:
            driver.terminate_app("com.apple.Preferences")
            time.sleep(0.5)
            driver.activate_app("com.apple.Preferences")
            time.sleep(1.2)
            reached = all(_tap_settings_row(driver, row) for row in nav)
            time.sleep(0.6)
            path = str(folder / f"state_proof_{group}.png")
            driver.get_screenshot_as_file(path)
            print(f"  state_proof → {path}" + ("" if reached else "（部分導航未命中，截目前畫面）"))
        except Exception as exc:
            print(f"  [warn] state_proof {group} 擷取失敗：{exc}")
    try:
        driver.activate_app(BUNDLE_ID)   # 回到受測 app
        time.sleep(0.8)
    except Exception:
        pass


def ideviceinfo(key=None, domain=None):
    """讀 ideviceinfo 單一 key（或整個 domain）。查不到回 ''。需 libimobiledevice。"""
    if not _have("ideviceinfo"):
        return ""
    cmd = ["ideviceinfo"]
    if UDID:
        cmd += ["-u", UDID]
    if domain:
        cmd += ["-q", domain]
    if key:
        cmd += ["-k", key]
    try:
        return subprocess.check_output(cmd, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception as e:
        return f"[err: {e}]"


def start_syslog():
    """從 app 啟動前開始側錄 idevicesyslog（取代 adb logcat）。"""
    global SYSLOG_PROC
    if not _have("idevicesyslog"):
        print("  [warn] 找不到 idevicesyslog（brew install libimobiledevice）；跳過 syslog 側錄。")
        return
    cmd = ["idevicesyslog"]
    if UDID:
        cmd += ["-u", UDID]
    # 只留受測 app + Appier 相關行，避免整機 syslog 過大
    if BUNDLE_ID:
        cmd += ["-p", BUNDLE_ID]
    out = open(SYSLOG_TMP, "w")
    SYSLOG_PROC = subprocess.Popen(cmd, stdout=out, stderr=subprocess.DEVNULL)


def stop_syslog():
    global SYSLOG_PROC
    if SYSLOG_PROC is not None:
        SYSLOG_PROC.terminate()
        try:
            SYSLOG_PROC.wait(timeout=3)
        except subprocess.TimeoutExpired:
            SYSLOG_PROC.kill()
        SYSLOG_PROC = None


IMPRESSION_RE = re.compile(
    r"[?&]cid=([^&\s]+).*?[&]crid=([^&\s]+)")


def scan_syslog_ad_identity():
    """從 syslog 的 impression tracker URL 撈實際載入廣告的 cid/crid。查不到回 None。"""
    if not os.path.exists(SYSLOG_TMP):
        return None
    identity = None
    for line in open(SYSLOG_TMP, errors="ignore"):
        m = IMPRESSION_RE.search(line)
        if m:
            identity = {"cid": m.group(1), "crid": m.group(2)}
    return identity


def extract_bid_ids(logtext):
    ids = {}
    for key in ("bidobjid", "cid", "crid", "crpid", "oid"):
        m = re.search(key + r"=([A-Za-z0-9_-]+)", logtext)
        if m:
            ids[key] = m.group(1)
    return ids


def collect_environment_ios():
    """iOS 環境快照。key 盡量對齊 Android 版（下游報告卡共用），值改自 ideviceinfo。
    Android 專有欄位（root/battery_saver…）在 iOS 無對應，標 n/a 或 iOS 對應概念。"""
    if not _have("ideviceinfo"):
        note = "ideviceinfo 未安裝（brew install libimobiledevice）— 環境欄位多為空"
    else:
        note = "ideviceinfo (libimobiledevice)"
    tz = ideviceinfo("TimeZone")
    return {
        "platform": "ios",
        "bundle_id": BUNDLE_ID,
        "package": BUNDLE_ID,                       # 對齊下游 key
        "version_name": "—",                         # app 版本待 Phase 2 由 syslog/ipa 補
        "version_code": "—",
        "device": ideviceinfo("ProductType") or ideviceinfo("DeviceName"),
        "device_name": ideviceinfo("DeviceName"),
        "device_kind": "實體機",                      # iOS 這條流程只跑實機
        "os_name": ideviceinfo("ProductName") or "iOS",
        "os_version": ideviceinfo("ProductVersion"),
        "android": "—",                              # 下游模板欄位，iOS 無
        "build_fingerprint": ideviceinfo("BuildVersion"),
        "timezone": tz,
        "dark_mode": "n/a (iOS：需 XCUITest 讀 UITraitCollection，Phase 2)",
        "battery_saver": "n/a (iOS Low Power Mode，Phase 2)",
        "brightness": "n/a (Phase 2)",
        "font_scale": "n/a (iOS Dynamic Type，Phase 2)",
        "locale": ideviceinfo("Locale", domain="com.apple.international")
                  or ideviceinfo("Language", domain="com.apple.international"),
        "media_volume": "n/a (Phase 2)",
        "battery": "n/a (iOS，Phase 2)",
        "battery_source": "n/a",
        "root": "n/a (iOS jailbreak 偵測，Phase 2)",
        "vpn_active": None,
        "env_source": note,
    }


def snapshot_device_state_ios():
    """capture 當下的 iOS 裝置狀態文字（對照 Android snapshot_device_state）。"""
    raw = {
        "device":       ideviceinfo("ProductType"),
        "device_name":  ideviceinfo("DeviceName"),
        "os_version":   ideviceinfo("ProductVersion"),
        "build":        ideviceinfo("BuildVersion"),
        "timezone":     ideviceinfo("TimeZone"),
        "wifi_mac":     ideviceinfo("WiFiAddress"),
        # 以下需 Phase 2 由 XCUITest / 其他工具補（iOS 無直接等價）
        "dark_mode":    "n/a (Phase 2)",
        "battery":      "n/a (Phase 2)",
        "vpn_active":   "n/a (Phase 2)",
        "root":         "n/a (Phase 2)",
    }
    return "\n".join(f"  {k:<14}: {v}" for k, v in raw.items())


# ── evidence bundle（Phase 1：擷取＋摘要，不做 AND-xx 驗證）─────────────────────
def summarize_bid_fields(bid, prefix=""):
    """把 iOS bid 攤平成 dotted-path 清單，供之後建 iOS TC 目錄對照。"""
    rows = []
    if isinstance(bid, dict):
        for k, v in bid.items():
            rows += summarize_bid_fields(v, f"{prefix}{k}.")
    elif isinstance(bid, list):
        if bid:
            rows += summarize_bid_fields(bid[0], f"{prefix}0.")
        else:
            rows.append((prefix.rstrip("."), "[]"))
    else:
        val = str(bid)
        rows.append((prefix.rstrip("."), val[:80] + ("…" if len(val) > 80 else "")))
    return rows


SAVE_STEPS = ["環境快照", "app 截圖", "UI dump", "bid / 流量", "裝置狀態", "syslog", "TC 驗證 + 報告"]


def save_evidence(driver, ts):
    round_dir = resolve_round_dir()
    capture_name = (CAPTURE_LABEL or
                    ("baseline" if TC_ID == "BASELINE" else TC_ID.replace(",", "+")))
    folder = round_dir / f"{capture_name}_{ts}"
    folder.mkdir(parents=True, exist_ok=True)

    progress(1, len(SAVE_STEPS), "存證 · " + SAVE_STEPS[0])
    environment = collect_environment_ios()
    with open(folder / "environment.json", "w") as f:
        json.dump(environment, f, ensure_ascii=False, indent=2)

    # 1. app 畫面截圖（bid 當下）
    progress(2, len(SAVE_STEPS), "存證 · " + SAVE_STEPS[1])
    screenshot_path = str(folder / "phone.png")
    try:
        driver.get_screenshot_as_file(screenshot_path)
        print(f"  screenshot  → {screenshot_path}")
    except Exception as exc:
        print(f"  [warn] screenshot 失敗：{exc}")

    # 1a. 廣告 UI dump（XCUITest page_source）
    progress(3, len(SAVE_STEPS), "存證 · " + SAVE_STEPS[2])
    try:
        ad_ui = driver.page_source
        if ad_ui:
            with open(folder / "ad_ui.xml", "w") as f:
                f.write(ad_ui)
            print(f"  ad_ui       → {folder / 'ad_ui.xml'}")
    except Exception as exc:
        print(f"  [warn] ad_ui dump 失敗：{exc}")

    if STATE_ACTION:
        with open(folder / "state_action.txt", "w") as f:
            f.write(STATE_ACTION + "\n")

    # 2. detector 擷取的 bid / response / 全流量
    progress(4, len(SAVE_STEPS), "存證 · " + SAVE_STEPS[3])
    if os.path.exists(BID_FILE):
        shutil.copy(BID_FILE, folder / "bid_request.json")
        print(f"  bid_request → {folder / 'bid_request.json'}")
    if os.path.exists(FIRST_BID_FILE):
        shutil.copy(FIRST_BID_FILE, folder / "first_bid_request.json")
    if os.path.exists(BID_RESPONSE_FILE):
        shutil.copy(BID_RESPONSE_FILE, folder / "bid_response.json")
        print(f"  bid_response→ {folder / 'bid_response.json'}")
    if os.path.exists(IMPRESSION_FILE):
        shutil.copy(IMPRESSION_FILE, folder / "impression_ids.json")
        print(f"  impression  → {folder / 'impression_ids.json'}"
              " (bid body 因 cert pinning 無法取得時的識別碼備援)")
    if os.path.exists(TRAFFIC_FILE):
        shutil.copy(TRAFFIC_FILE, folder / "traffic.jsonl")
        print(f"  traffic     → {folder / 'traffic.jsonl'}")

    # 3. 裝置狀態
    progress(5, len(SAVE_STEPS), "存證 · " + SAVE_STEPS[4])
    state_path = folder / "device_state.txt"
    with open(state_path, "w") as f:
        f.write(f"Device State (iOS) — TC: {TC_ID} — {ts}\n")
        f.write("=" * 50 + "\n")
        f.write(snapshot_device_state_ios() + "\n")
    print(f"  device_state→ {state_path}")

    # 4. syslog（idevicesyslog 側錄）
    progress(6, len(SAVE_STEPS), "存證 · " + SAVE_STEPS[5])
    stop_syslog()
    if os.path.exists(SYSLOG_TMP):
        shutil.copy(SYSLOG_TMP, folder / "syslog.txt")
        log_txt = open(SYSLOG_TMP, errors="ignore").read()
        appier_lines = [l for l in log_txt.splitlines(keepends=True)
                        if re.search(r"appier|argus|datasignal", l, re.IGNORECASE)]
        with open(folder / "syslog_appier.txt", "w") as f:
            f.writelines(appier_lines)
        print(f"  syslog      → {folder / 'syslog.txt'} (appier-only: {len(appier_lines)} lines)")
        ids = extract_bid_ids(log_txt)
        if ids:
            with open(folder / "bid_ids.json", "w") as f:
                json.dump(ids, f, indent=2)
            print("  bid_ids     → " + ", ".join(f"{k}={v}" for k, v in ids.items()))

    # 5. iOS bid 欄位盤點（校準 IOS_VALIDATORS 用）＋ iOS TC 驗證報告
    progress(7, len(SAVE_STEPS), "存證 · " + SAVE_STEPS[6])
    if os.path.exists(BID_FILE):
        with open(BID_FILE) as f:
            bid = json.load(f)
        import ios_bid_inspector as ibi
        # ext_enc / req_enc 解碼：存明文供人看，盤點也用解碼後結構（不是不透明 blob）
        normalized = ibi.normalize_ios_bid(bid)
        if normalized is not bid:
            with open(folder / "bid_decoded.json", "w") as f:
                json.dump(normalized, f, ensure_ascii=False, indent=2)
            print(f"  bid_decoded → {folder / 'bid_decoded.json'} (ext_enc/req_enc 已解碼)")
        rows = summarize_bid_fields(normalized)
        with open(folder / "ios_bid_summary.txt", "w") as f:
            f.write(f"iOS bid request 欄位盤點（{len(rows)} 個路徑，ext_enc/req_enc 已解碼）\n")
            f.write("=" * 60 + "\n")
            for path, sample in rows:
                f.write(f"{path:<48} = {sample}\n")
        print(f"  bid_summary → {folder / 'ios_bid_summary.txt'} ({len(rows)} 欄位)")

        # iOS TC 驗證（ios_bid_inspector：IOS-xx；run_inspection 內部會自動解碼）
        tc_filter = (ibi.AUTO_TCS if TC_ID == "BASELINE"
                     else set(TC_ID.split(",")))
        results = ibi.run_inspection(bid, tc_filter)
        header = (f"Round: {round_dir.name}  |  Mode: {TEST_MODE}  |  Type: {TEST_TYPE}  |  "
                  f"CID: {TEST_CID}  |  By: {TEST_EXECUTOR}  |  TC: {TC_ID}  |  App: {BUNDLE_ID}")
        from bid_inspector import format_report
        report = format_report(results, str(folder / "bid_request.json"), header)
        with open(folder / "report.txt", "w") as f:
            f.write(report + "\n")
        print(f"  report      → {folder / 'report.txt'}")

        cal_fails = sum(1 for r in results if not r["passed"] and "[待校準]" in r.get("note", ""))
        with open(folder / "results.json", "w") as f:
            json.dump({
                "tc_id": TC_ID, "captured_at": ts, "platform": "ios",
                "app": BUNDLE_ID, "test_type": TEST_TYPE, "test_cid": TEST_CID,
                "test_mode": TEST_MODE, "test_executor": TEST_EXECUTOR,
                "environment": environment,
                "note": ("iOS TC 驗證（ios_bid_inspector）。標 [待校準] 的欄位路徑/期望值"
                         "需對照 ios_bid_summary.txt 修正後再判讀。"),
                "results": results,
            }, f, ensure_ascii=False, indent=2)
        print(f"  results     → {folder / 'results.json'} "
              f"({sum(r['passed'] for r in results)} pass / "
              f"{sum(not r['passed'] for r in results)} fail；其中 {cal_fails} 條疑似待校準)")
        print()
        print(report)

        # round 彙總
        rows2 = ibi.aggregate_round(str(round_dir))
        round_report = ibi.format_round_report(rows2, round_dir.name)
        with open(round_dir / "round_report.txt", "w") as f:
            f.write(round_report + "\n")
        print(f"\n  round report → {round_dir / 'round_report.txt'}")
    elif os.path.exists(IMPRESSION_FILE):
        # bid 端點本身因 cert pinning 看不到內容（2026-07-20 實機確認：Charles/mitmdump
        # 皆無法解密 apx.appier.net；syslog 也沒有 Appier 自訂 subsystem 可撈）。
        # 這種情況下不對空資料硬跑 IOS-xx 驗證（那只會產生一堆誤導性 FAIL）——
        # 老實記錄「中獎了、中的是哪個 creative」，其餘欄位待 bid body 有辦法取得再驗。
        impression_ids = json.load(open(IMPRESSION_FILE))
        note = ("bid request body 因 SDK-level cert pinning 無法取得（Charles/mitmdump "
                "皆看不到 apx.appier.net 內容，syslog 亦無 SDK log）；本次僅能從「已展示」"
                "callback URL 取得識別碼，Signal 欄位（device.ext.* 等）暫無法驗證。"
                "identifiers 見同資料夾 impression_ids.json。")
        with open(folder / "results.json", "w") as f:
            json.dump({
                "tc_id": TC_ID, "captured_at": ts, "platform": "ios",
                "app": BUNDLE_ID, "test_type": TEST_TYPE, "test_cid": TEST_CID,
                "test_mode": TEST_MODE, "test_executor": TEST_EXECUTOR,
                "environment": environment,
                "impression_ids": impression_ids,
                "note": note,
                "results": [],
            }, f, ensure_ascii=False, indent=2)
        print(f"  results     → {folder / 'results.json'} (無 bid body；"
              f"cid={impression_ids.get('cid')} crid={impression_ids.get('crid')} 已記錄)")
        print(f"  [note] {note}")
    else:
        print("  [warn] bid_request.json / impression_ids 都不存在 — 這輪沒擷到任何中獎證據")

    return folder


# ── main ────────────────────────────────────────────────────────────────────
def main():
    if not BUNDLE_ID:
        sys.exit("必填環境變數未設定：\n  export BUNDLE_ID=com.appier.ssp.sample")

    global UDID
    UDID = udid = detect_udid()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    test_type = resolve_test_type()
    test_mode = resolve_test_mode()
    test_cid = resolve_test_cid()

    # 頁籤 + 觸發版位：優先用使用者顯式覆蓋（TAB / TRIGGER_LABEL），否則依
    # TEST_MODE 從實機盤點過的對照表推斷（見 TAB_TRIGGER_LABEL 定義處註解）。
    tab_name = TAB_OVERRIDE or TAB_NAME.get(test_mode, "")
    trigger_label = TRIGGER_LABEL_OVERRIDE or TAB_TRIGGER_LABEL.get(test_mode, "")
    if not trigger_label:
        sys.exit(f"無法判定 TEST_MODE={test_mode!r} 的觸發版位，"
                 "請手動指定 TRIGGER_LABEL（或 AD_LABEL）環境變數。")

    print(f"[device] {udid}")
    print(f"[type  ] {test_type}")
    print(f"[mode  ] {test_mode}")
    print(f"[CID   ] {test_cid or '(none)'}")
    print(f"[by    ] {TEST_EXECUTOR}")
    print(f"[round ] {TEST_ROUND}")
    print(f"[TC    ] {TC_ID}")
    print(f"[app   ] {BUNDLE_ID}")
    print(f"[tab   ] '{tab_name or '(none)'}'")
    print(f"[tap   ] '{trigger_label}'")
    print()

    for f in (FLAG_FILE, BID_FILE, FIRST_BID_FILE, BID_STATUS_FILE,
              BID_RESPONSE_FILE, IMPRESSION_FILE, TRAFFIC_FILE, NETWORK_FILE):
        if os.path.exists(f):
            os.remove(f)

    print("[→] syslog recording ...")
    start_syslog()

    options = XCUITestOptions()
    options.bundle_id = BUNDLE_ID
    options.automation_name = "XCUITest"
    options.no_reset = True
    options.udid = udid
    # 全新裝置/app 重置後第一次要 IDFA 會跳 App Tracking Transparency 系統彈窗；
    # headless 自動化沒人可以點，交給 WDA 自動接受，不必等 dismiss_system_alert()
    # 剛好在對的時間點被呼叫到。
    options.set_capability("autoAcceptAlerts", True)
    if XCODE_ORG_ID:
        options.set_capability("xcodeOrgId", XCODE_ORG_ID)
        options.set_capability("xcodeSigningId", "Apple Development")
        options.set_capability("allowProvisioningDeviceRegistration", True)
        if WDA_BUNDLE_ID:
            options.set_capability("updatedWDABundleId", WDA_BUNDLE_ID)

    print("[→] launching via Appium ...")
    driver = webdriver.Remote(APPIUM_URL, options=options)
    time.sleep(2.0)

    try:
        dismiss_system_alert(driver)   # 保險：launch 當下若已有彈窗，先清掉
        if tab_name:
            print(f"[→] 切到頁籤 '{tab_name}' ...")
            select_tab(driver, tab_name)

        if DWELL_SEC > 0:
            print(f"[→] 前景停留 {DWELL_SEC:.0f}s ...")
            time.sleep(DWELL_SEC)

        attempt = 0
        status = None
        ad_identity = None
        hit = None
        source = None
        while True:
            attempt += 1
            if MAX_AD_ATTEMPTS and attempt > MAX_AD_ATTEMPTS:
                print(f"\n[停止] 已刷 {MAX_AD_ATTEMPTS} 次仍未命中指定 CID：{TEST_CID}")
                return 4

            if attempt > 1:
                stop_syslog()
                for f in (FLAG_FILE, BID_FILE, BID_STATUS_FILE, BID_RESPONSE_FILE, IMPRESSION_FILE):
                    if os.path.exists(f):
                        os.remove(f)
                start_syslog()
                try:
                    driver.back()
                except Exception:
                    pass
                time.sleep(1.2)
                if tab_name:
                    select_tab(driver, tab_name)

            progress(attempt - 1, MAX_AD_ATTEMPTS or attempt,
                     f"刷廣告 attempt {attempt}：tap '{trigger_label}'")
            tapped = False
            for _ in range(3):
                try:
                    driver.find_element("accessibility id", trigger_label).click()
                    tapped = True
                    break
                except Exception:
                    try:
                        driver.back()
                    except Exception:
                        pass
                    time.sleep(0.8)
                    if tab_name:
                        select_tab(driver, tab_name)
            if not tapped:
                print("    [retry] 找不到指定版位，重新啟動 app 後重試。")
                try:
                    driver.activate_app(BUNDLE_ID)
                    time.sleep(1.0)
                    if tab_name:
                        select_tab(driver, tab_name)
                except Exception:
                    pass
                time.sleep(AD_RETRY_DELAY)
                continue

            deadline = time.monotonic() + BID_TIMEOUT
            wait_with_countdown(deadline, lambda: os.path.exists(FLAG_FILE),
                                f"attempt {attempt} 等 bid request")

            if not os.path.exists(FLAG_FILE):
                print("    [retry] 沒偵測到 bid request（detector 未攔到 /v2/sdk/ios/ad）。")
                time.sleep(AD_RETRY_DELAY)
                continue

            hit = open(FLAG_FILE).read().strip()
            time.sleep(1.0)
            status = (open(BID_STATUS_FILE).read().strip()
                      if os.path.exists(BID_STATUS_FILE) else "?")
            source = "proxy"

            if attempt == 1 and os.path.exists(BID_FILE):
                shutil.copy(BID_FILE, FIRST_BID_FILE)

            # bid 端點本身因 cert pinning 看不到內容，SDK 也未見於 syslog（2026-07-20
            # 實機確認：syslog 裡沒有任何 Appier 自訂 subsystem，只有系統框架）；
            # 實際可用的識別碼來源是 detector.py 從「已展示」callback URL 解出的
            # IMPRESSION_FILE。scan_syslog_ad_identity() 保留當作未來備援，目前預期
            # 恆回 None。
            ad_identity = None
            if os.path.exists(IMPRESSION_FILE):
                try:
                    ad_identity = json.load(open(IMPRESSION_FILE))
                except Exception:
                    ad_identity = None
            if ad_identity is None:
                ad_identity = scan_syslog_ad_identity()
            if SAVE_ON_BID and (os.path.exists(BID_FILE) or ad_identity):
                if not ad_identity:
                    ad_identity = {"cid": "(no-win)", "crid": "(no-win)"}
                print(f"    [SAVE_ON_BID] bid request 已取得（response={status}），入庫。")
                break
            if status != "200":
                print(f"    [retry] response={status}，未命中廣告。")
            elif TEST_CID and (not ad_identity or ad_identity.get("cid") != TEST_CID):
                got = ad_identity.get("cid") if ad_identity else "(unknown)"
                print(f"    [retry] CID 不符：expected={TEST_CID}, actual={got}")
            else:
                break
            time.sleep(AD_RETRY_DELAY)

        cid_disp = ad_identity.get("cid") if ad_identity else "(unknown)"
        crid_disp = ad_identity.get("crid") if ad_identity else "(unknown)"
        print(f"\n[CAPTURED via {source}] {hit}  (response: {status}, "
              f"cid={cid_disp}, crid={crid_disp})\n")
        if status == "204":
            print("[判定] server 回 204 no-bid — 連線正常，目前沒有廣告可刷；"
                  "bid request 仍已留存。\n")

        print("[→] saving evidence ...")
        folder = save_evidence(driver, ts)
        print(f"\n[DONE] {folder}/")
        return 3 if status == "204" else 0

    finally:
        stop_syslog()
        try:
            driver.quit()
        except Exception as exc:
            print(f"[warn] driver.quit() 失敗（不影響已存證據）：{exc}")


if __name__ == "__main__":
    sys.exit(main() or 0)
