"""
用法:
    python run.py [最多幾輪，預設 30] [UDID]

    UDID 省略時自動偵測唯一連接的 iPhone。
    多台裝置時必須指定：python run.py 30 00008030-xxxx

前置：
    pip install Appium-Python-Client
    appium（另開 terminal: appium）
    手機 Wi-Fi proxy 設為 Mac IP:8080（跟 Charles 同 port）
"""

import os
import re
import subprocess
import sys
import time

from appium import webdriver
from appium.options.ios.xcuitest.base import XCUITestOptions

FLAG_FILE = "/tmp/appier_hit"
BUNDLE_ID = "com.appier.Random"
APPIUM_SERVER = "http://127.0.0.1:4723"
AD_WAIT_SEC = 2.0
MAX_ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 30


def detect_udid():
    if len(sys.argv) > 2:
        return sys.argv[2]
    out = subprocess.check_output(["xcrun", "xctrace", "list", "devices"], text=True)
    # 只抓 == Devices == 區塊（排除 Offline 跟 Simulators）
    devices_section = out.split("== Devices ==")[1].split("==")[0]
    udids = re.findall(r'\(([0-9a-f]{16,40})\)', devices_section)
    macs = re.findall(r'Mac.*\(([A-F0-9-]{36})\)', devices_section)
    udids = [u for u in udids if u not in macs]
    if not udids:
        sys.exit("找不到連接的 iPhone，請接上手機或手動指定 UDID。")
    if len(udids) > 1:
        sys.exit(f"偵測到多台裝置：{udids}\n請執行：python run.py {MAX_ROUNDS} <UDID>")
    print(f"[device] {udids[0]}")
    return udids[0]


# 清掉上一次的 flag
if os.path.exists(FLAG_FILE):
    os.remove(FLAG_FILE)

options = XCUITestOptions()
options.bundle_id = BUNDLE_ID
options.automation_name = "XCUITest"
options.no_reset = True
options.udid = detect_udid()

driver = webdriver.Remote(APPIUM_SERVER, options=options)

try:
    # 如果 app 停在廣告頁，先退回 list
    try:
        driver.find_element("accessibility id", "BackButton").click()
        time.sleep(1.0)
    except Exception:
        pass

    for i in range(1, MAX_ROUNDS + 1):
        print(f"[{i}/{MAX_ROUNDS}] tapping basic ...")

        driver.find_element("accessibility id", "basic").click()
        time.sleep(AD_WAIT_SEC)

        if os.path.exists(FLAG_FILE):
            hit = open(FLAG_FILE).read().strip()
            print(f"\n[STOP] Appier detected — {hit}")
            break

        driver.find_element("accessibility id", "BackButton").click()
        time.sleep(1.0)
    else:
        print(f"\n[DONE] {MAX_ROUNDS} 輪都沒出現 Appier ad。")

finally:
    driver.quit()
