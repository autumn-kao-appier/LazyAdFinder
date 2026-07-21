#!/usr/bin/env python3
"""ios_bid_inspector.py — iOS 版 SSP bid request 欄位驗證（Phase 2）。

重用 bid_inspector.py 的 check 引擎（run_validator 平台無關），換上 iOS 的
validator 清單 IOS_VALIDATORS。TC id 用 IOS-xx，數字沿用 Android AND-xx 以便對照。

由 AOS 84 條 TC 依四種處置改寫：
  ① 直接搬  — 同 path、同邏輯，只是值不同（device.ext.* 環境訊號、記憶體/螢幕…）
  ② 改語意  — GAID→IDFA(device.ifa)、App Set ID→IDFV、os→iOS、root→jailbreak…
  ③ 反轉    — SKAdNetwork：Android 斷言「不該有」→ iOS「本來就該有」
  ④ 新增/NA — ATT 授權狀態(新增)、applist iOS 隱私限制→應缺席、simulator(本流程只跑實機)

⚠️ 標了 "cal": True 的條目＝欄位路徑或期望值尚未對真實 iOS bid 校準（約兩成），
   實機擷到第一份 bid 後對照 ios_bid_summary.txt 修正。其餘八成用最合理推測值，
   路徑猜錯只會顯示 FAIL/missing（不會假 PASS），照樣提示要校準哪條。

用法：
    python ios_bid_inspector.py                       # 驗 /tmp/appier_bid.json 全部
    python ios_bid_inspector.py IOS-04               # 單條
    python ios_bid_inspector.py --file /path/bid.json
    python ios_bid_inspector.py --round evidence/IOS_...   # 彙總整個 round
"""
import glob
import json
import os
import re
import sys
from datetime import datetime

# 重用 AOS 引擎的純元件（平台無關）
from bid_inspector import (
    run_validator as _base_run_validator,
    get_field, _unwrap, _trunc, format_report,
    ZERO_UUID, ISO639_RE, INPUT_LANG_RE, SEMVER_RE, CELL_4G5G_RE,
)

# ── iOS 專用 regex ────────────────────────────────────────────────────────────
# IDFA / IDFV 在 iOS 是大寫 hex UUID（e.g. AEBE52E7-03EE-455A-B3C4-...），
# AOS 的 UUID_RE 只吃小寫，故 iOS 用不分大小寫版本。
UUID_CI_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
IOS_OS_RE = re.compile(r"^ios$", re.IGNORECASE)


# ── check 擴充：IDFA/IDFV 大小寫不敏感的非零 UUID ──────────────────────────────
def run_validator(bid, v, reference_ms=None):
    """iOS 專用 check 先攔，其餘一律委派給 AOS 引擎（run_validator）。"""
    check = v["check"]
    if check == "uuid_ci_nonzero":
        value, found = get_field(bid, v["field"])
        if not found or value is None:
            return False, None, "field missing"
        ok = (isinstance(value, str) and bool(UUID_CI_RE.fullmatch(value))
              and value.upper() != ZERO_UUID.upper())
        return ((True, value, "valid non-zero UUID ✓") if ok
                else (False, value, "expected non-zero UUID (IDFA/IDFV，不分大小寫)"))
    return _base_run_validator(bid, v, reference_ms=reference_ms)


# ── iOS TC validator 表 ───────────────────────────────────────────────────────
# cal=True → 待實機校準（路徑或期望值）。disp = 對照 AOS 的處置。
IOS_VALIDATORS = [
    # ── A. Core Identifiers（2026-07-21 對真實 iOS bid 校準：IDFA=device.ia、IDFV=device.ifv，皆大寫 UUID）
    {"tc": "IOS-01", "field": "device.ia", "check": "uuid_ci_nonzero",
     "disp": "改語意", "note": "ATT authorized → IDFA(device.ia) 為非零大寫 UUID"},
    {"tc": "IOS-28", "field": "device.ia", "check": "regex", "pattern": UUID_CI_RE,
     "disp": "改語意", "note": "IDFA 格式（大寫 UUID）"},
    {"tc": "IOS-02", "field": "device.ia", "check": "value_or_absent", "expected": ZERO_UUID,
     "disp": "改語意", "note": "ATT denied → 全零 IDFA 或缺席（IOS-01 相反狀態）"},
    {"tc": "IOS-03", "field": "device.ifv", "check": "regex", "pattern": UUID_CI_RE,
     "disp": "改語意", "note": "IDFV(identifierForVendor) 合法大寫 UUID；跨啟動穩定性手動確認"},
    {"tc": "IOS-75", "field": "device.lat", "check": "int_zero_or_absent",
     "disp": "直接搬", "note": "tracking allowed（ATT authorized）→ integer 0 或缺席"},
    {"tc": "IOS-76", "field": "device.lat", "check": "value", "expected": 1,
     "disp": "直接搬", "note": "tracking denied（ATT denied）→ 必為 1，且與 IOS-02 IDFA opt-out 一致"},
    # ── B. Device State - Bool
    {"tc": "IOS-04", "field": "device.ext.darkmode", "check": "value", "expected": True, "disp": "直接搬"},
    {"tc": "IOS-05", "field": "device.ext.darkmode", "check": "value", "expected": False, "disp": "直接搬"},
    {"tc": "IOS-06", "field": "device.charging", "check": "one_of_typed", "expected": [1, True],
     "disp": "直接搬", "note": "charging → 1 或 true"},
    {"tc": "IOS-07", "field": "device.charging", "check": "one_of_typed", "expected": [0, False],
     "disp": "直接搬", "note": "not charging → 0 或 false"},
    {"tc": "IOS-08", "field": "device.ext.battery_saver", "check": "value", "expected": True,
     "disp": "改語意", "note": "iOS Low Power Mode ON（欄位確認為 device.ext.battery_saver）"},
    {"tc": "IOS-09", "field": "device.ext.battery_saver", "check": "value", "expected": False,
     "disp": "改語意", "note": "iOS Low Power Mode OFF"},
    {"tc": "IOS-10", "field": "device.ext.jailbreak", "check": "value", "expected": False,
     "disp": "改語意", "note": "一般（未越獄）裝置 → false"},
    {"tc": "IOS-11", "field": "device.ext.jailbreak", "check": "value", "expected": True,
     "disp": "改語意", "note": "越獄裝置 → true（Blocked，除非有越獄機）"},
    {"tc": "IOS-13", "field": "device.ext.emulator", "check": "value", "expected": False,
     "disp": "NA", "note": "本流程只跑實機 → false。Simulator 情境（IOS-12）暫不測"},
    {"tc": "IOS-14", "field": "device.ext.vpn", "check": "vpn_active",
     "disp": "直接搬", "note": "VPN active → 非空協定字串"},
    {"tc": "IOS-15", "field": "device.ext.vpn", "check": "value_or_absent", "expected": "0",
     "disp": "改值", "note": "VPN inactive → iOS 送字串 \"0\"（或缺席）"},
    # ── C. Device State - Numeric
    {"tc": "IOS-16", "field": "device.batterylevel", "check": "value", "expected": 100, "disp": "直接搬"},
    {"tc": "IOS-17", "field": "device.batterylevel", "check": "value", "expected": 0, "disp": "直接搬"},
    {"tc": "IOS-19", "field": "device.ext.screen_bright", "check": "range", "min": 0.0, "max": 0.1, "disp": "直接搬"},
    {"tc": "IOS-20", "field": "device.ext.screen_bright", "check": "range", "min": 0.9, "max": 1.0, "disp": "直接搬"},
    {"tc": "IOS-21", "field": "device.ext.fontscale", "check": "value", "expected": 1.0, "cal": True,
     "disp": "改值", "note": "iOS Dynamic Type：預設值/刻度與 Android 不同，期望值待校準"},
    {"tc": "IOS-22", "field": "device.ext.fontscale", "check": "range", "min": 1.1, "max": 3.5, "cal": True,
     "disp": "改值", "note": "放大字體 → >1；iOS Dynamic Type 上限待校準"},
    {"tc": "IOS-23", "field": "device.ext.volume", "check": "value", "expected": 0.0, "disp": "直接搬"},
    {"tc": "IOS-24", "field": "device.ext.volume", "check": "value", "expected": 1.0, "disp": "直接搬"},
    {"tc": "IOS-25", "field": "device.utcoffset", "check": "value", "expected": 480, "disp": "直接搬", "note": "Asia/Taipei UTC+8"},
    {"tc": "IOS-26", "field": "device.utcoffset", "check": "one_of_typed", "expected": [-240, -300], "disp": "直接搬", "note": "America/New_York"},
    {"tc": "IOS-27", "field": "device.utcoffset", "check": "value", "expected": 0, "disp": "直接搬", "note": "UTC"},
    # ── D. Device / App State - Format
    {"tc": "IOS-30", "field": "device.lang", "check": "regex", "pattern": ISO639_RE, "disp": "直接搬", "note": "ISO-639-1 2-char lowercase"},
    {"tc": "IOS-31", "field": "device.langb", "check": "nonempty",
     "disp": "改值", "note": "baseline language；iOS 依裝置地區（如 en-TW），不固定 en-US → 只驗非空"},
    {"tc": "IOS-32", "field": "device.input_lang", "check": "array_regex", "pattern": INPUT_LANG_RE,
     "disp": "改語意", "note": "iOS 鍵盤語言清單（如 en-TW / zh-Hant-TW）"},
    {"tc": "IOS-33", "field": "req.app.ver", "root": "raw", "check": "nonempty",
     "disp": "改路徑", "note": "app 資訊在 req_enc.app（非 ext）；ver 如 '1.0'"},
    {"tc": "IOS-34", "field": "req.app.displaymanager", "root": "raw", "check": "nonempty",
     "disp": "改路徑", "note": "req_enc.app.displaymanager（如 AppierMobileAds-iOS）"},
    {"tc": "IOS-35", "field": "req.app.displaymanagerver", "root": "raw", "check": "nonempty",
     "disp": "改路徑", "note": "req_enc.app.displaymanagerver"},
    {"tc": "IOS-36", "field": "device.make", "check": "nonempty_notunknown", "cal": True,
     "disp": "改值", "note": "iOS 期望 'Apple'；先驗非空，校準後可改 value=Apple"},
    {"tc": "IOS-37", "field": "device.model", "check": "nonempty_notunknown", "disp": "直接搬", "note": "e.g. iPhone15,2"},
    {"tc": "IOS-38", "field": "device.ip", "check": "ipv4_nonzero", "cal": True,
     "disp": "直接搬", "note": "iOS SDK 未送 device.ip（RD gap，同 Android）"},
    {"tc": "IOS-39", "field": "device.ipv6", "check": "nonempty", "cal": True,
     "disp": "直接搬", "note": "iOS SDK 未送 device.ipv6（RD gap，同 Android）"},
    {"tc": "IOS-40", "field": "device.conntype", "check": "value", "expected": "wifi", "disp": "直接搬"},
    {"tc": "IOS-41", "field": "device.conntype", "check": "regex", "pattern": CELL_4G5G_RE, "cal": True,
     "disp": "直接搬", "note": "cellular_4g/5g；需 SIM 機（Blocked）"},
    {"tc": "IOS-66", "field": "req.app.bundle", "root": "raw", "check": "nonempty",
     "disp": "改路徑", "note": "req_enc.app.bundle = 受測 app bundle id"},
    {"tc": "IOS-67", "field": "req.app.sdk_version", "root": "raw", "check": "nonempty",
     "disp": "改路徑", "note": "req_enc.app.sdk_version = 本 build 的 SDK 版本"},
    {"tc": "IOS-68", "field": "device.type", "check": "nonempty", "disp": "直接搬", "note": "phone/tablet"},
    {"tc": "IOS-69", "field": "device.os", "check": "regex", "pattern": IOS_OS_RE,
     "disp": "改值", "note": "iOS 應送 'iOS'（AOS 是 'Android'）"},
    {"tc": "IOS-70", "field": "device.osv", "check": "nonempty", "disp": "直接搬", "note": "對照裝置實際 iOS 版本"},
    {"tc": "IOS-71", "field": "device.hwv", "check": "nonempty", "disp": "直接搬"},
    {"tc": "IOS-73", "field": "device.country", "check": "value", "expected": "TW",
     "disp": "改值", "note": "iOS 送大寫 alpha-2（TW），Android 是小寫"},
    {"tc": "IOS-74", "field": "device.locale", "check": "nonempty", "disp": "直接搬"},
    # ── E. Device State - Arrays（CoreMotion；iOS SDK 是否實作待確認）
    {"tc": "IOS-42", "field": "device.ext.gyroscope", "check": "array_number", "cal": True,
     "disp": "NA", "note": "iOS SDK 未送 gyroscope（實測 ext 無此欄，RD gap）"},
    {"tc": "IOS-43", "field": "device.ext.accelerometer", "check": "array_number", "cal": True,
     "disp": "NA", "note": "iOS SDK 未送 accelerometer（實測 ext 無此欄，RD gap）"},
    {"tc": "IOS-44", "field": "device.ext.boottime", "check": "array_timestamp",
     "disp": "直接搬", "note": "iOS 有送（epoch-ms 陣列）"},
    # ── F. Geolocation
    {"tc": "IOS-45", "field": "device.geo_lat", "check": "nonzero_range", "min": -90.0, "max": 90.0, "disp": "直接搬", "note": "定位授權"},
    {"tc": "IOS-45", "field": "device.geo_lon", "check": "nonzero_range", "min": -180.0, "max": 180.0, "disp": "直接搬", "note": "定位授權"},
    {"tc": "IOS-46", "field": "device.geo_lat", "check": "absent", "disp": "直接搬", "note": "定位拒絕 → 缺席"},
    {"tc": "IOS-46", "field": "device.geo_lon", "check": "absent", "disp": "直接搬", "note": "定位拒絕 → 缺席"},
    # ── G. In-Session（session_duration 語意 iOS 相同）
    {"tc": "IOS-47-1", "field": "user.session_duration", "check": "session_case", "case": 1,
     "disp": "直接搬", "note": "App 全程前景 → 下一 bid session 累進（B>A）"},
    {"tc": "IOS-47-2", "field": "user.session_duration", "check": "session_case", "case": 2,
     "disp": "直接搬", "note": "關整個 App 重開 → session 重置（B<A）"},
    {"tc": "IOS-47-3", "field": "user.session_duration", "check": "session_case", "case": 3,
     "disp": "直接搬", "note": "退背景再切回 → session 累進（B>A）"},
    {"tc": "IOS-48", "field": "user.session_duration", "check": "int_range", "min": 0, "max": 4, "disp": "直接搬", "note": "cold-start <5"},
    {"tc": "IOS-49", "field": "user.app_init_time", "check": "timestamp_recent", "disp": "直接搬"},
    {"tc": "IOS-50", "field": "user.last_foreground_time", "check": "array_timestamp", "disp": "直接搬"},
    {"tc": "IOS-50", "field": "user.last_background_time", "check": "array_timestamp", "disp": "直接搬"},
    {"tc": "IOS-51", "field": "user.impression_history", "check": "array_impression", "cal": True,
     "disp": "直接搬", "note": "iOS SDK 是否實作待確認"},
    {"tc": "IOS-52", "field": "user.app_duration", "check": "int_range", "min": 30, "max": 99_999_000, "disp": "直接搬"},
    # ── H. Memory / Disk
    {"tc": "IOS-53", "field": "device.ext.mem_total", "check": "positive_int", "disp": "直接搬", "note": "bytes"},
    {"tc": "IOS-54", "field": "device.ext.mem_available", "check": "leq_field", "ref_field": "device.ext.mem_total", "disp": "直接搬"},
    {"tc": "IOS-55", "field": "device.ext.disk_total", "check": "positive_int", "cal": True,
     "disp": "NA", "note": "iOS SDK 未送 disk_total（實測 ext 只有 mem_*，RD gap）"},
    {"tc": "IOS-56", "field": "device.ext.disk_free", "check": "leq_field", "ref_field": "device.ext.disk_total", "cal": True,
     "disp": "NA", "note": "iOS SDK 未送 disk_free（RD gap）"},
    # ── I. Screen / Display
    {"tc": "IOS-57", "field": "device.sw", "check": "positive_int", "disp": "直接搬"},
    {"tc": "IOS-58", "field": "device.sh", "check": "positive_int", "disp": "直接搬"},
    {"tc": "IOS-59", "field": "device.ppi", "check": "positive_int", "disp": "直接搬"},
    {"tc": "IOS-60", "field": "device.pxratio", "check": "range", "min": 2.0, "max": 3.5, "disp": "直接搬", "note": "iPhone @2x/@3x"},
    # ── K. Network Latency
    {"tc": "IOS-61", "field": "device.ext.latency", "check": "positive_int",
     "disp": "直接搬", "note": "iOS 有送 latency（ms，實測有值；Android 反而 hardcode null）"},
    # ── J. Negative / Absent
    {"tc": "IOS-62", "field": "device.ext.applist", "check": "falsy",
     "disp": "反轉", "note": "iOS 無法列舉已安裝 app（隱私限制）→ 應缺席或空陣列（與 AOS AND-62 相反）"},
    {"tc": "IOS-63", "field": "device.ext.iaphistory", "check": "array", "cal": True,
     "disp": "NA", "note": "iOS SDK 未送 iaphistory（實測 ext 無此欄，RD gap）"},
    {"tc": "IOS-64", "field": "device.carrier", "check": "absent_or_empty", "disp": "直接搬", "note": "no SIM"},
    {"tc": "IOS-64", "field": "device.mccmnc", "check": "absent_or_empty", "disp": "直接搬", "note": "no SIM"},
    {"tc": "IOS-72", "field": "device.operator", "check": "absent_or_empty", "disp": "直接搬", "note": "no SIM"},
    {"tc": "IOS-72", "field": "device.operator_name", "check": "absent_or_empty", "disp": "直接搬", "note": "no SIM"},
    # ── M. SKAdNetwork（反轉：iOS 本來就該送）
    {"tc": "IOS-81", "field": "skadn.versions", "root": "raw", "check": "array_nonempty",
     "disp": "反轉", "note": "iOS 有送 SKAdNetwork versions（在 req_enc.skadn，與 AOS AND-81「不該有」相反）"},
    {"tc": "IOS-81", "field": "skadn.skadnetids", "root": "raw", "check": "array_nonempty",
     "disp": "反轉", "note": "iOS 有送 skadnetids 陣列（req_enc.skadn）"},
    {"tc": "IOS-81", "field": "skadn.sourceapp", "root": "raw", "check": "present",
     "disp": "反轉", "note": "iOS 有送 SKAdNetwork sourceapp（req_enc.skadn）"},
    # ── L. Privacy Compliance（raw request under req.compliance）
    {"tc": "IOS-77", "field": "req.compliance.gdpr_applies", "root": "raw", "check": "value", "expected": 1, "disp": "直接搬"},
    {"tc": "IOS-78", "field": "req.compliance.force_gdpr_applies", "root": "raw", "check": "value", "expected": 0, "disp": "直接搬"},
    {"tc": "IOS-79", "field": "req.compliance.current_consent_status", "root": "raw", "check": "value", "expected": 1, "disp": "直接搬"},
    {"tc": "IOS-80", "field": "req.compliance.coppa_applies", "root": "raw", "check": "value", "expected": 1, "disp": "直接搬"},
    # ── N. Request Envelope
    {"tc": "IOS-82", "field": "req_ver", "root": "raw", "check": "value", "expected": 2, "disp": "直接搬"},
    {"tc": "IOS-83", "field": "zone_id", "root": "raw", "check": "nonempty",
     "disp": "直接搬", "note": "iOS zone_id（明文頂層，實測如 7906）"},
    {"tc": "IOS-84", "field": "test_mode", "root": "raw", "check": "absent", "disp": "直接搬"},
]

# 狀態切換類 TC → (互斥組, 如何設定, 截圖該證明什麼)。對照 AOS STATE，iOS 用詞。
# 跑這些 TC 時 run_ssp_ios 會導航到對應 iOS 設定頁截圖（state_proof_<group>.png），
# 報告端讓該卡用這張「設定當下」截圖，而非廣告畫面。
IOS_STATE = {
    "IOS-01": ("tracking", "維持允許追蹤（ATT authorized）", "設定→隱私權與安全性→追蹤：允許 App 要求追蹤為開"),
    "IOS-02": ("tracking", "拒絕追蹤（ATT denied）", "追蹤設定顯示已拒絕 / IDFA 全零"),
    "IOS-75": ("tracking", "維持允許追蹤", "追蹤設定顯示允許（lat=0）"),
    "IOS-76": ("tracking", "拒絕追蹤", "追蹤設定顯示拒絕（lat=1）"),
    "IOS-04": ("darkmode", "開啟深色模式", "設定→顯示與亮度：深色已選取"),
    "IOS-05": ("darkmode", "關閉深色模式（淺色）", "設定→顯示與亮度：淺色已選取"),
    "IOS-06": ("charging", "接上電源充電", "設定→電池：顯示充電中"),
    "IOS-07": ("charging", "拔除電源", "設定→電池：未充電"),
    "IOS-08": ("lowpower", "開啟低耗電模式", "設定→電池：低耗電模式為開"),
    "IOS-09": ("lowpower", "關閉低耗電模式", "設定→電池：低耗電模式為關"),
    "IOS-10": ("jailbreak", "使用未越獄裝置", "裝置未越獄"),
    "IOS-11": ("jailbreak", "使用已越獄裝置", "裝置已越獄"),
    "IOS-14": ("vpn", "連上 VPN", "設定→一般→VPN：已連線，狀態列顯示 VPN"),
    "IOS-15": ("vpn", "不連 VPN", "設定→一般→VPN：未連線"),
    "IOS-16": ("batterylevel", "電量充至 100%", "設定→電池：電量百分比"),
    "IOS-17": ("batterylevel", "電量降至極低", "設定→電池：電量百分比"),
    "IOS-19": ("brightness", "亮度調到最低", "設定→顯示與亮度：亮度滑桿最低"),
    "IOS-20": ("brightness", "亮度調到最高", "設定→顯示與亮度：亮度滑桿最高"),
    "IOS-21": ("textsize", "文字大小設為預設", "設定→顯示與亮度→文字大小：預設"),
    "IOS-22": ("textsize", "文字大小設為最大", "設定→顯示與亮度→文字大小：最大"),
    "IOS-25": ("tz", "時區設為台北（GMT+8）", "設定→一般→日期與時間：GMT+8"),
    "IOS-26": ("tz", "時區設為紐約", "設定→一般→日期與時間：美東時區"),
    "IOS-27": ("tz", "時區設為 UTC / 倫敦", "設定→一般→日期與時間：GMT+0"),
    "IOS-45": ("geo", "允許定位並開啟 GPS", "設定→隱私權→定位服務：App 已允許"),
    "IOS-46": ("geo", "拒絕定位", "設定→隱私權→定位服務：App 未允許"),
    "IOS-31": ("language", "語言地區設為 English (US)", "設定→一般→語言與地區：English (US)"),
    "IOS-30": ("language", "開啟語言與地區", "設定→一般→語言與地區：語言 en"),
    "IOS-73": ("language", "開啟語言與地區", "設定→一般→語言與地區：地區 台灣 TW"),
    "IOS-74": ("language", "開啟語言與地區", "設定→一般→語言與地區：locale"),
    "IOS-32": ("language", "開啟鍵盤設定", "設定→一般→鍵盤：輸入語言清單"),
    "IOS-37": ("deviceinfo", "開啟 設定→一般→關於本機", "型號 iPhone"),
    "IOS-69": ("deviceinfo", "開啟 設定→一般→關於本機", "系統 iOS"),
    "IOS-70": ("deviceinfo", "開啟 設定→一般→關於本機", "iOS 版本"),
    "IOS-71": ("deviceinfo", "開啟 設定→一般→關於本機", "機型代號"),
}


# baseline capture 該自動驗的 TC（非狀態切換類；對照 AOS AUTO_TCS 翻譯 + iOS 新增）
AUTO_TCS = {
    "IOS-01", "IOS-28", "IOS-03", "IOS-75",
    "IOS-30", "IOS-32", "IOS-33", "IOS-34", "IOS-35",
    "IOS-36", "IOS-37", "IOS-38", "IOS-39", "IOS-40", "IOS-66", "IOS-67",
    "IOS-68", "IOS-69", "IOS-70", "IOS-71", "IOS-72", "IOS-73", "IOS-74",
    "IOS-44", "IOS-49", "IOS-53", "IOS-54", "IOS-55", "IOS-56",
    "IOS-57", "IOS-58", "IOS-59", "IOS-60", "IOS-62", "IOS-63", "IOS-64",
    "IOS-42", "IOS-43", "IOS-51", "IOS-61",
    "IOS-77", "IOS-78", "IOS-79", "IOS-80", "IOS-81", "IOS-82", "IOS-83", "IOS-84",
}


# ── iOS 加密包解碼（2026-07-21 對真實 bid 確認結構）──────────────────────────────
# iOS bid 明文 body = {zone_id, req_ver, ext_enc, req_enc}：
#   ext_enc → {device, user}                  ← data-signal payload（多數 Signal TC 驗這裡）
#   req_enc → {compliance, app, device, skadn} ← ads SDK 的 req 區塊
# 都用 apr_xorenc 的 ae1 XOR 解碼。normalize 成 bid_inspector 認得的形狀：
#   ext（signal root）＋ req（raw root 的 req.*）＋頂層 skadn / zone_id / req_ver。
def normalize_ios_bid(body):
    if not isinstance(body, dict) or "ext_enc" not in body:
        return body
    from apr_xorenc import decode_ext_enc, decrypt
    out = {}
    for k in ("zone_id", "req_ver", "test_mode"):
        if k in body:
            out[k] = body[k]
    try:
        _, ext = decode_ext_enc(body)          # {device, user}
        if isinstance(ext, dict):
            out["ext"] = ext
    except Exception:
        pass
    try:
        req = json.loads(decrypt(body["req_enc"])) if body.get("req_enc") else None
        if isinstance(req, dict):
            out["req"] = req
            if isinstance(req.get("skadn"), dict):
                out["skadn"] = req["skadn"]     # skadn 提到頂層供 IOS-81（root:raw）驗
    except Exception:
        pass
    return out


# ── inspection / aggregation（引用 IOS_VALIDATORS）─────────────────────────────
def run_inspection(bid, tc_filter=None, reference_ms=None):
    bid = normalize_ios_bid(bid)               # iOS 加密包先解碼展開
    root = _unwrap(bid)
    results = []
    for v in IOS_VALIDATORS:
        if tc_filter and v["tc"] not in tc_filter:
            continue
        if v["check"] == "session_case":
            continue   # 跨 bid 對照，單一 bid 無法判定（同 AOS）
        source = bid if v.get("root") == "raw" else root
        passed, actual, msg = run_validator(source, v, reference_ms=reference_ms)
        note = v.get("note", "")
        if v.get("cal"):
            note = ("[待校準] " + note) if note else "[待校準]"
        results.append({
            "tc": v["tc"], "field": v["field"], "passed": passed,
            "actual": actual, "msg": msg, "note": note,
        })
    return results


def aggregate_round(round_dir):
    """彙總 round 內每個 capture 的 results.json，最新 capture 覆蓋同 (tc,field)。"""
    entries = {}
    for path in glob.glob(os.path.join(round_dir, "*", "results.json")):
        capture = os.path.basename(os.path.dirname(path))
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue
        ts = data.get("captured_at", "")
        for r in data.get("results", []):
            key = (r["tc"], r["field"])
            prev = entries.get(key)
            if prev is None or ts >= prev["_ts"]:
                entries[key] = {**r, "_ts": ts, "capture": capture}
    ordered = []
    for v in IOS_VALIDATORS:
        row = entries.get((v["tc"], v["field"]))
        if row is not None and row not in ordered:
            ordered.append(row)
    return ordered


def format_round_report(rows, round_name=""):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    W = 104
    lines = [
        "=" * W,
        f"  iOS SSP Bid Round Report — {round_name}  —  generated {ts}",
        "  每條 check 取該 round 內最新一次 capture 的結果（[待校準]＝欄位/期望值需實機確認）",
        "=" * W, "",
        f"{'TC':<9}  {'Field':<32}  {'Actual':<22}  {'Result':<7}  Capture",
        f"{'─'*9}  {'─'*32}  {'─'*22}  {'─'*7}  {'─'*24}",
    ]
    passed = failed = 0
    for r in rows:
        status = "PASS ✓" if r["passed"] else "FAIL ✗"
        lines.append(
            f"{r['tc']:<9}  {r['field']:<32}  {_trunc(r['actual'], 20):<22}  {status:<7}  {r['capture']}")
        if not r["passed"] and r.get("note"):
            lines.append(f"{'':9}  ↳ {r['note']}")
        passed += r["passed"]
        failed += not r["passed"]
    covered = {(r["tc"], r["field"]) for r in rows}
    missing = sorted({v["tc"] for v in IOS_VALIDATORS if (v["tc"], v["field"]) not in covered})
    cal = sorted({v["tc"] for v in IOS_VALIDATORS if v.get("cal")})
    lines += [
        "─" * W,
        f"  {passed} passed  /  {failed} failed  /  {len(rows)} checked  /  {len(missing)} 未擷取",
        f"  待校準 TC（{len(cal)}）: {', '.join(cal)}",
    ]
    if missing:
        lines.append(f"  未擷取: {', '.join(missing)}")
    lines.append("=" * W)
    return "\n".join(lines)


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tc_ids", nargs="*", help="TC IDs（e.g. IOS-04）。省略＝全跑")
    p.add_argument("--file", default="/tmp/appier_bid.json", help="bid request JSON")
    p.add_argument("--out", help="report 存檔路徑")
    p.add_argument("--round", help="round 資料夾 — 彙總所有 capture")
    args = p.parse_args()

    if args.round:
        rows = aggregate_round(args.round)
        if not rows:
            sys.exit(f"no capture results.json under {args.round}")
        report = format_round_report(rows, os.path.basename(args.round.rstrip("/")))
        print(report)
        with open(os.path.join(args.round, "round_report.txt"), "w") as f:
            f.write(report + "\n")
        return

    try:
        with open(args.file) as f:
            bid = json.load(f)
    except FileNotFoundError:
        sys.exit(f"bid file not found: {args.file}")
    except json.JSONDecodeError as e:
        sys.exit(f"invalid JSON in {args.file}: {e}")

    tc_filter = set(args.tc_ids) if args.tc_ids else None
    results = run_inspection(bid, tc_filter)
    report = format_report(results, args.file)
    print(report)
    if args.out:
        with open(args.out, "w") as f:
            f.write(report + "\n")
        print(f"\n→ saved: {args.out}")


if __name__ == "__main__":
    main()
