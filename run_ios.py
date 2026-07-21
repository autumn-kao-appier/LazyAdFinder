"""
用法:
    python run_ios.py [最多幾輪，預設 30] [UDID]

    UDID 省略時自動偵測唯一連接的 iPhone。
    多台裝置時必須指定：python run_ios.py 30 00008030-xxxx

前置：
    pip install Appium-Python-Client
    appium（另開 terminal: appium）
    手機 Wi-Fi proxy 設為 Mac IP:8888（跟 Charles 同 port）

環境變數（選填）：
    BUNDLE_ID   sample app bundle id，預設 "com.appier.Random"
    AD_LABEL    要點擊的 accessibility id，預設 "basic"
    STOP_ON     "win"（預設）= bid response 200 才停；204 no-bid 繼續。
                "bid" = 只要看到 bid request 就停。
"""

import os
import re
import subprocess
import sys
import time

from appium import webdriver
from appium.options.ios.xcuitest.base import XCUITestOptions

FLAG_FILE = "/tmp/appier_hit"
BID_STATUS_FILE = "/tmp/appier_bid_status"
BID_RESPONSE_FILE = "/tmp/appier_bid_response.json"
NETWORK_FILE = "/tmp/current_networks"
BUNDLE_ID = os.environ.get("BUNDLE_ID", "com.appier.Random")
AD_LABEL = os.environ.get("AD_LABEL", "basic")
STOP_ON = os.environ.get("STOP_ON", "win")
APPIUM_SERVER = "http://127.0.0.1:4723"
AD_TIMEOUT_SEC = 5.0
BID_STATUS_WAIT_SEC = 2.0
AD_POLL_INTERVAL = 0.1
MAX_ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 30

if STOP_ON not in ("win", "bid"):
    sys.exit("STOP_ON 只能是 'win' 或 'bid'。")

# 真機簽 WebDriverAgent 用的 Apple Development Team ID（cert 的 OU 欄位）。
# 用環境變數帶進來，不寫死在 repo 裡：
#   XCODE_ORG_ID=XXXXXXXXXX   # 必填才會自動簽名
#   WDA_BUNDLE_ID=com.you.wda # 選填，bundle id 衝突時換一個唯一的
XCODE_ORG_ID = os.environ.get("XCODE_ORG_ID")
WDA_BUNDLE_ID = os.environ.get("WDA_BUNDLE_ID")


def detect_udid():
    if len(sys.argv) > 2:
        return sys.argv[2]
    out = subprocess.check_output(["xcrun", "xctrace", "list", "devices"], text=True)
    # 只抓 == Devices == 區塊（排除 Offline 跟 Simulators）
    devices_section = out.split("== Devices ==")[1].split("==")[0]
    # iPhone UDID 格式：舊款 40 碼 hex，或新款 8 碼-16 碼。
    # Mac 本機是 8-4-4-4-12 的標準 UUID，不會被這個 pattern 命中，
    # 不用再靠主機名稱有沒有 "Mac" 來排除（主機名可能叫 MBP 之類的）。
    udids = re.findall(r'\(([0-9A-Fa-f]{40}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16})\)', devices_section)
    if not udids:
        sys.exit("找不到連接的 iPhone，請接上手機或手動指定 UDID。")
    if len(udids) > 1:
        sys.exit(f"偵測到多台裝置：{udids}\n請執行：python run_ios.py {MAX_ROUNDS} <UDID>")
    print(f"[device] {udids[0]}")
    return udids[0]


def ensure_on_list(driver):
    """確保停在 list 頁：找得到 AD_LABEL 就代表在 list，找不到就 driver.back() 退回。
    這個 app 的返回鍵沒有固定的 accessibility id，用 driver.back() 比較穩。"""
    for _ in range(4):
        try:
            driver.find_element("accessibility id", AD_LABEL)
            return
        except Exception:
            try:
                driver.back()
                time.sleep(1.0)
            except Exception:
                return


def read_bid_status():
    """等 bid response 回來，回傳 status code 字串或 None。"""
    deadline = time.monotonic() + BID_STATUS_WAIT_SEC
    while time.monotonic() < deadline:
        if os.path.exists(BID_STATUS_FILE):
            with open(BID_STATUS_FILE) as f:
                return f.read().strip()
        time.sleep(AD_POLL_INTERVAL)
    return None


# 清掉上一次的 capture 紀錄
for f in (FLAG_FILE, BID_STATUS_FILE, BID_RESPONSE_FILE, NETWORK_FILE):
    if os.path.exists(f):
        os.remove(f)

options = XCUITestOptions()
options.bundle_id = BUNDLE_ID
options.automation_name = "XCUITest"
options.no_reset = True
options.udid = detect_udid()

# 有給 Team ID 就讓 Appium 自動簽 + 自動建 provisioning profile，
# 不用手動進 Xcode 設 signing。
if XCODE_ORG_ID:
    options.set_capability("xcodeOrgId", XCODE_ORG_ID)
    options.set_capability("xcodeSigningId", "Apple Development")
    options.set_capability("allowProvisioningDeviceRegistration", True)
    if WDA_BUNDLE_ID:
        options.set_capability("updatedWDABundleId", WDA_BUNDLE_ID)

driver = webdriver.Remote(APPIUM_SERVER, options=options)

try:
    print(f"[bundle] {BUNDLE_ID}")
    print(f"[label ] {AD_LABEL}")
    print(f"[stop  ] {STOP_ON}")

    # 一開始先確保停在 list（app 可能停在廣告頁）
    ensure_on_list(driver)

    for i in range(1, MAX_ROUNDS + 1):
        # 每輪先確保回到 list 頁（不管上一輪停在哪）
        ensure_on_list(driver)

        for f in (FLAG_FILE, BID_STATUS_FILE, BID_RESPONSE_FILE, NETWORK_FILE):
            if os.path.exists(f):
                os.remove(f)

        print(f"[{i}/{MAX_ROUNDS}] tapping '{AD_LABEL}' ...")
        try:
            driver.find_element("accessibility id", AD_LABEL).click()
        except Exception:
            print(f"[{i}] 找不到 '{AD_LABEL}'，重試")
            continue

        deadline = time.monotonic() + AD_TIMEOUT_SEC
        # Phase 1: wait for first request (ad loaded)
        while time.monotonic() < deadline:
            if os.path.exists(NETWORK_FILE) or os.path.exists(FLAG_FILE):
                break
            time.sleep(AD_POLL_INTERVAL)
        # Phase 2: use remaining window to catch Appier requests that arrive after first hit
        while time.monotonic() < deadline:
            if os.path.exists(FLAG_FILE):
                break
            time.sleep(AD_POLL_INTERVAL)

        if os.path.exists(NETWORK_FILE):
            names = list(dict.fromkeys(open(NETWORK_FILE).read().splitlines()))
            print(f"         → {', '.join(names) if names else '(unknown)'}")
        else:
            print(f"         → (mitmdump 沒收到流量，確認 Charles upstream proxy 設為 127.0.0.1:8081)")

        if os.path.exists(FLAG_FILE):
            with open(FLAG_FILE) as f:
                hit = f.read().strip()
            status = read_bid_status()
            if status == "200":
                print(f"\n[STOP] Appier ad WON (bid 200) — {hit}")
                print("       request: /tmp/appier_bid.json  response: /tmp/appier_bid_response.json")
                break
            if status == "204":
                if STOP_ON == "bid":
                    print(f"\n[STOP] Appier bid request (204 no-bid) — {hit}")
                    break
                print("         → Appier bid 204 no-bid，繼續")
                continue
            # response 沒等到可能是 proxy/連線異常；避免把未知狀態當 no-bid。
            print(f"\n[STOP] Appier bid request (response 未確認) — {hit}")
            break
    else:
        print(f"\n[DONE] {MAX_ROUNDS} 輪都沒出現 Appier ad。")

finally:
    driver.quit()
