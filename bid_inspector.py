#!/usr/bin/env python3
"""
bid_inspector.py — SSP SDK bid request field validator

Field names/paths and expected values are taken from the "Android TCs" tab of
the SSP SDK Signal QA Test Plan:
    https://docs.google.com/spreadsheets/d/1_9ZcFgDE5sHbsqdzvdBFacyXcxF4gGUS5t-fPAuA-sg
cross-checked against the staging SDK v2 API spec:
    https://adx-stg.apx.appier.net/docs/index.html#/SDK%20v2/post_v2_sdk_aos_ad
    (POST /v2/sdk/aos/ad, schema controllers.SwaggerSDKBidRequest)

Wrapper structure confirmed from SDK source (appier-ads-android
AdRequestBodyBuilder.build() + appier-ads-data-signal SignalSerializer,
read 2026-07-09):
    {req_ver: 2, zone_id, w, h, interstitial?, test_mode?,
     req: {app, device, compliance},   <- ads SDK 自己組的 bid 參數
     ext: {app, device, user}}         <- data-signal payload（本 QA 的驗證對象）
The signal fields these TCs validate live under "ext" — _unwrap() prefers ext,
falls back to the bid itself (raw payload captured from the [AppierDataSignal]
logcat line), then req. Encryption is currently disabled in SignalManager
(fetchKeyAsync / encryptor.encrypt commented out), so ext is plaintext JSON;
if ext arrives as a string, encryption was re-enabled and this tool can't
inspect it offline.

Type conflicts in the TC sheet, resolved by reading the SDK implementation
(sheet needs updating; swagger was right on both):
  - device.charging: SDK sends integer 0/1 (SignalSerializer.booleanToInt),
    matching swagger. Sheet's boolean true/false is stale.
  - device.conntype: SDK sends string enum (wifi / cellular_4g / cellular_5g /
    ..., SignalSerializer.mapConnectionType), matching swagger. Sheet's OpenRTB
    integer codes are stale.

Fields the SDK currently hardcodes (per SignalSerializer) — these TCs will
FAIL/PASS trivially until RD implements them:
  - vpn, ip, ipv6, latency: always null
  - gyroscope, accelerometer, impression_history: always []
  - applist / iaphistory: key always present (empty array when nothing)

Unit notes from collectors: mem_*/disk_* are BYTES (MemoryStorageCollector),
session_duration / app_duration are MILLISECONDS (AppLifecycleTracker),
volume & screen_bright are 0.0-1.0 floats. org.json strips trailing ".0"
when serializing (1.0f → 1), so float expectations accept numerically-equal
ints.

Cat L (Privacy Compliance — gdpr_applies, force_gdpr_applies, current_consent_status,
coppa_applies / AND-77~80) is intentionally not implemented: the TC sheet itself
states the pass/fail criteria are pending RD confirmation of trigger method.

Usage:
    python bid_inspector.py                       # validate all TCs
    python bid_inspector.py AND-04                # single TC
    python bid_inspector.py AND-04 AND-46         # multiple TCs
    python bid_inspector.py --file /path/bid.json # specify input file
    python bid_inspector.py --out /path/report.txt
"""

import glob
import json
import os
import re
import sys
import time
from datetime import datetime

# ── request wrapper unwrap ────────────────────────────────────────────────────

def _unwrap(bid):
    """Locate the data-signal payload {app, device, user}.

    Signal fields live under top-level "ext" in real bid traffic; the bid
    file may also be the raw payload itself (from the [AppierDataSignal]
    logcat line). "req" is the ads SDK's own params — last-resort fallback
    only, most signal TCs will report missing against it.
    """
    if isinstance(bid, dict):
        ext = bid.get("ext")
        if isinstance(ext, str):
            sys.exit(
                "ext is a string — data-signal encryption appears to be "
                "re-enabled in the SDK; plaintext inspection is not possible."
            )
        if isinstance(ext, dict) and ({"app", "device", "user"} & ext.keys()):
            return ext
        if {"device", "user"} & bid.keys():
            return bid
        if isinstance(bid.get("req"), dict):
            return bid["req"]
    return bid


# ── field path resolver ───────────────────────────────────────────────────────

def get_field(bid, path):
    """Resolve dotted path against the unwrapped bid object."""
    parts = path.split(".")
    obj = bid
    for part in parts:
        if not isinstance(obj, dict) or part not in obj:
            return None, False
        obj = obj[part]
    return obj, True


# ── regex patterns ────────────────────────────────────────────────────────────

UUID_RE      = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Locale.toLanguageTag() can emit script/region subtags (zh-Hant-TW) or a bare
# language (en); input-method subtype locales may use underscores (en_US)
BCP47_RE     = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
INPUT_LANG_RE = re.compile(r"^[A-Za-z]{2,3}([_-][A-Za-z0-9]{2,8})*$")
ISO639_RE    = re.compile(r"^[a-z]{2}$")
CELL_4G5G_RE = re.compile(r"^cellular_[45]g$")
IPV4_RE      = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
SEMVER_RE    = re.compile(r"^\d+\.\d+\.\d+$")
ANDROID_OS_RE = re.compile(r"^android$", re.IGNORECASE)
ZERO_UUID    = "00000000-0000-0000-0000-000000000000"


# ── validator dispatch ────────────────────────────────────────────────────────

def run_validator(bid, v):
    """Returns (passed: bool, actual, message: str)."""
    field = v["field"]
    check = v["check"]
    value, found = get_field(bid, field)

    # checks that tolerate an absent field must run before the generic
    # "missing" gate below
    if check == "absent":
        if not found or value is None:
            return True, None, "absent ✓"
        return False, value, "expected absent"

    if check == "present":
        # 只確認欄位存在，值/空與否都可（例：applist 能拿多少算多少）
        if found:
            n = len(value) if isinstance(value, (list, dict, str)) else None
            return True, value, ("欄位存在 ✓" + (f"（{n} 項）" if n is not None else ""))
        return False, None, "欄位不存在"

    if check == "absent_or_empty":
        if not found or value is None or value == "":
            return True, value, "absent/empty ✓"
        return False, value, "expected absent or empty"

    if check == "value_or_absent":
        if not found or value is None:
            return True, value, "absent ✓"
        exp = v["expected"]
        if value == exp:
            return True, value, f"= {exp!r} ✓"
        return False, value, f"expected {exp!r} or absent"

    if check == "falsy":
        if not found or not value:
            return True, value, "falsy/absent ✓"
        return False, value, "expected falsy/absent"

    if not found or value is None:
        return False, None, "field missing"

    if check == "value":
        exp = v["expected"]
        # sheet repeatedly calls out "wrong type" as its own failure mode
        # (e.g. int 1 sent where bool true expected) — require exact type match
        if isinstance(exp, bool):
            ok = isinstance(value, bool) and value == exp
        elif isinstance(exp, int):
            ok = type(value) is int and value == exp
        elif isinstance(exp, float):
            # org.json serializes 1.0f as "1" (strips trailing .0), so a
            # float expectation must accept a numerically-equal int
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value == exp
        else:
            ok = value == exp
        if ok:
            return True, value, f"= {exp!r} ✓"
        return False, value, f"expected {exp!r}, got {value!r}"

    if check == "regex":
        if isinstance(value, str) and v["pattern"].match(value):
            return True, value, "format ✓"
        return False, value, f"format mismatch ({v['pattern'].pattern})"

    if check == "ipv4_nonzero":
        if isinstance(value, str) and IPV4_RE.match(value) and value != "0.0.0.0":
            return True, value, "valid IPv4, non-zero ✓"
        return False, value, "invalid format or 0.0.0.0"

    if check == "range":
        try:
            n = float(value)
            lo, hi = v["min"], v["max"]
            if lo <= n <= hi:
                return True, value, f"in [{lo}, {hi}] ✓"
            return False, value, f"out of range [{lo}, {hi}]"
        except (TypeError, ValueError):
            return False, value, "not numeric"

    if check == "positive_int":
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return True, value, "> 0 ✓"
        return False, value, "expected positive integer"

    if check == "positive_float":
        try:
            n = float(value)
            if n > 0:
                return True, value, "> 0 ✓"
            return False, value, "expected positive number"
        except (TypeError, ValueError):
            return False, value, "not numeric"

    if check == "nonempty":
        if value and str(value).strip():
            return True, value, "non-empty ✓"
        return False, value, "empty or null"

    if check == "nonempty_notunknown":
        s = str(value).strip().lower()
        if value and s and s != "unknown":
            return True, value, "non-empty ✓"
        return False, value, '"unknown"/empty'

    if check == "truthy":
        return (True, value, "truthy ✓") if value else (False, value, "expected truthy")

    if check == "array_nonempty":
        if isinstance(value, list) and value:
            return True, f"[{len(value)} items]", "non-empty ✓"
        return False, value, "expected non-empty array"

    if check == "array_regex":
        if not isinstance(value, list) or not value:
            return False, value, "expected non-empty array"
        bad = [x for x in value if not isinstance(x, str) or not v["pattern"].match(x)]
        if not bad:
            return True, f"[{len(value)} items]", f"all match ✓"
        return False, value, f"invalid: {bad}"

    if check == "array_number":
        if not isinstance(value, list) or not value:
            return False, value, "expected non-empty array"
        bad = [x for x in value if not isinstance(x, (int, float)) or isinstance(x, bool)]
        if not bad:
            return True, f"[{len(value)} values]", "numeric ✓"
        return False, value, f"{len(bad)} non-numeric elements"

    if check == "array_impression":
        if not isinstance(value, list) or not value:
            return False, value, "expected non-empty array"
        required = {"wintime", "displaytime", "adomain", "bundle",
                    "clicktime", "backgroundtime", "storeviewtime"}
        bad = [e for e in value if not isinstance(e, dict) or not required.issubset(e)]
        if not bad:
            return True, f"[{len(value)} impressions]", "structure ✓"
        return False, value, f"{len(bad)} elements missing keys"

    if check == "leq_field":
        ref, ref_found = get_field(bid, v["ref_field"])
        if not ref_found or ref is None:
            return False, value, f"ref {v['ref_field']} not found"
        try:
            if float(value) <= float(ref):
                return True, value, f"<= {v['ref_field']}={ref} ✓"
            return False, value, f"{value} > {v['ref_field']}={ref}"
        except (TypeError, ValueError):
            return False, value, "not numeric"

    if check == "timestamp_recent":
        try:
            ts_sec = int(value) / 1000
            diff = abs(time.time() - ts_sec)
            if diff < 120:
                return True, value, f"within {int(diff)}s ✓"
            return False, value, f"{int(diff)}s off from now"
        except (TypeError, ValueError):
            return False, value, "not a valid ms timestamp"

    return False, value, f"unknown check '{check}'"


# ── TC validator table (Android TCs tab) ──────────────────────────────────────

VALIDATORS = [
    # ── A. Core Identifiers
    {"tc": "AND-01", "field": "device.ia",  "check": "regex", "pattern": UUID_RE, "note": "GAID opt-in → device.ia 為合法 UUID 且非全零（採集＋格式一次驗完，原 AND-28 已併入）；需 Google Play Services"},
    {"tc": "AND-02", "field": "device.ia",  "check": "value_or_absent", "expected": ZERO_UUID, "note": "GAID opt-out → 全零 UUID 或缺席（AND-01 的相反狀態）"},
    {"tc": "AND-03", "field": "device.ifv", "check": "regex", "pattern": UUID_RE, "note": "App Set ID device.ifv 為合法 UUID（採集＋格式一次驗完，原 AND-29 已併入）；跨啟動穩定性手動確認"},
    {"tc": "AND-75", "field": "device.lat", "check": "falsy", "note": "tracking allowed → 0 or absent; must stay consistent with device.ia opt-in (AND-01)"},
    {"tc": "AND-76", "field": "device.lat", "check": "value", "expected": 1, "note": "tracking denied → must be 1, not absent; must stay consistent with device.ia opt-out (AND-02)"},
    # ── B. Device State - Bool
    {"tc": "AND-04", "field": "device.ext.darkmode",      "check": "value", "expected": True},
    {"tc": "AND-05", "field": "device.ext.darkmode",      "check": "value", "expected": False},
    {"tc": "AND-06", "field": "device.charging",          "check": "value", "expected": 1, "note": "SDK sends int 0/1 (SignalSerializer.booleanToInt), matching swagger — sheet's boolean expectation is stale, update sheet"},
    {"tc": "AND-07", "field": "device.charging",          "check": "value", "expected": 0, "note": "see AND-06 type note"},
    {"tc": "AND-08", "field": "device.ext.battery_saver", "check": "value", "expected": True},
    {"tc": "AND-09", "field": "device.ext.battery_saver", "check": "value", "expected": False},
    {"tc": "AND-10", "field": "device.ext.jailbreak",     "check": "value", "expected": False, "note": "Blocked — test device is rooted; needs non-root device or non-rooted AVD"},
    {"tc": "AND-11", "field": "device.ext.jailbreak",     "check": "value", "expected": True},
    {"tc": "AND-12", "field": "device.ext.emulator",      "check": "value", "expected": True,  "note": "AVD"},
    {"tc": "AND-13", "field": "device.ext.emulator",      "check": "value", "expected": False, "note": "real device"},
    {"tc": "AND-14", "field": "device.ext.vpn", "check": "truthy", "note": "SDK hardcodes vpn=null (SignalSerializer) — will FAIL until RD implements VPN detection"},
    {"tc": "AND-15", "field": "device.ext.vpn", "check": "falsy",  "note": "VPN inactive — false or absent (passes trivially while SDK hardcodes null)"},
    # ── C. Device State - Numeric
    {"tc": "AND-16", "field": "device.batterylevel",      "check": "value", "expected": 100},
    {"tc": "AND-17", "field": "device.batterylevel",      "check": "value", "expected": 0},
    {"tc": "AND-19", "field": "device.ext.screen_bright", "check": "value", "expected": 0.0},
    {"tc": "AND-20", "field": "device.ext.screen_bright", "check": "value", "expected": 1.0},
    {"tc": "AND-21", "field": "device.ext.fontscale",     "check": "value", "expected": 1.0},
    {"tc": "AND-22", "field": "device.ext.fontscale",     "check": "value", "expected": 1.5},
    {"tc": "AND-23", "field": "device.ext.volume",        "check": "value", "expected": 0.0, "note": "STREAM_MUSIC confirmed in DisplayCollector; value = volume/max as 0.0-1.0 float"},
    {"tc": "AND-24", "field": "device.ext.volume",        "check": "value", "expected": 1.0},
    {"tc": "AND-25", "field": "device.utcoffset", "check": "value", "expected": 480,  "note": "Asia/Taipei UTC+8"},
    {"tc": "AND-26", "field": "device.utcoffset", "check": "range", "min": -300, "max": -240, "note": "America/New_York EST/EDT"},
    {"tc": "AND-27", "field": "device.utcoffset", "check": "value", "expected": 0,    "note": "UTC"},
    # ── D. Device / App State - Format
    {"tc": "AND-30", "field": "device.lang",       "check": "regex", "pattern": ISO639_RE, "note": "ISO-639-1 2-char lowercase, no region suffix"},
    {"tc": "AND-31", "field": "device.langb",      "check": "regex", "pattern": BCP47_RE,  "note": "BCP47 e.g. en-US"},
    {"tc": "AND-32", "field": "device.input_lang", "check": "array_regex", "pattern": INPUT_LANG_RE, "note": "keyboard input languages, not display language; IME subtype locales may use underscores (en_US)"},
    {"tc": "AND-33", "field": "app.ver", "check": "regex", "pattern": SEMVER_RE, "note": "semver, no v-prefix"},
    {"tc": "AND-34", "field": "app.displaymanager",    "check": "nonempty", "note": "SDK currently sends placeholder \"appier\" (SignalSerializer TODO — backend meaning unconfirmed)"},
    {"tc": "AND-35", "field": "app.displaymanagerver", "check": "nonempty", "note": "SDK sends data-signal BuildConfig.VERSION_NAME (placeholder per serializer TODO)"},
    {"tc": "AND-36", "field": "device.make",  "check": "nonempty_notunknown"},
    {"tc": "AND-37", "field": "device.model", "check": "nonempty_notunknown"},
    {"tc": "AND-38", "field": "device.ip",    "check": "ipv4_nonzero", "note": "SDK hardcodes ip=null (server derives IP) — validate via Production Echo Server (adx.apx.appier.net), not the bid body; FAIL here is expected"},
    {"tc": "AND-39", "field": "device.ipv6",  "check": "nonempty", "note": "SDK hardcodes ipv6=null — same server-side story as AND-38. Also Blocked — needs 4G/5G SIM device; echo server (adx6.apx.appier.net) is ready."},
    {"tc": "AND-40", "field": "device.conntype", "check": "value", "expected": "wifi", "note": "SDK sends string enum (SignalSerializer.mapConnectionType), matching swagger — sheet's OpenRTB int codes are stale, update sheet"},
    {"tc": "AND-41", "field": "device.conntype", "check": "regex", "pattern": CELL_4G5G_RE, "note": "cellular_4g / cellular_5g — see AND-40 type note. Blocked — no SIM device available on team yet"},
    {"tc": "AND-66", "field": "app.bundle",      "check": "nonempty", "note": "compare against known test app applicationId"},
    {"tc": "AND-67", "field": "app.sdk_version", "check": "nonempty", "note": "in ext this is the DATA-SIGNAL SDK version (argus BuildConfig), not ads SDK 2.2.0 (ADQA-1857) — ads SDK version lives at req.app.sdk_version"},
    {"tc": "AND-68", "field": "device.type", "check": "nonempty", "note": "SDK sends \"phone\" or \"tablet\" (DeviceInfoCollector.getDeviceType, screenLayout-based)"},
    {"tc": "AND-69", "field": "device.os",   "check": "regex", "pattern": ANDROID_OS_RE, "note": "SDK sends exactly \"Android\" (SignalSerializer)"},
    {"tc": "AND-70", "field": "device.osv",  "check": "nonempty", "note": "compare against device's actual OS version manually"},
    {"tc": "AND-71", "field": "device.hwv",  "check": "nonempty", "note": "SDK maps hwv = Build.MODEL (same value as device.model) — flag to RD if sheet expects Build.HARDWARE"},
    {"tc": "AND-73", "field": "device.country", "check": "nonempty", "note": "confirm expected format (ISO 3166-1 alpha-2/alpha-3) with RD"},
    {"tc": "AND-74", "field": "device.locale",  "check": "nonempty", "note": "confirm overlap with device.langb with RD"},
    # ── E. Device State - Arrays
    {"tc": "AND-42", "field": "device.ext.gyroscope",     "check": "array_number", "note": "SDK hardcodes [] (collector not implemented) — will FAIL until RD implements"},
    {"tc": "AND-43", "field": "device.ext.accelerometer", "check": "array_number", "note": "SDK hardcodes [] (collector not implemented) — will FAIL until RD implements"},
    {"tc": "AND-44", "field": "device.ext.boottime",      "check": "positive_int", "note": "epoch-ms of most recent boot event (BootEventCollector, lastOrNull); null when no boot records yet"},
    # ── F. Geolocation
    {"tc": "AND-45", "field": "device.geo_lat", "check": "range",  "min": -90.0, "max": 90.0,   "note": "GPS granted"},
    {"tc": "AND-45", "field": "device.geo_lon", "check": "range",  "min": -180.0, "max": 180.0,  "note": "GPS granted"},
    {"tc": "AND-46", "field": "device.geo_lat", "check": "absent", "note": "P0 — GPS denied → lat absent"},
    {"tc": "AND-46", "field": "device.geo_lon", "check": "absent", "note": "P0 — GPS denied → lon absent"},
    # ── G. In-Session
    {"tc": "AND-47", "field": "user.session_duration",     "check": "range", "min": 30_000, "max": 99_999_000, "note": "ms (AppLifecycleTracker) — sheet's 30-99999 assumed seconds, update sheet"},
    {"tc": "AND-48", "field": "user.session_duration",     "check": "range", "min": 0,  "max": 5_000, "note": "cold start; ms"},
    {"tc": "AND-49", "field": "user.app_init_time",        "check": "timestamp_recent"},
    {"tc": "AND-50", "field": "user.last_foreground_time", "check": "array_nonempty"},
    {"tc": "AND-50", "field": "user.last_background_time", "check": "array_nonempty"},
    {"tc": "AND-51", "field": "user.impression_history",   "check": "array_impression", "note": "SDK hardcodes [] (not implemented) — will FAIL until RD implements"},
    {"tc": "AND-52", "field": "user.app_duration",         "check": "range", "min": 30_000, "max": 99_999_000, "note": "ms (foregroundTimeMs)"},
    # ── H. Memory / Disk
    {"tc": "AND-53", "field": "device.ext.mem_total",     "check": "range",     "min": 2 * 1024**3,  "max": 16 * 1024**3,  "note": "bytes (MemoryStorageCollector) — sheet's MB range is stale, update sheet"},
    {"tc": "AND-54", "field": "device.ext.mem_available", "check": "leq_field", "ref_field": "device.ext.mem_total"},
    {"tc": "AND-55", "field": "device.ext.disk_total",    "check": "range",     "min": 32 * 1024**3, "max": 512 * 1024**3, "note": "bytes — sheet's MB range is stale, update sheet"},
    {"tc": "AND-56", "field": "device.ext.disk_free",     "check": "leq_field", "ref_field": "device.ext.disk_total"},
    # ── I. Screen / Display
    {"tc": "AND-57", "field": "device.sw",      "check": "positive_int"},
    {"tc": "AND-58", "field": "device.sh",      "check": "positive_int"},
    {"tc": "AND-59", "field": "device.ppi",     "check": "positive_int"},
    {"tc": "AND-60", "field": "device.pxratio", "check": "positive_float", "note": "typical 2.0-3.5, no fixed upper bound"},
    # ── K. Network Latency
    {"tc": "AND-61", "field": "device.ext.latency", "check": "positive_int", "note": "SDK hardcodes latency=null. Also Blocked — Echo Server endpoint only returns IP, no latency measurement available yet"},
    # ── J. Negative / Absent
    {"tc": "AND-62", "field": "device.ext.applist",    "check": "present", "note": "RD 定：SDK 採集 applist 為可接受行為，能拿多少算多少、拿不到也 OK → 只確認欄位存在即可"},
    {"tc": "AND-63", "field": "device.ext.iaphistory", "check": "absent", "note": "same always-emitted-key issue as AND-62"},
    {"tc": "AND-64", "field": "device.carrier", "check": "absent_or_empty", "note": "no SIM; Blocked — confirm SIM simulation capability with RD"},
    {"tc": "AND-64", "field": "device.mccmnc",  "check": "absent_or_empty", "note": "no SIM"},
    {"tc": "AND-65", "field": "app.ext.islatestver", "check": "absent", "note": "app.ext doesn't exist at all in v2 schema — trivially absent; presence would indicate schema mismatch"},
    {"tc": "AND-72", "field": "device.operator",      "check": "absent_or_empty", "note": "no SIM"},
    {"tc": "AND-72", "field": "device.operator_name", "check": "absent_or_empty", "note": "no SIM"},
    # ── M. SKAdNetwork
    {"tc": "AND-81", "field": "skadn.sourceapp",  "check": "absent", "note": "SKAdNetwork is iOS-only; Android should never send this"},
    {"tc": "AND-81", "field": "skadn.versions",   "check": "absent", "note": "SKAdNetwork is iOS-only; Android should never send this"},
    {"tc": "AND-81", "field": "skadn.skadnetids", "check": "absent", "note": "SKAdNetwork is iOS-only; Android should never send this"},
]


# ── report ────────────────────────────────────────────────────────────────────

def _trunc(val, n=38):
    s = str(val) if not isinstance(val, str) else val
    return (s[:n] + "…") if len(s) > n else s


def run_inspection(bid, tc_filter=None):
    root = _unwrap(bid)
    results = []
    for v in VALIDATORS:
        if tc_filter and v["tc"] not in tc_filter:
            continue
        passed, actual, msg = run_validator(root, v)
        results.append({
            "tc":     v["tc"],
            "field":  v["field"],
            "passed": passed,
            "actual": actual,
            "msg":    msg,
            "note":   v.get("note", ""),
        })
    return results


def format_report(results, bid_file="", header=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    W = 76
    lines = [
        "=" * W,
        f"  SSP SDK Bid Inspector  —  {ts}",
        (f"  Source: {bid_file}" if bid_file else ""),
        (f"  {header}" if header else ""),
        "=" * W,
        "",
        f"{'TC':<10}  {'Field':<34}  {'Actual':<26}  Result",
        f"{'─'*10}  {'─'*34}  {'─'*26}  {'─'*10}",
    ]
    passed = failed = 0
    for r in results:
        status = "PASS ✓" if r["passed"] else "FAIL ✗"
        note   = f"  ← {r['note']}" if r["note"] and not r["passed"] else ""
        lines.append(
            f"{r['tc']:<10}  {r['field']:<34}  {_trunc(r['actual']):<26}  {status}{note}"
        )
        if r["passed"]:
            passed += 1
        else:
            failed += 1
    lines += [
        f"{'─'*W}",
        f"  {passed} passed  /  {failed} failed  /  {passed + failed} total",
        "=" * W,
    ]
    return "\n".join(l for l in lines if l is not None)


# ── round aggregation ─────────────────────────────────────────────────────────

def aggregate_round(round_dir):
    """Merge every capture's results.json in a round folder.

    Latest capture wins per (tc, field) — a targeted state capture (e.g.
    AND-04 darkmode-on) overrides the baseline capture's result for that
    check. Returns rows in VALIDATORS order, each with a "capture" key.
    """
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
    for v in VALIDATORS:
        row = entries.get((v["tc"], v["field"]))
        if row is not None and row not in ordered:
            ordered.append(row)
    return ordered


def format_round_report(rows, round_name=""):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    W = 104
    lines = [
        "=" * W,
        f"  SSP SDK Round Report — {round_name}  —  generated {ts}",
        "  每條 check 取該 round 內最新一次 capture 的結果",
        "=" * W,
        "",
        f"{'TC':<8}  {'Field':<32}  {'Actual':<24}  {'Result':<7}  Capture",
        f"{'─'*8}  {'─'*32}  {'─'*24}  {'─'*7}  {'─'*26}",
    ]
    passed = failed = 0
    for r in rows:
        status = "PASS ✓" if r["passed"] else "FAIL ✗"
        lines.append(
            f"{r['tc']:<8}  {r['field']:<32}  {_trunc(r['actual'], 22):<24}  {status:<7}  {r['capture']}"
        )
        if not r["passed"] and r.get("note"):
            lines.append(f"{'':8}  ↳ {r['note']}")
        if r["passed"]:
            passed += 1
        else:
            failed += 1
    covered = {(r["tc"], r["field"]) for r in rows}
    missing_tcs = sorted({v["tc"] for v in VALIDATORS if (v["tc"], v["field"]) not in covered})
    lines += [
        "─" * W,
        f"  {passed} passed  /  {failed} failed  /  {len(rows)} checked"
        f"  /  {len(missing_tcs)} TC not yet captured",
    ]
    if missing_tcs:
        lines.append(f"  not yet captured: {', '.join(missing_tcs)}")
    lines.append("=" * W)
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tc_ids", nargs="*", help="TC IDs (e.g. AND-04). Omit to run all.")
    p.add_argument("--file", default="/tmp/appier_bid.json", help="bid request JSON file")
    p.add_argument("--out",  help="save report to this path")
    p.add_argument("--round", help="round evidence folder — aggregate all captures into round_report.txt")
    args = p.parse_args()

    if args.round:
        rows = aggregate_round(args.round)
        if not rows:
            sys.exit(f"no capture results.json found under {args.round}")
        report = format_round_report(rows, os.path.basename(args.round.rstrip("/")))
        print(report)
        out = os.path.join(args.round, "round_report.txt")
        with open(out, "w") as f:
            f.write(report + "\n")
        print(f"\n→ saved: {out}")
        return

    try:
        with open(args.file) as f:
            bid = json.load(f)
    except FileNotFoundError:
        sys.exit(f"bid file not found: {args.file}\n(run mitmdump + trigger app first)")
    except json.JSONDecodeError as e:
        sys.exit(f"invalid JSON in {args.file}: {e}")

    tc_filter = set(args.tc_ids) if args.tc_ids else None
    results   = run_inspection(bid, tc_filter)
    report    = format_report(results, args.file)

    print(report)

    if args.out:
        with open(args.out, "w") as f:
            f.write(report + "\n")
        print(f"\n→ saved: {args.out}")


if __name__ == "__main__":
    main()
