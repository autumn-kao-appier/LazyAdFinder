"""
用法:
    python run_android.py [最多幾輪，預設 30] [UDID]

    UDID 省略時自動偵測唯一連接的 Android 裝置。
    多台裝置時必須指定：python run_android.py 30 emulator-5554

前置：
    pip install Appium-Python-Client
    appium（另開 terminal: appium）
    手機 Wi-Fi proxy 設為 Mac IP:8888（Charles），Charles upstream proxy 設為 127.0.0.1:8081（mitmdump）

環境變數（選填）：
    AD_LABEL   要點擊的廣告類型文字，預設 "Native - basic format"
               可選值對應 sample app 清單（Native）：
                 "Native - basic format"
                 "Native - in a listview"
                 "Native - in a floating window"
               其他格式：
                 "Banner - basic format"
                 "Banner - in a listview"
                 "Banner - in a floating window"
                 "Interstitial"
                 "Video"
"""

import os
import subprocess
import sys
import time

from appium import webdriver
from appium.options.android.uiautomator2.base import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy

FLAG_FILE = "/tmp/appier_hit"
NETWORK_FILE = "/tmp/current_networks"
APP_PACKAGE = "com.appier.android.sample"
APP_ACTIVITY = "com.appier.android.sample.MainActivity"
APPIUM_SERVER = "http://127.0.0.1:4723"
AD_TIMEOUT_SEC = 5.0
AD_POLL_INTERVAL = 0.1
MAX_ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
AD_LABEL = os.environ.get("AD_LABEL", "Native - basic format")


def detect_udid():
    if len(sys.argv) > 2:
        return sys.argv[2]
    out = subprocess.check_output(["adb", "devices"], text=True)
    devices = [
        line.split()[0]
        for line in out.splitlines()
        if line.strip() and not line.startswith("List") and line.split()[-1] == "device"
    ]
    if not devices:
        sys.exit("找不到連接的 Android 裝置，請確認 USB 偵錯已開啟。")
    if len(devices) > 1:
        sys.exit(f"偵測到多台裝置：{devices}\n請執行：python run_android.py {MAX_ROUNDS} <UDID>")
    print(f"[device] {devices[0]}")
    return devices[0]


def ensure_on_list(driver):
    """確保停在 Appier SDK 清單頁（能找到 AD_LABEL 文字即視為在列表）。"""
    for _ in range(3):
        try:
            driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR,
                                f'new UiSelector().text("{AD_LABEL}")')
            return
        except Exception:
            try:
                driver.back()
                time.sleep(1.0)
            except Exception:
                return


# 清掉上一次的 flag / network 紀錄
for f in (FLAG_FILE, NETWORK_FILE):
    if os.path.exists(f):
        os.remove(f)

options = UiAutomator2Options()
options.app_package = APP_PACKAGE
options.app_activity = APP_ACTIVITY
options.no_reset = True
options.udid = detect_udid()

driver = webdriver.Remote(APPIUM_SERVER, options=options)

try:
    ensure_on_list(driver)

    for i in range(1, MAX_ROUNDS + 1):
        ensure_on_list(driver)

        if os.path.exists(NETWORK_FILE):
            os.remove(NETWORK_FILE)

        print(f"[{i}/{MAX_ROUNDS}] tapping '{AD_LABEL}' ...")
        try:
            driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR,
                                f'new UiSelector().text("{AD_LABEL}")').click()
        except Exception:
            print(f"[{i}] 找不到 '{AD_LABEL}'，重試")
            continue

        deadline = time.monotonic() + AD_TIMEOUT_SEC
        # Phase 1: 等第一個 network request 進來
        while time.monotonic() < deadline:
            if os.path.exists(NETWORK_FILE) or os.path.exists(FLAG_FILE):
                break
            time.sleep(AD_POLL_INTERVAL)
        # Phase 2: 剩餘時間繼續等 Appier request（可能比其他 network 晚）
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
