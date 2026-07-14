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
    TRIGGER_TEXT    UI element text to tap to fire bid (leave unset if app auto-loads)

Three terminals:
    T1: mitmdump -s ~/LazyAdFinder/detector.py --listen-port 8081
    T2: appium
    T3: python ~/LazyAdFinder/run_ssp.py AND-04
"""

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
BID_STATUS_FILE   = "/tmp/appier_bid_status"
BID_RESPONSE_FILE = "/tmp/appier_bid_response.json"
LOGCAT_TMP   = "/tmp/appier_logcat.txt"

LOGCAT_PROC = None
APPIUM_URL   = "http://127.0.0.1:4723"
BID_TIMEOUT  = 12.0
EVIDENCE_DIR = Path(os.environ.get("EVIDENCE_DIR", Path(__file__).parent / "evidence"))

APP_PACKAGE  = os.environ.get("APP_PACKAGE")
APP_ACTIVITY = os.environ.get("APP_ACTIVITY")
TRIGGER_TEXT = os.environ.get("TRIGGER_TEXT")
TEST_ROUND   = os.environ.get("TEST_ROUND", "adhoc")
VALID_TYPES  = ("aibid", "reen-static", "reen-dynamic")
TEST_TYPE    = os.environ.get("TEST_TYPE", "").strip().lower()  # 這輪測什麼


def resolve_test_type():
    """回傳本輪測試類型（aibid / reen-static / reen-dynamic）。
    有設 env 就驗；沒設就問（互動）；非互動又沒設則標 unspecified。"""
    global TEST_TYPE
    if TEST_TYPE in VALID_TYPES:
        return TEST_TYPE
    if TEST_TYPE:
        print(f"[warn] TEST_TYPE='{TEST_TYPE}' 非法，應為 {VALID_TYPES}")
    if sys.stdin.isatty():
        print("這輪測什麼？ 1) aibid  2) reen-static  3) reen-dynamic")
        pick = input("選 1/2/3（或直接打名字）: ").strip().lower()
        TEST_TYPE = {"1": "aibid", "2": "reen-static", "3": "reen-dynamic"}.get(pick, pick)
        if TEST_TYPE not in VALID_TYPES:
            print(f"[warn] 未辨識 '{TEST_TYPE}'，記為 unspecified")
            TEST_TYPE = "unspecified"
    else:
        TEST_TYPE = "unspecified"
    return TEST_TYPE
STATE_ACTION = os.environ.get("STATE_ACTION")       # 本次實際做了什麼（實機/模擬）
DWELL_SEC    = float(os.environ.get("DWELL_SEC", "0"))  # 觸發廣告前先前景停留秒數

TC_ID = sys.argv[1] if len(sys.argv) > 1 else "BASELINE"
UDID  = sys.argv[2] if len(sys.argv) > 2 else None


def resolve_round_dir():
    """同 round 標籤重複執行時歸入既有資料夾；沒有才用當下時間戳開新的。"""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(d for d in EVIDENCE_DIR.glob(f"{TEST_ROUND}_*") if d.is_dir())
    if existing:
        return existing[-1]
    return EVIDENCE_DIR / f"{TEST_ROUND}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


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
# proxy: [AdRequestJSON] {...} is the request, onAdLoaded/onAdNoBid the result.
ADREQ_RE = re.compile(r"\[AdRequestJSON\]\s*(\{.*\})\s*$")
LOADED_RE = re.compile(r"onAdLoaded\(\)")
NOBID_RE = re.compile(r"onAdNoBid\(\)")


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
    return bid, status


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
    }

    # foreground activity + 受測 app 版本
    focus = adb("shell", "dumpsys", "window")
    m = re.search(r"mCurrentFocus=.*", focus)
    raw["foreground"] = m.group(0) if m else "(unknown)"
    pkg_dump = adb("shell", "dumpsys", "package", APP_PACKAGE)
    m = re.search(r"versionName=\S+", pkg_dump)
    raw["app_version"] = m.group(0) if m else "(unknown)"

    # summarise battery dump to relevant lines
    batt_lines = [
        l.strip() for l in raw["battery"].splitlines()
        if any(k in l for k in ("level", "AC powered", "USB powered", "status:"))
    ]
    raw["battery"] = " | ".join(batt_lines)

    lines = [f"  {k:<22}: {v}" for k, v in raw.items()]
    return "\n".join(lines)


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
    "AND-45": "geo", "AND-46": "geo",
    "AND-47": "session", "AND-48": "session", "AND-52": "session", "AND-50": "fgbg",
}

# 組 → (kind, arg, 說明該截圖證明什麼)
#   intent    : am start -a <arg>
#   component : am start -n <pkg/activity>（指定 activity，用於沒有 action 的頁）
#   app       : 啟動某 app（monkey launcher）— 例：Magisk 當 root 佐證
#   appdetails: App 詳情頁（權限 / 廣告 ID）
#   qs        : 下拉快捷面板（亮度滑桿）
#   volkey    : 按音量鍵叫出音量面板
#   notif     : 展開狀態列（充電 / VPN / 電量圖示）
#   None      : 截圖無法證明，改寫說明檔
STATE_SURFACE = {
    "darkmode":     ("intent", "android.settings.DISPLAY_SETTINGS", "Display 設定的 Dark theme 開關"),
    "batterysaver": ("intent", "android.settings.BATTERY_SAVER_SETTINGS", "省電模式開關狀態"),
    "batterylevel": ("intent", "android.intent.action.POWER_USAGE_SUMMARY", "電池頁面電量百分比"),
    "charging":     ("intent", "android.intent.action.POWER_USAGE_SUMMARY", "電池頁面充電狀態"),
    "tz":           ("intent", "android.settings.DATE_SETTINGS", "日期時間設定的時區（GMT offset）"),
    "geo":          ("intent", "android.settings.LOCATION_SOURCE_SETTINGS", "定位開關 / 權限"),
    "vpn":          ("intent", "android.settings.VPN_SETTINGS", "VPN 連線狀態"),
    "fontscale":    ("intent", "android.settings.ACCESSIBILITY_SETTINGS", "無障礙 → 字體大小"),
    "volume":       ("volkey", None, "音量面板 media 音量"),
    "screenbright": ("qs", None, "快捷面板亮度滑桿位置"),
    # 完整廣告 ID 頁（Reset/Delete/Get new advertising ID）；GMS 的 ADS_PRIVACY action 只開精簡頁
    "tracking":     ("component", "com.google.android.gms/.adsidentity.settings.AdsIdentitySettingsActivity",
                     "系統廣告 ID 頁：opt-in 顯示 ID+Reset/Delete、opt-out 顯示 Get new advertising ID"),
    # root 機用 Magisk 畫面當佐證（su binary 由 Magisk 提供）
    "jailbreak":    ("app", "com.topjohnwu.magisk", "Magisk app 畫面（root 佐證：版本 / package）"),
    "emulator":     (None, None, "實機 / AVD 需外部佐證（截圖不足以證明）"),
    "session":      (None, None, "session 時長無對應設定頁，靠 bid 值 + 操作時序佐證"),
    "fgbg":         (None, None, "前景/背景切換靠操作時序佐證"),
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
    """為單一 state TC 擷取「看得見該狀態」的證據截圖 → state_proof.png。

    bid 已經在正確狀態下送出（本函式在 save_evidence 內、capture 之後才跑），
    這裡只是把對應的系統畫面叫出來拍給人看。回傳 caption 或 None。
    """
    if "," in TC_ID or TC_ID == "BASELINE":
        return None
    group = STATE_GROUP.get(TC_ID)
    if not group:
        return None
    kind, arg, caption = STATE_SURFACE.get(group, (None, None, None))

    if kind is None:
        with open(folder / "state_proof.txt", "w") as f:
            f.write(f"{TC_ID} ({group}): {caption}\n")
        return caption

    # 先 force-stop Settings：否則若上一條 TC 停在別的設定頁（如 VPN），
    # am start 另一個設定頁常只把既有 Settings 工作列叫回前景、沒真的導頁 → 截到錯頁（bright/dark 出現在 VPN 頁即此因）。
    if kind in ("intent", "component", "appdetails"):
        adb("shell", "am", "force-stop", "com.android.settings")
        time.sleep(0.5)
    elif kind in ("qs", "volkey", "notif"):
        # 亮度/音量/狀態列是疊在當前畫面上，先回桌面 + 清 Settings，背景才乾淨
        adb("shell", "am", "force-stop", "com.android.settings")
        adb("shell", "input", "keyevent", "KEYCODE_HOME")
        time.sleep(0.5)

    if kind == "intent":
        adb("shell", "am", "start", "-a", arg)
    elif kind == "component":
        adb("shell", "am", "start", "-n", arg)
    elif kind == "app":
        # arg = package name；沒安裝就退回說明檔（例：非 Magisk 的 root 方案）
        installed = arg in adb("shell", "pm", "list", "packages", arg)
        if not installed:
            with open(folder / "state_proof.txt", "w") as f:
                f.write(f"{TC_ID} ({group}): {arg} 未安裝，改用其他 root 佐證\n")
            return caption
        adb("shell", "monkey", "-p", arg, "-c", "android.intent.category.LAUNCHER", "1")
    elif kind == "appdetails":
        adb("shell", "am", "start", "-a",
            "android.settings.APPLICATION_DETAILS_SETTINGS", "-d", f"package:{APP_PACKAGE}")
    elif kind == "qs":
        adb("shell", "cmd", "statusbar", "expand-settings")
    elif kind == "volkey":
        adb("shell", "input", "keyevent", "25")  # VOLUME_DOWN 叫出音量面板
    elif kind == "notif":
        adb("shell", "cmd", "statusbar", "expand-notifications")

    time.sleep(2.0)
    adb("shell", "settings", "put", "system", "pointer_location", "0")
    ok = adb_screencap(str(folder / "state_proof.png"))
    # 收起面板 / 回到桌面，避免影響下一輪
    adb("shell", "cmd", "statusbar", "collapse")
    if ok:
        print(f"  state_proof → {folder / 'state_proof.png'}  ({caption})")
        return caption
    return None


# ── evidence bundle ───────────────────────────────────────────────────────────

def save_evidence(driver, ts):
    round_dir = resolve_round_dir()
    capture_name = "baseline" if TC_ID == "BASELINE" else TC_ID.replace(",", "+")
    folder = round_dir / f"{capture_name}_{ts}"
    folder.mkdir(parents=True, exist_ok=True)

    # 關掉 Appium 的指標位置 debug 疊層，證據截圖才乾淨
    adb("shell", "settings", "put", "system", "pointer_location", "0")

    # 1. phone screenshot（bid 當下的 app 畫面 + 狀態列，證明這一 session）
    screenshot_path = str(folder / "phone.png")
    driver.get_screenshot_as_file(screenshot_path)
    print(f"  screenshot  → {screenshot_path}")

    # 1b. state-proof screenshot（叫出看得見該狀態的系統畫面，肉眼證據）
    proof_caption = capture_state_proof(folder)
    if proof_caption:
        with open(folder / "state_proof_caption.txt", "w") as f:
            f.write(proof_caption + "\n")

    # 1c. 本次實際執行（實機設定 / adb 模擬 real→mock），供報告老實標明
    if STATE_ACTION:
        with open(folder / "state_action.txt", "w") as f:
            f.write(STATE_ACTION + "\n")

    # 2. raw bid request JSON (+ response if the bid won)
    if os.path.exists(BID_FILE):
        shutil.copy(BID_FILE, folder / "bid_request.json")
        print(f"  bid_request → {folder / 'bid_request.json'}")
    if os.path.exists(BID_RESPONSE_FILE):
        shutil.copy(BID_RESPONSE_FILE, folder / "bid_response.json")
        print(f"  bid_response→ {folder / 'bid_response.json'}")

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
        tc_filter = set(TC_ID.split(",")) if TC_ID != "BASELINE" else None
        results = run_inspection(bid, tc_filter)
        header  = f"Round: {round_dir.name}  |  TC: {TC_ID}  |  App: {APP_PACKAGE}"
        report  = format_report(results, str(folder / "bid_request.json"), header)
        with open(report_path, "w") as f:
            f.write(report + "\n")
        print(f"  report      → {report_path}")
        with open(folder / "results.json", "w") as f:
            json.dump({"tc_id": TC_ID, "captured_at": ts, "app": APP_PACKAGE,
                       "test_type": TEST_TYPE, "results": results}, f, indent=2)
        print()
        print(report)

        # 5. refresh round report
        rows = aggregate_round(str(round_dir))
        round_report = format_round_report(rows, round_dir.name)
        with open(round_dir / "round_report.txt", "w") as f:
            f.write(round_report + "\n")
        print(f"\n  round report → {round_dir / 'round_report.txt'}")
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

    print(f"[device] {udid}")
    print(f"[type  ] {test_type}")
    print(f"[round ] {TEST_ROUND}")
    print(f"[TC    ] {TC_ID}")
    print(f"[app   ] {APP_PACKAGE}")
    if TRIGGER_TEXT:
        print(f"[tap   ] '{TRIGGER_TEXT}'")
    print()

    # clear previous flags
    for f in (FLAG_FILE, BID_FILE, BID_STATUS_FILE, BID_RESPONSE_FILE):
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

    try:
        # 前景停留（session_duration / app_duration 類 TC 需累積使用時間）
        if DWELL_SEC > 0:
            print(f"[→] 前景停留 {DWELL_SEC:.0f}s ...")
            time.sleep(DWELL_SEC)

        # optional tap to trigger bid
        if TRIGGER_TEXT:
            print(f"[→] tap '{TRIGGER_TEXT}' ...")
            try:
                el = driver.find_element(
                    AppiumBy.ANDROID_UIAUTOMATOR,
                    f'new UiSelector().text("{TRIGGER_TEXT}")'
                )
                el.click()
            except Exception as e:
                print(f"    [warn] tap failed: {e}")

        # wait for bid — either the proxy (FLAG_FILE) or the SDK's own logcat
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
            # proxy path: detector.py already wrote BID_FILE + status/response
            hit = open(FLAG_FILE).read().strip()
            time.sleep(1.0)  # give the bid response a moment to land
            status = (open(BID_STATUS_FILE).read().strip()
                      if os.path.exists(BID_STATUS_FILE) else "?")
            source = "proxy"
        else:
            # logcat path: derive body + status from SDK logs, no proxy needed
            time.sleep(1.0)  # let onAdLoaded/onAdNoBid land
            bid, status = scan_logcat_bid()
            if bid is None:
                print("\n[TIMEOUT] No Appier bid detected (proxy or logcat).")
                diagnose_no_ad()
                return
            with open(BID_FILE, "w") as f:
                json.dump(bid, f, indent=2)
            if status:
                with open(BID_STATUS_FILE, "w") as f:
                    f.write(status)
            hit = "POST /v2/sdk/aos/ad (from logcat)"
            source = "logcat"

        print(f"\n[CAPTURED via {source}] {hit}  (response: {status or '?'})\n")
        if status == "204":
            print("[判定] server 回 204 no-bid — 連線正常，目前沒有廣告可刷"
                  "（campaign 沒投遞 / 沒 fill）；bid request 仍已留存可驗欄位。\n")

        # save evidence
        print("[→] saving evidence ...")
        folder = save_evidence(driver, ts)
        print(f"\n[DONE] {folder}/")

    finally:
        stop_logcat()
        driver.quit()


if __name__ == "__main__":
    main()
