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
from appium.options import XCUITestOptions

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
# options.udid = "YOUR_DEVICE_UDID"  # 多台裝置時取消注解

driver = webdriver.Remote(APPIUM_SERVER, options=options)

try:
    for i in range(1, MAX_ROUNDS + 1):
        print(f"[{i}/{MAX_ROUNDS}] tapping basic ...")

        driver.find_element("accessibility id", "basic").click()
        time.sleep(AD_WAIT_SEC)

        if os.path.exists(FLAG_FILE):
            hit = open(FLAG_FILE).read().strip()
            print(f"\n[STOP] Appier detected — {hit}")
            print("查 mitmweb: http://127.0.0.1:8081")
            break

        # 回上一頁
        driver.find_element("accessibility id", BACK_BUTTON_LABEL).click()
        time.sleep(0.5)
    else:
        print(f"\n[DONE] {MAX_ROUNDS} 輪都沒出現 Appier ad。")

finally:
    driver.quit()
