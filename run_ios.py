"""
用法:
    python run.py [最多幾輪，預設 30] [UDID]

    UDID 省略時自動偵測唯一連接的 iPhone。
    多台裝置時必須指定：python run.py 30 00008030-xxxx

前置：
    pip install Appium-Python-Client
    appium（另開 terminal: appium）
    手機 Wi-Fi proxy 設為 Mac IP:8888（跟 Charles 同 port）
"""

import os
import re
import subprocess
import sys
import time

from appium import webdriver
from appium.options.ios.xcuitest.base import XCUITestOptions

FLAG_FILE = "/tmp/appier_hit"
NETWORK_FILE = "/tmp/current_networks"
BUNDLE_ID = "com.appier.Random"
APPIUM_SERVER = "http://127.0.0.1:4723"
AD_TIMEOUT_SEC = 5.0
AD_POLL_INTERVAL = 0.1
MAX_ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 30

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
        sys.exit(f"偵測到多台裝置：{udids}\n請執行：python run.py {MAX_ROUNDS} <UDID>")
    print(f"[device] {udids[0]}")
    return udids[0]


def ensure_on_list(driver):
    """確保停在 list 頁：找得到 basic 就代表在 list，找不到就 driver.back() 退回。
    這個 app 的返回鍵沒有固定的 accessibility id，用 driver.back() 比較穩。"""
    for _ in range(3):
        try:
            driver.find_element("accessibility id", "basic")
            return
        except Exception:
            try:
                driver.back()
                time.sleep(1.0)
            except Exception:
                return


# 清掉上一次的 flag
if os.path.exists(FLAG_FILE):
    os.remove(FLAG_FILE)

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
    # 一開始先確保停在 list（app 可能停在廣告頁）
    ensure_on_list(driver)

    for i in range(1, MAX_ROUNDS + 1):
        # 每輪先確保回到 list 頁（不管上一輪停在哪）
        ensure_on_list(driver)

        if os.path.exists(NETWORK_FILE):
            os.remove(NETWORK_FILE)

        print(f"[{i}/{MAX_ROUNDS}] tapping basic ...")
        try:
            driver.find_element("accessibility id", "basic").click()
        except Exception:
            print(f"[{i}] 找不到 basic，重試")
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
            hit = open(FLAG_FILE).read().strip()
            print(f"\n[STOP] Appier detected — {hit}")
            break
    else:
        print(f"\n[DONE] {MAX_ROUNDS} 輪都沒出現 Appier ad。")

finally:
    driver.quit()
