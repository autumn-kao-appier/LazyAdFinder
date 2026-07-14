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
    TAB        sample app 上方的分頁，預設 "Appier SDK"
               可選："Appier SDK" / "AdMob Mediation" / "AppLovin Mediation"
    AD_LABEL   要點擊的廣告類型文字，預設 "Native - basic format"
               各 tab 的清單（照 sample app source 對照）：
                 Appier SDK:
                   "Video", "Interstitial",
                   "Banner - basic format", "Banner - in a listview", "Banner - in a floating window",
                   "Native - basic format", "Native - in a listview", "Native - in a floating window"
                 AdMob Mediation:
                   "Interstitial",
                   "Banner - basic format", "Banner - in a floating window",
                   "Native - basic format", "Native - in a floating window"
                 AppLovin Mediation（注意大小寫不同）:
                   "Interstitial",
                   "Banner - basic format", "Banner - in a listview", "Banner - in a floating window",
                   "Native - Basic format", "Native - In a ListView", "Native - In a floating window"
    STOP_ON    "win"（預設）= bid response 200 才停（真的有廣告可看）；
               204 no-bid 會顯示後繼續下一輪。
               "bid" = 只要看到 bid request 就停（舊行為）。
"""

import os
import subprocess
import sys
import time

from appium import webdriver
from appium.options.android.uiautomator2.base import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy

FLAG_FILE = "/tmp/appier_hit"
BID_STATUS_FILE = "/tmp/appier_bid_status"
NETWORK_FILE = "/tmp/current_networks"
APP_PACKAGE = "com.appier.android.sample"
APP_ACTIVITY = "com.appier.android.sample.MainActivity"
APPIUM_SERVER = "http://127.0.0.1:4723"
AD_TIMEOUT_SEC = 5.0
BID_STATUS_WAIT_SEC = 2.0
AD_POLL_INTERVAL = 0.1
MAX_ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
TAB = os.environ.get("TAB", "Appier SDK")
AD_LABEL = os.environ.get("AD_LABEL", "Native - basic format")
STOP_ON = os.environ.get("STOP_ON", "win")


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


def find_onscreen(driver, text):
    """回傳畫面內符合文字的元素。

    ViewPager 會預載相鄰分頁，隔壁 tab 的同名項目也在 view hierarchy 裡
    （三個 tab 都有 "Native - basic format" 之類的重複 label），
    所以要用元素座標過濾掉畫面外的。
    TabLayout 的 tab 標題預設 textAllCaps，用 (?i) regex 同時吃兩種寫法。
    """
    import re as _re
    width = driver.get_window_size()["width"]
    elements = driver.find_elements(
        AppiumBy.ANDROID_UIAUTOMATOR,
        f'new UiSelector().textMatches("(?i){_re.escape(text)}")',
    )
    for el in elements:
        loc = el.location
        size = el.size
        center_x = loc["x"] + size["width"] // 2
        if 0 <= center_x < width:
            return el
    return None


def select_tab(driver):
    tab = find_onscreen(driver, TAB)
    if tab is not None:
        tab.click()
        time.sleep(0.5)
        return True
    return False


def ensure_on_list(driver):
    """確保停在 sample app 的目標分頁（AD_LABEL 在畫面內即視為就位）。"""
    for _ in range(4):
        if find_onscreen(driver, AD_LABEL) is not None:
            return
        if select_tab(driver) and find_onscreen(driver, AD_LABEL) is not None:
            return
        try:
            driver.back()
            time.sleep(1.0)
        except Exception:
            return


def read_bid_status():
    """等 bid response 回來，回傳 status code 字串（'200'/'204'）或 None。"""
    deadline = time.monotonic() + BID_STATUS_WAIT_SEC
    while time.monotonic() < deadline:
        if os.path.exists(BID_STATUS_FILE):
            return open(BID_STATUS_FILE).read().strip()
        time.sleep(AD_POLL_INTERVAL)
    return None


# 清掉上一次的 flag / network 紀錄
for f in (FLAG_FILE, BID_STATUS_FILE, NETWORK_FILE):
    if os.path.exists(f):
        os.remove(f)

options = UiAutomator2Options()
options.app_package = APP_PACKAGE
options.app_activity = APP_ACTIVITY
options.no_reset = True
options.udid = detect_udid()

driver = webdriver.Remote(APPIUM_SERVER, options=options)

try:
    print(f"[tab   ] {TAB}")
    print(f"[label ] {AD_LABEL}")
    print(f"[stop  ] {STOP_ON}")
    ensure_on_list(driver)

    for i in range(1, MAX_ROUNDS + 1):
        ensure_on_list(driver)

        for f in (FLAG_FILE, BID_STATUS_FILE, NETWORK_FILE):
            if os.path.exists(f):
                os.remove(f)

        print(f"[{i}/{MAX_ROUNDS}] tapping '{AD_LABEL}' ...")
        el = find_onscreen(driver, AD_LABEL)
        if el is None:
            print(f"[{i}] 畫面上找不到 '{AD_LABEL}'，重試")
            continue
        el.click()

        deadline = time.monotonic() + AD_TIMEOUT_SEC
        # Phase 1: 等第一個 network request 進來
        while time.monotonic() < deadline:
            if os.path.exists(NETWORK_FILE) or os.path.exists(FLAG_FILE):
                break
            time.sleep(AD_POLL_INTERVAL)
        # Phase 2: 剩餘時間繼續等 Appier bid request（可能比其他 network 晚）
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
            status = read_bid_status()
            if status == "200":
                print(f"\n[STOP] Appier ad WON (bid 200) — {hit}")
                print("       request: /tmp/appier_bid.json  response: /tmp/appier_bid_response.json")
                break
            if status == "204":
                if STOP_ON == "bid":
                    print(f"\n[STOP] Appier bid request (204 no-bid) — {hit}")
                    break
                print(f"         → Appier bid 204 no-bid，繼續")
                continue
            # response 沒等到（timeout）——保守起見停下來讓人看
            print(f"\n[STOP] Appier bid request (response 未確認) — {hit}")
            break
    else:
        print(f"\n[DONE] {MAX_ROUNDS} 輪都沒出現 Appier ad。")

finally:
    driver.quit()
