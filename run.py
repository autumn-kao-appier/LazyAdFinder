"""
用法（terminal 2，detector.py 跑起來後才執行）:
    python run.py [最多幾輪，預設 30]

前置：
    pip install Appium-Python-Client
    appium（另開 terminal: appium）
    手機 Wi-Fi proxy 設為 Mac IP:8080（跟 mitmproxy 同 port）
"""

import os
import sys
import time

from appium import webdriver
from appium.options.ios.xcuitest.base import XCUITestOptions

FLAG_FILE = "/tmp/appier_hit"
BUNDLE_ID = "com.appier.Random"
APPIUM_SERVER = "http://127.0.0.1:4723"
BACK_BUTTON_LABEL = "AdMob Mediation"
AD_WAIT_SEC = 2.0
MAX_ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 30

# 清掉上一次的 flag
if os.path.exists(FLAG_FILE):
    os.remove(FLAG_FILE)

options = XCUITestOptions()
options.bundle_id = BUNDLE_ID
options.automation_name = "XCUITest"
options.no_reset = True
options.udid = "00008030-001C68A11E80802E"  # AITA iPhone 11

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
