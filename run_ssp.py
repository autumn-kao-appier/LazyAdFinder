#!/usr/bin/env python3
"""
run_ssp.py — SSP SDK bid capture + evidence collector (Native)

Usage:
    python run_ssp.py [TC_ID] [UDID]

    TC_ID: e.g. AND-04, or comma-separated AND-04,AND-06.
           Omit (or BASELINE) = 一次 capture 驗全部 checks，資料夾名 baseline_<ts>。
    UDID:  auto-detected when only one device connected

Evidence layout（按 test round 分）:
    evidence/<TEST_ROUND>_<YYYYMMDD_HHMMSS>/     round 資料夾，首次 capture 時建立，
        round_report.txt                         同 round 標籤後續自動歸入同一夾
        baseline_<ts>/                           每個 capture 一個子資料夾
        AND-04_<ts>/
    round_report.txt 每次 capture 後自動重算（每條 check 取最新結果）；
    也可手動重算：python bid_inspector.py --round evidence/<round folder>

Env vars (required):
    APP_PACKAGE     e.g. com.appier.android.sample
    APP_ACTIVITY    e.g. com.appier.android.sample.MainActivity

Env vars (optional):
    TEST_ROUND      TC 表上的 round 標籤（e.g. R1），預設 adhoc
    TEST_TYPE       aibid / reen-static / reen-dynamic（未設定時互動詢問）
    TEST_MODE       standalone / admob-mediation / applovin-mediation（未設定時互動詢問）
    TEST_CID        測試用 CID（未設定時互動詢問）
    TRIGGER_TEXT    UI element text to tap to fire bid (leave unset if app auto-loads)
    DO_PRIVACY_CLICK  1 = capture 後自動點 privacy icon（TC-11；走 adpolicy，免點擊費用）
    DO_E2E_FLOW     1 = 點擊真實廣告並驗 landing（baseline 預設開啟）

Three terminals:
    T1: mitmdump -s ~/LazyAdFinder/detector.py --listen-port 8081
    T2: appium
    T3: python ~/LazyAdFinder/run_ssp.py AND-04
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

sys.path.insert(0, str(Path(__file__).parent))

from appium import webdriver
from appium.options.android.uiautomator2.base import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy

FLAG_FILE    = "/tmp/appier_hit"
BID_FILE     = "/tmp/appier_bid.json"
FIRST_BID_FILE = "/tmp/appier_first_bid.json"
BID_STATUS_FILE   = "/tmp/appier_bid_status"
BID_RESPONSE_FILE = "/tmp/appier_bid_response.json"
TRAFFIC_FILE = "/tmp/appier_traffic.jsonl"  # detector 的全流量 log（E2E 驗證用）
NETWORK_FILE      = "/tmp/current_networks"
LOGCAT_TMP   = "/tmp/appier_logcat.txt"

LOGCAT_PROC = None
APPIUM_URL   = "http://127.0.0.1:4723"
BID_TIMEOUT  = 12.0
EVIDENCE_DIR = Path(os.environ.get("EVIDENCE_DIR", Path(__file__).parent / "evidence"))

APP_PACKAGE  = os.environ.get("APP_PACKAGE")
APP_ACTIVITY = os.environ.get("APP_ACTIVITY")
TRIGGER_TEXT = os.environ.get("TRIGGER_TEXT", "Native - basic format")
TEST_ROUND   = os.environ.get("TEST_ROUND", "adhoc")
VALID_TYPES  = ("aibid", "reen-static", "reen-dynamic")
VALID_MODES  = ("standalone", "admob-mediation", "applovin-mediation")
TEST_TYPE    = os.environ.get("TEST_TYPE", "").strip().lower()  # 這輪測什麼
TEST_MODE    = os.environ.get("TEST_MODE", "").strip().lower()  # SDK 整合模式
TEST_CID     = os.environ.get("TEST_CID", "").strip()
TEST_EXECUTOR = os.environ.get("TEST_EXECUTOR", "").strip() or getpass.getuser()

MODE_TAB = {
    "standalone": "Appier SDK",
    "admob-mediation": "AdMob Mediation",
    "applovin-mediation": "AppLovin Mediation",
}


def find_onscreen_text(driver, text):
    """Find a visible element by text, ignoring case and off-screen ViewPager pages."""
    width = driver.get_window_size()["width"]
    elements = driver.find_elements(
        AppiumBy.ANDROID_UIAUTOMATOR,
        f'new UiSelector().textMatches("(?i){re.escape(text)}")',
    )
    for element in elements:
        location = element.location
        size = element.size
        center_x = location["x"] + size["width"] // 2
        if 0 <= center_x < width:
            return element
    return None


def select_test_mode_tab(driver):
    """Switch the sample app to the tab represented by TEST_MODE."""
    tab_name = MODE_TAB[TEST_MODE]
    for attempt in range(1, 5):
        tab = find_onscreen_text(driver, tab_name)
        if tab is not None:
            tab.click()
            time.sleep(0.8)
            if find_onscreen_text(driver, TRIGGER_TEXT) is not None:
                print(f"[tab   ] {tab_name}")
                return
        if attempt < 4:
            driver.back()
            time.sleep(0.8)
    raise RuntimeError(
        f"無法切換到 {tab_name} 或找不到版位 '{TRIGGER_TEXT}'；"
        "請確認 sample app 版本及 TRIGGER_TEXT。"
    )


def tap_trigger(driver):
    """Tap only the trigger belonging to the currently visible mode tab."""
    trigger = find_onscreen_text(driver, TRIGGER_TEXT)
    if trigger is None:
        return False
    trigger.click()
    return True


def resolve_test_type():
    """依序詢問 AIBID/REEN；REEN 再詢問 Static/Dynamic。"""
    global TEST_TYPE
    if TEST_TYPE in VALID_TYPES:
        return TEST_TYPE
    if TEST_TYPE:
        print(f"[warn] TEST_TYPE='{TEST_TYPE}' 非法，應為 {VALID_TYPES}")
    if not sys.stdin.isatty():
        sys.exit("非互動執行必須設定 TEST_TYPE=aibid|reen-static|reen-dynamic")

    while True:
        goal = input("整個流程的目標是？ 1) AIBID  2) REEN: ").strip().lower()
        goal = {"1": "aibid", "2": "reen"}.get(goal, goal)
        if goal == "aibid":
            TEST_TYPE = "aibid"
            break
        if goal == "reen":
            while True:
                creative = input("REEN 現在測的是？ 1) Static  2) Dynamic: ").strip().lower()
                creative = {"1": "static", "2": "dynamic"}.get(creative, creative)
                if creative in ("static", "dynamic"):
                    TEST_TYPE = f"reen-{creative}"
                    break
                print("請輸入 1/2、Static 或 Dynamic。")
            break
        print("請輸入 1/2、AIBID 或 REEN。")
    return TEST_TYPE


def resolve_test_mode():
    """取得 SDK 整合模式；與 AIBID/REEN 投放目的為獨立維度。"""
    global TEST_MODE
    aliases = {
        "1": "standalone", "2": "admob-mediation", "3": "applovin-mediation",
        "admob": "admob-mediation", "applovin": "applovin-mediation",
        "mediation": "admob-mediation",
    }
    TEST_MODE = aliases.get(TEST_MODE, TEST_MODE)
    if TEST_MODE in VALID_MODES:
        return TEST_MODE
    if TEST_MODE:
        print(f"[warn] TEST_MODE='{TEST_MODE}' 非法，應為 {VALID_MODES}")
    if not sys.stdin.isatty():
        sys.exit("非互動執行必須設定 TEST_MODE=standalone|admob-mediation|applovin-mediation")
    while True:
        value = input(
            "SDK 整合模式是？ 1) Standalone  2) AdMob Mediation  "
            "3) AppLovin Mediation: "
        ).strip().lower()
        value = aliases.get(value, value)
        if value in VALID_MODES:
            TEST_MODE = value
            return TEST_MODE
        print("請輸入 1/2/3、Standalone、AdMob 或 AppLovin。")


def resolve_test_cid():
    """取得本輪測試 CID；互動模式必問，非互動模式要求 TEST_CID。"""
    global TEST_CID
    if TEST_CID:
        return TEST_CID
    if not sys.stdin.isatty():
        sys.exit("非互動執行必須設定 TEST_CID")
    while not TEST_CID:
        TEST_CID = input("你的測試用 CID 是什麼？ ").strip()
        if not TEST_CID:
            print("CID 不可空白。")
    return TEST_CID
STATE_ACTION = os.environ.get("STATE_ACTION")       # 本次實際做了什麼（實機/模擬）
CAPTURE_LABEL = os.environ.get("CAPTURE_LABEL", "").strip()
DO_FGBG      = os.environ.get("DO_FGBG", "0") == "1"
DWELL_SEC    = float(os.environ.get("DWELL_SEC", "0"))  # 觸發廣告前先前景停留秒數
AD_RETRY_DELAY = float(os.environ.get("AD_RETRY_DELAY", "2"))
MAX_AD_ATTEMPTS = int(os.environ.get("MAX_AD_ATTEMPTS", "0"))  # 0 = retry without limit
# SAVE_ON_BID=1：偵測到 bid request 即入庫，不要求 200/CID 命中。
# 用於只驗 request payload 的 TC（如 AND-12 emulator / AND-10 非 root），
# 這類環境（模擬器新 GAID、opt-out）REEN campaign 本來就不出價
SAVE_ON_BID = os.environ.get("SAVE_ON_BID", "0") == "1"

# SESSION_CASE=1/2/3：user.session_duration 三情境（AND-47-1/2/3）。
# session_duration＝使用者 App 在前景的累積時間（毫秒），不是廣告 session 載入時間。
# 流程：命中 bid A → 情境動作 → 再觸發 bid B → 對照寫 session_case.json。
#   1 = 只關廣告頁（App 全程前景）→ 預期累進（B > A）
#   2 = force-stop 關整個 App 重開   → 預期重置（B < A）
#   3 = 退背景數秒再切回前景         → 預期累進（B > A）
SESSION_CASE = os.environ.get("SESSION_CASE", "").strip()
SESSION_GAP_SEC = float(os.environ.get("SESSION_GAP_SEC", "8"))  # 動作後累積前景秒數
SESSION_CASE_FILE  = "/tmp/appier_session_case.json"
SESSION_BID_A_FILE = "/tmp/appier_session_bid_a.json"
SESSION_LOGCAT_A   = "/tmp/appier_session_logcat_a.txt"

TC_ID = sys.argv[1] if len(sys.argv) > 1 else "BASELINE"
if SESSION_CASE and TC_ID == "BASELINE":
    TC_ID = f"AND-47-{SESSION_CASE}"   # 直接跑（不經 wizard）時自動掛對 TC

# E2E 完整流程（點擊 + landing）與 privacy icon 點擊：BASELINE 一律開（baseline 本來就
# 該跑完整 E2E 生命週期），狀態類 TC 預設關；皆可用環境變數覆蓋。需在 TC_ID 決定後才判斷。
DO_PRIVACY_CLICK = os.environ.get("DO_PRIVACY_CLICK", "1" if TC_ID == "BASELINE" else "0") == "1"
DO_E2E_FLOW = os.environ.get("DO_E2E_FLOW", "1" if TC_ID == "BASELINE" else "0") == "1"
# argv 優先；未帶時吃 UDID 環境變數（wizard 就是用 env 傳；多裝置在線時必要）
UDID  = sys.argv[2] if len(sys.argv) > 2 else (os.environ.get("UDID", "").strip() or None)


def resolve_round_dir():
    """同 round 標籤重複執行時歸入既有資料夾；沒有才用當下時間戳開新的。"""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    safe_cid = re.sub(r"[^A-Za-z0-9_-]+", "-", TEST_CID).strip("-")
    type_label = TEST_TYPE.upper().replace("-", "_")
    mode_label = TEST_MODE.upper().replace("-", "_")
    prefix = f"{mode_label}_{type_label}_CID_{safe_cid}_{TEST_ROUND}"
    existing = sorted(d for d in EVIDENCE_DIR.glob(f"{prefix}_*") if d.is_dir())
    if existing:
        return existing[-1]
    return EVIDENCE_DIR / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


# ── adb helpers ───────────────────────────────────────────────────────────────

def adb(*args):
    cmd = ["adb"]
    if UDID:
        cmd += ["-s", UDID]
    cmd += list(args)
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception as e:
        return f"[err: {e}]"


def detect_udid():
    if UDID:
        return UDID
    out = subprocess.check_output(["adb", "devices"], text=True)
    devices = [
        line.split()[0]
        for line in out.splitlines()
        if line.strip() and not line.startswith("List") and line.split()[-1] == "device"
    ]
    if not devices:
        sys.exit("No Android device found. Connect device or start emulator.")
    if len(devices) > 1:
        sys.exit(f"Multiple devices: {devices}\nSpecify: python run_ssp.py {TC_ID} <UDID>")
    return devices[0]


# ── logcat capture (session-concurrent) ───────────────────────────────────────

def start_logcat():
    """從 app 啟動前開始錄 logcat，bid capture 時整段收進 evidence。"""
    global LOGCAT_PROC
    adb("logcat", "-c")
    cmd = ["adb"]
    if UDID:
        cmd += ["-s", UDID]
    cmd += ["logcat", "-v", "time"]
    out = open(LOGCAT_TMP, "w")
    LOGCAT_PROC = subprocess.Popen(cmd, stdout=out, stderr=subprocess.DEVNULL)


def stop_logcat():
    global LOGCAT_PROC
    if LOGCAT_PROC is not None:
        LOGCAT_PROC.terminate()
        try:
            LOGCAT_PROC.wait(timeout=3)
        except subprocess.TimeoutExpired:
            LOGCAT_PROC.kill()
        LOGCAT_PROC = None


# SDK logs the full bid body + result to logcat, so field validation needs no
# proxy. 兩種格式都吃：舊 [AdRequestJSON] {...}；新 [Appier SDK] Ad request body: {...}
ADREQ_RE = re.compile(r"(?:\[AdRequestJSON\]|Ad request body:)\s*(\{.*\})\s*$")
LOADED_RE = re.compile(r"onAdLoaded\(\)")
NOBID_RE = re.compile(r"onAdNoBid\(\)")
LOADFAIL_RE = re.compile(r"onAdLoadFail\(\)")
IMPRESSION_RE = re.compile(r"Requesting impression tracker:.*?[?&]cid=([^&\s]+).*?[&]crid=([^&\s]+)")


def scan_logcat_bid():
    """從側錄的 logcat 抓最後一筆 bid body + 結果狀態。

    回傳 (bid_dict, status) — status 200=onAdLoaded / 204=onAdNoBid / None=未定。
    無 bid body 時回 (None, None)。純靠 SDK log，不需要 proxy/TLS 攔截。
    """
    if not os.path.exists(LOGCAT_TMP):
        return None, None
    bid = None
    status = None
    for line in open(LOGCAT_TMP, errors="ignore"):
        m = ADREQ_RE.search(line)
        if m:
            try:
                bid = json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        elif LOADED_RE.search(line):
            status = "200"
        elif NOBID_RE.search(line):
            status = "204"
        elif LOADFAIL_RE.search(line):
            status = "loadfail"
    return bid, status


def scan_logcat_ad_identity():
    """Return the identity of the ad that actually loaded, not merely requested."""
    if not os.path.exists(LOGCAT_TMP):
        return None
    identity = None
    for line in open(LOGCAT_TMP, errors="ignore"):
        match = IMPRESSION_RE.search(line)
        if match:
            identity = {"cid": match.group(1), "crid": match.group(2)}
    return identity


# ── no-ad diagnosis ───────────────────────────────────────────────────────────
# 刷不到廣告時分辨：沒廣告可刷（no-bid）vs 連線鏈路哪一段有問題。

NET_ERR_RE = re.compile(
    r"SSLHandshakeException|CertPathValidatorException|UnknownHostException|"
    r"ConnectException|SocketTimeoutException|Failed to connect|ERR_PROXY|"
    r"NO_FILL|network error",
    re.IGNORECASE,
)


def _mac_port_listening(port):
    """Mac 本機是否有人在聽該 port（Charles 8888 / mitmdump 8081）。None=無法判定。"""
    try:
        out = subprocess.run(
            ["/usr/sbin/lsof", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
        return bool(out.stdout.strip())
    except Exception:
        return None


def diagnose_no_ad():
    """Timeout 後解析 logcat + proxy 鏈路，印出「沒廣告」或「哪段連線斷了」的判定。"""
    print("\n[診斷] 解析刷不到廣告的原因 ...")

    log_txt = ""
    if os.path.exists(LOGCAT_TMP):
        log_txt = open(LOGCAT_TMP, errors="ignore").read()
    sdk_active = bool(re.search(r"appier", log_txt, re.IGNORECASE))
    bid_sent = bool(ADREQ_RE.search(log_txt))
    nobid = bool(NOBID_RE.search(log_txt))
    proxy_status = (open(BID_STATUS_FILE).read().strip()
                    if os.path.exists(BID_STATUS_FILE) else None)
    net_err = NET_ERR_RE.search(log_txt)

    # proxy 鏈路狀態
    phone_proxy = adb("shell", "settings", "get", "global", "http_proxy")
    charles_up = _mac_port_listening(8888)
    mitm_up = _mac_port_listening(8081)
    traffic_age = None
    if os.path.exists(NETWORK_FILE):
        traffic_age = time.time() - os.path.getmtime(NETWORK_FILE)

    def yn(v):
        return "?" if v is None else ("yes" if v else "NO")

    print(f"  SDK 有動靜 (logcat appier)   : {yn(sdk_active)}")
    print(f"  SDK 有送 bid (AdRequestJSON) : {yn(bid_sent)}")
    print(f"  proxy 看到 bid response      : {proxy_status or '(無)'}")
    print(f"  手機 http_proxy              : {phone_proxy or '(未設)'}")
    print(f"  Charles 在聽 8888            : {yn(charles_up)}")
    print(f"  mitmdump 在聽 8081           : {yn(mitm_up)}")
    if traffic_age is not None:
        print(f"  proxy 最近看到 ad 流量       : {traffic_age:.0f}s 前")

    # 判定（由具體到廣泛）
    if proxy_status == "204" or nobid:
        print("\n[判定] 沒有廣告可刷 — bid 有送出、server 回 204 no-bid，連線正常。")
        print("       是 campaign / fill 的問題，不是環境問題（等有量再刷、或換 zone）。")
    elif bid_sent or proxy_status:
        print("\n[判定] bid 已送出但 response 沒回來 — server 端或 TLS 中斷，"
              "檢查 mitmdump terminal 有無錯誤。")
    elif net_err:
        print(f"\n[判定] 連線問題 — device 端網路錯誤：{net_err.group(0)}")
        print(f"       logcat: {next(l for l in log_txt.splitlines() if net_err.group(0) in l).strip()[:160]}")
        print("       常見原因：手機沒裝/沒信任 Charles CA、proxy IP 過期、Wi-Fi 換網段。")
    elif not sdk_active:
        print("\n[判定] app 根本沒觸發廣告 — 不是連線問題。")
        print("       檢查：TRIGGER_TEXT 有沒有點到、Appier SDK log verbose 是否開啟、app 是否正確版本。")
    else:
        # SDK 有動但沒送 bid、proxy 也沒看到
        broken = []
        if phone_proxy in (None, "", "null") :
            broken.append("手機 http_proxy 未設")
        if charles_up is False:
            broken.append("Charles(8888) 沒開")
        if mitm_up is False:
            broken.append("mitmdump(8081) 沒開")
        if broken:
            print(f"\n[判定] 連線鏈路斷在：{'、'.join(broken)}")
        else:
            print("\n[判定] SDK 有載入但沒發 bid — 多半是 ad placement 沒觸發成功"
                  "（TRIGGER_TEXT 點錯頁）或 SDK 內部擋下（見 logcat_appier）。")


def extract_bid_ids(logtext):
    """從 logcat 的 impression/click tracker URL 解出本次 bid 的識別碼。
    比「每次都差不多的廣告截圖」有意義——bidobjid 唯一標識這次 bid/曝光。"""
    ids = {}
    for key in ("bidobjid", "cid", "crid", "crpid", "oid"):
        m = re.search(key + r"=([A-Za-z0-9_-]+)", logtext)
        if m:
            ids[key] = m.group(1)
    return ids


def _volume_music():
    """STREAM_MUSIC 音量 / 最大值。`media volume` 在部分機型不存在，改解 dumpsys audio。"""
    out = adb("shell", "cmd", "media_session", "volume", "--stream", "3", "--get")
    m = re.search(r"volume is\s+(\d+)\s+in range\s+\[(\d+)\.\.(\d+)\]", out, re.I)
    if m:
        return f"{m.group(1)}/{m.group(3)}"
    dump = adb("shell", "dumpsys", "audio")
    m = re.search(r"STREAM_MUSIC:.*?\n(?:.*\n)*?.*?Muted:", dump)
    seg = m.group(0) if m else dump
    cur = re.search(r"[Ss]treamVolume:\s*(\d+)", seg)
    mx = re.search(r"[Mm]ax(?:imum)?:\s*(\d+)", seg)
    if cur:
        return f"{cur.group(1)}" + (f"/{mx.group(1)}" if mx else "")
    return "(unavailable)"


def detect_root():
    """偵測裝置實際 root 狀態（ground truth），供對照 SDK 回報的 device.ext.jailbreak。

    不硬編「這台是不是 root」——每次 capture 實際查 Magisk / su binary，
    回傳 (is_rooted: bool|None, detail: str)。None = 查不出來。
    """
    signals = []
    pkgs = adb("shell", "pm", "list", "packages")
    if "[err" in pkgs or not pkgs.strip():
        return None, "無法判定 root（adb 查詢失敗：裝置未連或無授權）"
    if "topjohnwu.magisk" in pkgs:
        signals.append("Magisk app")
    for p in ("/system_ext/bin/su", "/sbin/su", "/system/bin/su", "/system/xbin/su"):
        ls = adb("shell", "ls", "-l", p)
        if "No such file" not in ls and "[err" not in ls and "Permission denied" not in ls:
            signals.append(f"su@{p}" + (" → magisk" if "magisk" in ls else ""))
    build_type = adb("shell", "getprop", "ro.build.type")
    debuggable = adb("shell", "getprop", "ro.debuggable")
    if signals:
        return True, "rooted (" + ", ".join(signals) + ")"
    if build_type == "userdebug" or debuggable == "1":
        return True, f"likely rooted (build.type={build_type}, debuggable={debuggable})"
    return False, f"not rooted (build.type={build_type}, no su/Magisk)"


# ── device state snapshot ─────────────────────────────────────────────────────

def snapshot_device_state():
    """Capture device state at moment of bid. Returns formatted string."""
    connectivity = adb("shell", "dumpsys", "connectivity")
    links = adb("shell", "ip", "link")
    vpn_active = bool(re.search(r"TRANSPORT_VPN|type:\s*VPN", connectivity, re.I) or
                      re.search(r"\b(tun\d+|ppp\d+|wg\d+|tailscale\d*)\b", links, re.I))
    raw = {
        "dark_mode":         adb("shell", "cmd", "uimode", "night"),
        "battery":           adb("shell", "dumpsys", "battery"),
        "battery_saver":     adb("shell", "settings", "get", "global", "low_power"),
        "screen_brightness": adb("shell", "settings", "get", "system", "screen_brightness"),
        "font_scale":        adb("shell", "settings", "get", "system", "font_scale"),
        "timezone":          adb("shell", "getprop", "persist.sys.timezone"),
        "volume_music":      _volume_music(),
        "locale":            adb("shell", "getprop", "persist.sys.locale"),
        "os_version":        adb("shell", "getprop", "ro.build.version.release"),
        "model":             adb("shell", "getprop", "ro.product.model"),
        "actual_root":       detect_root()[1],   # ground truth，對照 bid 的 device.ext.jailbreak
        "vpn_active":        vpn_active,
    }

    # foreground activity + 受測 app 版本
    focus = adb("shell", "dumpsys", "window")
    m = re.search(r"mCurrentFocus=.*", focus)
    raw["foreground"] = m.group(0) if m else "(unknown)"
    pkg_dump = adb("shell", "dumpsys", "package", APP_PACKAGE)
    m = re.search(r"versionName=\S+", pkg_dump)
    raw["app_version"] = m.group(0) if m else "(unknown)"

    battery_is_mocked = "UPDATES STOPPED" in raw["battery"]
    raw["battery_source"] = "MOCKED (Android Battery Service updates stopped)" if battery_is_mocked else "REAL"
    raw["battery_raw"] = raw["battery"]
    # summarise battery dump to relevant lines
    batt_lines = [
        l.strip() for l in raw["battery"].splitlines()
        if any(k in l for k in ("level", "AC powered", "USB powered", "status:"))
    ]
    raw["battery"] = " | ".join(batt_lines)

    lines = [f"  {k:<22}: {v}" for k, v in raw.items()]
    return "\n".join(lines)


def detect_device_kind():
    """實體機 or 模擬機（ground truth，來自 adb prop，不靠 SDK 的 device.ext.emulator）。

    AVD 判定訊號：qemu prop、goldfish/ranchu hardware、sdk_gphone 型號、
    fingerprint 含 emu/generic/sdk。任一命中即模擬機。
    """
    qemu = (adb("shell", "getprop", "ro.kernel.qemu") == "1"
            or adb("shell", "getprop", "ro.boot.qemu") == "1")
    hardware = adb("shell", "getprop", "ro.hardware").lower()
    model = adb("shell", "getprop", "ro.product.model").lower()
    fp = adb("shell", "getprop", "ro.build.fingerprint").lower()
    is_emu = (qemu
              or hardware in ("goldfish", "ranchu")
              or "sdk_gphone" in model
              or any(tok in fp for tok in ("emu", "sdk_gphone", "/generic")))
    return "模擬機" if is_emu else "實體機"


def collect_environment():
    """讀取已安裝 APK 與 Capture 當下環境，供 report 頂端稽核卡使用。"""
    pkg = adb("shell", "dumpsys", "package", APP_PACKAGE)
    def match(pattern, default="—"):
        m = re.search(pattern, pkg)
        return m.group(1) if m else default
    battery = adb("shell", "dumpsys", "battery")
    connectivity = adb("shell", "dumpsys", "connectivity")
    links = adb("shell", "ip", "link")
    ipv6_addrs = adb("shell", "ip", "-6", "addr", "show", "scope", "global")
    ipv6_match = re.search(r"inet6\s+([0-9a-f:]+)/\d+", ipv6_addrs, re.I)
    vpn_active = bool(re.search(r"TRANSPORT_VPN|type:\s*VPN", connectivity, re.I) or
                      re.search(r"\b(tun\d+|ppp\d+|wg\d+|tailscale\d*)\b", links, re.I))
    # 定位權限 ground truth：抓 bid 當下 app 實際的 runtime 權限，供報告 gate AND-45/46
    # （geo 是唯一先前沒存 ground-truth 的狀態）。fine 或 coarse 任一 granted 即視為允許。
    fine_m = re.search(r"ACCESS_FINE_LOCATION:\s*granted=(true|false)", pkg)
    coarse_m = re.search(r"ACCESS_COARSE_LOCATION:\s*granted=(true|false)", pkg)
    loc_granted = ((fine_m and fine_m.group(1) == "true")
                   or (coarse_m and coarse_m.group(1) == "true"))
    return {
        "package": APP_PACKAGE,
        "location_permission": "granted" if loc_granted else "denied",
        "location_fine": fine_m.group(1) if fine_m else "—",
        "location_coarse": coarse_m.group(1) if coarse_m else "—",
        "location_source": "dumpsys package runtime permissions",
        "version_name": match(r"versionName=([^\s]+)"),
        "version_code": match(r"versionCode=(\d+)"),
        "first_install_time": match(r"firstInstallTime=([^\n]+)"),
        "device": adb("shell", "getprop", "ro.product.model"),
        "device_kind": detect_device_kind(),
        "android": adb("shell", "getprop", "ro.build.version.release"),
        "build_fingerprint": adb("shell", "getprop", "ro.build.fingerprint"),
        "timezone": adb("shell", "getprop", "persist.sys.timezone"),
        "dark_mode": adb("shell", "cmd", "uimode", "night"),
        "battery_saver": adb("shell", "settings", "get", "global", "low_power"),
        "brightness": adb("shell", "settings", "get", "system", "screen_brightness"),
        "font_scale": adb("shell", "settings", "get", "system", "font_scale"),
        "locale": adb("shell", "getprop", "persist.sys.locale"),
        "app_locale": adb("shell", "cmd", "locale", "get-app-locales", APP_PACKAGE, "--user", "0"),
        "media_volume": _volume_music(),
        "battery": " | ".join(l.strip() for l in battery.splitlines()
                               if any(k in l for k in ("level", "powered", "status:"))),
        "battery_source": ("MOCKED" if "UPDATES STOPPED" in battery else "REAL"),
        "battery_raw": battery,
        "root": detect_root()[1],
        "vpn_active": vpn_active,
        "vpn_source": "dumpsys connectivity + ip link",
        "public_ipv6": ipv6_match.group(1) if ipv6_match else None,
        "ipv6_source": "ip -6 addr show scope global",
    }


# ── state-proof screenshot ──────────────────────────────────────────────────
# 狀態證據：把「看得見該狀態的畫面」叫出來截圖，讓人肉眼驗證，不是拍廣告頁。

# 單一 state TC → 互斥組
STATE_GROUP = {
    "AND-01": "tracking", "AND-02": "tracking", "AND-75": "tracking", "AND-76": "tracking",
    "AND-04": "darkmode", "AND-05": "darkmode",
    "AND-06": "charging", "AND-07": "charging",
    "AND-08": "batterysaver", "AND-09": "batterysaver",
    "AND-10": "jailbreak", "AND-11": "jailbreak",
    "AND-12": "emulator", "AND-13": "emulator",
    "AND-14": "vpn", "AND-15": "vpn",
    "AND-16": "batterylevel", "AND-17": "batterylevel",
    "AND-19": "screenbright", "AND-20": "screenbright",
    "AND-21": "fontscale", "AND-22": "fontscale",
    "AND-23": "volume", "AND-24": "volume",
    "AND-25": "tz", "AND-26": "tz", "AND-27": "tz",
    "AND-31": "locale",
    "AND-45": "geo", "AND-46": "geo",
    "AND-47-1": "session", "AND-47-2": "session", "AND-47-3": "session",
    "AND-48": "session", "AND-52": "session", "AND-50": "fgbg",
    # 裝置固有欄位：系統畫面看得到值 → 一頁涵蓋多條，讓實機每條都盡量有截圖
    "AND-36": "deviceinfo", "AND-37": "deviceinfo", "AND-69": "deviceinfo",
    "AND-70": "deviceinfo", "AND-71": "deviceinfo",
    "AND-30": "language", "AND-73": "language", "AND-74": "language", "AND-32": "language",
    "AND-55": "storage", "AND-56": "storage", "AND-53": "storage", "AND-54": "storage",
    "AND-62": "apps",
    "AND-40": "network", "AND-38": "network",
    "AND-33": "appinfo", "AND-66": "appinfo",
}

# 組 → (kind, arg, 說明該截圖證明什麼)
#   intent    : am start -a <arg>
#   component : am start -n <pkg/activity>（指定 activity，用於沒有 action 的頁）
#   app       : 啟動某 app（monkey launcher）— 例：Magisk 當 root 佐證
#   appdetails: App 詳情頁（權限 / 廣告 ID）
#   qs        : 下拉快捷面板（亮度滑桿）
#   volpanel  : 只顯示媒體音量面板，不改變音量
#   notif     : 展開狀態列（充電 / VPN / 電量圖示）
#   None      : 截圖無法證明，改寫說明檔
STATE_SURFACE = {
    "darkmode":     ("intent", "android.settings.DISPLAY_SETTINGS", "Display 設定的 Dark theme 開關"),
    "batterysaver": ("intent", "android.settings.BATTERY_SAVER_SETTINGS", "省電模式開關狀態"),
    "batterylevel": ("intent", "android.intent.action.POWER_USAGE_SUMMARY", "電池頁面電量百分比"),
    "charging":     ("intent", "android.intent.action.POWER_USAGE_SUMMARY", "電池頁面充電狀態"),
    "tz":           ("intent", "android.settings.DATE_SETTINGS", "日期時間設定的時區（GMT offset）"),
    "locale":       ("applocale", None, "Sample App 的 App language 設定頁"),
    "geo":          ("appdetails", None, "Sample App 詳情頁的 Permissions 摘要（Location 允許/拒絕）"),
    "vpn":          ("intent", "android.settings.VPN_SETTINGS", "VPN 連線狀態"),
    "fontscale":    ("intent", "android.settings.TEXT_READING_SETTINGS", "Display size and text 頁的 Font size 滑桿"),
    "volume":       ("volpanel", None, "媒體音量面板（僅顯示，不改變 Capture 值）"),
    "screenbright": ("qs", None, "快捷面板亮度滑桿位置"),
    # 完整廣告 ID 頁（Reset/Delete/Get new advertising ID）；GMS 的 ADS_PRIVACY action 只開精簡頁
    "tracking":     ("component", "com.google.android.gms/.adsidentity.settings.AdsIdentitySettingsActivity",
                     "系統廣告 ID 頁：opt-in 顯示 ID+Reset/Delete、opt-out 顯示 Get new advertising ID"),
    # root 機用 Magisk 畫面當佐證（su binary 由 Magisk 提供）
    "jailbreak":    ("app", "com.topjohnwu.magisk", "Magisk app 畫面（root 佐證：版本 / package）"),
    "emulator":     (None, None, "實機 / AVD 需外部佐證（截圖不足以證明）"),
    "session":      (None, None, "session 時長無對應設定頁，靠 bid 值 + 操作時序佐證"),
    "fgbg":         (None, None, "前景/背景切換靠操作時序佐證"),
    # 裝置固有欄位：一頁涵蓋多條，讓實機每條都盡量有截圖
    "deviceinfo":   ("intent", "android.settings.DEVICE_INFO_SETTINGS",
                     "關於手機：品牌 Google / 型號 Pixel 10a / Android 版本 16"),
    "language":     ("intent", "android.settings.LOCALE_SETTINGS",
                     "語言與地區：語言 en、地區 tw、locale en_US"),
    "storage":      ("intent", "android.settings.INTERNAL_STORAGE_SETTINGS",
                     "儲存空間：總容量 / 可用空間（RAM 情境）"),
    "apps":         ("intent", "android.settings.MANAGE_APPLICATIONS",
                     "已安裝應用程式清單（applist 對照）"),
    "network":      ("intent", "android.settings.WIFI_SETTINGS",
                     "連線類型：Wi-Fi（conntype；IP 情境）"),
    "appinfo":      ("appdetails", None, "Sample App 詳情頁：app 版本 1.4.0 / 套件名"),
}


def adb_screencap(path):
    cmd = ["adb"]
    if UDID:
        cmd += ["-s", UDID]
    cmd += ["exec-out", "screencap", "-p"]
    try:
        with open(path, "wb") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.DEVNULL, timeout=15, check=True)
        return os.path.getsize(path) > 0
    except Exception:
        return False


def capture_state_proof(folder):
    """為本次 TC 擷取所有對應狀態頁；批次 TC 會產生一組 proof 圖。

    bid 已經在正確狀態下送出（本函式在 save_evidence 內、capture 之後才跑），
    這裡只是把對應的系統畫面叫出來拍給人看。回傳 {group: caption}。
    """
    if TC_ID == "BASELINE":
        return {}
    groups = []
    for tc in TC_ID.split(","):
        group = STATE_GROUP.get(tc.strip())
        if group and group not in groups:
            groups.append(group)
    captions = {}
    for group in groups:
        caption = _capture_group_proof(folder, group)
        if caption:
            captions[group] = caption
    return captions


def _capture_group_proof(folder, group):
    """開啟單一 state group 的證據頁並存成 state_proof_<group>.png。"""
    kind, arg, caption = STATE_SURFACE.get(group, (None, None, None))
    supplemental = (folder / "results.json").exists()

    if kind is None:
        with open(folder / f"state_proof_{group}.txt", "w") as f:
            f.write(f"{TC_ID} ({group}): {caption}\n")
        return caption

    # 先 force-stop Settings：否則若上一條 TC 停在別的設定頁（如 VPN），
    # am start 另一個設定頁常只把既有 Settings 工作列叫回前景、沒真的導頁 → 截到錯頁（bright/dark 出現在 VPN 頁即此因）。
    if kind in ("intent", "component", "appdetails", "applocale"):
        adb("shell", "am", "force-stop", "com.android.settings")
        time.sleep(0.5)
    elif kind in ("qs", "volpanel", "notif"):
        # 亮度/音量/狀態列是疊在當前畫面上，先回桌面 + 清 Settings，背景才乾淨
        adb("shell", "am", "force-stop", "com.android.settings")
        adb("shell", "input", "keyevent", "KEYCODE_HOME")
        time.sleep(0.5)

    launch_result = ""
    volume_before = None
    if kind == "intent":
        launch_result = adb("shell", "am", "start", "-a", arg)
    elif kind == "component":
        launch_result = adb("shell", "am", "start", "-n", arg)
    elif kind == "app":
        # arg = package name；沒安裝就退回說明檔（例：非 Magisk 的 root 方案）
        installed = arg in adb("shell", "pm", "list", "packages", arg)
        if not installed:
            with open(folder / f"state_proof_{group}.txt", "w") as f:
                f.write(f"{TC_ID} ({group}): {arg} 未安裝，改用其他 root 佐證\n")
            return caption
        launch_result = adb("shell", "monkey", "-p", arg, "-c", "android.intent.category.LAUNCHER", "1")
    elif kind == "appdetails":
        launch_result = adb("shell", "am", "start", "-a",
                            "android.settings.APPLICATION_DETAILS_SETTINGS",
                            "-d", f"package:{APP_PACKAGE}")
    elif kind == "applocale":
        launch_result = adb("shell", "am", "start", "-a",
                            "android.settings.APP_LOCALE_SETTINGS",
                            "-d", f"package:{APP_PACKAGE}")
    elif kind == "qs":
        adb("shell", "cmd", "statusbar", "expand-settings")
    elif kind == "volpanel":
        # 在端點按同方向音量鍵，只叫出 system volume panel，不改變受測值。
        volume_before = _volume_music()
        match = re.fullmatch(r"(\d+)/(\d+)", volume_before)
        if not match:
            launch_result = "error: 無法讀取 STREAM_MUSIC current/max；不產生誤導截圖"
        elif int(match.group(1)) == int(match.group(2)):
            launch_result = adb("shell", "input", "keyevent", "KEYCODE_VOLUME_UP")
        elif int(match.group(1)) == 0:
            launch_result = adb("shell", "input", "keyevent", "KEYCODE_VOLUME_DOWN")
        else:
            launch_result = "error: STREAM_MUSIC 不在 min/max 端點，不能擷取端點證據"
    elif kind == "notif":
        adb("shell", "cmd", "statusbar", "expand-notifications")

    if any(x in launch_result.lower() for x in
           ("error", "exception", "unable to resolve", "unknown command", "unrecognized")):
        with open(folder / f"state_proof_{group}.txt", "w") as f:
            f.write(f"{TC_ID} ({group}): 無法開啟預期證據頁\n{launch_result}\n")
        return None

    time.sleep(2.0)
    ui_xml = ""
    if group == "tracking":
        remote_xml = "/sdcard/state_proof_tracking.xml"
        adb("shell", "uiautomator", "dump", remote_xml)
        ui_xml = adb("shell", "cat", remote_xml)
        if ui_xml.strip().startswith("<?xml"):
            with open(folder / "state_proof_tracking.xml", "w") as f:
                f.write(ui_xml)
        if re.search(r"Get new advertising ID|重新取得廣告 ID|取得新的廣告 ID", ui_xml, re.I):
            tracking_state = "opt-out (advertising ID deleted)"
        elif re.search(r"Delete advertising ID|刪除廣告 ID", ui_xml, re.I):
            tracking_state = "opt-in (advertising ID exists)"
        else:
            tracking_state = "unknown (UI label not recognized)"
        with open(folder / "state_proof_tracking_state.json", "w") as f:
            json.dump({"state": tracking_state, "source": "Google Ads settings UI dump"},
                      f, ensure_ascii=False, indent=2)
        caption = f"Google 廣告設定頁：{tracking_state}；與同次 device.ia / device.lat 對照"
        print(f"  state_ui    → {folder / 'state_proof_tracking.xml'}  ({tracking_state})")
        # GAID 字串在頁面底部：狀態判斷用的 XML 已 dump 完（頁首按鈕區），
        # 捲到頁底再截圖，讓截圖裡看得到 advertising ID 本體（與 device.ia 對照）
        adb("shell", "input", "swipe", "540", "1800", "540", "400", "300")
        time.sleep(0.8)
    elif group in {"charging", "batterylevel"}:
        remote_xml = f"/sdcard/state_proof_{group}.xml"
        adb("shell", "uiautomator", "dump", remote_xml)
        ui_xml = adb("shell", "cat", remote_xml)
        if ui_xml.strip().startswith("<?xml"):
            with open(folder / f"state_proof_{group}.xml", "w") as f:
                f.write(ui_xml)
        battery_dump = adb("shell", "dumpsys", "battery")
        source = "MOCKED" if "UPDATES STOPPED" in battery_dump else "REAL"
        caption = f"Android Battery 頁面；Capture source={source}"
    elif group == "volume":
        remote_xml = "/sdcard/state_proof_volume.xml"
        adb("shell", "uiautomator", "dump", remote_xml)
        ui_xml = adb("shell", "cat", remote_xml)
        if ui_xml.strip().startswith("<?xml"):
            with open(folder / "state_proof_volume.xml", "w") as f:
                f.write(ui_xml)
        volume_after = _volume_music()
        if volume_after != volume_before:
            with open(folder / "state_proof_volume.txt", "w") as f:
                f.write(f"{TC_ID} (volume): 顯示面板前後值改變：{volume_before} → {volume_after}\n")
            return None
        if not re.search(r"volume|音量|slider|seekbar", ui_xml, re.I):
            with open(folder / "state_proof_volume.txt", "w") as f:
                f.write(f"{TC_ID} (volume): 畫面未偵測到音量面板，不採納截圖\n")
            return None
        caption = f"System STREAM_MUSIC 音量面板；current/max={volume_after}；顯示前後讀值一致"

    # 對會切換 Activity 的頁面核對前景 package；啟動失敗時不可把上一頁誤當證據。
    # Settings 剛被 force-stop、冷啟動常超過 2 秒 → 輪詢等待，不能只查一次
    # （2026-07-15 曾因單次檢查讓 intent 類 proof 全數 fallback 成 txt）。
    if kind in ("intent", "component", "app", "appdetails", "applocale"):
        expected_pkgs = (["com.google.android.gms"] if kind == "component" else
                         [arg] if kind == "app" else
                         ["com.android.settings", "com.google.android.settings.intelligence"])
        focus_line = ""
        focus_ok = False
        for _ in range(12):
            # 注意：Android 15+ 的 `dumpsys window windows` 沒有 mCurrentFocus，
            # 要用不帶子命令的 `dumpsys window`
            focus = adb("shell", "dumpsys", "window")
            focus_line = next((line for line in focus.splitlines()
                               if "mCurrentFocus" in line or "mFocusedApp" in line
                               or "topResumedActivity" in line), "")
            if any(pkg in focus_line or pkg in ui_xml for pkg in expected_pkgs):
                focus_ok = True
                break
            time.sleep(0.5)
        if not focus_ok:
            with open(folder / f"state_proof_{group}.txt", "w") as f:
                f.write(f"{TC_ID} ({group}): 前景頁不是預期的 {expected_pkgs}\n{focus_line}\n")
            return None
    adb("shell", "settings", "put", "system", "pointer_location", "0")
    proof_path = folder / f"state_proof_{group}.png"
    ok = adb_screencap(str(proof_path))
    with open(folder / f"state_proof_{group}_meta.json", "w") as f:
        json.dump({
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "same_session": not supplemental,
            "evidence_class": "SUPPLEMENTAL" if supplemental else "SAME_CAPTURE",
        }, f, ensure_ascii=False, indent=2)
    # 收起面板 / 回到桌面，避免影響下一輪
    adb("shell", "cmd", "statusbar", "collapse")
    if ok:
        if supplemental:
            caption = "SUPPLEMENTAL（非原 Capture 同時）· " + caption
        print(f"  state_proof → {proof_path}  ({caption})")
        return caption
    return None


# ── evidence bundle ───────────────────────────────────────────────────────────

# ── TC-11: privacy icon 自動點擊 ─────────────────────────────────────────────

def _traffic_line_count():
    if not os.path.exists(TRAFFIC_FILE):
        return 0
    with open(TRAFFIC_FILE) as f:
        return sum(1 for _ in f)


def do_privacy_click(driver, folder):
    """點 privacy information icon → 等 adpolicy.appier.com 流量 → 截落地畫面。

    必須在 phone.png / ad_ui.xml 保存後、TRAFFIC_FILE 歸檔前呼叫：
    點擊會離開廣告畫面，落地流量要趕在 traffic.jsonl 複製前寫入。
    結束時把 app 拉回前景，不影響後續 state proof。
    """
    result = {"tapped": False, "adpolicy": None, "focus_after": ""}
    icon_id = f"{APP_PACKAGE}:id/native_privacy_information_icon_image"
    elem = None
    for locator in ((AppiumBy.ID, icon_id),
                    (AppiumBy.ANDROID_UIAUTOMATOR,
                     'new UiSelector().resourceIdMatches(".*privacy_information_icon.*")')):
        try:
            elem = driver.find_element(*locator)
            break
        except Exception:
            continue
    if elem is None:
        print("  [privacy] icon 不在畫面上，略過 TC-11 點擊")
        return result

    before = _traffic_line_count()
    elem.click()
    result["tapped"] = True
    print("  [privacy] icon 已點擊，等待 adpolicy.appier.com 流量 ...")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if os.path.exists(TRAFFIC_FILE):
            new_rows = open(TRAFFIC_FILE).read().splitlines()[before:]
            hit = next((r for r in new_rows if "adpolicy.appier.com" in r), None)
            if hit:
                try:
                    result["adpolicy"] = json.loads(hit)
                except Exception:
                    result["adpolicy"] = {"raw": hit}
                break
        time.sleep(0.5)

    time.sleep(2.0)  # 落地頁 render
    adb_screencap(str(folder / "privacy_landing.png"))
    focus = adb("shell", "dumpsys", "window")
    m = re.search(r"(mCurrentFocus|mFocusedApp)=.*", focus)
    result["focus_after"] = m.group(0).strip() if m else ""
    with open(folder / "privacy_click.json", "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    if result["adpolicy"]:
        print(f"  [privacy] adpolicy → {result['adpolicy'].get('status')}；privacy_landing.png 已存")
    else:
        print("  [privacy] proxy 未錄到 adpolicy 流量（可能 TLS passthrough／瀏覽器不走代理），"
              "靠 privacy_landing.png 人工核對")

    # privacy icon 開的是 Appier 內建瀏覽器（AppierBrowserActivity）：BACK 出瀏覽器、
    # 返回「同一個廣告」頁（NativeBasicActivity），後續 E2E 點擊才有版位可點。
    # 不能用 am start MainActivity（回選單廣告就沒了）；也不能停在 Browser。
    # mediation 的 privacy 連結可能開 Appier 內建瀏覽器或「外部 Chrome」
    # （com.android.chrome/ChromeTabbedActivity）；兩者都要 BACK 退出才回得到廣告頁。
    for _ in range(5):
        focus_line = next((l for l in adb("shell", "dumpsys", "window").splitlines()
                           if "mCurrentFocus" in l), "")
        on_browser = any(b in focus_line for b in
                         ("BrowserActivity", "ChromeTabbedActivity", "com.android.chrome"))
        on_ad = APP_PACKAGE in focus_line and not on_browser and "MainActivity" not in focus_line
        if on_ad:
            break
        adb("shell", "input", "keyevent", "KEYCODE_BACK")
        time.sleep(1.0)
    return result


# ── E2E 流程逐步截圖 + 點擊手勢 ──────────────────────────────────────────────

E2E_INIT_TMP = "/tmp/appier_e2e_init.png"   # app 啟動當下截圖（在 folder 建立前先落地）


def do_e2e_flow(driver, folder):
    """跑完整 E2E 流程並逐步截圖：③ 渲染 → ⑤ 點擊手勢 → ⑥ 落地。

    ① init 截圖在 main() app 啟動時已存到 E2E_INIT_TMP，這裡搬進 folder。
    測試廣告環境，點擊直接執行（不設核准 gate）；點擊會打 xclk，
    detector 已把流量寫進 traffic.jsonl，此處負責截圖與前景記錄。
    """
    result = {"clicked": False, "xclk": None, "focus_after": "", "steps": []}

    # ① init：把 app 啟動截圖搬進本 capture
    if os.path.exists(E2E_INIT_TMP):
        shutil.copy(E2E_INIT_TMP, folder / "e2e_step_init.png")
        result["steps"].append("init")

    # ③ render：廣告渲染畫面（點擊前）
    adb_screencap(str(folder / "e2e_step_render.png"))
    result["steps"].append("render")

    # ⑤ click：tap 廣告主圖／CTA 觸發 xclk。
    # 可點元素在 sample app 自己的 native_ad_view 內，standalone 與 admob/applovin
    # mediation 共用同一組 resource-id（2026-07-21 對 admob ad_ui.xml 確認）。
    before = _traffic_line_count()
    click_ids = ("native_main_image", "native_cta", "native_ad_view",
                 "native_icon_image", "native_title")

    def _find_ad_target():
        for rid in click_ids:
            try:
                return driver.find_element(AppiumBy.ID, f"{APP_PACKAGE}:id/{rid}")
            except Exception:
                continue
        return None

    target = _find_ad_target()
    # 前一步 privacy click（TC-11）可能把畫面留在瀏覽器/他處（mediation 尤其）；
    # BACK 退回廣告頁再找，最多 4 次。
    for _ in range(4):
        if target is not None:
            break
        adb("shell", "input", "keyevent", "KEYCODE_BACK")
        time.sleep(1.0)
        target = _find_ad_target()
    if target is None:
        try:
            present = sorted(set(re.findall(r'resource-id="([^"]*native[^"]*)"',
                                            driver.page_source)))
        except Exception:
            present = []
        print(f"  [e2e] 找不到廣告可點元素，略過點擊步驟（目前畫面 native_* id：{present}）")
        with open(folder / "e2e_flow.json", "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        return result

    target.click()
    result["clicked"] = True
    print("  [e2e] 已點擊廣告，等待 xclk 點擊鏈 ...")
    time.sleep(1.5)
    adb_screencap(str(folder / "e2e_step_click.png"))     # 點擊當下
    result["steps"].append("click")

    # ⑥ landing：等 deeplink 直開 target app / 落地頁 render
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if os.path.exists(TRAFFIC_FILE):
            new_rows = open(TRAFFIC_FILE).read().splitlines()[before:]
            hit = next((r for r in new_rows if "/xclk" in r), None)
            if hit:
                try:
                    result["xclk"] = json.loads(hit)
                except Exception:
                    result["xclk"] = {"raw": hit}
                break
        time.sleep(0.5)
    time.sleep(2.5)  # 落地頁／target app render
    adb_screencap(str(folder / "e2e_step_landing.png"))
    result["steps"].append("landing")

    focus = adb("shell", "dumpsys", "window")
    m = re.search(r"(mCurrentFocus|mFocusedApp|topResumedActivity)=.*", focus)
    result["focus_after"] = m.group(0).strip() if m else ""
    with open(folder / "e2e_flow.json", "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"  [e2e] xclk={'✓' if result['xclk'] else '未錄到'}；"
          f"落地前景={result['focus_after'][:80]}")

    # 拉回 app 前景收尾
    adb("shell", "am", "start", "-n", f"{APP_PACKAGE}/{APP_ACTIVITY}")
    time.sleep(1.5)
    return result


# ── user.session_duration 三情境（AND-47-1/2/3）────────────────────────────────
# session_duration＝使用者 App 在前景的累積時間（iOS 實作即此語意），
# 不是廣告 session 載入時間。每個 bid request 都帶此值 → 用「bid A → 動作 → bid B」
# 的相對變化驗行為；bid B 不要求命中 TEST_CID（204 no-bid 的 request 也帶 session）。

SESSION_CASE_SPEC = {
    1: ("只關廣告頁（App 全程留前景）", "累進：B > A"),
    2: ("force-stop 關整個 App 後重開", "重置：B < A"),
    3: ("App 退背景數秒再切回前景",     "累進：B > A"),
}


def _session_value(bid):
    """從 bid payload 取 user.session_duration（毫秒）。"""
    from bid_inspector import _unwrap, get_field
    value, found = get_field(_unwrap(bid), "user.session_duration")
    return value if found else None


def do_session_case(driver, case):
    """bid A 已在 BID_FILE：做情境動作 → 再觸發 bid B → 對照寫 SESSION_CASE_FILE。"""
    action_desc, expected = SESSION_CASE_SPEC[case]
    with open(BID_FILE) as f:
        bid_a = json.load(f)
    session_a = _session_value(bid_a)
    shutil.copy(BID_FILE, SESSION_BID_A_FILE)
    print(f"\n[session case {case}] bid A session_duration={session_a}；動作：{action_desc}")

    # bid A 階段 logcat 另存；重啟側錄，bid B 掃描才不會撈到 A 的 payload
    stop_logcat()
    if os.path.exists(LOGCAT_TMP):
        shutil.copy(LOGCAT_TMP, SESSION_LOGCAT_A)
    for f in (FLAG_FILE, BID_FILE, BID_STATUS_FILE, BID_RESPONSE_FILE):
        if os.path.exists(f):
            os.remove(f)
    start_logcat()

    if case == 1:
        driver.back()                                     # 只關廣告頁
        print(f"    前景停留 {SESSION_GAP_SEC:.0f}s 累積 session ...")
        time.sleep(SESSION_GAP_SEC)
    elif case == 2:
        adb("shell", "am", "force-stop", APP_PACKAGE)     # 關整個 App
        time.sleep(2)
        adb("shell", "am", "start", "-n", f"{APP_PACKAGE}/{APP_ACTIVITY}")
        time.sleep(3)                                     # 重開後盡快觸發，session 應接近 0
    else:
        adb("shell", "input", "keyevent", "KEYCODE_HOME")  # 退背景
        time.sleep(SESSION_GAP_SEC)
        adb("shell", "monkey", "-p", APP_PACKAGE,
            "-c", "android.intent.category.LAUNCHER", "1")  # 切回前景
        time.sleep(2)

    session_b = None
    for attempt in range(1, 4):
        print(f"[session case {case}] 觸發 bid B attempt {attempt} ...")
        tapped = False
        for _ in range(3):
            try:
                if tap_trigger(driver):
                    tapped = True
                    break
            except Exception:
                pass
            driver.back()
            time.sleep(0.8)
        if not tapped:
            adb("shell", "am", "start", "-n", f"{APP_PACKAGE}/{APP_ACTIVITY}")
            time.sleep(AD_RETRY_DELAY)
            continue
        deadline = time.monotonic() + BID_TIMEOUT
        bid_b = None
        while time.monotonic() < deadline:
            bid_b, _ = scan_logcat_bid()
            if bid_b is not None:
                break
            time.sleep(0.2)
        if bid_b is not None:
            with open(BID_FILE, "w") as f:      # bid B 即本 capture 的 bid_request.json
                json.dump(bid_b, f, indent=2)
            session_b = _session_value(bid_b)
            break
        driver.back()
        time.sleep(AD_RETRY_DELAY)

    if session_b is None and os.path.exists(SESSION_BID_A_FILE):
        # bid B 沒抓到：還原 bid A 當本 capture 的 bid_request.json，
        # results.json 才會落地（報告端靠它配對 capture），判定記無法對照
        shutil.copy(SESSION_BID_A_FILE, BID_FILE)

    passed = None
    if isinstance(session_a, (int, float)) and isinstance(session_b, (int, float)):
        passed = (session_b < session_a) if case == 2 else (session_b > session_a)
    payload = {
        "case": case, "tc": f"AND-47-{case}",
        "action": action_desc, "expected": expected,
        "gap_sec": SESSION_GAP_SEC, "unit": "ms",
        "session_a": session_a, "session_b": session_b,
        "passed": passed,
    }
    with open(SESSION_CASE_FILE, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    verdict_txt = ("PASS" if passed else "FAIL") if passed is not None else "無法判定（缺 session 值）"
    print(f"[session case {case}] A={session_a} → B={session_b}（預期 {expected}）→ {verdict_txt}")
    return payload


def save_evidence(driver, ts):
    round_dir = resolve_round_dir()
    capture_name = (CAPTURE_LABEL or
                    ("baseline" if TC_ID == "BASELINE" else TC_ID.replace(",", "+")))
    folder = round_dir / f"{capture_name}_{ts}"
    folder.mkdir(parents=True, exist_ok=True)

    environment = collect_environment()
    with open(folder / "environment.json", "w") as f:
        json.dump(environment, f, ensure_ascii=False, indent=2)

    # 關掉 Appium 的指標位置 debug 疊層，證據截圖才乾淨
    adb("shell", "settings", "put", "system", "pointer_location", "0")

    # 1. phone screenshot（bid 當下的 app 畫面 + 狀態列，證明這一 session）
    screenshot_path = str(folder / "phone.png")
    driver.get_screenshot_as_file(screenshot_path)
    print(f"  screenshot  → {screenshot_path}")

    # 1a. 廣告渲染 UI dump（E2E TC-06：畫面文字 ↔ response native 逐項比對）。
    # Appium session 活著時外部 uiautomator dump 會搶不到 accessibility，
    # 改用 driver.page_source（同一 session、內容等價）
    try:
        ad_ui = driver.page_source
        if ad_ui and "<hierarchy" in ad_ui:
            with open(folder / "ad_ui.xml", "w") as f:
                f.write(ad_ui)
            print(f"  ad_ui       → {folder / 'ad_ui.xml'}")
    except Exception as exc:
        print(f"  [warn] ad_ui dump 失敗：{exc}")

    # 1a-2 / 1a-3. TC-11 privacy 點擊 + E2E 完整流程（都要在 traffic.jsonl 歸檔前做）。
    # 順序關鍵：mediation 的 privacy 連結開「外部 Chrome」，round-trip 回來後廣告 view
    # 不再 render → 之後的 ⑤ 廣告點擊會找不到版位。故 mediation 先跑 E2E 點擊（趁廣告
    # 還新鮮），privacy 擺後面（次要，失敗不影響主流程）。standalone 的 privacy 開內建
    # 瀏覽器、BACK 回得到仍在 render 的廣告，維持原順序（privacy 先、E2E 後）。
    def _run_privacy():
        if DO_PRIVACY_CLICK:
            try:
                do_privacy_click(driver, folder)
            except Exception as exc:
                print(f"  [warn] privacy click 失敗（不影響其他證據）：{exc}")

    def _run_e2e():
        if DO_E2E_FLOW:
            try:
                do_e2e_flow(driver, folder)
            except Exception as exc:
                print(f"  [warn] E2E flow 失敗（不影響其他證據）：{exc}")

    if TEST_MODE in ("admob-mediation", "applovin-mediation"):
        _run_e2e()
        _run_privacy()
    else:
        _run_privacy()
        _run_e2e()

    # 1b. state-proof screenshot（叫出看得見該狀態的系統畫面，肉眼證據）
    proof_captions = capture_state_proof(folder)
    if proof_captions:
        with open(folder / "state_proof_captions.json", "w") as f:
            json.dump(proof_captions, f, ensure_ascii=False, indent=2)

    # 1c. 本次實際執行（實機設定 / adb 模擬 real→mock），供報告老實標明
    if STATE_ACTION:
        with open(folder / "state_action.txt", "w") as f:
            f.write(STATE_ACTION + "\n")

    # 2. raw bid request JSON (+ response if the bid won)
    if os.path.exists(BID_FILE):
        shutil.copy(BID_FILE, folder / "bid_request.json")
        print(f"  bid_request → {folder / 'bid_request.json'}")
        # 2a. ext_enc 暗碼欄位：存原始 blob + 解碼 JSON + 明文↔解碼對照（TC-17）
        try:
            from apr_xorenc import write_evidence as decode_ext_enc_evidence
            with open(BID_FILE) as bf:
                _decoded, _cmp_rows = decode_ext_enc_evidence(json.load(bf), str(folder))
            if _decoded is not None:
                _revealed = sum(1 for r in _cmp_rows if r["revealed"])
                print(f"  ext_enc     → ext_enc_raw.txt / ext_enc_decoded.json / "
                      f"ext_enc_all_fields.json / ext_enc_compare.txt"
                      f"（暗碼揭露 {_revealed}/{len(_cmp_rows)} 個重點欄）")
        except Exception as exc:
            print(f"  [warn] ext_enc 解碼失敗（不影響其他證據）：{exc}")
    if os.path.exists(FIRST_BID_FILE):
        shutil.copy(FIRST_BID_FILE, folder / "first_bid_request.json")
        print(f"  first_bid   → {folder / 'first_bid_request.json'}")
    # session case 對照證據（bid_request.json＝bid B；A 與判定另存）
    if SESSION_CASE and os.path.exists(SESSION_CASE_FILE):
        shutil.copy(SESSION_CASE_FILE, folder / "session_case.json")
        print(f"  session_case→ {folder / 'session_case.json'}")
    if SESSION_CASE and os.path.exists(SESSION_BID_A_FILE):
        shutil.copy(SESSION_BID_A_FILE, folder / "session_bid_a.json")
    if SESSION_CASE and os.path.exists(SESSION_LOGCAT_A):
        shutil.copy(SESSION_LOGCAT_A, folder / "logcat_session_a.txt")
    if os.path.exists(BID_RESPONSE_FILE):
        shutil.copy(BID_RESPONSE_FILE, folder / "bid_response.json")
        print(f"  bid_response→ {folder / 'bid_response.json'}")
    if os.path.exists(TRAFFIC_FILE):
        shutil.copy(TRAFFIC_FILE, folder / "traffic.jsonl")
        print(f"  traffic     → {folder / 'traffic.jsonl'}")

    # 3. device state at capture time
    state_str = snapshot_device_state()
    state_path = folder / "device_state.txt"
    with open(state_path, "w") as f:
        f.write(f"Device State — TC: {TC_ID} — {ts}\n")
        f.write("=" * 50 + "\n")
        f.write(state_str + "\n")
    print(f"  device_state→ {state_path}")

    # 4. logcat（session 同步側錄：app 啟動 → bid capture 全程）
    stop_logcat()
    if os.path.exists(LOGCAT_TMP):
        shutil.copy(LOGCAT_TMP, folder / "logcat.txt")
        log_txt = open(LOGCAT_TMP, errors="ignore").read()
        appier_lines = [l for l in log_txt.splitlines(keepends=True)
                        if re.search(r"appier|argus|datasignal", l, re.IGNORECASE)]
        with open(folder / "logcat_appier.txt", "w") as f:
            f.writelines(appier_lines)
        print(f"  logcat      → {folder / 'logcat.txt'}  (appier-only: logcat_appier.txt, {len(appier_lines)} lines)")

        # 4b. bid 識別碼（比廣告截圖有意義）：從 impression tracker URL 解 bidobjid/cid/crid/crpid
        ids = extract_bid_ids(log_txt)
        if ids:
            with open(folder / "bid_ids.json", "w") as f:
                json.dump(ids, f, indent=2)
            print("  bid_ids     → " + ", ".join(f"{k}={v}" for k, v in ids.items()))

    # 5. field validation report + structured results (round 彙總用)
    report_path = folder / "report.txt"
    if os.path.exists(BID_FILE):
        from bid_inspector import run_inspection, format_report, aggregate_round, format_round_report
        with open(BID_FILE) as f:
            bid = json.load(f)
        if TC_ID != "BASELINE":
            tc_filter = set(TC_ID.split(","))
        else:
            # BASELINE 只記標準狀態範圍：互斥狀態 TC 由同 round 內其他自動
            # state captures 提供，避免彙總「取最新 capture」時蓋掉正確結果。
            from build_artifact import AUTO_TCS
            tc_filter = set(AUTO_TCS)
        results = run_inspection(bid, tc_filter)
        header  = (f"Round: {round_dir.name}  |  Mode: {TEST_MODE}  |  Type: {TEST_TYPE}  |  "
                   f"CID: {TEST_CID}  |  Executor: {TEST_EXECUTOR}  |  "
                   f"TC: {TC_ID}  |  App: {APP_PACKAGE}")
        report  = format_report(results, str(folder / "bid_request.json"), header)
        with open(report_path, "w") as f:
            f.write(report + "\n")
        print(f"  report      → {report_path}")
        with open(folder / "results.json", "w") as f:
            json.dump({"tc_id": TC_ID, "captured_at": ts, "app": APP_PACKAGE,
                       "test_type": TEST_TYPE, "test_cid": TEST_CID,
                       "test_mode": TEST_MODE,
                       "test_executor": TEST_EXECUTOR,
                       "environment": environment,
                       "results": results}, f, indent=2)
        print()
        print(report)

        # 5. refresh round report
        rows = aggregate_round(str(round_dir))
        round_report = format_round_report(rows, round_dir.name)
        with open(round_dir / "round_report.txt", "w") as f:
            f.write(round_report + "\n")
        print(f"\n  round report → {round_dir / 'round_report.txt'}")

        # 6. E2E flow 自動評估（依 TEST_MODE/TEST_TYPE 決定適用性 + 跑驗證器）
        try:
            from e2e_catalog import evaluate as e2e_evaluate
            e2e_rows = e2e_evaluate(str(round_dir), TEST_MODE, TEST_TYPE)
            with open(round_dir / "e2e_results.json", "w") as f:
                json.dump({"generated_at": ts, "test_mode": TEST_MODE,
                           "test_type": TEST_TYPE, "results": e2e_rows},
                          f, ensure_ascii=False, indent=2)
            print(f"  e2e results → {round_dir / 'e2e_results.json'}")
        except Exception as exc:
            print(f"  [warn] E2E 評估失敗（不影響 signal 證據）：{exc}")
    else:
        print("  [warn] bid_request.json not found — no bid captured this run")

    return folder


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    if not APP_PACKAGE or not APP_ACTIVITY:
        sys.exit(
            "Required env vars not set:\n"
            "  export APP_PACKAGE=com.appier.ssp.sample\n"
            "  export APP_ACTIVITY=com.appier.ssp.MainActivity"
        )

    global UDID
    UDID = udid = detect_udid()
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    test_type = resolve_test_type()
    test_mode = resolve_test_mode()
    test_cid = resolve_test_cid()

    print(f"[device] {udid}")
    print(f"[type  ] {test_type}")
    print(f"[mode  ] {test_mode}")
    print(f"[CID   ] {test_cid}")
    print(f"[by    ] {TEST_EXECUTOR}")
    print(f"[round ] {TEST_ROUND}")
    print(f"[TC    ] {TC_ID}")
    print(f"[app   ] {APP_PACKAGE}")
    if TRIGGER_TEXT:
        print(f"[tap   ] '{TRIGGER_TEXT}'")
    print()

    # clear previous flags（TRAFFIC_FILE 只在 session 開始清一次，
    # 讓 app 啟動到 capture 的完整流量都留在 log 裡）
    for f in (FLAG_FILE, BID_FILE, FIRST_BID_FILE, BID_STATUS_FILE,
              BID_RESPONSE_FILE, TRAFFIC_FILE,
              SESSION_CASE_FILE, SESSION_BID_A_FILE, SESSION_LOGCAT_A):
        if os.path.exists(f):
            os.remove(f)

    # force-stop before Appium connects — Appium will launch on connect
    print("[→] force-stop ...")
    adb("shell", "am", "force-stop", APP_PACKAGE)
    time.sleep(0.5)

    print("[→] logcat recording ...")
    start_logcat()

    options = UiAutomator2Options()
    options.app_package  = APP_PACKAGE
    options.app_activity = APP_ACTIVITY
    options.no_reset     = True
    options.udid         = udid

    print("[→] launching via Appium ...")
    driver = webdriver.Remote(APPIUM_URL, options=options)
    time.sleep(2.0)

    # Wizard 的 state proof 會把 Settings / Tailscale 等 app 留在前景；即使
    # Appium session 已建立，也要顯式拉回受測 app，避免在外部 app 找 tab。
    driver.activate_app(APP_PACKAGE)
    time.sleep(1.0)

    # TEST_MODE 不只寫入報告：先切到對應 SDK integration tab，之後所有同名
    # trigger 也都以畫面座標過濾，避免點到 ViewPager 預載的相鄰分頁。
    select_test_mode_tab(driver)

    # E2E ① init：app 剛啟動的畫面（folder 尚未建立，先落地到暫存，save_evidence 再搬入）
    if DO_E2E_FLOW:
        if adb_screencap(E2E_INIT_TMP):
            print(f"  [e2e] init 截圖 → {E2E_INIT_TMP}")

    try:
        # 前景停留（session_duration / app_duration 類 TC 需累積使用時間）
        if DWELL_SEC > 0:
            print(f"[→] 前景停留 {DWELL_SEC:.0f}s ...")
            time.sleep(DWELL_SEC)

        if DO_FGBG:
            print("[→] 自動執行背景 → 前景切換 ...")
            adb("shell", "input", "keyevent", "KEYCODE_HOME")
            time.sleep(2)
            adb("shell", "monkey", "-p", APP_PACKAGE,
                "-c", "android.intent.category.LAUNCHER", "1")
            time.sleep(2)

        attempt = 0
        while True:
            attempt += 1
            if MAX_AD_ATTEMPTS and attempt > MAX_AD_ATTEMPTS:
                print(f"\n[停止] 已刷 {MAX_AD_ATTEMPTS} 次，仍未命中指定 CID：{TEST_CID}")
                print("       請檢查廣告流量／campaign 狀態、CID 投遞條件、"
                      "Tailscale 台灣 Office VPN（tpe-exit-3）與 GAID 狀態後再試。")
                return 4

            if attempt > 1:
                stop_logcat()
                for f in (FLAG_FILE, BID_FILE, BID_STATUS_FILE, BID_RESPONSE_FILE):
                    if os.path.exists(f):
                        os.remove(f)
                start_logcat()
                driver.back()
                time.sleep(1.2)

            print(f"[→] 刷廣告 attempt {attempt}：tap '{TRIGGER_TEXT}' ...")
            tapped = False
            for _ in range(3):
                try:
                    if tap_trigger(driver):
                        tapped = True
                        break
                except Exception:
                    pass
                driver.back()
                time.sleep(0.8)
            if not tapped:
                print("    [retry] 找不到指定版位，重新拉回 app 前景後重試。")
                # 其他 app（如 Tailscale）搶走前景時，back 無法復原；直接重新帶起主畫面
                adb("shell", "am", "start", "-n", f"{APP_PACKAGE}/{APP_ACTIVITY}")
                time.sleep(AD_RETRY_DELAY)
                continue

            print(f"[→] waiting for bid request (timeout {BID_TIMEOUT}s) ...")
            deadline = time.monotonic() + BID_TIMEOUT
            bid = None
            while time.monotonic() < deadline:
                if os.path.exists(FLAG_FILE):
                    break
                bid, _ = scan_logcat_bid()
                if bid is not None:
                    break
                time.sleep(0.2)

            if os.path.exists(FLAG_FILE):
                hit = open(FLAG_FILE).read().strip()
                time.sleep(1.0)
                status = (open(BID_STATUS_FILE).read().strip()
                          if os.path.exists(BID_STATUS_FILE) else "?")
                source = "proxy"
            else:
                time.sleep(1.0)
                bid, status = scan_logcat_bid()
                if bid is None:
                    print("    [retry] 沒偵測到 bid request。")
                    time.sleep(AD_RETRY_DELAY)
                    continue
                with open(BID_FILE, "w") as f:
                    json.dump(bid, f, indent=2)
                if status:
                    with open(BID_STATUS_FILE, "w") as f:
                        f.write(status)
                hit = "POST /v2/sdk/aos/ad (from logcat)"
                source = "logcat"

            if attempt == 1 and os.path.exists(BID_FILE):
                shutil.copy(BID_FILE, FIRST_BID_FILE)

            ad_identity = scan_logcat_ad_identity()
            if SAVE_ON_BID and os.path.exists(BID_FILE):
                # request payload 即證據；response/CID 不作為入庫條件
                if not ad_identity:
                    ad_identity = {"cid": "(no-win)", "crid": "(no-win)"}
                print(f"    [SAVE_ON_BID] bid request 已取得（response={status or 'unknown'}），入庫。")
                break
            if status != "200":
                print(f"    [retry] response={status or 'unknown'}，未命中廣告。")
            elif not ad_identity:
                print("    [retry] loaded ad identity 不明，不能 Capture。")
            elif TEST_CID and ad_identity["cid"] != TEST_CID:
                print(f"    [retry] CID 不符：expected={TEST_CID}, actual={ad_identity['cid']}")
            else:
                break
            time.sleep(AD_RETRY_DELAY)

        print(f"\n[CAPTURED via {source}] {hit}  (response: {status or 'unknown'}, "
              f"cid={ad_identity['cid']}, crid={ad_identity['crid']})\n")
        if status == "204":
            print("[判定] server 回 204 no-bid — 連線正常，目前沒有廣告可刷"
                  "（campaign 沒投遞 / 沒 fill）；bid request 仍已留存可驗欄位。\n")

        # session_duration 三情境：bid A 到手後做情境動作、抓 bid B 對照
        if SESSION_CASE in ("1", "2", "3"):
            try:
                do_session_case(driver, int(SESSION_CASE))
            except Exception as exc:
                print(f"[warn] session case 執行失敗（bid A 證據仍保留）：{exc}")

        # save evidence
        print("[→] saving evidence ...")
        folder = save_evidence(driver, ts)
        print(f"\n[DONE] {folder}/")
        result_code = 3 if status == "204" else 0

    finally:
        stop_logcat()
        try:
            driver.quit()
        except Exception as exc:
            # session 逾時/已死時 quit 會丟例外；證據已存完，不能讓收尾失敗把
            # 本 round 標成未完成
            print(f"[warn] driver.quit() 失敗（不影響已存證據）：{exc}")

    # 發布可能耗時超過 Appium newCommandTimeout，必須在 quit 後才做；否則
    # session 會在 git push 期間過期，污染 Wizard 下一個 capture。
    from publish_pages import auto_publish
    auto_publish()
    return result_code


if __name__ == "__main__":
    _t0 = time.monotonic()
    _rc = main() or 0
    _elapsed = time.monotonic() - _t0
    _mins, _secs = divmod(int(_elapsed), 60)
    _hms = f"{_mins}m{_secs:02d}s"
    print(f"\n[整體耗時] 本次 capture round 共 {_hms}（{_elapsed:.1f}s），exit={_rc}")
    try:
        with open(resolve_round_dir() / "round_timing.txt", "a") as _tf:
            _tf.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {TC_ID}  {_hms}  exit={_rc}\n")
    except Exception:
        pass
    sys.exit(_rc)
