#!/usr/bin/env python3
"""
build_artifact.py — 把一個 round 的 evidence 資料夾渲染成自包含 HTML report。

一條 TC 一張 evidence card：TC 定義、可驗度、expected vs actual、判定、
bid 欄位實際值、該 capture 的手機截圖（點開放大），以及「如何把手機設成
這個狀態 + 截圖該證明什麼」的重現步驟。涵蓋 bid_inspector 的全部 80 條 checks。

用法:
    python build_artifact.py <round_dir> [--out report.html]

    <round_dir>  evidence/<round>/ ；掃描底下每個 capture 子資料夾（含 baseline）
                 的 results.json / bid_request.json / phone.png。

判定分級:
    PASS      已 capture 且值符合規格
    FAIL      已 capture 且值違反規格（真缺陷；SDK 未實作會標 RD gap）
    PENDING   狀態類 TC，尚未在該 TC 要求的裝置狀態下 capture（需補抓）
    BLOCKED   受限於硬體/環境無法執行（無 SIM、需非 root 機、latency endpoint）
"""

import argparse
import base64
import glob
import html
import io
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image
except Exception:
    Image = None


def encode_shot(path, max_w=720, quality=78):
    """縮圖 + JPEG 編碼成 data URI，控制 artifact 體積（原尺寸 PNG 會爆 16MB 上限）。

    720px 寬足以肉眼讀設定頁的開關/文字；無 Pillow 時退回原檔 base64。
    """
    if Image is None:
        return "data:image/png;base64," + base64.b64encode(open(path, "rb").read()).decode()
    im = Image.open(path).convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

sys.path.insert(0, str(Path(__file__).parent))
from bid_inspector import (  # noqa: E402
    ANDROID_OS_RE,
    BCP47_RE,
    CELL_4G5G_RE,
    INPUT_LANG_RE,
    ISO639_RE,
    SEMVER_RE,
    UUID_RE,
    VALIDATORS,
    _unwrap,
    run_inspection,
)


# Golden Schema v8 (2026-07-09):
# https://appier.atlassian.net/wiki/spaces/AMT/pages/5215421112
#
# Cards deliberately keep schema facts separate from TC expectations.  The
# latter may reflect the current Android implementation (and can therefore use
# different units while an SDK/Swagger mismatch is under investigation).
FIELD_SCHEMA = {
    "device.ia": ("廣告 ID（GAID / IDFA）", "string",
                  "小寫 UUID；opt-in（AND-01）必須有值且非全零；全 0／缺席只在 opt-out（AND-02/76）才是預期",
                  "與 device.lat 連動"),
    "device.ifv": ("Vendor ID（App Set ID / IDFV）", "string", "UUID", "跨啟動應一致；不在 device.ext 下"),
    "device.lat": ("Limit Ad Tracking 旗標", "integer", "0 或 1", "1=拒絕追蹤；不是緯度"),
    "device.ext.darkmode": ("深色模式", "boolean", "true / false", ""),
    "device.charging": ("充電狀態", "integer", "0 / 1", "Golden 表格文字仍列 true/false；Swagger/SDK 實際為 integer"),
    "device.ext.battery_saver": ("省電模式", "boolean", "true / false", "底線命名"),
    "device.ext.jailbreak": ("Root / 越獄狀態", "boolean", "true / false", ""),
    "device.ext.emulator": ("模擬器偵測", "boolean", "true / false", ""),
    "device.ext.vpn": ("VPN 狀態", "string", "on=非空協定字串；off=absent/empty/null", "backend 型別＝string；依核准 TC 標準，不把 null 泛化成 boolean false"),
    "device.batterylevel": ("電量", "integer", "0–100", "不在 device.ext 下"),
    "device.ext.screen_bright": ("螢幕亮度", "number", "0.0–1.0", "底線命名"),
    "device.ext.fontscale": ("字體縮放比例", "number", "1.0 為預設值", ""),
    "device.ext.volume": ("音量", "number", "0.0–1.0", ""),
    "device.utcoffset": ("時區位移", "integer", "分鐘；UTC+8 = 480", "不在 device.geo 下"),
    "device.lang": ("語言代碼", "string", "ISO 639-1；2 碼小寫，如 en", "不含地區"),
    "device.langb": ("語言代碼（含地區）", "string", "BCP 47，如 en-US", ""),
    "device.input_lang": ("鍵盤輸入語言清單", "array(string)", "元素為 BCP 47", "Golden path 寫作 device.input_lang[]"),
    "app.ver": ("App 版本", "string", "semver，如 1.2.3", "無 v 前綴、三段數字"),
    "app.displaymanager": ("SDK 識別字串", "string", "非空且不含版本號", "確切預期值待 RD 確認"),
    "app.displaymanagerver": ("SDK Build 版本", "string", "與本輪 test build 一致", "不同於 app.sdk_version"),
    "device.make": ("製造商", "string", "非空，如 samsung / Apple", ""),
    "device.model": ("機型", "string", "非空，如 SM-S928B", "iOS 為硬體代碼，不是行銷名稱"),
    "device.ip": ("IPv4 位址", "string", "合法 IPv4 且非 0.0.0.0", "由 IPv4 echo ingress 取得"),
    "device.ipv6": ("IPv6 位址", "string", "合法 IPv6", "通常需真正支援 IPv6 的 4G/5G 網路"),
    "device.conntype": ("連線類型", "string", "SDK 值如 wifi / cellular_4g / cellular_5g", "Golden 的 OpenRTB 數字 enum 與 SDK 實作不一致"),
    "app.bundle": ("App Bundle ID", "string", "與 applicationId / CFBundleIdentifier 一致", ""),
    "app.sdk_version": ("Ads SDK 版本", "string", "semver，如 2.2.0", "ext 實際值來源仍須留意 data-signal SDK"),
    "ext.app.sdk_version": ("Ads SDK 版本", "string", "必須與 req.app.sdk_version 相同", "同一 request 內兩個 SDK version path 必須一致"),
    "device.type": ("Device Type", "string", "enum 值未載明", "需與 RD 確認"),
    "device.os": ("OS 名稱", "string", "Android 或 iOS", "大小寫待確認"),
    "device.osv": ("OS 版本", "string", "與裝置實際版本一致", ""),
    "device.hwv": ("硬體版本", "string", "定義未載明", "需與 RD 確認對應系統屬性"),
    "device.country": ("國家代碼", "string", "ISO 3166-1 alpha-2 / alpha-3", "確切格式待確認"),
    "device.locale": ("Locale", "string", "格式待確認", "與 langb 是否重複待確認"),
    "device.ext.gyroscope": ("陀螺儀陣列", "array(number)", "純數字陣列", "不是 {time,x,y,z}；元素語意待確認"),
    "device.ext.accelerometer": ("加速度計陣列", "array(number)", "純數字陣列", "元素語意待確認"),
    "device.ext.boottime": ("開機事件時間戳記", "array(integer)", "1–5 個 epoch-ms timestamps", "BootEventCollector 保留最近 5 次；serializer 輸出 JSONArray"),
    "device.geo_lat": ("緯度", "number", "-90.0–90.0", "扁平掛在 device 下；沒有 geo.type"),
    "device.geo_lon": ("經度", "number", "-180.0–180.0", "扁平掛在 device 下"),
    "user.session_duration": ("Session 時長", "integer", "毫秒；≥ 0",
                              "語意＝使用者 App 在前景的累積時間（iOS 實作即此語意），"
                              "非廣告 session 載入時間；行為以 bid A→B 對照驗證（AND-47-1/2/3）"),
    "user.app_init_time": ("App 初始化時間", "integer", "ms timestamp，接近當下 epoch", "不在 user.ext 下"),
    "user.last_foreground_time": ("前景時間戳記陣列", "array(integer)", "ms timestamp", "Golden path 寫作 []"),
    "user.last_background_time": ("背景時間戳記陣列", "array(integer)", "ms timestamp", "Golden path 寫作 []"),
    "user.impression_history": ("Impression 歷史", "array(object)", "wintime/displaytime 等欄位", "含 clicktime/backgroundtime/storeviewtime 陣列"),
    "user.app_duration": ("App 使用時長", "integer", "秒；≥ 0", "目前 SDK 實作/TC 以毫秒驗證，屬規格差異"),
    "device.ext.mem_total": ("總記憶體", "integer", "正整數 MB", "目前 Android collector/TC 以 bytes 驗證"),
    "device.ext.mem_available": ("可用記憶體", "integer", "正整數 MB；≤ mem_total", "目前 Android collector/TC 以 bytes 驗證"),
    "device.ext.disk_total": ("總硬碟空間", "integer", "正整數 MB", "Golden：僅 Android；目前 collector/TC 以 bytes 驗證"),
    "device.ext.disk_free": ("可用硬碟空間", "integer", "正整數 MB；≤ disk_total", "Golden：僅 Android；目前 collector/TC 以 bytes 驗證"),
    "device.sw": ("螢幕寬度", "integer", "正整數，與裝置規格一致", ""),
    "device.sh": ("螢幕高度", "integer", "正整數，與裝置規格一致", ""),
    "device.ppi": ("PPI", "integer", "正整數，與裝置規格一致", ""),
    "device.pxratio": ("Pixel Ratio", "number", "正浮點數；常見 2.0–3.5", ""),
    "device.ext.latency": ("網路延遲", "integer", "正整數 ms；> 0", "SDK 自行量測 RTT；echo endpoint 可作對照目標"),
    "device.ext.applist": ("已安裝 App 清單", "array(string)", "Publisher-provided", "SDK 無法自行完整採集"),
    "device.ext.iaphistory": ("App 內購商品 ID", "array(string)", "可為空陣列；有購買時列 product IDs", "合併 INAPP 與 SUBS，去重後輸出"),
    "device.carrier": ("Carrier 名稱", "string", "如 VERIZON", "無 SIM 時缺席"),
    "device.mccmnc": ("Mobile Country / Network Code", "string", "如 311-480", "無 SIM 時缺席"),
    "app.ext.islatestver": ("不存在的舊規格欄位", "—", "必須缺席", "Golden v2 的 app 物件沒有 ext"),
    "device.operator": ("Operator 識別碼", "string", "格式待確認", "無 SIM 時缺席"),
    "device.operator_name": ("Operator 名稱", "string", "格式待確認", "無 SIM 時缺席"),
    "skadn.sourceapp": ("SKAN 來源 App", "string", "iOS-only", "Android request 應缺席"),
    "skadn.versions": ("SKAN 版本清單", "array(string)", "iOS-only", "Golden path 寫作 []"),
    "skadn.skadnetids": ("SKAN 網路 ID 清單", "array(string)", "如 xxxxxxxxxx.skadnetwork", "iOS-only；Golden path 寫作 []"),
}


# ── TC 分類 / 可驗度 / 狀態重現 metadata ────────────────────────────────────────

CATEGORIES = {
    "A": "Core Identifiers",
    "B": "Device State — Bool",
    "C": "Device State — Numeric",
    "D": "Device / App — Format",
    "E": "Device State — Arrays",
    "F": "Geolocation",
    "G": "In-Session",
    "H": "Memory / Disk",
    "I": "Screen / Display",
    "J": "Negative / Absent",
    "K": "Network Latency",
    "L": "Privacy Compliance",
    "M": "SKAdNetwork",
    "N": "Request Envelope",
}

# TC → 分類字母（依 sheet Cat A–M）
CAT_OF = {
    "AND-01": "A", "AND-02": "A", "AND-03": "A", "AND-28": "D", "AND-29": "D",
    "AND-75": "A", "AND-76": "A",
    "AND-04": "B", "AND-05": "B", "AND-06": "B", "AND-07": "B", "AND-08": "B",
    "AND-09": "B", "AND-10": "B", "AND-11": "B", "AND-12": "B", "AND-13": "B",
    "AND-14": "B", "AND-15": "B",
    "AND-16": "C", "AND-17": "C", "AND-19": "C", "AND-20": "C", "AND-21": "C",
    "AND-22": "C", "AND-23": "C", "AND-24": "C", "AND-25": "C", "AND-26": "C",
    "AND-27": "C",
    "AND-30": "D", "AND-31": "D", "AND-32": "D", "AND-33": "D", "AND-34": "D",
    "AND-35": "D", "AND-36": "D", "AND-37": "D", "AND-38": "D", "AND-39": "D",
    "AND-40": "D", "AND-41": "D", "AND-66": "D", "AND-67": "D", "AND-68": "D",
    "AND-69": "D", "AND-70": "D", "AND-71": "D", "AND-73": "D", "AND-74": "D",
    "AND-42": "E", "AND-43": "E", "AND-44": "E",
    "AND-45": "F", "AND-46": "F",
    "AND-47-1": "G", "AND-47-2": "G", "AND-47-3": "G",
    "AND-48": "G", "AND-49": "G", "AND-50": "G", "AND-51": "G",
    "AND-52": "G",
    "AND-53": "H", "AND-54": "H", "AND-55": "H", "AND-56": "H",
    "AND-57": "I", "AND-58": "I", "AND-59": "I", "AND-60": "I",
    "AND-61": "K",
    "AND-62": "J", "AND-63": "J", "AND-64": "J", "AND-65": "J", "AND-72": "J",
    "AND-81": "M",
    "AND-77": "L", "AND-78": "L", "AND-79": "L", "AND-80": "L",
    "AND-82": "N", "AND-83": "N", "AND-84": "N",
}

# 狀態類 TC：group（互斥組）+ 如何設定 + 截圖該證明什麼
STATE = {
    "AND-01": ("tracking", "維持廣告追蹤開啟（未 opt out）", "系統「廣告」設定頁顯示廣告 ID 存在（可與 bid 的 ia 對照）"),
    "AND-02": ("tracking", "在系統設定刪除廣告 ID（opt out）", "系統「廣告」設定頁顯示廣告 ID 已刪除"),
    "AND-75": ("tracking", "維持廣告追蹤開啟", "廣告設定頁顯示追蹤未受限（lat=0）"),
    "AND-76": ("tracking", "刪除廣告 ID（opt out）", "廣告設定頁顯示追蹤已受限（lat=1）"),
    "AND-04": ("darkmode", "開啟深色主題", "顯示設定頁的「深色主題」開關為開"),
    "AND-05": ("darkmode", "關閉深色主題", "顯示設定頁的「深色主題」開關為關"),
    "AND-06": ("charging", "接上電源充電", "電池頁顯示充電中"),
    "AND-07": ("charging", "拔除電源（未充電）", "電池頁顯示未充電"),
    "AND-08": ("batterysaver", "開啟省電模式", "省電模式開關為開"),
    "AND-09": ("batterysaver", "關閉省電模式", "省電模式開關為關"),
    "AND-10": ("jailbreak", "使用未 root 裝置", "裝置未 root"),
    "AND-11": ("jailbreak", "使用已 root 裝置", "裝置已 root（Magisk / root checker 佐證）"),
    "AND-12": ("emulator", "在模擬器（AVD）執行", "模擬器畫面"),
    "AND-13": ("emulator", "在實體機執行", "實體機畫面"),
    "AND-14": ("vpn", "連上 VPN", "VPN 設定頁列出服務，且狀態列顯示 VPN key 圖示"),
    "AND-15": ("vpn", "不連 VPN", "VPN 設定頁與狀態列均無已連線／key 圖示"),
    "AND-16": ("batterylevel", "電量充至滿（100%）", "電池頁電量百分比"),
    "AND-17": ("batterylevel", "電量降至極低（0%）", "電池頁電量百分比"),
    "AND-19": ("screenbright", "螢幕亮度調到最低", "亮度滑桿在最低"),
    "AND-20": ("screenbright", "螢幕亮度調到最高", "亮度滑桿在最高"),
    "AND-21": ("fontscale", "字體大小設為預設", "字體大小為標準"),
    "AND-22": ("fontscale", "字體大小設為最大", "文字明顯放大"),
    "AND-23": ("volume", "媒體音量調到靜音", "音量面板 media 為 0"),
    "AND-24": ("volume", "媒體音量調到最大", "音量面板 media 為滿"),
    "AND-25": ("tz", "時區設為台北（GMT+8）", "日期時間設定顯示 GMT+8"),
    "AND-26": ("tz", "時區設為紐約（EST/EDT）", "日期時間設定顯示美東時區"),
    "AND-27": ("tz", "時區設為 UTC / 倫敦", "日期時間設定顯示 GMT+0"),
    "AND-31": ("locale", "系統語言與地區設為 English (United States)", "語言設定頁顯示 English (United States)"),
    "AND-45": ("geo", "授予 Sample App 定位權限並開啟 GPS", "Sample App 詳情頁的 Permissions 摘要顯示 Location 已允許"),
    "AND-46": ("geo", "拒絕 Sample App 定位權限", "Sample App 詳情頁的 Permissions 摘要顯示 Location 未允許"),
    "AND-47-1": ("session", "進廣告（bid A）→ 只關廣告頁（App 全程前景）→ 再觸發（bid B）", "session 累進：B > A"),
    "AND-47-2": ("session", "進廣告（bid A）→ force-stop 關整個 App 重開 → 再觸發（bid B）", "session 重置：B < A"),
    "AND-47-3": ("session", "進廣告（bid A）→ 退背景數秒切回前景 → 再觸發（bid B）", "session 累進：B > A"),
    "AND-48": ("session", "冷啟動後立即觸發廣告", "App 剛啟動"),
    "AND-50": ("fgbg", "把 App 切到背景再回前景", "App 有背景→前景切換"),
    "AND-52": ("session", "App 前景累積使用超過 30 秒", "App 已使用一段時間"),
    # 裝置固有欄位（B 類）：值在系統畫面看得到，一頁涵蓋多條 → 實機每條盡量有截圖
    "AND-36": ("deviceinfo", "開啟 設定 → 關於手機", "品牌 Google"),
    "AND-37": ("deviceinfo", "開啟 設定 → 關於手機", "型號 Pixel 10a"),
    "AND-69": ("deviceinfo", "開啟 設定 → 關於手機", "作業系統 Android"),
    "AND-70": ("deviceinfo", "開啟 設定 → 關於手機", "Android 版本 16"),
    "AND-71": ("deviceinfo", "開啟 設定 → 關於手機", "硬體版本 Pixel 10a"),
    "AND-30": ("language", "開啟 設定 → 語言與地區", "語言 English（en）"),
    "AND-73": ("language", "開啟 設定 → 語言與地區", "地區 台灣（tw）"),
    "AND-74": ("language", "開啟 設定 → 語言與地區", "locale en_US"),
    "AND-32": ("language", "開啟 設定 → 語言與地區", "輸入語言清單"),
    "AND-55": ("storage", "開啟 設定 → 儲存空間", "總容量"),
    "AND-56": ("storage", "開啟 設定 → 儲存空間", "可用空間"),
    "AND-53": ("storage", "開啟 設定 → 儲存空間", "RAM 總量（情境佐證）"),
    "AND-54": ("storage", "開啟 設定 → 儲存空間", "RAM 可用（情境佐證）"),
    "AND-62": ("apps", "開啟 設定 → 應用程式", "已安裝應用程式清單"),
    "AND-40": ("network", "開啟 設定 → Wi-Fi", "連線類型 Wi-Fi"),
    "AND-38": ("network", "開啟 設定 → Wi-Fi", "IP 位址（情境佐證）"),
    "AND-33": ("appinfo", "開啟 Sample App 詳情頁", "app 版本 1.4.0"),
    "AND-66": ("appinfo", "開啟 Sample App 詳情頁", "套件名 com.appier.android.sample"),
}

# adb 可自動設定/模擬該狀態的指令（鏡像 manual_wizard.py 實際流程），
# 寫進「應有值」卡片供人重現。battery 類設定會持續 → 附還原指令。
BATTERY_RESET = "adb shell dumpsys battery reset"
MOCK_CMD = {
    "AND-04": "adb shell cmd uimode night yes",
    "AND-05": "adb shell cmd uimode night no",
    "AND-06": "adb shell dumpsys battery set ac 1",
    "AND-07": "adb shell dumpsys battery unplug",
    "AND-08": "adb shell cmd power set-mode 1",
    "AND-09": "adb shell cmd power set-mode 0",
    "AND-16": "adb shell dumpsys battery set level 100",
    "AND-17": "adb shell dumpsys battery set level 0",
    "AND-19": ("adb shell settings put system screen_brightness_mode 0\n"
               "adb shell settings put system screen_brightness 0"),
    "AND-20": ("adb shell settings put system screen_brightness_mode 0\n"
               "adb shell settings put system screen_brightness 255"),
    "AND-21": "adb shell settings put system font_scale 1.0",
    "AND-22": "adb shell settings put system font_scale 1.5",
    "AND-23": "adb shell cmd audio set-volume 3 0",
    "AND-24": "adb shell cmd audio set-volume 3 <max>",
    "AND-25": "adb shell cmd alarm set-timezone Asia/Taipei",
    "AND-26": "adb shell cmd alarm set-timezone America/New_York",
    "AND-27": "adb shell cmd alarm set-timezone UTC",
    "AND-31": ("adb shell cmd locale set-app-locales com.appier.android.sample "
               "--user 0 --locales en-US"),
    "AND-45": ("adb shell pm grant com.appier.android.sample "
               "android.permission.ACCESS_FINE_LOCATION"),
    "AND-46": ("adb shell pm revoke com.appier.android.sample "
               "android.permission.ACCESS_FINE_LOCATION"),
}
# 這些狀態靠 adb 設定會持續，capture 後要還原
MOCK_NEEDS_RESET = {"AND-06", "AND-07", "AND-16", "AND-17"}

# 必須手動、adb 無法自動達成的狀態 → 後續測試者要驗這幾條時照這裡做
MANUAL = {}

# 環境/硬體限制無法執行
BLOCKED = {
    "AND-10": "需要非 root 裝置或 non-rooted AVD",
    "AND-12": "emulator=true 需在 Android 模擬器（AVD）另跑一輪",
    "AND-38": "Not in this Release",
    "AND-39": "device.ext IPv6 需 4G/5G SIM 實機；辦公室 WiFi 無 IPv6 路由，團隊暫無 SIM 機",
    "AND-41": "cellular conntype 需 SIM 實機；團隊暫無 SIM 機",
    "AND-42": "感應器固定值／操作方式未定，標準要求先跳過",
    "AND-43": "感應器固定值／操作方式未定，標準要求先跳過",
    "AND-51": "impression_history 尚未實作，標準列為 Block",
    "AND-61": "Echo Server 僅回 IP，無法量測 latency；待 RD 提供其他方式",
    "AND-64": "無 SIM 機；SIM 模擬能力未確認（R5）",
    # session 三情境：缺 capture 表示該情境 round 尚未跑
    "AND-47-1": "需跑 SESSION_CASE=1 round（run_ssp；bid A→關廣告頁→bid B 對照）",
    "AND-47-2": "需跑 SESSION_CASE=2 round（run_ssp；bid A→force-stop 重開→bid B 對照）",
    "AND-47-3": "需跑 SESSION_CASE=3 round（run_ssp；bid A→背景切回→bid B 對照）",
}

# 規格自身互相矛盾／expected 尚未定義；即使有 Capture 也不能判產品 Fail。
SPEC_BLOCKED = set()

# SDK 尚未實作 → 值恆為 null/[]，FAIL 屬 RD gap 而非執行問題
RD_GAP = {
    "AND-14": "SDK 目前 vpn 恆為 null",
}

# REEN ↔ GAID opt-out 互斥：opt-out 後 REEN campaign 不出價（204 no-bid），
# 抓不到指定 CID → opt-out 狀態 TC 在 REEN 輪標 N/A，改走 AIBID 輪驗
TYPE_NA_REEN = {
    "AND-02": "GAID opt-out 與 REEN 互斥（opt-out → 204 no-bid，抓不到指定 CID）；此 TC 走 AIBID 輪",
    "AND-76": "lat=1（opt-out）與 REEN 互斥（opt-out → 204 no-bid，抓不到指定 CID）；此 TC 走 AIBID 輪",
}

# E2E cases require evidence beyond a bid request. Never infer PASS from the
# signal payload alone; this table states the evidence gate for the current
# standalone round.
E2E_CASES = [
    ("TC-01", "BLOCKED", "自動化證據缺口：本 Round 未保存 init request/response；需讓代理自動保存 HTTP 200 response 並核對 bundle/sdk_version"),
    ("TC-02", "BLOCKED", "AdMob pubsetting；本輪為 Standalone"),
    ("TC-03", "BLOCKED", "AdMob mads/gma mediation；本輪為 Standalone"),
    ("TC-04", "BLOCKED", "自動化證據缺口：有 /v2/sdk/aos/ad request，但未保存 HTTP 200 response body，無法核對 adUnits[0].ad"),
    ("TC-05", "BLOCKED", "自動化證據缺口：未保存 icon/main/privacy asset 的 HTTP 200/304 network trace"),
    ("TC-06", "BLOCKED", "自動化證據缺口：已有指定 CID 畫面，但缺 response native content，無法完成畫面逐項比對"),
    ("TC-07", "BLOCKED", "自動化證據缺口：有 show_cb request/rc=200，但未保存 show_cb→winshowimg redirect chain 與 identity"),
    ("TC-08", "BLOCKED", "AdMob mediation fill_urls；本輪為 Standalone"),
    ("TC-09", "BLOCKED", "需手動／成本核准：執行一次受控點擊並保存完整 xclk redirect chain；未獲核准前自動流程不得點擊"),
    ("TC-10", "BLOCKED", "需手動：確認 REEN deeplink 直開 target app product page，並保存落地畫面／錄影"),
    ("TC-11", "BLOCKED", "需手動：排除 SSL proxy 後點擊 privacy icon，保存 privacyInformationLink trace 與落地畫面"),
    ("TC-14", "BLOCKED", "需手動／成本核准：保存 click、OneLink macro 展開、deeplink redirect chain 與落地畫面"),
    ("TC-15", "BLOCKED", "本 workspace 無 Spark raw_action / MMP 證據；需由有權限者匯出 attribution 結果並以 bidobjid/CID/CRID 對照"),
    ("TC-16", "BLOCKED", "AdMob fallback/nofill_urls；本輪為 Standalone"),
]

# 可驗度分級
ABSENT_CHECKS = {"absent", "absent_or_empty", "falsy", "value_or_absent"}
PARTIAL_CHECKS = {"range", "positive_int", "positive_float", "nonempty",
                  "nonempty_notunknown", "array_nonempty", "array_number",
                  "array_regex", "array_impression", "leq_field",
                  "timestamp_recent", "truthy", "ipv4_nonzero", "regex"}


def tier_of(check, tc):
    if tc in BLOCKED:
        return "Blocked"
    if check in ABSENT_CHECKS:
        return "Absent"
    if check in PARTIAL_CHECKS:
        return "Partial"
    return "Verifiable"


# 互斥組 → [(tc, expected), ...]，供 PENDING 判定
def build_groups():
    groups = {}
    exp = {}
    for v in VALIDATORS:
        exp.setdefault(v["tc"], v.get("expected"))
    for tc, (grp, _, _) in STATE.items():
        groups.setdefault(grp, []).append(tc)
    return groups, exp


# ── evidence 掃描 ───────────────────────────────────────────────────────────────

def load_captures(round_dir):
    """回傳 {capture_name: {"bid": obj, "shot_b64": str|None, "ts": str}}。"""
    caps = {}
    for results_path in glob.glob(os.path.join(round_dir, "*", "results.json")):
        folder = os.path.dirname(results_path)
        name = os.path.basename(folder)
        bid = None
        bid_path = os.path.join(folder, "bid_request.json")
        if os.path.exists(bid_path):
            try:
                bid = json.load(open(bid_path))
            except Exception:
                bid = None
        first_bid = None
        first_bid_path = os.path.join(folder, "first_bid_request.json")
        if os.path.exists(first_bid_path):
            try:
                first_bid = json.load(open(first_bid_path))
            except Exception:
                first_bid = None
        shot_path = os.path.join(folder, "phone.png")
        shot_path = shot_path if os.path.exists(shot_path) else None
        # state-proof：看得見該狀態的系統畫面（肉眼證據），優先於 app 截圖
        proof_paths = {}
        proof_caps = {}
        for p in glob.glob(os.path.join(folder, "state_proof_*.png")):
            group = os.path.basename(p)[len("state_proof_"):-len(".png")]
            proof_paths[group] = p
        # 向後相容舊 evidence 的單張 state_proof.png。
        legacy_proof = os.path.join(folder, "state_proof.png")
        if os.path.exists(legacy_proof):
            proof_paths["legacy"] = legacy_proof
        caps_path = os.path.join(folder, "state_proof_captions.json")
        if os.path.exists(caps_path):
            try:
                proof_caps = json.load(open(caps_path))
            except Exception:
                proof_caps = {}
        cap_path = os.path.join(folder, "state_proof_caption.txt")
        if os.path.exists(cap_path):
            proof_caps["legacy"] = open(cap_path).read().strip()
        # 本次實際執行了什麼（實機設定 / adb 模擬 real→mock）
        action = None
        act_path = os.path.join(folder, "state_action.txt")
        if os.path.exists(act_path):
            action = open(act_path).read().strip()
        # 本次 bid 識別碼（比廣告截圖有意義）
        bid_ids = None
        ids_path = os.path.join(folder, "bid_ids.json")
        if os.path.exists(ids_path):
            try:
                bid_ids = json.load(open(ids_path))
            except Exception:
                bid_ids = None
        # session 三情境對照（run_ssp SESSION_CASE 產出；AND-47-1/2/3）
        session_case = None
        sc_path = os.path.join(folder, "session_case.json")
        if os.path.exists(sc_path):
            try:
                session_case = json.load(open(sc_path))
            except Exception:
                session_case = None
        ts = ""
        stored = {}
        executed_tcs = set()
        test_type = ""
        test_mode = ""
        test_cid = ""
        test_executor = ""
        environment = {}
        try:
            data = json.load(open(results_path))
            tc_id = data.get("tc_id", "")
            executed_tcs = {item.strip() for item in tc_id.split(",") if item.strip()}
            ts = data.get("captured_at", "")
            test_type = data.get("test_type", "")
            test_mode = data.get("test_mode", "")
            test_cid = data.get("test_cid", "")
            test_executor = data.get("test_executor", "")
            environment = data.get("environment", {})
            # capture 當下算的結果才是權威（時間敏感的 check 事後重算會失真）
            stored = {(r["tc"], r["field"]): r for r in data.get("results", [])}
        except Exception:
            pass
        environment_path = os.path.join(folder, "environment.json")
        if os.path.exists(environment_path):
            try:
                environment.update(json.load(open(environment_path)))
            except Exception:
                pass
        caps[name] = {"bid": bid, "first_bid": first_bid,
                      "shot_path": shot_path, "ts": ts,
                      "captured_at_ms": os.path.getmtime(bid_path) * 1000 if os.path.exists(bid_path) else None,
                      "folder": name, "stored": stored,
                      "proof_paths": proof_paths, "proof_caps": proof_caps,
                      "action": action, "bid_ids": bid_ids, "session_case": session_case,
                      "test_type": test_type,
                      "test_mode": test_mode, "test_cid": test_cid}
        caps[name]["executed_tcs"] = executed_tcs
        caps[name]["test_executor"] = test_executor
        caps[name]["environment"] = environment
    return caps


M1_TCS = {"AND-01", "AND-75", "AND-05", "AND-07", "AND-09", "AND-10",
          "AND-13", "AND-15", "AND-16", "AND-19", "AND-21", "AND-23",
          "AND-25", "AND-31", "AND-45", "AND-48"}
M2_TCS = {"AND-02", "AND-76", "AND-04", "AND-08", "AND-14", "AND-17", "AND-20", "AND-22", "AND-24",
          "AND-26", "AND-46", "AND-50", "AND-52"}
M3_TCS = {"AND-06", "AND-27"}
AUTO_TCS = {
    "AND-01", "AND-28", "AND-03", "AND-29", "AND-75", "AND-11",
    "AND-30", "AND-32", "AND-33", "AND-34", "AND-35",
    "AND-36", "AND-37", "AND-38", "AND-39", "AND-40", "AND-66", "AND-67",
    "AND-68", "AND-69", "AND-70", "AND-71", "AND-72", "AND-73", "AND-74",
    "AND-44", "AND-49", "AND-53", "AND-54", "AND-55", "AND-56",
    "AND-57", "AND-58", "AND-59", "AND-60", "AND-62", "AND-63", "AND-64",
    "AND-42", "AND-43", "AND-51", "AND-61",
    "AND-77", "AND-78", "AND-79", "AND-80", "AND-81", "AND-82", "AND-83", "AND-84",
}


def expected_capture_label(tc):
    if tc in M1_TCS:
        return "M1"
    if tc in M2_TCS:
        return "M2"
    if tc in M3_TCS:
        return "M3"
    return "AUTO" if tc in AUTO_TCS else None


def capture_state_eligible(tc, cap):
    """Gate a Capture with independent device ground truth before bid comparison."""
    env = cap.get("environment", {})
    root = str(env.get("root", "")).lower()
    fingerprint = str(env.get("build_fingerprint", "")).lower()
    battery = str(env.get("battery", "")).lower()
    proofs = cap.get("proof_paths", {})
    checks = {
        "AND-04": lambda: "night mode: yes" in str(env.get("dark_mode", "")).lower(),
        "AND-05": lambda: "night mode: no" in str(env.get("dark_mode", "")).lower(),
        "AND-06": lambda: any(x in battery for x in ("ac powered: true", "usb powered: true", "wireless powered: true")),
        "AND-07": lambda: all(x not in battery for x in ("ac powered: true", "usb powered: true", "wireless powered: true")),
        "AND-08": lambda: str(env.get("battery_saver", "")) == "1",
        "AND-09": lambda: str(env.get("battery_saver", "")) == "0",
        "AND-10": lambda: root.startswith("not rooted") or root.startswith("unrooted"),
        "AND-11": lambda: root.startswith("rooted"),
        "AND-12": lambda: any(x in fingerprint for x in ("generic", "emulator", "sdk_gphone", "vbox")),
        "AND-13": lambda: bool(fingerprint) and not any(x in fingerprint for x in ("generic", "emulator", "sdk_gphone", "vbox")),
        "AND-39": lambda: bool(env.get("public_ipv6")),
        "AND-14": lambda: env.get("vpn_active") is True and "vpn" in proofs,
        "AND-15": lambda: env.get("vpn_active") is False and "vpn" in proofs,
        "AND-16": lambda: bool(re.search(r"\blevel:\s*100\b", battery)),
        "AND-17": lambda: bool(re.search(r"\blevel:\s*0\b", battery)),
        "AND-19": lambda: str(env.get("brightness", "")).isdigit() and int(env["brightness"]) <= 26,
        "AND-20": lambda: str(env.get("brightness", "")).isdigit() and int(env["brightness"]) >= 230,
        "AND-21": lambda: str(env.get("font_scale", "")) in {"1", "1.0"},
        "AND-22": lambda: str(env.get("font_scale", "")) == "1.5",
        "AND-25": lambda: env.get("timezone") == "Asia/Taipei",
        "AND-26": lambda: env.get("timezone") == "America/New_York",
        "AND-27": lambda: env.get("timezone") in {"UTC", "Etc/UTC", "Europe/London"},
        "AND-31": lambda: "en-us" in str(env.get("app_locale", "")).lower() and "locale" in proofs,
        "AND-23": lambda: bool(re.fullmatch(r"0/\d+", str(env.get("media_volume", "")))) and "volume" in proofs,
        "AND-24": lambda: (bool(re.fullmatch(r"(\d+)/(\d+)", str(env.get("media_volume", ""))))
                            and str(env.get("media_volume", "")).split("/")[0]
                            == str(env.get("media_volume", "")).split("/")[1]
                            and "volume" in proofs),
    }
    check = checks.get(tc)
    if check is None:
        return True
    try:
        return bool(check())
    except (TypeError, ValueError):
        return False


def pick_capture(tc, caps):
    """Pick the latest capture that actually established this TC's state."""
    matches = capture_candidates(tc, caps)
    return matches[-1] if matches else None


def capture_candidates(tc, caps):
    """All phase-matched attempts, including *_RETRYn Captures."""
    def declared(name):
        declared_tcs = caps[name].get("executed_tcs", set())
        return tc in declared_tcs or ("BASELINE" in declared_tcs and tc in AUTO_TCS)

    matches = sorted(n for n in caps
                     if (n.startswith(tc + "_") or n.startswith(tc.replace("-", "") + "_"))
                     and declared(n)
                     and capture_state_eligible(tc, caps[n]))
    if matches:
        return matches
    label = expected_capture_label(tc)
    if label is None:
        return []
    return sorted(n for n in caps
                  if n.startswith(label + "_")
                  and declared(n)
                  and capture_state_eligible(tc, caps[n]))


# ── 判定 ─────────────────────────────────────────────────────────────────────────

def classify(tc, check, passed, actual, groups, exp, targeted, has_capture,
             failed_attempts=0):
    """有 Capture 就硬比 expected/actual；只有無 Capture 才分類未執行原因。"""
    # 一旦有 Capture，判定只由 expected vs actual 決定。MANUAL/BLOCKED
    # 只能描述「尚未取得可判讀 Capture」，不得覆蓋 FAIL 或 PASS。
    if tc in SPEC_BLOCKED:
        return "BLOCKED", None
    if has_capture:
        if passed:
            return "PASS", None
        if failed_attempts >= 2:
            return "FAIL", RD_GAP.get(tc)
        return "BLOCKED", None
    if tc in BLOCKED:
        return "BLOCKED", None
    if tc in MANUAL:
        return "MANUAL", None
    return "BLOCKED", None


# ── HTML ─────────────────────────────────────────────────────────────────────────

STATUS_META = {
    "PASS":    ("pass", "Pass"),
    "FAIL":    ("fail", "Fail"),
    "PENDING": ("pending", "Pending capture"),
    "MANUAL":  ("manual", "需手動驗證"),
    "BLOCKED": ("blocked", "Blocked"),
}


def esc(x):
    return html.escape(str(x), quote=True)


def fmt_val(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        s = json.dumps(v, ensure_ascii=False)
        return s if len(s) <= 120 else s[:117] + "…"
    return str(v)


def device_kind_of(environment):
    """實體機 / 模擬機。優先讀 environment.device_kind（新 capture 已存）；
    舊 capture 沒有此欄位時，從 build_fingerprint / device 型號回推。"""
    kind = (environment or {}).get("device_kind")
    if kind in ("實體機", "模擬機"):
        return kind
    fp = str((environment or {}).get("build_fingerprint", "")).lower()
    model = str((environment or {}).get("device", "")).lower()
    is_emu = ("sdk_gphone" in model
              or any(tok in fp for tok in ("emu", "sdk_gphone", "/generic")))
    return "模擬機" if is_emu else "實體機"


def provenance_label(environment):
    """把裝置類型與狀態來源合成一個 SOURCE 標籤：
    實體機(REAL) / 實體機(MOCK) / 模擬機(REAL) / 模擬機(MOCK)。"""
    kind = device_kind_of(environment)
    raw = str((environment or {}).get("battery_source", "")).upper()
    src = "MOCK" if "MOCK" in raw else "REAL"
    return f"{kind}({src})"


def ground_truth_for(field, environment):
    """Return the independent device/app evidence saved beside this Capture."""
    mapping = {
        "app.bundle": ("environment.json · package", "package"),
        "app.ver": ("environment.json · version_name", "version_name"),
        "device.model": ("environment.json · device", "device"),
        "device.osv": ("environment.json · android", "android"),
        "device.langb": ("environment.json · app_locale", "app_locale"),
        "device.ext.darkmode": ("environment.json · dark_mode", "dark_mode"),
        "device.ext.battery_saver": ("environment.json · battery_saver", "battery_saver"),
        "device.ext.screen_bright": ("environment.json · brightness (0–255)", "brightness"),
        "device.ext.fontscale": ("environment.json · font_scale", "font_scale"),
        "device.charging": ("environment.json · battery", "battery"),
        "device.batterylevel": ("environment.json · battery", "battery"),
        "device.ext.jailbreak": ("environment.json · root", "root"),
        "device.utcoffset": ("environment.json · timezone", "timezone"),
    }
    item = mapping.get(field)
    if not item:
        return None
    label, key = item
    value = environment.get(key)
    if field in {"device.charging", "device.batterylevel"} and value not in (None, ""):
        value = f"{value} | source={environment.get('battery_source', 'UNKNOWN')}"
    return {"label": label, "value": value} if value not in (None, "") else None


def read_round_elapsed(round_dir):
    """讀 round_timing.txt（run_ssp 每次跑完累寫一行）→ 回總耗時字串。
    多行取最後一次成功（exit=0）的；無檔或無成功行回 None。"""
    path = os.path.join(round_dir, "round_timing.txt")
    if not os.path.exists(path):
        return None
    lines = [l.strip() for l in open(path) if l.strip()]
    ok = [l for l in lines if "exit=0" in l]
    pick = ok[-1] if ok else (lines[-1] if lines else None)
    if not pick:
        return None
    # 格式：<date> <time>  <TC>  <XmYYs>  exit=N
    parts = pick.split()
    for tok in parts:
        if tok.endswith("s") and ("m" in tok or tok[:-1].isdigit()):
            return tok
    return None


# flow 段落順序（與 e2e_catalog.FLOW_STEPS 對齊）；判「跑到哪一段 / 卡在哪」
_FLOW_ORDER = [
    ("init",       "① SDK Init"),
    ("bid",        "② Bid 請求/回應"),
    ("render",     "③ 廣告渲染"),
    ("impression", "④ Impression 回報"),
    ("click",      "⑤ 點擊"),
    ("landing",    "⑥ 落地"),
]


def compute_round_progress(e2e_data):
    """依 E2E 各段狀態判定這一輪跑到哪 / 卡在哪。
    回 {'complete': bool, 'reached': '⑥ 落地', 'stall': None|'④ Impression', 'label': str}。
    段落狀態：只要該段有 TC pass/observe 視為「有進展」；全 pending/fail 視為未達。"""
    by_step = {}
    for row in e2e_data or []:
        by_step.setdefault(row.get("step", ""), []).append(row.get("status"))

    def step_reached(key):
        st = by_step.get(key, [])
        # 該段沒有任何適用 TC（全 na_*）→ 不擋流程，視為 N/A 跳過
        applicable = [s for s in st if s not in ("na_mode", "na_type", "na_platform")]
        if not applicable:
            return None  # N/A：此 mode/type 無此段
        return any(s in ("pass", "observe") for s in applicable)

    reached_label, stall_label = None, None
    for key, label in _FLOW_ORDER:
        r = step_reached(key)
        if r is None:
            continue          # N/A 段跳過
        if r:
            reached_label = label
        elif stall_label is None:
            stall_label = label   # 第一個「有適用 TC 但未達」的段 = 卡關點
    complete = stall_label is None and reached_label == _FLOW_ORDER[-1][1]
    if complete:
        label = f"完整跑完 · 最後到 {reached_label}"
    elif stall_label:
        label = f"未完整 · 卡在 {stall_label}（已完成到 {reached_label or '—'}）"
    else:
        label = f"部分完成 · 到 {reached_label or '—'}"
    return {"complete": complete, "reached": reached_label,
            "stall": stall_label, "label": label}


def build(round_dir, out_path, e2e_round=None):
    caps = load_captures(round_dir)
    if not caps:
        sys.exit(f"no capture (results.json) found under {round_dir}")
    groups, exp = build_groups()
    round_name = os.path.basename(round_dir.rstrip("/"))
    # test_type 在組卡片前就要知道：REEN 輪的 opt-out TC（TYPE_NA_REEN）要標 N/A
    test_type = next((c["test_type"] for c in caps.values()
                      if c.get("test_type") and c["test_type"] != "unspecified"), "")

    # Always recompute from bid_request.json using the current approved rules.
    # Stored results.json may have been produced by an older, incorrect validator.
    cap_results = {}
    first_cap_results = {}
    for name, c in caps.items():
        if c["bid"] is not None:
            cap_results[name] = {(r["tc"], r["field"]): r
                                 for r in run_inspection(
                                     c["bid"], reference_ms=c.get("captured_at_ms"))}
        if c.get("first_bid") is not None:
            first_cap_results[name] = {(r["tc"], r["field"]): r
                                       for r in run_inspection(
                                           c["first_bid"], reference_ms=c.get("captured_at_ms"))}

    # session 三情境（AND-47-1/2/3）：跨 bid 對照，單一 bid 驗不了 →
    # 從 capture 的 session_case.json（run_ssp 當下記錄的 A/B 值）合成結果列
    for name, c in caps.items():
        sc = c.get("session_case")
        if not sc:
            continue
        tc_id = sc.get("tc") or f"AND-47-{sc.get('case')}"
        a, b = sc.get("session_a"), sc.get("session_b")
        verdict = sc.get("passed")
        msg = f"預期 {sc.get('expected', '')}"
        if verdict is None:
            msg += "；session 值缺失（A/B 其一未取得），無法對照"
        cap_results.setdefault(name, {})[(tc_id, "user.session_duration")] = {
            "tc": tc_id, "field": "user.session_duration",
            "passed": bool(verdict),
            "actual": f"A={a} → B={b} ms",
            "msg": msg,
            "note": sc.get("action", ""),
        }

    cards = []
    counts = {"PASS": 0, "FAIL": 0, "PENDING": 0, "MANUAL": 0, "BLOCKED": 0}
    for v in VALIDATORS:
        tc, field, check = v["tc"], v["field"], v["check"]
        cat = CAT_OF.get(tc, "D")
        cap_name = pick_capture(tc, caps)
        expected_label = expected_capture_label(tc)
        targeted = bool(cap_name and expected_label and cap_name.startswith(expected_label + "_"))
        # first_bid_request.json 若存在可提供更精確的 cold-start 證據；若不存在，
        # 不得把已存在的 regular Capture 隱藏成 Pending，仍以實際收到值判 Pass/Fail。
        result_source = (first_cap_results
                         if tc in {"AND-48", "AND-49"} and cap_name in first_cap_results
                         else cap_results)
        res = result_source.get(cap_name, {}).get((tc, field))
        passed = res["passed"] if res else False
        actual = res["actual"] if res else None
        attempts = []
        for attempt_name in capture_candidates(tc, caps):
            attempt_source = (first_cap_results
                              if tc in {"AND-48", "AND-49"} and attempt_name in first_cap_results
                              else cap_results)
            attempt_result = attempt_source.get(attempt_name, {}).get((tc, field))
            if attempt_result is not None:
                attempts.append({"capture": attempt_name,
                                 "passed": attempt_result["passed"],
                                 "actual": fmt_val(attempt_result["actual"]),
                                 "msg": attempt_result["msg"]})
        failed_attempts = sum(not item["passed"] for item in attempts)
        status, rd_note = classify(tc, check, passed, actual, groups, exp, targeted,
                                   res is not None, failed_attempts)
        counts[status] += 1
        # REEN 輪的 opt-out TC：無 capture 不是缺證據，是投放目的互斥 → 標 N/A
        # （計數仍歸 Blocked tile，與 E2E 的 na_* 處理一致）
        type_na_reason = (TYPE_NA_REEN.get(tc)
                          if (status == "BLOCKED" and res is None
                              and (test_type or "").startswith("reen"))
                          else None)

        expected = v.get("expected", None)
        if "pattern" in v:
            pattern_labels = {
                UUID_RE.pattern: "Valid UUID (8-4-4-4-12)",
                BCP47_RE.pattern: "Valid BCP 47 language tag",
                INPUT_LANG_RE.pattern: "Valid language / locale code",
                ISO639_RE.pattern: "ISO 639-1 lowercase code",
                CELL_4G5G_RE.pattern: "cellular_4g or cellular_5g",
                SEMVER_RE.pattern: "Semantic version (x.y.z)",
                ANDROID_OS_RE.pattern: "Android",
            }
            expected_disp = pattern_labels.get(v["pattern"].pattern, "Matches required format")
        elif "min" in v:
            expected_disp = f"{v['min']} … {v['max']}"
        elif check == "equals_field" and res is not None:
            expected_disp = res["msg"].removeprefix("expected ")
        elif check in ABSENT_CHECKS:
            expected_disp = "absent / empty"
        elif expected is not None:
            expected_disp = fmt_val(expected)
        else:
            expected_disp = check.replace("_", " ")

        st = STATE.get(tc)
        # 「應有值＝absent/empty」的負向互斥卡：寫清楚「為什麼預期沒值」，
        # 否則正反例只看到 absent 會困惑（值是被哪個狀態關掉的、截圖看哪裡）
        absent_reason = None
        if check in ABSENT_CHECKS and st:
            absent_reason = {"set": st[1], "shows": st[2]}
        signal, schema_type, schema_format, schema_note = FIELD_SCHEMA.get(
            field, (field, "—", "Golden Schema 未列", "")
        )
        actual_disp = fmt_val(actual) if res else "—"
        if status == "PASS":
            evidence_explanation = (
                f"同一個 Capture 的 bid_request.json 中，{field} = {actual_disp}；"
                f"符合本 TC 預期 {expected_disp}，因此判定 Pass。"
            )
        elif status == "FAIL":
            evidence_explanation = (
                f"同一個 Capture 的 bid_request.json 中，{field} = {actual_disp}；"
                f"不符合本 TC 預期 {expected_disp}，因此判定 Fail。"
            )
        elif status == "PENDING":
            evidence_explanation = (
                f"目前 Capture 讀到 {field} = {actual_disp}，但尚無足夠證據確認裝置已處於本 TC 要求狀態；"
                "需補同一次 Capture 的狀態截圖與 bid request 後才能判定。"
            )
        elif status == "MANUAL":
            evidence_explanation = (
                f"此狀態無法由自動化可靠設定。需人工完成指定狀態，並在同一次 Capture 留下設定截圖；"
                f"再以 bid_request.json 的 {field} 對照 {expected_disp}。"
            )
        else:
            if tc in SPEC_BLOCKED:
                evidence_explanation = (
                    f"Capture 讀到 {field} = {actual_disp}，但 TC expected 與 Golden/SDK 定義互相衝突；"
                    "在規格定案前不可判產品 Pass/Fail，因此判定 Blocked。"
                )
            elif res is not None:
                evidence_explanation = (
                    f"目前只有 {failed_attempts} 次 mismatch；完成態規則要求自動 Retry 後仍 mismatch "
                    "才能定案 Fail。本 Round 缺少後續 Retry Capture，因此判定 Blocked。"
                )
            elif type_na_reason:
                evidence_explanation = (
                    f"本輪 TEST_TYPE={test_type}；{type_na_reason}。"
                    "非缺證據，本輪判定 N/A。"
                )
            else:
                evidence_explanation = (
                    "本 Round 沒有符合測試前提的 Capture；"
                    "無 actual 可比對，因此判定 Blocked。"
                )
        cap = caps.get(cap_name, {})
        proof_group = st[0] if st else None
        proof_paths = cap.get("proof_paths", {})
        proof_key = proof_group if proof_group in proof_paths else (
            "legacy" if "legacy" in proof_paths else None
        )
        has_proof = bool(proof_key)
        if has_proof:
            # 只保留「看得見狀態」的設定頁截圖；廣告畫面 phone.png 不再當證據（改用 bidobjid）
            shot_key = cap_name + "::proof::" + proof_key
            shot_caption = cap.get("proof_caps", {}).get(proof_key) or "狀態證據截圖"
            shot_matched = True
        else:
            shot_key = None
            shot_caption = None
            shot_matched = False
        cards.append({
            "tc": tc, "field": field, "cat": cat,
            "round": round_name,
            "signal": signal, "schema_type": schema_type,
            "schema_format": schema_format, "schema_note": schema_note,
            "tier": tier_of(check, tc),
            "status": status, "status_cls": STATUS_META[status][0],
            "status_label": ("N/A（投放目的不適用）" if type_na_reason
                             else STATUS_META[status][1]),
            "type_na": bool(type_na_reason),
            "condition": v.get("note", "") or f"{field} — {check}",
            "expected": expected_disp,
            "actual": actual_disp,
            "evidence_explanation": evidence_explanation,
            "rd_note": rd_note,
            "blocked_reason": ((type_na_reason or BLOCKED.get(tc) or
                                ("首次 mismatch 後缺少自動 Retry Capture"
                                 if res is not None else
                                 "本 round 缺少符合測試前提的 capture（狀態未建立或未執行）"))
                               if status == "BLOCKED" else None),
            "set": st[1] if st else None,
            "shows": st[2] if st else None,
            "absent_reason": absent_reason,
            "mock_cmd": MOCK_CMD.get(tc),
            "mock_reset": tc in MOCK_NEEDS_RESET,
            "action": cap.get("action"),
            "bid_ids": cap.get("bid_ids"),
            "manual_hint": MANUAL.get(tc),
            "shot": shot_key,
            "shot_caption": shot_caption,
            "shot_matched": shot_matched,
            "capture": cap_name,
            # SOURCE 標籤：一律揭露來源裝置（實體機／模擬機）；
            # 可 adb-mock 的狀態（電量/充電）再補 (REAL/MOCK)
            "provenance": (
                provenance_label(cap.get("environment", {}))
                if field in {"device.batterylevel", "device.charging"}
                else (device_kind_of(cap["environment"])
                      if cap.get("environment") else None)),
            "ground_truth": ground_truth_for(field, cap.get("environment", {})),
            "attempts": attempts,
        })

    # Header counts are unique TCs, not validator rows. Multi-field TCs such as
    # geo lat/lon remain separate assertions inside one TC status.
    precedence = {"PASS": 0, "PENDING": 1, "MANUAL": 2, "FAIL": 3, "BLOCKED": 4}
    tc_status = {}
    for card in cards:
        old = tc_status.get(card["tc"])
        if old is None or precedence[card["status"]] > precedence[old]:
            tc_status[card["tc"]] = card["status"]
    counts = {key: sum(1 for value in tc_status.values() if value == key)
              for key in counts}
    total = len(tc_status)
    verified = counts["PASS"] + counts["FAIL"]
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 跨 capture 一致性：同一顆裝置每次 capture 的 ia / ifv 應恆定
    consistency = []
    latest_caps = []
    for label in ("AUTO", "M1", "M2", "M3"):
        names = sorted(name for name in caps if name.startswith(label + "_"))
        if names:
            latest_caps.append(caps[names[-1]])
    for label, field in (("GAID (device.ia)", "ia"), ("App Set ID (device.ifv)", "ifv")):
        vals = []
        for c in latest_caps:
            if c["bid"]:
                v = _unwrap(c["bid"]).get("device", {}).get(field)
                if v:
                    vals.append(v)
        distinct = sorted(set(vals))
        ok = len(distinct) == 1 and len(vals) > 1
        consistency.append({
            "label": label, "n": len(vals), "distinct": len(distinct),
            "ok": ok, "value": distinct[0] if distinct else "—",
        })

    # 截圖 data URI：狀態證據＝「該狀態的設定頁、切換後」的截圖（state_proof_<group>.png）
    shots_js = {}
    for name, c in caps.items():
        for group, proof_path in c.get("proof_paths", {}).items():
            shots_js[name + "::proof::" + group] = encode_shot(proof_path)
    latest_auto = sorted(name for name in caps if name.startswith("AUTO_"))
    e2e_ad_shot = None
    if latest_auto:
        auto_cap = caps[latest_auto[-1]]
        if auto_cap.get("shot_path") and os.path.exists(auto_cap["shot_path"]):
            e2e_ad_shot = "e2e::ad-render"
            shots_js[e2e_ad_shot] = encode_shot(auto_cap["shot_path"])

    # 圖片直接寫入各 card 的 <img src="data:...">；不依賴 JS lazy-load。
    for card in cards:
        card["shot_data"] = shots_js.get(card.get("shot"))

    # 分類分組
    by_cat = {}
    for c in cards:
        by_cat.setdefault(c["cat"], []).append(c)

    # 從任一 capture 的 bid 取裝置型號
    model = "Android"
    for c in caps.values():
        if c["bid"]:
            dev = _unwrap(c["bid"]).get("device", {})
            model = dev.get("model") or model
            break
    # 本輪測試類型（aibid / reen-static / reen-dynamic）
    test_mode = next((c["test_mode"] for c in caps.values()
                      if c.get("test_mode") and c["test_mode"] != "unspecified"), "")
    test_cid = next((c["test_cid"] for c in caps.values() if c.get("test_cid")), "")
    test_executor = next((c["test_executor"] for c in caps.values()
                          if c.get("test_executor")), "")
    environment = next((c["environment"] for c in caps.values()
                        if c.get("environment")), {})

    # E2E：用「當前」的 e2e_catalog validators 重新評估（跟 signal 一樣即時重算）。
    # e2e_round 指定時，E2E 從「另一個 round」評估（讓完整報告的 E2E 來自專跑 flow 的 round，
    # signal 仍來自本 round）→ 一份報告涵蓋兩者。
    e2e_src = e2e_round or round_dir
    e2e_data = None
    if test_type and test_mode:
        try:
            from e2e_catalog import evaluate as _e2e_eval
            e2e_data = _e2e_eval(e2e_src, test_mode, test_type)
        except Exception:
            e2e_data = None
    if e2e_data is None:
        e2e_path = os.path.join(e2e_src, "e2e_results.json")
        if os.path.exists(e2e_path):
            try:
                e2e_data = json.load(open(e2e_path)).get("results")
            except Exception:
                e2e_data = None
    if e2e_data is None:
        e2e_data = [{"tc": tc, "name": "", "priority": "", "status": "pending",
                     "note": reason, "evidence": []}
                    for tc, _status, reason in E2E_CASES]

    # 逐步截圖：把 e2e_step_*.png（step_shot 相對路徑）編成 data URI 掛到每列，
    # 供 E2E 分頁時間軸顯示；還沒重跑產圖時 step_shot 為 None，時間軸標「待截圖」
    for row in e2e_data:
        rel = row.get("step_shot")
        if rel:
            full = os.path.join(e2e_src, rel)
            if os.path.exists(full):
                row["step_shot_uri"] = encode_shot(full)

    elapsed = read_round_elapsed(round_dir)
    progress = compute_round_progress(e2e_data)
    html_out = render_html(round_name, generated, counts, total, verified,
                            by_cat, shots_js, model, consistency, test_type, test_mode, test_cid,
                            test_executor, environment, e2e_ad_shot, e2e_data, elapsed, progress)
    Path(out_path).write_text(html_out, encoding="utf-8")
    print(f"→ {out_path}")
    e2e_counts = _e2e_bucket_counts(e2e_data, counts)
    combined = {key: counts[key] + e2e_counts[key] for key in counts}
    print(f"  {total + len(e2e_data)} TCs: {combined['PASS']} pass / {combined['FAIL']} fail "
          f"/ {combined['PENDING']} pending / {combined['MANUAL']} manual / {combined['BLOCKED']} blocked "
          f"({total} Signal / {len(e2e_data)} E2E)")
    # E2E 三態計分（跟 E2E 分頁 scorecard 同一套規則）
    e2e_score = {k: 0 for k in E2E_SCORE_ORDER}
    for row in e2e_data:
        e2e_score[E2E_SCORE.get(row["status"], "BLOCKED")] += 1
    return {
        "out": out_path,
        "round_name": round_name,
        "test_type": test_type,
        "test_mode": test_mode,
        "test_cid": test_cid,
        "test_executor": test_executor,
        "model": model,
        "elapsed": elapsed,
        "signal_total": total,
        "signal_counts": dict(counts),
        "e2e_total": len(e2e_data),
        "e2e_score": e2e_score,
    }


# E2E status → 統計桶（舊版合併統計用；tabs 版 E2E 改走 E2E_SCORE）
E2E_BUCKET = {"pass": "PASS", "fail": "FAIL", "observe": "PENDING", "pending": "PENDING",
              "gated": "BLOCKED", "na_mode": "BLOCKED", "na_type": "BLOCKED",
              "na_platform": "BLOCKED", "backend": "BLOCKED"}


def _e2e_bucket_counts(e2e_data, counts):
    buckets = {key: 0 for key in counts}
    for row in e2e_data:
        buckets[E2E_BUCKET.get(row["status"], "PENDING")] += 1
    return buckets


# E2E 狀態統一成跟 Signal 一樣的三種：PASS / FAILED / BLOCKED（詳細原因留在說明文字）
# observe（有截圖佐證）算 PASS；na/backend/未執行 都歸 BLOCKED（暫時無法自動判定，說明會寫原因）
E2E_SCORE = {"pass": "PASS", "observe": "PASS", "fail": "FAILED",
             "pending": "BLOCKED", "backend": "BLOCKED", "gated": "BLOCKED",
             "na_mode": "BLOCKED", "na_type": "BLOCKED", "na_platform": "BLOCKED"}
E2E_SCORE_ORDER = ["PASS", "FAILED", "BLOCKED"]
E2E_SCORE_CLS = {"PASS": "pass", "FAILED": "fail", "BLOCKED": "blocked"}
# E2E 狀態 → 徽章色 class（三種）
E2E_STATUS_CLS = {"pass": "pass", "observe": "pass", "fail": "fail",
                  "pending": "blocked", "backend": "blocked", "gated": "blocked",
                  "na_mode": "blocked", "na_type": "blocked", "na_platform": "blocked"}
# 徽章文字也統一三種
E2E_BADGE_LABEL = {"pass": "PASS", "observe": "PASS", "fail": "FAILED",
                   "pending": "BLOCKED", "backend": "BLOCKED", "gated": "BLOCKED",
                   "na_mode": "BLOCKED", "na_type": "BLOCKED", "na_platform": "BLOCKED"}


def render_e2e_pane(e2e_data, test_mode, test_type):
    """E2E 分頁：獨立 scorecard + 依廣告流程步驟排的時間軸，每步一列含逐步截圖。"""
    from e2e_catalog import STATUS_LABEL as E2E_LABEL, FLOW_STEPS, STEP_OF
    # 舊 e2e_results.json 沒有 step 欄位 → 用 STEP_OF 補上（不必等重跑）
    for r in e2e_data:
        if not r.get("step"):
            r["step"] = STEP_OF.get(r["tc"], "")
    # scorecard
    score = {k: 0 for k in E2E_SCORE_ORDER}
    for r in e2e_data:
        score[E2E_SCORE.get(r["status"], "BLOCKED")] += 1
    score_tiles = "".join(
        f'<div class="e2e-tile e2e-t-{E2E_SCORE_CLS[k]}">'
        f'<span class="e2e-tile-n">{score[k]}</span><span class="e2e-tile-l">{esc(k)}</span></div>'
        for k in E2E_SCORE_ORDER if score[k] > 0)

    # 依流程步驟分組（FLOW_STEPS 順序）
    by_step = {}
    for r in e2e_data:
        by_step.setdefault(r.get("step") or "other", []).append(r)

    step_blocks = []
    for key, title, desc in FLOW_STEPS:
        rows = by_step.get(key, [])
        if not rows:
            continue
        row_html = []
        for r in rows:
            cls = E2E_STATUS_CLS.get(r["status"], "blocked")
            label = E2E_BADGE_LABEL.get(r["status"], "BLOCKED")
            shot = (f'<button class="shot e2e-step-shot" data-shot="e2e::{esc(r["tc"])}">'
                    f'<img alt="{esc(r["tc"])} step" src="{esc(r["step_shot_uri"])}"></button>'
                    if r.get("step_shot_uri") else
                    '<div class="e2e-noshot">尚無截圖<br><small>重跑 DO_E2E_FLOW 補</small></div>')
            row_html.append(
                f'<div class="e2e-row">'
                f'<div class="e2e-row-shot">{shot}</div>'
                f'<div class="e2e-row-body">'
                f'<div class="e2e-row-head"><span class="e2e-tc">{esc(r["tc"])}</span>'
                f'<b>{esc(r.get("name",""))}</b>'
                f'<span class="e2e-badge e2e-b-{cls}">{esc(label)}</span></div>'
                f'<div class="e2e-kv">'
                f'<div class="e2e-block e2e-expect"><span class="e2e-lbl">應有值</span>'
                f'<div class="e2e-val">{esc(r.get("expected") or "—")}</div>'
                f'<code class="e2e-endpoint">{esc(r.get("endpoint",""))}</code></div>'
                f'<div class="e2e-block e2e-actual"><span class="e2e-lbl">實際 · logcat / proxy</span>'
                f'<div class="e2e-val">{esc(r.get("note",""))}</div></div>'
                f'</div>'
                f'</div></div>')
        step_blocks.append(
            f'<section class="e2e-step"><div class="e2e-step-head">'
            f'<h3>{esc(title)}</h3><span class="e2e-step-desc">{esc(desc)}</span></div>'
            f'<div class="e2e-step-rows">{"".join(row_html)}</div></section>')

    return (
        f'<div class="e2e-scorecard">{score_tiles}</div>'
        f'<p class="lead e2e-lead">E2E 驗整條廣告流程走完，每步一列（截圖＋流量佐證）。'
        f'依 TEST_MODE=<b>{esc(test_mode or "?")}</b> / TEST_TYPE=<b>{esc(test_type or "?")}</b> '
        f'自動判定適用性。狀態同 Signal 三種：<b>PASS</b>（含有截圖佐證的步驟）／<b>FAILED</b>／'
        f'<b>BLOCKED</b>（暫時無法自動判定：本模式不適用、需後端資料、或本輪未跑——原因見每列說明）。</p>'
        f'<div class="e2e-timeline">{"".join(step_blocks)}</div>')


def render_html(round_name, generated, counts, total, verified, by_cat, shots_js, model,
                consistency, test_type="", test_mode="", test_cid="", test_executor="",
                environment=None, e2e_ad_shot=None, e2e_data=None, elapsed=None, progress=None):
    environment = environment or {}
    e2e_data = e2e_data or []
    assertion_count = sum(len(items) for items in by_cat.values())
    e2e_counts = _e2e_bucket_counts(e2e_data, counts)   # 舊 print 相容用
    e2e_total = len(e2e_data)
    # Signal 頂部 tile 只算 Signal（E2E 有自己的 scorecard 在 E2E 分頁）
    tiles = [
        ("Pass", counts["PASS"], "pass"),
        ("Fail", counts["FAIL"], "fail"),
        ("Pending capture", counts["PENDING"], "pending"),
        ("Blocked", counts["BLOCKED"], "blocked"),
    ]
    tiles_html = "".join(
        f'<button class="tile" data-filter="{cls}"><span class="tile-n">{n}</span>'
        f'<span class="tile-l">{esc(label)}</span></button>'
        for label, n, cls in tiles if not (cls in {"manual", "pending"} and n == 0)
    )
    e2e_pane_html = render_e2e_pane(e2e_data, test_mode, test_type)

    sections = []
    for letter in [k for k in CATEGORIES if k in by_cat]:
        cat_cards = by_cat[letter]
        cards_html = "".join(render_card(c) for c in cat_cards)
        sections.append(
            f'<section class="cat" id="cat-{letter}" data-cat="{letter}">'
            f'<h2 class="cat-h"><span class="cat-k">Cat {letter}</span>'
            f'{esc(CATEGORIES[letter])}<span class="cat-n">{len(cat_cards)}</span></h2>'
            f'<div class="grid">{cards_html}</div></section>'
        )
    sections_html = "\n".join(sections)

    # 後續測試者清單只列「本次最終狀態」仍為 Manual/Blocked 的 TC；
    # metadata 內即使留有環境限制說明，也不得把已判 Pass/Fail 的 TC 再列進來。
    tc_status = {}
    status_rank = {"PASS": 0, "PENDING": 1, "MANUAL": 2, "FAIL": 3, "BLOCKED": 4}
    for items in by_cat.values():
        for card in items:
            old = tc_status.get(card["tc"])
            if old is None or status_rank[card["status"]] > status_rank[old]:
                tc_status[card["tc"]] = card["status"]
    manual_now = {tc: hint for tc, hint in MANUAL.items() if tc_status.get(tc) == "MANUAL"}
    # Blocked 面板必須列出「全部」目前 Blocked 的 TC（數字要跟 tile 一致），
    # 依原因分兩類：硬體受限（BLOCKED 表）/ 缺證據（無 eligible capture、缺 Retry…）
    hw_blocked, ev_blocked, na_blocked = {}, {}, {}
    pending_now = {}
    for items in by_cat.values():
        for card in items:
            tc = card["tc"]
            if tc_status.get(tc) == "BLOCKED" and card["status"] == "BLOCKED" \
                    and tc not in hw_blocked and tc not in ev_blocked \
                    and tc not in na_blocked:
                if card.get("type_na"):
                    na_blocked[tc] = (card.get("blocked_reason")
                                      or "本輪投放目的不適用")
                elif tc in BLOCKED:
                    hw_blocked[tc] = BLOCKED[tc]
                else:
                    ev_blocked[tc] = (card.get("blocked_reason")
                                      or "本 round 缺少符合測試前提的 capture")
            if tc_status.get(tc) == "PENDING" and card["status"] == "PENDING" \
                    and tc not in pending_now:
                pending_now[tc] = card.get("evidence_explanation", "等待可判讀的 capture")
    man_rows = "".join(
        f'<tr><td class="mtc">{esc(tc)}</td><td class="mtag mtag-man">需手動</td>'
        f'<td>{esc(hint)}</td></tr>'
        for tc, hint in sorted(manual_now.items()))
    blk_rows = "".join(
        f'<tr><td class="mtc">{esc(tc)}</td><td class="mtag mtag-blk">硬體受限</td>'
        f'<td>{esc(reason)}</td></tr>'
        for tc, reason in sorted(hw_blocked.items()))
    evb_rows = "".join(
        f'<tr><td class="mtc">{esc(tc)}</td><td class="mtag mtag-blk">缺證據</td>'
        f'<td>{esc(reason)}</td></tr>'
        for tc, reason in sorted(ev_blocked.items()))
    nab_rows = "".join(
        f'<tr><td class="mtc">{esc(tc)}</td><td class="mtag mtag-blk">投放目的不適用</td>'
        f'<td>{esc(reason)}</td></tr>'
        for tc, reason in sorted(na_blocked.items()))
    # E2E 已獨立成分頁，此清單只列 Signal TC
    pending_total = len(pending_now)
    pend_rows = "".join(
        f'<tr><td class="mtc">{esc(tc)}</td><td class="mtag mtag-man">Pending</td>'
        f'<td>{esc(reason)}</td></tr>'
        for tc, reason in sorted(pending_now.items()))
    checklist = (
        '<details class="manlist" id="checklist" open><summary>未完成項目與環境限制（Signal）'
        f'（{len(manual_now)} 需手動 · {len(hw_blocked)} 硬體受限 · {len(ev_blocked)} 缺證據'
        f' · {len(na_blocked)} 投放目的不適用 · {pending_total} pending）</summary>'
        f'<p class="manlist-lead">Signal Blocked tile = 本表 '
        f'{len(hw_blocked) + len(ev_blocked) + len(na_blocked)} 個 Signal TC'
        f'（硬體受限＋缺證據＋投放目的不適用）；E2E 未完成項見「E2E」分頁。'
        f'每列附「為什麼沒完成／缺什麼」：</p>'
        '<div class="mwrap"><table class="mtable"><tbody>'
        + man_rows + blk_rows + evb_rows + nab_rows + pend_rows +
        '</tbody></table></div></details>')

    # E2E 已改成獨立分頁（render_e2e_pane），此處不再產舊表格

    # 跨 capture 一致性面板
    con_rows = "".join(
        f'<div class="con-row"><span class="con-ok con-{"y" if c["ok"] else "n"}">'
        f'{"✓" if c["ok"] else "✗"}</span>'
        f'<span class="con-lab">{esc(c["label"])}</span>'
        f'<span class="con-msg">{c["distinct"]} 種值 / {c["n"]} 次 capture'
        f'{" — 跨啟動恆定" if c["ok"] else " — 不一致，需查"}</span>'
        f'<code class="con-val">{esc(c["value"])}</code></div>'
        for c in consistency)
    con_panel = (f'<section class="con"><h2 class="con-h">跨 capture 一致性</h2>'
                 f'<p class="con-lead">同一裝置每次 bid 的識別碼應恆定；用本輪所有 capture 自動比對。</p>'
                 f'{con_rows}</section>')

    shots_json = json.dumps(shots_js)

    # 舊 evidence 沒有 test_type；維持既有 AIBID 標題以確保向後相容。
    default_title = "SDK_AUTOMATION - " + " · ".join(
        x.upper() for x in (test_mode, test_type or "AIBID") if x
    )
    report_title = os.environ.get("REPORT_TITLE", default_title)
    # 完成階段 banner：完整跑完 = 綠；卡關 = 琥珀 + 指出卡在哪段
    progress = progress or {"complete": False, "label": "無 E2E 資料", "stall": None}
    _pg_cls = "ok" if progress.get("complete") else ("stall" if progress.get("stall") else "warn")
    _pg_icon = "✅" if progress.get("complete") else "⚠️"
    progress_banner = (
        f'<div class="progress-banner {_pg_cls}">'
        f'<span class="pg-icon">{_pg_icon}</span>'
        f'<span class="pg-label">本輪流程：{esc(progress.get("label", "—"))}</span>'
        + (f'<span class="pg-stall">卡關段落：{esc(progress["stall"])}</span>'
           if progress.get("stall") else "")
        + "</div>"
    )
    env_rows = "".join(
        f'<div><span>{esc(label)}</span><strong>{esc(environment.get(key, "—"))}</strong></div>'
        for label, key in (("APK", "package"), ("versionName", "version_name"),
                           ("versionCode", "version_code"), ("Device", "device"),
                           ("Android", "android"), ("Build", "build_fingerprint"))
    )
    condition_rows = "".join(
        f'<div><span>{esc(label)}</span><strong>{esc(environment.get(key, "—"))}</strong></div>'
        for label, key in (("Timezone", "timezone"), ("Dark mode", "dark_mode"),
                           ("Battery Saver", "battery_saver"), ("Battery", "battery"),
                           ("Brightness", "brightness"), ("Font scale", "font_scale"),
                           ("Media volume", "media_volume"), ("Root", "root"))
    )
    return f"""<title>{esc(report_title)}</title>
<style>{CSS}</style>
<header class="top">
  <div class="top-in">
    <div class="brand">
      <div class="sig" aria-hidden="true"></div>
      <div>
        <div class="kicker">Appier SDK 開發案 · 自動化測試</div>
        <h1>{esc(report_title)}</h1>
      </div>
    </div>
    <dl class="meta">
      <div><dt>Round</dt><dd>{esc(round_name)}</dd></div>
      <div><dt>類型</dt><dd>{esc(test_type or '—')}</dd></div>
      <div><dt>整合模式</dt><dd>{esc(test_mode or '—')}</dd></div>
      <div><dt>Test CID</dt><dd>{esc(test_cid or '—')}</dd></div>
      <div><dt>執行人</dt><dd>{esc(test_executor or '—')}</dd></div>
      <div><dt>Device</dt><dd>Android · {esc(model)}</dd></div>
      <div><dt>Signal / E2E</dt><dd>{total} / {e2e_total}</dd></div>
      <div><dt>整體耗時</dt><dd>{esc(elapsed or '—')}</dd></div>
      <div><dt>Generated</dt><dd>{esc(generated)}</dd></div>
    </dl>
    {progress_banner}
  </div>
  <div class="tabbar">
    <button class="tabbtn is-on" data-tab="signal">Signal<span class="tabbtn-n">{total}</span></button>
    <button class="tabbtn" data-tab="e2e">E2E<span class="tabbtn-n">{e2e_total}</span></button>
  </div>
  <div class="tiles" data-tabtiles="signal">{tiles_html}
    <button class="tile tile-all is-on" data-filter="all"><span class="tile-n">{total}</span><span class="tile-l">All Signal</span></button>
  </div>
</header>
<main>
  <div class="tab-pane" data-pane="signal">
  <section class="setup-cards">
    <article><h2>測試環境 · APK</h2><div class="setup-grid">{env_rows}</div></article>
    <article><h2>Capture 前置狀態</h2><div class="setup-grid">{condition_rows}</div></article>
  </section>
  <p class="lead"><b>Signal TC {total} 個（展開為 {assertion_count} 個欄位 assertions）。</b><br>
  每張 signal assertion card 都必須顯示精確 JSON path、Golden expected、bid request actual 與 Capture 來源。
  <b>Pass/Fail</b> 代表 assertion 已依 Capture 的 expected/actual 比對；
  <b>Blocked</b> 代表本輪暫時無法執行驗證——例如當輪 RD 尚未上對應 code、硬體/SIM 受限、權限或環境未到位；條件補齊後即可重測。每條的實際原因會在理由中標註。mock 欄位會標明「真實值 → 模擬值」。</p>
  {con_panel}
  {checklist}
  {sections_html}
  </div>
  <div class="tab-pane" data-pane="e2e" hidden>
  {e2e_pane_html}
  </div>
</main>
<div class="lightbox" id="lb" hidden><img alt="evidence screenshot" id="lb-img"><button class="lb-x" id="lb-x" aria-label="close">×</button></div>
<script>{js_block(shots_json, json.dumps(round_name), json.dumps(e2e_counts))}</script>
"""


def render_card(c):
    badges = f'<span class="tier tier-{c["tier"].lower()}">{esc(c["tier"])}</span>'
    if c.get("shot"):
        badges += '<span class="tier">有狀態截圖</span>'
    shot_html = ""
    if c["shot"]:
        matched = "" if c["shot_matched"] else ' data-unmatched="1"'
        cap_lbl = c["shot_caption"] or ""
        # src 直接內嵌 data URI：不依賴 JS 填圖，任何瀏覽器/時序都必定顯示
        shot_html = (f'<button class="shot" data-shot="{esc(c["shot"])}"{matched} '
                     f'title="點擊放大">'
                     f'<img alt="{esc(c["tc"])} screenshot" src="{esc(c.get("shot_data") or "")}">'
                     f'<span class="shot-cap">狀態截圖 — {esc(cap_lbl)}</span></button>')
    repro = ""
    if c["set"]:
        repro = (f'<div class="repro"><div><span class="rl">設定狀態</span>{esc(c["set"])}</div>'
                 f'<div><span class="rl">截圖佐證</span>{esc(c["shows"])}</div></div>')
    note = ""
    if c["rd_note"]:
        note = f'<div class="note note-rd">⚑ RD gap — {esc(c["rd_note"])}</div>'
    elif c["blocked_reason"]:
        note = f'<div class="note note-bl">⛔ {esc(c["blocked_reason"])}</div>'
    elif c.get("manual_hint"):
        note = f'<div class="note note-man">🔧 需手動驗證 — {esc(c["manual_hint"])}</div>'
    action = ""
    if c.get("action"):
        action = f'<div class="action"><span class="rl">本次執行</span>{esc(c["action"])}</div>'
    b = c.get("bid_ids") or {}
    identity_rows = "".join(
        f'<div><span>{key}</span><code>{esc(b.get(key) or "—")}</code></div>'
        for key in ("bidobjid", "cid", "crid", "crpid")
    )

    evidence_source = (
        f'<div class="capture-id"><div class="capture-file"><span>SOURCE</span>'
        f'<code>{esc(c.get("capture") or "—")}/bid_request.json</code></div></div>'
    )
    bid_evidence = (
        f'<div class="bid-evidence"><span class="result-label">CAPTURE BID REQUEST</span>'
        f'<code><b>{esc(c["field"])}</b> = {esc(c["actual"])}</code></div>'
    )
    gt = c.get("ground_truth")
    ground_truth = (
        f'<div class="bid-evidence"><span class="result-label">INDEPENDENT DEVICE / APP EVIDENCE</span>'
        f'<code><b>{esc(gt["label"])}</b> = {esc(gt["value"])}</code></div>'
        if gt else ""
    )
    attempt_rows = "".join(
        f'<div><span>{esc(item["capture"])}</span><code>'
        f'{"MATCH" if item["passed"] else "MISMATCH"} · actual={esc(item["actual"])} · '
        f'{esc(item["msg"])}</code></div>'
        for item in c.get("attempts", []))
    retry_history = (
        f'<div class="identity"><span class="result-label">ATTEMPT / RETRY HISTORY</span>{attempt_rows}</div>'
        if attempt_rows else ""
    )
    if c["shot"]:
        proof_state = '<div class="proof-state proof-ok">✓ 同一 Capture 有狀態截圖</div>'
    elif c.get("set"):
        proof_state = (
            '<div class="proof-state proof-missing">△ 缺少同一 Capture 的狀態截圖；'
            f'補證方式：先{esc(c["set"])}，截取「{esc(c["shows"])}」，再重新 Capture。</div>'
        )
    else:
        proof_state = (
            '<div class="proof-state proof-missing">△ 本卡已有 bid_request 值證據，'
            '但沒有外部／系統畫面對照；需補同次 Capture 的獨立來源證據。</div>'
        )
    # 卡片正面直接顯示截圖狀態（精簡版），不用翻面才知道有沒有佐證
    if c["shot"]:
        proof_front = '<div class="proof-state proof-ok">✓ 同一 Capture 有狀態截圖</div>'
    elif c.get("set"):
        proof_front = '<div class="proof-state proof-missing">△ 缺少同一 Capture 的狀態截圖</div>'
    else:
        proof_front = ""
    # 自動 Blocked 的卡片：原因直接顯示在正面，不用翻面找
    if c["status_cls"] == "blocked" and c.get("blocked_reason"):
        proof_front = (f'<div class="note note-bl">⛔ {esc(c["blocked_reason"])}</div>'
                       + proof_front)

    return f"""<article class="card" data-status="{c['status_cls']}" data-auto="{c['status_cls']}" data-key="{esc(c['tc'])}|{esc(c['field'])}">
  <div class="card-inner">
    <section class="face card-front" aria-label="{esc(c['tc'])} result">
      <div class="card-top">
        <span class="tc">{esc(c['tc'])}</span>
        {badges}
      </div>
      <div class="field">{esc(c['field'])}</div>
      <div class="signal">{esc(c['signal'])}</div>
      <div class="result-kv">
        <div class="result-block golden-block">
          <span class="result-label">應有值</span>
          <strong>{esc(c['expected'])}</strong>
          {f'<div class="absent-why">預期沒有值，是因為：<b>{esc(c["absent_reason"]["set"])}</b><br><span class="absent-shot">狀態截圖佐證：{esc(c["absent_reason"]["shows"])}</span></div>' if c.get('absent_reason') else ''}
          {f'<div class="mock-cmd"><span class="mock-label">Mock 指令（adb 設定此狀態）</span><code>{esc(c["mock_cmd"])}</code>{f"<code class=mock-reset># 還原：{esc(BATTERY_RESET)}</code>" if c.get("mock_reset") else ""}</div>' if c.get('mock_cmd') else ''}
          <small class="schema-ref">Schema · {esc(c['schema_type'])} · {esc(c['schema_format'])}</small>
          {f'<small class="schema-note">{esc(c["schema_note"])}</small>' if c['schema_note'] else ''}
        </div>
        <div class="result-block actual-block">
          <span class="result-label">CAPTURE · 實際收到</span>
          <strong>{esc(c['actual'])}</strong>
          {f'<small class="capture-ref">SOURCE · <b>{esc(c["provenance"])}</b></small>' if c.get('provenance') else ''}
        </div>
      </div>
      <div class="status-result status-{c['status_cls']}">
        <span>RESULT</span><strong>{esc(c['status_label'])}</strong>
      </div>
      {proof_front}
      <div class="edit review-edit">
        <label><span class="rl">人工判定</span>
          <select class="ovr" aria-label="人工覆寫判定">
            <option value="">自動（{esc(c['status_label'])}）</option>
            <option value="pass">Pass</option>
            <option value="fail">Fail</option>
            <option value="pending">Pending</option>
            <option value="blocked">Blocked</option>
          </select>
        </label>
        <label class="reason-label"><span class="rl">理由</span>
          <input class="ovr-note" placeholder="例如：無 SIM，無法驗證 cellular" aria-label="人工覆寫理由">
        </label>
      </div>
      <button class="flip-btn flip-open" type="button">查看 Evidence／狀態截圖 <span aria-hidden="true">↗</span></button>
    </section>
    <section class="face card-back" aria-label="{esc(c['tc'])} evidence">
      <div class="back-head">
        <div><span class="tc">{esc(c['tc'])}</span><span class="back-title">Evidence</span></div>
        <button class="flip-btn flip-close" type="button" aria-label="返回結果">返回結果 ↩</button>
      </div>
      <div class="back-scroll">
        {evidence_source}
        {shot_html}
        {proof_state}
        {bid_evidence}
        {ground_truth}
        {retry_history}
        <div class="bid-identity"><span class="rl">BID IDENTITY</span>{identity_rows}</div>
        <div class="proof-why"><span class="rl">如何證明</span><p>{esc(c['evidence_explanation'])}</p></div>
        <details class="tc-detail"><summary>TC 判定條件與技術備註</summary><p class="cond">{esc(c['condition'])}</p></details>
        {note}
        {action}
        {repro}
      </div>
    </section>
  </div>
</article>"""


def js_block(shots_json, round_json, e2e_counts_json):
    return """
const SHOTS = %s;
const ROUND = %s;
const E2E_COUNTS = %s;
// lazy-set thumbnails
document.querySelectorAll('.shot img[data-src]').forEach(img=>{
  const k=img.getAttribute('data-src'); if(SHOTS[k]) img.src=SHOTS[k];
});
// filter
const tiles=document.querySelectorAll('.tile');
tiles.forEach(t=>t.addEventListener('click',()=>{
  tiles.forEach(x=>x.classList.remove('is-on')); t.classList.add('is-on');
  const f=t.dataset.filter;
  document.querySelectorAll('.card').forEach(c=>{
    c.style.display=(f==='all'||c.dataset.status===f)?'':'none';
  });
  document.querySelectorAll('.cat').forEach(sec=>{
    const any=[...sec.querySelectorAll('.card')].some(c=>c.style.display!=='none');
    sec.style.display=any?'':'none';
  });
  // 點 Pending/Blocked 但 signal 卡片區沒有對應卡（例如 pending 都在 E2E）時，
  // 捲到「未完成項目」面板顯示原因，不留空白頁
  if(f!=='all' && ![...document.querySelectorAll('.card')].some(c=>c.style.display!=='none')){
    const panel=document.getElementById('checklist');
    if(panel){panel.open=true;panel.scrollIntoView({behavior:'smooth',block:'start'});}
  }
}));
// lightbox
const lb=document.getElementById('lb'), lbImg=document.getElementById('lb-img');
document.querySelectorAll('.shot').forEach(s=>s.addEventListener('click',()=>{
  const k=s.dataset.shot;
  // E2E 逐步截圖 img src 是內嵌的（不在 SHOTS）；SHOTS 找不到就用按鈕自己的 img
  let src=SHOTS[k]; if(!src){const im=s.querySelector('img'); src=im&&im.src;}
  if(!src)return;
  lbImg.src=src; lb.hidden=false; lb.classList.add('open');
}));
function closeLb(){lb.classList.remove('open'); lb.hidden=true; lbImg.src='';}
document.getElementById('lb-x').addEventListener('click',closeLb);
lb.addEventListener('click',e=>{if(e.target===lb)closeLb();});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeLb();});

// card flip: front = result, back = evidence
document.querySelectorAll('.card').forEach(card=>{
  card.querySelector('.flip-open')?.addEventListener('click',()=>card.classList.add('is-flipped'));
  card.querySelector('.flip-close')?.addEventListener('click',()=>card.classList.remove('is-flipped'));
});

// ── manual status override (localStorage，重整不掉；同一 artifact URL 持久) ──
const ST = {pass:'Pass', fail:'Fail', pending:'Pending', manual:'需手動', blocked:'Blocked'};
const OVR_KEY = 'appier-qa-ovr:'+ROUND;
let OVR = {};
try { OVR = JSON.parse(localStorage.getItem(OVR_KEY) || '{}'); } catch(e){ OVR = {}; }
function applyStatus(card, st){
  card.dataset.status = st;
  const pill = card.querySelector('.pill');
  if(pill){ pill.className = 'pill pill-'+st; pill.textContent = ST[st] || st; }
  const result = card.querySelector('.status-result');
  if(result){ result.className = 'status-result status-'+st; result.querySelector('strong').textContent = ST[st] || st; }
}
function saveOvr(k, st, n){
  if(!st && !n){ delete OVR[k]; } else { OVR[k] = {st:st, note:n}; }
  localStorage.setItem(OVR_KEY, JSON.stringify(OVR));
}
function recount(){
  const rank = {pass:0,pending:1,manual:2,fail:3,blocked:4};
  const byTc = {};
  const cards = document.querySelectorAll('.card');
  cards.forEach(x=>{
    const tc=(x.dataset.key||'').split('|')[0], st=x.dataset.status;
    if(!byTc[tc] || rank[st] > rank[byTc[tc]]) byTc[tc]=st;
  });
  const c = {pass:0,fail:0,pending:0,manual:0,blocked:0};
  Object.values(byTc).forEach(st=>{c[st]=(c[st]||0)+1;});
  // tile 只算 Signal（E2E 有自己的 scorecard）
  document.querySelectorAll('.tile').forEach(t=>{
    const f=t.dataset.filter, n=t.querySelector('.tile-n');
    if(!n) return;
    if(f==='all') n.textContent = Object.keys(byTc).length;
    else if(c[f]!==undefined) n.textContent = c[f];
  });
}
// ── tab 切換：Signal / E2E ──
document.querySelectorAll('.tabbtn').forEach(b=>b.addEventListener('click',()=>{
  const tab=b.dataset.tab;
  document.querySelectorAll('.tabbtn').forEach(x=>x.classList.toggle('is-on',x===b));
  document.querySelectorAll('.tab-pane').forEach(p=>{p.hidden=(p.dataset.pane!==tab);});
  const tiles=document.querySelector('.tiles');
  if(tiles) tiles.style.display=(tab==='signal')?'':'none';
}));
document.querySelectorAll('.card').forEach(card=>{
  const k=card.dataset.key, sel=card.querySelector('.ovr'), note=card.querySelector('.ovr-note');
  if(!sel) return;
  const o = OVR[k];
  if(o){ if(o.st){ sel.value=o.st; applyStatus(card, o.st); } if(o.note && note){ note.value=o.note; } }
  sel.addEventListener('change',()=>{
    applyStatus(card, sel.value || card.dataset.auto);
    saveOvr(k, sel.value, note ? note.value : '');
    recount();
  });
  if(note) note.addEventListener('input',()=> saveOvr(k, sel.value, note.value));
});
recount();
""" % (shots_json, round_json, e2e_counts_json)


CSS = """
:root{
  --bg:#f4f6f8; --panel:#ffffff; --ink:#131a21; --ink-soft:#4a5761; --line:#dde3e9;
  --accent:#0e7c86; --accent-soft:#e3f0f1;
  --pass:#2f7d3a; --pass-bg:#e6f2e8; --fail:#c0392b; --fail-bg:#fbe9e7;
  --pend:#5b6b78; --pend-bg:#eceff2; --block:#b5761a; --block-bg:#fbf0dd;
  --man:#7a5cc4; --man-bg:#efe9fb;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans TC",sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0f1519; --panel:#161e24; --ink:#e7edf1; --ink-soft:#9fb0bc; --line:#26313a;
  --accent:#38bdc9; --accent-soft:#123037;
  --pass:#5cc46a; --pass-bg:#16281a; --fail:#f0766a; --fail-bg:#2c1613;
  --pend:#9fb0bc; --pend-bg:#1c252c; --block:#e0a94a; --block-bg:#2a2011;
  --man:#b49af0; --man-bg:#221a33;
}}
:root[data-theme="dark"]{
  --bg:#0f1519; --panel:#161e24; --ink:#e7edf1; --ink-soft:#9fb0bc; --line:#26313a;
  --accent:#38bdc9; --accent-soft:#123037;
  --pass:#5cc46a; --pass-bg:#16281a; --fail:#f0766a; --fail-bg:#2c1613;
  --pend:#9fb0bc; --pend-bg:#1c252c; --block:#e0a94a; --block-bg:#2a2011;
  --man:#b49af0; --man-bg:#221a33;
}
:root[data-theme="light"]{
  --bg:#f4f6f8; --panel:#ffffff; --ink:#131a21; --ink-soft:#4a5761; --line:#dde3e9;
  --accent:#0e7c86; --accent-soft:#e3f0f1;
  --pass:#2f7d3a; --pass-bg:#e6f2e8; --fail:#c0392b; --fail-bg:#fbe9e7;
  --pend:#5b6b78; --pend-bg:#eceff2; --block:#b5761a; --block-bg:#fbf0dd;
  --man:#7a5cc4; --man-bg:#efe9fb;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
  line-height:1.5;-webkit-font-smoothing:antialiased}
.top{position:relative;z-index:20;background:color-mix(in srgb,var(--panel) 92%,transparent);
  backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.top-in{max-width:1180px;margin:0 auto;padding:18px 24px 12px;display:grid;gap:14px}
.brand{display:flex;gap:14px;align-items:center}
.sig{width:40px;height:40px;border-radius:9px;flex:none;
  background:
    linear-gradient(var(--accent),var(--accent)) 0 50%/100% 2px no-repeat,
    radial-gradient(circle at 18% 50%,var(--accent) 3px,transparent 3.5px),
    radial-gradient(circle at 50% 22%,var(--accent) 3px,transparent 3.5px),
    radial-gradient(circle at 82% 68%,var(--accent) 3px,transparent 3.5px);
  border:1px solid var(--line);background-color:var(--accent-soft)}
.kicker{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);font-weight:600}
h1{font-size:21px;margin:2px 0 0;letter-spacing:-.01em}
.meta{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));
  gap:0;margin:0;width:100%;border-top:1px solid var(--line);padding-top:10px}
.meta div{display:flex;flex-direction:column;min-width:0;padding:0 12px;border-left:1px solid var(--line)}
.meta div{padding-block:4px}.meta div:nth-child(4n+1){padding-left:0;border-left:0}
.meta dt{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-soft)}
.meta dd{margin:0;font-family:var(--mono);font-size:12px;font-variant-numeric:tabular-nums;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.progress-banner{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:10px;
  padding:8px 14px;border-radius:8px;font-size:13px;border:1px solid var(--line)}
.progress-banner .pg-icon{font-size:15px}
.progress-banner .pg-label{font-weight:600}
.progress-banner .pg-stall{font-family:var(--mono);font-size:12px;padding:2px 8px;border-radius:5px;
  background:rgba(0,0,0,.06)}
.progress-banner.ok{background:rgba(34,160,90,.12);border-color:rgba(34,160,90,.4);color:#137a43}
.progress-banner.stall{background:rgba(214,138,20,.14);border-color:rgba(214,138,20,.45);color:#a5670a}
.progress-banner.warn{background:rgba(120,120,120,.12);border-color:var(--line);color:var(--ink-soft)}
/* ── Signal / E2E 分頁 ── */
.tabbar{max-width:1180px;margin:0 auto;padding:4px 24px 0;display:flex;gap:6px}
.tabbtn{cursor:pointer;border:1px solid var(--line);border-bottom:none;background:transparent;
  color:var(--ink-soft);font-family:var(--sans);font-size:14px;font-weight:700;
  padding:9px 18px;border-radius:9px 9px 0 0;display:flex;align-items:center;gap:8px}
.tabbtn:hover{color:var(--ink)}
.tabbtn.is-on{background:var(--panel);color:var(--accent);border-color:var(--accent);box-shadow:0 -2px 0 var(--accent) inset}
.tabbtn-n{font:700 12px var(--mono);background:var(--accent-soft);color:var(--accent);
  padding:1px 8px;border-radius:999px}
.tab-pane[hidden]{display:none}
/* E2E scorecard */
.e2e-scorecard{max-width:1180px;margin:0 auto;padding:4px 0 16px;display:flex;gap:10px;flex-wrap:wrap}
.e2e-tile{border:1px solid var(--line);background:var(--panel);border-radius:10px;
  padding:9px 16px;display:flex;flex-direction:column;min-width:88px}
.e2e-tile-n{font:700 21px var(--mono);font-variant-numeric:tabular-nums}
.e2e-tile-l{font-size:11px;color:var(--ink-soft);letter-spacing:.03em}
.e2e-t-pass .e2e-tile-n{color:var(--pass)} .e2e-t-fail .e2e-tile-n{color:var(--fail)}
.e2e-t-blocked .e2e-tile-n{color:var(--block)}
.e2e-lead{margin-top:0}
/* E2E 流程時間軸 */
.e2e-timeline{max-width:1180px;margin:0 auto;display:flex;flex-direction:column;gap:14px}
.e2e-step{border:1px solid var(--line);border-radius:12px;background:var(--panel);overflow:hidden}
.e2e-step-head{padding:11px 16px;background:var(--accent-soft);display:flex;align-items:baseline;gap:12px;
  border-bottom:1px solid var(--line)}
.e2e-step-head h3{margin:0;font-size:15px;color:var(--accent)}
.e2e-step-desc{font-size:12px;color:var(--ink-soft)}
.e2e-step-rows{display:flex;flex-direction:column}
.e2e-row{display:flex;gap:14px;padding:14px 16px;border-top:1px solid var(--line)}
.e2e-row:first-child{border-top:none}
.e2e-row-shot{flex:0 0 132px;width:132px}
.e2e-row-shot .shot{cursor:zoom-in;border:1px solid var(--line);border-radius:8px;overflow:hidden;
  display:block;padding:0;background:#111;width:100%}
.e2e-row-shot .shot img{display:block;width:100%;max-height:220px;object-fit:contain;object-position:top}
.e2e-noshot{border:1px dashed var(--line);border-radius:8px;padding:18px 8px;text-align:center;
  color:var(--ink-soft);font-size:12px;line-height:1.5;background:var(--pend-bg)}
.e2e-row-body{flex:1;min-width:0}
.e2e-row-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.e2e-tc{font:700 13px var(--mono)}
/* 應有值 / 實際 兩塊並排（比照 Signal 卡） */
.e2e-kv{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px}
.e2e-block{border:1px solid var(--line);border-radius:9px;padding:8px 11px;min-width:0}
.e2e-expect{background:var(--accent-soft)}
.e2e-lbl{display:block;font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--ink-soft);margin-bottom:4px}
.e2e-val{font-size:13px;line-height:1.55;color:var(--ink);word-break:break-word;overflow-wrap:anywhere}
.e2e-endpoint{display:block;margin-top:6px;padding-top:5px;border-top:1px dashed var(--line);
  font:11px var(--mono);color:var(--ink-soft);word-break:break-all;white-space:normal}
@media(max-width:640px){.e2e-kv{grid-template-columns:1fr}}
.e2e-badge{font-size:11px;font-weight:800;padding:3px 10px;border-radius:999px;white-space:nowrap}
.e2e-b-pass{color:#0a7d3c;background:var(--pass-bg)} .e2e-b-fail{color:var(--fail);background:var(--fail-bg)}
.e2e-b-blocked{color:var(--block);background:var(--block-bg)}
@media (max-width:640px){.e2e-row{flex-direction:column}.e2e-row-shot{width:100%;flex-basis:auto}}
.tiles{max-width:1180px;margin:0 auto;padding:6px 24px 14px;display:flex;gap:8px;flex-wrap:wrap}
.tile{cursor:pointer;border:1px solid var(--line);background:var(--panel);border-radius:9px;
  padding:8px 14px;display:flex;flex-direction:column;min-width:78px;font-family:var(--sans);
  color:var(--ink);transition:border-color .15s,transform .05s}
.tile:hover{border-color:var(--accent)}
.tile:active{transform:translateY(1px)}
.tile.is-on{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.tile-n{font-size:19px;font-weight:700;font-variant-numeric:tabular-nums;font-family:var(--mono)}
.tile-l{font-size:11px;color:var(--ink-soft);letter-spacing:.02em}
.tile[data-filter=pass] .tile-n{color:var(--pass)} .tile[data-filter=fail] .tile-n{color:var(--fail)}
.tile[data-filter=pending] .tile-n{color:var(--pend)} .tile[data-filter=blocked] .tile-n{color:var(--block)}
.tile[data-filter=manual] .tile-n{color:var(--man)}
main{max-width:1180px;margin:0 auto;padding:22px 24px 80px}
.setup-cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-bottom:18px}
.setup-cards article{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.setup-cards h2{font-size:13px;color:var(--accent);margin:0 0 10px}
.setup-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px 14px}
.setup-grid div{display:flex;flex-direction:column;min-width:0}.setup-grid span{font-size:9.5px;color:var(--ink-soft)}
.setup-grid strong{font:11px var(--mono);word-break:break-all}
.lead{color:var(--ink-soft);font-size:14px;max-width:80ch;margin:0 0 26px;border-left:2px solid var(--accent);
  padding-left:14px}
.lead b{color:var(--ink)}
.cat{margin:0 0 34px}
.cat-h{display:flex;align-items:center;gap:12px;font-size:15px;margin:0 0 14px;
  padding-bottom:8px;border-bottom:1px solid var(--line);letter-spacing:.01em}
.cat-k{font-family:var(--mono);font-size:12px;color:var(--accent);background:var(--accent-soft);
  padding:3px 8px;border-radius:6px;letter-spacing:.04em}
.cat-n{margin-left:auto;font-family:var(--mono);font-size:12px;color:var(--ink-soft);
  font-variant-numeric:tabular-nums}
/* 欄數依寬度自適應（窄→單欄、寬→多欄），卡高依視窗高縮放；內容超出由正/反面自行捲動 */
.grid{display:grid;gap:clamp(12px,1.4vw,18px);align-items:stretch;
  grid-template-columns:repeat(auto-fill,minmax(min(100%,clamp(300px,42vw,440px)),1fr))}
.card{height:clamp(480px,72vh,600px);position:relative;perspective:1200px;border-radius:12px}
.card-inner{position:absolute;inset:0;transition:transform .42s cubic-bezier(.2,.7,.2,1);
  transform-style:preserve-3d}
.card.is-flipped .card-inner{transform:rotateY(180deg)}
.face{position:absolute;inset:0;background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:clamp(11px,1.8vh,16px);overflow:hidden;backface-visibility:hidden;-webkit-backface-visibility:hidden}
/* 正面不捲動：內距/間距/字級皆隨卡片高（72vh）縮放，保持簡潔一頁到底。
   完整證據（截圖、mock 指令、retry 歷史…）在背面（可捲）。 */
.card-front{display:flex;flex-direction:column;gap:clamp(5px,.9vh,10px);overflow:hidden}
.card-back{transform:rotateY(180deg);display:flex;flex-direction:column;padding-bottom:0}
.face::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px}
.card[data-status=pass] .face::before{background:var(--pass)}
.card[data-status=fail] .face::before{background:var(--fail)}
.card[data-status=pending] .face::before{background:var(--pend)}
.card[data-status=manual] .face::before{background:var(--man)}
.card[data-status=blocked] .face::before{background:var(--block)}
.card-top{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.tc{font-family:var(--mono);font-weight:700;font-size:clamp(12px,1.9vh,15px);letter-spacing:.02em}
.tier{font-size:10px;letter-spacing:.05em;text-transform:uppercase;color:var(--ink-soft);
  border:1px solid var(--line);border-radius:5px;padding:1px 6px}
.pill{margin-left:auto;font-size:11px;font-weight:600;padding:3px 10px;border-radius:20px;letter-spacing:.02em}
.pill-pass{color:var(--pass);background:var(--pass-bg)} .pill-fail{color:var(--fail);background:var(--fail-bg)}
.pill-pending{color:var(--pend);background:var(--pend-bg)} .pill-blocked{color:var(--block);background:var(--block-bg)}
.pill-manual{color:var(--man);background:var(--man-bg)}
.field{font-family:var(--mono);font-size:clamp(11px,1.7vh,12.5px);color:var(--accent);word-break:break-all}
.signal{font-size:clamp(11.5px,1.8vh,13px);font-weight:700;color:var(--text);margin-top:clamp(-9px,-1.2vh,-6px)}
.schema{display:grid;gap:4px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--bg)}
.schema strong{font-size:13px}.schema span,.schema small{font-size:11.5px;color:var(--muted);line-height:1.45}
.schema code{color:var(--accent)}
.cond{margin:0;font-size:12.5px;color:var(--ink-soft)}
.result-kv{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,.9fr);gap:clamp(7px,1vh,9px);
  align-items:stretch;margin:3px 0 0;min-height:clamp(96px,18vh,145px)}
.result-block{border:1px solid var(--line);border-radius:10px;padding:clamp(9px,1.5vh,13px) 12px;
  background:var(--bg);min-width:0;min-height:clamp(96px,18vh,145px);
  display:flex;flex-direction:column;gap:clamp(6px,1vh,9px);justify-content:flex-start}
.result-label{font-size:clamp(9px,1.3vh,9.5px);letter-spacing:.08em;text-transform:uppercase;color:var(--ink-soft)}
.result-block strong{font-family:var(--mono);font-size:clamp(11.5px,1.8vh,13px);line-height:1.5;word-break:break-word}
.result-block small{font-size:clamp(10px,1.5vh,11px);line-height:1.4;color:var(--ink-soft)}
.result-block small b{font-family:var(--mono);color:var(--ink)}
.capture-ref{margin-top:auto;padding-top:7px;border-top:1px dashed var(--line);word-break:break-all}
.golden-block{border-color:color-mix(in srgb,var(--accent) 48%,var(--line));background:var(--accent-soft)}
.schema-ref{margin-top:6px;padding-top:5px;border-top:1px dashed var(--line);
  font-family:var(--mono);color:var(--ink-soft);opacity:.85;
  word-break:break-word;overflow-wrap:anywhere;white-space:normal}
.absent-why{margin:5px 0 2px;padding:5px 9px;border-radius:8px;font-size:clamp(10.5px,1.55vh,12px);line-height:1.5;
  color:#b4431f;background:rgba(220,90,40,.1);border-left:4px solid #dc5a28}
.absent-why b{font-weight:800}
.absent-shot{color:var(--ink-soft);font-size:11.5px}
@media(prefers-color-scheme:dark){.absent-why{color:#ff9b6b;background:rgba(220,90,40,.16)}}
.mock-cmd{margin:5px 0 2px;padding:6px 9px;border-radius:8px;min-width:0;
  background:rgba(59,110,165,.09);border-left:4px solid #3b6ea5}
.mock-label{display:block;font-size:11px;font-weight:700;color:#2f5f96;margin-bottom:3px}
/* 長指令換行、不橫向撐破卡片（pre-wrap 保留換行、break-all 允許在字元間斷） */
.mock-cmd code{display:block;font-family:var(--mono);font-size:clamp(10px,1.5vh,11.5px);line-height:1.45;
  white-space:pre-wrap;word-break:break-all;overflow-wrap:anywhere;color:var(--ink)}
.mock-cmd .mock-reset{color:var(--ink-soft);opacity:.85;margin-top:2px}
@media(prefers-color-scheme:dark){.mock-label{color:#8fbdf0}.mock-cmd{background:rgba(59,110,165,.16)}}
.schema-note{margin-top:auto}
.card[data-status=pass] .actual-block strong{color:var(--pass)}
.card[data-status=fail] .actual-block strong{color:var(--fail)}
.status-result{display:flex;align-items:center;justify-content:space-between;border:1px solid currentColor;
  border-radius:10px;padding:clamp(6px,1.2vh,9px) 13px;font-weight:800}
.status-result span{font:700 clamp(8.5px,1.3vh,9.5px) var(--sans);letter-spacing:.13em;opacity:.75}
.status-result strong{font-size:clamp(15px,2.6vh,20px);line-height:1;text-transform:uppercase;letter-spacing:.02em}
.status-pass{color:var(--pass);background:var(--pass-bg)}
.status-fail{color:var(--fail);background:var(--fail-bg)}
.status-pending{color:var(--pend);background:var(--pend-bg)}
.status-manual{color:var(--man);background:var(--man-bg)}
.status-blocked{color:var(--block);background:var(--block-bg)}
.flip-btn{cursor:pointer;border:1px solid var(--line);background:var(--panel);color:var(--accent);
  border-radius:8px;padding:clamp(5px,1vh,7px) 10px;font:600 clamp(10.5px,1.5vh,11.5px) var(--sans)}
.flip-btn:hover{border-color:var(--accent);background:var(--accent-soft)}
.flip-open{margin-top:auto;width:100%}
.back-head{display:flex;justify-content:space-between;align-items:center;gap:10px;padding-bottom:10px;
  border-bottom:1px solid var(--line);flex:none}
.back-title{font-size:11px;color:var(--ink-soft);margin-left:8px}
.flip-close{padding:4px 8px}
.back-scroll{display:flex;flex-direction:column;gap:9px;overflow:auto;min-height:0;padding:11px 2px 12px}
/* flex 空間不足時 overflow:hidden 的 .shot 會被壓成 0 高（截圖看起來像消失）；禁止壓縮，超出改由 back-scroll 捲動 */
.back-scroll>*{flex-shrink:0}
.ev-source{font-size:11px;color:var(--ink-soft);display:flex;gap:8px;align-items:flex-start}
.ev-source .rl{font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);flex:none}
.ev-source code{font-family:var(--mono);font-size:10.5px;word-break:break-all}
.capture-id{display:block;padding:10px 12px;
  border:1px solid var(--line);border-radius:9px;background:var(--bg)}
.capture-id>div{display:flex;flex-direction:column;gap:3px;min-width:0}
.capture-id span,.bid-identity .rl{font-size:9px;letter-spacing:.09em;color:var(--accent);font-weight:700}
.capture-id strong,.capture-id code{font:600 11px var(--mono);word-break:break-all}
.capture-id .capture-file{padding:0}
.bid-evidence{display:flex;flex-direction:column;gap:7px;padding:11px 12px;border:1px solid var(--line);
  border-radius:9px;background:var(--bg)}
.bid-evidence code{font-family:var(--mono);font-size:12px;line-height:1.55;word-break:break-all}
.bid-evidence code b{color:var(--accent)}
.bid-identity{display:grid;grid-template-columns:auto minmax(0,1fr);gap:5px 10px;padding:10px 12px;
  border:1px solid var(--line);border-radius:9px;background:var(--bg)}
.bid-identity>.rl{grid-column:1/-1;margin-bottom:2px}
.bid-identity>div{display:contents}.bid-identity div span{font-size:10px;color:var(--ink-soft)}
.bid-identity div code{font:11px var(--mono);word-break:break-all;color:var(--ink)}
.proof-state{font-size:clamp(10px,1.45vh,11px);font-weight:700;padding:clamp(5px,.9vh,6px) 9px;border-radius:7px}
.proof-ok{color:var(--pass);background:var(--pass-bg)}
.proof-missing{color:var(--pend);background:var(--pend-bg)}
.proof-why{padding:10px 12px;border-left:3px solid var(--accent);background:var(--accent-soft);border-radius:0 8px 8px 0}
.proof-why .rl{font-size:9.5px;letter-spacing:.08em;color:var(--accent);text-transform:uppercase;font-weight:700}
.proof-why p{font-size:12px;line-height:1.55;margin:5px 0 0;color:var(--ink)}
.tc-detail{font-size:11.5px;color:var(--ink-soft);border-top:1px dashed var(--line);padding-top:8px}
.tc-detail summary{cursor:pointer;color:var(--accent);font-weight:600}.tc-detail .cond{margin-top:7px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;align-items:baseline}
.kv .k{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-soft)}
.kv .v{font-family:var(--mono);font-size:12.5px;word-break:break-all;font-variant-numeric:tabular-nums}
.v-exp{color:var(--ink)} .v-act{color:var(--ink);font-weight:600}
.card[data-status=fail] .v-act{color:var(--fail)}
.card[data-status=pass] .v-act{color:var(--pass)}
.edit{display:flex;align-items:center;gap:7px;flex-wrap:wrap;border-top:1px solid var(--line);
  padding:10px 0 11px;background:var(--panel);flex:none;margin-top:auto}
.edit .rl{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--accent)}
.edit .ovr{font-family:var(--sans);font-size:12px;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:3px 6px}
.edit .ovr-note{flex:1;min-width:120px;font-family:var(--sans);font-size:12px;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:3px 8px}
.edit .ovr-note::placeholder{color:var(--ink-soft)}
.review-edit{display:grid;grid-template-columns:130px minmax(0,1fr);gap:8px;margin:0;
  padding:clamp(7px,1.2vh,10px);border:1px solid var(--line);border-radius:10px;background:var(--bg)}
.review-edit label{display:flex;flex-direction:column;gap:clamp(3px,.6vh,5px);min-width:0}
.review-edit .rl{font-weight:700;color:var(--ink-soft)}
.review-edit .ovr,.review-edit .ovr-note{width:100%;box-sizing:border-box;
  height:clamp(26px,3.6vh,30px);background:var(--panel)}
.review-edit .reason-label{min-width:0}
.note{font-size:clamp(10px,1.5vh,11.5px);border-radius:7px;padding:clamp(5px,1vh,7px) 10px;line-height:1.4}
.note-rd{background:var(--fail-bg);color:var(--fail)}
.note-bl{background:var(--block-bg);color:var(--block)}
.note-man{background:var(--man-bg);color:var(--man)}
.action{font-size:11.5px;color:var(--ink-soft);border-top:1px dashed var(--line);padding-top:9px}
.action .rl{display:inline-block;min-width:64px;font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);margin-right:8px}
.ids{font-size:11.5px;color:var(--ink-soft);border-top:1px dashed var(--line);padding-top:9px;line-height:1.7}
.ids .rl{display:inline-block;min-width:64px;font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);margin-right:8px}
.ids code{font-family:var(--mono);font-size:11px;background:var(--bg);padding:1px 5px;border-radius:4px;word-break:break-all}
.manlist{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin:0 0 26px}
.manlist summary{cursor:pointer;font-weight:600;font-size:14px;color:var(--man)}
.manlist-lead{font-size:12.5px;color:var(--ink-soft);margin:10px 0}
.manlist code{font-family:var(--mono);font-size:11.5px;background:var(--bg);padding:1px 5px;border-radius:4px}
.mwrap{overflow-x:auto}
.mtable{border-collapse:collapse;width:100%;font-size:12.5px}
.mtable td{padding:6px 10px;border-top:1px solid var(--line);vertical-align:top}
.mtc{font-family:var(--mono);font-weight:600;white-space:nowrap}
.mtag{white-space:nowrap;font-size:10px;letter-spacing:.04em;border-radius:5px;padding:2px 7px}
.mtag-man{color:var(--man);background:var(--man-bg)} .mtag-blk{color:var(--block);background:var(--block-bg)}
.mtag-pass{color:var(--pass);background:var(--pass-bg)} .mtag-fail{color:var(--fail);background:var(--fail-bg)}
.con{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin:0 0 26px}
.con-h{font-size:14px;margin:0;color:var(--accent)}
.con-lead{font-size:12.5px;color:var(--ink-soft);margin:6px 0 12px}
.con-row{display:flex;align-items:center;gap:10px;padding:7px 0;border-top:1px solid var(--line);flex-wrap:wrap}
.con-ok{font-weight:700;width:18px;text-align:center}
.con-y{color:var(--pass)} .con-n{color:var(--fail)}
.con-lab{font-weight:600;font-size:13px;min-width:180px}
.con-msg{font-size:12px;color:var(--ink-soft);flex:1}
.con-val{font-family:var(--mono);font-size:11.5px;background:var(--bg);padding:2px 7px;border-radius:5px;word-break:break-all}
.repro{display:flex;flex-direction:column;gap:5px;font-size:11.5px;color:var(--ink-soft);
  border-top:1px dashed var(--line);padding-top:9px}
.repro .rl{display:inline-block;min-width:64px;font-size:10px;letter-spacing:.06em;text-transform:uppercase;
  color:var(--accent);margin-right:8px}
.shot{margin-top:2px;padding:0;border:1px solid var(--line);border-radius:8px;overflow:hidden;
  cursor:zoom-in;background:var(--bg);display:block;width:100%;text-align:left}
.shot img{display:block;width:100%;max-height:360px;object-fit:contain;object-position:top;background:#111}
.e2e-shot{max-width:520px;margin:14px auto 2px;background:var(--panel)}
.e2e-shot img{max-height:520px;object-fit:contain;background:var(--bg)}
.shot[data-unmatched] img{filter:grayscale(.5) opacity(.7)}
.shot-cap{display:block;font-size:10px;color:var(--ink-soft);padding:4px 8px;background:var(--panel)}
.lightbox{position:fixed;inset:0;z-index:50;background:rgba(6,10,13,.85);display:none;
  align-items:center;justify-content:center;padding:30px}
.lightbox.open{display:flex}
.lightbox img{max-width:min(440px,90vw);max-height:90vh;border-radius:10px;
  box-shadow:0 20px 60px rgba(0,0,0,.5)}
.lb-x{position:absolute;top:18px;right:22px;width:40px;height:40px;border-radius:50%;border:none;
  background:rgba(255,255,255,.14);color:#fff;font-size:24px;cursor:pointer;line-height:1}
.lb-x:hover{background:rgba(255,255,255,.26)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media (max-width:820px){.meta{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px 0}
  .meta div:nth-child(odd){padding-left:0;border-left:0}}
@media (max-width:420px){.review-edit{grid-template-columns:1fr}}
@media (max-width:640px){.top-in{padding:14px 16px 10px}.tiles{padding:6px 16px 12px}main{padding:18px 16px 60px}}
@media (max-width:640px){.setup-cards{grid-template-columns:1fr}}
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("round_dir", help="evidence round 資料夾（Signal 來源）")
    ap.add_argument("--out", help="輸出 HTML 路徑（預設 <round_dir>/report.html）")
    ap.add_argument("--e2e-round", dest="e2e_round",
                    help="E2E 分頁改從此 round 評估（合併：signal 用 round_dir、E2E 用專跑 flow 的 round）")
    args = ap.parse_args()
    out = args.out or os.path.join(args.round_dir, "report.html")
    build(args.round_dir, out, e2e_round=args.e2e_round)


if __name__ == "__main__":
    main()
